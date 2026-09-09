# Phase 4 — live verification on the Windows terminal

Green in CI (199 pytest items).

**Read this first:** the bot now decides **once per H1 bar**, not every
30 seconds. Expect roughly **96 decisions/day** across four symbols
instead of ~11,500 API calls. Long quiet stretches between bars are
correct, not a hang.

---

## 1. Cadence

- [ ] Console prints `[CYCLE <id>] new H1 bar on: …` roughly on the
      hour, then nothing until the next bar
- [ ] Between bars the process is still alive — reconciliation and the
      ledger keep running every 15 seconds

```
sqlite3 data\quantbot.db "SELECT substr(created_at,1,13) AS hour, COUNT(*) FROM decisions GROUP BY hour ORDER BY hour DESC LIMIT 8;"
```

- [ ] About **4 decisions per hour** (one per symbol), not hundreds

```
sqlite3 data\quantbot.db "SELECT symbol, COUNT(DISTINCT bar_time_utc), COUNT(*) FROM decisions WHERE bar_time_utc IS NOT NULL GROUP BY symbol;"
```

- [ ] The two counts are **equal** per symbol — one decision per bar,
      never the same bar twice

---

## 2. Closed bars only

```
sqlite3 data\quantbot.db "SELECT symbol, bar_time_utc, bar_time_server, created_at FROM market_states ORDER BY id DESC LIMIT 5;"
```

- [ ] `bar_time_server` is always at least one full hour **before**
      `created_at` converted to server time — never the current bar

---

## 3. Label stability

Watch one symbol across a single hour.

```
sqlite3 data\quantbot.db "SELECT bar_time_utc, market_regime, daily_trend, h1_trend, volatility_bucket, atr FROM market_states WHERE symbol='EURUSDm' ORDER BY id DESC LIMIT 6;"
```

- [ ] Rows sharing a `bar_time_utc` have **identical** labels and ATR

---

## 4. Replay reproduces history

This is what Phase 8 is built on.

```
sqlite3 data\quantbot.db "SELECT COUNT(*) FROM decision_bars;"
sqlite3 data\quantbot.db "SELECT d.id, d.symbol, length(b.daily_json), length(b.hourly_json) FROM decisions d JOIN decision_bars b ON b.decision_id=d.id ORDER BY d.id DESC LIMIT 5;"
```

- [ ] One `decision_bars` row per decision
- [ ] Both JSON blobs are non-trivial (hundreds of bytes)

Then verify a real stored decision replays exactly:

```
python -c "from backend.database import initialize_database, repository as repo; from backend.market import replay; initialize_database(); rows=repo.get_decision_bars_batch(limit=20); bad=[(r['decision_id'], replay.compare_to_stored(r)[1]) for r in rows if not replay.compare_to_stored(r)[0]]; print('checked', len(rows), 'mismatches:', bad)"
```

- [ ] **`mismatches: []`** — every stored decision replays byte-for-byte

> A mismatch means the replay engine disagrees with history, which
> would invalidate every Phase 8 backtest. Send me the output.

---

## 5. Regime now uses both timeframes

```
sqlite3 data\quantbot.db "SELECT symbol, market_regime, json_extract(features_json,'$.efficiency_ratio_daily'), json_extract(features_json,'$.efficiency_ratio_h1') FROM market_states ORDER BY id DESC LIMIT 8;"
```

- [ ] Both efficiency ratios are populated
- [ ] `market_regime` reads as the geometric mean of the two — a clean
      daily with noisy H1 should NOT say `trending`

```
sqlite3 data\quantbot.db "SELECT DISTINCT feature_version FROM market_states;"
```

- [ ] `1.1.0` for rows written since the update (older rows NULL)

---

## 6. Switching arms (for the Phase 6 E2 experiment)

Set `DECISION_MODE=interval` in `.env` and restart.

- [ ] Cycles resume every 30s
- [ ] Set it back to `per_bar` afterwards

---

## Result

| Check | Pass | Notes |
|---|---|---|
| 1. ~4 decisions/hour, one per bar |  | |
| 2. Closed bars only |  | |
| 3. Labels stable within the hour |  | |
| 4. Replay mismatches = [] |  | |
| 5. Regime uses both frames |  | |
| 6. Mode switch works |  | |
