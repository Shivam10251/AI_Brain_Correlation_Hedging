import MetaTrader5 as mt5
import pandas as pd

from config import SYMBOLS


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
            "ask_price": float
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
    # 3. Current account equity
    # ---------------------------------------------------------
    account_info = mt5.account_info()

    if account_info is None:
        raise RuntimeError(
            f"Could not retrieve account information: {mt5.last_error()}"
        )

    equity = account_info.equity

    # ---------------------------------------------------------
    # 4. Current Ask price
    # ---------------------------------------------------------
    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        raise RuntimeError(
            f"Could not retrieve tick data for {symbol}: {mt5.last_error()}"
        )

    ask_price = tick.ask

    # ---------------------------------------------------------
    # 5. Return everything
    # ---------------------------------------------------------
    return {
        "symbol": symbol,
        "daily_csv": daily_csv,
        "hourly_csv": hourly_csv,
        "equity": equity,
        "ask_price": ask_price,
    }