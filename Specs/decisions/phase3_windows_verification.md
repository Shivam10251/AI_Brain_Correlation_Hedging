# Phase 3 — live verification on the Windows terminal

Green in CI (170 pytest items). These need the real terminal.

**Read this first:** Phase 3 changes what the bot will and will not
trade. The default `mt5-demo` profile enforces **score ≥ 70**, **one
position per symbol**, **no hedging**, **0.25% risk**, **2% daily loss**
and **3 losses/day**. Before this, four of those did not exist and the
bot stacked positions every cycle. **Expect far fewer trades.** That is
the point, not a fault.

---

## 1. The profile loaded and is the one you meant

Startup console:

- [ ] `Risk profile: mt5-demo (id=…, checksum …) · score>=70 · risk 0.25% · daily 2% · 3 losses/day · cap 1/symbol`

```
curl -H "Authorization: Bearer %API_TOKEN%" http://127.0.0.1:8000/api/risk/state
```

- [ ] `profile.profile_id` is what you expect
- [ ] `limits.risk_budget_per_trade` ≈ 0.25% of your balance
- [ ] `limits.daily_loss_allowance` ≈ 2% of your balance
- [ ] `checks` lists all **14** names

> If the profile fails to load, the console says so and an ERROR event
> is written. The engine then refuses every trade. That is deliberate.

---

## 2. Refusals are explainable from the row

Let it run a few cycles, then:

```
sqlite3 data\quantbot.db "SELECT symbol, ai_signal, final_decision, substr(override_reason,1,90) FROM decisions WHERE override_reason IS NOT NULL ORDER BY id DESC LIMIT 10;"
```

- [ ] Every override has a specific reason naming the failing check
      (score floor, hedging, cap, daily loss, …) — never a generic one

```
sqlite3 data\quantbot.db "SELECT COUNT(*) FROM decisions WHERE risk_checks_json IS NULL AND ai_signal IN ('BUY','SELL');"
```

- [ ] Returns **0** — every real signal carries the full 14-check verdict

---

## 3. The kill switch stops orders within one cycle

```
curl -X POST -H "Authorization: Bearer %API_TOKEN%" -H "Content-Type: application/json" ^
  -d "{\"action\":\"halt\",\"reason\":\"drill\"}" http://127.0.0.1:8000/api/risk/halt
```

- [ ] Next cycle logs `Kill switch active: drill` as the override reason
- [ ] **No new orders appear in the MT5 terminal**

Now the part that matters:

- [ ] **Stop the backend entirely (Ctrl+C) and restart it.** The
      console must warn `KILL SWITCH IS ENGAGED`. It is durable on
      purpose — a halt that evaporates on crash is not a halt.

```
curl -X POST -H "Authorization: Bearer %API_TOKEN%" -H "Content-Type: application/json" ^
  -d "{\"action\":\"resume\"}" http://127.0.0.1:8000/api/risk/halt
```

- [ ] Trading resumes on the next cycle

> `flatten=true` reports `unavailable` — the close path arrives in
> Phase 5. New orders are blocked; existing positions are untouched.

---

## 4. The hedging ban

With one position open on a symbol, wait for the AI to flip direction
on it.

- [ ] The decision is refused with
      `hedging is prohibited. Close first (reversal), do not open the opposite side.`
- [ ] **No opposite position is opened in the terminal**

This matters on a NETTING account too: an opposite order there would
silently reduce or close the position instead — a different trade from
the one the AI asked for.

---

## 5. The position cap

- [ ] With one position open on a symbol, a second BUY on that symbol
      is refused with `is at the cap of 1`
- [ ] Other symbols still trade normally

---

## 6. Money limits (only if you can reach them safely)

Do **not** force a real loss to test these. Instead read the projections:

```
curl -H "Authorization: Bearer %API_TOKEN%" http://127.0.0.1:8000/api/risk/state
```

- [ ] `today.exposure` matches realised + floating in the terminal
- [ ] `limits.daily_loss_remaining` shrinks as the day goes against you

To test the refusal safely, temporarily edit `Specs/risk/mt5-demo.yaml`
to a tiny `daily_loss_limit_pct` (e.g. `0.05`), restart, and confirm:

- [ ] Trades are refused with `past the allowance of -…`
- [ ] **Restore the file afterwards**

---

## 7. Nothing regressed

- [ ] Trades still execute when all checks pass
- [ ] `risk_amount` still populated (Phase 2)
- [ ] Ledger still reconciles with drift 0.00
- [ ] Dashboards still load

---

## The important judgement call

After a day or two:

```
sqlite3 data\quantbot.db "SELECT json_extract(value,'$') FROM (SELECT override_reason AS value FROM decisions WHERE override_reason IS NOT NULL);"
```

or more simply:

```
sqlite3 data\quantbot.db "SELECT substr(override_reason,1,40) AS reason, COUNT(*) FROM decisions WHERE override_reason IS NOT NULL GROUP BY reason ORDER BY 2 DESC;"
```

- [ ] Look at what is refusing most. If **score floor** dominates and
      you are getting almost no trades, the Phase 6 calibration run
      will take too long to reach 200 closed trades — tell me and we
      lower `min_ai_score` for the study (it is a profile edit, not a
      code change).

---

## Result

| Check | Pass | Notes |
|---|---|---|
| 1. Profile loaded |  | |
| 2. Verdicts persisted |  | |
| 3. Kill switch + durability |  | |
| 4. Hedging ban |  | |
| 5. Position cap |  | |
| 6. Money limits |  | |
| 7. No regression |  | |
| Trades/day after gates |  | |
