"""
Cloud Function entry: hello_http（台股技術分析版）
─────────────────────────────────────────────
- Runtime: Python 3.12 (Ubuntu 22 Full)
- Entry point: hello_http
- Trigger: HTTP POST application/json

此檔僅做 HTTP Adapter：解析 request → 呼叫 engine.run_pipeline → 序列化回應 + LINE 推播。
所有 Domain 邏輯位於 engine.py / data.py。
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

import functions_framework

from data import (
    TPE_TZ, TradingViewSessionExpired, _load_session_meta,
    push_line_message, push_session_expired_alert,
    push_session_expiring_soon_alert, push_error_alert,
)
from engine import ConfigError, run_pipeline

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s %(message)s',
    stream=sys.stdout,
    force=True,
)
for noisy in ('yfinance', 'urllib3', 'requests', 'peewee', 'werkzeug'):
    logging.getLogger(noisy).setLevel(logging.WARNING)

logger = logging.getLogger('hello_http')


# =============================================================================
# LINE 訊息格式化（台股版）
# =============================================================================
def format_line_message(result, current_holdings: list, start_dt, end_dt) -> str:
    summary = result.to_dict()
    SEP = '─' * 12
    lines = [
        f'🇹🇼 {end_dt.strftime("%Y-%m-%d")} 台股每日建議',
        f'設立日：{start_dt.strftime("%Y-%m-%d")}',
        SEP,
        '【系統績效】',
        f'總報酬  {summary["total_return"]}',
        f'年化    {summary["annualized_return"]}',
        f'最大回撤 {summary["max_drawdown"]}',
        f'Sharpe  {summary["sharpe_ratio"]}',
        f'勝率    {summary["win_rate"]}',
        f'交易    {summary["total_trades"]} 筆',
        SEP,
        '【交易訊號（近3日）】',
    ]
    cutoff = (end_dt - timedelta(days=3)).strftime('%Y-%m-%d')
    recent = [t for t in result.trades if t['date'] >= cutoff]
    if recent:
        for t in recent:
            icon = '🟢' if t['type'] == 'buy' else '🔴'
            action = '買入' if t['type'] == 'buy' else '賣出'
            lines.append(f'{icon} {t["date"][5:]} {action} {t["symbol"]}')
    else:
        lines.append('（無訊號）')
    lines += [SEP, '【現有倉位】']
    if current_holdings:
        for h in sorted(current_holdings, key=lambda x: x['pnl_pct'], reverse=True):
            lines.append(f'🇹🇼 {h["symbol"]:<8} {h["pnl_pct"]:+.1%}')
    else:
        lines.append('（無持倉）')
    return '\n'.join(lines)


# =============================================================================
# 錯誤回應
# =============================================================================
_STATUS_MAP = {
    'TRADINGVIEW_SESSION_EXPIRED': 422,
    'CONFIG_ERROR': 400,
    'INVALID_REQUEST': 400,
    'NO_DATA': 422,
    'INTERNAL': 500,
}


def _error_response(
    code: str, message: str, remediation: str = '', details: Optional[dict] = None,
) -> Tuple[dict, int]:
    body = {
        'ok': False,
        'error': {
            'code': code,
            'message': message,
            'remediation': remediation,
            'details': details or {},
        },
    }
    return body, _STATUS_MAP.get(code, 500)


# =============================================================================
# HTTP entry
# =============================================================================
@functions_framework.http
def hello_http(request):
    """Cloud Function HTTP entry。詳見 docs/API_SPEC.md。"""
    if request.method == 'OPTIONS':
        return ('', 204, {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '3600',
        })

    cors_headers = {'Access-Control-Allow-Origin': '*'}
    started = time.perf_counter()

    body = request.get_json(silent=True) or {}

    backtest_params = body.get('backtest') or {}
    if not isinstance(backtest_params, dict):
        resp, status = _error_response('INVALID_REQUEST', 'backtest 必須為物件')
        return (resp, status, cors_headers)

    notify = body.get('notify') or {}
    if not isinstance(notify, dict):
        resp, status = _error_response('INVALID_REQUEST', 'notify 必須為物件')
        return (resp, status, cors_headers)
    line_enabled = bool(notify.get('line', False))

    try:
        ctx = run_pipeline(backtest_params)
    except TradingViewSessionExpired as e:
        push_session_expired_alert(e.expires_at)
        resp, status = _error_response(
            'TRADINGVIEW_SESSION_EXPIRED',
            f'TradingView session 已過期（預計到期日 {e.expires_at}）',
            e.detail,
            details={'expires_at': e.expires_at},
        )
        return (resp, status, cors_headers)
    except ConfigError as e:
        logger.warning('CONFIG_ERROR: %s', e)
        push_error_alert('CONFIG_ERROR', str(e))
        resp, status = _error_response('CONFIG_ERROR', str(e), '請檢查 config.yaml 的 backtest 區段設定')
        return (resp, status, cors_headers)
    except RuntimeError as e:
        logger.exception('NO_DATA: %s', e)
        push_error_alert('NO_DATA', str(e))
        resp, status = _error_response('NO_DATA', str(e))
        return (resp, status, cors_headers)
    except Exception as e:
        logger.exception('未預期錯誤')
        push_error_alert('INTERNAL', f'{type(e).__name__}: {e}')
        resp, status = _error_response('INTERNAL', f'{type(e).__name__}: {e}')
        return (resp, status, cors_headers)

    result = ctx['result']
    current_holdings = ctx['current_holdings']
    start_dt = ctx['start_dt']
    end_dt = ctx['end_dt']

    # LINE 推播（只在 Cloud Scheduler 傳 "line": true 時執行）
    notifications = {'line': {'sent': False}}
    if line_enabled:
        msg = format_line_message(result, current_holdings, start_dt, end_dt)
        line_result = push_line_message(msg)
        notifications['line'] = line_result

        # session 到期預警（≤7 天才推，附在每日排程之後，避免本地測試/demo.py 誤推）
        expires_at, _, _ = _load_session_meta()
        push_session_expiring_soon_alert(expires_at)

    elapsed_ms = round((time.perf_counter() - started) * 1000)
    now_tpe = datetime.now(tz=TPE_TZ)

    response_body = {
        'ok': True,
        'result': result.to_dict(),
        'holdings': current_holdings,
        'trades': result.trades,
        'equity_curve': result.equity_curve,
        'benchmark_curve': ctx['benchmark_curve'],
        'benchmark_name': ctx['benchmark_name'],
        'meta': {
            'timestamp': now_tpe.isoformat(),
            'execution_time_ms': elapsed_ms,
            'portfolio_source': ctx['portfolio_source'],
            'symbols_count': ctx['symbols_count'],
            'data_range': [
                result.equity_curve[0]['date'] if result.equity_curve else None,
                result.equity_curve[-1]['date'] if result.equity_curve else None,
            ],
            'margin_data_loaded': ctx.get('margin_data_loaded', False),
        },
        'notifications': notifications,
    }
    return (response_body, 200, cors_headers)
