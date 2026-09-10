You are an expert quantitative trading engineer, institutional hedge fund systems architect, and full-stack Python developer.

Build a complete, production-grade, 24/7 Autonomous AI Hedge Fund Trading System and Web Dashboard in Python using FastAPI, MetaTrader 5 (MT5), and DeepSeek LLM (deepseek-chat) with an integrated Self-Learning Auditor Loop.

The system must run autonomously, evaluate multiple assets across multiple timeframes, dynamically size positions based on ATR volatility, enforce strict duplicate and reversal guards, record all execution context, automatically reconcile closed trades from MT5 deal history, analyze loss patterns using an LLM auditor, persist learned rules, and inject those rules into future AI trading decisions to penalize bad setups.

Below is the complete architectural specification, schema definitions, logic, and required files. Generate all files with complete, working, production-ready code. Do not output placeholders or omissions.

================================================================================
PROJECT FILE STRUCTURE
================================================================================
.
├── .env.example
├── config.py
├── data_engine.py
├── ai_brain.py
├── execution.py
├── memory_store.py
├── auditor.py
├── main.py
├── templates/
│   └── index.html
├── run_server.bat
├── start_background.ps1
├── start_background.bat
├── stop_background.ps1
├── stop_background.bat
└── requirements.txt

================================================================================
DETAILED FILE SPECIFICATIONS
================================================================================

1. requirements.txt
Include all required libraries:
- fastapi
- uvicorn[standard]
- pydantic
- jinja2
- python-dotenv
- MetaTrader5
- pandas
- pandas_ta
- requests

2. .env.example
Provide all required configuration keys with helpful documentation comments:
- DEEPSEEK_API_KEY=your_deepseek_api_key_here
- DEEPSEEK_API_BASE=https://api.deepseek.com
- DEEPSEEK_MODEL=deepseek-chat
- MT5_LOGIN=your_mt5_account_number
- MT5_PASSWORD=your_mt5_password
- MT5_SERVER=Exness-MT5Trial7
- MT5_PATH=
- DEFAULT_RISK_PERCENT=1.0
- SCAN_INTERVAL_SECONDS=30
- CONFIDENCE_THRESHOLD=65
- SYMBOLS=EURUSDm,GBPUSDm,USDJPYm,USDCHFm,USDCADm,AUDUSDm,NZDUSDm,BTCUSDm,XAUUSDm
- AUDIT_TRADE_THRESHOLD=10
- AUDIT_COOLDOWN_HOURS=12

3. config.py
- Use python-dotenv to load .env.
- Define strongly typed variables with sane defaults for: DEEPSEEK_API_KEY, DEEPSEEK_API_BASE, DEEPSEEK_MODEL, MT5_LOGIN (int), MT5_PASSWORD (str), MT5_SERVER (str), MT5_PATH (str or None), DEFAULT_RISK_PERCENT (float, default 1.0), SCAN_INTERVAL_SECONDS (int, default 30), CONFIDENCE_THRESHOLD (int, default 65), and SYMBOLS (list of str parsed from a comma-separated string).
- Set TIMEFRAMES to mt5.TIMEFRAME_H1 and mt5.TIMEFRAME_D1.
- Set technical indicator parameters: EMA_PERIOD=200, RSI_PERIOD=14, ATR_PERIOD=14, VOL_MA_PERIOD=20.
- Set AUDIT_TRADE_THRESHOLD (default 10), AUDIT_COOLDOWN_HOURS (default 12), and paths BASE_DIR, MEMORY_FILE (memory.json), RULES_FILE (new_rules.json).

4. data_engine.py
- initialize_mt5(): call mt5.initialize(path=config.MT5_PATH) or default mt5.initialize(). If MT5_LOGIN is provided, call mt5.login(config.MT5_LOGIN, password=config.MT5_PASSWORD, server=config.MT5_SERVER). Ensure connection succeeds and log account equity, balance, and server details.
- calculate_indicators(df): Given OHLCV columns [time, open, high, low, close, tick_volume], calculate EMA(200) on close, RSI(14) using exponential smoothing, ATR(14) using true range max(H-L, |H-prevC|, |L-prevC|), and relative volume = tick_volume / tick_volume.rolling(20).mean(). Return latest scalar values plus recent 10-bar price trend/structure.
- fetch_multi_timeframe_data(symbol): enable the symbol in MarketWatch; fetch bid, ask, and spread; fetch 250 H1 bars and 250 Daily bars; compute indicators; obtain live account equity and balance; return a structured dictionary containing symbol, bid, ask, spread, equity, balance, h1_data, and daily_data.
- fetch_correlated_asset_prices(current_symbol, all_symbols): for all other configured symbols, fetch current tick and return {sym: {bid, ask}}.

5. memory_store.py
- Store trade history and market snapshots in memory.json.
- append_trade_memory(record): load memory.json or create an empty list; append the record including ticket/deal/order, entry price, SL, TP, volume, and full market_context (H1/D1 ATR, relative volume, tick volume, correlated prices); save formatted JSON.
- load_trade_memory(): return the stored list.
- reconcile_closed_trades(): scan CONFIRMED records without exit_deal; query MT5 historical deals using position/order ticket; filter exit deals where deal.entry == 1 (out of position); extract exit deal ticket, exit price/time, realized P/L, and outcome (WIN, LOSS, or BREAKEVEN); update records to CLOSED and persist them. Return newly reconciled count.

6. auditor.py — The Self-Learning Core
- load_rules_document(): read new_rules.json. If missing or invalid, return {"rules": [], "last_audit_at": None, "trades_analyzed": 0}.
- save_rules_document(doc): persist the document.
- run_audit(force=False): reconcile first; use the last 50 closed trades with realized P/L; return insufficient_trades when fewer than 3 trades exist; respect AUDIT_COOLDOWN_HOURS unless forced; isolate losses; return no_losses when none exist.
- Summarize loss trades with symbol, side, prices, realized P/L, H1/D1 relative volume, H1/D1 ATR, and correlated-asset prices. Ask DeepSeek, acting as Chief Risk Officer and Quantitative Auditor, to identify recurring loss patterns: low-volume breakdowns/breakouts, overextended ATR or volatility traps, and adverse cross-asset correlations.
- Require raw JSON rules shaped as: affected_symbol, setup, confidence_reduction_points (15–30), sample_size, and evidence. Merge them into new_rules.json, set last_audit_at, and return an audit summary.

7. ai_brain.py
- load_learned_rules(symbol): return active rules targeting the symbol or ALL.
- get_ai_decision(market_data, symbol): load active learned rules and include an ACTIVE RISK AUDIT RULES section that mandates specified confidence penalties when conditions match.
- Call DeepSeek Chat /chat/completions with temperature 0.1. Supply Daily and H1 EMA(200), RSI(14), ATR(14), relative volume, ask, bid, spread, and the active learned rules.
- The system prompt must enforce a strict risk-managed hedge-fund persona and raw JSON only:
  {"signal":"BUY" | "SELL" | "HOLD", "confidence_score": 0 to 100 integer, "stop_loss": float, "take_profit": float, "logic":"concise explanation including indicators and learned-rule deductions"}
- If confidence_score is below CONFIDENCE_THRESHOLD, force HOLD. Validate geometric stops: for BUY, SL < price < TP; for SELL, TP < price < SL. When invalid, use 1.5x ATR for SL and 3.0x ATR for TP.

8. execution.py
- calculate_position_size(symbol, stop_loss_price, risk_percent, equity): get symbol info; calculate monetary risk = equity * (risk_percent / 100.0); use price distance, contract size, tick value/size; clamp between volume_min and volume_max; round to volume_step.
- get_open_positions(symbol=None): use mt5.positions_get.
- close_position(ticket_or_pos): fetch position; submit the opposite BUY/SELL order using compatible IOC/FOK/RETURN type filling; return a result dictionary.
- execute_trade(symbol, signal, stop_loss, take_profit, risk_percent): obtain tick and equity; calculate volume; create MT5 TRADE_ACTION_DEAL BUY/SELL request with SL/TP; send it; require TRADE_RETCODE_DONE or raise a useful exception; return deal, order, price, volume, SL, and TP.

9. main.py — FastAPI Server & Autonomous Engine
- Initialize FastAPI and Jinja2Templates.
- Create global bot_state with: is_running False, interval config.SCAN_INTERVAL_SECONDS, risk_percent config.DEFAULT_RISK_PERCENT, equity 0.0, last_logic empty, last_confidence 0, last_signal HOLD, last_entry_price None, last_stop_loss None, last_take_profit None, trade_history [], open_positions [], learned_rules [], last_audit_at None.
- Endpoints: GET / renders index.html; GET /api/status updates equity, positions, and learned rules; POST /api/control accepts action start/stop, interval, risk_percent; POST /api/close accepts ticket; POST /api/audit accepts force; GET /api/rules returns new_rules.json.
- Create an infinite asyncio trading_loop. When running, loop through config.SYMBOLS. Fetch market data and AI decision. For BUY/SELL: prevent duplicate same-direction positions with [DUPLICATE PREVENTED]; when an opposite position exists, close it first via [REVERSAL]. Execute valid trades, record memory, reconcile closed trades after each complete scan, force an audit for newly reconciled trades, update bot_state learned rules, then sleep for the configured interval.
- At startup initialize MT5, reconcile prior trades, load active rules, check whether an audit is due, and spawn trading_loop as a background task.

10. templates/index.html
- Build a high-performance, modern Bloomberg/Terminal-inspired dashboard using Inter, dark navy/slate #090d14, and responsive grid layout.
- Add a top bar with brand, scan interval selector (30s, 1m, 5m, 1h), risk-per-trade input, and Start/Stop Engine toggle.
- Add four metric cards: live account equity, engine status with pulsing green/gray indicator, AI confidence, and confirmed fills count.
- Add Decision Intelligence: BUY/SELL/HOLD signal badge, model rationale, entry reference, ATR SL, and ATR TP tags; a Risk Framework Guardrails card; a Portfolio Exposure Table with ticket, symbol, side, volume, prices, SL, TP, floating P/L, and individual Close buttons.
- Add a Self-Learning Desk with active-rule count, Run Audit Now, and a table of symbol, discovered pattern, penalty, sample size, auditor evidence, and status. Add Confirmed Fills Ledger with local time, symbol, side, fill, SL, TP, volume, risk %, deal ID, and status.
- Add an HTML5 Canvas topology for pulsing connected recent-trade nodes. Poll /api/status every three seconds. Include JS to control engine/risk/interval, close positions with confirmation, and run the audit.

11. Windows 24/7 Process Management Scripts
- run_server.bat: navigate to the project and launch .venv\Scripts\python.exe main.py >> server.log 2>&1.
- start_background.ps1: use WMI Invoke-CimMethod Win32_Process Create to spawn cmd.exe /c run_server.bat detached beneath WmiPrvSE.exe so it survives terminal closure.
- start_background.bat: a double-clickable shortcut that invokes start_background.ps1 with ExecutionPolicy Bypass.
- stop_background.ps1 and stop_background.bat: gracefully locate and terminate python/cmd processes running main.py or listening on port 8000.

Ensure clean code formatting, robust exception handling around all MT5 and API network calls, and clear console logs with prefixes: [SYSTEM], [AI], [TRADE], [DUPLICATE PREVENTED], [REVERSAL], [LEARNING], and [AUDITOR].