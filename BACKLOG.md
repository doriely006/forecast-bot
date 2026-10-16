# BACKLOG

## 收工日誌

### 2026-10-16 v0.1.0 初版
- 底：Metaculus/metac-bot-template `096cb207`（remote `upstream`）。forecasting-tools 0.3.4，Actions 用 Python 3.12＋整個 .venv 快取（lock 沒變就跳過 poetry）。
- 新檔：guards.py（夾限／MC floor／單位錯／CDF 檢查）、budget.py（OpenRouter /key 三態＋無上限拒跑）、runner.py（分頁抓題、跳已答、每題重試 2、發佈重試 3、job summary）、tests/。
- 移除：`.claude/skills/review-bot/SKILL.md`、main_with_no_framework.py、integrations/、review_bot／metaculus_cup workflow。
- 測試：`poetry run pytest` -> 54 passed / 0 failed（本機 Python 3.12.15，全離線、socket 封鎖）。
- SDK 實讀結論：comment＝`publish_report_to_metaculus` 先 POST forecast 再 POST `/comments/create/`（`is_private=True`）；explanation 必須以 `#` 開頭；`get_all_open_questions_from_tournament` 只抓第一頁 100 題 -> 改自己分頁；PredictedOptionList 解析時已把選項夾到 [1%,99%] 並正規化。
- 排程 `17 * * * *` 每小時；估 ~1,000 分鐘／月（悲觀 ~1,750）。BOT_ENABLED／PUBLISH 都未設。
- 未驗：沒有 token，真 API 一次都沒打（題目抓取、`plugins: web engine native` 在 litellm->OpenRouter 的實際行為、/key 回應欄位都只照文件與 SDK 寫）。

### 下一輪開工先做
- user 放好 secrets 後：手動跑 Test Bot（PUBLISH 未設）-> 看 Summary 的花費、log 有沒有 web search 引用來源；再設 BOT_ENABLED=1 跑一天 dry run，量開放窗（Summary 表格的 open/close）。開放窗 < 2 小時要回報。
- 跑一週後對 Actions 實際計費分鐘：月推估 > 1,500 就改 cron 那一行成每兩小時。
- 額度對帳：每題 ~$0.4 × 每月 ~240 題 ≈ $100／月，跟捐贈額度比，決定是否預設 3 次預測。

## v0.2 清單
- standings 小工具：讀正賽／MiniBench 名次與獎金欄。第一輪 MiniBench 結算前要有。停損判準＝MiniBench 連兩輪後半段就停。
- agentic 迭代搜尋（多輪查、查到缺口再查）。
- 已結案相似題＋預測市場（Polymarket／Kalshi／Metaculus 已結案題）查詢，當 base rate 來源。
- 跨家族 ensemble（GPT＋Claude），先小樣本比對再上。
- Platt 校準（用已結算題擬合；binary 先做）。
- 回測：避開 model knowledge cutoff 洩漏（只用 cutoff 之後開的題）。
