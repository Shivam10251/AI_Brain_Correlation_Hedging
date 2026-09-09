# MT5 Build Plan — phase by phase

**Scope:** MetaTrader 5 only. Platforms, accounts, and instruments outside MT5 are OUT OF SCOPE.
**Repo:** `AI_Brain_Correlation_Hedging` @ `637a75d`, branch `feat/quant-db-dashboard`.
**Codebase check (2026-09-09):** zero references to any non-MT5 platform or to any specific broker in `backend/`, `frontend/`, `tests/`. Nothing to remove. The `…m` symbol suffix is just the demo broker's naming; broker identity is still TBD and is captured in Phase 1 as data, not assumed.
**Prop target:** The5ers $100K Summer 2-Step (MT5). The other account in Doc 2 is not MT5 → OUT OF SCOPE.
**Accounts in play:** `mt5-demo` (broker TBD, calibration) → `the5ers-100k` (evaluation, later).

Phase 0 (audit, strategy reconstruction, ambiguities) is complete — see `Phase0_Audit_and_Execution_Plan.md`. Everything below assumes its findings.

Ordering rule: no phase starts until the previous phase's acceptance criteria pass in CI and on the demo terminal. Phases 1–5 are edits to existing modules; nothing is rewritten.

---

## Phase 1 — Foundation, versioning, broker profile, safety

**Goal:** make every decision reproducible, make the process safe to expose, and capture the real broker facts the code currently assumes.

| Deliverable | Where | Detail |
|---|---|---|
| `strategy_versions` table | `schema.sql` (additive) | `id, strategy_id, version, prompt_hash, model_alias, temperature, params_json, parent_version_id, created_at`. Seed `llm-mtf` v1.0.0 from the current `prompts.py` + `config.py` |
| Decision provenance | `ai/brain.py`, `repo_trading.py` | Store `prompt_hash` (sha256 of rendered system prompt + user-prompt template), `model` from API response body, `temperature`, `strategy_version_id` on every `decisions` row |
| Broker profile | new `backend/market/broker_profile.py`, `broker_profiles` table | At startup, per account: `login`, `server`, `company`, `currency`, `leverage`, `margin_mode` (`mt5.account_info().margin_mode`: 0 netting / 2 hedging), `server_utc_offset_min` measured as `round((symbol_info_tick(s).time − time.time())/1800)*30`, asserted stable across 3 samples; per symbol: `digits, point, trade_tick_size, trade_tick_value, trade_tick_value_profit/loss, volume_min, volume_step, volume_max, filling_mode` bitmask, `trade_stops_level`, `trade_freeze_level`, `spread`, `trade_mode`, `swap_long/short` |
| Filling mode | `execution.py` | Choose from `symbol_info.filling_mode` bits: FOK if set, else IOC, else RETURN. Never hardcode |
| Deviation | `config.py`, `execution.py` | `DEVIATION_BPS` (default 5 bps) → points = `price × bps/1e4 / point`, clamped to `[trade_stops_level, 10×]` |
| Timestamps | `data_engine.py` | Bar times converted using `server_utc_offset_min`; store both `bar_time_server` and `bar_time_utc`; `session` computed from UTC |
| Auth | `main.py` | Bearer token from `.env` (`API_TOKEN`) on every `/api/*` route except `/api/health`; default bind `127.0.0.1` in `run.txt` |
| Hygiene | repo | Pin `requirements.txt`; `pytest` with both suites; GitHub Actions workflow; fix `start_bot.bat` (`backend.main:app`, no hardcoded path); fix `adopt_untracked_positions` known-set (`WHERE closed_at IS NULL`); project-specific `CLAUDE.md`; `Specs/` created with `strategy/source/` (immutable PDFs), `decisions/` (seeded from `task_pending/README.md` + Phase 0), `architecture/`, `risk/`, `news/`, `data/` |

**Acceptance:** CI green · unauthenticated `POST /api/control` → 401 · startup event logs `server_utc_offset_min`, `margin_mode`, and per-symbol filling mode · a demo market order is accepted on every symbol in `SYMBOLS` · every new `decisions` row has `prompt_hash`, `model`, `strategy_version_id` non-null · existing 79 tests still pass.

---

## Phase 2 — Money risk and the daily ledger

**Goal:** every trade has a currency risk figure before it is sent; every account has a trading-day P&L that reconciles to MT5.

| Deliverable | Detail |
|---|---|
| `risk_amount` at intent | `risk_amount = |entry − sl| / trade_tick_size × trade_tick_value × volume`, converted to account currency (MT5 `trade_tick_value` is already in account currency). Stored on `trades` before `order_send` |
| Stop distance net of spread | Effective risk for BUY = `(ask − sl) + spread`… i.e. measure from the marking price (bid for longs, ask for shorts). `r_multiple` uses the same distance |
| `daily_ledger` table | `(account_id, trading_day, realized_pnl, commission, swap, max_floating_loss, max_equity, min_equity, eod_equity, eod_balance, trade_count, loss_count, win_count)`. `trading_day` = server day shifted by `server_utc_offset_min` (The5ers computes its day on server time) |
| Ledger reconciliation | New reconciler pass 4: rebuild today's ledger row from `history_deals_get(day_start, now)` filtered by magic; drift > 0.01 raises a `RECONCILE` WARN event |
| `account_id` everywhere | `decisions, trades, equity_snapshots, market_states, events` gain `account_id` (from `account_info().login`) |
| Floating loss tracking | Equity snapshot cadence unchanged; ledger `max_floating_loss` updated every cycle from `account_info().profit` |

**Acceptance:** after a demo day with ≥5 closed trades, `daily_ledger.realized_pnl` equals the sum of MT5 deal profit+commission+swap for that day to the cent · every `trades` row has `risk_amount > 0` · unit tests for `risk_amount` on a 5-digit FX symbol, a 2-digit crypto symbol, and a 3-digit metal symbol using stored `broker_profiles` values.

---

## Phase 3 — Risk Engine v1

**Goal:** replace `apply_risk_checks` with a module that has sole authority over whether an order may be sent, driven by a per-account risk profile.

| Deliverable | Detail |
|---|---|
| `backend/risk/engine.py` | `evaluate(account, symbol, signal, decision, sizing) → (allow: bool, reason: str, checks: list)`. Ordered checks, first failure wins, all checks logged: (1) AI status ok · (2) MT5 connected · (3) kill switch off · (4) score ≥ threshold · (5) session/flatten window open · (6) news blackout clear (Phase 7 plugs in; until then returns `clear`) · (7) spread ≤ `max_spread_pct_of_stop` · (8) no opposite-direction open position on symbol (hedging ban, regardless of `margin_mode`) · (9) open positions on symbol < cap · (10) per-trade `risk_amount` ≤ `max_risk_per_trade` · (11) `today.realized + floating − risk_amount ≥ −daily_loss_limit + buffer` · (12) `loss_count_today < max_losses_per_day` · (13) `equity − risk_amount ≥ max_loss_floor + buffer` · (14) net USD-beta exposure after this trade ≤ `max_net_exposure_r` (EURUSD, GBPUSD long = short USD; XAU, BTC treated as short-USD proxies, configurable weights) |
| `risk_profiles` table + YAML in `Specs/risk/` | Profiles: `mt5-demo` (permissive, for calibration: cap 1, score 70, risk 0.25%, daily 2%, 3 losses/day) and `the5ers-100k` (cap 1, score 70, risk 0.5% max, daily loss 3% of max(EOD equity, balance) with 0.5% buffer, floor $90,000 + $500 buffer, 3 losses/day, news ±15/15, no hedging, min holding 1 bar) |
| Kill switch | `engine_state.kill_switch` + `POST /api/risk/halt` (authed). When set: no new orders; optional `flatten=true` closes all magic positions via the Phase 5 close path |
| Override persistence | Unchanged pattern: `final_decision`, `override_reason`, WARN event; plus `risk_checks_json` on the decision row |
| Tests | `tests/test_risk_engine.py`: one test per check that constructs the violating state and asserts refusal; boundary tests at exactly the limit; profile-load validation rejects contradictory profiles (e.g. `max_risk_per_trade > daily_loss_limit`) |

**Acceptance:** all 14 checks have a failing-state test that is refused with a distinct reason · a decision that passes all checks produces one order · The5ers profile validated against Doc 2 numbers · kill switch stops new orders within one cycle.

---

## Phase 4 — Data engine correctness and decision cadence

**Goal:** the model sees only closed bars, labels are stable within a bar, and decisions happen when new information exists.

| Deliverable | Detail |
|---|---|
| Closed bars only | `copy_rates_from_pos(sym, tf, 1, n)` for both frames (skip position 0). Current price still comes from the tick |
| Regime fix | `_market_regime` uses both frames as its signature implies (D1 efficiency ratio × H1 efficiency ratio, or a documented combination); label recorded with a `feature_version` |
| Per-bar cadence | `DECISION_MODE = per_bar | interval`. `per_bar`: poll every 15s, decide for a symbol only when H1 bar time advances; `interval`: current behaviour, retained as an experiment arm. Default `per_bar` |
| Decision bar snapshot | `decision_bars` table (or Parquet under `data/bars/`): the exact 10 D1 + 24 H1 rows + tick each decision saw, keyed by `decision_id`. Enables offline replay without API calls |
| Feature versioning | `feature_version` on `market_states` and `decisions`; changing a formula bumps it |

**Acceptance:** label-stability test: features computed at minute 5 and minute 55 of the same hour are identical · decisions/day on 4 symbols ≈ 96 (±API failures) in `per_bar` mode · replaying a stored `decision_bars` row through `compute_features` reproduces the stored `features_json` exactly.

---

## Phase 5 — Sizing and exits

**Goal:** position size follows risk, and the bot can close and modify positions through one idempotent path.

| Deliverable | Detail |
|---|---|
| Sizing | `volume = risk_amount_target / (stop_ticks × trade_tick_value)`, floored to `volume_step`, clamped to `[volume_min, min(volume_max, profile.max_volume)]`; margin check via `mt5.order_calc_margin` against `margin_free` × 0.8. `risk_amount_target = profile.risk_pct × balance` |
| Stop model as versioned param | v1.0.0 keeps fixed 0.20%/0.40%; v1.1.0 = `sl = k_sl × ATR14(H1)`, `tp = k_tp × ATR14(H1)` with `k_sl=1.0, k_tp=2.0`. Selected per `strategy_version`, never both at once. Respect `trade_stops_level` |
| Close path | `close_position(position_ticket, client_order_id)`: `TRADE_ACTION_DEAL` opposite type with `position=ticket`, same comment-tag idempotency, reconciled from deal history like opens. Handles `DONE_PARTIAL` by re-issuing for the remainder |
| Modify path | `modify_sltp(position_ticket, sl, tp)` via `TRADE_ACTION_SLTP`; used later by break-even/trailing (not enabled in v1.x) |
| Close-on-reversal | Risk Engine check (8) + exit manager: if AI flips direction with score ≥ threshold while a position is open → close, then (optionally, next bar) enter |
| Session flatten | Profile-driven: `flatten_before_weekend_min` (e.g. 30 min before Friday server close) and any daily flatten window; job runs in the loop |
| Trade lineage | `trades.exit_reason ∈ {SL, TP, REVERSAL, FLATTEN, KILL, MANUAL}` populated by the reconciler from deal comments/reason codes |

**Acceptance:** sizing unit tests on three symbol types hit `risk_amount` within one `volume_step` · demo: BUY then SELL signal yields one net position and a `REVERSAL` exit row · flatten fires at boundary in a clock-mocked test · partial close test with fake MT5.

---

## Phase 6 — Demo calibration (go/no-go gate)

**Goal:** answer whether the AI score predicts outcomes. No further build-out until this passes.

| Deliverable | Detail |
|---|---|
| Run | `mt5-demo` profile, strategy `llm-mtf` v1.0.0 frozen, `per_bar`, gates on. Target ≥200 closed trades (~4 symbols × ~1–2 trades/day → 4–8 weeks; use 1h interval arm in parallel on a second demo login if available to double throughput) |
| Experiment arms | E1 calibration (primary) · E2 `per_bar` vs `interval=30` · E4 `MEMORY_ENABLED` on/off, alternated weekly |
| Analytics | `GET /api/analytics/calibration?version=` → score buckets (0–39, 40–59, 60–79, 80–100): n, WR, avg R, expectancy after costs, Brier score vs `score/100`; bootstrap CI on expectancy · per-version metrics everywhere (`strategy_version_id` filter) |
| `experiments` table | `id, hypothesis, strategy_version_id, profile, start, end, arms_json, metrics_json, conclusion, decision`. Append-only |
| Dashboard graph | Replace the random-position node canvas in `index.html` (`spawnTradeCircle`, distance-based edges) with a data-driven view: x = time, y = cumulative R, node colour = symbol, node size = \|R\|, edges only between consecutive closed trades on the same symbol; hover shows decision id, score, regime, session. Source: `GET /api/history/trades?status=CLOSED`. No edge implies a relationship until the `relationships` table (Later) exists |
| Report | `Specs/research/E1_calibration.md` |

**Go criteria:** WR monotonic non-decreasing across buckets (Spearman ρ > 0.5 on bucket means) **and** expectancy after costs > 0 in the top bucket with lower 90% CI > −0.1R **and** ≥50 trades in the top two buckets combined.
**No-go:** keep infrastructure; replace signal with a mechanical, backtestable rule set (Phase 8 tooling supports this) and rerun Phase 6.

---

## Phase 7 — News engine (MT5-native, fail-closed)

**Goal:** centralised ±15-minute blackout as a Risk Engine check, with a broker-timestamped calendar source.

| Deliverable | Detail |
|---|---|
| Source | Primary: MQL5 `CalendarValueHistory` inside the terminal (the Python `MetaTrader5` package exposes no calendar API). A small MQL5 script/EA (`Specs/news/CalendarExport.mq5`) runs on a 60s timer and writes `MQL5/Files/calendar.json` (next 48h: `event_id, time_server, currency, importance, name, actual/forecast/previous, revision`). Python reads the file; freshness checked. Secondary (optional): an HTTP calendar provider behind the same `NewsProvider` interface |
| Normalisation | `news_events` table: `event_id, time_utc, currency, importance ∈ {high, medium, low}, name, source, ingested_at, revised_from`. Server time → UTC via `server_utc_offset_min` |
| Affected assets | Currency map: EURUSD ← {EUR, USD}; GBPUSD ← {GBP, USD}; XAUUSD ← {USD}; BTCUSD ← {USD} (configurable) |
| Blackout rule | `blackout(symbol, now) = ∃ event: currency ∈ affected(symbol) ∧ importance ≥ profile.min_importance ∧ now ∈ [time − pre, time + post]`. Profile default `pre=post=15`, `min_importance=high`. Entries blocked; exits always allowed |
| Fail-closed | File older than `max_age_s` (default 300) or unparsable → `blackout=unknown` → Risk Engine refuses entries and raises a WARN; existing positions untouched |
| Prompt context | Same provider also feeds `decisions.news_json` (`minutes_to_next`, `minutes_since_last`, `importance`, `condition`) as before |
| Experiments | `news_windows` param on the profile so ±15, ±30/±15 by importance, and event-specific windows are versioned arms, not code changes |

**Acceptance:** boundary tests at exactly −15:00, 0:00, +15:00 (allowed / blocked / allowed at the edges per documented convention, half-open interval) · stale file → entries refused, event logged · clock-mocked test across a DST change using a non-zero `server_utc_offset_min` · calendar export verified on the demo terminal for one real high-impact event.

---

## Phase 8 — Replay, backtest, and experiment framework

**Goal:** evaluate rule and parameter changes offline from stored decisions and bars, with realistic costs.

| Deliverable | Detail |
|---|---|
| Replay engine | Input: `decision_bars` + stored AI outputs + `broker_profiles` (spread history from `market_states`). Re-runs gates, sizing, stop model, and news rule for any `strategy_version` / `risk_profile`; produces synthetic trades with entry at ask/bid + half-spread slippage model, commission from broker profile, exit at SL/TP on subsequent bars (bar-based; conservative fill: if a bar touches both, assume SL) |
| Historical extension | `copy_rates_range` bulk-load of D1/H1 into `bars` table for the universe (≥2 years) so mechanical rule sets can be backtested without AI calls; LLM strategy replays only where stored decisions exist |
| Costs | Spread from stored `market_states` by session; commission/swap from `broker_profiles`; slippage param default 0.5 × spread |
| Walk-forward | Rolling train/validate/test windows for any parameterised rule (stop `k`, score threshold, cadence); results into `experiments` |
| Reproducibility | Every backtest records `strategy_version_id, risk_profile_id, dataset_version (bars table hash), params, code git sha, python/pandas versions` |

**Acceptance:** replaying the Phase 6 demo period reproduces realised P&L within ±10% and trade count within ±5% · a walk-forward run over the stop-`k` grid completes and writes an `experiments` row per fold · bar loader validates no gaps/duplicates and logs them.

---

## Phase 9 — Robustness and prop-constraint simulation

**Goal:** know the failure modes before risking an evaluation fee.

| Deliverable | Detail |
|---|---|
| Monte Carlo | Bootstrap/permute closed-trade R sequences (≥1,000 paths): distribution of max DD, DD duration, time-to-target, P(hit 3% daily), P(hit 10% floor) |
| Prop simulator | Applies The5ers rules to each path: 3% daily on max(EOD equity, balance), $10k floor, Phase-1 10%/8% and Phase-2 5% targets, ±2m execution ban (superset ±15 already applied), funded 50% consistency. Output: P(pass Phase 1), P(pass Phase 2), expected days, P(breach) |
| Sensitivity | Score threshold, risk %, stop `k`, cadence, exposure cap — each swept; report stability, not just best |
| Stress | Spread ×3, slippage ×3, 24h news-feed outage, 10-loss streak injected |

**Acceptance:** report in `Specs/research/robustness_v1.md` with P(breach) and P(pass) at the chosen profile; profile adjusted so P(daily breach) < 5% per month and P(floor breach) < 5% over the evaluation.

---

## Phase 10 — Shadow / paper on The5ers rule profile

**Goal:** run the exact The5ers configuration on demo before paying for the evaluation.

| Deliverable | Detail |
|---|---|
| Profile switch | `the5ers-100k` profile on the demo login; symbol names mapped via `symbol_map` in the profile (demo `…m` → The5ers names) |
| Reconciliation | Daily ledger vs MT5, floor and daily-limit distance shown on dashboard, consistency ratio tracked |
| Alerts | Telegram (or email) on: order rejected, override streak, kill switch, ledger drift, news feed stale, MT5 disconnect > 60s, daily loss > 50% of limit |
| Duration | ≥4 weeks or ≥60 trades, whichever later, with zero rule breaches |

**Acceptance:** zero simulated breaches · all alerts fired at least once in a drill · every trade traceable decision → order → deal → ledger.

---

## Phase 11 — Controlled live (The5ers evaluation)

**Goal:** smallest exposure that can pass within a sane time.

| Deliverable | Detail |
|---|---|
| Config | Risk % from Phase 9 (expect 0.25–0.5%), cap 1, 3 losses/day, kill switch armed, `RESUME_ENGINE_ON_STARTUP=false` |
| Operator runbook | `Specs/decisions/runbook_live.md`: start/stop, what each alert means, manual flatten procedure, when to halt |
| Live checks | First 5 trades verified manually against the terminal (ticket resolution, fills, comment tags, SL/TP placement) |

**Acceptance:** first week with no reconciliation drift, no unexplained overrides, ledger matching the firm's dashboard.

---

## Phase 12 — Production hardening

| Deliverable | Detail |
|---|---|
| Monitoring | `/api/health` extended (last decision age per symbol, news freshness, ledger drift, lock owner); external watchdog (Windows Task Scheduler / scheduled script) restarts the process and alerts |
| Backups | Nightly `data/quantbot.db` copy + WAL checkpoint; restore drill documented |
| Deployment | Tagged releases; `strategy_version` and `risk_profile` recorded in a `deployments` table with git sha |
| Clock | NTP verified; startup asserts `abs(local − NTP) < 2s`; server-offset re-measured hourly |

---

## Later — explicitly not yet

| Item | Unlock condition |
|---|---|
| Relationship discovery (asset × regime × session × outcome) | ≥500 closed trades under frozen versions; multiple-testing control (BH-FDR) built into the analytics first |
| Feature store beyond the current 10 features | A Phase 8 experiment shows a specific feature changes replay outcomes |
| ML filter on AI decisions | Phase 6 pass + ≥500 trades; compared against the rule baseline with walk-forward |
| Correlation hedging | Net-exposure check (Phase 3 #14) has been live for ≥1 month and the exposure × outcome analysis justifies it |
| Break-even / trailing stops | Replay (Phase 8) shows improvement; ships as a new strategy version |
| PostgreSQL / TimescaleDB / Redis / queues | Multi-process need or >10M rows. Not before |
| MQL5 EA as executor | Only if Python↔terminal latency or reliability becomes a measured problem; the terminal-side calendar script in Phase 7 is the only MQL5 code planned |

---

## Dependency graph

```
P1 foundation ─┬─► P2 ledger ─► P3 risk engine ─┬─► P5 sizing/exits ─► P6 calibration (GATE)
               └─► P4 data/cadence ─────────────┘            │
                                                             ├─► P7 news ─┐
                                                             ├─► P8 replay/backtest ─► P9 robustness ─► P10 shadow ─► P11 live ─► P12 hardening
                                                             └────────────┘
```

Phases 7 and 8 can run in parallel after 6. Nothing in "Later" starts before 9.