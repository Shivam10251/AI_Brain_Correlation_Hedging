import MetaTrader5 as mt5

from config import SL_PERCENT, TP_PERCENT


def execute_trade(symbol, signal):
    """
    Execute a market trade through MetaTrader 5.
    """

    signal = signal.upper()

    if signal == "HOLD":
        return {
            "status": "SKIPPED",
            "reason": "AI signal is HOLD"
        }

    if signal not in {"BUY", "SELL"}:
        raise ValueError(f"Invalid signal: {signal}")

    # ---------------------------------------------------------
    # Symbol information
    # ---------------------------------------------------------

    symbol_info = mt5.symbol_info(symbol)

    if symbol_info is None:
        raise RuntimeError(
            f"Symbol not found: {symbol} | {mt5.last_error()}"
        )

    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(
                f"Could not select symbol: {symbol}"
            )

        symbol_info = mt5.symbol_info(symbol)

    # ---------------------------------------------------------
    # Current price
    # ---------------------------------------------------------

    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        raise RuntimeError(
            f"No tick data for {symbol} | {mt5.last_error()}"
        )

    # ---------------------------------------------------------
    # Price / SL / TP
    # ---------------------------------------------------------

    if signal == "BUY":

        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask

        sl = price * (1 - SL_PERCENT)
        tp = price * (1 + TP_PERCENT)

    else:

        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid

        sl = price * (1 + SL_PERCENT)
        tp = price * (1 - TP_PERCENT)

    # ---------------------------------------------------------
    # CRITICAL: use broker's digits
    # ---------------------------------------------------------

    digits = symbol_info.digits

    price = round(price, digits)
    sl = round(sl, digits)
    tp = round(tp, digits)

    # ---------------------------------------------------------
    # Print what we're attempting
    # ---------------------------------------------------------

    print("\n========================================")
    print("ATTEMPTING MT5 TRADE")
    print("Symbol:", symbol)
    print("Signal:", signal)
    print("Price:", price)
    print("SL:", sl)
    print("TP:", tp)
    print("Digits:", digits)
    print("Volume:", 0.01)
    print("========================================")

    # ---------------------------------------------------------
    # Trade request
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

        # Let MT5 use the symbol's supported filling mode
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    # ---------------------------------------------------------
    # Send order
    # ---------------------------------------------------------

    result = mt5.order_send(request)

    if result is None:

        raise RuntimeError(
            f"order_send returned None | "
            f"MT5 error: {mt5.last_error()}"
        )

    # ---------------------------------------------------------
    # ALWAYS inspect the MT5 result
    # ---------------------------------------------------------

    print("\n========================================")
    print("MT5 ORDER RESULT")
    print("Retcode:", result.retcode)
    print("Comment:", result.comment)
    print("Order:", result.order)
    print("Deal:", result.deal)
    print("Volume:", result.volume)
    print("Price:", result.price)
    print("========================================\n")

    # ---------------------------------------------------------
    # Successful market execution
    # ---------------------------------------------------------

    successful_codes = {
        mt5.TRADE_RETCODE_DONE,
        mt5.TRADE_RETCODE_DONE_PARTIAL,
    }

    if result.retcode not in successful_codes:

        raise RuntimeError(
            f"MT5 REJECTED TRADE | "
            f"retcode={result.retcode} | "
            f"comment={result.comment}"
        )

    return result