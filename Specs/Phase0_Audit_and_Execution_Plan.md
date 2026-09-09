# Phase 0 — Repository Audit, Strategy Normalization, Ambiguities, Execution Plan

**Repo:** `Shivam10251/AI_Brain_Correlation_Hedging` · branch `feat/quant-db-dashboard` · commit `637a75d` (2026-09-09, "Add read-only Quant DB Dashboard at /dashboard")
**Inputs:** master prompt (Doc 1), prop-firm rules (Doc 2), code analysis of commit 368355b (Doc 3), and the repository itself.
**Prepared:** 2026-09-09 · **Scope reset 2026-09-09: MT5 is the only platform. Every NT8 / LucidFlex line below is retained for history and marked OUT OF SCOPE. See `MT5_Build_Plan.md` for the active plan.**

---

## 0. What was inspected and what was not

| Inspected | Method |
|---|---|
| `backend/` (5.1k LOC Python, all 22 files) | Read in full |
| `frontend/templates/{index,dashboard}.html` | Skimmed; API surface traced from `main.py` |
| `tests/` | Read; **executed**: `test_restart_recovery.py` 48/48, `test_api.py` 31/31 pass (Linux, fake MT5) |
| `CLAUDE.md`, `requirements.txt`, `run.txt`, `start_bot.bat`, `.gitignore` | Read |
| `.claude/`, `.claude-flow/` (~250 files) | Sampled; identified as claude-flow/ruflo agent tooling |
| `task_pending/README.md` | Read — this is the de facto spec/handoff doc |

| Not inspectable | Why | Impact |
|---|---|---|
| `.env` | Not committed (correct) | Live parameter values unknown; defaults assumed |
| `data/quantbot.db` | Gitignored | **No live trade history available.** Cannot assess whether the strategy has ever produced a closed trade |
| `ruVector.db`, `.swarm/` | Gitignored runtime state of claude-flow tooling | Confirmed **not** a trading database. Exclude from trading architecture |
| `venv/` | Not committed | — |
| `Specs/` | **Does not exist** on this branch | Doc 1's tree is aspirational; nothing to adapt yet |
| Live MT5 / broker behaviour | No terminal | Filling mode, ticket resolution, server timezone all unverified |
| Any NT8 code | **None exists** | NT8 adapter is greenfield, not evolution | *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*

Doc 3 (commit 368355b) is accurate against 637a75d. Delta between the two commits: `backend/database/explorer.py` (673 LOC), `frontend/templates/dashboard.html` (1704 LOC), five `/api/db/*` endpoints. Everything else byte-identical in behaviour.

---

## 1. Architecture map (derived from source)

| Component | Location | Responsibility | Inputs | Outputs | DB tables | Status | Classification |
|---|---|---|---|---|---|---|---|
| Trading loop + risk gate | `core/engine.py` | 30s cycle, per-symbol pipeline, `apply_risk_checks`, execute-and-record | MT5 data, AI decision, config | decisions, trades, events | decisions, trades, events, market_states, equity_snapshots | Working (tested vs fake) | **Retain, refactor** — gate must become a proper Risk Engine |
| Runtime | `core/runtime.py` | `bot_state` cache, MT5 connect, single-writer lock, restore on restart | engine_state | — | engine_state | Working | Retain |
| Reconciler | `core/reconciler.py` | 3 passes: resolve PENDING, settle CLOSED from MT5 history, adopt untracked | MT5 deals/positions | trade updates, experiences | trades, experiences, events | Working (tested vs fake) | **Retain** — this is the best code in the repo; becomes MT5 adapter's sync layer |
| AI brain | `ai/brain.py` | DeepSeek call, JSON validation, safe-HOLD on every failure | market_data, memory, news | decision dict | — | Working | Retain; **this is the strategy** |
| Prompts | `ai/prompts.py` | System + user prompt text | csv candles, features, experiences, news | strings | — | Working | Retain; **must be versioned** (§4 A1) |
| Memory | `ai/memory.py` | Distil closed trade → experience; retrieve ≤5 by recency | trades, market_states | prompt context | experiences | Working | Retain, experimental (unvalidated effect) |
| Data engine | `market/data_engine.py` | Fetch 10×D1 + 24×H1, tick, account; compute 8 features | MT5 | market_data dict | — | Partially working (see §2.2, §2.5) | Retain, fix |
| Execution | `market/execution.py` | Build + send market order, idempotency tag, ticket resolution | signal, config | result dict | — | Partially working (IOC hardcoded) | Retain, fix → MT5 adapter |
| News | `market/news.py` | Provider registry; `NullNewsProvider` returns None | symbol | context dict or None | — | Unused (scaffold only) | **Replace contract** — currently prompt-context only, never a gate (§2.3) |
| Config | `config.py` | All tunables from `.env` | env | module constants | — | Working | Retain; migrate to versioned config (§4 D3) |
| DB layer | `database/{connection,repository,repo_*,_common}.py` | SQLite WAL, per-thread conn, façade | — | — | 7 tables | Working | Retain for MVP |
| Analytics | `database/analytics.py` | P&L, WR, PF, expectancy, DD, streaks, breakdowns; `sample_note` <30 trades | trades | metrics | trades | Working | Retain, extend |
| DB explorer | `database/explorer.py` | Read-only SQL console (`query_only` pragma + keyword blocklist + single-statement) | SQL | rows | all (read) | Working | Retain |
| API | `main.py` | FastAPI, lifespan recovery, 20 endpoints | — | JSON | — | Working | Retain; **add auth** (§2.1) |
| Dashboards | `frontend/templates/index.html`, `dashboard.html` | Live bot UI; DB explorer UI | `/api/*` | — | — | Working | Retain; do not build a third |
| Tests | `tests/` | Fake MT5, custom runner (not pytest) | — | — | — | Working | Retain; migrate to pytest + CI |
| Agent tooling | `.claude/`, `.claude-flow/`, `CLAUDE.md` | claude-flow/ruflo swarm scaffolding | — | — | ruVector.db (gitignored) | Unrelated to trading | **Out of scope** for trading architecture. `CLAUDE.md` is generic ruflo boilerplate (references `npm run build && npm test`, which do not exist here) — needs a project-specific rewrite |
| Handoff doc | `task_pending/README.md` | Blockers, decisions, risks, .env template | — | — | — | Current | Seed for `Specs/decisions/` |
| Entry points | `run.txt`, `start_bot.bat` | `uvicorn backend.main:app`; `.bat` still uses old `main:app` and hardcoded path | — | — | — | `.bat` **broken** (stale module path) | Fix |

---

## 2. Findings not in Doc 3

### 2.1 Unauthenticated control plane on `0.0.0.0:8000` — **P0 before any prop account**
`POST /api/control` starts/stops the engine and sets the interval (minimum 1s) with no auth. `run.txt` binds to all interfaces. On a VPS this means anyone who can reach port 8000 can start the bot or set a 1-second interval. `POST /api/db/query` is read-only but still exposes full trade history. Fix: bind `127.0.0.1` or add a bearer token; both are one-line changes.

### 2.2 MT5 server time is labelled as UTC — **AMBIGUITY, blocks the news engine**
`data_engine.py` does `pd.to_datetime(rates["time"], unit="s", utc=True)`. MT5 returns bar times in the **broker server's** timezone (many brokers run UTC+2/+3 with DST; some UTC+0). If the server offset ≠ 0, every `session` label, every stored `created_at` vs bar time comparison, and any future news-window alignment is off by that offset. Must be measured on the live terminal (compare `mt5.symbol_info_tick().time` to `time.time()`) and stored as a per-broker config value.

### 2.3 `news.py` cannot block a trade even with a real provider
The contract is "return context for the prompt". `apply_risk_checks` never consults news. The ±15-minute rule therefore requires a **risk-gate change**, not a provider plug-in. Doc 1 §13 and task_pending's "just add a provider" are both incomplete.

### 2.4 Money risk is never tracked
`trades.risk_amount` exists in the schema but is never populated (engine passes no value). R-multiples are price-based only. No per-trade, per-day, or per-account currency risk figure exists anywhere. Every prop-firm rule (3% daily, $1K trail) is denominated in money — the system cannot currently evaluate a single one of them.

### 2.5 Stop distance excludes spread
BUY: entry at ask, SL = ask×(1−0.002). The position is marked at bid, so the real distance to stop is 0.20% minus spread. R-multiples are optimistic by spread/stop; on BTC/XAU in thin hours this is material.

### 2.6 `DEVIATION=20` points is instrument-blind
20 points on EURUSDm (5 digits) = 2 pips; on BTCUSDm (2 digits) = $0.20; on XAUUSDm (3 digits) = $0.02. Expect requote rejections (retcode 10004) on BTC/XAU. Should be expressed in bps or ATR fraction.

### 2.7 Adoption pass can create duplicate rows after 1,000 trades
`adopt_untracked_positions` builds `known` from `get_trades(limit=1000)`. Any still-open position whose row is older than the last 1,000 trades will be re-adopted every cycle. At uncapped 30s cadence 1,000 trades is ~1–2 days. Query should be `WHERE closed_at IS NULL` instead.

### 2.8 Per-symbol cap counts DB rows, not MT5 positions
Consistent only because pass 3 (adopt) runs before the gate each cycle. Acceptable for MVP; the Risk Engine should read MT5 positions directly.

### 2.9 Non-reproducible model
`DEEPSEEK_MODEL=deepseek-chat` is a moving alias. Temperature 0.1, not 0. The response `model` field is not stored. Two runs of the same prompt on different days are not comparable, and no prompt hash is stored on the decision row.

### 2.10 Engineering hygiene
`requirements.txt` unpinned (except dotenv) · no CI · custom test runner · `start_bot.bat` stale · no `Specs/` · `CLAUDE.md` misleading.

---

## 3. Strategy reconstruction

### A. Existing rules (as built — source of truth is the code)

| # | Rule | Source |
|---|---|---|
| A1 | **Signal = DeepSeek `deepseek-chat`, temp 0.1, system prompt in `prompts.py:build_system_prompt`, user prompt = 10×D1 CSV + 24×H1 CSV + 10 engine features + ≤5 experiences + news (None)**. The prompt text IS the strategy. | `ai/prompts.py`, `ai/brain.py:get_ai_decision` |
| A2 | Universe: `EURUSDm, GBPUSDm, BTCUSDm, XAUUSDm` (current MT5 demo broker's naming; broker not yet identified) | `config.py:34` |
| A3 | Timeframes seen: D1 (10 bars) + H1 (24 bars), newest bar **partial** | `data_engine.py:fetch_multi_timeframe_data` |
| A4 | Cadence: every `interval` (30s default) + processing time; sequential per symbol | `engine.py:trading_loop` |
| A5 | Entry: market order (`TRADE_ACTION_DEAL`), BUY at ask / SELL at bid, on any BUY/SELL with `status == ok` | `execution.py:execute_trade` |
| A6 | Size: fixed `LOT_SIZE=0.01` | `config.py:58` |
| A7 | SL = ±0.20% of entry, TP = ±0.40% of entry (1:2), broker-side | `execution.py` |
| A8 | Exit: **only** broker SL/TP. No close, modify, trail, BE, time, reversal, or EOD exit | Single `order_send` call site |
| A9 | Gates (order): AI status ok → signal in {BUY,SELL} → MT5 connected → score ≥ `MIN_SCORE_TO_TRADE` (default 0 = off) → open count < `MAX_OPEN_POSITIONS_PER_SYMBOL` (default 0 = off) | `engine.py:apply_risk_checks` |
| A10 | No daily loss, drawdown, exposure, correlation, spread, session, or news gate | absence |
| A11 | Filling `IOC` hardcoded; `DEVIATION=20`; `magic=100001`; comment `AIQ-<12hex>` | `execution.py` |
| A12 | Reconciliation each cycle: PENDING→EXECUTED/FAILED via comment tag; EXECUTED→CLOSED via deal history; untracked magic positions adopted | `core/reconciler.py` |
| A13 | Memory: closed trade → experience row → ≤5 most recent by progressively looser filter → injected as text | `ai/memory.py` |
| A14 | Restart does not resume trading (`RESUME_ENGINE_ON_STARTUP=false`) | `config.py` |
| A15 | Features (deterministic, prompt context only, never gate): ATR14(H1), atr_pct, volatility_bucket (P25/P75 of 24 TRs), daily/h1 trend (drift vs 20% of range), momentum (3v3 closes / ATR%), structure (half-window HH/LL), regime (efficiency ratio, **H1 only**), session (UTC hour of last bar), spread | `data_engine.py:compute_features` |

### B. Proposed improvements (not applied — each is a new strategy version)
| # | Change | Rationale |
|---|---|---|
| B1 | `MAX_OPEN_POSITIONS_PER_SYMBOL=1`, `MIN_SCORE_TO_TRADE=70` | Already built and tested; converts stacking into one-position-per-symbol |
| B2 | Drop partial bar: fetch `n+1` from pos 0 and discard index 0, or fetch from pos 1 | Removes intra-bar contamination of all labels |
| B3 | Decide once per new H1 bar, not every 30s | Data changes hourly; 120 re-decisions per bar is model noise + 11.5k calls/day |
| B4 | ATR-scaled SL/TP (e.g. SL = 1.0×ATR14, TP = 2.0×ATR14) instead of fixed % | Same R:R, instrument-consistent stop placement |
| B5 | Close on reversal (AI flips BUY→SELL with score ≥ threshold) instead of opening opposite position | Eliminates accidental hedging (prohibited on both prop firms) |
| B6 | Spread gate: skip if `spread_pct` > X% of stop distance | Cost drag control; measured already |
| B7 | Fix regime to use both frames as the signature implies | Label currently misdescribed |
| B8 | Risk-based sizing: lots = f(account, risk_pct, stop_distance) | Required for any prop account |

### C. Research-backed recommendations (industry practice / established)
| # | Recommendation | Evidence class |
|---|---|---|
| C1 | Do not trade before the score→outcome relationship is measured (calibration curve on ≥200 closed trades, bucketed by score) | Established (probability calibration; Brier score) |
| C2 | Treat DeepSeek output as a **feature**, not a strategy; wrap it in mechanical gates | Industry practice for any discretionary/ML signal |
| C3 | Aggregate exposure by USD-beta: EURUSD/GBPUSD (ρ≈0.85–0.95), XAU and BTC as risk/USD proxies — one net-dollar limit, not four independent caps | Established (portfolio construction, correlated positions) |
| C4 | Daily-loss and drawdown enforcement must live in a single Risk Engine that the adapter cannot bypass | Industry practice (kill-switch authority) |
| C5 | Bar-based backtest is sufficient for this strategy's sensitivity (H1 decisions, % stops); tick-level only for spread/slippage calibration | Engineering recommendation |
| C6 | Walk-forward + Monte Carlo on trade sequences before any parameter change is adopted | Established (Pardo; White's Reality Check for multiple testing) |

### D. Engineering requirements
| # | Requirement |
|---|---|
| D1 | Store `prompt_hash` (sha256 of system+user prompt template), `model` from API response, `temperature` on every decision row |
| D2 | Populate `risk_amount` (account currency) at intent time; add `account_id`, `strategy_version`, `rule_version` columns |
| D3 | Versioned config: `strategy_versions` table (prompt hash, params JSON, created_at, parent_version); every decision/trade/experiment references one |
| D4 | Risk Engine module with authority over: per-trade risk, daily loss, trailing DD, max positions, net exposure, news blackout, session/flatten windows — evaluated **before** `execute_trade`, result persisted as `override_reason` (pattern already exists) |
| D5 | Broker/account profile: server UTC offset, filling mode (from `symbol_info.filling_mode`), digits/point per symbol, hedging vs netting, prop-firm rule set |
| D6 | Bind API to localhost or add bearer token |
| D7 | `pytest` + pinned `requirements.txt` + GitHub Actions running both suites |
| D8 | Fix `adopt_untracked_positions` known-set query |
| D9 | `Specs/` seeded from `task_pending/README.md` + this document; original PDFs immutable under `Specs/strategy/source/` |

### E. Experimental hypotheses (must be tested, not assumed)
| # | Hypothesis | Test |
|---|---|---|
| E1 | AI score is monotonic with win rate | Calibration curve on ≥200 closed demo trades; reject if bucket 80–100 WR ≤ bucket 40–59 WR |
| E2 | Once-per-bar decisions perform ≥ 30s decisions net of cost | A/B on demo, same prompt version |
| E3 | ATR-scaled stops improve expectancy vs fixed 0.20% | Replay stored decisions with both stop rules (requires candle storage — D-phase) |
| E4 | Experience injection improves outcomes | Demo with `MEMORY_ENABLED` on vs off, same period, interleaved |
| E5 | ±15m news blackout vs ±30m high / ±15m medium vs event-specific | Out-of-sample after news feed exists |
| E6 | Net-USD exposure cap reduces drawdown without proportionally reducing return | Backtest on stored decisions |

### F. Open decisions → see §4

---

## 4. AMBIGUITY / NEEDS DECISION

Each item: what is ambiguous → decision required → my default if you say nothing.

1. **Strategy identity and versioning.** The strategy is a prompt + model alias + temperature. There is no version. → Decide: freeze current prompt as `v1.0.0` (hash it) before any change. *Default: yes, today.*
2. **Which account first.** ~~Open~~ **DECIDED 2026-09-09: MT5 demo account (broker TBD) for LLM calibration; prop accounts only after E1 passes.** The current bot cannot survive either prop account (§2.4, §6).
3. **Symbol universe per venue.** Demo-broker `…m` names ≠ The5ers names; LucidFlex is futures (MNQ) — a different asset with different microstructure. The Doc 3 strategy has never seen NQ. **DECIDED 2026-09-09: LucidFlex runs BOTH the existing mechanical NinjaScript strategies (ORB break-retest, PDH/PDL sweep reversal) and an LLM-on-MNQ strategy, as separate versioned strategies with separate validation.** *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*
4. **News rule.** Doc 1 says ±15m is "current"; code has none; The5ers is ±2m execution-only; LucidFlex none. → Decide per-account policy. *Default: `news_blackout_minutes` in account profile; The5ers = 15/15 (superset of 2/2, entries only, existing positions held); LucidFlex = 0/0; MT5 demo = 15/15 to match the intended rule.* *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*
5. **Broker server UTC offset** (§2.2). → Measure on live terminal. *Default: unknown → block news engine until measured.*
6. **Decision cadence.** 30s vs once-per-H1-bar. → *Default: once per new H1 bar (B3), 30s retained as an explicit experiment arm.*
7. **Gate values.** cap and score threshold. → *Default: cap=1, score≥70, then sweep 60/70/80 as experiment.*
8. **Position sizing model.** Fixed lot vs % risk. → *Default: risk % of balance with per-account caps: The5ers 0.5% ($500) per trade, LucidFlex $75 per trade (see §6).* *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*
9. **Reversal handling.** Open opposite (current) vs close vs ignore. → *Default: close existing if AI flips with score ≥ threshold; never hold both directions.*
10. **Filling mode.** → *Default: read `symbol_info.filling_mode`, prefer FOK→IOC→RETURN by broker support.*
11. **Account type.** Hedging vs netting on the MT5 demo broker (and on The5ers) changes position semantics and reconciler behaviour. → Confirm `mt5.account_info().margin_mode`. *Default: enforce one-direction-per-symbol in Risk Engine regardless.*
12. **Consistency rules need day-level P&L.** "Best day ≤ 50% of total" requires a daily P&L ledger per account, timezone-aligned to the firm's trading day. Which timezone defines a "day" for each firm? → Verify on firm pages. *Default: The5ers server day; LucidFlex 6:00 PM ET rollover.* *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*
13. **Model pinning.** `deepseek-chat` alias vs a dated snapshot. → *Default: log response `model`; pin when DeepSeek exposes a dated ID.*
14. **Risk authority.** Python engine vs an MQL5 EA on the terminal. → *Default: Python engine is authoritative; MT5 side is dumb executor with its own hard ceiling (max lots, max positions) as belt-and-braces.*
15. **LucidFlex platform/data path.** **DECIDED 2026-09-09: NinjaTrader 8.** Data/order connection (Rithmic/Tradovate/other) still to confirm. *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*

---

## 5. Critical weaknesses (ranked)

| # | Problem | Why it matters | Evidence | Solution | Validation test |
|---|---|---|---|---|---|
| 1 | No validated edge; strategy = LLM opinion on 34 bars | Everything downstream is worthless if E1 fails | Zero backtests, zero live history in repo | Calibration study on demo before any build-out | E1 |
| 2 | Uncapped stacking + zero portfolio risk | Guaranteed breach of 3% daily / $1K trail | `config.py` defaults; §14 Doc 3 | Risk Engine (D4) with cap, daily loss, net exposure | Tests that attempt to breach each limit; must be refused |
| 3 | Money risk untracked | Cannot evaluate a single prop rule | `risk_amount` never set | D2 | Every trade row has `risk_amount` > 0 |
| 4 | Sizing mismatched to both accounts | 0.01 lot can't reach targets; MNQ math unrelated | §6 | B8 | Sizing unit tests per venue | *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*
| 5 | Accidental hedging on reversal | Prohibited on both firms | Single open-only code path | B5 | Test: BUY then SELL signal → one net position |
| 6 | Cadence ≫ data rate | Noise trades; API cost; "microscalping" optics | 120 decisions/bar | B3 | E2 |
| 7 | Partial bar in every feature | Look-ahead-ish contamination; labels unstable within the hour | `copy_rates_from_pos(…,0,n)` | B2 | Label stability test across a bar |
| 8 | Timezone unknown | Sessions wrong; news alignment impossible | §2.2 | D5 | Offset measured and asserted at startup |
| 9 | Unauthenticated control API | Remote start / 1s interval | §2.1 | D6 | Unauthenticated POST returns 401 |
| 10 | Non-reproducible model + no prompt version | Cannot compare experiments | §2.9 | D1, D3 | Decision rows carry hash + model |
| 11 | Filling mode / deviation instrument-blind | Silent 100% rejection on some symbols | §2.6, Doc 3 §7 | D5 | Demo order on every symbol accepted |
| 12 | Four correlated bets | 4× intended risk on one dollar move | Doc 3 §12 | C3 | Net-USD exposure test |

---

## 6. Prop-rule → Risk Engine requirements matrix

| Rule | The5ers $100K 2-Step | LucidFlex $25K | Engine check (before every order) | Data needed | Test | *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*
|---|---|---|---|---|---|
| Max loss | $10,000 below initial balance | $1,000 EOD-trailing from highest qualifying EOD balance; locks at $25,100 once trail balance reaches $26,100 | `equity − floor ≥ risk_amount + buffer` | account equity, floor, EOD ledger | Order at floor+buffer−1 refused |
| Daily loss | 3% of max(EOD equity, balance) — hard breach | Optional DLL (soft breach, locks to next session) | `today_realized + floating − risk_amount ≥ −daily_limit + buffer` | daily ledger in firm's day TZ | Boundary test at exactly limit |
| Consistency | Funded: best day ≤ 50% of total profit | Eval: best day ≤ 50% of total (≈$625 on $1,250) | Advisory cap on daily profit target; block new entries once day P&L > cap | daily ledger | Day at cap → entries refused |
| News | No order execution ±2 min high-impact | None | `now ∉ [event − pre, event + post]` for events affecting symbol's currencies | news feed + offset | Exactly −15m, 0, +15m boundary tests |
| Overnight / flatten | Per Summer plan (verify) | Flat by 4:45 PM ET; reopen 6:00 PM ET Sun–Thu | Flatten job at T−5 min; no entries in last 30 min | ET clock | Position at 4:44 PM ET → closed |
| Position size | Leverage 1:100 | 2 minis / 20 micros | `contracts ≤ max` | contract spec | Order for 21 micros refused |
| Hedging | Prohibited/restricted | Prohibited | One net direction per symbol | open positions | BUY+SELL same symbol refused |
| Microscalping / HFT | Prohibited | Prohibited | Min holding time / min decision interval config | — | Decision at <1 bar refused |
| Martingale / add-to-loser | Restricted | Prohibited | No size increase after loss; no adding to red position | trade history | Test |
| EA source ownership | Required | — | N/A (you own it) | — | — |

**Sizing arithmetic (defaults for decision 8):**
- The5ers: risk 0.5% = $500/trade. EURUSD 22-pip stop → $500 ÷ (22 × $10/pip/lot) ≈ **2.2 lots**. Six consecutive full losses = $3,000 = the daily limit. Use 0.25–0.5% and a **max 3 losses/day** stop.
- LucidFlex: 1 MNQ = $2/pt. 0.20% of NQ ≈ 48 pts ≈ $96 risk — ~10% of the $1K trail per trade. Default **$75/trade** (≈37 pts on 1 micro, or 2 micros at ~19 pts) and **max 3 losses/day**, which keeps a 4-loss day under 30% of the trail. *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*

---

## 7. Execution plan

### FIRST 10 THINGS TO BUILD — exact order

| # | Item | Depends on | Acceptance criteria |
|---|---|---|---|
| 1 | **Freeze strategy v1.0.0**: `prompt_hash`, response `model`, `temperature`, `strategy_version` on `decisions` and `trades`; `strategy_versions` table | — | Every new decision row carries all four; migration is additive |
| 2 | **Security + hygiene**: bind localhost / bearer token; pin requirements; pytest + CI; fix `start_bot.bat`; fix §2.7 query; project-specific `CLAUDE.md` | — | CI green on push; unauthenticated `/api/control` → 401 |
| 3 | **Broker profile + timezone assertion**: measure server offset at startup, store; read `filling_mode` per symbol; per-symbol deviation in bps | — | Startup event logs offset; order accepted on all 4 symbols on demo |
| 4 | **Money-risk plumbing**: compute `risk_amount` at intent (tick value × stop distance × volume); daily P&L ledger table keyed by `(account_id, trading_day)` | 1 | Sum of ledger = MT5 realized P&L for the day (reconciled) |
| 5 | **Risk Engine v1** (replaces `apply_risk_checks`): per-symbol cap, score floor, per-trade risk cap, daily loss cap, max losses/day, one-direction-per-symbol, spread gate. Authority: engine only | 4 | Test suite attempts every violation; all refused with `override_reason`; existing 79 tests still pass |
| 6 | **Feature fixes**: drop partial bar; regime uses both frames; decide once per new H1 bar (30s kept as config for experiment arm) | 3 | Label stability test; decisions/day drops from ~11.5k to ~100 |
| 7 | **Sizing v1**: `lots = risk_amount / (stop_pts × tick_value)`, capped by broker/firm limits; ATR-scaled stop as a *versioned* alternative (B4) | 4, 5 | Unit tests per instrument; v1.0.0 keeps fixed-% stop, v1.1.0 = ATR stop |
| 8 | **Exit manager v1**: close-on-reversal; EOD/session flatten job; both through the same execution path with idempotency | 5 | BUY→SELL flip yields one net position; flatten test at boundary |
| 9 | **Demo calibration run** (MT5 demo, v1.0.0 + gates on): collect ≥200 closed trades; `breakdown/ai_score` + calibration curve + Brier score; also E2 and E4 arms | 1–8 | **Go/no-go gate**: proceed only if score is monotonic with WR and expectancy > 0 after costs in at least one bucket |
| 10 | **Candle + decision store for replay**: persist the exact 34 bars each decision saw (parquet or SQLite blob) so E3/E6 can be replayed offline without new API calls | 1 | Replaying a stored decision reproduces the stored features byte-for-byte |

### THINGS NOT TO BUILD YET
| Item | Reason |
|---|---|
| NT8 adapter (before step 9) | Steps 1–8 add `account_id`/`platform`; adapter is step 11. Shape committed: NinjaScript owns execution + hard Lucid rules (20-micro cap, 4:45 PM ET flatten, no-hedge, local trailing-DD floor); Python is ledger/risk-state/analytics; NT8 posts order/execution/position events to local FastAPI with idempotent IDs; kill switch is pull (NinjaScript polls `/api/risk/state`, fails safe to local limits); NT8 account is truth for its own reconciliation; Lucid day keyed on ET | *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*
| News engine with live feed | Blocked on timezone (step 3) and on the rule being a gate (step 5); until then it's prompt decoration. Build as step 11 with fail-closed behaviour |
| PostgreSQL / TimescaleDB / Redis / Kafka | SQLite WAL handles ~100 decisions/day trivially; nothing here is multi-process |
| Relationship discovery, feature store, ML filter | Need ≥500 closed trades under a frozen version first; otherwise it's noise-mining |
| Correlation hedging (the repo's name) | Downstream of net-exposure limit (step 5) and of the edge existing at all |
| Third dashboard | Two exist; extend `dashboard.html` |
| Experience-memory tuning | E4 first; if no measurable effect, leave it off |

### KEY ARCHITECTURAL DECISIONS
| Decision | Commit now | Commit after validation |
|---|---|---|
| Risk Engine is the sole authority; adapters are dumb executors | ✔ | |
| Strategy = versioned (prompt, model, params); every row references a version | ✔ | |
| SQLite stays for MVP; migration only at multi-process or >10M rows | ✔ | |
| Reconciler pattern (intent-before-send, MT5 as truth) becomes the MT5 adapter contract | ✔ | |
| Decision cadence = per bar | ✔ (as default; 30s as experiment arm) | |
| ATR-based stops replace fixed % | | after E3 |
| Once-per-bar vs 30s | | after E2 |
| NT8 adapter shape: NinjaScript-owned execution, local HTTP events to Python, pull-based kill switch | ✔ | | *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*
| Any ML layer | | after ≥500 trades under frozen version |

### KEY RESEARCH EXPERIMENTS (in order)
E1 calibration → E2 cadence → E4 memory on/off → E3 stop rule (offline replay) → E6 net-exposure → E5 news windows.

### KEY OPEN QUESTIONS
Decisions 2, 3, 5, 11, 12, 15 in §4 — the rest have workable defaults.

---

## 8. The brutally practical answer

**Build first:** steps 1–8 (roughly one to two weeks of focused work; all are edits to existing modules, no rewrites).
**Research first:** E1 — does the AI score predict anything? Nothing else matters until that is answered with ≥200 demo trades under a frozen prompt hash.
**Postpone:** NT8 adapter until step 11 (shape is decided), news feed, any new database, relationship discovery, ML, and the correlation-hedging logic the repo is named after. *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*
**Commit only after validation:** ATR stops, cadence, NT8 adapter shape, memory injection, any prop-account deployment. *(OUT OF SCOPE — NT8/LucidFlex, 2026-09-09)*

If E1 fails, the honest outcome is: keep the infrastructure (it is good), replace the signal with a mechanical rule set that *can* be backtested, and re-run this plan from step 9.
