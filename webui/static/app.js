/**
 * TA Audition Engine — 前端主邏輯（台股版）
 * 職責：
 *  1. 頁面載入時從 /api/config 讀取 config.yaml 填入表單
 *  2. 執行回測 → /api/backtest → 渲染權益曲線、績效指標、持倉、交易、產業排名
 *  3. 儲存策略 → /api/config（POST）
 */

// ── 全域狀態 ─────────────────────────────────────────────────────────────────
let equityChart = null;

// ── DOM 就緒 ──────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  console.log('[app] DOMContentLoaded — 初始化開始');
  setStatus('連線中…', 'running');
  try {
    initChart();
    console.log('[app] ECharts 初始化完成');
  } catch (e) {
    console.error('[app] ECharts 初始化失敗（CDN 未載入？）:', e);
  }
  loadConfig();
});

// ── 工具 ─────────────────────────────────────────────────────────────────────
function $(id) { return document.getElementById(id); }

function setStatus(msg, state = '') {
  $('status-text').textContent = msg;
  const dot = $('status-dot');
  dot.className = 'status-dot' + (state ? ' ' + state : '');
}

// ── Tab 切換 ──────────────────────────────────────────────────────────────────
function switchTab(id) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  const idx = {'tab-base':0,'tab-buy':1,'tab-sell':2,'tab-risk':3}[id] ?? 0;
  document.querySelectorAll('.tab-btn')[idx].classList.add('active');
}

function setSelectValue(id, value) {
  const el = $(id);
  if (el && value && [...el.options].some(o => o.value === String(value))) el.value = String(value);
}

// ── 條件欄位填入 ──────────────────────────────────────────────────────────────
function fillConditions(bt) {
  const bc  = bt.buy_conditions    || {};
  const sc  = bt.sell_conditions   || {};
  const rs  = bt.rebalance_strategy || {};
  const mf  = bt.market_filter     || {};
  const slr = bt.stop_loss_reentry || {};
  const fee = (bt.fees || {}).tw   || {};

  // 市場機制
  setCheck('mf-enabled',     mf.enabled);
  setNum  ('mf-ma_window',   mf.ma_window);
  setCheck('slr-enabled',    slr.enabled);

  // 費率
  setNum('fee-buy-rate',  fee.buy_rate);
  setNum('fee-sell-rate', fee.sell_rate);
  setNum('fee-min-fee',   fee.min_fee);

  // Sharpe 篩選
  setCheck('buy-sharpe-rank-enabled',       bc.sharpe_rank?.enabled);
  setNum  ('buy-sharpe-rank-top_n',         bc.sharpe_rank?.top_n);
  setCheck('buy-sharpe-threshold-enabled',  bc.sharpe_threshold?.enabled);
  setNum  ('buy-sharpe-threshold-threshold',bc.sharpe_threshold?.threshold);
  setCheck('buy-growth-streak-enabled',     bc.growth_streak?.enabled);
  setNum  ('buy-growth-streak-days',        bc.growth_streak?.days);
  setNum  ('buy-growth-streak-percentile',  bc.growth_streak?.percentile);
  setCheck('buy-sort-sharpe-enabled',       bc.sort_sharpe?.enabled);
  setCheck('buy-sort-margin-enabled',        bc.sort_margin?.enabled);
  setNum  ('buy-sort-margin-days',           bc.sort_margin?.days);

  // 台股特色買進條件
  setCheck('buy-margin-net-long-enabled',          bc.margin_net_long?.enabled);
  setNum  ('buy-margin-net-long-days',             bc.margin_net_long?.days);
  setNum  ('buy-margin-net-long-min_avg_change',   bc.margin_net_long?.min_avg_change);
  setCheck('buy-no-parabolic-enabled',             bc.no_parabolic?.enabled);
  setNum  ('buy-no-parabolic-lookback',            bc.no_parabolic?.lookback);
  setNum  ('buy-no-parabolic-max_gain',            bc.no_parabolic?.max_gain);

  // 再平衡策略
  setSelectValue('rebalance-type',          rs.type);
  setNum  ('rebalance-top_n',               rs.top_n);
  setNum  ('rebalance-sharpe_threshold',    rs.sharpe_threshold);

  // 賣出條件
  setCheck('sell-sharpe-fail-enabled',      sc.sharpe_fail?.enabled);
  setNum  ('sell-sharpe-fail-periods',      sc.sharpe_fail?.periods);
  setNum  ('sell-sharpe-fail-top_n',        sc.sharpe_fail?.top_n);
  setCheck('sell-drawdown-enabled',         sc.drawdown?.enabled);
  setNum  ('sell-drawdown-threshold',       sc.drawdown?.threshold);
  setCheck('sell-growth-fail-enabled',      sc.growth_fail?.enabled);
  setNum  ('sell-growth-fail-days',         sc.growth_fail?.days);
  setCheck('sell-not-selected-enabled',     sc.not_selected?.enabled);
  setNum  ('sell-not-selected-periods',     sc.not_selected?.periods);

  // 台股特色賣出條件
  setCheck('sell-margin-collapse-enabled',    sc.margin_collapse?.enabled);
  setNum  ('sell-margin-collapse-days',       sc.margin_collapse?.days);
  setNum  ('sell-margin-collapse-threshold',  sc.margin_collapse?.threshold);
}

function setCheck(id, val) {
  const el = $(id);
  if (el && val !== undefined) el.checked = Boolean(val);
}
function setNum(id, val) {
  const el = $(id);
  if (el && val !== undefined && val !== null) el.value = val;
}

// ── 條件欄位收集 ──────────────────────────────────────────────────────────────
function collectConditions() {
  const chk = id => $(id)?.checked ?? false;
  const num = id => parseFloat($(id)?.value) || 0;
  const int = id => parseInt($(id)?.value, 10) || 0;
  return {
    market_filter: {
      enabled:   chk('mf-enabled'),
      indicator: 'twii_200ma',
      ma_window: int('mf-ma_window') || 200,
    },
    stop_loss_reentry: {
      enabled: chk('slr-enabled'),
      type:    'price_recovery',
    },
    fees: {
      tw: {
        buy_rate:  num('fee-buy-rate')  || 0.0015,
        sell_rate: num('fee-sell-rate') || 0.0045,
        min_fee:   int('fee-min-fee')   || 20,
      },
    },
    buy_conditions: {
      sharpe_rank:      { enabled: chk('buy-sharpe-rank-enabled'),      top_n: int('buy-sharpe-rank-top_n') },
      sharpe_threshold: { enabled: chk('buy-sharpe-threshold-enabled'), threshold: num('buy-sharpe-threshold-threshold') },
      growth_streak:    { enabled: chk('buy-growth-streak-enabled'),    days: int('buy-growth-streak-days'), percentile: int('buy-growth-streak-percentile') },
      sort_sharpe:      { enabled: chk('buy-sort-sharpe-enabled') },
      sort_margin:      { enabled: chk('buy-sort-margin-enabled'), days: int('buy-sort-margin-days') },
      margin_net_long:  {
        enabled: chk('buy-margin-net-long-enabled'),
        days: int('buy-margin-net-long-days'),
        min_avg_change: num('buy-margin-net-long-min_avg_change'),
      },
      no_parabolic: {
        enabled: chk('buy-no-parabolic-enabled'),
        lookback: int('buy-no-parabolic-lookback'),
        max_gain: num('buy-no-parabolic-max_gain'),
      },
    },
    sell_conditions: {
      sharpe_fail:   { enabled: chk('sell-sharpe-fail-enabled'),    periods: int('sell-sharpe-fail-periods'), top_n: int('sell-sharpe-fail-top_n') },
      drawdown:      { enabled: chk('sell-drawdown-enabled'),        threshold: num('sell-drawdown-threshold') },
      growth_fail:   { enabled: chk('sell-growth-fail-enabled'),     days: int('sell-growth-fail-days'), threshold: 0 },
      not_selected:  { enabled: chk('sell-not-selected-enabled'),    periods: int('sell-not-selected-periods') },
      margin_collapse: {
        enabled: chk('sell-margin-collapse-enabled'),
        days: int('sell-margin-collapse-days'),
        threshold: num('sell-margin-collapse-threshold'),
      },
    },
    rebalance_strategy: {
      type:             $('rebalance-type')?.value || 'delayed',
      top_n:            int('rebalance-top_n'),
      sharpe_threshold: num('rebalance-sharpe_threshold'),
    },
  };
}

// ── 圖表初始化 ────────────────────────────────────────────────────────────────
function initChart() {
  equityChart = echarts.init($('equity-chart'));
  equityChart.setOption({
    backgroundColor: '#0d1117',
    animation: false,
    grid: { left: 70, right: 20, top: 16, bottom: 36 },
    xAxis: {
      type: 'category', data: [],
      axisLine:  { lineStyle: { color: '#30363d' } },
      axisLabel: { color: '#8b949e', fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value', scale: true, position: 'right',
      axisLine:  { lineStyle: { color: '#30363d' } },
      axisLabel: { color: '#8b949e', fontSize: 10, formatter: v => v.toLocaleString() },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#161b22', borderColor: '#30363d',
      textStyle: { color: '#c9d1d9', fontSize: 11 },
    },
    legend: {
      top: 0, right: 20,
      textStyle: { color: '#8b949e', fontSize: 10 },
    },
    dataZoom: [{ type: 'inside', xAxisIndex: 0 }],
    series: [],
  });
}

// ── 載入 config ───────────────────────────────────────────────────────────────
async function loadConfig() {
  console.log('[app] loadConfig()');
  try {
    const resp = await fetch('/api/config');
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error?.message || '載入失敗');
    const cfg = data.config || {};
    const bt = cfg.backtest || {};
    const tv = cfg.tradingview || {};
    const line = cfg.line || {};

    // 憑證
    setVal('tv-session-id', tv.session_id);
    setVal('tv-watchlist-id', tv.watchlist_id);
    setVal('tv-expires-at', tv.expires_at);
    setVal('line-token', line.channel_access_token);
    setVal('line-group', line.group_id);

    // 策略基本
    setVal('initial-capital', bt.initial_capital);
    setVal('amount-per-stock', bt.amount_per_stock);
    setVal('max-positions', bt.max_positions);
    setVal('start-date', bt.start_date);
    setVal('end-date', bt.end_date || '');
    setSelectValue('rebalance-freq', bt.rebalance_freq);

    fillConditions(bt);

    setStatus('就緒', 'ok');
    console.log('[app] config 載入完成');
  } catch (e) {
    console.error('[app] loadConfig error:', e);
    setStatus('config 載入失敗：' + e.message, 'error');
  }
}

function setVal(id, val) {
  const el = $(id);
  if (el && val !== undefined && val !== null) el.value = val;
}

// ── 執行回測 ──────────────────────────────────────────────────────────────────
async function runBacktest() {
  console.log('[app] runBacktest()');
  setStatus('回測中…', 'running');
  $('run-btn').disabled = true;

  const params = buildBacktestParams();
  console.log('[app] backtest params:', params);

  try {
    const resp = await fetch('/api/backtest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ backtest: params }),
    });
    const data = await resp.json();
    if (!data.ok) {
      const err = data.error || {};
      setStatus(`❌ ${err.code}: ${err.message}`, 'error');
      return;
    }
    renderResults(data);
    const meta = data.meta || {};
    const marginMsg = meta.margin_data_loaded ? '（融資資料已載入）' : '';
    setStatus(`✅ 完成 ${meta.execution_time_ms}ms | ${meta.symbols_count} 檔台股 ${marginMsg}`, 'ok');
  } catch (e) {
    console.error('[app] runBacktest error:', e);
    setStatus('❌ 連線失敗：' + e.message, 'error');
  } finally {
    $('run-btn').disabled = false;
  }
}

function buildBacktestParams() {
  const cond = collectConditions();
  return {
    market: 'tw',
    initial_capital: parseInt($('initial-capital')?.value) || 1000000,
    amount_per_stock: parseInt($('amount-per-stock')?.value) || 100000,
    max_positions:    parseInt($('max-positions')?.value)   || 10,
    start_date: $('start-date')?.value || '2025-01-01',
    end_date:   $('end-date')?.value   || null,
    rebalance_freq: $('rebalance-freq')?.value || 'weekly',
    ...cond,   // 包含 market_filter / stop_loss_reentry / fees / buy_conditions / sell_conditions
  };
}

// ── 渲染結果 ──────────────────────────────────────────────────────────────────
function renderResults(data) {
  const { result, holdings, trades, equity_curve, benchmark_curve, benchmark_name, market_regime } = data;

  renderMetrics(result);
  renderChart(equity_curve, benchmark_curve, benchmark_name, market_regime || []);
  renderHoldings(holdings || []);
  renderTrades(trades || []);
  renderIndustry(holdings || []);
  renderMarketRegimeBadge(market_regime || []);
}

function renderMarketRegimeBadge(regime) {
  const badge = $('market-regime-badge');
  if (!badge || !regime.length) return;
  const last = regime[regime.length - 1];
  const bearDays = regime.filter(r => r.bearish).length;
  const ratio = Math.round(bearDays / regime.length * 100);
  if (last.bearish) {
    badge.textContent = `⚠️ 空頭 | 空頭天數 ${ratio}%`;
    badge.style.cssText = 'display:inline-block;background:#3d1c1c;color:#ef5350;margin-left:8px;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;';
  } else {
    badge.textContent = `✅ 多頭 | 空頭天數 ${ratio}%`;
    badge.style.cssText = 'display:inline-block;background:#1c3a2a;color:#4caf50;margin-left:8px;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;';
  }
}

function renderMetrics(result) {
  const grid = $('metrics-grid');
  if (!result || !grid) return;
  const items = [
    { label: '總報酬', value: result.total_return, cls: pct2cls(result.total_return) },
    { label: '年化報酬', value: result.annualized_return, cls: pct2cls(result.annualized_return) },
    { label: '最大回撤', value: result.max_drawdown, cls: 'metric-red' },
    { label: 'Sharpe', value: result.sharpe_ratio },
    { label: '勝率', value: result.win_rate },
    { label: '總交易', value: result.total_trades },
    { label: '初始資金', value: result.initial_capital },
    { label: '最終資產', value: result.final_equity },
  ];
  grid.innerHTML = items.map(({ label, value, cls }) =>
    `<div class="metric-card ${cls||''}"><div class="metric-label">${label}</div><div class="metric-value">${value}</div></div>`
  ).join('');
}

function pct2cls(s) {
  if (!s) return '';
  return s.startsWith('-') ? 'metric-red' : 'metric-green';
}

function renderChart(equity, benchmark, benchmarkName, regime) {
  if (!equityChart || !equity?.length) return;
  const dates = equity.map(p => p.date);
  const eqVals = equity.map(p => p.equity);
  const bmVals = benchmark?.length
    ? dates.map(d => { const p = benchmark.find(b => b.date === d); return p?.equity ?? null; })
    : [];

  // 計算空頭連續區間，產生 markArea 遮罩
  const regimeMap = {};
  (regime || []).forEach(r => { regimeMap[r.date] = r.bearish; });
  const bearAreas = [];
  let bearStart = null;
  dates.forEach((d, i) => {
    const isBear = regimeMap[d] === true;
    if (isBear && bearStart === null) bearStart = d;
    if (!isBear && bearStart !== null) {
      bearAreas.push([{ xAxis: bearStart }, { xAxis: dates[i - 1] || d }]);
      bearStart = null;
    }
  });
  if (bearStart !== null) bearAreas.push([{ xAxis: bearStart }, { xAxis: dates[dates.length - 1] }]);

  const series = [
    {
      name: '策略', type: 'line', data: eqVals,
      lineStyle: { color: '#58a6ff', width: 1.5 },
      itemStyle: { color: '#58a6ff' }, symbol: 'none',
      markArea: bearAreas.length ? {
        silent: true,
        itemStyle: { color: 'rgba(239,83,80,0.08)' },
        data: bearAreas,
      } : undefined,
    },
  ];
  if (bmVals.length) {
    series.push({
      name: benchmarkName || '台灣加權', type: 'line', data: bmVals,
      lineStyle: { color: '#8b949e', width: 1, type: 'dashed' },
      itemStyle: { color: '#8b949e' }, symbol: 'none',
    });
  }
  equityChart.setOption({ xAxis: { data: dates }, series });
  $('chart-meta') && ($('chart-meta').textContent = `${dates[0] || ''} ~ ${dates[dates.length-1] || ''}`);
}

function fmtSymbol(symbol, name) {
  return name && name !== symbol ? `${symbol}(${name})` : symbol;
}

function renderHoldings(holdings) {
  const tbody = document.querySelector('#holdings-table tbody');
  if (!tbody) return;
  if (!holdings.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="placeholder-text">目前無持倉</td></tr>';
    return;
  }
  tbody.innerHTML = holdings.map(h => `
    <tr>
      <td class="symbol-link" onclick="openStockChart('${h.symbol}')">🇹🇼 ${fmtSymbol(h.symbol, h.name)}</td>
      <td>${h.industry || '—'}</td>
      <td>${(h.shares||0).toLocaleString()}</td>
      <td>${h.avg_cost || '—'}</td>
      <td>${h.current_price || '—'}</td>
      <td>${(h.market_value_twd||0).toLocaleString()}</td>
      <td class="${h.pnl_pct>=0?'green':'red'}">${((h.pnl_pct||0)*100).toFixed(2)}%</td>
      <td>${h.buy_date || '—'}</td>
    </tr>
  `).join('');
}

// ── 交易記錄分頁 ──────────────────────────────────────────────────────────────
const TRADES_PAGE_SIZE = 20;
let _allTrades = [];
let _tradesPage = 0;

function renderTrades(trades) {
  _allTrades = [...trades].reverse(); // 最新在前
  _tradesPage = 0;
  _renderTradesPage();
}

function tradesPagerGo(delta) {
  const totalPages = Math.ceil(_allTrades.length / TRADES_PAGE_SIZE);
  _tradesPage = Math.max(0, Math.min(_tradesPage + delta, totalPages - 1));
  _renderTradesPage();
}

function _renderTradesPage() {
  const tbody = document.querySelector('#trades-table tbody');
  if (!tbody) return;
  if (!_allTrades.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="placeholder-text">無交易記錄</td></tr>';
    $('trades-pager').style.display = 'none';
    $('trades-label').textContent = '近期交易';
    return;
  }
  const totalPages = Math.ceil(_allTrades.length / TRADES_PAGE_SIZE);
  const start = _tradesPage * TRADES_PAGE_SIZE;
  const page = _allTrades.slice(start, start + TRADES_PAGE_SIZE);

  tbody.innerHTML = page.map(t => `
    <tr>
      <td>${t.date}</td>
      <td class="symbol-link" onclick="openStockChart('${t.symbol}')">${fmtSymbol(t.symbol, t.name)}</td>
      <td class="${t.type==='buy'?'green':'red'}">${t.type==='buy'?'🟢 買入':'🔴 賣出'}</td>
      <td>${(t.shares||0).toLocaleString()}</td>
      <td>${t.price || '—'}</td>
      <td>${t.amount_twd || '—'}</td>
      <td class="${(t.profit||'').startsWith('-')?'red':'green'}">${t.profit || '—'}</td>
      <td style="font-size:10px;color:var(--text-muted)">${t.reason || '—'}</td>
    </tr>
  `).join('');

  $('trades-label').textContent = `全部交易（共 ${_allTrades.length} 筆）`;
  $('trades-page-info').textContent = `第 ${_tradesPage + 1} / ${totalPages} 頁`;
  $('trades-prev').disabled = _tradesPage === 0;
  $('trades-next').disabled = _tradesPage >= totalPages - 1;
  $('trades-pager').style.display = totalPages > 1 ? 'flex' : 'none';
}

function renderIndustry(holdings) {
  const el = $('industry-list');
  if (!el) return;
  if (!holdings.length) {
    el.innerHTML = '<p class="empty-state">執行回測後顯示</p>';
    return;
  }
  const counts = {};
  holdings.forEach(h => { counts[h.industry || '未分類'] = (counts[h.industry || '未分類'] || 0) + 1; });
  const sorted = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  el.innerHTML = sorted.map(([ind, cnt]) =>
    `<div class="industry-item"><span class="industry-name">${ind}</span><span class="industry-count">${cnt}</span></div>`
  ).join('');
}

// ── 儲存策略 ──────────────────────────────────────────────────────────────────
async function deployConfig() {
  console.log('[app] deployConfig()');
  const cond = collectConditions();
  const payload = {
    tradingview: {
      session_id:  $('tv-session-id')?.value || '',
      watchlist_id: $('tv-watchlist-id')?.value || '',
      expires_at:  $('tv-expires-at')?.value || '',
    },
    line: {
      channel_access_token: $('line-token')?.value || '',
      group_id: $('line-group')?.value || '',
    },
    backtest: {
      market: 'tw',
      initial_capital: parseInt($('initial-capital')?.value) || 1000000,
      amount_per_stock: parseInt($('amount-per-stock')?.value) || 100000,
      max_positions:    parseInt($('max-positions')?.value) || 10,
      start_date: $('start-date')?.value || '',
      end_date:   $('end-date')?.value || null,
      rebalance_freq: $('rebalance-freq')?.value || 'weekly',
      ...cond,
    },
  };
  try {
    setStatus('儲存中…', 'running');
    const resp = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.ok) {
      setStatus('✅ 策略已儲存至 config.yaml', 'ok');
    } else {
      setStatus('❌ 儲存失敗：' + (data.error?.message || '未知錯誤'), 'error');
    }
  } catch (e) {
    setStatus('❌ 連線失敗：' + e.message, 'error');
  }
}

// ── 重新載入 ──────────────────────────────────────────────────────────────────
function reloadConfig() {
  setStatus('重新載入中…', 'running');
  loadConfig();
}
// ── 個股圖表 Modal ──────────────────────────────────────────────────────────
let _stockChart = null;
let _stockSym = '';

async function openStockChart(symbol) {
  _stockSym = symbol;
  $('stock-modal-title').textContent = symbol;
  $('stock-modal').style.display = 'flex';
  $('stock-modal-loading').style.display = 'block';
  $('stock-modal-chart').style.display = 'none';
  $('stock-modal-error').style.display = 'none';
  await _fetchStock(symbol);
}

function closeStockModal() {
  $('stock-modal').style.display = 'none';
  if (_stockChart) { _stockChart.dispose(); _stockChart = null; }
}

async function reloadStockChart() {
  if (!_stockSym) return;
  $('stock-modal-loading').style.display = 'block';
  $('stock-modal-chart').style.display = 'none';
  $('stock-modal-error').style.display = 'none';
  await _fetchStock(_stockSym);
}

async function _fetchStock(symbol) {
  const days = $('stock-chart-days')?.value || 120;
  try {
    const resp = await fetch(`/api/stock_chart/${encodeURIComponent(symbol)}?days=${days}`);
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error?.message || '資料取得失敗');
    _renderStockModal(data);
  } catch(e) {
    $('stock-modal-loading').style.display = 'none';
    const errEl = $('stock-modal-error');
    errEl.style.display = 'block';
    errEl.textContent = `載入失敗：${e.message}`;
  }
}

function _renderStockModal(data) {
  $('stock-modal-loading').style.display = 'none';
  const el = $('stock-modal-chart');
  el.style.display = 'block';
  if (_stockChart) _stockChart.dispose();
  _stockChart = echarts.init(el, 'dark');

  const ohlcv   = data.ohlcv   || [];
  const margin  = data.margin  || [];
  const holding = data.holding || [];

  const kDates  = ohlcv.map(d => d.date);
  const kData   = ohlcv.map(d => [d.open, d.close, d.low, d.high]);
  const volData = ohlcv.map(d => ({
    value: d.volume,
    itemStyle: { color: d.close >= d.open ? '#ef5350' : '#26a69a' }
  }));
  const mDates      = margin.map(d => d.date);
  const mBalance    = margin.map(d => d.margin_balance);
  const shortBal    = margin.map(d => d.short_balance);
  const hDates      = holding.map(d => d.date);
  const hPct        = holding.map(d => d.pct);

  const hasMargin  = margin.length  > 0;
  const hasHolding = holding.length > 0;

  // ── 動態建立格子（K線 / 量 / 融資 / 融券 / 大戶）──────────────────────
  // 每個 panel 的定義：[高度%, 標題, 是否有資料]
  const panels = [
    { h: 36, label: 'K 線',   has: true        },
    { h: 10, label: '成交量', has: true        },
    { h: 12, label: '融資餘額（千股）', has: hasMargin  },
    { h: 10, label: '融券餘額（千股）', has: hasMargin  },
    { h: 12, label: '外資持股比例（%）', has: hasHolding },
  ].filter(p => p.has);

  // 計算各格 top，留 slider=20px、上邊距=30px
  const TOP_OFFSET = 30, BOTTOM_RESERVE = 22, GAP = 2;
  const totalH = 100 - TOP_OFFSET / 5.2 - BOTTOM_RESERVE / 5.2;
  const totalParts = panels.reduce((s, p) => s + p.h, 0);
  let curTop = TOP_OFFSET;
  const heights = panels.map(p => Math.round(p.h / totalParts * (el.clientHeight - TOP_OFFSET - BOTTOM_RESERVE * 5)));

  // 轉成 %（以 clientHeight 為底）
  const ch = el.clientHeight || 520;
  panels.forEach((p, i) => {
    p.topPx  = curTop;
    p.heightPx = Math.round(p.h / totalParts * (ch - TOP_OFFSET - BOTTOM_RESERVE * 5));
    curTop += p.heightPx + GAP;
  });

  const grids   = panels.map(p => ({ left: 60, right: 12, top: p.topPx, height: p.heightPx }));
  const xAxes   = panels.map((p, i) => ({
    type: 'category',
    data: [0,2,3,4].includes(i) ? (i===0||i===1 ? kDates : i===2||i===3 ? mDates : hDates) : kDates,
    gridIndex: i,
    axisLabel: i === panels.length - 1
      ? { fontSize: 8, color: '#aaa', rotate: 15 }
      : { show: false },
    axisLine: { lineStyle: { color: '#333' } },
    axisTick: { show: false },
  }));

  // 修正 xAxis data 對應
  const dataForGrid = (i) => {
    const label = panels[i].label;
    if (label === '融資餘額（千股）' || label === '融券餘額（千股）') return mDates;
    if (label === '大戶持股（集保%）') return hDates;
    return kDates;
  };
  xAxes.forEach((ax, i) => { ax.data = dataForGrid(i); });

  const yAxes = panels.map((p, i) => ({
    gridIndex: i,
    scale: true,
    splitLine: { lineStyle: { color: i === 0 ? '#1e2530' : '#1a1a2a' } },
    axisLabel: { fontSize: 8, color: '#888' },
    name: p.label,
    nameLocation: 'start',
    nameGap: 4,
    nameTextStyle: { fontSize: 8, color: '#666', align: 'left' },
  }));

  const series = [];
  const dzIdx  = panels.map((_, i) => i);

  panels.forEach((p, gi) => {
    const yi = gi;
    if (p.label === 'K 線') {
      series.push({
        name: 'K 線', type: 'candlestick',
        xAxisIndex: gi, yAxisIndex: yi, data: kData,
        itemStyle: { color: '#ef5350', color0: '#26a69a', borderColor: '#ef5350', borderColor0: '#26a69a' },
      });
    } else if (p.label === '成交量') {
      series.push({
        name: '成交量', type: 'bar',
        xAxisIndex: gi, yAxisIndex: yi, data: volData, barMaxWidth: 6,
      });
    } else if (p.label === '融資餘額（千股）') {
      series.push({
        name: '融資餘額', type: 'line',
        xAxisIndex: gi, yAxisIndex: yi, data: mBalance,
        smooth: false, symbol: 'none',
        lineStyle: { color: '#f0c27f', width: 1.5 },
        areaStyle: { color: 'rgba(240,194,127,0.12)' },
      });
    } else if (p.label === '融券餘額（千股）') {
      series.push({
        name: '融券餘額', type: 'line',
        xAxisIndex: gi, yAxisIndex: yi, data: shortBal,
        smooth: false, symbol: 'none',
        lineStyle: { color: '#ef9a9a', width: 1.5 },
        areaStyle: { color: 'rgba(239,154,154,0.10)' },
      });
    } else if (p.label === '外資持股比例（%）') {
      series.push({
        name: '外資持股%', type: 'line',
        xAxisIndex: gi, yAxisIndex: yi, data: hPct,
        smooth: true, symbol: 'none',
        lineStyle: { color: '#82aaff', width: 1.5 },
        areaStyle: { color: 'rgba(130,170,255,0.10)' },
      });
    }
  });

  _stockChart.setOption({
    backgroundColor: '#0d1117',
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', lineStyle: { color: '#555' } },
      backgroundColor: '#1c2128',
      borderColor: '#333',
      textStyle: { fontSize: 11, color: '#ccc' },
      formatter: params => {
        if (!params.length) return '';
        const date = params[0].axisValue;
        const lines = params.map(p => {
          if (!p.seriesName || p.seriesName === 'K 線') return null;
          const v = Array.isArray(p.value)
            ? `開:${p.value[0]} 收:${p.value[1]} 低:${p.value[2]} 高:${p.value[3]}`
            : (typeof p.value === 'object' ? (p.value?.value ?? '—') : (p.value ?? '—'));
          return `${p.marker}${p.seriesName}: <b>${v}</b>`;
        }).filter(Boolean);
        // K線單獨處理
        const kp = params.find(p => p.seriesName === 'K 線');
        const kLine = kp && Array.isArray(kp.value)
          ? `${kp.marker}開:${kp.value[0]} 收:${kp.value[1]} 低:${kp.value[2]} 高:${kp.value[3]}`
          : null;
        return `<b>${date}</b><br/>${[kLine, ...lines].filter(Boolean).join('<br/>')}`;
      },
    },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    dataZoom: [
      { type: 'inside', xAxisIndex: dzIdx, start: 0, end: 100 },
      { type: 'slider', xAxisIndex: dzIdx, height: 16, bottom: 4,
        borderColor: '#333', fillerColor: 'rgba(80,100,130,0.3)', handleStyle: { color: '#555' } },
    ],
    legend: {
      top: 4, right: 8,
      textStyle: { fontSize: 9, color: '#aaa' },
      data: series.map(s => s.name).filter(n => n !== 'K 線' && n !== '成交量'),
    },
    grid:   grids,
    xAxis:  xAxes,
    yAxis:  yAxes,
    series,
  });
}

// ESC 關閉 Modal
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeStockModal();
});