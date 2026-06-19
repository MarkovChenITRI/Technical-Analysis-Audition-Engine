"""
demo.py — GCF endpoint 驗證客戶端（台股技術分析版）

此腳本是 HTTP 客戶端，專責驗證已部署的 GCF endpoint。
不 import engine / data，不依賴 Domain 邏輯。

用法：
    python demo.py <GCF_URL>
    python demo.py https://asia-east1-your-project.cloudfunctions.net/tw-ta-audition-engine
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error


def call_gcf(url: str, payload: dict, timeout: int = 300) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return json.loads(body)
        except Exception:
            return {'ok': False, 'error': {'code': f'HTTP_{e.code}', 'message': body.decode(errors='replace')}}
    except Exception as e:
        return {'ok': False, 'error': {'code': 'CLIENT_ERROR', 'message': str(e)}}


def main():
    if len(sys.argv) < 2:
        print('用法：python demo.py <GCF_URL>')
        print('範例：python demo.py https://asia-east1-your-project.cloudfunctions.net/tw-ta-audition-engine')
        sys.exit(1)

    url = sys.argv[1].rstrip('/')
    print(f'[demo] GCF endpoint: {url}')
    print('[demo] 傳送回測請求（market=tw，不啟用 LINE 推播）…')

    payload = {
        'backtest': {
            'start_date': '2025-01-01',
            'max_positions': 5,
        },
        'notify': {'line': False},
    }

    t0 = time.perf_counter()
    result = call_gcf(url, payload)
    elapsed = round((time.perf_counter() - t0) * 1000)

    print(f'[demo] 收到回應（{elapsed} ms）:')
    if result.get('ok'):
        meta = result.get('meta', {})
        summary = result.get('result', {})
        holdings = result.get('holdings', [])
        print(f'  ✅ 成功')
        print(f'  標的數量  : {meta.get("symbols_count", "?")}')
        print(f'  總報酬    : {summary.get("total_return", "?")}')
        print(f'  年化報酬  : {summary.get("annualized_return", "?")}')
        print(f'  Sharpe    : {summary.get("sharpe_ratio", "?")}')
        print(f'  最大回撤  : {summary.get("max_drawdown", "?")}')
        print(f'  當前持倉  : {len(holdings)} 檔')
        print(f'  執行時間  : {meta.get("execution_time_ms", elapsed)} ms')
        print(f'  融資資料  : {"已載入" if meta.get("margin_data_loaded") else "未啟用"}')
        if holdings:
            print('\n  持倉明細：')
            for h in sorted(holdings, key=lambda x: x['pnl_pct'], reverse=True):
                print(f'    🇹🇼 {h["symbol"]:<12} {h["pnl_pct"]:+.1%}  買入日：{h["buy_date"]}')
    else:
        err = result.get('error', {})
        print(f'  ❌ 失敗：[{err.get("code")}] {err.get("message")}')
        if err.get('remediation'):
            print(f'  修復步驟：{err["remediation"]}')


if __name__ == '__main__':
    main()
