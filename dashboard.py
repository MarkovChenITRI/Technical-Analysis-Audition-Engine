"""
Dashboard (WebUI Adapter) — 台股技術分析版
─────────────────────────────────────────────
本地 demo：表單 → 直接 import engine.run_pipeline → JSON → 前端 Chart.js 繪圖。
入口位於根目錄；templates / static 資產位於 webui/。

啟動：
    python dashboard.py
    瀏覽器開 http://127.0.0.1:5000
"""
from __future__ import annotations

import logging
import sys
import time
import traceback
import yaml
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'cloud_function'))

from flask import Flask, jsonify, render_template, request  # noqa: E402

from data import (  # noqa: E402
    TradingViewSessionExpired, TPE_TZ,
    push_session_expired_alert,
)
from engine import ConfigError, run_pipeline  # noqa: E402

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
for noisy in ('yfinance', 'urllib3', 'requests', 'peewee'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger('dashboard')

CONFIG_YAML = ROOT / 'cloud_function' / 'config.yaml'

_STATUS_MAP = {
    'TRADINGVIEW_SESSION_EXPIRED': 422,
    'CONFIG_ERROR': 400,
    'INVALID_REQUEST': 400,
    'NO_DATA': 422,
    'INTERNAL': 500,
}


app = Flask(
    __name__,
    template_folder=str(ROOT / 'webui' / 'templates'),
    static_folder=str(ROOT / 'webui' / 'static'),
)


@app.after_request
def _log_request(response):
    logger.info('%s %s → %d', request.method, request.path, response.status_code)
    return response


def _err(code: str, message: str, remediation: str = '', details: dict | None = None):
    return jsonify({
        'ok': False,
        'error': {'code': code, 'message': message, 'remediation': remediation, 'details': details or {}},
    }), _STATUS_MAP.get(code, 500)


@app.route('/')
def index():
    logger.info('GET / → render index.html')
    return render_template('index.html')


@app.get('/api/config')
def api_get_config():
    logger.info('GET /api/config')
    try:
        with open(CONFIG_YAML, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error('讀取 config.yaml 失敗: %s', e)
        return _err('INTERNAL', f'無法讀取 config.yaml: {e}')
    logger.info('config.yaml 讀取成功')
    return jsonify({'ok': True, 'config': cfg})


@app.post('/api/config')
def api_save_config():
    body = request.get_json(silent=True) or {}
    sections = [k for k in ('tradingview', 'line', 'backtest') if k in body]
    logger.info('POST /api/config → 更新區段: %s', sections)
    try:
        with open(CONFIG_YAML, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error('讀取 config.yaml 失敗: %s', e)
        return _err('INTERNAL', f'無法讀取 config.yaml: {e}')

    for section in ('tradingview', 'line'):
        if section in body:
            cfg[section] = body[section]
    if 'backtest' in body:
        bt = cfg.setdefault('backtest', {})
        for k, v in body['backtest'].items():
            if isinstance(bt.get(k), dict) and isinstance(v, dict):
                bt[k] = {**bt[k], **v}
            else:
                bt[k] = v
        # 鐵則：market 強制 tw
        bt['market'] = 'tw'

    try:
        with open(CONFIG_YAML, 'w', encoding='utf-8') as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    except Exception as e:
        logger.error('寫入 config.yaml 失敗: %s', e)
        return _err('INTERNAL', f'無法寫入 config.yaml: {e}')
    logger.info('config.yaml 寫入成功')
    return jsonify({'ok': True})


@app.post('/api/backtest')
def api_backtest():
    started = time.perf_counter()
    body = request.get_json(silent=True) or {}
    logger.info('POST /api/backtest → params=%s', body.get('backtest', {}))

    backtest_params = body.get('backtest') or {}
    if not isinstance(backtest_params, dict):
        return _err('INVALID_REQUEST', 'backtest 必須為物件')

    try:
        ctx = run_pipeline(backtest_params)
    except TradingViewSessionExpired as e:
        push_session_expired_alert(e.expires_at)
        return _err('TRADINGVIEW_SESSION_EXPIRED',
                    f'TradingView session 已過期（預計到期日 {e.expires_at}）',
                    remediation=e.detail, details={'expires_at': e.expires_at})
    except ConfigError as e:
        logger.warning('CONFIG_ERROR: %s', e)
        return _err('CONFIG_ERROR', str(e))
    except RuntimeError as e:
        logger.exception('NO_DATA: %s', e)
        return _err('NO_DATA', str(e))
    except Exception as e:
        logger.exception('未預期錯誤')
        return _err('INTERNAL', f'{type(e).__name__}: {e}',
                    details={'traceback': traceback.format_exc()[-1000:]})

    result = ctx['result']
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    logger.info('回測完成 %d ms', elapsed_ms)

    return jsonify({
        'ok': True,
        'result': result.to_dict(),
        'holdings': ctx['current_holdings'],
        'trades': result.trades,
        'equity_curve': result.equity_curve,
        'benchmark_curve': ctx['benchmark_curve'],
        'benchmark_name': ctx['benchmark_name'],
        'market_regime': ctx.get('market_regime', []),
        'meta': {
            'execution_time_ms': elapsed_ms,
            'portfolio_source': ctx['portfolio_source'],
            'symbols_count': ctx['symbols_count'],
            'margin_data_loaded': ctx.get('margin_data_loaded', False),
        },
    })


from datetime import timedelta


@app.get('/api/stock_chart/<symbol>')
def api_stock_chart(symbol):
    """個股 K 線 + 融資融券 + 大戶持股資料。"""
    import yfinance as yf
    import requests as req

    days = min(int(request.args.get('days', 120)), 365)
    stock_id = symbol.replace('.TW', '').replace('.TWO', '')
    end_date = datetime.today().strftime('%Y-%m-%d')
    start_date = (datetime.today() - timedelta(days=days + 10)).strftime('%Y-%m-%d')

    # FinMind token
    try:
        with open(CONFIG_YAML, encoding='utf-8') as f:
            _cfg = yaml.safe_load(f) or {}
        fm_token = _cfg.get('finmind', {}).get('token', '')
    except Exception:
        fm_token = ''
    fm_params = {'token': fm_token} if fm_token else {}

    # 1. OHLCV ────────────────────────────────────────────────────────────
    import math
    ohlcv = []
    try:
        hist = yf.Ticker(symbol).history(start=start_date, end=end_date)
        hist.index = hist.index.tz_localize(None)
        for idx, row in hist.iterrows():
            o, h, l, c, v = row.Open, row.High, row.Low, row.Close, row.Volume
            # 跳過任何含 NaN 或全零的行
            if any(math.isnan(x) for x in (o, h, l, c)) or (o == 0 and v == 0):
                continue
            ohlcv.append({
                'date': idx.strftime('%Y-%m-%d'),
                'open':   round(float(o), 2),
                'high':   round(float(h), 2),
                'low':    round(float(l), 2),
                'close':  round(float(c), 2),
                'volume': int(v) if not math.isnan(v) else 0,
            })
    except Exception as e:
        logger.warning('OHLCV 抓取失敗 %s: %s', symbol, e)

    # 2. 融資融券 ─────────────────────────────────────────────────────────
    margin = []
    try:
        r = req.get('https://api.finmindtrade.com/api/v4/data', params={
            'dataset': 'TaiwanStockMarginPurchaseShortSale',
            'data_id': stock_id, 'start_date': start_date, 'end_date': end_date,
            **fm_params,
        }, timeout=30)
        raw = r.json()
        if raw.get('status') == 200:
            for row in raw.get('data', []):
                mb = float(row.get('MarginPurchaseTodayBalance', 0) or 0)
                mp = float(row.get('MarginPurchaseYesterdayBalance', 0) or 0)
                sb = float(row.get('ShortSaleTodayBalance', 0) or 0)
                sp = float(row.get('ShortSaleYesterdayBalance', 0) or 0)
                margin.append({
                    'date': row['date'],
                    'margin_balance': mb,
                    'margin_chg_pct': round((mb - mp) / mp * 100, 2) if mp > 0 else 0.0,
                    'short_balance': sb,
                    'short_chg_pct': round((sb - sp) / sp * 100, 2) if sp > 0 else 0.0,
                })
    except Exception as e:
        logger.warning('融資融券抓取失敗 %s: %s', symbol, e)

    # 3. 外資持股比例（替代集保分級，免費帳號可用）──────────────────────
    holding = []
    try:
        h_start = (datetime.today() - timedelta(days=400)).strftime('%Y-%m-%d')
        r2 = req.get('https://api.finmindtrade.com/api/v4/data', params={
            'dataset': 'TaiwanStockShareholding',
            'data_id': stock_id, 'start_date': h_start, 'end_date': end_date,
            **fm_params,
        }, timeout=30)
        raw2 = r2.json()
        if raw2.get('status') == 200:
            for row in raw2.get('data', []):
                d = row.get('date', '')
                pct = float(row.get('ForeignInvestmentSharesRatio', 0) or 0)
                if d:
                    holding.append({'date': d, 'pct': round(pct, 2)})
    except Exception as e:
        logger.warning('外資持股抓取失敗 %s: %s', symbol, e)

    return jsonify({'ok': True, 'symbol': symbol,
                    'ohlcv': ohlcv, 'margin': margin, 'holding': holding})


if __name__ == '__main__':
    logger.info('啟動 TA Audition Engine WebUI → http://127.0.0.1:5000')
    app.run(debug=True, port=5000)
