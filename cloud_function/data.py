"""
資料層：TradingView watchlist、yfinance 股價、FinMind 融資融券、日期對齊、幣別、匯率。

設計原則：
- 純函數，無檔案系統依賴（無 BASE_DIR）
- TradingView session 過期透過 TradingViewSessionExpired 例外向上傳遞
- FinMind 融資融券 API 失敗時優雅降級（不拋例外，使條件自動通過）
- 所有外部憑證與策略參數存於 cloud_function/config.yaml
- 本地開發支援 pickle 快取（歷史區間永久快取；當日區間每日更新）
  快取目錄：{project_root}/.cache/（不上傳 GCF）

台股專版差異：
- 僅處理 TW market（TWSE / TPEX），不含美股 FX 換算複雜度
- 新增 TWMarginData 類別（FinMind 融資融券範圍查詢，343 天 → ~6 次 API）
- Benchmark 固定為台灣加權指數（^TWII）
"""
from __future__ import annotations

import calendar
import logging
import pickle
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

import pandas as pd
import requests
import yaml
import yfinance as yf

logger = logging.getLogger(__name__)

# =============================================================================
# 常數
# =============================================================================
SHARPE_WINDOW = 252
RISK_FREE_RATE = 0.02          # 台灣無風險利率（較低）
DATA_PERIOD = '6y'
MIN_HISTORY_DAYS = 100
MIN_STOCKS_FOR_VALID_DAY = 20
MIN_STOCKS_FOR_VALID_DAY_RATIO = 0.5
NON_TRADABLE_INDUSTRIES = frozenset({'Market Index', 'Index'})

CONFIG_FILE = Path(__file__).parent / 'config.yaml'
TPE_TZ = timezone(timedelta(hours=8))

# 快取目錄：cloud_function/ 的上層 .cache/（不上傳 GCF）
_CACHE_DIR = Path(__file__).parent.parent / '.cache'

# FinMind API（融資融券、股票基本資料）
_FINMIND_URL = 'https://api.finmindtrade.com/api/v4/data'
_FINMIND_CHUNK_MONTHS = 3   # 每次查詢的月數（343 天 → 約 6 次 API 呼叫）
_STOCK_INFO_CACHE_FILE = _CACHE_DIR / 'finmind_stock_info.pkl'  # 中文簡稱對照，永久快取


def _load_finmind_token() -> str:
    """從 config.yaml 讀取 FinMind token。"""
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get('finmind', {}).get('token', '')
    except Exception:
        return ''


# =============================================================================
# 本地快取工具
# =============================================================================
def _cache_path(key: str) -> Path:
    today = date.today().isoformat()
    safe_key = key.replace('/', '_').replace(':', '_')
    return _CACHE_DIR / f'{safe_key}__{today}.pkl'


def _load_cache(key: str):
    try:
        p = _cache_path(key)
        if p.exists():
            data = pickle.loads(p.read_bytes())  # noqa: S301 本地己寫己讀
            logger.debug('[cache hit] %s', key)
            return data
    except Exception as e:
        logger.debug('[cache read error] %s: %s', key, e)
    return None


def _save_cache(key: str, data) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(key).write_bytes(pickle.dumps(data))
        logger.debug('[cache saved] %s', key)
    except Exception as e:
        logger.debug('[cache write error] %s: %s', key, e)


# =============================================================================
# 例外
# =============================================================================
class TradingViewSessionExpired(RuntimeError):
    """TradingView sessionid cookie 已過期或無效。"""

    def __init__(self, expires_at: Optional[str], detail: str):
        self.expires_at = expires_at
        self.detail = detail
        super().__init__(detail)


# =============================================================================
# 幣別與 Money（台股專版：主要用 TWD，保留 USD 以備未來擴充）
# =============================================================================
class Currency(Enum):
    TWD = 'TWD'
    USD = 'USD'

    def __str__(self) -> str:
        return self.value


class CurrencyMismatchError(TypeError):
    def __init__(self, left: Currency, right: Currency, op: str):
        super().__init__(f'幣別不匹配: {left} {op} {right}')


@dataclass
class Money:
    amount: float
    currency: Currency

    def __post_init__(self):
        if isinstance(self.currency, str):
            object.__setattr__(self, 'currency', Currency(self.currency.upper()))

    def __add__(self, other):
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency != other.currency:
            raise CurrencyMismatchError(self.currency, other.currency, '+')
        return Money(self.amount + other.amount, self.currency)

    def __radd__(self, other):
        if other == 0:
            return self
        return self.__add__(other)

    def __sub__(self, other):
        if not isinstance(other, Money):
            return NotImplemented
        if self.currency != other.currency:
            raise CurrencyMismatchError(self.currency, other.currency, '-')
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, n):
        if isinstance(n, Money):
            raise TypeError('Money 不能與 Money 相乘')
        return Money(self.amount * n, self.currency)

    def __rmul__(self, n):
        return self.__mul__(n)

    def __truediv__(self, other):
        if isinstance(other, Money):
            if self.currency != other.currency:
                raise CurrencyMismatchError(self.currency, other.currency, '/')
            return self.amount / other.amount
        return Money(self.amount / other, self.currency)

    def __eq__(self, other):
        if not isinstance(other, Money):
            return False
        return self.currency == other.currency and abs(self.amount - other.amount) < 1e-6

    def __lt__(self, other):
        if self.currency != other.currency:
            raise CurrencyMismatchError(self.currency, other.currency, '<')
        return self.amount < other.amount

    def __le__(self, other):
        return self == other or self < other

    def __gt__(self, other):
        if self.currency != other.currency:
            raise CurrencyMismatchError(self.currency, other.currency, '>')
        return self.amount > other.amount

    def __ge__(self, other):
        return self == other or self > other

    def __neg__(self):
        return Money(-self.amount, self.currency)

    def __bool__(self):
        return self.amount != 0

    def __hash__(self):
        return hash((round(self.amount, 6), self.currency))

    def __str__(self):
        if self.currency == Currency.TWD:
            return f'${self.amount:,.0f} TWD'
        return f'${self.amount:,.2f} USD'

    def is_twd(self) -> bool:
        return self.currency == Currency.TWD

    def is_usd(self) -> bool:
        return self.currency == Currency.USD


def twd(amount: float) -> Money:
    return Money(amount, Currency.TWD)


def usd(amount: float) -> Money:
    return Money(amount, Currency.USD)


# =============================================================================
# 匯率服務（台股專版：主要用 TWD，FX 保留相容性）
# =============================================================================
class FX:
    """台股專版：匯率主要用於顯示一致性，不做 USD/TWD 轉換計算。"""
    DEFAULT_RATE = 32.0

    def __init__(self):
        self._history: Dict[str, float] = {}
        self._latest = self.DEFAULT_RATE
        self._fetch_from_yfinance()

    def _fetch_from_yfinance(self):
        cache_key = 'fx__TWDUSD__6y'
        cached = _load_cache(cache_key)
        if cached is not None:
            self._history = cached['history']
            self._latest = cached['latest']
            return
        try:
            df = yf.Ticker('TWD=X').history(period='6y', interval='1d')
            if df.empty:
                return
            self._history = {
                d.strftime('%Y-%m-%d'): round(float(r['Close']), 4)
                for d, r in df.iterrows() if pd.notna(r.get('Close'))
            }
            if self._history:
                self._latest = self._history[max(self._history.keys())]
            _save_cache(cache_key, {'history': self._history, 'latest': self._latest})
        except Exception as e:
            logger.warning('FX 抓取失敗，使用預設匯率 %.2f: %s', self.DEFAULT_RATE, e)

    def rate(self, date_str: Optional[str] = None) -> float:
        if date_str is None:
            return self._latest
        if date_str in self._history:
            return self._history[date_str]
        if self._history:
            for d in reversed(sorted(self._history.keys())):
                if d <= date_str:
                    return self._history[d]
        return self.DEFAULT_RATE

    def to_twd(self, m: Money, date_str: Optional[str] = None) -> Money:
        if m.is_twd():
            return m
        return twd(m.amount * self.rate(date_str))

    def to_usd(self, m: Money, date_str: Optional[str] = None) -> Money:
        if m.is_usd():
            return m
        return usd(m.amount / self.rate(date_str))


# =============================================================================
# FinMind 股票基本資料（中文簡稱）
# =============================================================================
def get_stock_names() -> Dict[str, str]:
    """
    取得全市場股票代號（不含 .TW/.TWO 後綴）→ 中文簡稱對照表。

    來源：FinMind TaiwanStockInfo（單次查詢回傳全市場，非逐股票/逐日期分段查詢）。
    永久快取：公司簡稱幾乎不變動，快取檔存在即直接使用，不重新呼叫 API。
    若需強制刷新（如新股上市、公司改名），手動刪除快取檔即可。
    API 失敗時降級回傳空字典，呼叫端應以代號本身作為備援顯示名稱。
    """
    try:
        if _STOCK_INFO_CACHE_FILE.exists():
            return pickle.loads(_STOCK_INFO_CACHE_FILE.read_bytes())  # noqa: S301
    except Exception as e:
        logger.debug('[cache read error] finmind_stock_info: %s', e)

    name_map: Dict[str, str] = {}
    params = {'dataset': 'TaiwanStockInfo'}
    token = _load_finmind_token()
    if token:
        params['token'] = token

    try:
        resp = requests.get(_FINMIND_URL, params=params, timeout=30)
        resp.raise_for_status()
        raw = resp.json()
        if raw.get('status') != 200:
            logger.warning('FinMind TaiwanStockInfo 回應 status=%s: %s',
                           raw.get('status'), raw.get('msg'))
            return name_map
        for row in raw.get('data', []):
            stock_id = str(row.get('stock_id', '')).strip()
            stock_name = row.get('stock_name', '')
            if stock_id and stock_name:
                name_map[stock_id] = stock_name
    except Exception as e:
        logger.warning('FinMind TaiwanStockInfo 抓取失敗（降級）: %s', e)
        return name_map

    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _STOCK_INFO_CACHE_FILE.write_bytes(pickle.dumps(name_map))
    except Exception as e:
        logger.debug('[cache write error] finmind_stock_info: %s', e)

    return name_map


# =============================================================================
# FinMind 融資融券資料
# =============================================================================
class TWMarginData:
    """
    融資融券資料管理器（透過 FinMind API）。

    與 TWSE 進步之處：
    - 支援日期範圍查詢，343 天僅需 ~6 次 API 呼叫（原 TWSE 需 343 次）
    - 歷史區間永久快取（舊資料不改變）；當日區間每日更新
    - API 失敗時靜默降級，不影響回測主流程
    - 支援 .TW（上市）和 .TWO（上櫃）

    FinMind dataset: TaiwanStockMarginPurchaseShortSale
    Token 設定：config.yaml 的 finmind.token
    """

    def __init__(self):
        # {date_str: {symbol: {'margin_balance', 'margin_prev_balance',
        #                       'margin_change_rate', 'short_balance', 'short_change_rate'}}}
        self._data: Dict[str, Dict[str, Dict]] = {}
        self._token = _load_finmind_token()

    # ─── 資料載入 ──────────────────────────────────────────────────────────

    def load_for_dates(self, trading_dates: List[str],
                       symbols: Optional[List[str]] = None) -> None:
        """批次預載融資融券資料。

        當提供 symbols 時，以「逐股票 × 季度區塊」方式呼叫 FinMind（免費方案可用）。
        未提供 symbols 時，嘗試以「全市場 × 季度區塊」方式呼叫（需付費方案）。
        """
        needed = [d for d in trading_dates if d not in self._data]
        if not needed:
            return
        if symbols:
            self._load_by_symbols(symbols, needed)
        else:
            self._load_bulk(needed)

    def _load_by_symbols(self, symbols: List[str], trading_dates: List[str]) -> None:
        """逐股票 + 季度區塊查詢（FinMind 免費方案）。"""
        tw_symbols = [s for s in symbols if s.endswith('.TW')]
        if not tw_symbols:
            return
        chunks = self._date_chunks(trading_dates[0], trading_dates[-1])
        total_calls = len(tw_symbols) * len(chunks)
        logger.info('融資融券：逐股票模式，%d 檔 × %d 區塊 = %d 次 API',
                    len(tw_symbols), len(chunks), total_calls)
        for i, symbol in enumerate(tw_symbols):
            stock_id = symbol.replace('.TW', '')
            for chunk_start, chunk_end in chunks:
                self._fetch_range(chunk_start, chunk_end, data_id=stock_id)
                time.sleep(0.3)  # 避免 FinMind rate limit / 連線池耗盡
            if i % 10 == 9:
                logger.info('融資融券：已處理 %d/%d 檔', i + 1, len(tw_symbols))
        loaded = sum(1 for d in trading_dates if self._data.get(d))
        logger.info('融資融券：載入完成 %d/%d 日有資料', loaded, len(trading_dates))

    def _load_bulk(self, trading_dates: List[str]) -> None:
        """全市場查詢（FinMind 付費方案）。"""
        chunks = self._date_chunks(trading_dates[0], trading_dates[-1])
        logger.info('融資融券：全市場模式，%d 個區塊', len(chunks))
        for chunk_start, chunk_end in chunks:
            self._fetch_range(chunk_start, chunk_end)
        loaded = sum(1 for d in trading_dates if self._data.get(d))
        logger.info('融資融券：載入完成 %d/%d 日有資料', loaded, len(trading_dates))

    def _date_chunks(self, start: str, end: str) -> List[Tuple[str, str]]:
        """將 start~end 切分成每 _FINMIND_CHUNK_MONTHS 個月的區塊。"""
        s = datetime.strptime(start, '%Y-%m-%d').date()
        e = datetime.strptime(end, '%Y-%m-%d').date()
        chunks = []
        cur = s
        while cur <= e:
            end_month = cur.month + _FINMIND_CHUNK_MONTHS - 1
            end_year = cur.year + (end_month - 1) // 12
            end_month = (end_month - 1) % 12 + 1
            last_day = calendar.monthrange(end_year, end_month)[1]
            chunk_end = date(end_year, end_month, last_day)
            if chunk_end > e:
                chunk_end = e
            chunks.append((cur.strftime('%Y-%m-%d'), chunk_end.strftime('%Y-%m-%d')))
            next_month = end_month % 12 + 1
            next_year = end_year + (1 if end_month == 12 else 0)
            cur = date(next_year, next_month, 1)
        return chunks

    def _merge_chunk_data(self, chunk_data: Dict[str, Dict[str, Dict]]) -> None:
        """逐日期合併股票資料，避免 dict.update() 淺層覆蓋掉其他股票已寫入的當日資料。"""
        for d, syms in chunk_data.items():
            self._data.setdefault(d, {}).update(syms)

    def _fetch_range(self, start_date: str, end_date: str,
                     data_id: Optional[str] = None) -> None:
        """呼叫 FinMind API 取得日期範圍內融資融券資料。

        data_id: 股票代號（不含 .TW 後綴）。
            - 提供時：單檔查詢（FinMind 免費方案可用）
            - 不提供時：全市場查詢（需付費方案）

        快取策略：
        - end_date < 今日 → 歷史資料不改變，永久快取（無日期後綴）
        - end_date >= 今日 → 當前資料，每日快取（有日期後綴）
        """
        is_historical = datetime.strptime(end_date, '%Y-%m-%d').date() < date.today()
        id_part = f'__{data_id}' if data_id else ''
        safe_key = f'finmind_margin{id_part}__{start_date}__{end_date}'.replace('-', '')
        if is_historical:
            cache_file = _CACHE_DIR / f'{safe_key}.pkl'
        else:
            cache_file = _cache_path(safe_key)

        try:
            if cache_file.exists():
                chunk_data = pickle.loads(cache_file.read_bytes())  # noqa: S301
                self._merge_chunk_data(chunk_data)
                logger.debug('[cache hit] finmind margin %s %s ~ %s',
                             data_id or 'ALL', start_date, end_date)
                return
        except Exception:
            pass

        params = {
            'dataset': 'TaiwanStockMarginPurchaseShortSale',
            'start_date': start_date,
            'end_date': end_date,
        }
        if data_id:
            params['data_id'] = data_id
        if self._token:
            params['token'] = self._token

        for attempt in range(2):
            try:
                resp = requests.get(_FINMIND_URL, params=params, timeout=30)
                resp.raise_for_status()
                raw = resp.json()

                if raw.get('status') != 200:
                    logger.warning('FinMind API 回應 status=%s (%s %s ~ %s): %s',
                                   raw.get('status'), data_id or 'ALL',
                                   start_date, end_date, raw.get('msg'))
                    return

                chunk_data: Dict[str, Dict[str, Dict]] = {}
                for row in raw.get('data', []):
                    d = row.get('date', '')
                    stock_id = str(row.get('stock_id', '')).strip()
                    if not d or not stock_id:
                        continue
                    symbol = f'{stock_id}.TW'
                    today_bal   = float(row.get('MarginPurchaseTodayBalance', 0) or 0)
                    prev_bal    = float(row.get('MarginPurchaseYesterdayBalance', 0) or 0)
                    short_today = float(row.get('ShortSaleTodayBalance', 0) or 0)
                    short_prev  = float(row.get('ShortSaleYesterdayBalance', 0) or 0)
                    margin_change = (today_bal - prev_bal) / prev_bal if prev_bal > 0 else 0.0
                    short_change  = (short_today - short_prev) / short_prev if short_prev > 0 else 0.0
                    chunk_data.setdefault(d, {})[symbol] = {
                        'margin_balance':      today_bal,
                        'margin_prev_balance': prev_bal,
                        'margin_change_rate':  margin_change,
                        'short_balance':       short_today,
                        'short_change_rate':   short_change,
                    }

                self._merge_chunk_data(chunk_data)
                # status=200 即視為成功查詢，即使該股該區間無融資資料（chunk_data 為空）
                # 也要快取，避免每次回測重複對相同組合發送請求
                try:
                    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    cache_file.write_bytes(pickle.dumps(chunk_data))
                except Exception:
                    pass
                logger.debug('FinMind margin %s %s ~ %s: %d 日 / %d 筆資料',
                             data_id or 'ALL', start_date, end_date, len(chunk_data),
                             sum(len(v) for v in chunk_data.values()))
                return  # 成功，跳出 retry loop
            except Exception as e:
                if attempt == 0:
                    logger.debug('FinMind %s %s ~ %s 第 1 次失敗，等待 3s 後重試: %s',
                                 data_id or 'ALL', start_date, end_date, e)
                    time.sleep(3.0)
                else:
                    logger.warning('FinMind 融資融券 %s %s ~ %s 抓取失敗（降級）: %s',
                                   data_id or 'ALL', start_date, end_date, e)

    # ─── 查詢接口 ──────────────────────────────────────────────────────────

    def get_margin_change_rate(self, symbol: str, date_str: str) -> Optional[float]:
        """回傳指定股票在指定日期的融資增減率（None 表示無資料）。"""
        day_data = self._data.get(date_str, {})
        stock_data = day_data.get(symbol)
        return stock_data['margin_change_rate'] if stock_data else None

    def get_recent_avg_margin_change(self, symbol: str, dates: List[str], lookback: int) -> Optional[float]:
        """
        計算近 lookback 個交易日的融資增減率平均值。
        回傳 None 表示資料不足（少於 lookback//2 個有效資料點）。
        """
        if symbol.endswith('.TWO'):
            # 上櫃股票：TWSE API 不含，降級（視為通過）
            return None

        recent_dates = dates[-lookback:] if len(dates) >= lookback else dates
        vals = []
        for d in recent_dates:
            v = self.get_margin_change_rate(symbol, d)
            if v is not None:
                vals.append(v)

        min_required = max(1, lookback // 2)
        if len(vals) < min_required:
            return None  # 資料不足，降級
        return sum(vals) / len(vals)


# =============================================================================
# TradingView Session 過期偵測
# =============================================================================
def _load_session_meta() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """讀取 config.yaml，回傳 (expires_at, session_id, watchlist_id)。"""
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            meta = yaml.safe_load(f) or {}
        tv = meta.get('tradingview', {})
        return (
            tv.get('expires_at'),
            tv.get('session_id') or None,
            tv.get('watchlist_id') or None,
        )
    except Exception as e:
        logger.warning('無法讀取 %s: %s', CONFIG_FILE, e)
        return None, None, None


def _is_session_likely_expired(expires_at: Optional[str]) -> bool:
    if not expires_at:
        return True
    try:
        return date.today() >= datetime.strptime(expires_at, '%Y-%m-%d').date()
    except ValueError:
        return True


def _build_session_expired_error(expires_at: Optional[str] = None) -> TradingViewSessionExpired:
    if expires_at is None:
        expires_at, _, _ = _load_session_meta()
    remediation = (
        '請至 https://www.tradingview.com 重新登入，'
        '從瀏覽器 DevTools 取得 sessionid cookie，'
        '將新值填入 cloud_function/config.yaml 的 tradingview.session_id 欄位，'
        '並同步將 expires_at 改為新 cookie 的預計到期日（+30 天）。'
    )
    return TradingViewSessionExpired(expires_at=expires_at, detail=remediation)


# =============================================================================
# TradingView Watchlist（台股專版：僅保留 TWSE / TPEX）
# =============================================================================
def fetch_watchlist() -> Tuple[Dict, Dict]:
    """
    從 TradingView 取得台股觀察清單。
    僅保留 TWSE（.TW）與 TPEX（.TWO）股票。
    """
    expires_at, session_id, watchlist_id = _load_session_meta()
    if not session_id:
        raise RuntimeError(
            '設定缺少 TradingView session_id：請填入 cloud_function/config.yaml'
        )
    if not watchlist_id:
        raise RuntimeError(
            '設定缺少 TradingView watchlist_id：請填入 cloud_function/config.yaml'
        )

    url = f'https://in.tradingview.com/api/v1/symbols_list/custom/{watchlist_id}'
    headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'cookie': f'sessionid={session_id}',
        'x-requested-with': 'XMLHttpRequest',
    }

    auth_failed = False
    api_failed = False
    failure_detail = ''
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code in (401, 403):
            auth_failed = True
            failure_detail = f'HTTP {response.status_code}'
        else:
            response.raise_for_status()
            body = response.json()
            if 'symbols' not in body or not body['symbols']:
                api_failed = True
                failure_detail = '回應缺少 symbols 欄位（session 可能已過期）'
            else:
                symbols = body['symbols']
    except TradingViewSessionExpired:
        raise
    except Exception as e:
        api_failed = True
        failure_detail = f'{type(e).__name__}: {e}'

    if auth_failed:
        raise _build_session_expired_error(expires_at)
    if api_failed:
        if _is_session_likely_expired(expires_at):
            raise _build_session_expired_error(expires_at)
        raise RuntimeError(f'TradingView 暫時無法存取: {failure_detail}')

    return _parse_symbols(symbols)


def _parse_symbols(symbols: List[str]) -> Tuple[Dict, Dict]:
    """將 TradingView API 回傳的 symbols 解析為 watchlist + stock_info（僅台股）。"""
    watchlist: Dict = {}
    stock_info: Dict = {}
    current_key = None
    skipped = 0
    name_map = get_stock_names()

    for item in symbols:
        if '###' in item:
            current_key = item.strip('###\u2064')
            watchlist[current_key] = {}
        elif current_key:
            if ':' not in item:
                continue
            provider, code = item.split(':', 1)
            if provider not in watchlist[current_key]:
                watchlist[current_key][provider] = []

            if provider == 'TWSE':
                yf_code = f'{code}.TW'
                country = 'TW'
            elif provider == 'TPEX':
                yf_code = f'{code}.TWO'
                country = 'TW'
            else:
                # 非台股：跳過（台股專版）
                skipped += 1
                continue

            watchlist[current_key][provider].append(yf_code)
            stock_info[yf_code] = {
                'country': country,
                'industry': current_key,
                'provider': provider,
                'original_code': code,
                'name': name_map.get(code, code),
            }

    if skipped:
        logger.info('跳過 %d 個非台股標的（台股專版）', skipped)
    return watchlist, stock_info


# =============================================================================
# 使用者自定義 portfolio（台股專版）
# =============================================================================
def build_stock_info_from_portfolio(symbols: List[str]) -> Dict:
    """
    當使用者直接傳入 portfolio 時，建立 stock_info（台股）。
    規則：
    - 含 '.TW' → TWSE 上市
    - 含 '.TWO' → TPEX 上櫃
    - 其餘數字 → 自動補 '.TW' 後綴
    """
    stock_info: Dict = {}
    name_map = get_stock_names()
    for sym in symbols:
        sym = sym.strip()
        if sym.endswith('.TWO'):
            country, provider, yf_code = 'TW', 'TPEX', sym
        elif sym.endswith('.TW'):
            country, provider, yf_code = 'TW', 'TWSE', sym
        elif sym.isdigit():
            country, provider, yf_code = 'TW', 'TWSE', f'{sym}.TW'
        else:
            country, provider, yf_code = 'TW', 'TWSE', f'{sym}.TW'
        original_code = sym.replace('.TW', '').replace('.TWO', '')
        stock_info[yf_code] = {
            'country': country,
            'industry': 'Custom',
            'provider': provider,
            'original_code': original_code,
            'name': name_map.get(original_code, original_code),
        }
    return stock_info


# =============================================================================
# 股價歷史
# =============================================================================
def fetch_stock_history(ticker: str, period: str = DATA_PERIOD) -> pd.DataFrame:
    cache_key = f'stock__{ticker}__{period}'
    cached = _load_cache(cache_key)
    if cached is not None:
        return cached
    try:
        df = yf.Ticker(ticker).history(period=period, interval='1d')
        if df.empty:
            return pd.DataFrame()
        df = df.tz_localize(None).sort_index()
        result = df[['Open', 'High', 'Low', 'Close', 'Volume']]
        _save_cache(cache_key, result)
        return result
    except Exception as e:
        logger.debug('%s: %s', ticker, e)
        return pd.DataFrame()


_BATCH_DOWNLOAD_SIZE = 30  # yfinance 單次批次大小


def fetch_all_stock_data(stock_info: Dict) -> Dict:
    """根據 stock_info 批次抓取所有台股歷史資料。

    策略：
    1. 先命中今日快取，不需網路請求。
    2. 未快取的股票以 yf.download() 批次下載（每批 30 支，一次 HTTP 請求）。
    3. 單批失敗時降級為逐支下載（加 0.5s 間隔）。
    """
    raw_data: Dict = {}
    total = len(stock_info)

    # ── 快取命中 ──────────────────────────────────────────────────────────
    missing: List[str] = []
    for ticker in stock_info.keys():
        cached = _load_cache(f'stock__{ticker}__{DATA_PERIOD}')
        if cached is not None and len(cached) >= MIN_HISTORY_DAYS:
            raw_data[ticker] = cached
        else:
            missing.append(ticker)

    logger.info('股價快取命中 %d/%d，需下載 %d 檔', len(raw_data), total, len(missing))
    if not missing:
        return raw_data

    # ── 批次下載 ──────────────────────────────────────────────────────────
    for batch_start in range(0, len(missing), _BATCH_DOWNLOAD_SIZE):
        batch = missing[batch_start:batch_start + _BATCH_DOWNLOAD_SIZE]
        logger.debug('批次下載 %d~%d / %d', batch_start + 1, batch_start + len(batch), len(missing))
        try:
            raw = yf.download(
                ' '.join(batch),
                period=DATA_PERIOD,
                interval='1d',
                auto_adjust=True,
                progress=False,
                timeout=60,
            )
            if raw.empty:
                raise ValueError('批次回傳空 DataFrame')

            # 移除時區後一致化索引
            if getattr(raw.index, 'tz', None) is not None:
                raw.index = raw.index.tz_localize(None)
            raw = raw.sort_index()

            for ticker in batch:
                try:
                    if isinstance(raw.columns, pd.MultiIndex):
                        # 多支股票：columns = MultiIndex[(OHLCV, ticker)]
                        available = raw.columns.get_level_values(-1).unique()
                        if ticker not in available:
                            logger.debug('%s: 批次結果中無此股票資料', ticker)
                            continue
                        df = raw.xs(ticker, level=-1, axis=1).copy()
                    else:
                        # 單支股票：普通 columns
                        df = raw.copy()

                    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].dropna(how='all')
                    if len(df) < MIN_HISTORY_DAYS:
                        logger.debug('%s: 資料不足 (%d 日)，略過', ticker, len(df))
                        continue
                    raw_data[ticker] = df
                    _save_cache(f'stock__{ticker}__{DATA_PERIOD}', df)
                except Exception as e:
                    logger.debug('%s: 解析批次資料失敗: %s', ticker, e)

        except Exception as e:
            logger.warning('批次下載失敗（batch %d），改為逐支下載: %s', batch_start // _BATCH_DOWNLOAD_SIZE, e)
            for ticker in batch:
                df = fetch_stock_history(ticker)
                if not df.empty and len(df) >= MIN_HISTORY_DAYS:
                    raw_data[ticker] = df
                time.sleep(0.5)

        # 批次間短暫等待，避免連續打 API
        if batch_start + _BATCH_DOWNLOAD_SIZE < len(missing):
            time.sleep(1.0)

    logger.info('股價抓取完成: %d/%d', len(raw_data), total)
    return raw_data


# =============================================================================
# 日期對齊
# =============================================================================
def align_data_with_bfill(raw_data: Dict) -> Tuple[Dict, pd.DatetimeIndex]:
    if not raw_data:
        return {}, pd.DatetimeIndex([])

    date_stock_count: Dict = {}
    for df in raw_data.values():
        if df.empty:
            continue
        for d in df.index:
            date_stock_count[d] = date_stock_count.get(d, 0) + 1

    min_required = min(MIN_STOCKS_FOR_VALID_DAY, max(1, int(len(raw_data) * MIN_STOCKS_FOR_VALID_DAY_RATIO)))
    valid_dates = [d for d, c in date_stock_count.items() if c >= min_required]
    if not valid_dates:
        valid_dates = list(date_stock_count.keys())

    unified_dates = pd.DatetimeIndex(sorted(valid_dates))
    aligned_data = {
        t: df.reindex(unified_dates).bfill().ffill()
        for t, df in raw_data.items() if not df.empty
    }
    return aligned_data, unified_dates


def build_close_df(aligned_data: Dict) -> pd.DataFrame:
    close_dict = {t: df['Close'] for t, df in aligned_data.items() if 'Close' in df.columns}
    if not close_dict:
        return pd.DataFrame()
    return pd.DataFrame(close_dict).sort_index()


def filter_by_market(close_df: pd.DataFrame, stock_info: Dict, market: str) -> Tuple[pd.DataFrame, Dict]:
    """台股專版：market 固定為 'tw'，此函數保留相容性。"""
    if market != 'tw':
        logger.warning('台股專版引擎接收到非 tw market=%s，強制使用 tw', market)
    keep = [t for t in close_df.columns if stock_info.get(t, {}).get('country') == 'TW']
    filtered_info = {t: i for t, i in stock_info.items() if i.get('country') == 'TW'}
    return close_df[keep], filtered_info


# =============================================================================
# 基準指數（台股版：固定 ^TWII）
# =============================================================================
def fetch_benchmark_prices(symbol: str, period: str = '6y') -> Dict[str, float]:
    cache_key = f'benchmark__{symbol}__{period}'
    cached = _load_cache(cache_key)
    if cached is not None:
        return cached
    try:
        df = yf.Ticker(symbol).history(period=period, interval='1d')
        if df.empty:
            return {}
        df = df.tz_localize(None).sort_index()
        result = {d.strftime('%Y-%m-%d'): float(r['Close']) for d, r in df.iterrows() if pd.notna(r['Close'])}
        _save_cache(cache_key, result)
        return result
    except Exception as e:
        logger.warning('Benchmark %s 抓取失敗: %s', symbol, e)
        return {}


# =============================================================================
# LINE 推播（共用）
# =============================================================================
def _load_line_credentials() -> tuple[str, str]:
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        line_cfg = cfg.get('line', {})
        return line_cfg.get('channel_access_token', ''), line_cfg.get('group_id', '')
    except Exception as e:
        logger.warning('無法讀取 LINE 憑證 (%s): %s', CONFIG_FILE, e)
        return '', ''


def _load_line_quiet_days() -> int:
    """從 config.yaml 讀取「連續無交易訊號則跳過推播」的天數門檻。"""
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg = yaml.safe_load(f) or {}
        return int(cfg.get('line', {}).get('quiet_days_without_signal', 3))
    except Exception as e:
        logger.warning('無法讀取 LINE 靜默天數設定 (%s): %s', CONFIG_FILE, e)
        return 3


def push_line_message(text: str) -> dict:
    token, group_id = _load_line_credentials()
    if not (token and group_id):
        return {'sent': False, 'reason': 'LINE 憑證未設定（config.yaml）'}
    try:
        from linebot import LineBotApi
        from linebot.models import TextSendMessage
        LineBotApi(token).push_message(group_id, TextSendMessage(text=text))
        return {'sent': True}
    except Exception as e:
        logger.error('LINE 推播失敗: %s', e)
        return {'sent': False, 'reason': f'{type(e).__name__}: {e}'}


def push_session_expired_alert(expires_at: str | None) -> None:
    msg = (
        '⚠️ TA Audition Engine 警告\n'
        f'TradingView session 已失效（expires_at: {expires_at}）\n'
        '請至 tradingview.com 重新登入，\n'
        '取得新的 sessionid cookie 後填入 config.yaml。'
    )
    result = push_line_message(msg)
    logger.info('session 過期警告 LINE %s', '已發送' if result['sent'] else f'未發送（{result.get("reason", "")}）')


def push_session_expiring_soon_alert(expires_at: str | None) -> None:
    if not expires_at:
        return
    try:
        days_left = (datetime.strptime(expires_at, '%Y-%m-%d').date() - date.today()).days
        if days_left > 7:
            return
        urgency = '《立刻》' if days_left <= 0 else (f'《明天》' if days_left == 1 else f'《{days_left} 天後》')
        msg = (
            f'⏰ TA Audition Engine 到期預警\n'
            f'TradingView session 將{urgency}失效\n'
            f'到期日：{expires_at}（剩 {max(days_left, 0)} 天）\n'
            '請提前至 tradingview.com 重新登入。'
        )
        result = push_line_message(msg)
        logger.info('session 到期預警 LINE %s（剩 %d 天）',
                    '已發送' if result['sent'] else f'未發送', days_left)
    except Exception as e:
        logger.warning('到期預警推播失敗: %s', e)


def push_error_alert(code: str, message: str) -> None:
    msg = (
        f'❌ TA Audition Engine 執行失敗\n'
        f'錯誤代碼：{code}\n'
        f'訊息：{message}\n'
        '請至 Cloud Logging 查看完整 traceback。'
    )
    result = push_line_message(msg)
    logger.info('錯誤告警 LINE %s（%s）', '已發送' if result['sent'] else '未發送', code)
