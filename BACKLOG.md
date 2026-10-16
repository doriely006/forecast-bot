# BACKLOG

## 收工日誌

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
- user 放好 secrets、確認 auto top-up 關閉、填表單後：手動跑 Test Bot（PUBLISH 未設）-> 看 Summary 有無紅框、花費、log 裡 web search 有沒有引用來源。過了就設 BOT_ENABLED=1＋PUBLISH=1。
- 第一次真跑後對：precheck 列題數＝main 抓到的題數；Summary 每題實際花費 vs 估算（tier 成本在 budget.py 的 token 假設，要校正）；standings 那段是否讀得到。
- 45 天內要有 commit（公開 repo 排程 60 天停用）。
- 第一次真跑看 log 有無 "Health probe failed"（探測沒打過真 API；誤判失敗會把毒題計數撤回）。
- README Technical 段除了止血那段，其餘仍有 v0.1.1 以前的描述，沒逐條對過 code；下次動 README 時順手對。
- 開 BOT_ENABLED／PUBLISH 前，user 的 Test Bot Summary 截圖要先給主 session 看過（README 第 6–8 步已寫）。

## v0.2 清單
- **MiniBench Claude 對照組（user 10-17 拍板）**：第一輪 MiniBench 結算後（約 11-01，C6 切便宜設定那天），MiniBench 的最終預測 model 換成 Claude（OpenRouter 上的 Anthropic 款），正賽維持 GPT。跑滿一輪 MiniBench 後，比兩邊非 metac 名次（standings.py），Claude 贏就提案正賽換。user 想押 Claude；參賽表單 models 欄已寫兩家＋這個計畫。
  - 公平性：C6 原訂 11-01 後 MiniBench 走最便宜設定——對照組期間作廢，Claude 輪要跑跟正賽 GPT 同檔位（full），否則比較不算數。
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
