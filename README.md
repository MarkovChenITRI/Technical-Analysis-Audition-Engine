# Technical Analysis Audition Engine

**台股量化策略海選引擎**：結合 Sharpe 排名 + 融資融券訊號 + 橫盤蓄積過濾，在本地 WebUI 設計策略並回測，滿意後將 `cloud_function/` 部署至 Google Cloud Function；GCF 每日由 Cloud Scheduler 觸發，跑當日回測並透過 LINE Bot 推播持倉建議。

> 本專案為 Keep-Buying-Audition-Engine 的**台股專版**，架構完全相同，策略調整為「Sharpe 體質 + 機構資金流入 + 橫盤蓄積」。

---

## 策略核心

| 步驟 | 訊號 | 說明 |
|---|---|---|
| 1 | Sharpe Rank | 篩出相對強度前 N 的台股 |
| 2 | Sharpe Threshold | 確保 Sharpe > 閾值（預設 0.5） |
| 3 | Growth Streak | Sharpe 排名連續上升 |
| 4 | **融資淨增** | TWSE 融資餘額近 N 日持續增加 |
| 5 | **無爆漲** | 近 M 日漲幅不超過 max_gain |

---

## 工作流總覽

```
WebUI 調整策略參數 → 回測視覺化確認
        ↓ 儲存策略（寫入 config.yaml）
cloud_function/config.yaml（憑證 + 策略）
        ↓ git push → GitHub Actions 自動部署
Google Cloud Function（Python 3.12）
        ↓ Cloud Scheduler 每日觸發
LINE Bot 推播台股持倉建議
        ↓ demo.py 驗證 GCF endpoint
確認部署成功
```

---

## 目錄結構

```
Technical_Analysis_AE/
├── cloud_function/      ← 上傳至 GCF 的完整套件（單層，無子目錄）
│   ├── main.py              GCF entry point（hello_http）
│   ├── engine.py            回測引擎：Sharpe + 融資 + 橫盤指標
│   ├── data.py              資料層：TradingView / yfinance / TWSE 融資 / FX
│   ├── config.yaml          憑證 + 策略參數（唯一配置來源）
│   └── requirements.txt
│
├── dashboard.py         ← 本地 WebUI 入口（Flask）
├── webui/               ← WebUI 前端資產
│
├── demo.py              ← GCF endpoint 驗證客戶端
└── docs/API_SPEC.md     ← GCF HTTP 介面契約
```

---

## 本地開發環境建置

本專案以 **[uv](https://docs.astral.sh/uv/)** 管理本地 Python 環境，鎖定 Python 3.12。

```powershell
# 1. 在專案根目錄初始化 Python 3.12 環境
uv init -p 3.12

# 2. 建立虛擬環境（.venv）
uv venv

# 3. 安裝所有相依套件
uv pip install -r cloud_function/requirements.txt

# 4. 啟動 WebUI
uv run python dashboard.py
# 瀏覽器開 http://127.0.0.1:5000
```

---

## 設定 config.yaml

```yaml
# ── 憑證區 ─────────────────────────────────────────────────────
tradingview:
  session_id: "你的 sessionid cookie"
  watchlist_id: "台股觀察清單 ID"
  expires_at: "YYYY-MM-DD"

line:
  channel_access_token: ""
  group_id: ""

# ── 策略區（由 WebUI「儲存策略」按鈕寫入）──────────────────
backtest:
  initial_capital: 1000000
  amount_per_stock: 100000
  max_positions: 10
  market: tw              # ← 鐵則：只有台股
  start_date: "2025-01-01"
  rebalance_freq: weekly
  buy_conditions:
    sharpe_rank:      { enabled: true,  top_n: 15 }
    sharpe_threshold: { enabled: true,  threshold: 0.5 }
    growth_streak:    { enabled: true,  days: 2, percentile: 30 }
    sort_sharpe:      { enabled: true }
    margin_net_long:  { enabled: true,  days: 5, min_avg_change: 0.0 }
    no_parabolic:     { enabled: true,  lookback: 10, max_gain: 0.15 }
  sell_conditions:
    sharpe_fail:      { enabled: true,  periods: 2, top_n: 15 }
    drawdown:         { enabled: true,  threshold: 0.10 }
    margin_collapse:  { enabled: false, days: 3, threshold: -0.30 }
  rebalance_strategy:
    type: delayed
    top_n: 5
    sharpe_threshold: 0
```

**TradingView sessionid 取得方式**：登入 TradingView → DevTools（F12）→ Application → Cookies → 複製 `sessionid` 值 → 填入 `tradingview.session_id`，並將 `expires_at` 設為今日 +30 天。

**台股觀察清單**：在 TradingView 建立僅含台股的 watchlist，watchlist ID 可從 URL 或 API 回應取得。

---

## 部署 GCF

將整個 `cloud_function/` 目錄（含 `config.yaml`）上傳至 Google Cloud Function：

```bash
gcloud functions deploy tw-ta-audition-engine \
  --runtime python312 \
  --trigger-http \
  --allow-unauthenticated \
  --entry-point hello_http \
  --source cloud_function/ \
  --region asia-east1 \
  --memory 512MB \
  --timeout 300s
```

---

## 關鍵設計說明

### TWSE 融資融券資料

本專案透過 TWSE 公開 API 取得每日融資融券資料：
- 端點：`https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN`
- 快取策略：每日 pickle 快取（同 yfinance 機制）
- **降級保護**：API 失敗時條件自動通過（不阻斷回測）

### 台股特定調整

| 參數 | Keep-Buying 預設 | 本專案預設 | 原因 |
|---|---|---|---|
| `sharpe_threshold` | 1.0 | 0.5 | 台股 Sharpe 普遍較低 |
| `drawdown.threshold` | 0.40 | 0.10 | 台股容易急殺，嚴格停損 |
| `market` | us/tw/global | **tw（鎖定）** | 台股專版 |
