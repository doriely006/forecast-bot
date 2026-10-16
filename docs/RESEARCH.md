# Metaculus FutureEval Fall 2026 bot research (查證日 2026-10-16)
標記：[V]=本輪讀到的頁面/檔；[記憶]=未驗證。WebFetch 403 的頁：tournament-rules、futureeval/participate（未讀到原文）。

## A. 賽事
- 官方頁 [V] https://www.metaculus.com/tournament/fall-futureeval-2026/ ；slug `fall-futureeval-2026`；數字 ID 查不到（程式用 client.CURRENT_AI_COMPETITION_ID，MiniBench 用 client.CURRENT_MINIBENCH_ID，見範本 main.py）
- 公告 [V] https://forum.effectivealtruism.org/posts/hrJrsgMCwsdBuAqgQ/announcing-fall-2026-futureeval-bot-tournament ；https://www.metaculus.com/notebooks/45615/announcement-of-futureeval-fall-2026/
- 參加說明頁 https://www.metaculus.com/futureeval/participate/ （403，沒讀到）；資源頁 https://www.metaculus.com/notebooks/38928/ai-benchmark-resources/ [V]
- 獎金 [V]：每季 $50k 池，300–400 題。MiniBench 每個兩週賽 $1,000、約 60 題、全自動出題/結算（較吵）。Market Pulse Q3 $7,000（bot 可拿）。Metaculus Cup bot 不領獎。
- 日期 [V]：MiniBench warmup 2026-09-21 00:00 UTC 起（多數題前幾天開，第二週通常無新題）；正賽題 09-28 起開，前 1–2 週慢；加入窗到「題目停開」（約 2027-01-01 前幾週）；晚進場從 0 分、排行榜中段起。Summer 獎金目標 10–11 月發。
- 名次數／分配：查不到（公告與 notebook 都沒寫）。
- 每題開放多久：查不到。
- 計分：公告只說「points」；Metaculus 分析文用 head-to-head spot peer score 比較 Pros vs 最佳 bot 隊、FutureEval skill score 以 GPT-4o 為 0 [V] https://forum.effectivealtruism.org/posts/Spyz3wESZu2eeqhDj/ai-forecasting-in-2026-what-11-analyses-say 。賽內實際用哪種分，查不到。
- Bot 規則 [V，公告]：
  - 領獎要每則預測有 comment，且需如實反映 bot 推理與最終決定。
  - 領獎要填賽季末 bot survey（贏家輸家都要；Summer survey 沒填 → Fall 失格風險）。
  - 商業 bot（營利實體、>=3 創辦人/員工/承包者）不領獎，除非賽季末前全開源。個人/solo 不算商業。
  - 只用 Binary/Numeric/Discrete/MultipleChoice。
  - comment 超過 30 天且 >1000 字會被每日封存，列表只回前 200 字；全文 api/comments/<id>/（8 次/10 秒）。
- 「no human in the loop／一人一隻領獎 bot／要給看 code」：公告沒寫；只在 Spring 2026 搜尋摘要見到（Spring 規則：no human in loop、每則附 comment、每人一隻 prize-eligible bot、分享 code 或描述並讓 Metaculus 檢查）[二手，搜尋摘要] https://www.metaculus.com/aib/2026/spring/ 。Fall 版原文要看 tournament-rules（403）→ 沒驗證。
- 參加表單 [V] https://forms.gle/aQdYMq9Pisrf1v7d8 ：所有人必填第一節，3 題必填（社群人口/動機、專案曝光、LLM 額度申請）；3 題具體內容查不到。範本 README 說送預測前要填。

## B. 報名與經費
- 建 bot 帳號 [V，資源頁]：1) 先用人類帳號 "sign up"（bot 帳號須由人類帳號建）2) 到 settings 3) 點 "My Forecasting Bots" → "Create a Bot" 4) 填必要欄位 5) 複製 API Key = METACULUS_TOKEN。看 bot：settings → "My Bots"/"My Forecasting Bots" → "Switch to bot account"（兩處按鈕名資源頁與 README 略有出入）。
- LLM 經費 [V]：現在走 OpenRouter 發的捐贈額度（不是舊的 Metaculus proxy）。只支援 OpenAI、Anthropic、Google 模型。key 放 OPENROUTER_API_KEY；程式用 forecasting-tools 的 GeneralLlm 或 litellm，model 名如 `openrouter/openai/gpt-4o`（範本註解寫法）。模型名後加 ":online" 啟用供應商原生搜尋；避免會走 Exa 的非原生搜尋（額度不含）。用量查 OpenRouter current-key endpoint 的 limit_remaining。
- 額度政策 [V]：Summer 錢用完所以更挑；部分 bot 先給約 $100 供 MiniBench＋正賽；MiniBench 表現高於平均會自動加碼；開源 bot 約雙倍（可能變）；額度每季重申請；商業 bot 無額度。具體可用模型清單：查不到。Google 模型有跨參賽者共享 150 req/min，常 429 要降量。
- AskNews [V]：免費額度 1k 次/月、整賽 4k 次、5M tokens（最新新聞 1 次、archive 5 次）。步驟：1) my.asknews.app 用與 Metaculus bot 同 email 註冊 2) 進 AskNews Discord 私訊 @freqai，或寄 AskNews 官方聯絡信箱（見 asknews.app；bot 名、註冊 email、全名、LinkedIn、所屬：公司/實驗室/獨立）3) 等啟用 4) 在 API credentials 產生金鑰放 .env（範本 .env.template 用 ASKNEWS_CLIENT_ID／ASKNEWS_SECRET；資源頁寫 ASKNEWS_API_KEY，兩者不一致，以範本為準）。每季要續。一人/隊一帳號。

## C. 範本 repo（clone 於 scratchpad/tmpl/repo，只讀；最新 commit 2026-10-15）
- 結構 [V]：main.py（forecasting-tools 版，建議）、main_with_no_framework.py、bot_helpers.py、integrations/、.claude/skills、pyproject.toml、poetry.lock、.env.template、.github/workflows/{test_bot,run_bot_on_tournament,run_bot_on_metaculus_cup,review_bot}.yaml
- Python ^3.11（workflow 用 3.11）；套件管理 Poetry（pyproject package-mode=false）；forecasting-tools ">=0.2.90,<0.4.0"（公告要求升 v0.3.0；實際鎖版本在 poetry.lock，沒查）；另 asknews ^0.13.11、openai ^2.0.0、numpy ^2.3.0。
- 選賽事：main.py --mode {tournament|metaculus_cup|test_questions}，預設 tournament＝先跑 client.CURRENT_AI_COMPETITION_ID 再跑 client.CURRENT_MINIBENCH_ID；test_questions＝"bot-testing-area"（ID 32977 per 公告）。
- Actions 排程 [V]：run_bot_on_tournament.yaml cron "7,27,47 * * * *"（每 20 分）；metaculus_cup "0 0 */2 * *"（每 2 天）；test_bot 僅 workflow_dispatch；concurrency 不並行；runs-on ubuntu-latest。
- Secrets/env [V]：必要 METACULUS_TOKEN＋至少一個 LLM key（建議 OPENROUTER_API_KEY）；可選 OPENAI_API_KEY、ANTHROPIC_API_KEY、PERPLEXITY_API_KEY、EXA_API_KEY、ASKNEWS_CLIENT_ID、ASKNEWS_SECRET。（Settings → Secrets and variables → Actions → New repository secret）
- comment 怎麼送：框架自動——publish_reports_to_metaculus=True 時把 report explanation 當 comment 送出（main.py 設 extra_metadata_in_explanation=True）。我沒追進 forecasting-tools 原始碼確認 API 細節 → 記憶/推論，未驗證。
- 只預測不送出 [V，README]：main.py 把 FallTemplateBot2026(...) 的 publish_reports_to_metaculus=False（目前由 main 內 publish_to_metaculus = True 變數控制）；no_framework 版設 SUBMIT_PREDICTION=False。注意 test_questions 模式仍會送到 bot-testing-area（除非改 False）。
- 預設參數 [V]：research_reports_per_question=1、predictions_per_research_report=5、skip_previously_forecasted_questions=True。
- 提醒：每 20 分鐘排程 + 每次 poetry install 無快取，私人 repo 分鐘數會吃很快（見 E）。範本把 venv cache 註解掉。

## D. 策略（資料來源：Metaculus 分析文，皆 [V] 讀到摘要，非全文）
- Spring 2026 結果 https://forum.effectivealtruism.org/posts/iJ3qBDwpWCfNy9ZGd/pros-beat-bots-but-the-gap-is-nearly-gone ：Pros 略勝 top-10 bot 隊，差距不顯著（-1.25）。前 10 非 Metaculus bot 的受訪者都用 GPT-5.2+，9 個用 GPT-5.4；最佳單 prompt baseline 掉到第 18 → 複雜 harness 開始領先。
- Spring 2026 survey https://forum.effectivealtruism.org/posts/tJoxFWkRev5wrEBar/what-ai-forecasting-strategy-works-best-spring-2026-survey （n=48，無一項統計顯著，贏家回覆率偏高，當假說看）：重 research 勝重 reasoning；web scraping r=+0.33；查相似題/預測市場 r=+0.34；GPT-5.4 為最終模型 r=+0.42；Claude Opus 最終模型≈0；用程式 extremize r=-0.30（變差）；多模型 ensemble r=-0.10、aggregate r=-0.19（無明顯好處）；Exa 負相關、AskNews 無關；開發時數 r=+0.32。
- 2026 彙整 https://forum.effectivealtruism.org/posts/Spyz3wESZu2eeqhDj/ai-forecasting-in-2026-what-11-analyses-say ：frontier 模型是最大差異；scaffold 前五名比 baseline 每題高 5–11 peer points；agentic 迭代搜尋勝一次性；Fall 2025 贏家 86% 做 aggregate（建議 3–7 次、跨模型家族）；Platt scaling 改善 Brier 0.016（binary）；cap 預測上下限是贏家內最強區分（r=+0.48）；明算 base rate r=+0.38；查已結算相似題；贏家平均 ~28 LLM calls/題、$1.40/題 vs 後半 $0.50。
- 與 Spring survey 對照：ensemble 的證據前後矛盾（Fall 2025 贏家多做 vs Spring 無益）→ 查不到定論。
- 坑 [V]：bot 把未結束的題當已結算而給近乎確定的預測（要加 guard）；單位 bug 一題丟 80 分；漏題；回測 cutoff 有洩漏；對齊 community prediction 疑似變差；prompt 寫 "Bayesian" 在小樣本較差；multi-persona 沒幫助。
- 開源 bot 清單（資源頁）：Panshul42（Q2 2025 第 1）、nostreambot（Spring 2026 第 10）、joy.void.joy-bot、alekthebearbot 等；https://github.com/No-Stream/metaculus-bot 。
- 其他研究清單：資源頁有 search provider 影響、自動校準、near-term→far-term 預測等報告，我沒逐篇讀。

## E. GitHub Actions
- 免費方案私人 repo：2,000 分鐘/月、500MB artifact、10GB cache，每月初重置 [V] https://docs.github.com/en/billing/concepts/product-billing/github-actions
- 進位 [V]：每個 job 的分鐘與零頭各自無條件進位到整數分鐘 https://docs.github.com/en/billing/reference/actions-minute-multipliers
- Linux 2-core 超量價 $0.006/min（頁面列 per-minute 價；Linux 倍率是否 1x 該頁沒寫，[記憶] 1x）。公開 repo 標準 runner 免費 [V]。
- 估算（我算，非來源）：範本每 20 分一次＝每月約 2160 次 run；若每 run 實際跑 ≥1 分鐘，光進位就 ≥2160 分鐘 > 2000。要壓：降頻、加 venv cache、或用公開 repo（但公開 = 程式碼公開，開源 bot 另有額度加成）。
