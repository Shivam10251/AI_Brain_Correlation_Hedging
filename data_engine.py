"""
MetaTrader 5 connection, market data, and indicator calculation.

Indicators are computed here by hand rather than pulled from a library
so the exact formula is visible and cannot change under you when a
dependency updates. Every one of them is standard.
"""

import MetaTrader5 as mt5
import pandas as pd

import config


# =============================================================
# Connection
# =============================================================

def initialize_mt5():
    """
    Attach to the MetaTrader 5 terminal.

    If MT5_LOGIN is set we force that account; otherwise we attach to
    whatever the terminal is already logged into. Returns the account
    info object.
    """

    if config.MT5_PATH:
        started = mt5.initialize(path=config.MT5_PATH)
    else:
        started = mt5.initialize()

    if not started:
        raise RuntimeError(
            f"[SYSTEM] MT5 initialize failed: {mt5.last_error()}"
        )

    if config.MT5_LOGIN:

        logged_in = mt5.login(
            config.MT5_LOGIN,
            password=config.MT5_PASSWORD,
            server=config.MT5_SERVER,
        )

        if not logged_in:
            raise RuntimeError(
                f"[SYSTEM] MT5 login failed for {config.MT5_LOGIN} on "
                f"{config.MT5_SERVER}: {mt5.last_error()}"
            )

    account = mt5.account_info()

    if account is None:
        raise RuntimeError(
            f"[SYSTEM] Connected, but account_info() returned nothing: "
            f"{mt5.last_error()}"
        )

    print(
        f"[SYSTEM] MT5 connected | account {account.login} | "
        f"{account.server} | equity {account.equity:.2f} "
        f"{account.currency} | balance {account.balance:.2f} | "
        f"leverage 1:{account.leverage}"
    )

    return account


def shutdown_mt5():
    mt5.shutdown()

    print("[SYSTEM] MT5 connection closed.")


# =============================================================
# Indicators
# =============================================================

def _ema(series, period):
    """Exponential moving average."""

    return series.ewm(span=period, adjust=False).mean()


def _rsi(close, period):
    """
    Relative Strength Index, exponentially smoothed (Wilder's).

    Wilder's smoothing is an EMA with alpha = 1/period, which is what
    `ewm(alpha=1/period)` gives directly.
    """

    delta = close.diff()

    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()

    # A run with no losing bars is RSI 100, not a division by zero.
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)

    rsi = 100.0 - (100.0 / (1.0 + rs))

    return rsi.fillna(100.0).where(avg_gain > 0, 0.0)


def _atr(df, period):
    """
    Average True Range.

    True range is the largest of:
        high - low
        |high - previous close|
        |low  - previous close|

    The last two are what make it gap-aware; a plain high-low range
    understates volatility on any instrument that gaps, which is every
    instrument over a weekend.
    """

    prev_close = df["close"].shift(1)

    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )

    true_range = ranges.max(axis=1)

    return true_range.ewm(alpha=1.0 / period, adjust=False).mean()


def _describe_structure(df, bars=10):
    """
    A short plain-English reading of the last `bars` candles.

    The model reads this alongside the raw numbers. Higher highs with
    higher lows is a different market from the same net change achieved
    by chopping sideways, and a scalar trend figure cannot say so.
    """

    recent = df.tail(bars)

    if len(recent) < 3:
        return "insufficient history"

    highs = recent["high"].tolist()
    lows = recent["low"].tolist()

    higher_highs = highs[-1] > max(highs[:-1])
    lower_lows = lows[-1] < min(lows[:-1])

    first_close = float(recent["close"].iloc[0])
    last_close = float(recent["close"].iloc[-1])

    change_pct = (
        ((last_close - first_close) / first_close * 100.0)
        if first_close else 0.0
    )

    if higher_highs and not lower_lows:
        shape = "making higher highs"
    elif lower_lows and not higher_highs:
        shape = "making lower lows"
    elif higher_highs and lower_lows:
        shape = "expanding range (both ends extended)"
    else:
        shape = "contained inside the prior range"

    direction = "up" if change_pct > 0 else "down"

    return (
        f"{shape}; {abs(change_pct):.2f}% {direction} over the last "
        f"{len(recent)} bars"
    )


def calculate_indicators(df):
    """
    EMA(200), RSI(14), ATR(14) and relative volume for one frame.

    Returns the latest scalar of each, plus a structural reading of the
    recent bars. Scalars are what the prompt needs; handing the model
    250 rows of indicator history would bury the signal.
    """

    if df is None or df.empty:
        raise ValueError("calculate_indicators received an empty frame")

    frame = df.copy()

    frame["ema"] = _ema(frame["close"], config.EMA_PERIOD)
    frame["rsi"] = _rsi(frame["close"], config.RSI_PERIOD)
    frame["atr"] = _atr(frame, config.ATR_PERIOD)

    volume_ma = frame["tick_volume"].rolling(config.VOL_MA_PERIOD).mean()

    # Relative volume: 1.0 is an average bar, 2.0 is twice normal
    # participation. This is the number the auditor leans on hardest -
    # a breakout on 0.4x volume is the classic false one.
    frame["relative_volume"] = frame["tick_volume"] / volume_ma

    latest = frame.iloc[-1]

    def scalar(name, default=None):
        value = latest.get(name)

        if value is None or pd.isna(value):
            return default

        return float(value)

    return {
        "close": scalar("close"),
        "ema_200": scalar("ema"),
        "rsi_14": scalar("rsi"),
        "atr_14": scalar("atr"),
        "tick_volume": scalar("tick_volume", 0.0),
        "relative_volume": scalar("relative_volume", 1.0),
        "price_vs_ema": (
            "above" if scalar("close", 0) > (scalar("ema") or 0) else "below"
        ),
        "structure": _describe_structure(frame),
    }


# =============================================================
# Market data
# =============================================================

def _fetch_frame(symbol, timeframe, count):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)

    if rates is None or len(rates) == 0:
        raise RuntimeError(
            f"No bars for {symbol} on timeframe {timeframe}: "
            f"{mt5.last_error()}"
        )

    frame = pd.DataFrame(rates)

    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)

    return frame


def fetch_multi_timeframe_data(symbol):
    """
    Everything one decision needs about one symbol.

    Daily is the macro trend, H1 the micro momentum. Both carry the
    full indicator set so the model can see when they disagree.
    """

    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(
            f"Could not enable {symbol} in MarketWatch: {mt5.last_error()}"
        )

    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        raise RuntimeError(
            f"No tick data for {symbol}: {mt5.last_error()}"
        )

    account = mt5.account_info()

    if account is None:
        raise RuntimeError(
            f"Could not read account info: {mt5.last_error()}"
        )

    h1_frame = _fetch_frame(
        symbol, mt5.TIMEFRAME_H1, config.BARS_TO_FETCH
    )

    daily_frame = _fetch_frame(
        symbol, mt5.TIMEFRAME_D1, config.BARS_TO_FETCH
    )

    return {
        "symbol": symbol,
        "bid": float(tick.bid),
        "ask": float(tick.ask),
        "spread": round(float(tick.ask - tick.bid), 8),
        "equity": float(account.equity),
        "balance": float(account.balance),
        "h1_data": calculate_indicators(h1_frame),
        "daily_data": calculate_indicators(daily_frame),
    }


def fetch_correlated_asset_prices(current_symbol, all_symbols):
    """
    Live bid/ask for every OTHER symbol in the universe.

    Stored with each trade so the auditor can later ask whether losses
    cluster around a particular cross-asset configuration - a dollar
    bid across every pair at once, say.
    """

    prices = {}

    for symbol in all_symbols:

        if symbol == current_symbol:
            continue

        try:
            if not mt5.symbol_select(symbol, True):
                continue

            tick = mt5.symbol_info_tick(symbol)

            if tick is None:
                continue

            prices[symbol] = {
                "bid": float(tick.bid),
                "ask": float(tick.ask),
            }

        except Exception as error:                  # noqa: BLE001
            # One unavailable symbol must not cost us the snapshot of
            # all the others.
            print(f"[SYSTEM] correlated price for {symbol} failed: {error}")

    return prices
