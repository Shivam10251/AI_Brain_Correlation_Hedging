-- =====================================================================
-- AI Hedge Fund Bot - persistent store
--
-- Design notes:
--   * Every table stores RAW DATA ONLY. No rendered charts are stored;
--     the dashboard reconstructs every graph from these rows.
--   * All timestamps are ISO-8601 UTC strings ("2026-09-07T12:00:00+00:00")
--     so they sort lexicographically and survive timezone changes.
--   * MT5 remains the source of truth for live account/position state.
--     These tables are a durable mirror + audit trail.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Strategy versions (Phase 1).
--
-- The strategy IS the prompt + model + temperature + params. Without a
-- version, two runs on different days are not comparable and no
-- experiment can be attributed. Every decisions row references one.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS strategy_versions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,

    strategy_id       TEXT    NOT NULL,        -- 'llm-mtf'
    version           TEXT    NOT NULL,        -- '1.0.0'

    -- sha256 of the rendered system prompt + the user-prompt template
    prompt_hash       TEXT    NOT NULL,

    model_alias       TEXT,                    -- 'deepseek-chat'
    temperature       REAL,
    params_json       TEXT,                    -- frozen config snapshot

    parent_version_id INTEGER REFERENCES strategy_versions(id),

    UNIQUE (strategy_id, version)
);

CREATE INDEX IF NOT EXISTS idx_strategy_hash
    ON strategy_versions(prompt_hash);


-- ---------------------------------------------------------------------
-- Broker profile, one row per MT5 account (Phase 1).
--
-- These are facts MEASURED from the terminal, never assumed. The server
-- UTC offset in particular is what every session label and (later) the
-- news blackout depends on.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS broker_profiles (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT,

    account_id            INTEGER NOT NULL UNIQUE,
    server                TEXT,
    company               TEXT,
    currency              TEXT,
    leverage              INTEGER,

    -- mt5.account_info().margin_mode: 0 netting, 2 hedging
    margin_mode           INTEGER,
    margin_mode_label     TEXT,

    -- round((tick.time - time.time()) / 1800) * 30, asserted stable
    server_utc_offset_min INTEGER,
    offset_samples_json   TEXT,
    offset_stable         INTEGER,

    trade_allowed         INTEGER,
    terminal_json         TEXT
);


-- ---------------------------------------------------------------------
-- Per-symbol broker specification (Phase 1).
--
-- Sizing, stop placement, deviation and filling mode are all derived
-- from these rather than hardcoded.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS symbol_profiles (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at              TEXT    NOT NULL,
    updated_at              TEXT,

    account_id              INTEGER NOT NULL,
    symbol                  TEXT    NOT NULL,

    digits                  INTEGER,
    point                   REAL,
    contract_size           REAL,

    trade_tick_size         REAL,
    trade_tick_value        REAL,
    trade_tick_value_profit REAL,
    trade_tick_value_loss   REAL,

    volume_min              REAL,
    volume_step             REAL,
    volume_max              REAL,

    filling_mode            INTEGER,        -- broker bitmask
    filling_mode_chosen     TEXT,           -- 'FOK' | 'IOC' | 'RETURN'
    filling_order_type      INTEGER,        -- mt5.ORDER_FILLING_*

    trade_stops_level       INTEGER,
    trade_freeze_level      INTEGER,
    spread                  INTEGER,
    trade_mode              INTEGER,

    swap_long               REAL,
    swap_short              REAL,

    UNIQUE (account_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_symbol_profiles
    ON symbol_profiles(account_id, symbol);


-- ---------------------------------------------------------------------
-- Risk profiles (Phase 3): the per-account rulebook, snapshotted.
--
-- The YAML under Specs/risk/ is the editable source; this table is the
-- immutable record of what was in force when a decision was made. An
-- edited profile registers a NEW row (new checksum) rather than
-- mutating the old one, so historical decisions keep their limits.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_profiles (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL,

    profile_id   TEXT    NOT NULL,        -- 'mt5-demo' | 'the5ers-100k'
    name         TEXT,
    checksum     TEXT    NOT NULL,        -- sha256 of the limits
    source_path  TEXT,
    params_json  TEXT    NOT NULL,

    UNIQUE (profile_id, checksum)
);


-- ---------------------------------------------------------------------
-- AI decisions: one row per DeepSeek analysis, valid or not.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,
    cycle_id          TEXT,
    symbol            TEXT    NOT NULL,
    timeframe         TEXT,
    model             TEXT,

    -- 'ok' | 'invalid' | 'error'
    status            TEXT    NOT NULL DEFAULT 'ok',

    -- structured AI output (Phase 5)
    daily_trend       TEXT,
    h1_trend          TEXT,
    momentum          TEXT,
    market_structure  TEXT,
    market_regime     TEXT,
    setup             TEXT,
    ai_score          INTEGER,

    -- what the AI asked for, and what the engine actually did
    ai_signal         TEXT,
    final_decision    TEXT,
    override_reason   TEXT,

    -- natural language kept verbatim for the UI
    reasoning         TEXT,
    daily_analysis    TEXT,
    h1_analysis       TEXT,

    -- provenance
    features_json     TEXT,
    news_json         TEXT,
    experience_ids    TEXT,
    raw_response      TEXT,
    latency_ms        INTEGER,
    error             TEXT,

    -- reproducibility (Phase 1): which exact strategy produced this
    prompt_hash         TEXT,
    temperature         REAL,
    strategy_version_id INTEGER REFERENCES strategy_versions(id),

    -- which MT5 account (Phase 2)
    account_id          INTEGER,

    -- Risk Engine verdict (Phase 3): all 14 checks, in order, with
    -- the outcome of each. A refusal must be explainable from the row.
    risk_checks_json    TEXT,
    risk_profile_id     INTEGER REFERENCES risk_profiles(id),

    -- Phase 4: feature formulas and the bar this decision was made on
    feature_version     TEXT,
    bar_time_utc        TEXT,

    trade_id          INTEGER REFERENCES trades(id)
);

CREATE INDEX IF NOT EXISTS idx_decisions_created  ON decisions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol   ON decisions(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_trade    ON decisions(trade_id);
CREATE INDEX IF NOT EXISTS idx_decisions_version  ON decisions(strategy_version_id);


-- ---------------------------------------------------------------------
-- Trades: one row per execution attempt (including rejections).
--
-- client_order_id is generated BEFORE the order is sent and is unique,
-- which is what makes execution idempotent across a crash/restart.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id   TEXT    NOT NULL UNIQUE,

    created_at        TEXT    NOT NULL,
    opened_at         TEXT,
    closed_at         TEXT,

    symbol            TEXT    NOT NULL,
    direction         TEXT    NOT NULL,
    volume            REAL,

    requested_price   REAL,
    entry_price       REAL,
    exit_price        REAL,
    stop_loss         REAL,
    take_profit       REAL,

    pnl               REAL,
    commission        REAL,
    swap              REAL,
    result            TEXT,
    r_multiple        REAL,

    -- Money at risk in ACCOUNT CURRENCY, computed before order_send
    -- (Phase 2). Price-based R alone cannot evaluate a single
    -- prop-firm rule, all of which are denominated in money.
    risk_amount       REAL,

    -- Stop geometry, kept so the spread drag is measurable later.
    --   stop_distance_price      |entry - sl|, what you lose if stopped
    --   stop_distance_effective  |marking price - sl|, how far the
    --                            market must move to stop you out
    stop_distance_price     REAL,
    stop_distance_effective REAL,
    spread_at_entry         REAL,

    -- Phase 5: how the trade ended, and the stop model that placed it.
    -- SL | TP | REVERSAL | FLATTEN | KILL | MANUAL
    exit_reason             TEXT,
    exit_client_order_id    TEXT,
    stop_model              TEXT,

    account_id        INTEGER,

    reason            TEXT,
    decision_id       INTEGER REFERENCES decisions(id),

    -- PENDING | EXECUTED | REJECTED | FAILED | CLOSED | ORPHANED
    execution_status  TEXT    NOT NULL DEFAULT 'PENDING',

    mt5_retcode       INTEGER,
    mt5_comment       TEXT,
    order_ticket      INTEGER,
    deal_ticket       INTEGER,
    position_ticket   INTEGER,
    magic             INTEGER,

    -- denormalised decision context so analytics can slice without a join
    market_regime     TEXT,
    setup             TEXT,
    ai_score          INTEGER,
    timeframe         TEXT,
    news_condition    TEXT,

    raw_result        TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_created   ON trades(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_symbol    ON trades(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_status    ON trades(execution_status);
CREATE INDEX IF NOT EXISTS idx_trades_position  ON trades(position_ticket);


-- ---------------------------------------------------------------------
-- Daily ledger (Phase 2): one row per account per TRADING DAY.
--
-- Every prop-firm rule is denominated in money over a day - 3% daily
-- loss, consistency, the max-loss floor. None of them could be
-- evaluated before this table existed (Phase 0 §2.4).
--
-- trading_day is the BROKER SERVER's day (UTC shifted by
-- server_utc_offset_min), because that is the day the firm computes
-- its limits on. Storing UTC days here would silently misalign the
-- daily loss limit by the offset.
--
-- realized_pnl is maintained incrementally as trades close, then
-- reconciled against MT5's own deal history every cycle. MT5 wins.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_ledger (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT,

    account_id        INTEGER NOT NULL,
    trading_day       TEXT    NOT NULL,       -- 'YYYY-MM-DD', server time

    -- Realised, net of costs. gross_pnl excludes them.
    realized_pnl      REAL    NOT NULL DEFAULT 0,
    gross_pnl         REAL    NOT NULL DEFAULT 0,
    commission        REAL    NOT NULL DEFAULT 0,
    swap              REAL    NOT NULL DEFAULT 0,

    -- Open risk. The daily limit is breached by realised + floating,
    -- so the floating low-water mark has to be tracked separately.
    max_floating_loss REAL    NOT NULL DEFAULT 0,

    start_equity      REAL,
    start_balance     REAL,
    max_equity        REAL,
    min_equity        REAL,
    eod_equity        REAL,
    eod_balance       REAL,

    trade_count       INTEGER NOT NULL DEFAULT 0,
    win_count         INTEGER NOT NULL DEFAULT 0,
    loss_count        INTEGER NOT NULL DEFAULT 0,

    -- Reconciliation against MT5 deal history.
    reconciled_at     TEXT,
    drift             REAL,

    UNIQUE (account_id, trading_day)
);

CREATE INDEX IF NOT EXISTS idx_ledger_day
    ON daily_ledger(account_id, trading_day DESC);


-- ---------------------------------------------------------------------
-- Decision bar snapshots (Phase 4).
--
-- The EXACT bars each decision saw, keyed by decision_id. This is what
-- makes offline replay possible: Phase 8 can re-run a changed stop
-- rule, threshold or feature formula over real history without a
-- single new API call, and without hoping MT5 still serves the same
-- bars months later.
--
-- Stored as raw MT5 rows (epoch seconds, server time) so
-- compute_features can be re-run against them byte-for-byte.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_bars (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at            TEXT    NOT NULL,

    decision_id           INTEGER UNIQUE REFERENCES decisions(id),
    symbol                TEXT    NOT NULL,

    feature_version       TEXT,
    server_utc_offset_min INTEGER,

    daily_json            TEXT    NOT NULL,   -- 10 D1 rows, closed
    hourly_json           TEXT    NOT NULL,   -- 24 H1 rows, closed
    tick_json             TEXT,
    symbol_info_json      TEXT,

    -- The features that were actually stored, so a replay can be
    -- compared against them rather than trusted.
    features_json         TEXT
);

CREATE INDEX IF NOT EXISTS idx_decision_bars_symbol
    ON decision_bars(symbol, created_at DESC);


-- ---------------------------------------------------------------------
-- Equity snapshots: raw points for the equity curve.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    equity          REAL    NOT NULL,
    balance         REAL,
    margin          REAL,
    margin_free     REAL,
    floating_pnl    REAL,
    open_positions  INTEGER,
    account_login   INTEGER,
    currency        TEXT,
    account_id      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_equity_created ON equity_snapshots(created_at DESC);


-- ---------------------------------------------------------------------
-- Market state: derived features per symbol per cycle.
-- Candles themselves are NOT stored (MT5 already has them); we store the
-- computed features the AI actually reasoned over.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_states (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    bid               REAL,
    ask               REAL,
    spread            REAL,
    last_close        REAL,
    atr               REAL,
    atr_pct           REAL,
    volatility_bucket TEXT,
    daily_trend       TEXT,
    h1_trend          TEXT,
    market_regime     TEXT,
    session           TEXT,
    features_json     TEXT,
    account_id        INTEGER,

    -- Phase 4: which formulas produced these numbers, and the bar the
    -- decision was made on (both clocks).
    feature_version   TEXT,
    bar_time_utc      TEXT,
    bar_time_server   TEXT
);

CREATE INDEX IF NOT EXISTS idx_market_symbol ON market_states(symbol, created_at DESC);


-- ---------------------------------------------------------------------
-- Experiences: distilled outcome of a finished trade (Phase 8).
-- These are what get retrieved and fed back into the next prompt.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiences (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        TEXT    NOT NULL,
    trade_id          INTEGER UNIQUE REFERENCES trades(id),

    symbol            TEXT    NOT NULL,
    timeframe         TEXT,
    market_regime     TEXT,
    setup             TEXT,
    direction         TEXT,

    ai_score          INTEGER,
    score_bucket      TEXT,
    volatility_bucket TEXT,
    session           TEXT,
    news_condition    TEXT,

    entry_price       REAL,
    exit_price        REAL,
    r_multiple        REAL,
    pnl               REAL,
    outcome           TEXT,
    holding_minutes   REAL,

    lesson            TEXT,
    context_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_exp_symbol  ON experiences(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exp_regime  ON experiences(symbol, market_regime, setup);


-- ---------------------------------------------------------------------
-- Events: durable execution feed / audit log.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    level       TEXT    NOT NULL DEFAULT 'INFO',
    category    TEXT,
    symbol      TEXT,
    message     TEXT    NOT NULL,
    trade_id    INTEGER,
    decision_id INTEGER,
    data_json   TEXT,
    account_id  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);


-- ---------------------------------------------------------------------
-- Engine state: small durable key/value store (interval, run state,
-- single-writer lock).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS engine_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
