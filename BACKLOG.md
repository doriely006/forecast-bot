# BACKLOG

## 收工日誌

### 2026-10-16 v0.1.2 跨輪失敗止血（C5 部分回復，SPEC 12.4 末行）
- state.py：`.state/state.json` 加 `question_failures`（key=question id，無 id 用 `p<post_id>`；count／title／url／last_error）。同一個 actions/cache、同一個 `.state/changed` 觸發存檔。
- 計數：runner `_count_failure`，Outcome failed 且非 fatal -> +1；submitted／comment_failed／dry_run -> 清零；401／402（Outcome.fatal）與 stopped／skipped_budget 不動。每題出結果當下就寫檔。
- 跳過：`_drop_failing`（cap 之前，不佔 cap）；每輪順手 prune 已不在待預測清單的 key（有抓題失敗時不 prune）。上限 `MAX_QUESTION_FAILED_RUNS`（預設 3，非數字或 <1 回預設），workflow env 已轉接 `vars.MAX_QUESTION_FAILED_RUNS`。
- precheck：已跳過的題不算 work；黃框列 id／題名前 60 字／次數／最後錯誤。runner 只在沒有 precheck 的情境（Test Bot）自己列，避免重複。
- main.py：tournament 模式（含 dry run）都載入 store；test_questions 不載入、不計數。
- 沒解的洞：整個 job 被殺（timeout 40 分、runner 掛）的題不會被計數，因為計數在出結果時才寫；要補得改成開跑前 write-ahead。
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

## v0.2 清單
- Date／Conditional 題型：碰到就記錄跳過（現況：Conditional 已跳過並列在 Summary；Date 仍會預測）。
- 配速是否偏重 MiniBench（MiniBench 表現好會自動加碼額度）；現在是 11-01 後正賽 80%。
- 失敗止血（v0.1.2）上線後盯：3 輪上限是否太緊（暫時性 LLM 故障連 1 小時會誤殺題）；要不要加冷卻時間而不是永久跳過。
- agentic 迭代搜尋（多輪查、查到缺口再查）。
- 已結案相似題＋預測市場（Polymarket／Kalshi／Metaculus 已結案題）查詢，當 base rate 來源。
- 跨家族 ensemble（GPT＋Claude），先小樣本比對再上。
- Platt 校準（用已結算題擬合；binary 先做）。
- 回測：避開 model knowledge cutoff 洩漏（只用 cutoff 之後開的題）。
