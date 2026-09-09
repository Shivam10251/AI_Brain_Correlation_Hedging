# Phase 2 — live verification on the Windows terminal

Green in CI against the fake broker (101 pytest items). These are the
checks a fake cannot answer.

---

## 1. Every trade has a money-risk figure

**Acceptance:** *"every `trades` row has `risk_amount > 0`."*

```
sqlite3 data\quantbot.db "SELECT COUNT(*) FROM trades WHERE created_at > date('now','-1 day') AND (risk_amount IS NULL OR risk_amount <= 0);"
```

- [ ] Returns **0**

If it does not, check the events feed for
`risk_amount could not be computed` — that means the symbol has no
stored broker specification, i.e. Phase 1's capture did not see it.

Spot-check the figure is *right*, not merely present:

```
sqlite3 data\quantbot.db "SELECT symbol, volume, entry_price, stop_loss, stop_distance_price, spread_at_entry, risk_amount FROM trades WHERE execution_status='EXECUTED' ORDER BY id DESC LIMIT 5;"
```

- [ ] For 0.01 lots, `risk_amount` is roughly:
      EURUSD ≈ **$2.20**, GBPUSD ≈ **$2.50**, BTCUSD ≈ **$1.28**,
      XAUUSD ≈ **$4.80** (0.20% stop). Order of magnitude is what
      matters — if gold reads $0.05 or $480, the tick specification is
      being misread and I need to know.

---

## 2. The daily ledger reconciles to MT5

**Acceptance:** *"after a demo day with ≥5 closed trades,
`daily_ledger.realized_pnl` equals the sum of MT5 deal
profit+commission+swap for that day, to the cent."*

Let the bot run until at least 5 trades have closed, then:

```
sqlite3 data\quantbot.db "SELECT trading_day, realized_pnl, commission, swap, trade_count, win_count, loss_count, max_floating_loss, drift, reconciled_at FROM daily_ledger ORDER BY trading_day DESC LIMIT 3;"
```

- [ ] `trade_count` ≥ 5
- [ ] `reconciled_at` is recent (it runs every cycle)
- [ ] **`drift` is 0.00** (or below 0.01)

Cross-check against the terminal: MT5 → **History** tab → set to Today
→ read the **Profit** total (it includes commission and swap).

- [ ] Terminal total == `realized_pnl` **to the cent**

> A non-zero drift is not necessarily a bug — it is the system telling
> you the local mirror disagreed and was corrected to MT5. But a drift
> that reappears every cycle means something is double-counting, and I
> need the `RECONCILE` events.

---

## 3. The trading day is the SERVER day

This is the one most likely to be subtly wrong, and it silently
misaligns every daily limit.

```
sqlite3 data\quantbot.db "SELECT trading_day, trade_count FROM daily_ledger ORDER BY trading_day DESC LIMIT 3;"
```

- [ ] Around the broker's midnight, a trade closing at e.g. 23:55
      **server** time lands on the day that is ending, not the next
      one, even though UTC has not rolled over yet

Confirm the offset in use:

```
curl -H "Authorization: Bearer %API_TOKEN%" http://127.0.0.1:8000/api/ledger
```

- [ ] `server_utc_offset_min` matches what Phase 1 recorded
- [ ] `trading_day` matches the date shown in the MT5 terminal

---

## 4. Floating loss is being sampled

A position that dipped hard and recovered leaves **no trace** in deal
history, so the ledger samples it every cycle.

- [ ] After a session with an open position that went underwater,
      `max_floating_loss` is > 0 and roughly matches the worst
      unrealised loss you saw in the terminal

---

## 5. R-multiples are money-based now

```
sqlite3 data\quantbot.db "SELECT id, symbol, pnl, risk_amount, r_multiple FROM trades WHERE execution_status='CLOSED' ORDER BY id DESC LIMIT 10;"
```

- [ ] `r_multiple` ≈ `pnl / risk_amount`
- [ ] A trade stopped out at full SL is close to **−1.0R**
      (slightly worse than −1 is correct — commission is included)

---

## 6. Account attribution + migration

```
sqlite3 data\quantbot.db "SELECT COUNT(*) FROM trades WHERE account_id IS NULL AND created_at > date('now','-1 day');"
```

- [ ] Returns **0** for rows written since the update
      (older rows are legitimately NULL — they predate the column)

- [ ] Console printed `Database migrated: added trades.account_id, …`
- [ ] Row counts unchanged from before the update

**Back up first:** `copy data\quantbot.db data\quantbot.db.bak`

---

## Result

| Check | Pass | Notes |
|---|---|---|
| 1. risk_amount > 0 and plausible |  | |
| 2. Ledger == MT5 to the cent |  | drift = |
| 3. Server-day boundary |  | offset = |
| 4. Floating loss sampled |  | |
| 5. Money-based R |  | |
| 6. account_id + migration |  | |
