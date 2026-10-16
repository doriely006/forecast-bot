# forecast-bot v0.1 規格（2026-10-16 主 session）

> 副本。本機絕對路徑已遮。2026-10-16 追加裁定：repo 維持私人、不公開；Actions 月分鐘以私人 repo 2,000 分鐘計，估算超過 1,500 分鐘就降頻。

業主＝user（Metaculus 帳號、獎金、金鑰都是她的）。目標：Fall 2026 FutureEval 正賽＋MiniBench 全自動參賽。
v0.1 定位＝「不犯錯的範本」：題題都交、不給極端值、數值題不出單位錯、花費有上限。研究深度與 ensemble 調校留 v0.2。
依據：同目錄 metaculus-research.md D 節。重點：陽春範本（GPT-5.1＋AskNews、單 prompt）春季第 18／173；後段輸在漏題、把未結案當已結案、單位 bug；贏家間最強區分＝cap 上下限；GPT-5.4 當最終模型是最強標記；prompt 寫 "Bayesian"、multi-persona、程式 extremize 都變差；bot 作者共識「simplicity beats complexity」。

## 硬規則（違反＝退件）
1. 金鑰只從環境變數讀：METACULUS_TOKEN、OPENROUTER_API_KEY（可選 ASKNEWS_CLIENT_ID／ASKNEWS_SECRET）。repo／log／comment／fixture 不得出現金鑰。不准索取金鑰、不准跑 `gh secret set`。
2. 兩個 repo variable 當總開關，交付時都不設，你也不准設：
   - BOT_ENABLED：未設＝排程 job 整個 skip（job-level `if:`），不吃分鐘、不寄失敗信。
   - PUBLISH：未設＝dry run（test_questions 模式也不送）。只有 =1 才送出預測與 comment。
3. no human in the loop：不得有任何針對單題手動覆寫預測的機制。
4. 不漏題：每次執行抓正賽＋MiniBench 全部開放題、跳過已預測；單題失敗重試 2 次，仍失敗記錄後繼續下一題。結尾寫 GitHub job summary：版號、開放題數、新交幾題、失敗幾題、本次估計花費、OpenRouter 剩餘額度。
5. 花費護欄（GET https://openrouter.ai/api/v1/key）：
   - key 沒設上限（limit 為 null）→ 拒跑、summary 標紅，除非 ALLOW_UNLIMITED_KEY=1（防 user 自己的卡被刷爆）。
   - 剩餘 < $15 → 省錢模式（較少次預測、較便宜研究）；< $3 → 不呼叫 LLM、summary 標紅。門檻用環境變數可調。
6. comment 是領獎條件：先讀 forecasting-tools 原始碼確認 publish 時 comment 怎麼送，寫 mock 測試驗證有送。comment 必須如實反映最終數字：預測被夾限時寫明「模型原值 → 最終值（夾限）」。
7. 夾限與檢查：
   - binary 夾 [0.03, 0.97]（環境變數可調）。
   - multiple choice 每項 ≥0.01 後重新正規化。
   - numeric／discrete：CDF 單調、遵守開閉邊界、有最小寬度；中位數偏離題目範圍超過一個數量級＝判單位錯，重問一次，再錯退回寬分布（不准交窄的錯值）。
8. prompt：注入今天日期與題目結算日，明講「此題尚未結算」；先 outside view（base rate、現狀延續機率）再 inside view。不寫 "Bayesian"、不用 multi-persona、不程式 extremize。
9. model：先打 https://openrouter.ai/api/v1/models（公開）確認 ID 與單價。主力 openai/gpt-5.4（不存在取最新 GPT-5.x）。研究用 ":online" 原生搜尋（只用 OpenAI／Anthropic 原生，不走 Exa，額度不含 Exa）；有 AskNews key 就加 AskNews。不用 Google 當主力（跨參賽者共享 150 req/min，常 429）。正常模式目標每題 ≤ $0.60，README 寫實際估算。
10. 排程：預設每小時一次＋poetry／venv 快取。dry run 時把開放題的 open_time／close_time 印進 summary（量開放窗）。README 寫月分鐘估算（私人 repo 全帳號共用 2,000 分鐘、每 job 零頭進位）；試 `gh api` 查帳號本月已用分鐘，查不到就寫查不到。開放窗 < 2 小時或估算 > 1,500 分鐘 → 回報主 session，不准自己改 repo visibility。
11. 範本的 `.claude/` 整個不搬（第三方 skill＝指令注入面），回報列出原本有哪些檔。workflow 只留 test_bot（手動）與 tournament 排程；metaculus_cup（bot 不領獎）、review_bot 移除。

## Repo 與環境
- 新建 private repo doriely006/forecast-bot，本機路徑（副本已遮）。底＝Metaculus/metac-bot-template 最新 commit（記 SHA、加 upstream remote；不能 fork，public 的 fork 不能轉私有）。參考 clone：同目錄 tmpl/repo。
- forecasting-tools 升到 ≥0.3.0（公告要求）。Python ≥3.11；本機只有 3.9.6：`python3 -m pip install --user uv` 後 `uv python install 3.12`，不要 brew；被擋就只靠 Actions 跑測試並註明。
- 版號 v0.1.0（pyproject＋每次 summary 印出）。

## 測試（全離線、假 LLM、不打真 API）
- 單元：夾限、MC 正規化、數值單位錯 1000 倍要抓到、預算護欄三態、PUBLISH 預設關、comment 有送（mock client）、BOT_ENABLED 未設時 job skip（解析 workflow YAML 斷言 `if:`）。
- 整合：mock LLM＋本地 fixture 題目跑完整一輪，斷言每題有預測、有 comment、無例外。
- 一行指令跑全部，PASS/FAIL 數寫進 BACKLOG。

## 交付
- README：上半給 user（功能語言：它在做什麼、怎麼看它有沒有在跑、怎麼關）；下半技術。
- BACKLOG.md：收工日誌 ≤40 行＋v0.2 清單：standings 小工具（讀正賽／MiniBench 名次與獎金欄；第一輪 MiniBench 結算前要有；停損判準＝MiniBench 連兩輪後半段就停）、agentic 迭代搜尋、已結案相似題與預測市場查詢、跨家族 ensemble、Platt 校準、回測（避開 model cutoff 洩漏）。
- docs/SPEC.md＝本檔副本。
- push main。回報 ≤30 行：測試 PASS/FAIL 數＋log 路徑、model ID 與單價、每題估算成本、排程與月分鐘估算、沒做到的事、用了哪些預設。
