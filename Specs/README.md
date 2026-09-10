# Specs

The design record for the MT5 trading system: the plan, the gates, and
the risk profiles. Created in Phase 1, slimmed in Phase 5.

For the source PDFs and written explanations of how the system works,
see [`Knowledge/`](../Knowledge/README.md).

```
Specs/
  MT5_Build_Plan.md                  the active 12-phase plan
  Phase0_Audit_and_Execution_Plan.md the audit everything below assumes

  decisions/   decisions made, and the checks only a human can run
  risk/        risk profiles (YAML) - LOADED BY THE CODE at runtime
  news/        calendar export script and mapping - filled in Phase 7
  research/    experiment reports (E1 calibration, robustness, ...)
```

## Rules

- **`risk/` is not documentation.** `backend/risk/profiles.py` reads
  these YAML files at runtime. A typo here is a trading bug, not a
  formatting one. Changing a number is a trading-behaviour change: it
  ships as a new profile version, never as an in-place edit to one
  that is running.
- **A decision goes in `decisions/`, not in a commit message.** If a
  choice would surprise someone six months from now, write it down
  with the reason.
- **`news/` and `research/` are placeholders with named owners.** The
  build plan references `news/CalendarExport.mq5` (Phase 7),
  `research/E1_calibration.md` (Phase 6) and
  `research/robustness_v1.md` (Phase 9) by filename. Each carries a
  `.gitkeep` until the phase that fills it.

Phase 5 removed `architecture/` and `data/`, which were empty
placeholders no deliverable ever claimed, and moved `strategy/source/`
to `Knowledge/source/`.

## Phase status

| Phase | State |
|---|---|
| 0 — audit | Complete (`Phase0_Audit_and_Execution_Plan.md`) |
| 1 — foundation, versioning, broker profile, safety | Code complete; live checks in `decisions/phase1_windows_verification.md` |
| 2 — money risk and daily ledger | Code complete; live checks in `decisions/phase2_windows_verification.md` |
| 3 — Risk Engine v1 | Code complete; live checks in `decisions/phase3_windows_verification.md` |
| 4 — data engine and cadence | Code complete; live checks in `decisions/phase4_windows_verification.md` |
| 5 — sizing and exits | Code complete, CI green; **needs `decisions/phase5_windows_verification.md`** before merge to `main` |
| 6 — demo calibration | **GATE** — needs a 4–8 week live demo run |
| 7–12 | Blocked behind the Phase 6 gate |
