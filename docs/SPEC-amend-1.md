# forecast-bot 規格修正一（2026-10-16，opus 對抗審查後主 session 蓋章）
審查全文見本 session；以下取代原規格的衝突條文。附加進 repo 的 docs/SPEC.md 當「§12 審查修正」。

## 開賽前必修
A1（原§2/§10）dry run 預設＝只列題：抓開放題、印 id／題型／open_time／close_time，不呼叫 LLM。另設 DRY_RUN_LLM_MAX_QUESTIONS（預設 2）才跑 LLM 路徑。理由：框架的「跳過已預測」只看 Metaculus 端 already_forecasted（forecast_bot.py L231-233），dry run 不送出＝每小時重跑全部題、幾小時燒光 $100。
A2（原§5/§9）預算配速取代「每題 ≤$0.60」：每題預算＝(剩餘額度−保留額)÷估計剩餘題數，起手額度約 $100 時約 $0.15／題。預算低時降到最便宜可用設定（單次研究＋單次預測），不因預算跳題；只有剩餘 < $3 才停。summary 印每題預算與預估見底日期。
A3（原§6）comment 失敗：框架先送預測再送 comment（binary_report.py L66-71），comment 失敗就永遠沒 comment。要：失敗計數、summary 標紅；下一輪找出「已預測但沒有本 bot comment」的題補送；測試加 comment 拋例外的情境。
A4（原§6/§7）夾限只准做在一個地方：覆寫的 _aggregate_predictions（或建 report 之前的唯一出口）。測試斷言 comment 的 Final Prediction＝實際送出值。
A5（原§10）cron 不放整點（例如 "17 * * * *"）。BOT_ENABLED 開啟後頭 48 小時只跑只列題模式量開放窗，量完回報主 session 再定頻率。

## v0.1 應修
B1 raise_errors=False；summary 寫在 finally 或 `if: always()` 步驟；只有系統性故障（全題失敗、401、402）才讓 job 變紅。
B2 同一題跨輪最多重試 3 輪（失敗次數用 actions/cache 之類持久化），超過就跳過並記錄。不送 0.5 或寬分布當退回值：peer score 下對手比亂猜準，垃圾預測的期望值比不交還差。
B3 OpenRouter 回 402：整輪立刻停，不進單題重試，summary 標紅。
B4 搜尋改用 plugins [{"id":"web","engine":"native"}]，不用 ":online"（不支援原生搜尋的模型會默默改走 Exa）。
B5 開關值只認 "1"；其他非空值讓 job 失敗並寫原因。BOT_ENABLED 只管排程；PUBLISH 管所有觸發方式的送出（手動執行沒 PUBLISH=1 一律 dry run）。summary 第一行固定印「已送出」或「DRY RUN」。
B6 每個 job 加 timeout-minutes: 40；保留範本的 concurrency 區塊。
B7 test_questions 模式加題數上限，預設 3。
B8 數值題 CDF 合法性交給框架（numeric_report.py L530-569），只寫測試驗；單位檢查寫成明確公式、只對正值範圍做比值判斷。
B9 README 上半加「只有你能做的事」清單附日期：參加表單、賽季末 survey、每季重新申請額度。AskNews 預設不用（要全名＋LinkedIn）。
B10 workflow 加 `permissions: contents: read`；第三方 action 鎖 SHA；env 拿掉用不到的 secret（EXA、PERPLEXITY 等；有 EXA key 時 researcher 預設會被換掉）。
B11 model 備援改明列清單（openai/gpt-5.4 優先），排除 -pro、-image、:batch。
B12 正賽與 MiniBench 各自 try，一邊抓題失敗不擋另一邊（原審查列 v0.2，升 v0.1：攸關漏題）。
B13 MC 下限改迭代式，確保每項 ≥0.01 後總和仍為 1。
B14 閒置的排程輪要在 60 秒內結束：快取依賴＋先用輕量檢查有沒有新題再裝重套件。README 月分鐘估算照此重算。

## 留 v0.2（寫進 BACKLOG）
Date／Conditional 題型碰到就記錄跳過；配速策略是否偏重 MiniBench（MiniBench 表現好會自動加碼額度）。
