# forecast-bot

Metaculus 預測機器人，參加 Fall 2026 FutureEval 正賽＋MiniBench。v0.1.0。

## 給你看的部分

### 它在做什麼
- 每小時醒來一次，到 Metaculus 抓正賽跟 MiniBench 所有「還開著」的題目。
- 已經答過的題跳過，新題逐題上網查資料、讓 AI 想五次取中間值，送出預測，每題附一則說明（comment，領獎條件）。
- 送出前會把太極端的數字收回來（是非題最低 3%、最高 97%；選擇題每個選項至少 1%；數值題單位看起來錯了會重問一次，還錯就交一個寬的分布，不交錯的窄值）。有收回的話，說明裡會寫「模型原值 -> 實際送出值」。
- 花錢有上限：OpenRouter 額度剩不到 $15 自動進省錢模式（每題只想三次、用便宜的查資料模型）；剩不到 $3 就整個停，不再呼叫 AI。
- 沒有任何人工改單題預測的開關（比賽規則：no human in the loop）。

### 怎麼看它有沒有在跑
1. Metaculus：切到 bot 帳號（settings -> My Forecasting Bots -> 切換），看題目上有沒有它的預測。說明 comment 是**私人**留言（官方範本的預設），切到 bot 帳號才看得到。
2. GitHub：repo 的 **Actions** 分頁 -> 「Forecast on tournament questions」-> 點最新一次 -> 頁面下方 Summary 有：版號、開放題數、這次新交幾題、失敗幾題、這次大約花多少、OpenRouter 剩多少。紅框＝停了，裡面寫原因。
3. 額度用完或 key 沒設上限時，那次執行會標失敗，GitHub 會寄信給你。

### 開關（Settings -> Secrets and variables -> Actions -> **Variables** 分頁）
| 變數 | 沒設 | 設成 1 |
|---|---|---|
| `BOT_ENABLED` | 排程完全不跑（不吃分鐘、不寄信） | 每小時跑 |
| `PUBLISH` | 試跑：只列出開著的題目和開放／截止時間，什麼都不送 | 真的送出預測＋說明 |

- 要關掉：把 `BOT_ENABLED` 刪掉或改成 0。立刻生效。
- 只想停止送出、保留每小時盤點：把 `PUBLISH` 刪掉。
- 手動試一次：Actions -> 「Test Bot」-> Run workflow。它從官方測試區（bot-testing-area）挑 3 題；`PUBLISH` 沒設就不送。

### 金鑰（Secrets 分頁，你自己放）
`METACULUS_TOKEN`（bot 帳號的 token）、`OPENROUTER_API_KEY`（**請在 openrouter.ai 幫這把 key 設花費上限**，沒上限它會拒跑），選填 `ASKNEWS_CLIENT_ID`／`ASKNEWS_SECRET`。

### 花費與分鐘（估算，跑一週後看 Summary 校正）
- 每題約 $0.35–0.55（正常模式），省錢模式約 $0.20。實際數字以每次 Summary 的「Estimated spend」為準。
- 每月題量約 240 題（正賽 ~110＋MiniBench ~120）-> 正常模式約 $85–130／月。捐贈額度若只有 ~$100，大約一個月後會自動進省錢模式。
- GitHub Actions 分鐘：私人 repo 全帳號每月共用 2,000 分鐘，每次執行零頭進位成整分鐘。每小時一次＝每月 ~744 次；沒新題的執行約 40–55 秒（算 1 分鐘），有新題每題約多 1 分鐘。估計 **~1,000 分鐘／月**（悲觀情況：沒新題也超過 60 秒 -> ~1,750）。帳號本月已用分鐘要 `user` 權限才查得到，目前查不到；替代查法：2026-10-16 時帳號所有私人 repo 十月的 Actions 執行次數都是 0。
- 要降頻：只改 `.github/workflows/tournament.yaml` 裡 `cron:` 那一行（例如 `"17 */2 * * *"`＝每兩小時）。

---

## Technical

Base: [Metaculus/metac-bot-template](https://github.com/Metaculus/metac-bot-template) at
`096cb207d643da3090c4830cd828255bf18da2d5` (2026-10-15, remote `upstream`), forecasting-tools 0.3.4
(`>=0.3.0,<0.4.0`), Python 3.12 on Actions.

### Files
| file | role |
|---|---|
| `main.py` | `ForecastBot2026` (template flow, rewritten prompts), model selection, CLI |
| `runner.py` | one run: budget check, fetch all pages of open questions, retries, publish, job summary |
| `guards.py` | final-value guardrails (binary clamp, MC floor, unit-error check, CDF checks), pure functions |
| `budget.py` | OpenRouter `GET /api/v1/key` spend guard |
| `bot_helpers.py` | template env check |
| `tests/` | offline tests (sockets blocked, fake LLM, fixture questions) |

Removed from the template: `.claude/` (third-party skill), `main_with_no_framework.py`, `integrations/`,
`review_bot` and `metaculus_cup` workflows.

### Flow per run
1. `GET openrouter.ai/api/v1/models` (public) -> pick `openai/gpt-5.4` (or newest `openai/gpt-5.x`) and
   `openai/gpt-5.4-mini`; register their prices with litellm so per-question cost estimates are real.
2. `GET /api/v1/key` -> state `normal | economy | stop | refuse_unlimited | unknown`
   (`BUDGET_ECONOMY_BELOW`=15, `BUDGET_STOP_BELOW`=3, `ALLOW_UNLIMITED_KEY`). Rechecked before every question.
3. Fetch every open question of `MetaculusClient.CURRENT_AI_COMPETITION_ID` and `CURRENT_MINIBENCH_ID`
   with pagination (the SDK's `get_all_open_questions_from_tournament` only reads the first 100).
   Group questions are unpacked. Already-forecasted questions are skipped. Closing-soonest first.
4. Per question (3 in parallel): research (gpt-5.4 + OpenRouter web plugin pinned to `engine: native`,
   plus AskNews if keys exist) -> 5 forecasts (3 in economy) -> SDK median/mean aggregate -> `guards.py`.
   Up to 2 retries per question; a question that still fails is listed and the run continues.
5. Publish (only `PUBLISH=1`): `report.publish_report_to_metaculus()` = `POST /questions/forecast/` then
   `POST /comments/create/` with the full explanation (`is_private=True`, `included_forecast=True`, SDK defaults).
   The bot itself runs with `publish_reports_to_metaculus=False`; `runner.py` publishes with 3 attempts so a
   failed comment is retried without re-running the LLMs.
6. Job summary to `$GITHUB_STEP_SUMMARY`. Exit 1 on budget stop, fetch failure, or when every attempted
   question failed.

Per-run forecast cap (`MAX_FORECASTS_PER_RUN`): none when publishing; **0 in scheduled dry runs** (a dry run
never marks questions as forecasted, so it would re-forecast the same questions every hour); 3 in
`test_questions` mode.

### Prompts
Today's date, close date and scheduled resolution date are injected, with an explicit "this question has NOT
resolved yet". Order: time left -> outside view (reference class, base rate, status quo) -> inside view ->
scenarios -> answer. No "Bayesian", no personas, no programmatic extremizing.

### Comment
Starts with `# forecast-bot v0.1.0` and a "Final adjustments" block listing every guardrail change as
`model value -> submitted value`, followed by the SDK's standard summary / research / per-forecaster rationale.

### Cost model (assumptions, USD, prices from OpenRouter 2026-10-16)
gpt-5.4 $2.50 in / $15 out per M tokens; gpt-5.4-mini $0.75 / $4.50; web search ~$0.01 per request.
Research ~10k in / 3k out = ~$0.08–0.10; 5 forecasts x (4k in / 2.5–5k out incl. reasoning) = $0.24–0.43;
parsing 10 mini calls ~$0.03; summary ~$0.01. **~$0.36–0.57 per question.** Economy ~$0.20.

### Tests
```
poetry install --with dev && poetry run pytest
```
All offline. Covers clamp, MC normalization, 1000x unit errors, budget states, PUBLISH default off,
comment sent with the submitted value, BOT_ENABLED job-level `if:`, full fixture round with a mock LLM.
