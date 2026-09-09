# Specs

Design record for the MT5 trading system. Created in Phase 1.

```
Specs/
  MT5_Build_Plan.md                  the active 12-phase plan
  Phase0_Audit_and_Execution_Plan.md the audit everything below assumes

  strategy/source/   immutable source documents - never edited, only added to
  decisions/         decisions made, and the checks only a human can run
  architecture/      module contracts and data-flow notes
  risk/              risk profiles (YAML) - populated in Phase 3
  news/              calendar export script and mapping - populated in Phase 7
  data/              dataset and bar-store notes - populated in Phase 8
  research/          experiment reports (E1 calibration, robustness, ...)
```

## Rules

- **`strategy/source/` is append-only.** Those PDFs are the original
  brief. Superseding one means adding a new file and noting it here,
  never editing in place.
- **A decision goes in `decisions/`, not in a commit message.** If a
  choice would surprise someone six months from now, write it down with
  the reason.
- **Empty directories are placeholders.** Each carries a `.gitkeep`
  until the phase that fills it.

## Phase status

| Phase | State |
|---|---|
| 0 — audit | Complete (`Phase0_Audit_and_Execution_Plan.md`) |
| 1 — foundation, versioning, broker profile, safety | Code complete; live checks in `decisions/phase1_windows_verification.md` |
| 2 — money risk and daily ledger | Code complete; live checks in `decisions/phase2_windows_verification.md` |
| 3 — Risk Engine v1 | Code complete; live checks in `decisions/phase3_windows_verification.md` |
| 4 — data engine and cadence | Not started |
| 5 — sizing and exits | Not started |
| 6 — demo calibration | **GATE** — needs a 4–8 week live demo run |
| 7–12 | Blocked behind the Phase 6 gate |
