"""
回測引擎（台股技術分析版）：配置驗證、指標計算、回測流程、benchmark、報表格式化。

策略核心：
  Sharpe 排名（主排序）
  + 融資淨增（margin_net_long）：機構資金持續布局
  + 無爆漲（no_parabolic）：確認尚在橫盤蓄積
  賣出：Sharpe 退出 + 嚴格停損（10%）+ 融資崩潰（選用）

依賴：data.py（Money / FX / TWMarginData / 對齊資料 / benchmark 抓取）
"""
from __future__ import annotations

import copy
import logging
import math
import yaml
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from data import (
    CONFIG_FILE, FX, NON_TRADABLE_INDUSTRIES, RISK_FREE_RATE, SHARPE_WINDOW,
    TWMarginData, Money,
    align_data_with_bfill, build_close_df, build_stock_info_from_portfolio,
    fetch_all_stock_data, fetch_benchmark_prices, fetch_watchlist,
    filter_by_market, twd, usd,
)

logger = logging.getLogger(__name__)


# =============================================================================
# 配置：CONDITION_OPTIONS + DEFAULT_CONFIG + load_config
# =============================================================================
CONDITION_OPTIONS = {
    'buy_conditions': {
        # ── 繼承自 Keep-Buying ──────────────────────────────────────────
        'sharpe_rank':      {'params': {'top_n': {'default': 15}}},
        'sharpe_threshold': {'params': {'threshold': {'default': 0.5}}},   # 台股調低
        'sharpe_streak':    {'params': {'days': {'default': 3}, 'top_n': {'default': 10}}},
        'growth_rank':      {'params': {'top_n': {'default': 7}}},
        'growth_streak':    {'params': {'days': {'default': 2}, 'percentile': {'default': 30}}},
        'sort_sharpe':      {'params': {}},
        'sort_margin':      {
            'params': {'days': {'default': 5}},
            'description': '依融資布局強度排序（成長型）：Sharpe 篩選後優先選融資增幅最大的標的',
        },
        'sort_industry':    {'params': {'per_industry': {'default': 2}}},
        # ── 台股新增 ─────────────────────────────────────────────────────
        'margin_net_long':  {
            'params': {
                'days': {'default': 5},              # 近 N 個交易日
                'min_avg_change': {'default': 0.0},  # 融資平均增減率 ≥ 此值
            },
            'description': '融資淨增：近 N 日融資平均增減率 ≥ min_avg_change（機構布局訊號）',
        },
        'no_parabolic':     {
            'params': {
                'lookback': {'default': 10},          # 回看交易日數
                'max_gain': {'default': 0.15},         # 漲幅上限（0.15 = 15%）
            },
            'description': '無爆漲：近 lookback 日漲幅 < max_gain（橫盤蓄積訊號）',
        },
        'industry_rank':    {
            'params': {'top_n': {'default': 1}},  # 產業平均 Sharpe 排名前 N 名
            'description': '產業濾網：候選股所屬產業之平均 Sharpe 須排在全市場前 top_n 名（題材輪動訊號）',
        },
    },
    'sell_conditions': {
        # ── 繼承自 Keep-Buying ──────────────────────────────────────────
        'sharpe_fail':  {'params': {'periods': {'default': 2}, 'top_n': {'default': 15}}},
        'growth_fail':  {'params': {'days': {'default': 5}, 'threshold': {'default': 0}}},
        'not_selected': {'params': {'periods': {'default': 3}}},
        'drawdown':     {'params': {'threshold': {'default': 0.10}, 'from_highest': {'default': False}}},
        'weakness':     {'params': {'rank_k': {'default': 20}, 'periods': {'default': 3}}},
        # ── 台股新增 ─────────────────────────────────────────────────────
        'margin_collapse': {
            'params': {
                'threshold': {'default': -0.30},   # 融資增減率平均 ≤ 此值（-0.30 = 減少30%）
                'days': {'default': 3},             # 近 N 個交易日
            },
            'description': '融資崩潰：近 days 日融資平均減少幅度超過 threshold（資金快速撤退）',
        },
    },
    'rebalance_strategies': {
        'immediate':   {'params': {}},
        'batch':       {'params': {'batch_ratio': {'default': 0.20}}},
        'delayed':     {'params': {'top_n': {'default': 5}, 'sharpe_threshold': {'default': 0}}},
        'concentrated':{'params': {'concentrate_top_k': {'default': 3}, 'lead_margin': {'default': 0.30}}},
        'none':        {'params': {}},
    },
}

DEFAULT_CONFIG = {
    'initial_capital': 1_000_000,
    'amount_per_stock': 100_000,
    'max_positions': 10,
    'market': 'tw',              # 鐵則：台股專版不可改
    'start_date': '2025-01-01',
    'end_date': None,
    'rebalance_freq': 'weekly',
    'fees': {
        'tw': {'buy_rate': 0.0015, 'sell_rate': 0.0045, 'min_fee': 20},
    },
    'buy_conditions': {
        'sharpe_rank':      {'enabled': True,  'top_n': 15},
        'sharpe_threshold': {'enabled': True,  'threshold': 0.5},
        'sharpe_streak':    {'enabled': False, 'days': 3,  'top_n': 10},
        'growth_streak':    {'enabled': True,  'days': 2,  'percentile': 50},
        'growth_rank':      {'enabled': False, 'top_n': 7},
        'sort_sharpe':      {'enabled': True},
        'sort_margin':      {'enabled': False, 'days': 5},
        'sort_industry':    {'enabled': False, 'per_industry': 2},
        'margin_net_long':  {'enabled': True,  'days': 5,  'min_avg_change': 0.02},
        'no_parabolic':     {'enabled': True,  'lookback': 30, 'max_gain': 0.40},
        'industry_rank':    {'enabled': False, 'top_n': 1},
    },
    'sell_conditions': {
        'sharpe_fail':     {'enabled': False, 'periods': 2, 'top_n': 15},
        'growth_fail':     {'enabled': False, 'days': 5, 'threshold': 0},
        'not_selected':    {'enabled': False, 'periods': 3},
        'drawdown':        {'enabled': True,  'threshold': 0.10, 'from_highest': False},
        'weakness':        {'enabled': False, 'rank_k': 20, 'periods': 3},
        'margin_collapse': {'enabled': True,  'threshold': -0.15, 'days': 3},
    },
    'rebalance_strategy': {
        'type': 'delayed',
        'top_n': 10,
        'sharpe_threshold': 0.3,
        'batch_ratio': 0.20,
        'concentrate_top_k': 3,
        'lead_margin': 0.30,
        'hold_top_n': 25,
    },
    'market_filter': {
        'enabled': True,
        'indicator': 'twii_200ma',
        'ma_window': 200,
    },
    'stop_loss_reentry': {
        'enabled': True,
        'type': 'price_recovery',
    },
}


class ConfigError(ValueError):
    """回測配置欄位不合法。"""


def _fill_condition_params(result: dict) -> None:
    for group in ('buy_conditions', 'sell_conditions'):
        for cond_name, cond_val in result.get(group, {}).items():
            option = CONDITION_OPTIONS[group].get(cond_name)
            if not option:
                continue
            for p_name, p_spec in option['params'].items():
                cond_val.setdefault(p_name, p_spec['default'])

    strategy = result.get('rebalance_strategy', {})
    stype = strategy.get('type')
    option = CONDITION_OPTIONS['rebalance_strategies'].get(stype) if stype else None
    if option:
        for p_name, p_spec in option['params'].items():
            strategy.setdefault(p_name, p_spec['default'])


def _apply_config_layer(result: dict, layer: dict) -> None:
    for key in ('initial_capital', 'amount_per_stock', 'max_positions',
                'market', 'start_date', 'end_date', 'rebalance_freq', 'fees',
                'market_filter', 'stop_loss_reentry'):
        if key in layer:
            result[key] = layer[key]
    for key in ('buy_conditions', 'sell_conditions', 'rebalance_strategy'):
        if key not in layer:
            continue
        for ck, cv in layer[key].items():
            current = result[key].get(ck)
            if isinstance(current, dict) and isinstance(cv, dict):
                result[key][ck] = {**current, **cv}
            else:
                result[key][ck] = cv


def load_config(user_params: Optional[dict] = None) -> dict:
    """優先級：DEFAULT_CONFIG < config.yaml backtest 區 < user_params。"""
    user_params = user_params or {}
    result = copy.deepcopy(DEFAULT_CONFIG)

    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            file_cfg = yaml.safe_load(f) or {}
        _apply_config_layer(result, file_cfg.get('backtest', {}))
    except Exception:
        pass

    _apply_config_layer(result, user_params)

    # 鐵則：台股專版強制 market = 'tw'
    result['market'] = 'tw'

    _fill_condition_params(result)
    _validate_config(result)
    return result


def _validate_config(config: dict) -> None:
    v = config.get('initial_capital')
    if not isinstance(v, (int, float)) or v <= 0:
        raise ConfigError(f'initial_capital 必須是正數，收到 {v!r}')
    v = config.get('amount_per_stock')
    if not isinstance(v, (int, float)) or v <= 0:
        raise ConfigError(f'amount_per_stock 必須是正數，收到 {v!r}')
    v = config.get('max_positions')
    if not isinstance(v, int) or not (1 <= v <= 100):
        raise ConfigError(f'max_positions 必須是 1–100 整數，收到 {v!r}')
    if config.get('market') != 'tw':
        raise ConfigError('台股專版引擎：market 必須是 tw')
    if config.get('rebalance_freq') not in {'daily', 'weekly', 'monthly'}:
        raise ConfigError('rebalance_freq 必須是 daily/weekly/monthly')
    try:
        pd.Timestamp(config['start_date'])
    except Exception:
        raise ConfigError(f'start_date 無法解析: {config.get("start_date")!r}')
    if config.get('end_date') is not None:
        try:
            pd.Timestamp(config['end_date'])
        except Exception:
            raise ConfigError(f'end_date 無法解析: {config.get("end_date")!r}')
    stype = config.get('rebalance_strategy', {}).get('type')
    if stype not in {'immediate', 'batch', 'delayed', 'concentrated', 'none'}:
        raise ConfigError(f'rebalance_strategy.type 不合法: {stype!r}')


# =============================================================================
# 指標：Sharpe / Ranking / Growth
# =============================================================================
def _calculate_sharpe_matrix(close_df: pd.DataFrame) -> pd.DataFrame:
    returns = close_df.pct_change()
    daily_rf = RISK_FREE_RATE / SHARPE_WINDOW
    excess = returns - daily_rf
    rolling_mean = excess.rolling(window=SHARPE_WINDOW).mean()
    rolling_std = excess.rolling(window=SHARPE_WINDOW).std().replace(0, np.nan)
    sharpe = (rolling_mean / rolling_std) * np.sqrt(SHARPE_WINDOW)
    return sharpe.replace([np.inf, -np.inf], np.nan).bfill().ffill()


def _compute_daily_ranks_by_country(matrix: pd.DataFrame, stock_info: dict) -> Dict[str, Dict[str, List[str]]]:
    if matrix is None or matrix.empty:
        return {}
    ranks: Dict = {}
    for d in matrix.index:
        date_str = str(d)[:10]
        row = matrix.loc[d].dropna()
        tw = [(t, v) for t, v in row.items() if stock_info.get(t, {}).get('country') == 'TW']
        ranks[date_str] = {
            'TW': [t for t, _ in sorted(tw, key=lambda x: x[1], reverse=True)],
        }
    return ranks


class Indicators:
    """
    台股版指標集合：Sharpe / Growth + 融資淨增 + 無爆漲。

    margin_data 由外部注入（run_pipeline 預載），指標方法透過它查詢融資資料。
    """

    def __init__(self, close_df: pd.DataFrame, stock_info: dict,
                 margin_data: Optional[TWMarginData] = None):
        self.close = close_df
        self.stock_info = stock_info
        self.margin_data = margin_data

        self._sharpe = None
        self._rank = None
        self._growth = None
        self._sharpe_rank_by_country = None
        self._growth_rank_by_country = None
        self._industry_sharpe = None
        self._industry_rank = None

    # ── Sharpe / Growth 指標（同 Keep-Buying）────────────────────────────

    @property
    def sharpe(self) -> pd.DataFrame:
        if self._sharpe is None:
            self._sharpe = _calculate_sharpe_matrix(self.close)
        return self._sharpe

    @property
    def rank(self) -> pd.DataFrame:
        if self._rank is None:
            self._rank = self.sharpe.rank(axis=1, ascending=False, method='min')
        return self._rank

    @property
    def growth(self) -> pd.DataFrame:
        if self._growth is None:
            self._growth = self.rank.shift(1) - self.rank
        return self._growth

    @property
    def sharpe_rank_by_country(self):
        if self._sharpe_rank_by_country is None:
            self._sharpe_rank_by_country = _compute_daily_ranks_by_country(self.sharpe, self.stock_info)
        return self._sharpe_rank_by_country

    @property
    def growth_rank_by_country(self):
        if self._growth_rank_by_country is None:
            self._growth_rank_by_country = _compute_daily_ranks_by_country(self.growth, self.stock_info)
        return self._growth_rank_by_country

    def get_dates(self) -> List[str]:
        return [str(d)[:10] for d in self.close.index]

    def get_sharpe(self, symbol: str, idx: int) -> float:
        return self.sharpe.iloc[idx].get(symbol, np.nan)

    def get_growth(self, symbol: str, idx: int) -> float:
        return self.growth.iloc[idx].get(symbol, np.nan)

    def check_in_sharpe_top_k(self, symbol: str, date_str: str, country: str, top_k: int) -> bool:
        return symbol in self.sharpe_rank_by_country.get(date_str, {}).get(country, [])[:top_k]

    def check_in_growth_top_k(self, symbol: str, date_str: str, country: str, top_k: int) -> bool:
        return symbol in self.growth_rank_by_country.get(date_str, {}).get(country, [])[:top_k]

    def get_sharpe_rank_position(self, symbol: str, date_str: str, country: str) -> int:
        ranking = self.sharpe_rank_by_country.get(date_str, {}).get(country, [])
        try:
            return ranking.index(symbol)
        except ValueError:
            return -1

    def get_growth_rank_position(self, symbol: str, date_str: str, country: str) -> int:
        ranking = self.growth_rank_by_country.get(date_str, {}).get(country, [])
        try:
            return ranking.index(symbol)
        except ValueError:
            return -1

    def check_in_growth_top_percentile(self, symbol: str, date_str: str, country: str, percentile: float) -> bool:
        ranking = self.growth_rank_by_country.get(date_str, {}).get(country, [])
        if not ranking:
            return False
        top_n = max(1, math.ceil(len(ranking) * percentile / 100))
        return symbol in ranking[:top_n]

    def check_sharpe_streak(self, symbol: str, idx: int, days: int, top_n: int) -> bool:
        if idx < days - 1:
            return False
        country = self.stock_info.get(symbol, {}).get('country', 'TW')
        dates = self.get_dates()
        for i in range(days):
            j = idx - i
            if j < 0 or not self.check_in_sharpe_top_k(symbol, dates[j], country, top_n):
                return False
        return True

    def check_growth_streak(self, symbol: str, idx: int, days: int, percentile: float = 50) -> bool:
        if idx < days - 1:
            return False
        country = self.stock_info.get(symbol, {}).get('country', 'TW')
        dates = self.get_dates()
        for i in range(days):
            j = idx - i
            if j < 0 or not self.check_in_growth_top_percentile(symbol, dates[j], country, percentile):
                return False
        return True

    # ── 台股新增：融資淨增 ────────────────────────────────────────────────

    def check_margin_net_long(self, symbol: str, idx: int, days: int, min_avg_change: float) -> bool:
        """
        融資淨增條件：近 days 個交易日融資平均增減率 ≥ min_avg_change。
        使用 T-1 資料（融資資料盤後公布，當日無法取得）。
        無融資資料（API 失敗 / .TWO 股票）→ 降級通過（回傳 True）
        """
        if self.margin_data is None:
            return True
        dates = self.get_dates()
        recent_dates = dates[max(0, idx - days): idx]
        avg = self.margin_data.get_recent_avg_margin_change(symbol, recent_dates, days)
        if avg is None:
            return True
        return avg >= min_avg_change

    def check_margin_collapse(self, symbol: str, idx: int, days: int, threshold: float) -> bool:
        """
        融資崩潰條件：近 days 日融資平均增減率 ≤ threshold（負值）。
        使用 T-1 資料（融資資料盤後公布，當日無法取得）。
        無融資資料 → 降級不觸發（回傳 False）
        """
        if self.margin_data is None:
            return False
        dates = self.get_dates()
        recent_dates = dates[max(0, idx - days): idx]
        avg = self.margin_data.get_recent_avg_margin_change(symbol, recent_dates, days)
        if avg is None:
            return False
        return avg <= threshold

    # ── 台股新增：無爆漲 ──────────────────────────────────────────────────

    def check_no_parabolic(self, symbol: str, idx: int, lookback: int, max_gain: float) -> bool:
        """
        無爆漲條件：近 lookback 個交易日的累積漲幅 < max_gain。
        - 只用收盤價計算，不需外部 API
        - 資料不足時降級通過
        """
        if idx < lookback:
            return True
        prices = self.close.iloc[idx - lookback: idx + 1][symbol]
        valid = prices.dropna()
        if len(valid) < 2:
            return True
        first_price = valid.iloc[0]
        last_price = valid.iloc[-1]
        if first_price <= 0:
            return True
        gain = (last_price - first_price) / first_price
        return gain < max_gain

    # ── 台股新增：產業濾網 ────────────────────────────────────────────────

    @property
    def industry_sharpe(self) -> pd.DataFrame:
        """各產業在每個交易日的平均 Sharpe（同一產業內所有候選股取平均）。"""
        if self._industry_sharpe is None:
            industry_map = pd.Series({
                s: self.stock_info.get(s, {}).get('industry', 'Unknown')
                for s in self.close.columns
                if self.stock_info.get(s, {}).get('industry', '') not in NON_TRADABLE_INDUSTRIES
            })
            self._industry_sharpe = self.sharpe[industry_map.index].T.groupby(industry_map).mean().T
        return self._industry_sharpe

    @property
    def industry_rank(self) -> pd.DataFrame:
        if self._industry_rank is None:
            self._industry_rank = self.industry_sharpe.rank(axis=1, ascending=False, method='min')
        return self._industry_rank

    def check_in_top_industry(self, symbol: str, idx: int, top_n: int) -> bool:
        """
        產業濾網：候選股所屬產業的平均 Sharpe 排名須在全市場前 top_n 名。
        台股容易以題材（產業）輪動炒作，此條件確認該股所屬產業正處於熱門族群。
        """
        industry = self.stock_info.get(symbol, {}).get('industry', 'Unknown')
        rank = self.industry_rank.iloc[idx].get(industry, np.nan)
        if pd.isna(rank):
            return False
        return rank <= top_n


# =============================================================================
# Trade / Position / Result 資料類
# =============================================================================
class TradeType(Enum):
    BUY = 'buy'
    SELL = 'sell'


@dataclass
class Trade:
    date: str
    symbol: str
    type: TradeType
    shares: int
    price: Money
    amount: Money
    amount_twd: Money
    fee: Money
    reason: str = ''
    profit: Money = field(default_factory=lambda: twd(0))

    def to_dict(self, stock_info: Optional[Dict] = None) -> dict:
        return {
            'date': self.date,
            'symbol': self.symbol,
            'name': (stock_info or {}).get(self.symbol, {}).get('name', self.symbol),
            'type': self.type.value,
            'shares': self.shares,
            'price': str(self.price),
            'amount': str(self.amount),
            'amount_twd': f'${self.amount_twd.amount:,.0f}',
            'fee': f'${self.fee.amount:,.0f}',
            'reason': self.reason,
            'profit': f'${self.profit.amount:+,.0f}' if self.profit.amount != 0 else '',
        }


@dataclass
class Position:
    symbol: str
    shares: int
    avg_cost: Money
    cost_basis: Money
    buy_date: str
    buy_price: Money
    peak_price: float = 0.0
    country: str = 'TW'


@dataclass
class BacktestResult:
    initial_capital: Money
    final_equity: Money
    total_return: float
    annualized_return: float
    total_trades: int
    win_trades: int
    loss_trades: int
    win_rate: float
    max_drawdown: float
    sharpe_ratio: float
    trades: list
    equity_curve: list

    def to_dict(self) -> dict:
        return {
            'initial_capital': str(self.initial_capital),
            'final_equity': str(self.final_equity),
            'total_return': f'{self.total_return:.2%}',
            'annualized_return': f'{self.annualized_return:.2%}',
            'total_trades': self.total_trades,
            'win_trades': self.win_trades,
            'loss_trades': self.loss_trades,
            'win_rate': f'{self.win_rate:.2%}',
            'max_drawdown': f'{self.max_drawdown:.2%}',
            'sharpe_ratio': round(self.sharpe_ratio, 2),
        }


# =============================================================================
# 回測引擎（台股版）
# =============================================================================
class BacktestEngine:
    def __init__(self, close_df, indicators: Indicators, stock_info, config, fx=None,
                 twii_bearish: Optional[Dict[str, bool]] = None):
        self.close = close_df
        self.indicators = indicators
        self.stock_info = stock_info
        self.config = config
        self.fx = fx or FX()
        self.twii_bearish: Dict[str, bool] = twii_bearish or {}

        initial = config['initial_capital']
        self.cash: Money = initial if isinstance(initial, Money) else twd(initial)
        self.positions: Dict[str, Position] = {}
        self.trades: List[Trade] = []
        self.equity_curve: List[dict] = []

        self._stop_loss_prices: Dict[str, float] = {}

    def _is_rebalance_day(self, idx: int) -> bool:
        freq = self.config.get('rebalance_freq', 'weekly')
        if freq == 'daily':
            return True
        if idx == 0:
            return True
        ts = self.close.index[idx]
        prev = self.close.index[idx - 1]
        if freq == 'weekly':
            return ts.isocalendar()[1] != prev.isocalendar()[1]
        if freq == 'monthly':
            return ts.month != prev.month
        return True

    def _exec_price(self, idx: int, symbol: str, fallback: Optional[float] = None) -> Optional[float]:
        """回傳 T+1 收盤價作為實際成交價；末日則沿用當日收盤。"""
        exec_idx = min(idx + 1, len(self.close) - 1)
        price = self.close.iloc[exec_idx].get(symbol)
        if price is None or pd.isna(price) or float(price) <= 0:
            return fallback
        return float(price)

    def run(self, start_date, end_date) -> BacktestResult:
        date_index = self.close.index
        start_idx = date_index.searchsorted(start_date)
        end_idx = date_index.searchsorted(end_date, side='right') - 1
        logger.info('回測: %s ~ %s', date_index[start_idx].date(), date_index[end_idx].date())
        for idx in range(start_idx, end_idx + 1):
            self._process_day(idx)
        return self._calculate_result(start_idx, end_idx)

    def _process_day(self, idx: int):
        date_str = self.close.index[idx].strftime('%Y-%m-%d')
        self._update_peaks(idx)
        self._process_emergency_sells(idx, date_str)
        self._process_rebalance(idx, date_str)
        equity, holdings_value, holdings_snapshot = self._calc_equity_with_holdings(idx)
        self.equity_curve.append({
            'date': date_str,
            'equity': equity.amount,
            'cash': self.cash.amount,
            'holdingsValue': holdings_value,
            'holdings': holdings_snapshot,
        })

    def _update_peaks(self, idx: int):
        for sym, pos in self.positions.items():
            price = self.close.iloc[idx].get(sym, pos.peak_price)
            if price > pos.peak_price:
                pos.peak_price = price

    def _build_ideal_portfolio(self, idx: int) -> List[str]:
        buy_cond = self.config['buy_conditions']
        candidates = []
        for symbol in self.close.columns:
            industry = self.stock_info.get(symbol, {}).get('industry', '')
            if industry in NON_TRADABLE_INDUSTRIES:
                continue
            if not self._check_buy(symbol, idx, buy_cond):
                continue
            candidates.append({
                'symbol': symbol,
                'sharpe': self.indicators.get_sharpe(symbol, idx),
                'industry': self.stock_info.get(symbol, {}).get('industry', 'Unknown'),
            })

        if buy_cond.get('sort_industry', {}).get('enabled'):
            per_industry = buy_cond['sort_industry']['per_industry']
            groups: Dict[str, list] = {}
            for c in candidates:
                groups.setdefault(c['industry'], []).append(c)
            for ind in groups:
                groups[ind].sort(key=lambda x: x['sharpe'] if x['sharpe'] is not None else -999, reverse=True)
            industries = sorted(
                groups.keys(),
                key=lambda i: (groups[i][0]['sharpe'] if groups[i][0]['sharpe'] is not None else -999),
                reverse=True,
            )
            selected, counts, has_more = [], {}, True
            max_rounds = per_industry * len(industries) + 1
            r = 0
            while has_more and r < max_rounds:
                has_more = False
                for ind in industries:
                    c = counts.get(ind, 0)
                    if c >= per_industry or c >= len(groups[ind]):
                        continue
                    selected.append(groups[ind][c])
                    counts[ind] = c + 1
                    has_more = True
                r += 1
            candidates = selected
        elif buy_cond.get('sort_margin', {}).get('enabled') and self.indicators.margin_data is not None:
            # 成長型：在 Sharpe 篩選後，依融資布局強度（近 N 日平均增減率）降序排列
            days_m = buy_cond['sort_margin'].get('days', 5)
            dates_all = self.indicators.get_dates()
            recent_dates = dates_all[max(0, idx - days_m + 1): idx + 1]

            def _margin_sort_key(c: dict) -> float:
                avg = self.indicators.margin_data.get_recent_avg_margin_change(
                    c['symbol'], recent_dates, days_m
                )
                return avg if avg is not None else -999.0

            candidates.sort(key=_margin_sort_key, reverse=True)
        elif buy_cond.get('sort_sharpe', {}).get('enabled'):
            candidates.sort(key=lambda x: x['sharpe'] if x['sharpe'] is not None else -999, reverse=True)

        return [c['symbol'] for c in candidates]

    def _build_hold_set(self, idx: int) -> set:
        """持有資格（寬鬆）：現有持股僅檢查 Sharpe 排名是否仍在前 hold_top_n。
        margin_net_long / no_parabolic / growth_streak 屬進場時機訊號，不影響持有判斷；
        個股風險已由 drawdown / margin_collapse（_process_emergency_sells）覆蓋。"""
        date_str = self.close.index[idx].strftime('%Y-%m-%d')
        hold_top_n = self.config['rebalance_strategy'].get('hold_top_n', 25)
        held = set()
        for symbol in self.positions:
            country = self.stock_info.get(symbol, {}).get('country', 'TW')
            if self.indicators.check_in_sharpe_top_k(symbol, date_str, country, hold_top_n):
                held.add(symbol)
        return held

    def _check_buy(self, symbol: str, idx: int, buy_cond: dict) -> bool:
        date_str = self.close.index[idx].strftime('%Y-%m-%d')
        country = self.stock_info.get(symbol, {}).get('country', 'TW')

        if self.config.get('stop_loss_reentry', {}).get('enabled', False):
            stop_price = self._stop_loss_prices.get(symbol)
            if stop_price is not None:
                current = self.close.iloc[idx].get(symbol, 0)
                if current > stop_price:
                    del self._stop_loss_prices[symbol]
                else:
                    return False

        if buy_cond.get('sharpe_rank', {}).get('enabled'):
            if not self.indicators.check_in_sharpe_top_k(symbol, date_str, country, buy_cond['sharpe_rank']['top_n']):
                return False

        if buy_cond.get('sharpe_threshold', {}).get('enabled'):
            sharpe = self.indicators.get_sharpe(symbol, idx)
            if pd.isna(sharpe) or sharpe < buy_cond['sharpe_threshold']['threshold']:
                return False

        if buy_cond.get('sharpe_streak', {}).get('enabled'):
            if not self.indicators.check_sharpe_streak(
                    symbol, idx, buy_cond['sharpe_streak']['days'], buy_cond['sharpe_streak']['top_n']):
                return False

        if buy_cond.get('growth_streak', {}).get('enabled'):
            if not self.indicators.check_growth_streak(
                    symbol, idx, buy_cond['growth_streak']['days'], buy_cond['growth_streak']['percentile']):
                return False

        if buy_cond.get('growth_rank', {}).get('enabled'):
            if not self.indicators.check_in_growth_top_k(symbol, date_str, country, buy_cond['growth_rank']['top_n']):
                return False

        # ── 台股新增條件 ─────────────────────────────────────────────────

        if buy_cond.get('margin_net_long', {}).get('enabled'):
            cfg = buy_cond['margin_net_long']
            if not self.indicators.check_margin_net_long(
                    symbol, idx, cfg['days'], cfg['min_avg_change']):
                return False

        if buy_cond.get('no_parabolic', {}).get('enabled'):
            cfg = buy_cond['no_parabolic']
            if not self.indicators.check_no_parabolic(
                    symbol, idx, cfg['lookback'], cfg['max_gain']):
                return False

        if buy_cond.get('industry_rank', {}).get('enabled'):
            if not self.indicators.check_in_top_industry(symbol, idx, buy_cond['industry_rank']['top_n']):
                return False

        return True

    def _check_delayed_gate(self, idx: int, date_str: str) -> bool:
        """delayed 策略的買進閘門：僅控制買進，不影響旋轉賣出。"""
        strategy = self.config['rebalance_strategy']
        if strategy['type'] != 'delayed':
            return True
        top_n = strategy['top_n']
        sharpe_threshold = strategy['sharpe_threshold']
        tw_top = self.indicators.sharpe_rank_by_country.get(date_str, {}).get('TW', [])[:top_n]
        vals = [self.indicators.get_sharpe(s, idx) for s in tw_top]
        vals = [v for v in vals if not pd.isna(v)]
        avg_sh = sum(vals) / len(vals) if vals else 0
        passed = avg_sh > sharpe_threshold
        if not passed:
            logger.info('[%s] delayed gate 未通過：TW前%d名平均Sharpe=%.3f ≤ 門檻%.3f，本次暫停買進',
                        date_str, top_n, avg_sh, sharpe_threshold)
        return passed

    def _process_emergency_sells(self, idx: int, date_str: str):
        """每日緊急止損：drawdown（停損）與 margin_collapse（融資崩潰）。
        Method B 的旋轉賣出由 _process_rebalance 負責。"""
        sell_cond = self.config['sell_conditions']
        to_sell = []
        for symbol, pos in list(self.positions.items()):
            reason = None

            if sell_cond.get('drawdown', {}).get('enabled'):
                threshold = sell_cond['drawdown']['threshold']
                from_highest = sell_cond['drawdown']['from_highest']
                price = self.close.iloc[idx].get(symbol, pos.buy_price.amount)
                ref = (pos.peak_price or pos.buy_price.amount) if from_highest else pos.buy_price.amount
                if ref > 0 and (ref - price) / ref >= threshold:
                    reason = f'drawdown({threshold:.0%})'

            if reason is None and sell_cond.get('margin_collapse', {}).get('enabled'):
                cfg = sell_cond['margin_collapse']
                if self.indicators.check_margin_collapse(symbol, idx, cfg['days'], cfg['threshold']):
                    reason = f'margin_collapse({cfg["threshold"]:.0%})'

            if reason:
                to_sell.append((symbol, reason))

        for symbol, reason in to_sell:
            self._sell(symbol, idx, date_str, reason)

    def _process_rebalance(self, idx: int, date_str: str):
        if not self._is_rebalance_day(idx):
            return

        # Step 1：持有資格檢查（寬鬆：僅看 Sharpe 排名是否仍具相對優勢）→ 旋轉出場
        # （TWII 空頭時仍執行，避免空頭期間繼續持有轉弱標的）
        hold_top_n = self.config['rebalance_strategy'].get('hold_top_n', 25)
        hold_ok = self._build_hold_set(idx)
        to_rotate = [s for s in list(self.positions.keys()) if s not in hold_ok]
        for sym in to_rotate:
            country = self.stock_info.get(sym, {}).get('country', 'TW')
            rank = self.indicators.get_sharpe_rank_position(sym, date_str, country)
            reason = (f'rotation(Sharpe排名#{rank + 1}，掉出前{hold_top_n}名)'
                      if rank >= 0 else f'rotation(掉出前{hold_top_n}名)')
            self._sell(sym, idx, date_str, reason)

        # Step 2：市場過濾 — 空頭期間暫停新增買進
        mf = self.config.get('market_filter', {})
        if mf.get('enabled', False) and self.twii_bearish.get(date_str, False):
            logger.info('[%s] TWII 處於 200MA 空頭，本次暫停買進', date_str)
            return

        # Step 3：買進閘門檢查（delayed 策略）
        if not self._check_delayed_gate(idx, date_str):
            return

        # Step 4：買進進場候選（嚴格 5 條件 AND-chain）中尚未持有的標的
        # 候選清單不在此處依 slots 截斷，交由 _buy_stocks 依序嘗試並跳過資金不足以
        # 買進的標的，改買排名較後但價格較低的候選，避免資金閒置
        slots = self.config['max_positions'] - len(self.positions)
        if slots <= 0:
            return
        entry_candidates = self._build_ideal_portfolio(idx)
        to_buy = [s for s in entry_candidates if s not in self.positions]
        if not to_buy:
            return

        ts = self.close.index[idx]
        if ts.day <= 3 and (idx == 0 or self.close.index[idx - 1].month != ts.month):
            logger.info('[%s] 持有=%d 旋轉出場=%d 進場候選=%d 待買=%d',
                        date_str, len(hold_ok), len(to_rotate), len(entry_candidates), min(len(to_buy), slots))

        strategy = self.config['rebalance_strategy']
        stype = strategy['type']

        if stype == 'none':
            return
        elif stype == 'batch':
            invest = self.cash * strategy['batch_ratio']
            self._buy_stocks(to_buy, idx, date_str, max_buys=slots, budget=invest)
        elif stype == 'concentrated':
            top_k = strategy['concentrate_top_k']
            lead_margin = strategy['lead_margin']
            tw_top_k = self.indicators.sharpe_rank_by_country.get(date_str, {}).get('TW', [])[:top_k]
            tw_next_k = self.indicators.sharpe_rank_by_country.get(date_str, {}).get('TW', [])[top_k: top_k * 2]
            def _avg_sharpe(tickers):
                vs = [self.indicators.get_sharpe(t, idx) for t in tickers]
                vs = [v for v in vs if not pd.isna(v)]
                return sum(vs) / len(vs) if vs else 0
            top_avg, next_avg = _avg_sharpe(tw_top_k), _avg_sharpe(tw_next_k)
            should_concentrate = (next_avg <= 0 and top_avg > 0) or (
                next_avg > 0 and (top_avg - next_avg) / abs(next_avg) >= lead_margin
            )
            if not should_concentrate:
                return
            buys = min(top_k, slots)
            self._buy_stocks(to_buy, idx, date_str, max_buys=buys)
        else:
            # immediate / delayed（閘門已在 Step 3 通過）
            self._buy_stocks(to_buy, idx, date_str, max_buys=slots)

    def _buy_stocks(self, symbols, idx, date_str, max_buys: int, budget: Optional[Money] = None):
        """依 Sharpe 排名順序買進，每格預算 = 剩餘可用資金 ÷ 剩餘空格數。
        若排名較前的標的當前預算買不起，改尋找排名較後但買得起的標的；
        買進後剩餘空格變少、每格預算隨之提高，再從頭重新嘗試先前買不起的標的。
        允許零股交易，股數以 1 股為最小單位。"""
        if budget is None:
            budget = self.cash
        if not isinstance(budget, Money):
            budget = twd(budget)

        remaining = [s for s in symbols
                      if s not in self.positions and self._exec_price(idx, s) is not None]

        slots_left = max_buys
        while remaining and slots_left > 0 and budget.amount > 0:
            per_stock = budget / slots_left
            bought_symbol = None
            for symbol in remaining:
                exec_price = self._exec_price(idx, symbol)
                # 假設可零股交易，股數以 1 股為最小單位
                shares = int(per_stock.amount / exec_price)
                if shares <= 0:
                    continue

                price_money = twd(exec_price)
                fee_cfg = self.config['fees']['tw']
                amount_original = twd(shares * exec_price)
                fee = twd(max(amount_original.amount * fee_cfg['buy_rate'], fee_cfg['min_fee']))
                total_cost = amount_original + fee
                # 含手續費後總成本可能微幅超出預算，逐股遞減直到落在可用資金內
                while shares > 0 and (total_cost > self.cash or total_cost > budget):
                    shares -= 1
                    amount_original = twd(shares * exec_price)
                    fee = twd(max(amount_original.amount * fee_cfg['buy_rate'], fee_cfg['min_fee']))
                    total_cost = amount_original + fee
                if shares <= 0:
                    continue

                self.cash = self.cash - total_cost
                budget = budget - total_cost
                self.positions[symbol] = Position(
                    symbol=symbol, shares=shares, avg_cost=price_money,
                    cost_basis=total_cost, buy_date=date_str, buy_price=price_money,
                    peak_price=exec_price, country='TW',
                )
                country = self.stock_info.get(symbol, {}).get('country', 'TW')
                rank = self.indicators.get_sharpe_rank_position(symbol, date_str, country)
                reason = f'buy(Sharpe排名#{rank + 1})' if rank >= 0 else 'buy'
                self.trades.append(Trade(
                    date=date_str, symbol=symbol, type=TradeType.BUY,
                    shares=shares, price=price_money, amount=amount_original,
                    amount_twd=amount_original, fee=fee, reason=reason,
                ))
                if len([t for t in self.trades if t.type == TradeType.BUY]) == 1:
                    logger.info('[%s] 首筆買進：%s shares=%d cost_twd=%.0f', date_str, symbol, shares, total_cost.amount)
                bought_symbol = symbol
                break

            if bought_symbol is None:
                for symbol in remaining:
                    exec_price = self._exec_price(idx, symbol)
                    logger.debug('[%s] %s 每格預算不足買進 1 股（每格預算=%.0f, exec_price=%.2f）',
                                  date_str, symbol, per_stock.amount, exec_price)
                break

            remaining.remove(bought_symbol)
            slots_left -= 1

    def _sell(self, symbol, idx, date_str, reason):
        if symbol not in self.positions:
            return
        pos = self.positions[symbol]
        exec_price = self._exec_price(idx, symbol, fallback=pos.avg_cost.amount)
        price_money = twd(exec_price)
        amount_original = twd(pos.shares * exec_price)
        amount_twd = amount_original
        fee_cfg = self.config['fees']['tw']
        fee = twd(max(amount_twd.amount * fee_cfg['sell_rate'], fee_cfg['min_fee']))
        profit = amount_twd - pos.cost_basis - fee
        self.cash = self.cash + amount_twd - fee

        if 'drawdown' in reason and self.config.get('stop_loss_reentry', {}).get('enabled', False):
            # 記錄訊號觸發當日收盤作為回收門檻
            trigger = self.close.iloc[idx].get(symbol, exec_price)
            self._stop_loss_prices[symbol] = float(trigger) if trigger else exec_price

        del self.positions[symbol]
        self.trades.append(Trade(
            date=date_str, symbol=symbol, type=TradeType.SELL,
            shares=pos.shares, price=price_money, amount=amount_original,
            amount_twd=amount_twd, fee=fee, reason=reason, profit=profit,
        ))

    def _calc_equity_with_holdings(self, idx: int):
        date_str = self.close.index[idx].strftime('%Y-%m-%d')
        equity = self.cash
        holdings_value = 0.0
        snapshot: Dict = {}
        for sym, pos in self.positions.items():
            price_raw = self.close.iloc[idx].get(sym, pos.avg_cost.amount)
            mv = twd(pos.shares * price_raw)
            equity = equity + mv
            holdings_value += mv.amount
            cb = pos.cost_basis.amount
            pnl_pct = (mv.amount - cb) / cb if cb > 0 else 0
            info = self.stock_info.get(sym, {})
            snapshot[sym] = {
                'shares': pos.shares,
                'avgCost': round(pos.avg_cost.amount, 2),
                'currentPrice': round(price_raw, 2),
                'marketValue': round(mv.amount, 0),
                'pnlPct': round(pnl_pct * 100, 2),
                'buyDate': pos.buy_date,
                'industry': info.get('industry', 'Unknown'),
                'country': 'TW',
            }
        return equity, holdings_value, snapshot

    def _calc_equity(self, idx: int) -> Money:
        equity, _, _ = self._calc_equity_with_holdings(idx)
        return equity

    def _calculate_result(self, start_idx: int, end_idx: int) -> BacktestResult:
        initial_cfg = self.config['initial_capital']
        initial = initial_cfg if isinstance(initial_cfg, Money) else twd(initial_cfg)
        final = self._calc_equity(end_idx)
        total_return = (final.amount - initial.amount) / initial.amount
        days = end_idx - start_idx + 1
        years = days / 252
        annualized = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0

        sells = [t for t in self.trades if t.type == TradeType.SELL]
        wins = sum(1 for t in sells if t.profit.amount > 0)
        losses = sum(1 for t in sells if t.profit.amount <= 0)
        win_rate = wins / len(sells) if sells else 0

        max_eq = initial.amount
        max_dd = 0
        for p in self.equity_curve:
            eq = p['equity']
            if eq > max_eq:
                max_eq = eq
            dd = (max_eq - eq) / max_eq if max_eq > 0 else 0
            if dd > max_dd:
                max_dd = dd

        if len(self.equity_curve) > 1:
            eqs = [p['equity'] for p in self.equity_curve]
            rets = np.diff(eqs) / np.array(eqs[:-1])
            sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(252)) if np.std(rets) > 0 else 0
        else:
            sharpe = 0

        return BacktestResult(
            initial_capital=initial, final_equity=final,
            total_return=total_return, annualized_return=annualized,
            total_trades=len(self.trades), win_trades=wins, loss_trades=losses,
            win_rate=win_rate, max_drawdown=max_dd, sharpe_ratio=sharpe,
            trades=[t.to_dict(self.stock_info) for t in self.trades],
            equity_curve=self.equity_curve,
        )


# =============================================================================
# Benchmark 曲線（台股版：固定 ^TWII）
# =============================================================================
def calculate_benchmark_curve(market: str, trading_dates: list, initial_capital: float, fx: FX) -> Tuple[list, str]:
    if not trading_dates:
        return [], ''

    name = '台灣加權指數'
    prices = fetch_benchmark_prices('^TWII')
    if not prices:
        return [], name
    curve, first = [], None
    for d in trading_dates:
        p = prices.get(d)
        if not p:
            continue
        if first is None:
            first = p
        curve.append({'date': d, 'equity': round(initial_capital * (p / first), 2)})
    return curve, name


# =============================================================================
# 當前持倉 snapshot
# =============================================================================
def build_current_holdings(engine: BacktestEngine, close_df: pd.DataFrame, end_dt) -> list:
    date_index = close_df.index
    actual_end_idx = date_index.searchsorted(end_dt, side='right') - 1
    end_date_str = close_df.index[actual_end_idx].strftime('%Y-%m-%d')

    holdings = []
    for symbol, pos in engine.positions.items():
        current_price = close_df.iloc[actual_end_idx].get(symbol, pos.avg_cost.amount)
        market_value = twd(pos.shares * current_price)
        cost_in_twd = pos.cost_basis
        pnl_pct = (market_value.amount - cost_in_twd.amount) / cost_in_twd.amount if cost_in_twd.amount > 0 else 0
        holdings.append({
            'symbol': symbol,
            'shares': pos.shares,
            'avg_cost': str(pos.avg_cost),
            'current_price': str(twd(current_price)),
            'market_value_twd': round(market_value.amount, 0),
            'pnl_pct': pnl_pct,
            'buy_date': pos.buy_date,
            'industry': engine.stock_info.get(symbol, {}).get('industry', 'Unknown'),
            'name': engine.stock_info.get(symbol, {}).get('name', symbol),
            'country': 'TW',
        })
    holdings.sort(key=lambda x: x['buy_date'], reverse=True)
    return holdings


# =============================================================================
# Pipeline：唯一 Domain 入口
# =============================================================================
def resolve_portfolio() -> Tuple[Dict, str]:
    """從 TradingView watchlist 取得台股投資組合。"""
    _, stock_info = fetch_watchlist()
    return stock_info, 'tradingview'


def run_pipeline(
    backtest_params: Optional[dict] = None,
) -> Dict:
    """
    完整回測管線：設定 → 資料 → 融資預載 → 對齊 → 運行 → 基準 → 持倉 snapshot。

    Returns:
        dict：config, result, current_holdings, benchmark_curve, benchmark_name,
              start_dt, end_dt, portfolio_source, symbols_count, margin_data_dates

    Raises:
        ConfigError: 參數不合法
        TradingViewSessionExpired: session 已過期
        RuntimeError: 資料抽取失敗或無可用標的
    """
    config = load_config(backtest_params or {})
    logger.info('config 載入完成：start=%s end=%s capital=%s max_positions=%s rebalance_freq=%s',
                config.get('start_date'), config.get('end_date'),
                config.get('initial_capital'), config.get('max_positions'), config.get('rebalance_freq'))

    stock_info, portfolio_source = resolve_portfolio()
    logger.info('標的解析完成：source=%s count=%d', portfolio_source, len(stock_info))
    if not stock_info:
        raise RuntimeError('無可用台股標的（TradingView watchlist 為空）')

    raw_data = fetch_all_stock_data(stock_info)
    logger.info('資料抽取完成：成功=%d / 請求=%d', len(raw_data), len(stock_info))
    if not raw_data:
        raise RuntimeError('所有標的的歷史資料皆抽取失敗')

    aligned, _ = align_data_with_bfill(raw_data)
    close_df = build_close_df(aligned)
    logger.info('對齊完成：shape=%s date_range=%s〜%s',
                close_df.shape,
                close_df.index[0].date() if not close_df.empty else None,
                close_df.index[-1].date() if not close_df.empty else None)
    if close_df.empty:
        raise RuntimeError('對齊後無可用股價資料')

    close_df, stock_info = filter_by_market(close_df, stock_info, 'tw')
    logger.info('台股過濾後剩餘標的=%d', len(stock_info))
    if close_df.empty:
        raise RuntimeError('台股過濾後無可用資料')

    end_date_str = config.get('end_date')
    end_dt_raw = pd.Timestamp(datetime.today().date()) if not end_date_str else pd.Timestamp(end_date_str)
    date_index = close_df.index
    end_idx = date_index.searchsorted(end_dt_raw, side='right') - 1
    if end_idx < 0:
        raise ConfigError(f'結束日期 {end_dt_raw.date()} 早於所有可用資料')
    end_dt = date_index[end_idx]

    start_idx = date_index.searchsorted(pd.Timestamp(config['start_date']), side='left')
    if start_idx >= len(date_index):
        raise ConfigError(f'開始日期 {config["start_date"]} 晚於所有可用資料')
    start_dt = date_index[start_idx]
    if start_dt >= end_dt:
        raise ConfigError(f'start_date {start_dt.date()} 必須早於 end_date {end_dt.date()}')
    logger.info('回測區間：%s ~ %s（%d 個交易日）',
                start_dt.date(), end_dt.date(), end_idx - start_idx + 1)

    # ── 融資融券資料預載（只在有啟用融資條件時才抓取）────────────────────
    trading_dates = [close_df.index[i].strftime('%Y-%m-%d') for i in range(start_idx, end_idx + 1)]
    margin_data: Optional[TWMarginData] = None
    buy_cond = config['buy_conditions']
    sell_cond = config['sell_conditions']
    needs_margin = (
        buy_cond.get('margin_net_long', {}).get('enabled', False) or
        sell_cond.get('margin_collapse', {}).get('enabled', False)
    )
    if needs_margin:
        margin_data = TWMarginData()
        margin_data.load_for_dates(trading_dates, symbols=list(close_df.columns))
    else:
        logger.info('融資條件均未啟用，跳過 TWSE API 預載')

    # ── TWII 200MA 市場過濾資料準備 ───────────────────────────────────────────
    twii_bearish: Dict[str, bool] = {}
    twii_prices_raw = fetch_benchmark_prices('^TWII')
    if twii_prices_raw and config.get('market_filter', {}).get('enabled', False):
        ma_window = config.get('market_filter', {}).get('ma_window', 200)
        twii_series = pd.Series(
            {pd.Timestamp(d): p for d, p in twii_prices_raw.items()}, dtype=float
        ).reindex(close_df.index).bfill().ffill()
        twii_ma = twii_series.rolling(window=ma_window, min_periods=max(50, ma_window // 4)).mean()
        for i, ts in enumerate(close_df.index):
            d = ts.strftime('%Y-%m-%d')
            if not pd.isna(twii_ma.iloc[i]):
                twii_bearish[d] = bool(twii_series.iloc[i] < twii_ma.iloc[i])
        bear_days = sum(1 for v in twii_bearish.values() if v)
        logger.info('TWII 200MA 計算完成：空頭天數=%d / 總天數=%d', bear_days, len(twii_bearish))

    indicators = Indicators(close_df, stock_info, margin_data)
    fx = FX()
    engine = BacktestEngine(close_df, indicators, stock_info, config, fx, twii_bearish=twii_bearish)
    logger.info('開始執行回測引擎…')
    result = engine.run(start_date=start_dt, end_date=end_dt)
    logger.info('回測完成：equity_points=%d trades=%d', len(result.equity_curve), len(result.trades))

    benchmark_curve, benchmark_name = calculate_benchmark_curve(
        'tw', [p['date'] for p in result.equity_curve], config['initial_capital'], fx,
    )
    logger.info('基準指數計算完成：%s (%d 點)', benchmark_name, len(benchmark_curve))

    current_holdings = build_current_holdings(engine, close_df, end_dt)
    logger.info('當前持倉快照：%d 檔', len(current_holdings))

    market_regime = [
        {'date': d, 'bearish': b}
        for d, b in twii_bearish.items()
        if d >= config['start_date']
    ]

    return {
        'config': config,
        'result': result,
        'current_holdings': current_holdings,
        'benchmark_curve': benchmark_curve,
        'benchmark_name': benchmark_name,
        'market_regime': market_regime,
        'start_dt': start_dt,
        'end_dt': end_dt,
        'portfolio_source': portfolio_source,
        'symbols_count': len(stock_info),
        'margin_data_loaded': needs_margin,
    }
