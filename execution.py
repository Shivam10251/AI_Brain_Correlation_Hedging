"""
Order placement, position sizing and position closing.

The sizing here is the difference between a toy and a system. Volume is
derived from the distance to the stop, so every trade risks the same
percentage of equity regardless of instrument or how wide the stop is.
A fixed lot size does the opposite: it makes a tight stop cheap and a
wide stop ruinous, which is exactly backwards.
"""

import MetaTrader5 as mt5

import config


# Preference order. FOK is the strictest and the most widely accepted
# for market orders; RETURN is the fallback for exchange execution.
_FILLING_PREFERENCE = (
    (mt5.SYMBOL_FILLING_FOK, mt5.ORDER_FILLING_FOK),
    (mt5.SYMBOL_FILLING_IOC, mt5.ORDER_FILLING_IOC),
)


def _resolve_filling_mode(symbol_info):
    """
    Pick a filling mode the broker actually supports.

    Hardcoding IOC is a common cause of every order being rejected on
    a broker that only accepts FOK, with a retcode that does not
    obviously say so.
    """

    mask = int(getattr(symbol_info, "filling_mode", 0) or 0)

    for bit, order_filling in _FILLING_PREFERENCE:
        if mask & bit:
            return order_filling

    return getattr(mt5, "ORDER_FILLING_RETURN", 2)


def calculate_position_size(symbol, stop_loss_price, risk_percent, equity):
    """
    The volume that risks `risk_percent` of equity to the stop.

        money_at_risk = equity * risk_percent / 100
        loss_per_lot  = stop_distance / tick_size * tick_value
        volume        = money_at_risk / loss_per_lot

    Then snapped to the broker's volume step and clamped to its
    min/max. Returns the broker minimum if the maths cannot be done -
    never zero, and never something unbounded.
    """

    symbol_info = mt5.symbol_info(symbol)

    if symbol_info is None:
        raise RuntimeError(
            f"Symbol not found: {symbol} | {mt5.last_error()}"
        )

    volume_min = float(symbol_info.volume_min)
    volume_max = float(symbol_info.volume_max)
    volume_step = float(symbol_info.volume_step) or 0.01

    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        raise RuntimeError(f"No tick data for {symbol} | {mt5.last_error()}")

    price = float(tick.ask)

    stop_distance = abs(price - float(stop_loss_price))

    if stop_distance <= 0:
        print(
            f"[TRADE] {symbol}: stop distance is zero; falling back to "
            f"minimum volume {volume_min}"
        )
        return volume_min

    money_at_risk = float(equity) * (float(risk_percent) / 100.0)

    tick_size = float(getattr(symbol_info, "trade_tick_size", 0) or 0)
    tick_value = float(getattr(symbol_info, "trade_tick_value", 0) or 0)

    if tick_size > 0 and tick_value > 0:
        loss_per_lot = (stop_distance / tick_size) * tick_value
    else:
        # Fall back to contract size. Correct for most FX pairs quoted
        # in the account currency, approximate elsewhere.
        contract_size = float(
            getattr(symbol_info, "trade_contract_size", 0) or 0
        )

        if contract_size <= 0:
            print(
                f"[TRADE] {symbol}: broker gives no tick value or "
                f"contract size; using minimum volume"
            )
            return volume_min

        loss_per_lot = stop_distance * contract_size

    if loss_per_lot <= 0:
        return volume_min

    volume = money_at_risk / loss_per_lot

    # Snap DOWN to the step. Rounding up would risk more than asked.
    steps = int(volume / volume_step)

    volume = steps * volume_step

    volume = max(volume_min, min(volume_max, volume))

    # Kill floating-point tails like 0.30000000000000004, which some
    # brokers reject outright.
    decimals = len(str(volume_step).split(".")[-1]) if "." in str(
        volume_step
    ) else 2

    volume = round(volume, decimals)

    print(
        f"[TRADE] {symbol}: risking {money_at_risk:.2f} over a "
        f"{stop_distance:.5f} stop -> {volume} lots"
    )

    return volume


# =============================================================
# Positions
# =============================================================

def get_open_positions(symbol=None):
    """Live positions, as a list of plain dicts."""

    positions = (
        mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    )

    if positions is None:
        return []

    return [
        {
            "ticket": int(p.ticket),
            "symbol": p.symbol,
            "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
            "type": int(p.type),
            "volume": float(p.volume),
            "price_open": float(p.price_open),
            "price_current": float(p.price_current),
            "sl": float(p.sl),
            "tp": float(p.tp),
            "profit": float(p.profit),
            "magic": int(p.magic),
            "comment": p.comment,
        }
        for p in positions
    ]


def close_position(ticket_or_pos):
    """
    Close one position with an opposite market order.

    Returns a result dict rather than raising, so a reversal that
    cannot complete is reported and logged instead of taking the whole
    scan down with it.
    """

    ticket = (
        int(ticket_or_pos.get("ticket"))
        if isinstance(ticket_or_pos, dict)
        else int(ticket_or_pos)
    )

    positions = mt5.positions_get(ticket=ticket)

    if not positions:
        return {
            "status": "NOT_FOUND",
            "ticket": ticket,
            "message": "Position is not open in MT5",
        }

    position = positions[0]

    symbol = position.symbol

    symbol_info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)

    if symbol_info is None or tick is None:
        return {
            "status": "FAILED",
            "ticket": ticket,
            "message": f"No market data for {symbol}: {mt5.last_error()}",
        }

    is_long = position.type == mt5.POSITION_TYPE_BUY

    order_type = mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY

    price = round(
        float(tick.bid if is_long else tick.ask), symbol_info.digits
    )

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(position.volume),
        "type": order_type,
        "position": ticket,
        "price": price,
        "deviation": config.DEVIATION,
        "magic": config.MAGIC_NUMBER,
        "comment": "AI Hedge Fund close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _resolve_filling_mode(symbol_info),
    }

    result = mt5.order_send(request)

    if result is None:
        return {
            "status": "FAILED",
            "ticket": ticket,
            "message": f"order_send returned None: {mt5.last_error()}",
        }

    if result.retcode not in {
        mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL
    }:
        return {
            "status": "REJECTED",
            "ticket": ticket,
            "retcode": int(result.retcode),
            "message": f"MT5 rejected the close: {result.comment}",
        }

    print(f"[TRADE] closed position {ticket} on {symbol} at {price}")

    return {
        "status": "CLOSED",
        "ticket": ticket,
        "symbol": symbol,
        "close_price": float(result.price) or price,
        "volume": float(result.volume),
        "deal": int(result.deal),
        "retcode": int(result.retcode),
    }


# =============================================================
# Entry
# =============================================================

def execute_trade(symbol, signal, stop_loss, take_profit, risk_percent):
    """
    Place one market order with the stops the brain chose.

    Raises RuntimeError on rejection, carrying the retcode and the
    broker's own comment - the caller logs it and moves to the next
    symbol.
    """

    signal = signal.upper()

    if signal not in {"BUY", "SELL"}:
        raise ValueError(f"execute_trade got a non-trading signal: {signal}")

    symbol_info = mt5.symbol_info(symbol)

    if symbol_info is None:
        raise RuntimeError(f"Symbol not found: {symbol} | {mt5.last_error()}")

    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Could not enable {symbol} in MarketWatch")

        symbol_info = mt5.symbol_info(symbol)

    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        raise RuntimeError(f"No tick data for {symbol} | {mt5.last_error()}")

    account = mt5.account_info()

    if account is None:
        raise RuntimeError(f"Could not read account info: {mt5.last_error()}")

    equity = float(account.equity)

    digits = symbol_info.digits

    if signal == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = float(tick.ask)
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = float(tick.bid)

    price = round(price, digits)
    stop_loss = round(float(stop_loss), digits)
    take_profit = round(float(take_profit), digits)

    volume = calculate_position_size(
        symbol, stop_loss, risk_percent, equity
    )

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "sl": stop_loss,
        "tp": take_profit,
        "deviation": config.DEVIATION,
        "magic": config.MAGIC_NUMBER,
        "comment": "AI Hedge Fund Bot",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": _resolve_filling_mode(symbol_info),
    }

    print(
        f"[TRADE] sending {signal} {volume} {symbol} @ {price} "
        f"SL={stop_loss} TP={take_profit}"
    )

    result = mt5.order_send(request)

    if result is None:
        raise RuntimeError(
            f"order_send returned None for {symbol} | {mt5.last_error()}"
        )

    if result.retcode not in {
        mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL
    }:
        raise RuntimeError(
            f"MT5 REJECTED {signal} {symbol} | retcode={result.retcode} | "
            f"{result.comment}"
        )

    # The position id is what deal history is keyed by later. It is not
    # always the same number as the order ticket.
    position_id = None

    deals = mt5.history_deals_get(ticket=int(result.deal))

    if deals:
        position_id = int(getattr(deals[0], "position_id", 0)) or None

    print(
        f"[TRADE] FILLED {signal} {symbol} deal={result.deal} "
        f"@ {result.price}"
    )

    return {
        "status": "EXECUTED",
        "symbol": symbol,
        "side": signal,
        "deal": int(result.deal),
        "order": int(result.order),
        "position": position_id or int(result.order),
        "price": float(result.price) or price,
        "entry_price": float(result.price) or price,
        "volume": float(result.volume) or volume,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "risk_percent": float(risk_percent),
        "retcode": int(result.retcode),
    }
