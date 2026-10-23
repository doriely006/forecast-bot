# forecast-bot

v0.2.0. A low-cost, fully automated Metaculus bot for the Fall 2026 FutureEval tournament and MiniBench.
Based on [Metaculus/metac-bot-template](https://github.com/Metaculus/metac-bot-template) at
`096cb207d643da3090c4830cd828255bf18da2d5` (2026-10-15), forecasting-tools 0.3.4 (locked; `>=0.3.0,<0.4.0`),
Python 3.12 on GitHub Actions. Spec: `docs/SPEC.md`. No human edits any individual forecast.

## How it works
A scheduled workflow runs every 20 minutes (`7,27,47 * * * *`).
1. `precheck.py` (standard library only) lists open questions of both tournaments, re-sends pending
   comments, reads the budget (OpenRouter key balance, or the locally tracked Anthropic spend) and decides
   whether there is work. Idle runs stop here.
2. Research pass: one report per question on the cheap model with web search (OpenRouter native search,
   or the Anthropic server-side web search tool).
3. Final forecasts: 1 to 5 forecasts per question (chosen by budget pacing), each followed by a parse step.
4. Guardrails: binary probabilities clamped to `[BINARY_MIN, BINARY_MAX]` (0.03 / 0.97), multiple-choice
   options floored at `MC_MIN_PROB` (0.01), numeric answers with an implausible unit re-asked once and
   then dropped (no fallback distribution is ever submitted). The comment states raw and submitted values.
5. Submission: forecast first, then a private comment. The comment text is stored before the forecast is
   sent and re-sent by the next run if posting failed (gives up after 24 runs).

## Safety switches
- `BOT_ENABLED`: the schedule runs only when this repo variable is set. Value must be exactly `1`.
- `PUBLISH`: forecasts and comments are submitted only when set to exactly `1`; otherwise a dry run
  that lists questions only. Manual runs honour it too.
- Any other value (`0`, `true`, `yes`) fails the run with a message.
- Budget guard: OpenRouter `/key` is read every run (Anthropic: the local spend record, see below); below
  `BUDGET_STOP_BELOW` ($3) no LLM call is made. HTTP 401/402 anywhere in an error chain stops the whole run
  at once; on Anthropic a 402, or a 400/429 whose message is about credit balance or spend limits, is
  treated as 402.
- Per-question failure counter (carried between runs through `actions/cache`, write-ahead): after
  `MAX_QUESTION_FAILED_RUNS` consecutive failed runs a question is skipped, and retried once after
  `SKIP_COOLDOWN_HOURS`. If at least two questions fail in one run, a tiny health probe on the cheap
  model decides whether it was an upstream outage (counts rolled back) or the questions' own fault.
- The job turns red only for systemic problems (bad switch, token rejected, both tournaments unreadable,
  provider 401/402 when LLM work is needed, every attempted question or comment failed).

## LLM provider (v0.2.0)
`LLM_PROVIDER` (`anthropic` or `openrouter`) picks the provider. Unset: `openrouter` when
`OPENROUTER_API_KEY` is set (unchanged behaviour), otherwise `anthropic` when `ANTHROPIC_API_KEY` is set.
- The Anthropic path uses the official `anthropic` Python SDK (not litellm). Final forecasts:
  `claude-opus-5-5`, effort `ANTHROPIC_EFFORT` (default `medium`), server-side refusal fallback
  (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`). Research: `claude-haiku-5-5` with the
  `web_search_20250305` server tool (`max_uses` 5); summary and parsing: `claude-haiku-5-5` at effort `low`.
  No `temperature` / `top_p` / `top_k`, no `thinking` parameter, no forced `tool_choice`, no prefill.
  A `refusal` stop reason is checked before content is read and counts as an ordinary failure.
- The Anthropic API has no balance endpoint. Each response's `usage` is priced locally (input, output,
  cache write 1.25x / read 0.1x input, web search $10 per 1,000) and added to `.state/state.json`
  (same `actions/cache` as the other state; Test Bot restores and saves it too). The record is per credit
  period, which starts on `ANTHROPIC_CREDIT_RESET_DAY` (default 13) each month; the budget per period is
  `ANTHROPIC_BUDGET_USD` (default 90). Pacing spreads the remaining budget until the next reset instead of
  `SEASON_END`. If no saved record is found the summary shows a yellow warning and spend counts from $0.
- Spend outside this bot (Console, other keys in the same org) is not seen; lower `ANTHROPIC_BUDGET_USD`
  to leave room for it.

## Repo variables (Settings -> Secrets and variables -> Actions -> Variables)
Secrets: `METACULUS_TOKEN`, and `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY`. All variables are optional.
| variable | purpose | default |
|---|---|---|
| `BOT_ENABLED` | schedule switch | unset = schedule does not run |
| `PUBLISH` | submit forecasts and comments | unset = dry run |
| `MAX_QUESTION_FAILED_RUNS` | consecutive failed runs before a question is skipped | 3 |
| `SKIP_COOLDOWN_HOURS` | wait before a skipped question gets another try | 6 |
| `DRY_RUN_LLM_MAX_QUESTIONS` | questions used by a manual `dry_run_llm` run | 2 |
| `TEST_MAX_QUESTIONS` | questions used by the Test Bot workflow | 3 |
| `MAIN_FORECAST_MODEL` | final-forecast model, main tournament | `openai/gpt-6.1-sol` |
| `MINIBENCH_FORECAST_MODEL` | final-forecast model, MiniBench | `openai/gpt-6.1-sol` |
| `ASSUMED_BUDGET_USD` | total budget assumed when the key has no spending limit | 100 |
| `BUDGET_STOP_BELOW` | stop all LLM calls below this remaining balance (USD) | 3 |
| `BUDGET_RESERVE` | balance excluded from pacing | `BUDGET_STOP_BELOW` |
| `BUDGET_SPLIT_DATE` | from this date the main tournament gets most of the budget | 2026-11-01 |
| `MAIN_BUDGET_SHARE` | main tournament share after the split date | 0.8 |
| `MAIN_QUESTIONS_PER_DAY` / `MINIBENCH_QUESTIONS_PER_DAY` | new-question estimate for pacing | 4 |
| `SEASON_END` | pacing horizon | 2027-01-01 |
| `BINARY_MIN` / `BINARY_MAX` | binary probability bounds | 0.03 / 0.97 |
| `MC_MIN_PROB` | minimum probability per multiple-choice option | 0.01 |
| `BOT_USERNAME` | bot account name for standings | looked up via `/users/me` |
| `LLM_PROVIDER` | `anthropic` or `openrouter` | by which key is set (OpenRouter first) |
| `ANTHROPIC_BUDGET_USD` | Anthropic budget per credit period (USD) | 90 |
| `ANTHROPIC_CREDIT_RESET_DAY` | day of month the Anthropic credit resets (1-28) | 13 |
| `ANTHROPIC_FORECAST_MODEL` / `ANTHROPIC_RESEARCH_MODEL` | Anthropic model ids | `claude-opus-5-5` / `claude-haiku-5-5` |
| `ANTHROPIC_FORECAST_PRICE` / `ANTHROPIC_RESEARCH_PRICE` | price override, `in/out` USD per million tokens | built-in table |
| `ANTHROPIC_EFFORT` / `ANTHROPIC_RESEARCH_EFFORT` | effort for final forecasts / research | `medium` / `medium` |
| `ANTHROPIC_MAX_TOKENS` | `max_tokens` for forecasts and research (thinking included) | 16000 |
| `ANTHROPIC_FALLBACKS` | `off` disables the server-side refusal fallback | on |
| `ANTHROPIC_WEB_SEARCH_TOOL` / `ANTHROPIC_WEB_SEARCH_MAX_USES` | web search tool type / searches per research call | `web_search_20250305` / 5 |

## Models
- Final forecasts: `openai/gpt-6.1-sol`; fallback `openai/gpt-5.6-terra`, then `openai/gpt-5.4`
  (first one listed by OpenRouter `/models` wins). Research, summary and parsing: `openai/gpt-5.4-mini`.
- Explicit lists only. `-pro` (including `gpt-6.1-sol-pro`), `-image`, `:batch` and `codex` variants are never used.
- Research uses OpenRouter `plugins: [{"id": "web", "engine": "native"}]`.
- Anthropic path: see "LLM provider" above.

## Cost model
OpenRouter prices 2026-10-17, USD per million tokens (input / output): gpt-6.1-sol 2 / 10;
gpt-5.6-terra 2 / 12; gpt-5.4 2.50 / 15; gpt-5.4-mini 0.75 / 4.50; native search about $0.01 per call.
Per question: research (10k in / 3k out on mini) plus search about $0.03, summary about $0.005; each
forecast (4k / 4k on gpt-6.1-sol) plus two mini parse calls about $0.054.

| tier | forecasts | cost per question |
|---|---|---|
| full | 5 | ~$0.30 |
| standard | 3 | ~$0.20 |
| lean | 2 | ~$0.14 |
| minimum | 1 | ~$0.09 |

Pacing: per-question budget = (remaining - `BUDGET_RESERVE`) / estimated remaining questions; the richest
tier that fits is used, and a question is never skipped for budget. From `BUDGET_SPLIT_DATE` MiniBench
always uses the minimum tier. About 240 questions per month is roughly $22-34 per month. Actual spend is in
each run's job summary. Public repos use standard runners for free.

Anthropic (USD per million tokens: Opus 5.5 4 / 20, Haiku 5.5 0.10 / 0.50, web search $0.01 each). Assumed
per question: research 30k in / 3k out plus 3 searches about $0.035, summary under $0.001; each forecast
(4k in / 6k out including thinking at effort `medium`) plus two parse calls about $0.137. These token
counts are unmeasured; the summary shows real spend.

| tier | forecasts | cost per question (Anthropic) |
|---|---|---|
| full | 5 | ~$0.72 |
| standard | 3 | ~$0.45 |
| lean | 2 | ~$0.31 |
| minimum | 1 | ~$0.17 |

## Stop-loss
`standings.py` computes the daily rank among non-`metac-*` bots. After 40 resolved main-tournament
questions, a rank in the bottom half is reported in the job summary as a stop-loss trigger.

## Files
| file | role |
|---|---|
| `precheck.py` | light step: switch validation, open-question listing, pending-comment re-send, work decision, repo warnings |
| `standings.py` | daily standings and stop-loss verdict |
| `budget.py` | key guard, provider selection, model candidate lists, prices, tier costs, credit period, pacing |
| `state.py` | pending comments, failure counters and Anthropic spend carried between runs |
| `main.py` | `ForecastBot2026`: template flow, prompts, final guards, Anthropic SDK client, CLI |
| `runner.py` | fetch, retries, health probe, publish, job summary |
| `guards.py` | binary clamp, multiple-choice floor, unit-error check |
| `tests/` | offline tests |

## Tests
```
poetry install --with dev && poetry run pytest
```
All tests are offline (sockets blocked, fake LLM, fixture questions).
