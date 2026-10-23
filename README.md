# forecast-bot

v0.3.0. A low-cost, fully automated Metaculus bot for the Fall 2026 FutureEval tournament and MiniBench.
Based on [Metaculus/metac-bot-template](https://github.com/Metaculus/metac-bot-template) at
`096cb207d643da3090c4830cd828255bf18da2d5` (2026-10-15), forecasting-tools 0.3.4 (locked; `>=0.3.0,<0.4.0`),
Python 3.12 on GitHub Actions. Spec: `docs/SPEC.md`. No human edits any individual forecast.

## How it works
A scheduled workflow runs every 20 minutes (`7,27,47 * * * *`).
1. `precheck.py` (standard library only) lists open questions of both tournaments, re-sends pending
   comments, reads the budget (OpenRouter key balance, or the locally tracked Anthropic / OpenAI usage) and
   decides whether there is work. Idle runs stop here.
2. Research pass: one report per question on the cheap model with web search (OpenRouter native search,
   the Anthropic server-side web search tool, or the OpenAI `web_search` tool).
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
  treated as 402; on OpenAI the quota and spend-limit codes (`insufficient_quota`,
  `organization_spend_limit_exceeded`, `project_spend_limit_exceeded`, `credit_balance_exhausted`,
  `organization_usage_limit_exceeded`) are treated as 402.
- Per-question failure counter (carried between runs through `actions/cache`, write-ahead): after
  `MAX_QUESTION_FAILED_RUNS` consecutive failed runs a question is skipped, and retried once after
  `SKIP_COOLDOWN_HOURS`. If at least two questions fail in one run, a tiny health probe on the cheap
  model decides whether it was an upstream outage (counts rolled back) or the questions' own fault.
- The job turns red only for systemic problems (bad switch, token rejected, both tournaments unreadable,
  provider 401/402 when LLM work is needed, every attempted question or comment failed).

## LLM provider (v0.2.0, v0.3.0)
`LLM_PROVIDER` (`openrouter`, `anthropic` or `openai`) picks the provider. Unset: the first key that is set
wins, in the order `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`.
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

### OpenAI (v0.3.0)
Designed around OpenAI's data-sharing free daily tokens (250K tokens/day for the "big" group: gpt-5.4,
gpt-5.2, gpt-5.1, gpt-5, gpt-4.1, gpt-4o, o1, o3; 2.5M/day for the "mini" group: gpt-5.4-mini, gpt-5.4-nano,
gpt-5-mini, gpt-5-nano, gpt-4.1-mini, gpt-4.1-nano, gpt-4o-mini, o3-mini, o4-mini). Requires data sharing
to be enabled on the organization.
- Official `openai` SDK, Responses API. Final forecasts: `gpt-5.4`, `reasoning.effort` `OPENAI_EFFORT`
  (default `medium`; the model's own default is `none`, so it is always sent). Research: `gpt-5.4-mini`
  with the `web_search` tool (`search_context_size` `low`, `max_tool_calls` 3), effort `low`; summary and
  parsing: `gpt-5.4-mini`, effort `low`. No `temperature` / `top_p`. Defaults are free-list models only.
- Local ledger in `.state/state.json` (`openai_usage`): tokens per UTC day per group, and USD per calendar
  month for everything not free: requests that use a tool (assumption: tool use is excluded from the free
  tokens, so the research call is billed in full; `OPENAI_TOOL_TOKENS_FREE=1` changes that), web search
  calls ($10 per 1,000), models outside the free lists, and any request that would take its group past the
  daily allowance (charged in full, following the help-article example). Assumption: the allowance resets
  at 00:00 UTC.
- Free-token pacing: each run takes the richest tier whose projected big-group tokens (this run plus the
  questions expected later today, from `*_QUESTIONS_PER_DAY`) fit today's allowance minus
  `OPENAI_FREE_MARGIN` (10%). If not even the minimum tier fits, the questions that still fit run free,
  questions closing within `OPENAI_DEFER_HOURS` (30) run at the minimum tier and are paid, and the rest are
  deferred to a later run (they are not counted as failures; Test Bot never defers). Precheck applies the
  same plan, so a run whose questions would all be deferred ends at the precheck step.
- Dollar budget: `OPENAI_BUDGET_USD` (default 4.5) per calendar month; below `OPENAI_BUDGET_STOP_BELOW`
  ($0.25) no LLM call is made. Keep it under the account's hard monthly limit. The ledger does not see
  other traffic on the same organization.

## Repo variables (Settings -> Secrets and variables -> Actions -> Variables)
Secrets: `METACULUS_TOKEN`, and one of `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`. All variables are optional.
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
| `LLM_PROVIDER` | `openrouter`, `anthropic` or `openai` | by which key is set (OpenRouter, Anthropic, OpenAI) |
| `ANTHROPIC_BUDGET_USD` | Anthropic budget per credit period (USD) | 90 |
| `ANTHROPIC_CREDIT_RESET_DAY` | day of month the Anthropic credit resets (1-28) | 13 |
| `ANTHROPIC_FORECAST_MODEL` / `ANTHROPIC_RESEARCH_MODEL` | Anthropic model ids | `claude-opus-5-5` / `claude-haiku-5-5` |
| `ANTHROPIC_FORECAST_PRICE` / `ANTHROPIC_RESEARCH_PRICE` | price override, `in/out` USD per million tokens | built-in table |
| `ANTHROPIC_EFFORT` / `ANTHROPIC_RESEARCH_EFFORT` | effort for final forecasts / research | `medium` / `medium` |
| `ANTHROPIC_MAX_TOKENS` | `max_tokens` for forecasts and research (thinking included) | 16000 |
| `ANTHROPIC_FALLBACKS` | `off` disables the server-side refusal fallback | on |
| `ANTHROPIC_WEB_SEARCH_TOOL` / `ANTHROPIC_WEB_SEARCH_MAX_USES` | web search tool type / searches per research call | `web_search_20250305` / 5 |
| `OPENAI_BUDGET_USD` / `OPENAI_BUDGET_STOP_BELOW` | OpenAI paid budget per calendar month / stop line (USD) | 4.5 / 0.25 |
| `OPENAI_FREE_DAILY_BIG` / `OPENAI_FREE_DAILY_MINI` | free tokens per UTC day per group | 250000 / 2500000 |
| `OPENAI_FREE_MARGIN` | share of the daily allowance pacing leaves unused | 0.1 |
| `OPENAI_DEFER_HOURS` | with free tokens gone, questions closing later than this wait | 30 |
| `OPENAI_FORECAST_MODEL` / `OPENAI_RESEARCH_MODEL` / `OPENAI_PARSE_MODEL` | OpenAI model ids | `gpt-5.4` / `gpt-5.4-mini` / `gpt-5.4-mini` |
| `OPENAI_FORECAST_PRICE` / `OPENAI_RESEARCH_PRICE` / `OPENAI_PARSE_PRICE` | price override, `in/out` USD per million tokens | built-in table |
| `OPENAI_EFFORT` / `OPENAI_RESEARCH_EFFORT` | `reasoning.effort` for forecasts / research | `medium` / `low` |
| `OPENAI_MAX_TOKENS` | `max_output_tokens` for forecasts (reasoning included) | 16000 |
| `OPENAI_WEB_SEARCH_MAX_CALLS` / `OPENAI_SEARCH_CONTEXT` | `max_tool_calls` / `search_context_size` for research | 3 / `low` |
| `OPENAI_WEB_SEARCH_COST` | USD per web search call | 0.01 |
| `OPENAI_TOOL_TOKENS_FREE` | `1` = tokens of tool-using requests count as free | unset |

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

OpenAI (developers.openai.com pricing, 2026-10-24, USD per million tokens input / cached / output):
gpt-5.4 2.50 / 0.25 / 15; gpt-5.4-mini 0.75 / 0.075 / 4.50; web search $10 per 1,000 calls, search content
billed as input tokens. Assumed per forecast: 6k in / 5k out on gpt-5.4 (11k free big-group tokens) plus two
parse calls (6.2k free mini-group tokens). Research (paid): 15k in / 2.5k out plus 2.5 searches, about $0.05
per question. With a 10% margin, 225K big-group tokens per day cover:

| tier | forecasts | big-group tokens per question | questions per day within the free tokens |
|---|---|---|---|
| full | 5 | 55k | 4 |
| standard | 3 | 33k | 6 |
| lean | 2 | 22k | 10 |
| minimum | 1 | 11k | 20 |

A minimum-tier question past the free allowance costs about $0.09 more. These token counts are unmeasured.

## Stop-loss
`standings.py` computes the daily rank among non-`metac-*` bots. After 40 resolved main-tournament
questions, a rank in the bottom half is reported in the job summary as a stop-loss trigger.

## Files
| file | role |
|---|---|
| `precheck.py` | light step: switch validation, open-question listing, pending-comment re-send, work decision, repo warnings |
| `standings.py` | daily standings and stop-loss verdict |
| `budget.py` | key guard, provider selection, model candidate lists, prices, tier costs, credit period, pacing, OpenAI free-token plan |
| `state.py` | pending comments, failure counters, Anthropic spend and OpenAI usage carried between runs |
| `main.py` | `ForecastBot2026`: template flow, prompts, final guards, Anthropic and OpenAI SDK clients, CLI |
| `runner.py` | fetch, retries, health probe, publish, job summary |
| `guards.py` | binary clamp, multiple-choice floor, unit-error check |
| `tests/` | offline tests |

## Tests
```
poetry install --with dev && poetry run pytest
```
All tests are offline (sockets blocked, fake LLM, fixture questions).
