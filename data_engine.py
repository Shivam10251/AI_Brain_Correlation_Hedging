import MetaTrader5 as mt5
import pandas as pd


# =====================================================================
# FEATURE COMPUTATION
#
# These are deterministic, engine-side measurements of the market.
# They are stored in market_states and also handed to the AI as
# context. They are NOT the AI's opinion - the model returns its own
# labels separately, and the two are kept apart in the database.
# =====================================================================

def _true_ranges(df):
    """True Range series from an OHLC dataframe."""

    ranges = []

    closes = df["close"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()

    for i in range(len(df)):

        if i == 0:
            ranges.append(highs[i] - lows[i])
            continue

        previous_close = closes[i - 1]

        ranges.append(
            max(
                highs[i] - lows[i],
                abs(highs[i] - previous_close),
                abs(lows[i] - previous_close),
            )
        )

    return ranges


def _average_true_range(df, period=14):
    """Simple ATR. Falls back to the mean of whatever data we have."""

    ranges = _true_ranges(df)

    if not ranges:
        return None

    window = ranges[-period:] if len(ranges) >= period else ranges

    return sum(window) / len(window)


def _percentile(values, fraction):
    """Nearest-rank percentile; avoids a numpy dependency here."""

    if not values:
        return None

    ordered = sorted(values)

    index = min(
        len(ordered) - 1,
        max(0, int(round(fraction * (len(ordered) - 1)))),
    )

    return ordered[index]


def _volatility_bucket(df):
    """
    Classify current volatility RELATIVE TO THIS SYMBOL'S own recent
    behaviour, so the label means the same thing for EURUSD and BTC.
    """

    ranges = _true_ranges(df)

    if len(ranges) < 5:
        return "unknown"

    current = ranges[-1]

    low_cut = _percentile(ranges, 0.25)
    high_cut = _percentile(ranges, 0.75)

    if current >= high_cut:
        return "high"

    if current <= low_cut:
        return "low"

    return "normal"


def _trend_label(df):
    """
    Coarse trend label from close-price drift and swing structure.

    Intentionally simple and explainable - this is a feature for the
    prompt and for slicing analytics, not a trading signal on its own.
    """

    closes = df["close"].tolist()

    if len(closes) < 3:
        return "unknown"

    first = closes[0]
    last = closes[-1]

    if first == 0:
        return "unknown"

    change_pct = (last - first) / abs(first) * 100.0

    # Threshold scaled to the instrument's own range so it is not
    # tuned to any single asset class.
    span = max(df["high"]) - min(df["low"])

    threshold = (span / abs(first) * 100.0) * 0.20 if first else 0.1

    if change_pct > threshold:
        return "bullish"

    if change_pct < -threshold:
        return "bearish"

    return "neutral"


def _market_structure(df):
    """Higher highs/lows vs lower highs/lows over the last swings."""

    if len(df) < 4:
        return "unknown"

    highs = df["high"].tolist()
    lows = df["low"].tolist()

    midpoint = len(highs) // 2

    recent_high = max(highs[midpoint:])
    earlier_high = max(highs[:midpoint])

    recent_low = min(lows[midpoint:])
    earlier_low = min(lows[:midpoint])

    if recent_high > earlier_high and recent_low > earlier_low:
        return "higher_highs"

    if recent_high < earlier_high and recent_low < earlier_low:
        return "lower_lows"

    if recent_high > earlier_high and recent_low < earlier_low:
        return "expanding"

    return "compressing"


def _market_regime(daily_df, hourly_df):
    """
    Trending vs ranging, from how much of the total travelled distance
    turned into net directional movement (an efficiency ratio).
    """

    closes = hourly_df["close"].tolist()

    if len(closes) < 3:
        return "unknown"

    net_movement = abs(closes[-1] - closes[0])

    total_movement = sum(
        abs(closes[i] - closes[i - 1])
        for i in range(1, len(closes))
    )

    if total_movement == 0:
        return "flat"

    efficiency = net_movement / total_movement

    if efficiency > 0.45:
        return "trending"

    if efficiency < 0.20:
        return "ranging"

    return "choppy"


def _momentum(df):
    """Direction and strength of the most recent leg."""

    closes = df["close"].tolist()

    if len(closes) < 6:
        return "unknown"

    recent = closes[-3:]
    prior = closes[-6:-3]

    recent_avg = sum(recent) / len(recent)
    prior_avg = sum(prior) / len(prior)

    if prior_avg == 0:
        return "unknown"

    change_pct = (recent_avg - prior_avg) / abs(prior_avg) * 100.0

    atr = _average_true_range(df)

    # Normalise the move against ATR so "strong" is comparable across
    # instruments.
    if atr and prior_avg:
        atr_pct = atr / abs(prior_avg) * 100.0
    else:
        atr_pct = 0.0

    if atr_pct == 0:
        return "neutral"

    ratio = change_pct / atr_pct

    if ratio > 1.0:
        return "strong_bullish"

    if ratio > 0.3:
        return "bullish"

    if ratio < -1.0:
        return "strong_bearish"

    if ratio < -0.3:
        return "bearish"

    return "neutral"


def _session(timestamp):
    """Rough FX session label from the UTC hour."""

    hour = timestamp.hour

    if hour < 7:
        return "asian"

    if hour < 12:
        return "london"

    if hour < 16:
        return "london_ny_overlap"

    if hour < 21:
        return "new_york"

    return "off_hours"


def compute_features(daily_df, hourly_df, tick, symbol_info):
    """
    Bundle every derived feature into one flat dict.

    Stored in market_states.features_json and summarised into the AI
    prompt.
    """

    last_close = float(hourly_df["close"].iloc[-1])

    atr = _average_true_range(hourly_df)

    atr_pct = (atr / last_close * 100.0) if (atr and last_close) else None

    bid = getattr(tick, "bid", None)
    ask = getattr(tick, "ask", None)

    spread = (ask - bid) if (ask is not None and bid is not None) else None

    return {
        "symbol": symbol_info.name if symbol_info else None,

        "bid": bid,
        "ask": ask,
        "spread": spread,
        "spread_pct": (
            (spread / last_close * 100.0)
            if (spread is not None and last_close) else None
        ),

        "last_close": last_close,

        "daily_close": float(daily_df["close"].iloc[-1]),
        "daily_high_10": float(max(daily_df["high"])),
        "daily_low_10": float(min(daily_df["low"])),

        "h1_high_24": float(max(hourly_df["high"])),
        "h1_low_24": float(min(hourly_df["low"])),

        "atr_h1": atr,
        "atr_pct": atr_pct,
        "volatility_bucket": _volatility_bucket(hourly_df),

        "daily_trend": _trend_label(daily_df),
        "h1_trend": _trend_label(hourly_df),

        "momentum": _momentum(hourly_df),
        "market_structure": _market_structure(hourly_df),
        "market_regime": _market_regime(daily_df, hourly_df),

        "session": _session(hourly_df["time"].iloc[-1]),

        "digits": symbol_info.digits if symbol_info else None,
        "point": symbol_info.point if symbol_info else None,
    }


# =====================================================================
# ACCOUNT SNAPSHOT
# =====================================================================

def fetch_account_snapshot():
    """
    Current account state straight from MT5.

    MT5 stays the source of truth; we only mirror this into the
    equity_snapshots table.
    """

    account_info = mt5.account_info()

    if account_info is None:
        raise RuntimeError(
            f"Could not retrieve account information: {mt5.last_error()}"
        )

    positions = mt5.positions_get()

    open_positions = len(positions) if positions else 0

    return {
        "equity": account_info.equity,
        "balance": account_info.balance,
        "margin": account_info.margin,
        "margin_free": account_info.margin_free,
        "floating_pnl": account_info.profit,
        "open_positions": open_positions,
        "account_login": account_info.login,
        "currency": account_info.currency,
    }


# =====================================================================
# MARKET DATA
# =====================================================================

def fetch_multi_timeframe_data(symbol):
    """
    Fetch market data for a symbol across multiple timeframes.

    Daily:
        Last 10 candles -> macro trend

    H1:
        Last 24 candles -> micro momentum

    Returns:
        {
            "symbol": symbol,
            "daily_csv": str,
            "hourly_csv": str,
            "equity": float,
            "ask_price": float,

            # added for persistence / AI context
            "bid_price": float,
            "features": dict,
            "account": dict
        }
    """

    # Make sure the symbol is available in Market Watch
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Could not select symbol: {symbol}")

    # ---------------------------------------------------------
    # 1. Fetch Daily data - last 10 candles
    # ---------------------------------------------------------
    daily_rates = mt5.copy_rates_from_pos(
        symbol,
        mt5.TIMEFRAME_D1,
        0,
        10
    )

    if daily_rates is None or len(daily_rates) == 0:
        raise RuntimeError(
            f"Could not fetch Daily data for {symbol}: {mt5.last_error()}"
        )

    daily_df = pd.DataFrame(daily_rates)

    # Convert Unix timestamp to readable UTC datetime
    daily_df["time"] = pd.to_datetime(
        daily_df["time"],
        unit="s",
        utc=True
    )

    # Convert to raw CSV text
    daily_csv = daily_df.to_csv(index=False)

    # ---------------------------------------------------------
    # 2. Fetch H1 data - last 24 candles
    # ---------------------------------------------------------
    hourly_rates = mt5.copy_rates_from_pos(
        symbol,
        mt5.TIMEFRAME_H1,
        0,
        24
    )

    if hourly_rates is None or len(hourly_rates) == 0:
        raise RuntimeError(
            f"Could not fetch H1 data for {symbol}: {mt5.last_error()}"
        )

    hourly_df = pd.DataFrame(hourly_rates)

    hourly_df["time"] = pd.to_datetime(
        hourly_df["time"],
        unit="s",
        utc=True
    )

    # Convert to raw CSV text
    hourly_csv = hourly_df.to_csv(index=False)

    # ---------------------------------------------------------
    # 3. Current account state
    # ---------------------------------------------------------
    account = fetch_account_snapshot()

    equity = account["equity"]

    # ---------------------------------------------------------
    # 4. Current tick
    # ---------------------------------------------------------
    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        raise RuntimeError(
            f"Could not retrieve tick data for {symbol}: {mt5.last_error()}"
        )

    ask_price = tick.ask

    # ---------------------------------------------------------
    # 5. Derived features
    # ---------------------------------------------------------
    symbol_info = mt5.symbol_info(symbol)

    features = compute_features(
        daily_df,
        hourly_df,
        tick,
        symbol_info
    )

    # ---------------------------------------------------------
    # 6. Return everything
    # ---------------------------------------------------------
    return {
        "symbol": symbol,
        "daily_csv": daily_csv,
        "hourly_csv": hourly_csv,
        "equity": equity,
        "ask_price": ask_price,
        "bid_price": tick.bid,
        "features": features,
        "account": account,
    }
