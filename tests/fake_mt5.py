"""
Minimal in-memory stand-in for the MetaTrader5 package.

MetaTrader5 is Windows-only, so the persistence and restart-recovery
logic cannot be exercised on macOS/Linux without this. It implements
only the surface the bot actually calls.

Install it before any bot module is imported:

    import sys, tests.fake_mt5 as fake_mt5
    sys.modules["MetaTrader5"] = fake_mt5
"""

import sys
import time


# ---------------------------------------------------------------------
# Constants mirrored from the real package
# ---------------------------------------------------------------------

TIMEFRAME_D1 = 16408
TIMEFRAME_H1 = 16385

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1

POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

TRADE_ACTION_DEAL = 1

ORDER_TIME_GTC = 0
ORDER_FILLING_IOC = 1

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_REJECT = 10006

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1


# ---------------------------------------------------------------------
# Mutable world state - tests poke at these directly
# ---------------------------------------------------------------------

class World:
    def __init__(self):
        self.reset()

    def reset(self):
        self.initialized = False
        self.fail_initialize = False
        self.reject_orders = False
        self.order_send_returns_none = False

        self.equity = 10_000.0
        self.balance = 10_000.0

        self.positions = {}        # ticket -> _Position
        self.deals = []            # list of _Deal

        self.next_ticket = 5_000_000
        self.base_price = 1.1000


world = World()


def _ticket():
    world.next_ticket += 1
    return world.next_ticket


# ---------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------

class _Namespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        return f"{type(self).__name__}({self.__dict__})"


class _Position(_Namespace):
    pass


class _Deal(_Namespace):
    pass


class _Result(_Namespace):
    pass


# ---------------------------------------------------------------------
# Terminal / account
# ---------------------------------------------------------------------

def initialize(*args, **kwargs):
    if world.fail_initialize:
        return False

    world.initialized = True
    return True


def shutdown():
    world.initialized = False


def last_error():
    return (1, "fake mt5 error")


def terminal_info():
    return _Namespace(connected=world.initialized) if world.initialized else None


def account_info():
    if not world.initialized:
        return None

    floating = sum(p.profit for p in world.positions.values())

    return _Namespace(
        equity=world.equity + floating,
        balance=world.balance,
        margin=0.0,
        margin_free=world.balance,
        profit=floating,
        login=123456,
        currency="USD",
    )


# ---------------------------------------------------------------------
# Symbols and rates
# ---------------------------------------------------------------------

def symbol_select(symbol, enable=True):
    return True


def symbol_info(symbol):
    return _Namespace(
        name=symbol,
        digits=5,
        point=0.00001,
        visible=True,
    )


def symbol_info_tick(symbol):
    return _Namespace(
        bid=world.base_price,
        ask=world.base_price + 0.00010,
        time=int(time.time()),
    )


def copy_rates_from_pos(symbol, timeframe, start, count):
    """Deterministic synthetic candles with a mild uptrend."""

    import numpy as np

    now = int(time.time())

    step = 86400 if timeframe == TIMEFRAME_D1 else 3600

    rows = []

    for i in range(count):
        drift = i * 0.0004

        open_price = world.base_price + drift
        close_price = open_price + 0.0002
        high_price = close_price + 0.0003
        low_price = open_price - 0.0003

        rows.append((
            now - (count - i) * step,
            open_price,
            high_price,
            low_price,
            close_price,
            100 + i,
            2,
            0,
        ))

    return np.array(rows, dtype=[
        ("time", "<i8"),
        ("open", "<f8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("close", "<f8"),
        ("tick_volume", "<u8"),
        ("spread", "<i4"),
        ("real_volume", "<u8"),
    ])


# ---------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------

def order_send(request):
    if world.order_send_returns_none:
        return None

    if world.reject_orders:
        return _Result(
            retcode=TRADE_RETCODE_REJECT,
            comment="Fake rejection",
            order=0,
            deal=0,
            volume=0.0,
            price=0.0,
            request_id=1,
        )

    order_ticket = _ticket()
    deal_ticket = _ticket()
    position_ticket = _ticket()

    price = request["price"]

    is_buy = request["type"] == ORDER_TYPE_BUY

    world.positions[position_ticket] = _Position(
        ticket=position_ticket,
        symbol=request["symbol"],
        type=POSITION_TYPE_BUY if is_buy else POSITION_TYPE_SELL,
        volume=request["volume"],
        price_open=price,
        price_current=price,
        sl=request.get("sl", 0.0),
        tp=request.get("tp", 0.0),
        profit=0.0,
        magic=request.get("magic", 0),
        comment=request.get("comment", ""),
        time=int(time.time()),
    )

    world.deals.append(_Deal(
        ticket=deal_ticket,
        order=order_ticket,
        position_id=position_ticket,
        symbol=request["symbol"],
        entry=DEAL_ENTRY_IN,
        price=price,
        volume=request["volume"],
        profit=0.0,
        commission=0.0,
        swap=0.0,
        comment=request.get("comment", ""),
        time=int(time.time()),
    ))

    return _Result(
        retcode=TRADE_RETCODE_DONE,
        comment="Request executed",
        order=order_ticket,
        deal=deal_ticket,
        volume=request["volume"],
        price=price,
        request_id=1,
    )


def positions_get(ticket=None, symbol=None):
    values = list(world.positions.values())

    if ticket is not None:
        values = [p for p in values if p.ticket == ticket]

    if symbol is not None:
        values = [p for p in values if p.symbol == symbol]

    return tuple(values)


def history_deals_get(*args, **kwargs):
    position = kwargs.get("position")
    ticket = kwargs.get("ticket")
    order = kwargs.get("order")

    deals = world.deals

    if position is not None:
        deals = [d for d in deals if d.position_id == position]

    if ticket is not None:
        deals = [d for d in deals if d.ticket == ticket]

    if order is not None:
        deals = [d for d in deals if d.order == order]

    return tuple(deals)


# ---------------------------------------------------------------------
# Test helpers (not part of the real MT5 API)
# ---------------------------------------------------------------------

def close_position(position_ticket, exit_price=None, profit=12.34):
    """Simulate SL/TP being hit or a manual close."""

    position = world.positions.pop(position_ticket, None)

    if position is None:
        return None

    exit_price = (
        exit_price
        if exit_price is not None
        else position.price_open + 0.0004
    )

    world.deals.append(_Deal(
        ticket=_ticket(),
        order=_ticket(),
        position_id=position_ticket,
        symbol=position.symbol,
        entry=DEAL_ENTRY_OUT,
        price=exit_price,
        volume=position.volume,
        profit=profit,
        commission=-0.10,
        swap=0.0,
        comment=position.comment,
        time=int(time.time()),
    ))

    world.balance += profit
    world.equity += profit

    return exit_price


def install():
    """Register this module as MetaTrader5 for the rest of the process."""

    sys.modules["MetaTrader5"] = sys.modules[__name__]
