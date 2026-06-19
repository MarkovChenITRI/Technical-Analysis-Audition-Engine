# 策略邏輯與決策文件

> **適用版本**：Technical Analysis Audition Engine（台股專版）  
> **市場**：台股（TWSE / TPEX），`market: tw`，不可更改

---

## 一、策略哲學

本策略圍繞一個核心問題：**如何在台股中找到「體質好、尚未爆漲、有資金進場跡象」的標的？**

### 三層過濾邏輯

```
第一層：相對動量（Relative Momentum）
  → Sharpe 排名確認標的具備長期風險調整後的相對強度（體質篩選）

第二層：資金訊號
  → 融資淨增確認近期散戶槓桿持續流入（動能確認）

第三層：蓄積過濾
  → no_parabolic 排除已爆漲標的，只買仍在橫盤蓄積區間的股票（位置控制）
```

### 完整閉環架構

```
市場層（絕對動量）
  └── TWII > 200MA？
        否 → 停止一切新買進（只執行賣出）
        是 ↓

標的層（相對動量）
  └── Sharpe 排名前 N + 融資淨增 + 無爆漲？
        否 → 不納入候選
        是 ↓

資金部署層
  └── delayed 閘門：市場品質夠好才進場
        否 → 等下週
        是 ↓

持倉管理層
  └── 每週檢查賣出條件
        Sharpe 退步 2 週 → 主動出場
        融資崩潰 → 主動出場
        回撤 10% → 硬停損
        已停損標的未站回觸發價 → 禁止重新買進
```

### 核心哲學

> **保護資本的優先級高於抓住機會。**

在台股這種波動集中、容易急殺的市場（2015 / 2020 / 2022 各一次 -30%），避開一次系統性下跌，遠比抓住十次 +10% 波段更重要。這是雙動量框架（Antonacci 2014、Faber 2007）的核心結論。

---

## 二、市場層：絕對動量過濾（TWII 200MA）

> **實證依據**：Meb Faber（2007）對美股 1900–2007 的研究，加入 10 月均線後最大回撤從 -83% 降至 -29%，年化報酬幾乎不變。台股 2015 / 2020 / 2022 三次 -30% 修正均在 TWII 跌破 200MA 後發生。

```yaml
market_filter:
  enabled: true
  indicator: twii_200ma   # TWII 收盤 vs. 200 日移動平均線
```

**規則**：
- TWII 收盤 > 200 日 MA → 市場處於多頭，允許開新倉
- TWII 收盤 ≤ 200 日 MA → 市場處於空頭，**停止所有新買進，只執行賣出**

**重要語意**：此過濾作用於「是否開新倉」，不影響既有持倉的賣出判斷。持倉仍依 `sharpe_fail`、`margin_collapse`、`drawdown` 正常出場。

**資料來源**：`^TWII` 歷史資料已在 `fetch_benchmark_prices` 中抓取，無需新 API。

---

## 三、買進條件

### 3.1 核心篩選層（建議維持啟用）

#### `sharpe_rank`（Sharpe 排名篩選）

```yaml
sharpe_rank: { enabled: true, top_n: 15 }
```

**邏輯**：只考慮 Sharpe 排名在前 `top_n` 名的標的。  
**計算基礎**：252 交易日滾動窗口（`SHARPE_WINDOW = 252`），年化超額報酬 / 標準差。  
**語意**：長期相對強度過濾，排除結構性弱勢股。

| 參數 | 預設 | 調整建議 |
|---|---|---|
| `top_n` | 15 | 牛市可放寬至 20；熊市收緊至 10 |

**注意**：`top_n` 是候選池入場門檻，不是持倉上限；最終持倉數由 `max_positions` 控制。

---

#### `sharpe_threshold`（Sharpe 絕對值門檻）

```yaml
sharpe_threshold: { enabled: true, threshold: 0.5 }
```

**邏輯**：在通過排名篩選後，Sharpe 絕對值必須 ≥ `threshold`。  
**語意**：排除「排名靠前但整體 Sharpe 仍為負」的情況——熊市全盤皆輸時前幾名可能也是負值。  
**台股調整**：台股 Sharpe 普遍低於美股，預設 0.5（Keep-Buying 預設 1.0）。

| 參數 | 預設 | 調整建議 |
|---|---|---|
| `threshold` | 0.5 | 牛市可降至 0.3；保守策略可提高至 0.8 |

---

#### `margin_net_long`（融資淨增）

```yaml
margin_net_long: { enabled: true, days: 5, min_avg_change: 0.02 }
```

**資料來源**：FinMind `TaiwanStockMarginPurchaseShortSale`（`MarginPurchaseTodayBalance`）  
**邏輯**：近 `days` 個交易日，融資餘額日均增減率 ≥ `min_avg_change`。  
**語意**：散戶槓桿資金持續流入，作為「有人在積極布局」的動能確認訊號。

> **語意說明**：台股融資是散戶的槓桿工具，機構法人不使用此機制。正確語意是「散戶動能確認」，而非「機構布局訊號」。若要追蹤機構動向，應使用三大法人資料（見 §3.3 選配項）。

**降級策略**：
- FinMind API 失敗 → 條件視為通過（不阻斷買進）
- 上櫃（`.TWO`）股票 → 條件視為通過（TPEX 融資資料另有來源）

| 參數 | 預設 | 調整建議 |
|---|---|---|
| `days` | 5 | 3 日更敏感（短線）；10 日更穩定（中線） |
| `min_avg_change` | 0.02 | 0.01 放寬；0.05 收緊（只取強動能） |

---

#### `no_parabolic`（無爆漲過濾）

```yaml
no_parabolic: { enabled: true, lookback: 30, max_gain: 0.40 }
```

**資料來源**：收盤價（yfinance），不需外部 API  
**邏輯**：近 `lookback` 個交易日累積漲幅 < `max_gain`。  
**語意**：排除已在短期急速拉升的標的，優先選擇仍在橫盤蓄積區間的股票。

**與 `sharpe_rank` 的時間維度協調**：
- Sharpe 窗口 252 日（長期），`no_parabolic` 窗口 30 日（中期）
- Sharpe 確認「長期體質好」；`no_parabolic` 確認「近期入場位置尚未追高」
- `max_gain: 0.40` 對應台股漲停 10% × 4 天以上的爆衝，保留正常強勢股（30 日漲 20–30% 仍可通過）

| 參數 | 預設 | 調整建議 |
|---|---|---|
| `lookback` | 30 | 10–20 日適合短線；30 日適合中線位置控制 |
| `max_gain` | 0.40 | 0.30 更嚴格；0.50 僅排除極端爆漲 |

---

#### `growth_streak`（Sharpe 排名成長連續性）

```yaml
growth_streak: { enabled: true, days: 2, percentile: 50 }
```

**邏輯**：近 `days` 個交易日，Sharpe 排名改善幅度持續位於全體前 `percentile`%。  
**語意**：確認標的正在「持續進步」而非只是某天排名剛好靠前。

| 參數 | 預設 | 調整建議 |
|---|---|---|
| `days` | 2 | 增加至 3 可要求更持久的排名上升 |
| `percentile` | 50 | 數值越低 = 越嚴格（只允許排名改善最快的前 N%） |

---

### 3.2 排序策略（三選一）

篩選後的候選清單透過以下之一排序，決定優先買進順序：

#### `sort_sharpe`（依 Sharpe 排序）★ 預設

```yaml
sort_sharpe: { enabled: true }
```

**語意**：Sharpe 最高者優先。適合穩健型策略，確保資本優先流向最強標的。

---

#### `sort_margin`（依融資布局強度排序）

```yaml
sort_margin: { enabled: false, days: 5 }
```

**語意**：Sharpe 篩選後，依近 `days` 日融資平均增幅降序排列。  
**適用**：成長型 / 動能型策略，相信資金流入方向預示短期表現。  
**注意**：與 `sort_sharpe` 互斥，同時啟用時 `sort_margin` 優先。

---

#### `sort_industry`（產業分散排序）

```yaml
sort_industry: { enabled: false, per_industry: 2 }
```

**語意**：每個產業最多選 `per_industry` 檔，依輪選方式填滿持倉。  
**適用**：分散風險、避免單一產業集中。會犧牲部分績效最佳化，換取相關性降低。

---

### 3.3 選配：三大法人訊號（建議追加，v2）

```yaml
institutional_net_buy: { enabled: false, days: 5, min_net_buy_ratio: 0.003 }
```

**資料來源**：FinMind `TaiwanStockInstitutionalInvestorsBuySell`（外資 + 投信淨買超）  
**語意**：這才是真正的「機構布局訊號」，與 `margin_net_long` 形成互補：
- `margin_net_long` = 散戶動能
- `institutional_net_buy` = 機構動能

兩者同時為正代表「散戶跟機構同向布局」，是最強的入場訊號。

> **實作狀態**：v1 尚未實作，架構設計相容（與 `TWMarginData` 同模式）。

---

### 3.4 選配：偶發性強化條件

#### `sharpe_streak`（Sharpe 排名連續在榜）

```yaml
sharpe_streak: { enabled: false, days: 3, top_n: 10 }
```

連續 `days` 天都在 Sharpe 前 `top_n` 名。比 `sharpe_rank` 更嚴格，要求長期穩定在榜。

---

#### `growth_rank`（依成長排名篩選）

```yaml
growth_rank: { enabled: false, top_n: 7 }
```

Sharpe 排名改善幅度位於前 `top_n` 名。追求「最快進步的標的」，適合動能反轉型策略。

---

## 四、賣出條件

### 4.1 主動出場（訊號賣出）

#### `sharpe_fail`（Sharpe 退步出場）

```yaml
sharpe_fail: { enabled: true, periods: 2, top_n: 15 }
```

**邏輯**：持倉股連續 `periods` 個再平衡週期不在 Sharpe 前 `top_n` 名，強制賣出。  
**閉環對應**：對應買進條件 `sharpe_rank`，體質不再符合當初買進前提時出場。

| 參數 | 預設 | 調整建議 |
|---|---|---|
| `periods` | 2 | 1 = 更激進；3 = 更耐心（容忍短暫波動） |
| `top_n` | 15 | 建議與買進 `sharpe_rank.top_n` 一致 |

---

#### `margin_collapse`（融資崩潰出場）

```yaml
margin_collapse: { enabled: true, days: 3, threshold: -0.15 }
```

**資料來源**：FinMind `TaiwanStockMarginPurchaseShortSale`  
**邏輯**：近 `days` 日融資餘額日均增減率 ≤ `threshold`（負值代表快速縮減）。  
**語意**：資金快速撤退，市場在強制去槓桿，強制出場保護資本。  
**閉環對應**：對應買進條件 `margin_net_long`，融資增加時買進 → 融資崩潰時賣出。

**降級策略**：API 失敗 → 條件不觸發（不強制賣出）。

| 參數 | 預設 | 調整建議 |
|---|---|---|
| `days` | 3 | 5 日可避免雜訊誤觸 |
| `threshold` | -0.15 | -0.10 更敏感；-0.30 只在崩潰時才觸發 |

---

### 4.2 被動止損

#### `drawdown`（最大回撤停損）

```yaml
drawdown: { enabled: true, threshold: 0.10, from_highest: false }
```

**邏輯**：
- `from_highest: false`（預設）：從買進價格下跌 ≥ `threshold` 即觸發
- `from_highest: true`：從持倉期間最高點下跌 ≥ `threshold` 即觸發（移動停利停損）

**台股理由**：台股波動集中，10% 是紀律性停損的合理位置。

| 參數 | 預設 | 調整建議 |
|---|---|---|
| `threshold` | 0.10 | 台股建議維持 0.08–0.12；超過 0.15 風險大增 |
| `from_highest` | false | 改為 true 可實現移動停損，鎖定已實現的獲利 |

---

### 4.3 停損後重新入場：價格恢復條件

> **實證依據**：Stan Weinstein（1988）的第二段買點概念——等待新的起漲點，而非在下跌中攤平。純時間冷卻（等 N 週）是任意的，價格恢復才有經濟學依據。

**規則**：被 `drawdown` 停損出場的股票，必須收盤站回停損觸發價格以上，方可再次納入買進候選。

```yaml
stop_loss_reentry:
  enabled: true
  type: price_recovery    # 價格站回停損觸發價才解除限制
```

**語意**：停損代表「此標的在持有期間表現不如預期」。站回停損價意味著市場已消化那波賣壓，回到原評估的價值區間。在此之前，它只是一支正在下跌的股票，不應重複買進（避免連續停損陷阱）。

---

### 4.4 選配賣出條件

#### `weakness`（綜合弱勢出場）

```yaml
weakness: { enabled: false, rank_k: 20, periods: 3 }
```

連續 `periods` 個週期，Sharpe 排名和 Growth 排名都在第 `rank_k` 名以後。  
要求兩個指標同時弱才出場，比 `sharpe_fail` 更保守。

---

#### `growth_fail`（成長趨勢惡化出場）

```yaml
growth_fail: { enabled: false, days: 5, threshold: 0 }
```

近 `days` 日排名改善均值 < `threshold`。  
見退步跡象即換股，比 `sharpe_fail` 更積極。

---

#### `not_selected`（持續未被選中出場）

```yaml
not_selected: { enabled: false, periods: 3 }
```

持倉股連續 `periods` 個週期不在買進候選清單中，出場。搭配 `sort_industry` 使用時有助產業輪動。

---

## 五、再平衡策略

再平衡策略決定「何時以何種力道部署資本」。

### `delayed`（延遲閘門策略）★ 目前預設

```yaml
rebalance_strategy:
  type: delayed
  top_n: 10
  sharpe_threshold: 0.3
```

**邏輯**：計算 Sharpe 排名前 `top_n` 名的 Sharpe 均值，若 ≤ `sharpe_threshold` 則當週不買任何新標的。  
**語意**：市場整體品質不佳時，暫停新進場。`top_n` 與 `max_positions` 對齊，確保閘門的代表性樣本等於目標持倉規模。

| 參數 | 預設 | 調整建議 |
|---|---|---|
| `top_n` | 10 | 必須等於 `max_positions` |
| `sharpe_threshold` | 0.3 | 0 = 幾乎不擋；0.5 = 中等嚴格；1.0 = 只在強牛市進場 |

---

### `immediate`（立即買進）

```yaml
rebalance_strategy: { type: immediate }
```

每次再平衡日，所有通過條件的新候選立即全額買進。適合驗證訊號本身有效性的回測。

---

### `batch`（批次分批買進）

```yaml
rebalance_strategy: { type: batch, batch_ratio: 0.20 }
```

每次再平衡日用 `cash × batch_ratio` 的資金平均分配給新候選。分批入場，降低單點高買風險。

---

### `concentrated`（集中強勢策略）

```yaml
rebalance_strategy:
  type: concentrated
  concentrate_top_k: 3
  lead_margin: 0.30
```

只在前 `top_k` 名的 Sharpe 均值領先後 `top_k` 名 ≥ `lead_margin` 時才進場，且只買前 `top_k` 名。適合趨勢強烈的牛市波段。

---

### `none`（不自動再平衡）

只執行賣出邏輯，不主動買進。適合手動控制或純測試賣出條件。

---

## 六、倉位管理

### 等額投資（預設）

```yaml
position_sizing:
  type: equal_weight
  amount_per_stock: 100000
```

每檔股票固定投入 `amount_per_stock`，邏輯簡單、回測可重現。

---

### Sharpe 比例加權（選配，v2）

> **理論依據**：Kelly Criterion 的簡化版——以訊號強度決定投注比例。Sharpe 越高代表 edge 越大，應分配更多資本。

```yaml
position_sizing:
  type: sharpe_weighted
  base_amount: 100000
  max_multiplier: 1.5    # Sharpe 最高者最多 1.5 倍
  min_multiplier: 0.6    # Sharpe 剛過門檻者最少 0.6 倍
```

**語意**：在 `max_positions` 範圍內，Sharpe 最高的標的獲得更多資本，Sharpe 剛好過門檻的獲得較少。

> **實作狀態**：v1 尚未實作。

---

## 七、費用模型

```yaml
fees:
  tw:
    buy_rate: 0.0015     # 買進手續費 0.1425%（取整）
    sell_rate: 0.0045    # 賣出手續費 0.1425% + 證交稅 0.3%
    min_fee: 20          # 台股最低手續費 NT$20
```

| 操作 | 費率 | 說明 |
|---|---|---|
| 買進 | 0.15% | 券商手續費，可議價（折扣後約 0.07–0.1%） |
| 賣出 | 0.45% | 手續費 0.15% + 證交稅 0.3%（固定，無法議價） |
| 來回合計 | ~0.60% | 週度換倉每年約 30 次 × 0.6% ≈ 18% 隱性成本 |

> 高換手率是策略的主要隱性成本。`delayed` 閘門降低換手率的效果直接反映在淨績效上。

---

## 八、推薦配置組合

### 保守型（穩健收益，控制回撤）

```yaml
backtest:
  amount_per_stock: 80000
  max_positions: 10
  rebalance_freq: weekly
  market_filter:    { enabled: true, indicator: twii_200ma }
  buy_conditions:
    sharpe_rank:      { enabled: true, top_n: 12 }
    sharpe_threshold: { enabled: true, threshold: 0.8 }
    margin_net_long:  { enabled: true, days: 5, min_avg_change: 0.03 }
    no_parabolic:     { enabled: true, lookback: 30, max_gain: 0.30 }
    growth_streak:    { enabled: true, days: 2, percentile: 50 }
    sort_sharpe:      { enabled: true }
  sell_conditions:
    sharpe_fail:     { enabled: true, periods: 2, top_n: 12 }
    drawdown:        { enabled: true, threshold: 0.08 }
    margin_collapse: { enabled: true, days: 3, threshold: -0.15 }
  stop_loss_reentry:  { enabled: true, type: price_recovery }
  rebalance_strategy: { type: delayed, top_n: 10, sharpe_threshold: 0.5 }
```

---

### 標準型（平衡績效與風控）★ 建議起點

```yaml
backtest:
  amount_per_stock: 100000
  max_positions: 10
  rebalance_freq: weekly
  market_filter:    { enabled: true, indicator: twii_200ma }
  buy_conditions:
    sharpe_rank:      { enabled: true, top_n: 15 }
    sharpe_threshold: { enabled: true, threshold: 0.5 }
    margin_net_long:  { enabled: true, days: 5, min_avg_change: 0.02 }
    no_parabolic:     { enabled: true, lookback: 30, max_gain: 0.40 }
    growth_streak:    { enabled: true, days: 2, percentile: 50 }
    sort_sharpe:      { enabled: true }
  sell_conditions:
    sharpe_fail:     { enabled: true, periods: 2, top_n: 15 }
    drawdown:        { enabled: true, threshold: 0.10 }
    margin_collapse: { enabled: true, days: 3, threshold: -0.15 }
  stop_loss_reentry:  { enabled: true, type: price_recovery }
  rebalance_strategy: { type: delayed, top_n: 10, sharpe_threshold: 0.3 }
```

---

### 成長型（追求報酬，接受較高波動）

```yaml
backtest:
  amount_per_stock: 100000
  max_positions: 8
  rebalance_freq: weekly
  market_filter:    { enabled: true, indicator: twii_200ma }
  buy_conditions:
    sharpe_rank:      { enabled: true, top_n: 10 }
    sharpe_threshold: { enabled: true, threshold: 0.5 }
    margin_net_long:  { enabled: true, days: 3, min_avg_change: 0.03 }
    no_parabolic:     { enabled: true, lookback: 20, max_gain: 0.50 }
    growth_streak:    { enabled: true, days: 2, percentile: 60 }
    sort_margin:      { enabled: true, days: 5 }
  sell_conditions:
    sharpe_fail:     { enabled: true, periods: 1, top_n: 10 }
    drawdown:        { enabled: true, threshold: 0.10 }
    margin_collapse: { enabled: true, days: 3, threshold: -0.10 }
  stop_loss_reentry:  { enabled: true, type: price_recovery }
  rebalance_strategy: { type: immediate }
```

---

## 九、閉環完整性檢查

| 買進條件 | 對應賣出條件 | 狀態 |
|---|---|---|
| `sharpe_rank` / `sharpe_threshold`（體質篩選） | `sharpe_fail`（體質退步出場） | ✓ 閉合 |
| `margin_net_long`（散戶動能確認） | `margin_collapse`（資金撤退） | ✓ 閉合 |
| `no_parabolic`（橫盤蓄積位置過濾） | `drawdown: 10%`（硬停損兜底） | ✓ 兜底 |
| 任何條件 | `stop_loss_reentry`（停損後禁止重新買進） | ✓ 閉合 |
| 市場空頭 | `market_filter`（TWII 200MA 停止新買進） | ✓ 閉合 |
| `delayed` 閘門 | — | ✓ 入場紀律 |

---

## 十、資料降級行為彙整

| 情境 | 降級行為 | 偏差方向 |
|---|---|---|
| FinMind API 失敗 | `margin_net_long` 視為通過 | 偏多（允許買進） |
| FinMind API 失敗 | `margin_collapse` 不觸發 | 偏多（不強制賣出） |
| 上櫃（`.TWO`）股票 | 兩個融資條件均降級通過 | 偏多 |
| 股價資料不足 | `no_parabolic` 視為通過 | 偏多 |
| TWII 資料失敗 | `market_filter` 視為多頭（允許買進） | 偏多 |
| Benchmark 抓取失敗 | 只顯示策略曲線，無比較基準 | 無偏差，僅顯示影響 |

> 所有降級均偏向「允許買進 / 不強制賣出」。在網路不穩定或 API 異常期間，系統傾向維持現有部位而非保守現金，需注意此系統性偏差。

---

## 十一、功能路線圖

| 版本 | 功能 | 狀態 |
|---|---|---|
| v1 | 核心策略（Sharpe + 融資 + no_parabolic） | ✓ 已實作 |
| v1 | TWII 200MA 市場過濾 | 待實作 |
| v1 | 停損後價格恢復條件 | 待實作 |
| v1 | 費率拆分（buy_rate / sell_rate） | 待實作 |
| v2 | 三大法人訊號（`institutional_net_buy`） | 規劃中 |
| v2 | Sharpe 比例加權倉位管理 | 規劃中 |
