# AI_Brain_Correlation_Hedging

A MetaTrader 5 trading bot: an LLM reads multi-timeframe bars, a risk
layer decides whether its signal may become an order, and SQLite is a
durable audit trail with MT5 as the source of truth.

**MetaTrader 5 only.** Any other platform, broker or instrument is out
of scope — see `Specs/MT5_Build_Plan.md`.

---

## Run it

```bash
uvicorn backend.main:app --host 127.0.0.1 --port 8000   # macOS/Linux (no MT5)
start_bot.bat                                            # Windows trading host
```

- `http://127.0.0.1:8000/` — live engine dashboard
- `http://127.0.0.1:8000/dashboard` — read-only Quant DB explorer

Config comes from `.env`; copy `env.example` (no leading dot — the
`.gitignore` pattern `.env.*` would swallow `.env.example`).

## Test it

```bash
pytest                    # everything: 55 items, incl. all 79 legacy checks
pytest -q tests/test_phase1_foundation.py
```

There is no `npm`, no build step. `pytest` is the whole gate, and CI
runs exactly that.

`MetaTrader5` is Windows-only, so **every test runs against
`tests/fake_mt5.py`**, installed into `sys.modules` by
`tests/conftest.py` before any backend import. CI asserts the real
package is *not* installed.

The two original suites (`test_api.py`, `test_restart_recovery.py`) are
standalone scripts that `sys.exit()` at import. They are excluded from
collection and driven as subprocesses by `test_legacy_suites.py`, so
all 79 of their checks still run.

---

## Layout

```
backend/
  main.py            FastAPI app, lifespan recovery, all endpoints
  auth.py            bearer-token middleware (Phase 1)
  config.py          every setting, from .env
  core/
    engine.py        per-cycle pipeline and the risk gate
    runtime.py       shared bot_state, MT5 connection, engine lock
    reconciler.py    database <-> MT5 reconciliation, 3 passes
  ai/
    brain.py         DeepSeek call, validation, safe-HOLD on every failure
    prompts.py       prompt construction  <- THIS IS THE STRATEGY
    versioning.py    prompt hashing + strategy_versions (Phase 1)
    memory.py        experience distillation and retrieval
  market/
    data_engine.py   candles, account snapshot, derived features
    execution.py     order placement, idempotent client order ids
    broker_profile.py measured broker/symbol facts (Phase 1)
    news.py          extension point, no-op by default
  database/
    schema.sql       tables; additive migrations in connection.py
    repository.py    public data-access façade
    repo_trading.py  decisions + trades
    repo_context.py  equity, market state, experiences, events
    repo_meta.py     strategy versions, broker/symbol profiles
    explorer.py      read-only SQL surface for /dashboard
    analytics.py     P&L, win rate, drawdown, streaks

frontend/templates/  index.html (engine), dashboard.html (DB explorer)
data/quantbot.db     runtime, gitignored
Specs/               plans, decisions, research  <- read before building
```

---

## Invariants — do not break these

1. **MT5 is the source of truth** for positions, fills and P&L. The
   database mirrors it. The reconciler corrects the mirror; it never
   writes to MT5.

2. **Intent is written before the order is sent.** The PENDING `trades`
   row plus its `client_order_id` comment tag is what makes execution
   idempotent across a crash. After a restart an ambiguous attempt is
   resolved by *asking MT5*, never by re-sending.

3. **`get_ai_decision()` never raises.** Every failure returns a dict
   with `signal: "HOLD"` and a non-`ok` status. A malformed AI response
   must not be able to reach the execution path.

4. **A rejection is data, not an exception.** `execute_trade()` returns
   a structured result; rejections are persisted with their retcode.

5. **No silent overrides.** If `final_decision` differs from
   `ai_signal`, `override_reason` says why and a WARN event is written.

6. **Never assume a broker fact.** Filling mode, digits, tick value,
   stops level and the server UTC offset are *measured* into
   `broker_profiles` / `symbol_profiles` at startup. Hardcoding any of
   them is how the pre-Phase-1 bugs happened (Phase 0 §2.2, §2.6).

7. **Migrations are additive only.** `ADDITIVE_COLUMNS` in
   `database/connection.py` may add a column; it may never drop,
   rename or retype one. A live database holds real trading history.

8. **The dashboard cannot write.** `explorer.py` opens its own
   connection with `PRAGMA query_only = ON`.

9. **Every decision references a strategy version.** Editing
   `prompts.py` changes the hash, which registers a new version rather
   than silently polluting the previous one's trade population.

---

## Working rules

- Do what was asked; nothing more.
- Prefer editing an existing module over adding one.
- Read a file before editing it.
- Never commit `.env`, secrets, or anything under `data/`.
- Do not add a `Co-Authored-By` trailer unless
  `.claude/settings.json` sets `attribution.commit`.
- Run `pytest` after any change. All 79 legacy checks must stay green.
- Trading-behaviour changes ship as a **new strategy version**, never
  as an in-place edit to a running one.

## Phases

`Specs/MT5_Build_Plan.md` is the active plan; one branch per phase,
merged to `main` before the next begins. No phase starts until the
previous one's acceptance criteria pass — in CI *and* on the demo
terminal (`Specs/decisions/phase*_windows_verification.md`).

**Phase 6 is a hard gate.** It asks whether the AI score predicts
anything at all, over ≥200 closed demo trades. Nothing after it gets
built until it passes.
