# Technical Analysis Audition Engine — 專案脈絡（CLAUDE.md）

> 本檔僅承載本專案特有的技術脈絡與決策。跨專案通用的思維框架（Clean Architecture、YAGNI、反硬編碼、手術修改）已封裝於 `.claude/skills/connect-ai-style/SKILL.md`，此處不重述。

---

## 一、Domain 定位

本專案是 **Keep-Buying-Audition-Engine 的台股專版**，策略核心保留 Sharpe 排名排序，並疊加台股特有的**融資融券**與**大戶持股**訊號，專門篩選處於橫盤蓄積、尚未爆漲的優質台股。

### 與 Keep-Buying-Audition-Engine 的核心差異

| 維度 | Keep-Buying | Technical Analysis（本專案） |
|---|---|---|
| 市場 | US / TW / Global | **僅台股（market: tw，不可改）** |
| 主要排序 | Sharpe Rank | Sharpe Rank（相同） |
| 新增買進條件 | — | `margin_net_long`（融資淨增）、`no_parabolic`（無爆漲） |
| 新增賣出條件 | — | `margin_collapse`（融資崩潰） |
| 停損幅度 | 40% | **10%**（台股波動較集中） |
| 資料來源 | TradingView + yfinance | TradingView + yfinance + **TWSE 融資融券 API** |
| Benchmark | NASDAQ / TWII / 加權 | **^TWII（台灣加權指數）** |

### 策略流派定位

> **「Sharpe 體質 + 機構買進訊號 + 橫盤蓄積過濾」**

1. 用 Sharpe Rank 確認**長期相對強度**
2. 用**融資淨增**確認機構資金正在布局
3. 用**no_parabolic** 確認股票尚未爆漲、仍在蓄積區間
4. 停損紀律：10% 嚴格停損（台股容易急殺）

---

## 二、工作流邊界

```
Technical_Analysis_AE/
├── cloud_function/          ← 唯一 Domain Core（上傳至 GCF 的完整套件）
│   ├── main.py              ← GCF HTTP entry（hello_http）
│   ├── engine.py            ← 回測引擎 + 新指標 + run_pipeline()
│   ├── data.py              ← 資料層：TradingView / yfinance / FX / TWSE 融資 / LINE
│   ├── config.yaml          ← 憑證 + 策略參數（唯一配置來源）
│   └── requirements.txt
│
├── dashboard.py             ← WebUI Adapter（本地 Flask）
├── webui/                   ← 前端資產
│
├── demo.py                  ← GCF HTTP 測試客戶端
└── docs/
    └── API_SPEC.md
```

### 鐵則（繼承自 Keep-Buying + 本專案追加）

| 規則 | 說明 |
|---|---|
| **market 鎖定台股** | `config.yaml` 的 `market` 固定為 `tw`，WebUI 不提供市場選擇 |
| **GCF 平面化** | `cloud_function/` 不得有子目錄 |
| **唯一 Domain 來源** | `dashboard.py` 透過 `sys.path.insert` import `engine`，禁止複製邏輯 |
| **Domain 純淨** | `engine.py` / `data.py` 不得 import Flask / functions_framework |
| **常數單一來源** | `CONFIG_FILE`、`TPE_TZ` 定義於 `data.py` |
| **TWSE API 優雅降級** | 融資資料抓取失敗時，`margin_net_long` / `margin_collapse` 條件自動跳過（不阻斷回測） |

---

## 三、新增指標說明

### 3.1 `margin_net_long`（融資淨增 — 買進條件）

- **資料來源**：TWSE 每日公布 MI_MARGN（各股融資餘額）
- **計算方式**：近 `days` 個交易日的融資增減率平均 ≥ `min_avg_change`
- **語意**：機構資金持續流入，確認布局訊號
- **降級策略**：若 TWSE API 無法取得資料，條件結果視為 `True`（不阻斷買進）

### 3.2 `no_parabolic`（無爆漲過濾 — 買進條件）

- **資料來源**：收盤價（yfinance）
- **計算方式**：近 `lookback` 日價格漲幅 < `max_gain`
- **語意**：確認標的尚未爆漲，處於蓄積或橫盤區間
- **注意**：`max_gain` 建議設 0.10～0.20（10%～20%）

### 3.3 `margin_collapse`（融資崩潰 — 賣出條件）

- **資料來源**：TWSE 每日 MI_MARGN
- **計算方式**：近 `days` 日融資增減率平均 ≤ `threshold`（負值表示減少）
- **語意**：資金快速撤退，強制出場保護資本
- **降級策略**：TWSE API 失敗時條件不觸發（不強制賣出）

---

## 四、`config.yaml` 職責

```yaml
# ── 憑證區 ─────────────────────────────────────────
tradingview:
  session_id: ""
  watchlist_id: ""       # TW 專用觀察清單
  expires_at: "YYYY-MM-DD"

line:
  channel_access_token: ""
  group_id: ""

# ── 策略區 ─────────────────────────────────────────
backtest:
  initial_capital: 1000000
  amount_per_stock: 100000
  max_positions: 10
  market: tw             # ← 鐵則：只有 tw
  start_date: "2025-01-01"
  rebalance_freq: weekly
  buy_conditions:
    sharpe_rank:     { enabled: true,  top_n: 15 }
    sharpe_threshold:{ enabled: true,  threshold: 0.5 }
    growth_streak:   { enabled: true,  days: 2, percentile: 30 }
    sort_sharpe:     { enabled: true }
    margin_net_long: { enabled: true,  days: 5, min_avg_change: 0.0 }
    no_parabolic:    { enabled: true,  lookback: 10, max_gain: 0.15 }
  sell_conditions:
    sharpe_fail:     { enabled: true,  periods: 2, top_n: 15 }
    drawdown:        { enabled: true,  threshold: 0.10 }
    margin_collapse: { enabled: false, days: 3, threshold: -0.30 }
  rebalance_strategy:
    type: delayed
    top_n: 5
    sharpe_threshold: 0
```

---

## 五、Cloud Function 規格

完全繼承 Keep-Buying-Audition-Engine，除下列差異外完全相同：

| 項目 | 值 |
|---|---|
| `benchmark_name` | 台灣加權指數（^TWII） |
| LINE 訊息 flag | 🇹🇼 台股（無 🇺🇸 旗幟） |
| 市場過濾 | `market: tw` 強制；response 中不含美股資料 |

### TWSE API 相依

- URL: `https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN`
- 觸發時機：`run_pipeline()` 開始時批次預載回測區間所有交易日
- 快取：每日 pickle 快取（同 yfinance 機制）
- GCF 限制：GCF 無持久化快取，每次 trigger 重新抓最近 N 日

---

## 六、關鍵設計決策

### 6.1 為何只支援台股

台股融資融券資料由 TWSE 公開提供，格式穩定；美股 margin data 無對等免費 API。若未來需支援美股，需更換 margin 資料來源。

### 6.2 TWSE API 批次載入策略

回測期間可能跨數百個交易日。`TWMarginData.load_for_dates()` 會：
1. 篩出未快取的日期
2. 每次 API 呼叫間隔 0.3 秒（避免 rate limit）
3. 週末 / 假日 API 回傳 `stat: "No Data"` 時自動略過

### 6.3 TWO（興櫃/上櫃）股票處理

- TWSE MI_MARGN 僅含上市股票（`.TW` 後綴）
- `.TWO`（上櫃）需另呼叫 TPEX API（v1 暫不實作，留 `TODO`）
- 遇 `.TWO` 股票時 margin 條件降級（視為通過）

### 6.4 Sharpe 門檻台股調整

台股 Sharpe 普遍低於美股（市場流動性、震盪幅度不同），建議 `sharpe_threshold` 預設 0.5（Keep-Buying 預設 1.0）。
