# Phase 1 — live verification on the Windows terminal

Everything in Phase 1 is green in CI against `tests/fake_mt5.py`
(55 pytest items, including all 79 original checks). The checks below
are the ones a fake broker **cannot** answer. They need the real MT5
terminal.

Run them before Phase 2 starts.

---

## 0. Setup

```bat
cd C:\path\to\AI_Brain_Correlation_Hedging
git pull
venv\Scripts\activate
pip install -r requirements.txt
pip install MetaTrader5==5.0.4874
```

Create `.env` from the template (note: **no leading dot** on the
template file, because `.gitignore` excludes `.env.*`):

```bat
copy env.example .env
```

Generate and set a token:

```bat
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put it in `.env` as `API_TOKEN=...`, and set `DEEPSEEK_API_KEY`.

Start it:

```bat
start_bot.bat
```

---

## 1. Startup log — broker facts are MEASURED, not assumed

**Acceptance:** *"startup event logs `server_utc_offset_min`,
`margin_mode`, and per-symbol filling mode."*

- [ ] Console shows `Strategy: llm-mtf v1.0.0 (id=…, prompt …, model …, temp 0.1)`
- [ ] Console shows `Broker profile: account <login> on <server> · server offset ±N min · RETAIL_NETTING|RETAIL_HEDGING`
- [ ] One line per symbol: `EURUSDm: filling=FOK|IOC|RETURN digits=… stops_level=… volume_step=…`

**Write the offset down here:** `server_utc_offset_min = ______`

> If it prints `UNKNOWN` or `(UNSTABLE)`, stop and tell me. Every
> session label and the whole Phase 7 news engine depend on this
> number. `UNSTABLE` usually means the sampled symbols' markets are
> closed, so retry during market hours.

**If any symbol is reported unavailable** — the console prints
`[ERROR] unavailable on this broker: …` — those names are wrong in
`SYMBOLS`. **This is the most likely explanation for "only XAUUSD
trades".** Fix the names in `.env` and restart.

---

## 2. Auth

**Acceptance:** *"unauthenticated `POST /api/control` → 401."*

```bat
curl -i -X POST http://127.0.0.1:8000/api/control -H "Content-Type: application/json" -d "{\"action\":\"stop\"}"
```

- [ ] Returns **401**
- [ ] `curl http://127.0.0.1:8000/api/health` returns **200** (watchdogs need it open)
- [ ] Both dashboards still load and populate at `/` and `/dashboard`
      — they receive the token server-side, so nothing should look broken

---

## 3. Filling mode — an order is accepted on EVERY symbol

**Acceptance:** *"a demo market order is accepted on every symbol in
`SYMBOLS`."* This is the check the fake broker cannot make for you.

Click **Initialize Engine**, let it run until each symbol has traded at
least once (or force it with `MIN_SCORE_TO_TRADE=0`).

```
sqlite3 data\quantbot.db "SELECT symbol, execution_status, mt5_retcode, COUNT(*) FROM trades GROUP BY symbol, execution_status, mt5_retcode;"
```

- [ ] Every symbol has at least one `EXECUTED` row
- [ ] **No row has `mt5_retcode = 10030`** (Invalid fill) — that would
      mean the filling-mode resolution picked a mode this broker
      refuses, and I need to know

---

## 4. Decision provenance

**Acceptance:** *"every new `decisions` row has `prompt_hash`, `model`,
`strategy_version_id` non-null."*

```
sqlite3 data\quantbot.db "SELECT COUNT(*) FROM decisions WHERE prompt_hash IS NULL OR model IS NULL OR strategy_version_id IS NULL;"
```

- [ ] Returns **0**

```
sqlite3 data\quantbot.db "SELECT id, strategy_id, version, substr(prompt_hash,1,12), model_alias, temperature FROM strategy_versions;"
```

- [ ] Exactly one row: `llm-mtf | 1.0.0 | <hash> | deepseek-chat | 0.1`
- [ ] If you see a `1.0.0+hash.xxxxxxxx` row, the prompt was edited
      without bumping the version — that is the system telling you so,
      not a bug

Check whether the served model differs from the alias:

```
sqlite3 data\quantbot.db "SELECT DISTINCT model FROM decisions;"
```

---

## 5. Migration safety — your existing data survived

If you already had `data\quantbot.db` before this update:

- [ ] Console printed `Database migrated: added decisions.prompt_hash, …`
- [ ] Row counts are unchanged:

```
sqlite3 data\quantbot.db "SELECT (SELECT COUNT(*) FROM trades) AS trades, (SELECT COUNT(*) FROM decisions) AS decisions, (SELECT COUNT(*) FROM equity_snapshots) AS equity;"
```

- [ ] Old rows have `prompt_hash IS NULL` (expected — they predate versioning)

**Take a backup first:** `copy data\quantbot.db data\quantbot.db.bak`

---

## 6. Deviation is instrument-correct

```
sqlite3 data\quantbot.db "SELECT symbol, digits, point, trade_stops_level, filling_mode_chosen FROM symbol_profiles;"
```

- [ ] `BTCUSDm` shows `digits=2`, `XAUUSDm` shows `digits=3`,
      FX shows `digits=5` (or 3/5 depending on broker)
- [ ] Console `Deviation: N points` differs per symbol in the
      `ATTEMPTING MT5 TRADE` block — the old fixed 20 is gone

---

## 7. Regression — nothing that worked before is broken

- [ ] Restart the backend. Trade feed, equity curve, P&L and AI
      reasoning are all still there (dashboard does not start blank)
- [ ] `GET /api/positions` vs `GET /api/history/trades`: every live
      position with magic `100001` has a trade row
- [ ] No duplicate order was sent on restart — check the MT5 terminal's
      Trade tab, not just the dashboard

---

## Still open from the original handoff

**RISK A — position ticket resolution.** Unchanged by Phase 1 and still
unverified on a real broker. After one trade executes:

```
sqlite3 data\quantbot.db "SELECT id, order_ticket, deal_ticket, position_ticket FROM trades ORDER BY id DESC LIMIT 1;"
```

- [ ] `position_ticket` matches the position ticket in the MT5 terminal

If they differ, trades will never close in the database — no P&L, no
experiences, no analytics. Tell me and I will fix the resolution before
Phase 2.

---

## Result

| Check | Pass | Notes |
|---|---|---|
| 1. Startup log |  | offset = |
| 2. Auth 401 |  | |
| 3. Order on every symbol |  | |
| 4. Provenance non-null |  | |
| 5. Migration safe |  | |
| 6. Deviation per instrument |  | |
| 7. Regression |  | |
| RISK A ticket resolution |  | |
