# Where the project stands

**As of commit `6219029`, branch `phase-5-sizing-exits`, 2026-09-10.**
255 tests passing. CI green on Python 3.12 and 3.13.

---

## What this thing is

A MetaTrader 5 trading bot with three layers that do not trust each
other:

1. **An LLM reads bars and produces an opinion.** Multi-timeframe D1 +
   H1 candles go in, a `{signal, score, reasoning}` comes out.
2. **A risk engine decides whether that opinion may become an order.**
   Fourteen ordered checks. The AI cannot overrule it.
3. **SQLite records everything, and MT5 is the source of truth.** The
   database mirrors the broker; a reconciler corrects the mirror. It
   never writes back to MT5.

The separation is the whole design. The AI is treated as an untrusted
input — `get_ai_decision()` is contractually incapable of raising, and
returns a safe HOLD on every failure path, so a malformed model
response cannot reach the execution path.

---

## Where the build is

Twelve phases, five done. Each is a branch, merged to `main` only after
its acceptance criteria pass in CI *and* on a real demo terminal.

| Phase | What it bought | State |
|---|---|---|
| 0 | Audit of the original code; found 8 ranked weaknesses | Complete |
| 1 | Reproducibility, auth, measured broker facts | Merged |
| 2 | Money risk per trade, daily ledger reconciled to MT5 | Merged |
| 3 | Risk Engine v1 — 14 checks, profiles, kill switch | Merged |
| 4 | Closed bars only, stable labels, per-bar cadence | Merged |
| 5 | Risk-based sizing, exits, close/modify paths | **Code complete, not merged** |
| 6 | Demo calibration | **HARD GATE — nothing after it gets built until it passes** |

Phase 5 is finished and pushed but deliberately not merged: the
project's own rule requires a demo-terminal sign-off
(`Specs/decisions/phase5_windows_verification.md`) that can only be
produced on Windows with MT5 attached. That document is the single
thing standing between here and Phase 6.

---

## What Phase 5 actually changed

Before it, position size was a fixed `0.01` lot regardless of account,
instrument or stop distance — and the *only* way a position ever closed
was the broker hitting SL or TP. There was no close path at all.

**Sizing** now inverts the risk formula: `volume = target_risk /
(stop_ticks × tick_value)`, floored to the broker's `volume_step`,
clamped to its min/max, then margin-checked at 80% of free margin.
Flooring rather than rounding is deliberate — rounding up would risk
more than the profile allows, which is the one direction the risk
engine must never be surprised in. If the minimum tradeable lot already
exceeds the budget, the trade is **refused and reported**, never
silently taken.

**Stops became a versioned parameter** rather than a constant: v1.0.0
is the frozen `0.20%/0.40%` baseline, v1.1.0 is `k × ATR14(H1)`. Never
both at once, selected per strategy version, so the Phase 6 study
measures one thing.

**Exits** exist for the first time — reversal, weekend flatten, and
kill-switch flatten, all through one idempotent `close_position` that
carries the same comment-tag crash-safety as opens. When the AI flips
direction, the bot now *closes* the existing position instead of
opening the opposite one and leaving both legs open. That was hedging,
which both target firms prohibit outright.

---

## Two bugs the audit caught

Worth recording, because both were invisible in a passing test suite.

**Reversal cycles wrote no decision row.** `process_symbol` returned
early — before `repo.insert_decision` — so every flip produced no
`decisions` row, no `strategy_version_id`, no stored bars for replay.
Phase 6's calibration would have silently under-counted exactly the
population it most needs to see. Fixed by letting the reversal fall
through to the shared persistence path.

**CI had been red since Phase 1.** Nine consecutive failing runs,
`main` included. `requirements.txt` pins `numpy==2.5.3`, which requires
Python ≥3.12 and ships no cp311 wheel, so the 3.11 matrix leg died at
`pip install` every single time. The tests were always fine; the 3.12
leg passed on all nine. But "CI green" is Phase 1's stated acceptance
criterion and every subsequent phase was gated on it. The red X had
been there long enough to stop being read.

---

## Known gaps, deliberately open

- **`min_holding_bars` is declared but never enforced.** Both risk
  profiles set it (The5ers uses 1, for their microscalping ban), and
  `profiles.py` validates it, but no check consults it. It is not among
  Phase 3's fourteen checks, so the plan does not require it before
  Phase 10. Note the interaction: a Phase 5 reversal can close a
  position seconds after opening it — precisely what that rule forbids.
  Phase 10 must either enforce it or consciously exempt reversals.
- **A sizing refusal leaves `risk_checks_json` empty**, because the
  guard returns HOLD before the risk engine runs. The `override_reason`
  explains it, but a Phase 6 query slicing on the verdict will not see
  those rows.

---

## The honest summary

The infrastructure is in good shape: reproducible decisions, measured
broker facts, money risk known before an order is sent, a risk engine
with sole authority, and a reconciler that treats MT5 as truth.

None of that answers the only question that matters, which is Phase 6's:
**does the AI's score predict anything at all?** That needs ≥200 closed
demo trades under a frozen strategy version. If the answer is no, the
plan is to keep every bit of this infrastructure and replace the signal
with a mechanical, backtestable rule set — which is why the sizing,
exits, risk engine and replay store were all built to be
signal-agnostic.
