# forecast-bot

Metaculus 預測機器人，參加 Fall 2026 FutureEval 正賽＋MiniBench。v0.1.4。

## 只有你能做的事（依序，做一次就好）
金鑰只貼進網頁格子，不貼給任何人（包括 Claude）。標「按鈕名待確認」的，介面可能改版，找最像的那顆。

1. **開 Metaculus 帳號**：到 metaculus.com 按 Sign up，用 email 註冊。不需要放照片或真名。
2. **建 bot 帳號、拿 token**：登入後進 Settings → My Forecasting Bots → Create a Bot（按鈕名待確認）。名字建議 `midnightsharp-bot`。建好後複製它的 API Key（一長串字），先存進密碼管理員。
3. **填參加表單**：https://forms.gle/aQdYMq9Pisrf1v7d8 。問到 open source 選「是」，網址填 https://github.com/doriely006/forecast-bot 。這張表同時是 AI 額度的申請。
4. **等額度信，照信拿 OpenRouter 金鑰**：額度是 Metaculus 送的，照信上的說明拿金鑰。**不要自己儲值、不要綁信用卡。** 如果信要你在自己的 OpenRouter 帳號建 key，建的時候把上限設成送你的金額，並到 Credits 頁確認 auto top-up 是關的（按鈕名待確認）。申請被拒或一直沒來信 → 先停，跟 Claude 說，不要自己付錢。
5. **把兩把金鑰貼進 GitHub**：本 repo 上方 Settings → 左側 Secrets and variables → Actions → Secrets 分頁 → New repository secret。
   - Name 填 `METACULUS_TOKEN`，Secret 貼第 2 步的 API Key，按 Add secret。
   - 再按一次 New repository secret，Name 填 `OPENROUTER_API_KEY`，貼第 4 步的金鑰，按 Add secret。
   - 看到：清單出現這兩個名字，內容看不到（正常）。
6. **試跑一次（不會送出任何預測）**：上方 Actions → 左側 Test Bot → Run workflow → 綠色 Run workflow。等幾分鐘點進最新一筆。
   - 看到：最下面 Summary 第一行寫 DRY RUN，3 題都有預測。**把 Summary 截圖給 Claude 看**，有紅框就停在這裡。
7. **打開排程**（Claude 看過第 6 步截圖說可以之後）：Settings → Secrets and variables → Actions → Variables 分頁 → New repository variable，Name 填 `BOT_ENABLED`，Value 只填 `1`。
   - 看到：之後每 20 分鐘跑一次，還是只列題、不送出，Summary 第一行是 DRY RUN。
8. **打開真的送出**（Claude 說可以之後）：同一個 Variables 分頁再加一個，Name 填 `PUBLISH`，Value 填 `1`。
   - 看到：下一輪 Summary 第一行變成「已送出」。要停送出就刪掉 PUBLISH，要整個停就刪掉 BOT_ENABLED。

### 之後的例行事項
- 這個 repo 每 60 天內要有一次 commit，不然 GitHub 會停掉排程。滿 45 天 Summary 會出黃框提醒。
- 賽季結束（約 2027 年 1 月）：填賽季末 survey，贏輸都要填，沒填下一季可能失格。
- 每一季開賽前：重新申請 AI 額度。
- 收到得獎通知後 180 天內：交 W-8BEN 和收款資料，逾期獎金沒收（日數來自搜尋摘要，以通知信為準）。

## 給你看的部分

### 它在做什麼
- 每 20 分鐘醒來一次（每小時的 7、27、47 分），到 Metaculus 看正賽跟 MiniBench 所有「還開著」的題目。
- 沒有新題：幾秒鐘看完就結束，不裝任何東西、不叫 AI。
- 有新題：上網查資料、讓 AI 預測，送出預測，每題附一則說明（comment，領獎條件）。說明送不出去時，下一輪自動補送同一份。
- 送出前會把太極端的數字收回來（是非題最低 3%、最高 97%；選擇題每個選項至少 1%），說明裡會寫「模型原值 -> 實際送出值」。數值題的單位看起來錯了會重問一次；還錯就不交那題，不交垃圾答案。
- 花錢自動配速：把剩下的額度平均分給到賽季結束前預估還會出的題目，錢多就每題多想幾次，錢少就每題只查一次、想一次，**不會因為錢少跳題**。剩不到 $3 才整個停。
- 11 月 1 日（第一輪 MiniBench 結算）以後，正賽拿大部分預算，MiniBench 改用最省的設定。兩個是分開的排行榜：MiniBench 省錢只影響「表現好會加碼額度」的機會，不影響正賽分數。
- 沒有任何人工改單題預測的開關（比賽規則：no human in the loop）。

AskNews（另一個新聞來源）預設不用：申請要全名和 LinkedIn。

### 可選的 repo variables（Settings -> Secrets and variables -> Actions -> **Variables** 分頁）
`BOT_ENABLED`、`PUBLISH` 只認 `1`；設成其他值（`0`、`true`、`yes`）那次執行會失敗並寫原因。其餘都是可選，沒設就用預設值。
| 變數 | 用途 | 預設 |
|---|---|---|
| `BOT_ENABLED` | 排程開關：有設才每 20 分鐘跑 | 沒設＝排程不跑（不寄信） |
| `PUBLISH` | 真的送出預測＋說明 | 沒設＝試跑，只列題不送 |
| `MAX_QUESTION_FAILED_RUNS` | 同一題連續幾輪失敗就先跳過 | 3 |
| `SKIP_COOLDOWN_HOURS` | 被跳過的題，隔幾小時再試一次 | 6 |
| `DRY_RUN_LLM_MAX_QUESTIONS` | 手動勾「dry_run_llm」時 AI 試跑幾題 | 2 |
| `TEST_MAX_QUESTIONS` | Test Bot 跑幾題 | 3 |
| `MAIN_FORECAST_MODEL` | 正賽最終預測用的模型 | `openai/gpt-5.6-terra` |
| `MINIBENCH_FORECAST_MODEL` | MiniBench 最終預測用的模型 | `openai/gpt-5.6-terra` |
| `ASSUMED_BUDGET_USD` | key 沒設上限時假定的總額度 | 100 |
| `BUDGET_STOP_BELOW` | 剩餘額度低於這個金額整個停 | 3 |
| `BUDGET_RESERVE` | 配速時保留不花的額度 | 等於 `BUDGET_STOP_BELOW` |
| `BUDGET_SPLIT_DATE` | 這天起正賽拿大部分預算 | 2026-11-01 |
| `MAIN_BUDGET_SHARE` | 分界日後正賽佔預算比例 | 0.8 |
| `MAIN_QUESTIONS_PER_DAY` / `MINIBENCH_QUESTIONS_PER_DAY` | 配速用的每日新題數估計 | 4 |
| `SEASON_END` | 配速算到哪天 | 2027-01-01 |
| `BINARY_MIN` / `BINARY_MAX` | 是非題送出機率下限／上限 | 0.03 / 0.97 |
| `MC_MIN_PROB` | 選擇題每個選項最低機率 | 0.01 |
| `BOT_USERNAME` | 排行榜用的 bot 帳號名 | 沒設就問 Metaculus `/users/me` |

- 手動執行（Run workflow）也看 `PUBLISH`：沒設一律不送。

### 怎麼看它有沒有在跑
1. GitHub：repo 的 **Actions** 分頁 -> 「Forecast on tournament questions」-> 點最新一次 -> 頁面下方 Summary。
   - 第一行是「**已送出**模式」或「**DRY RUN**」。
   - 「快速檢查」：開放題數、還沒預測幾題、額度剩多少、每題預算、預估額度見底日。每天第一輪多一段排行榜名次。
   - 有新題時多一段「預測」：這輪新交幾題、失敗幾題、這輪大約花多少。
   - **紅框**＝停了或有題目的說明送不出去，裡面寫原因。**黃框**＝提醒（key 沒設上限、repo 變私人、太久沒 commit、一邊賽事暫時抓不到題）。
2. 整輪停掉（額度用完、金鑰被拒、兩邊都抓不到題、全部題目失敗）時那次執行標失敗，GitHub 會寄信給你。單題失敗不寄信，只寫在 Summary。
3. Metaculus：切到 bot 帳號（settings -> My Forecasting Bots -> 切換）看題目上有沒有它的預測。說明是**私人**留言，切到 bot 帳號才看得到。

### 停損
排行榜那段會算「扣掉 Metaculus 自家 bot（metac- 開頭）之後的名次」。正賽累計 40 題結算後還在後 50%，Summary 會寫「觸發停損」：該停掉或換 model。

### 花費與分鐘（估算；實際以 Summary 為準）
- 起手約 $100：10 月中到 1 月初預估還有約 600 題（正賽、MiniBench 各約每天 4 題），每題預算約 $0.15，對應「查一次、想一次」，約 $0.10／題。題目比預估少時，配速會自動加到每題想 2–5 次。
- 每月約 240 題，約 $25–40／月；整季花費不會超過額度（配速就是在保證這件事）。
- GitHub Actions 分鐘：**公開 repo 用標準 runner 免費**，每 20 分鐘一次（每月約 2,200 次）不花錢。repo 被改回私人時 Summary 會出黃框（私人 repo 每月只有 2,000 分鐘）。
- 要降頻：只改 `.github/workflows/tournament.yaml` 裡 `cron:` 那一行。

---

## Technical

Base: [Metaculus/metac-bot-template](https://github.com/Metaculus/metac-bot-template) at
`096cb207d643da3090c4830cd828255bf18da2d5` (2026-10-15, remote `upstream`), forecasting-tools 0.3.4
(locked; `>=0.3.0,<0.4.0`), Python 3.12 on Actions. Spec: `docs/SPEC.md` (section 12 = review amendments).

### Files
| file | role |
|---|---|
| `precheck.py` | light step, stdlib only: switch validation, open-question listing (all pages), pending-comment re-send, work/no-work decision, repo warnings |
| `standings.py` | daily standings among non-`metac-*` bots + stop-loss verdict (stdlib; endpoints unverified) |
| `budget.py` | OpenRouter `/key` guard, model candidate lists, tier costs, pacing (stdlib) |
| `state.py` | pending comments carried between runs via `actions/cache` (write-ahead) |
| `main.py` | `ForecastBot2026` (template flow, rewritten prompts, final guards), CLI |
| `runner.py` | heavy run: fetch, per-question retries, 401/402 stop, publish forecast then comment, job summary |
| `guards.py` | binary clamp, iterative MC floor, unit-error formula (pure functions) |
| `tests/` | offline tests (sockets blocked, fake LLM, fixture questions/leaderboards) |

### Flow per scheduled run (`tournament.yaml`, every 20 min, `7,27,47 * * * *`)
1. Job-level `if:` skips the job while `BOT_ENABLED` is unset. `permissions: contents: read`, no PR triggers,
   actions pinned by SHA, `timeout-minutes: 40`, template `concurrency` kept.
2. `actions/cache/restore` brings back `.state/` (comments waiting to be re-sent).
3. `python3 precheck.py` on the runner's Python (nothing installed). Writes the summary header, lists open
   questions of FutureEval Fall 2026 (33121) and MiniBench (`minibench`) from `GET /api/posts/`, re-sends
   pending comments (`PUBLISH=1`), reads `/key`, prints pacing, sets `work`. Idle runs end here.
4. `standings.py --daily` (00:07 UTC run, `continue-on-error`).
5. Only if `work == 'true'`: setup-python, cached `.venv` (keyed on `poetry.lock`), `main.py --mode tournament`.
6. `actions/cache/save` when `.state/changed` exists.

### main.py / runner.py
- Models: final forecasts `MAIN_FORECAST_MODEL` / `MINIBENCH_FORECAST_MODEL`, default `openai/gpt-5.6-terra`,
  fallback `openai/gpt-5.4`; research, summary and parsing `openai/gpt-5.4-mini`. Explicit lists only;
  `-pro`, `-image`, `:batch` are never used. Research uses OpenRouter `plugins: [{"id": "web", "engine": "native"}]`.
- Pacing: per-question budget = (remaining - `BUDGET_RESERVE`) / estimated remaining questions
  (days to `SEASON_END` x `*_QUESTIONS_PER_DAY`). Richest affordable tier: 5 / 3 / 2 / 1 forecasts.
  From `BUDGET_SPLIT_DATE` (2026-11-01) the main tournament gets `MAIN_BUDGET_SHARE` (0.8) and MiniBench the
  1-forecast tier. Key without limit: remaining = `ASSUMED_BUDGET_USD` (100) - usage, yellow warning.
- Per question: up to 3 attempts within one run (`PER_QUESTION_RETRIES` = 2 extra). OpenRouter 401/402
  anywhere in the error chain stops the whole run at once, no retry.
- Cross-run failure count (`state.py`, via `actions/cache`, write-ahead: +1 and `last_attempt` are saved before
  the question is forecast, so a killed job still leaves the count; one success resets it). After
  `MAX_QUESTION_FAILED_RUNS` (default 3) consecutive failed runs the question is skipped with no LLM call;
  `SKIP_COOLDOWN_HOURS` (default 6) after its last attempt it gets one more try. 401/402, budget skips and
  stopped runs are not counted (the +1 is rolled back).
- Health probe: if >= 2 questions were attempted in a run and all failed (no 401/402), `runner.py` makes one tiny
  cheap-model call (up to 2 HTTP tries, 30 s each). Probe ok -> the failures are the questions' own, counts
  stay. Probe failed -> treated as an upstream outage, all counts of that run are rolled back, red box in the
  Summary. Probe 401/402 -> run stops, counts rolled back.
- Final value changed in one place only: `_aggregate_predictions` -> `apply_final_guards` (binary clamp,
  MC floor, numeric aggregate unit check). The comment's "Final Prediction" is the submitted value.
- Numeric: a forecaster whose median is off by >10x (positive ranges only: `median > 10*upper` or
  `lower > 0 and median < lower/10`) is re-asked once, then dropped. CDF validity is the SDK's job.
  No wide or 0.5 fallback is ever submitted.
- Publish: the SDK posts the forecast through a wrapper that holds back the comment; the comment is then
  posted with 3 attempts. The text is stored before the forecast is sent and removed once the comment is
  confirmed; leftovers are re-sent by the next run's precheck (gives up after 24 runs).
- Caps: publishing = none; scheduled dry run = 0 (listing only); manual dry run with `dry_run_llm` =
  `DRY_RUN_LLM_MAX_QUESTIONS` (2); Test Bot = `TEST_MAX_QUESTIONS` (3).
- Job red only for systemic problems: bad switch, token rejected, both tournaments unreadable, OpenRouter
  401/402/unreadable when LLM work is needed, every attempted question failed, every comment failed.

### Cost model (OpenRouter prices 2026-10-16, USD per M tokens)
gpt-5.6-terra $2 / $12; gpt-5.4 $2.50 / $15; gpt-5.4-mini $0.75 / $4.50; native search ~$0.01 per call.
Per question: research 10k/3k on mini + search ~$0.03, summary ~$0.005, each forecast 4k/4k on terra plus two
mini parse calls ~$0.06. Tiers: 1 forecast ~$0.10, 2 ~$0.16, 3 ~$0.22, 5 ~$0.34.

### Tests
```
poetry install --with dev && poetry run pytest
```
All offline (sockets blocked). See `BACKLOG.md` for the latest count.
