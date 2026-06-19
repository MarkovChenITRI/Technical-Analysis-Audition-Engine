"""
獨立工具腳本：取得 TradingView 觀察清單的產業分組與個股 Dict。

執行：
    python fetch_watchlist_dict.py
    python fetch_watchlist_dict.py --flat      # 輸出扁平 list（所有 ticker）
    python fetch_watchlist_dict.py --json      # 輸出 JSON 格式

輸出結構：
    industry_dict = {
        "半導體": ["2330.TW", "2454.TW", ...],
        "金融":   ["2882.TW", ...],
        ...
    }
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 讓腳本能直接 import cloud_function/
sys.path.insert(0, str(Path(__file__).parent / 'cloud_function'))

from data import fetch_watchlist  # noqa: E402


def build_industry_dict(watchlist: dict) -> dict[str, list[str]]:
    """將 fetch_watchlist() 回傳的 watchlist 攤平為 {industry: [ticker, ...]}。"""
    result: dict[str, list[str]] = {}
    for industry, providers in watchlist.items():
        tickers: list[str] = []
        for ticker_list in providers.values():
            tickers.extend(ticker_list)
        if tickers:
            result[industry] = tickers
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description='取得 TradingView 觀察清單產業 Dict')
    parser.add_argument('--flat', action='store_true', help='輸出扁平 ticker list')
    parser.add_argument('--json', action='store_true', help='以 JSON 格式輸出')
    args = parser.parse_args()

    watchlist, stock_info = fetch_watchlist()
    industry_dict = build_industry_dict(watchlist)

    if args.flat:
        all_tickers = [t for tickers in industry_dict.values() for t in tickers]
        if args.json:
            print(json.dumps(all_tickers, ensure_ascii=False, indent=2))
        else:
            for t in all_tickers:
                print(t)
        return

    if args.json:
        print(json.dumps(industry_dict, ensure_ascii=False, indent=2))
        return

    # ── 預設：人類可讀格式 ──────────────────────────────────────────────────
    total = sum(len(v) for v in industry_dict.values())
    print(f'\nTradingView 觀察清單  共 {len(industry_dict)} 產業 / {total} 檔\n')
    print('industry_dict = {')
    for i, (industry, tickers) in enumerate(industry_dict.items()):
        comma = ',' if i < len(industry_dict) - 1 else ''
        print(f'    "{industry}": {tickers}{comma}')
    print('}')

    print(f'\n# stock_info keys（共 {len(stock_info)} 筆）:')
    for ticker, info in list(stock_info.items())[:5]:
        print(f'#   {ticker}: {info}')
    if len(stock_info) > 5:
        print(f'#   ... 其餘 {len(stock_info) - 5} 筆略')


if __name__ == '__main__':
    main()
