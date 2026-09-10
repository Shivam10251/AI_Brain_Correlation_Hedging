# After Phase 5 — what YOU have to do next

Companion to `README.md`, which is the pre-Phase-1 record and is now
mostly historical. This file is current as of commit `6219029`,
branch `phase-5-sizing-exits`, 2026-09-10. 255 tests passing, CI green.

Everything below needs a Windows machine with MetaTrader 5, a funded
demo login, weeks of calendar time, or a decision that is yours.

Work top to bottom. Nothing later survives something earlier being
skipped.

---

## STEP 1 — Verify Phase 5 on the demo terminal

**This is the only thing blocking the merge to `main`.** Phases 1-4
each produced a `Specs/decisions/phaseN_windows_verification.md`.
Phase 5 needs its own, and the project's own rule in `CLAUDE.md` says
no phase starts until the previous one passes *in CI and on the demo
terminal*. CI is now green; the terminal half is outstanding.

Start the bot on the Windows host:

```
start_bot.bat
```

Then walk this list. Every item was verified against `tests/fake_mt5.py`,
which cannot reproduce broker quirks.

- [ ] **1. Sizing is no longer a fixed lot.** Let one trade execute on
      each of EURUSD, XAUUSD and BTCUSD. Volume must differ per symbol
      and must NOT be `0.01` on all three.
      ```
      sqlite3 data\quantbot.db "SELECT symbol,volume,risk_amount,stop_model FROM trades ORDER BY id DESC LIMIT 5;"
      ```
      `risk_amount` should sit near `risk_pct × balance` (0.25% on the
      demo profile) for every row.

- [ ] **2. The order carries the sized volume.** Compare the volume in
      the MT5 terminal's Trade tab against `trades.volume`. Phase 5
      wired `execute_trade` to accept the planned volume and stops; if
      the terminal shows `0.01` while the database shows something
      else, that wiring is not reaching the broker.

- [ ] **3. Stops respect `trade_stops_level`.** Confirm no order is
      rejected with retcode `10016` (invalid stops). XAUUSD is the one
      to watch — it has the widest stops level of your four symbols.

- [ ] **4. A reversal closes rather than hedges.** Wait for (or force)
      a BUY then a SELL on the same symbol with score ≥ 70.
      - the first position must CLOSE, not sit alongside a new one
      - the MT5 terminal must show **one net position**, never two
      - `trades.exit_reason` must read `REVERSAL`
      - a `decisions` row must exist for the reversal cycle with
        `final_decision=HOLD` (this was a bug, fixed in `471b34b` —
        confirm the row is really there)
      ```
      sqlite3 data\quantbot.db "SELECT id,symbol,exit_reason,pnl FROM trades WHERE exit_reason IS NOT NULL ORDER BY id DESC LIMIT 5;"
      ```

- [ ] **5. Partial close.** Hard to force deliberately; if you see
      `partial_fills > 0` in an EXIT event, confirm the position
      actually reached zero rather than being left half open.

- [ ] **6. Kill switch flattens.** With at least one position open:
      ```
      curl -X POST http://127.0.0.1:8000/api/risk/halt ^
        -H "Authorization: Bearer %API_TOKEN%" ^
        -H "Content-Type: application/json" ^
        -d "{\"action\":\"halt\",\"flatten\":true}"
      ```
      Every magic-`100001` position must close, `exit_reason=KILL`, and
      no new order may be sent afterwards.

- [ ] **7. Weekend flatten fires.** Set
      `flatten_before_weekend_min: 30` and be watching on Friday near
      the broker's close. Confirm it uses the **broker's** clock — if
      your server is UTC+3, it should fire at 23:29 *server* time, not
      23:29 UTC.

- [ ] **8. Exit lineage is right.** After a few trades close naturally:
      ```
      sqlite3 data\quantbot.db "SELECT exit_reason,COUNT(*) FROM trades WHERE closed_at IS NOT NULL GROUP BY exit_reason;"
      ```
      SL and TP should dominate. A pile of `MANUAL` means the
      reconciler's price-matching tolerance is wrong for your broker —
      tell me and I will adjust `EXIT_PRICE_TOLERANCE`.

Write the results up as `Specs/decisions/phase5_windows_verification.md`
in the same shape as the Phase 4 one, then merge the branch to `main`.

---

## STEP 2 — Decisions to make before the calibration run

These change what Phase 6 measures, so they must be settled *before*
the run starts, not during it.

### DECISION 1 — Which stop model does the calibration freeze?

`STOP_MODEL=percent` (v1.0.0) is the current default and the plan's
frozen baseline. The ATR model (v1.1.0) exists and is tested but has
never been shown to be better — that is question E3, and it is answered
offline in Phase 8 by replaying stored decisions, not by running it
live.

**Recommendation: leave it on `percent`.** Changing it mid-study means
you have measured two things and can conclude neither.

### DECISION 2 — Risk per trade on the demo

`Specs/risk/mt5-demo.yaml` sets `risk_pct: 0.25`. This is calibration,
not profit — the point is a clean dataset, and a blown demo account
ends the study early. Leave it unless you have a reason.

### DECISION 3 — One demo login or two?

The plan suggests running the `interval=30` arm on a *second* demo
login in parallel to double throughput. Two logins turns 8 weeks into
roughly 4. If you can get a second demo account, do it — the wait is
the expensive part of Phase 6.

### DECISION 4 — `min_holding_bars` is declared but not enforced

Both profiles set it (The5ers uses `1`, for their microscalping ban).
`profiles.py` loads and validates it, but **no check consults it**. It
is not one of Phase 3's fourteen checks, so the plan does not require
it before Phase 10.

The interaction that matters: a Phase 5 reversal can close a position
seconds after opening it, which is exactly what that rule prohibits.
Harmless on demo. Before Phase 10 you must either enforce it or
consciously exempt reversals. **Tell me which** and I will build it as
a fifteenth check.

---

## STEP 3 — Phase 6, the hard gate

**Nothing after this gets built until it passes.** Phase 6 asks the
only question that matters: does the AI score predict anything?

### The run

- Profile `mt5-demo`, strategy `llm-mtf` v1.0.0 **frozen**,
  `DECISION_MODE=per_bar`, all gates on.
- Target **≥200 closed trades**. At ~1-2 trades/day across 4 symbols
  that is 4-8 weeks.
- Do not edit `backend/ai/prompts.py` during the run. Editing it
  changes the prompt hash, which registers a new strategy version and
  splits your trade population in half (invariant 9).

### Experiment arms

| Arm | Question |
|---|---|
| E1 | Score calibration — the primary study |
| E2 | `per_bar` vs `interval=30` |
| E4 | `MEMORY_ENABLED` on/off, alternated weekly |

### What I still have to build for it

These are mine, not yours, but they need to exist before the data is
worth reading. Ask me when you are ready:

- [ ] `GET /api/analytics/calibration?version=` — score buckets
      (0-39, 40-59, 60-79, 80-100) with n, win rate, avg R, expectancy
      after costs, Brier score, bootstrap CI on expectancy
- [ ] `experiments` table — append-only, one row per arm
- [ ] Replace the random-node canvas in `index.html` with a real chart:
      x = time, y = cumulative R, colour = symbol, size = |R|
- [ ] The report itself: `Specs/research/E1_calibration.md`

### Go / no-go

**GO** requires all three:

1. Win rate monotonic non-decreasing across buckets — Spearman ρ > 0.5
   on bucket means
2. Expectancy after costs > 0 in the top bucket, with lower 90% CI
   > −0.1R
3. ≥50 trades in the top two buckets combined

**NO-GO** is not the end of the project. Keep every bit of this
infrastructure — sizing, exits, risk engine, replay store were all
built signal-agnostic on purpose — and replace the LLM signal with a
mechanical, backtestable rule set. Then rerun Phase 6.

Be honest at this gate. It is the whole reason the project has one.

---

## What comes after, briefly

Only if Phase 6 passes:

| Phase | What | Depends on |
|---|---|---|
| 7 | News engine — MQL5 calendar export, ±15min blackout, fail-closed | Can run parallel with 8 |
| 8 | Replay and backtest from stored `decision_bars` | Can run parallel with 7 |
| 9 | Monte Carlo + The5ers prop-constraint simulation | 8 |
| 10 | Shadow run on the The5ers rule profile, ≥4 weeks, zero breaches | 9 |
| 11 | Controlled live on the real evaluation | 10 |
| 12 | Production hardening — watchdog, backups, NTP | 11 |

Do not skip 9. It is what tells you the probability of breaching the
daily limit *before* you pay an evaluation fee.

---

## Known open items

- **`min_holding_bars` unenforced** — see DECISION 4 above.
- **A sizing refusal leaves `risk_checks_json` empty.** The guard in
  `apply_risk_checks` returns HOLD before the risk engine runs, so
  those decisions carry an `override_reason` but no verdict JSON. A
  Phase 6 query slicing on the verdict will not see them. Cosmetic
  unless you plan to slice that way.
- **`task_pending/README.md` is stale.** It describes a 3-pass
  reconciler and 7 tables; there are now 4 passes and 13 tables. Kept
  as the pre-Phase-1 record. Read this file instead.
