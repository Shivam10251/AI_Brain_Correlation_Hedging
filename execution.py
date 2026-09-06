import MetaTrader5 as mt5

from config import SL_PERCENT, TP_PERCENT


def execute_trade(symbol, signal):
    """
    Execute a market trade on MetaTrader 5.

    Args:
        symbol (str): Trading symbol, e.g. "EURUSDm"
        signal (str): "BUY", "SELL", or "HOLD"

    Returns:
        MT5 order result object.
    """

    # ---------------------------------------------------------
    # 1. Validate signal
    # ---------------------------------------------------------
    signal = signal.upper()

    if signal == "HOLD":
        return {
            "status": "SKIPPED",
            "reason": "AI signal is HOLD"
        }

    if signal not in {"BUY", "SELL"}:
        raise ValueError(
            f"Invalid trading signal: {signal}"
        )

    # ---------------------------------------------------------
    # 2. Get symbol information
    # ---------------------------------------------------------
    symbol_info = mt5.symbol_info(symbol)

    if symbol_info is None:
        raise RuntimeError(
            f"Could not retrieve symbol information for {symbol}"
        )

    # Make sure the symbol is visible in Market Watch
    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(
                f"Could not select symbol {symbol}"
            )

        symbol_info = mt5.symbol_info(symbol)

    # ---------------------------------------------------------
    # 3. Get current market price
    # ---------------------------------------------------------
    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        raise RuntimeError(
            f"Could not retrieve tick data for {symbol}"
        )

    # ---------------------------------------------------------
    # 4. Determine order type and entry price
    # ---------------------------------------------------------
    if signal == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask

        # BUY:
        # SL below entry
        # TP above entry
        sl = price * (1 - SL_PERCENT)
        tp = price * (1 + TP_PERCENT)

    else:  # SELL
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid

        # SELL:
        # SL above entry
        # TP below entry
        sl = price * (1 + SL_PERCENT)
        tp = price * (1 - TP_PERCENT)

    # ---------------------------------------------------------
    # 5. CRITICAL: Round SL and TP using symbol digits
    # ---------------------------------------------------------
    digits = symbol_info.digits

    sl = round(sl, digits)
    tp = round(tp, digits)
    price = round(price, digits)

    # ---------------------------------------------------------
    # 6. Build MT5 trade request
    # ---------------------------------------------------------
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": 0.01,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": 100001,
        "comment": "AI Hedge Fund Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    # ---------------------------------------------------------
    # 7. Send trade
    # ---------------------------------------------------------
    result = mt5.order_send(request)

    if result is None:
        raise RuntimeError(
            f"MT5 order_send() failed: {mt5.last_error()}"
        )

    # ---------------------------------------------------------
    # 8. Return MT5 result
    # ---------------------------------------------------------
    return result