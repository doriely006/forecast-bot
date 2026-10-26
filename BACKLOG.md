# BACKLOG

## 收工日誌

### 2026-10-27 v0.4.1 MiniBench 備援：Anthropic 用不了就改 OpenAI（同一輪）
- 真跑 run 40355520725（暫把 FROM 設 10-26，跑完改回 11-01）：split 路徑起得來（minibench anthropic／main openai、配速 minimum），但 MiniBench 0 開放題＝備援路徑沒被真打到，仍未驗。
- user 10-26 裁定：MiniBench 不准停。觸發＝`runner.unusable(r)`：有 `stopped`／`skipped_budget` outcome，或 fatal failed，或 probe fatal（連 non-fatal failed 也搬）。一般單題失敗不觸發（照計數）。
- `Group.fallback`（lazy callable）：main.py 對 provider≠全域的組掛 `make_group(kind, 全域, solo=False)`＝不帶 `_PACE_KINDS`，配速同 11-01 前 OpenAI 兼兩賽事（含 C6 minimum）；同一 store＝OpenAI 免費 token／月付費同一本帳。
- `run()` 新 kwarg（預設關＝v0.4.0）：`only_urls`（備援只做被搬的題，抓題後過濾）、`serial_first`（有 fallback 的組第一題單跑 -> 死 provider 一輪只吃 1 個失敗請求；SDK 內建 429 重試仍可能多打，見沒驗）。
- `run_split`：被搬的 outcome 從原組移除，原組記 `fell_back`／`fallback_to`／`moved`，備援組 `fallback_of`、windows 清空。`SplitResult.active` 排除 fell_back 組 -> 健康／降黃／紅只看備援結果；OpenAI 也壞＝同 v0.4.0（main 好黃、都壞紅）。
- Summary：黃框「MiniBench 這輪改用 openai（原因：…）」＋「minibench 這輪送出：anthropic N、openai M」＋`### minibench：openai（備援）`。precheck：Anthropic part 有 problem 且≠全域 -> 再算一份全域 part（`minibench（anthropic，用不了）`／`（openai，備援）`＋黃框），work 看備援。
- `.state/provider_log.jsonl`：split＋publish＋tournament 時每題 MiniBench 已送（submitted/comment_failed）一行 `{id,provider,model,date}`，touch `changed`。單一路徑（11-01 前）不寫。
- 版號 0.4.1（pyproject／main／precheck／README／test_workflows）。README provider 段加一段。
- 測試 209 passed（舊 200，改 1：precheck 預算停改斷言備援；新 9）。
- 沒驗：真 402 時 SDK／forecasting_tools 是否一個請求就拋（Anthropic SDK max_retries=2 對 429 會重試）；真 API 的備援 run。

### 2026-10-26 v0.4.0 每賽事分開 provider（MiniBench 11-01 起 Claude）
- 新 var：`MAIN_LLM_PROVIDER`／`MINIBENCH_LLM_PROVIDER`（值同 LLM_PROVIDER）＋`*_LLM_PROVIDER_FROM`（UTC 日期，空＝即刻）。壞值／壞日期＝validate_switches 紅。
- `budget.tournament_provider`：未設、日期前、或＝全域 -> `select_provider`；key 缺 -> 全域＋黃框 note。`provider_env(env, p, [kind])` 複製 env、只在 p≠全域時塞 LLM_PROVIDER、加內部 `_PACE_KINDS`；既有 lookup_for／provider_prices／pacing_horizon 全跟著走。
- `pace`／`openai_plan`／`openai_pacing` 讀 `pace_kinds`：單一賽事時 spendable 全給它、不套 C6 minibench minimum、daily／later 只算該賽事題量。KINDS 全選＝原碼路徑。
- 兩邊 provider 相同（含全未設）-> cli／precheck 走原單一路徑（`provider_setup` 抽出、內容不變）。不同 -> `runner.run_split`：main 先、minibench 後，各自 `run()`（自己的 decision／stop／fatal／probe／ledger）。
- `SplitResult.systemic`：某組 provider 問題（fatal／預算 systemic）在另一組健康時降黃（`demoted`），兩組都壞才紅；「全部失敗」排除 fatal outcome；抓題紅照 B12 合併判。
- 隔離補洞：組間清 `store._wa`（probe rollback_all 不吃別組 +1）；prune_failures 改 split 尾端用兩組 key 聯集（不然 main 會 prune 掉 minibench 計數）。
- Summary：`### main：openai`／`### minibench：anthropic` 分節；precheck 每組縮排列 Provider／額度／本期或本月／配速；FROM 前有「預定 … 起改用」行。
- dry run cap（DRY_RUN_LLM_MAX_QUESTIONS）在 split 時每組各算一次。
- workflow：兩支加 4 個 var（tournament 放 job env）。README 變數表＋provider 段。版號 0.4.0。
- 測試：200 passed（舊 176＋`tests/test_split_provider.py` 24）。
- 估算：$15、stop 線 BUDGET_STOP_BELOW $3 -> 可用 $12；11-01 到 11-13 重置 48 題 -> $0.25／題 -> minimum（Opus x1，估 $0.17）。11-13 本機記帳換期歸零、會當又有 $15；若那 $15 是一次性儲值，實際額度用完會 402 -> MiniBench 黃框停、main 不受影響。
- 沒驗：真 API 的 split run（只有 offline）；`.state` 同一 state.json 兩本帳並寫（同進程依序，無並發）。

### 2026-10-26 v0.3.3 standings 真形狀（run 40336280903）
- bot 帳號＝midnightsharp-bot（users/me 實測）。`GET /leaderboards/project/<id>/` 回 list of leaderboard 物件（鍵含 is_primary_leaderboard／entries／bot_exclusion_status…），resolve_project 正常。
- `parse_entries`：list -> 取 is_primary_leaderboard 為真者，否則第一個，讀其 `entries`；dict 舊路徑不變。
- entries 內欄位名仍未知：全無 username 或解析丟例外 -> `EntryShapeError`，失敗行印首筆 entry 的 key（巢狀 dict 展一層，不印值）。entries 空 -> `榜上 0 筆`（正常狀態）。
- 待：下次手動跑看首筆 entry keys，必要時調 name/rank/score/is_bot 欄位讀法。

### 2026-10-26 v0.3.2 standings
- `due()` 改「當日第一輪」：日期存 `.state/standings_day`（YYYY-MM-DD，UTC）；缺檔／讀不到＝跑（fail-open）；workflow_dispatch 永遠跑。
- `mark_done` 在 fetch 前寫日期並 touch `.state/changed`（precheck 已先 clear_changed，cache restore 在 precheck 之前，順序不用動）-> 尾端 cache/save 存新 entry。標在 fetch 前＝榜讀失敗也不會每 20 分鐘重打。
- Summary 加 `- bot 帳號：<name>`。
- 讀榜失敗印 `Class: msg`（≤200）＋`shape_of`（top type、top keys、第一個 list 欄位的長度與首元素 keys，只有型別／key 名、不印值），每板 ≤400 字。users/me body 不印。
- 版號 pyproject／main／precheck／README／test 斷言 -> 0.3.2。yaml 沒動。
- 測試：pytest 全綠（見 commit）；新增 once-a-day、fail-open、username、shape 不洩值。
- 沒驗：真回應形狀（下次 workflow_dispatch 看 log）。

### 2026-10-26 OpenAI 帳單校準
- user 截 Usage 頁：10 月實付 $0.35（org 上限 $5.00），35 web search requests，472,836 tokens／153 requests。本機記帳同期 $0.75。
- $0.35＝35×$0.01 -> 用 tool 的請求 token 也走免費額度，只收 search 費；v0.3.0「tool 請求整筆付費」假設證偽。
- repo var `OPENAI_TOOL_TOKENS_FREE=1` 已設（主 session，gh variable set）。下輪週檢對：本機記帳 vs Usage 頁應接近。

### 週檢 2026-10-25
- 「Forecast on tournament questions」10-18 起：success 11／failure 1／skipped 33（precheck 無工作）。唯一 failure＝39991270934（10-23 首輪 live，405 already closed，v0.3.1 已修），之後全綠。
- 本週新交 4（40108814448:2、40133156079:1、40150278803:1）；失敗 0（v0.3.1 後）；臨截止跳過 0；預算不足跳過 0。
- 最近成功 run 40150278803：OpenAI 本月付費 $0.41／$4.50（本機記帳）、剩餘 $4.09、配速 lean（main／minibench 各 $0.07／題）、預估見底 2026-11-04。
- 無停損、無 fatal。
- 判斷：沒事。花費尚未對 OpenAI 帳單校準（本機記帳已知高估），推播請 user 截 Usage 頁。

### 2026-10-24 v0.3.1 臨截止跳過＋closed 分類（首輪 live run 39991270934）
- 病：FutureEval 題只開 ~1.5h，研究完已截止，post 回 405 "already closed to forecasting" -> 3 posts x 庫內 3 次 backoff ~6 分鐘、outcome=failed、唯一 attempted -> systemic -> exit 1（紅 job＋寄信）。
- `runner.run`：fetch 迴圈內 `clock < close_time <= clock+MIN_MINUTES_BEFORE_CLOSE`（預設 10，非數字／<0 回 10）-> `Outcome skipped_closing`，不進 to_forecast（不吃 cap／配速／openai plan／store）；`result.outcomes += closing` 在 after-lookup 前。clock＝`now` 或真時鐘，日期 != `today` 時用 today 的 00:00 UTC（同 openai plan 規則）。已過期（close<=clock）的不擋——API 只回 open，且舊測試 fixture 103 刻意用過去時間。
- `publish_one`：post 例外 `is_closed_error`（status 405／文字含 "closed to forecasting"／字面 405）-> `store.remove`、立刻 return `Outcome closed`，不再跑外層 3 輪。`_count_failure` 走 else -> `rollback`（不進 MAX_QUESTION_FAILED_RUNS）。
- `NOT_ATTEMPTED` 常數（skipped_budget／stopped／skipped_closing／closed）給 `systemic` 與 `_upstream_suspect`。Summary 新行「臨截止跳過（不花 LLM）／作答期間已截止（沒送出）」。
- workflow：兩支 yaml 加 `MIN_MINUTES_BEFORE_CLOSE: vars.…`（tournament 放 job env）；README 變數表加一列；版號 pyproject／main／precheck／test。
- 測試 171 passed（舊 167＋4：臨截止跳過且 systemic 空、閾值可調＋無 close_time 不受影響、post 405 -> closed 不計數不紅、is_closed_error）。
- 沒做：庫內 `retry_with_exponential_backoff` 對 405 的重試（decorator 在 metaculus_client `_post_question_prediction`，要 monkeypatch 內部；每次 post 仍白等 ~2 分鐘）。post 前預檢 close_time 已過也沒做（scheduled_close_time 可能不等於實際）。
- 已知：precheck 仍把「只剩臨截止題」算有工作（job 會起、不花 LLM、log 短）；closed 題下輪若仍 open 會再試一次。

### 2026-10-24 v0.3.0 OpenAI 直連路徑（data-sharing 免費日額度）
- 選路：`LLM_PROVIDER` 收 openai；未設時 OPENROUTER_API_KEY > ANTHROPIC_API_KEY > OPENAI_API_KEY。`provider_label`／`decide` key 名／`lookup_for`／`pacing_horizon`（月底）／`provider_prices`（cheap＝parse model）都加 openai 分支。
- SDK：用已在依賴裡的官方 `openai` 2.13.0（poetry.lock 不動），Responses API。main.py `OpenAILlm(GeneralLlm)` 照 AnthropicLlm 模式：自己 retry（quota／401／403／400／404 不重試）、`reasoning.effort` 一律送（gpt-5.4 系預設 none）、不送 temperature/top_p。research＝`web_search` tool、`search_context_size` low、`max_tool_calls` 3；url_citation 附在研究尾；`status=="incomplete"` 只 log；refusal content part -> 普通失敗。
- 模型（env 可換）：forecast gpt-5.4（big 組）、research／parse gpt-5.4-mini（mini 組）。`budget.free_group` 認 dated snapshot。
- 記帳 `state.openai`（state.json `openai_usage`）：`day`＋`tokens{big,mini}`（UTC 日換日歸零）、`month`＋`spent`（月換歸零）。`openai_call_charge`：用 tool 的請求整筆付費且不計入免費 token（假設，`OPENAI_TOOL_TOKENS_FREE=1` 可翻）；越過日額度那一筆整筆付費（help 文章範例）；非免費清單 model 全付費（未列價格用 5/30 偏高估）；web search $0.01/次只數 action=search。
- 配速 `openai_plan`：最豐 tier 使 (本輪題數＋今天剩餘時數×每日題數估計)×big token/題 ≤ 日額度×(1−10%)−已用。連 minimum 都放不下：塞得下的免費、截止 ≤30h 付費 minimum、其餘延後（不計失敗；Test Bot 不延後）。`openai_pacing` 生 Pacing 給 Summary（USD/題＝月剩餘/月底前預估題數）。precheck 跑同一個 plan，全延後＝work=false。
- 美元：`OPENAI_BUDGET_USD`=4.5／月，`OPENAI_BUDGET_STOP_BELOW`=0.25（不吃 BUDGET_STOP_BELOW=3，不然 $4.5 只能花 $1.5）。
- fatal：`openai_fatal`：402，或 code/type ∈ insufficient_quota／credit_balance_exhausted／organization|project_spend_limit_exceeded／organization_usage_limit_exceeded -> `OpenAIQuotaError(402)`；401 走 SDK 自帶 status_code。runner `_FATAL_PATTERNS` 加這些 code 字串（report.errors）。
- Summary／precheck：`Provider：openai`、今日免費 token 各組已用／剩、本月付費／預算、延後題黃框、付費作答題數。
- workflow：兩支都轉接 `secrets.OPENAI_API_KEY`＋19 個 `vars.OPENAI_*`（tournament 放 job env）。
- 測試：`.venv/bin/python -m pytest` -> 167 passed（舊 146＋新 21 `tests/test_openai.py`；舊檔只改版號斷言、conftest 清 OPENAI_API_KEY）。另在 scratchpad 用 httpx MockTransport 對真 SDK 驗：請求序列化（instructions／reasoning／tools／max_tool_calls）、真 `Response` 物件解析含 url_citation、429 insufficient_quota -> 402（未進 repo）。
- 估算（token 未量測）：big 組每題 full 55k／standard 33k／lean 22k／minimum 11k -> 225K 可用＝4／6／10／20 題/天。research 付費約 $0.0475/題 -> 可花 $4.25（扣 stop 線）約 89 題/月（約 3 題/天）；預設估 8 題/天＝約 $11/月，會在月中撞 stop。
- 未驗（沒打真 API）：免費 token 是否真的不含 tool 請求的 token（help 文章 403，只有搜尋摘要「tool use is excluded」）；00:00 UTC 重置（搜尋摘要寫 2025-03-06 起，標假設）；Responses 預設 store=true 是否為 data sharing 資格前提（沒送 store，維持預設）；gpt-5.4 effort medium 的實際 reasoning token（假設 5k out）；research 實際 search content token（假設 15k）與每次 search 數；`max_output_tokens=16` 探測在 reasoning model 回 200 還是 400；reasoning 文件一句「effort none 回 400」與 model 頁「預設 none」矛盾（我們從不送 none）。
- 已知盲點：本機記帳看不到同 org 其他流量（Playground、別支 key 吃同一份免費額度）；Test Bot 與 tournament 同時跑 `.state` 互蓋（v0.2.0 就有）；美元 stop 會連免費的 forecast 一起停（research 付費是唯一花費來源）。

### 2026-10-23 v0.2.0 Anthropic API 路徑（OpenRouter 額度沒到）
- 選路：`budget.select_provider`。`LLM_PROVIDER`（anthropic|openrouter，其他值＝validate_switches 紅）；未設時有 OPENROUTER_API_KEY 走 openrouter（原行為不動），否則有 ANTHROPIC_API_KEY 走 anthropic。OpenRouter 路徑只換成 `provider_prices()` 包裝，內容同舊碼。
- `anthropic` SDK 2.2.0 進 pyproject／poetry.lock（`poetry add --lock`，帶進 httpx2 等）。不走 litellm。
- main.py `AnthropicLlm(GeneralLlm)`：覆寫 `_mockable_direct_call_to_model`＋自己的 retry（auth／credit／refusal／400／404 不重試，其他重試 1 次；SDK 自己 429/5xx 重試 2 次）。`client.beta.messages.create`；Opus 帶 `fallbacks="default"`＋beta `server-side-fallback-2026-07-01`，Haiku 不帶（Haiku 5.5 無 server-side fallback）。effort：forecast `ANTHROPIC_EFFORT`(medium)、research medium、summary／parser low。從不送 temperature/top_p/top_k/thinking/tool_choice、無 prefill。先查 `stop_reason=="refusal"` 再讀 content -> `AnthropicRefusal`（普通失敗）。research＝`web_search_20250305` max_uses 5（skill 的 dynamic-filter 版清單沒列 Haiku 5.5，故用 basic）；200 裡的 web_search error object 只 log；pause_turn 最多續 3 次；citations URL 附在研究尾。
- 錯誤：`anthropic_fatal`：402，或 400/429 訊息含 credit balance／spend limit|cap／usage limit／billing -> `AnthropicCreditError(status_code=402)`；401 直接用 SDK 的 status_code。runner 既有 `llm_fatal_status` 吃得到，整輪停、不計數。fatal 字串改 `{Anthropic|OpenRouter} HTTP N`。
- 記帳：`budget.anthropic_usage_cost`（usage.iterations 有就逐 attempt 依各自 model 計價；cache write 1.25x、read 0.1x 為假設；web search $0.01／次＝官方文件 10-23 實查；Haiku >100K input 用 0.50/2.50）。每次呼叫（含 refusal、探測）當下 `state.add_spend` 寫 `.state/state.json` 的 `anthropic_spend`（period_start／spent／calls）；換期歸零。期間＝`credit_period(today, ANTHROPIC_CREDIT_RESET_DAY=13)`，預算 `ANTHROPIC_BUDGET_USD`=90。
- 預算：`anthropic_lookup_from_store` 偽裝成 KeyLookup（limit=預算、usage=本期已花），沿用 `decide()`／逐題 live 檢查；state.json 不存在 -> `KeyLookup.note` -> 黃框。`pace(..., horizon_end, provider)`：Anthropic 配速到下次重置日，tier 成本用 `A_*` token 假設。
- Summary／precheck：多 `Provider：` 行、Anthropic 本期花費／預算／剩餘、標籤依 provider。precheck 不打 OpenRouter。探測 `make_anthropic_probe`：haiku、max_tokens 16、max_retries 0、任何 200＝ok，花費記帳。
- workflow：兩支都轉接 `secrets.ANTHROPIC_API_KEY`＋`vars.LLM_PROVIDER`／`ANTHROPIC_*`（tournament 放 job env，precheck 與 main 都吃到）；Test Bot 加 `.state` cache restore/save（花費記錄）。
- 測試：`.venv/bin/python -m pytest` -> 146 passed（舊 123＋新 23，`tests/test_anthropic.py`；舊檔只改版號斷言、secret 清單、conftest 清 ANTHROPIC_API_KEY／LLM_PROVIDER）。另用 httpx2 MockTransport 對真 SDK 驗過序列化（beta header、fallbacks、無 sampling 參數）與 400 credit -> 402（scratchpad，未進 repo）。
- 每題估算（Anthropic，token 數未量測）：full $0.72／standard $0.45／lean $0.31／minimum $0.17。$87 可用、21 天到 11-13 -> standard。
- 未驗（完全沒打真 API）：Opus 5.5 effort medium 的實際 thinking token（假設 6k out）；Haiku research 實際 input（假設 30k）；credit 用完的真實錯誤形狀（文件只寫 400 spend limit／402 billing／429 tier cap，「credit balance is too low」字串是 [K]）；`fallbacks="default"` 在 2.2.0 SDK 經 beta 端點是否被 API 接受；cache 計價倍率；Test Bot 與 tournament 同時跑時 `.state` cache 互蓋（後存者贏，可能丟一邊的花費／comment 狀態）。
- 已知盲點：本機記帳看不到同 org 其他用量（Console、別支 key）；本期 10-13 起若已有花費，`ANTHROPIC_BUDGET_USD` 要先扣掉。403 PermissionDenied 沒列 fatal（會當普通失敗＋探測）。

### 2026-10-17 v0.1.5 最終 model 換 gpt-6.1-sol
- budget.py：FORECAST_CANDIDATES＝gpt-6.1-sol -> gpt-5.6-terra -> gpt-5.4；`_EXCLUDED` 加 `codex`；FALLBACK_PRICES 加 6.1-sol／6-sol 2/10、6-astra 10/50、6-luna 0.10/0.50（10-17 實價，主 session 查）。main.py 沒動（預設走 FORECAST_CANDIDATES，正賽與 MiniBench 同步）。
- 新預設 tier 每題：full(5) $0.303／standard(3) $0.196／lean(2) $0.143／minimum(1) $0.089。README 成本段與月花費同步，舊數字刪除。SPEC 12.2 後補 10-17 一行。
- 新測試 3 個（預設 sol、sol 不在清單退 terra 再退 5.4、-pro／codex 等變體排除含 sol-pro）。
- README 改成中立英文技術版；業主操作手冊移到 repo 外（主 session 的 artifact）。
- 未驗：gpt-6.1-sol 在 OpenRouter 的實際可用性與 reasoning token 用量（FORECAST_TOKENS 4k out 假設可能偏低）；第一次真跑看 Summary 實際花費。

### 2026-10-16 README
- README 修正（對應驗收 FAIL 1–4）：user 步驟全文置頂（八步＋例行事項）、版號 0.1.4、停損段改寫成跨輪計數＋探測、補 repo variables 清單。

### 2026-10-16 v0.1.4 毒題漏洞修補（SPEC 12.4 末行）
- runner：`_upstream_suspect`（試 ≥2 且全 failed 非 fatal）現在只是「要不要探測」的條件。`_health_probe`：`probe_fn` 最多 `PROBE_ATTEMPTS`=2 次、各 `PROBE_TIMEOUT_S`=30 秒（asyncio.wait_for）。成功 -> `result.probe="ok"`、計數照舊、summary 黃框；非 401／402 失敗 -> `probe="failed"`＋`upstream_suspect`＋`store.rollback_all()`、紅框附 `probe_error`；401／402 -> `probe="fatal"`、設 `result.fatal`、rollback_all、不重試。`probe_fn=None` 不探測、照計數（測試預設）。
- 花費：`probe_calls`／`probe_cost`（cheap 價 x 30 in／16 out token 粗估）；`spend` 有 key usage 時用 usage 差（已含探測），沒有時 outcomes＋probe_cost；summary 花費行尾註「含探測 N 次」。
- main：`make_probe(cheap_id)` 直接 `litellm.acompletion`（openrouter/<cheap>、max_tokens=16、num_retries=0，任何回應都算通，不看內容是否為空，避免 reasoning model 把 16 token 吃光被誤判）；cli 傳 `probe_fn`。這支沒有被真 API 驗過。
- 舊測試改動：`test_round_where_two_or_more_all_fail_counts_nothing`／`test_systemic_round_restores_the_old_entries_exactly` 行為被取代，改成注入「探測失敗」的 fake probe 後斷言原樣；其餘舊測試未動。新增 9 個（毒題 3 輪後跳過、探測失敗撤回、重試成功、timeout、402／401、不該探測的三種輪、dry run LLM 路徑與無 probe_fn、花費）。
- 測試：`.venv/bin/python -m pytest` -> 119 passed。
- 未驗：探測對真 OpenRouter 的行為（model id 前綴、max_tokens=16 對 gpt-5.4-mini 是否回 200）；第一次真跑看 log 有無 "Health probe failed"。
- 已知殘洞：上游故障期間每 20 分鐘整批重試（以前 3 輪就停）、失敗呼叫仍可能已花研究費；探測只擋「計數」，不擋重試。

### 2026-10-16 v0.1.3 止血補洞（SPEC 12.4 末行）
- state.py：failure entry 加 `last_attempt`（UTC ISO）。`begin_attempt` write-ahead（+1、寫檔、記進記憶體 `_wa` 存舊值）／`finish_failure`（只補 last_error）／`rollback`／`rollback_all`；`clear_failure` 順手清 `_wa`。`split_skipped(limit, cooldown_h, now)` -> (冷卻中含 `next_retry`, 到期 key)。`skip_cooldown_hours(env)` 預設 6。`record_failure` 保留（直接 +1，測試造資料用）。
- runner：`one()` 在 forecast_one 前 begin_attempt（`stop` 已設就不記）；`_count_failure` 依 outcome：failed 非 fatal 保留、成功清零、其餘（fatal／stopped／skipped_budget）rollback。gather 後 `_upstream_suspect`（試 ≥2 且全 failed 非 fatal）-> `rollback_all`＋`result.upstream_suspect`，summary 紅框。`_drop_failing` 只丟冷卻中的，到期的留下試。`run()` 加 `now` 參數（測試用）。
- precheck：`Deps.now`；冷卻到期＝有工作、冷卻中＝沒工作；黃框列「下次重試 UTC」。workflow env 加 `SKIP_COOLDOWN_HOURS`（job 層，precheck 與 main 都吃得到）。
- 舊 state（無 last_attempt）的已跳過題：視為到期，立刻多試一次。
- 測試：`.venv/bin/python -m pytest` -> 110 passed。
- 已知殘洞：①連兩題以上「同時毒題」被當上游故障不計數（v0.1.4 已補）；②上游故障期間每 20 分鐘整批重試（以前 3 輪就停），失敗呼叫仍可能已花研究費。

### 2026-10-16 v0.1.2 跨輪失敗止血（C5 部分回復，SPEC 12.4 末行）
- state.py：`.state/state.json` 加 `question_failures`（key=question id，無 id 用 `p<post_id>`；count／title／url／last_error）。同一個 actions/cache、同一個 `.state/changed` 觸發存檔。
- 計數：runner `_count_failure`，Outcome failed 且非 fatal -> +1；submitted／comment_failed／dry_run -> 清零；401／402（Outcome.fatal）與 stopped／skipped_budget 不動。每題出結果當下就寫檔。
- 跳過：`_drop_failing`（cap 之前，不佔 cap）；每輪順手 prune 已不在待預測清單的 key（有抓題失敗時不 prune）。上限 `MAX_QUESTION_FAILED_RUNS`（預設 3，非數字或 <1 回預設），workflow env 已轉接 `vars.MAX_QUESTION_FAILED_RUNS`。
- precheck：已跳過的題不算 work；黃框列 id／題名前 60 字／次數／最後錯誤。runner 只在沒有 precheck 的情境（Test Bot）自己列，避免重複。
- main.py：tournament 模式（含 dry run）都載入 store；test_questions 不載入、不計數。
- （v0.1.3 已補：write-ahead 計數、上游故障輪不計、冷卻。）
- 重試被跳過的題：把 MAX_QUESTION_FAILED_RUNS 調高；沒有「清單歸零」按鈕，題目截止後 prune 自動清掉。
- 測試：`.venv/bin/python -m pytest` -> 100 passed。

### 2026-10-16 v0.1.1 審查修正（SPEC §12：A1–A5、B1–B14、C1–C10＋主 session 覆寫）
- repo 已 PUBLIC。排程 `7,27,47 * * * *`；公開 repo 標準 runner 免費。BOT_ENABLED／PUBLISH 仍未設。
- 新檔：precheck.py（stdlib 輕量步：開關只認 "1"、分頁列題、補送 comment、決定 work、45 天沒 commit／repo 變私人標黃）、state.py（只存待補送 comment，write-ahead，actions/cache 跨輪；B2 失敗計數已刪）、standings.py（C7，非 metac 名次＋停損）。
- budget.py：/key 狀態 ok/stop/auth_error/unknown；無上限 key 用 ASSUMED_BUDGET_USD−usage 配速＋標黃（C3）；model 明列清單 terra→5.4、mini（B11/C2）；tier 5/3/2/1 次預測＋配速＋11-01 後正賽 80%（A2/C6）。
- main.py：研究固定 mini＋native search；數值單位錯兩次＝該 forecaster 失敗，不送寬分布（B2）；CDF 合法性交 SDK（B8）；summary 寫在 finally（B1）。
- runner.py：每賽事各自 try（B12）；401/402 整輪停不重試（B3）；預測與 comment 分開送、comment 失敗標紅＋存檔（A3）；dry run 預設只列題、手動 dry_run_llm 上限 2（A1）；Test Bot 上限 3（B7）。
- workflow：contents: read、無 PR 觸發、action 鎖 SHA、timeout 40、閒置輪只跑 checkout＋precheck（B14）。
- 測試：`.venv/bin/python -m pytest` -> 90 passed / 0 failed（本機 Python 3.12.15，全離線、socket 封鎖；v0.1.0 的 54 個裡，行為被規格改掉的改寫成新斷言或換成對應新測試，其餘原樣）。
- 未驗（沒有 token）：/api/posts 欄位（my_forecasts、scheduled_close_time）只照 SDK 原始碼寫；leaderboard／projects／users/me 端點與欄位是推測；litellm 對 OpenRouter 402 的實際例外形狀；schedule 事件是否帶 `github.event.repository.private`（沒帶就改問 GitHub API）。

### 2026-10-16 v0.1.0 初版
- 底：Metaculus/metac-bot-template `096cb207`（remote `upstream`）。forecasting-tools 0.3.4，Actions 用 Python 3.12＋整個 .venv 快取。
- 移除：`.claude/skills/review-bot/SKILL.md`、main_with_no_framework.py、integrations/、review_bot／metaculus_cup workflow。
- SDK 實讀：`publish_report_to_metaculus` 先 POST forecast 再 POST `/comments/create/`（`is_private=True`）；explanation 必須以 `#` 開頭；`get_all_open_questions_from_tournament` 只抓第一頁 100 題。

### 下一輪開工先做
- v0.4.0 MiniBench Claude（11-01 自動切）：10-26 主 session 已設 var `LLM_PROVIDER=openai`、`ANTHROPIC_BUDGET_USD=14`（Console credit $14.72）、`BUDGET_STOP_BELOW=0.5`（只 Anthropic 吃）、`MINIBENCH_LLM_PROVIDER=anthropic`、`MINIBENCH_LLM_PROVIDER_FROM=2026-11-01`；run 40346375097 v0.4.0 綠、Provider openai。secret `ANTHROPIC_API_KEY` 10-26 user 已放（key 到期 2027 年底）。Console org 月上限 $100 本月已被**非 bot** 用量吃滿（$100.28，Max 5x 附送 $100 API credit 10-12 入帳、11-15 到期），11-01 00:00 UTC 才恢復；11 月那個不明用量會跟 bot 搶同一個上限，10-26 查出：主因＝10-18 twin 判官 eval（當時 `claude -p` 被記成 API，約 $68）；10-26 實測 `claude -p` 已走訂閱（API 被擋時照樣成功），原因未明（memory claude-p-bills-api-credit）。11-01 手動跑一次：Summary 要有 `### main：openai`／`### minibench：anthropic` 兩節、MiniBench 本期花費有動、Console 用量對得上；若 Anthropic org 上限仍滿（v0.4.1）：要看到黃框「MiniBench 這輪改用 openai（原因：…）」、`### minibench：openai（備援）`、`.state/provider_log.jsonl` 有 openai 行，job 綠。
- standings v0.3.3 真跑 OK（run 40336563878）：bot 帳號 midnightsharp-bot；正賽 33121 榜 359 筆、尚無本 bot（未有結算題）；MiniBench 榜 0 筆。剩：隔天確認排程只跑一次；本 bot 上榜後核對名次欄位讀法（entry 欄位對應未經真資料驗）。
- v0.3.0 OpenAI：已上線（10-23 起 PUBLISH=1）；tool token 假設 10-26 已校準（見收工日誌）。剩：校正 `O_*` token 假設（Usage 頁 472K tokens／153 requests 可當起點）；11 月首次週檢對本機記帳 vs Usage 頁。
- v0.2.0 Anthropic：user 放 `ANTHROPIC_API_KEY` secret（不設 OPENROUTER_API_KEY 就自動走 Anthropic）、評估本期已花多少再設 `ANTHROPIC_BUDGET_USD` -> 手動跑 Test Bot（PUBLISH 未設）-> 對 Summary 的本期花費 vs Console 實際用量、log 有無 refusal／web search error／max_tokens 警告，校正 `A_*` token 假設。
- user 放好 secrets、確認 auto top-up 關閉、填表單後：手動跑 Test Bot（PUBLISH 未設）-> 看 Summary 有無紅框、花費、log 裡 web search 有沒有引用來源。過了就設 BOT_ENABLED=1＋PUBLISH=1。
- 第一次真跑後對：precheck 列題數＝main 抓到的題數；Summary 每題實際花費 vs 估算（tier 成本在 budget.py 的 token 假設，要校正）；standings 那段是否讀得到。
- 45 天內要有 commit（公開 repo 排程 60 天停用）。
- 第一次真跑看 log 有無 "Health probe failed"（探測沒打過真 API；誤判失敗會把毒題計數撤回）。
- README Technical 段除了止血那段，其餘仍有 v0.1.1 以前的描述，沒逐條對過 code；下次動 README 時順手對。
- 開 BOT_ENABLED／PUBLISH 前，user 的 Test Bot Summary 截圖要先給主 session 看過（README 第 6–8 步已寫）。

## v0.2 清單
- **MiniBench Claude 對照組（user 10-17 拍板）**：第一輪 MiniBench 結算後（約 11-01，C6 切便宜設定那天），MiniBench 的最終預測 model 換成 Claude（OpenRouter 上的 Anthropic 款），正賽維持 GPT。跑滿一輪 MiniBench 後，比兩邊非 metac 名次（standings.py），Claude 贏就提案正賽換。user 想押 Claude；參賽表單 models 欄已寫兩家＋這個計畫。
  - 公平性：C6 原訂 11-01 後 MiniBench 走最便宜設定——對照組期間作廢，Claude 輪要跑跟正賽 GPT 同檔位（full），否則比較不算數。
  - 10-26 改道（v0.4.0）：走 Anthropic 直連（`MINIBENCH_LLM_PROVIDER=anthropic`＋FROM 11-01），不走 OpenRouter；main 留 OpenAI 免費額度。預算 $15 -> 配速出 minimum（Opus x1）；GPT 那輪 MiniBench 實跑 lean x2 gpt-5.4，檔位本來就不相等 -> 比較是「整套設定 vs 整套設定」、受成本限制，不是同檔位對照，結果只當提示。
  - 輪次（10-17 主 session 提、user 問 Fable）：第 2 輪 Opus 5.5 full tier（約 $0.54／題）；第 3 輪 Fable 5.1 **minimum tier**（1 次預測，約 $0.28／題＝跟 terra full 同價），順便測「一次強預測 vs 五次中等預測平均」。Fable full tier 約 $1.25／題，不跑。單輪約 60 題、雜訊大，結果只當提示。
  - gpt-6-astra（$10/$50）比照 Fable：只跑 minimum tier（1 次預測）。
  - 價格出處：OpenRouter /api/v1/models 10-17 實查；單題成本＝budget.py 的 token 假設推算。申請額度表單填 300。
- 待驗：gpt-6-luna（$0.10/$0.50）是否支援 OpenRouter native web plugin；可行就把研究 model 換成 luna 省錢（不要在沒驗前換）。
- Date／Conditional 題型：碰到就記錄跳過（現況：Conditional 已跳過並列在 Summary；Date 仍會預測）。
- 配速是否偏重 MiniBench（MiniBench 表現好會自動加碼額度）；現在是 11-01 後正賽 80%。
- 失敗止血上線後盯：上游故障期間整批重試的實際花費；探測第一次真跑是否回 200。
- agentic 迭代搜尋（多輪查、查到缺口再查）。
- 已結案相似題＋預測市場（Polymarket／Kalshi／Metaculus 已結案題）查詢，當 base rate 來源。
- 跨家族 ensemble（GPT＋Claude），先小樣本比對再上。
- Platt 校準（用已結算題擬合；binary 先做）。
- 回測：避開 model knowledge cutoff 洩漏（只用 cutoff 之後開的題）。
