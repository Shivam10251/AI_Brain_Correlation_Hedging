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
TRADE_ACTION_SLTP = 2

ORDER_TIME_GTC = 0

# Order filling ENUM (what a request carries).
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

# Symbol filling BITMASK (what a broker advertises). Different
# numbering from the enum above - conflating the two is exactly the
# Phase 1 bug being guarded against.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_INVALID_FILL = 10030

DEAL_ENTRY_IN = 0
DEAL_ENTRY_OUT = 1

# account_info().margin_mode
ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_EXCHANGE = 1
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2


# ---------------------------------------------------------------------
# Mutable world state - tests poke at these directly
# ---------------------------------------------------------------------

# Per-symbol broker specifications, so tests can exercise the three
# instrument shapes the sizing maths has to survive: a 5-digit FX pair,
# a 2-digit crypto symbol and a 3-digit metal.
SYMBOL_SPECS = {
    "EURUSDm": {
        "digits": 5, "point": 0.00001, "price": 1.10000,
        "trade_tick_size": 0.00001, "trade_tick_value": 1.0,
        "trade_contract_size": 100_000.0,
        "volume_min": 0.01, "volume_step": 0.01, "volume_max": 200.0,
        "filling_mode": SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC,
        "trade_stops_level": 10, "spread": 12,
    },
    "GBPUSDm": {
        "digits": 5, "point": 0.00001, "price": 1.27000,
        "trade_tick_size": 0.00001, "trade_tick_value": 1.0,
        "trade_contract_size": 100_000.0,
        "volume_min": 0.01, "volume_step": 0.01, "volume_max": 200.0,
        "filling_mode": SYMBOL_FILLING_IOC,
        "trade_stops_level": 12, "spread": 15,
    },
    "BTCUSDm": {
        "digits": 2, "point": 0.01, "price": 64_000.00,
        "trade_tick_size": 0.01, "trade_tick_value": 0.01,
        "trade_contract_size": 1.0,
        "volume_min": 0.01, "volume_step": 0.01, "volume_max": 10.0,
        "filling_mode": SYMBOL_FILLING_IOC,
        "trade_stops_level": 0, "spread": 3500,
    },
    "XAUUSDm": {
        "digits": 3, "point": 0.001, "price": 2_400.000,
        "trade_tick_size": 0.001, "trade_tick_value": 0.1,
        "trade_contract_size": 100.0,
        "volume_min": 0.01, "volume_step": 0.01, "volume_max": 50.0,
        "filling_mode": SYMBOL_FILLING_FOK,
        "trade_stops_level": 35, "spread": 28,
    },
}

# Anything not listed above behaves like the original fake: a generic
# 5-digit instrument.
DEFAULT_SPEC = {
    "digits": 5, "point": 0.00001, "price": 1.10000,
    "trade_tick_size": 0.00001, "trade_tick_value": 1.0,
    "trade_contract_size": 100_000.0,
    "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0,
    "filling_mode": SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC,
    "trade_stops_level": 10, "spread": 12,
}


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

        # ---- Phase 1: broker facts ----

        # Server clock offset from UTC, in minutes. Non-zero values
        # exercise the timezone handling that Phase 0 §2.2 flagged.
        self.server_offset_minutes = 0

        # Makes one offset sample disagree, to test the stability
        # assertion rather than only the happy path.
        self.unstable_offset = False

        self.margin_mode = ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
        self.login = 123456
        self.server = "FakeBroker-Demo"
        self.company = "Fake Broker Ltd"
        self.leverage = 100

        self.symbol_specs = {k: dict(v) for k, v in SYMBOL_SPECS.items()}

        # Symbols the broker does not offer at all -> symbol_select
        # returns False, which is one explanation for "only one symbol
        # ever trades".
        self.unknown_symbols = set()

        self._offset_calls = 0


world = World()


def spec_for(symbol):
    """Broker specification for one symbol."""

    return world.symbol_specs.get(symbol, DEFAULT_SPEC)


def _ticket():
    world.next_ticket += 1
    return world.next_ticket


def _server_epoch():
    """
    Now, on the BROKER's clock.

    MT5 reports deal and tick times as server wall clock expressed as
    an epoch, so the ledger can read a deal's server date straight off
    it. Modelling that here is what makes the trading-day boundary
    testable.
    """

    return int(time.time() + world.server_offset_minutes * 60)


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
    if not world.initialized:
        return None

    return _Namespace(
        connected=True,
        build=4260,
        name="FakeTerminal",
        company=world.company,
        trade_allowed=True,
    )


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
        login=world.login,
        currency="USD",

        # Phase 1 broker facts
        server=world.server,
        company=world.company,
        leverage=world.leverage,
        margin_mode=world.margin_mode,
    )


# ---------------------------------------------------------------------
# Symbols and rates
# ---------------------------------------------------------------------

def symbol_select(symbol, enable=True):
    """False for a symbol this broker does not offer."""

    return symbol not in world.unknown_symbols


def symbol_info(symbol):
    if symbol in world.unknown_symbols:
        return None

    spec = spec_for(symbol)

    return _Namespace(
        name=symbol,
        visible=True,

        digits=spec["digits"],
        point=spec["point"],
        trade_contract_size=spec["trade_contract_size"],

        trade_tick_size=spec["trade_tick_size"],
        trade_tick_value=spec["trade_tick_value"],
        trade_tick_value_profit=spec["trade_tick_value"],
        trade_tick_value_loss=spec["trade_tick_value"],

        volume_min=spec["volume_min"],
        volume_step=spec["volume_step"],
        volume_max=spec["volume_max"],

        filling_mode=spec["filling_mode"],

        trade_stops_level=spec["trade_stops_level"],
        trade_freeze_level=0,
        spread=spec["spread"],
        trade_mode=4,

        swap_long=-2.5,
        swap_short=0.8,
    )


def symbol_info_tick(symbol):
    if symbol in world.unknown_symbols:
        return None

    spec = spec_for(symbol)

    price = spec["price"]

    # tick.time is SERVER time, not UTC. Offsetting it here is what
    # lets the offset measurement be tested honestly.
    server_now = time.time() + world.server_offset_minutes * 60

    if world.unstable_offset:
        world._offset_calls += 1

        # Every third sample disagrees by an hour.
        if world._offset_calls % 3 == 0:
            server_now += 3600

    return _Namespace(
        bid=price,
        ask=price + spec["point"] * spec["spread"],
        time=int(server_now),
    )


def copy_rates_from_pos(symbol, timeframe, start, count):
    """
    Deterministic synthetic candles with a mild uptrend.

    Bar times are SERVER time (offset from UTC), matching real MT5.
    `start` is honoured, so requesting position 1 really does skip the
    forming bar - which is what Phase 4 needs to verify.
    """

    import numpy as np

    step = 86400 if timeframe == TIMEFRAME_D1 else 3600

    server_now = int(time.time() + world.server_offset_minutes * 60)

    # Align to the bar grid, then step back by `start` bars so
    # start=1 returns only closed bars.
    latest = server_now - (server_now % step) - start * step

    spec = spec_for(symbol)

    base = spec["price"] if symbol in world.symbol_specs else world.base_price

    scale = base * 0.0004

    rows = []

    for i in range(count):
        drift = i * scale

        open_price = base + drift
        close_price = open_price + scale * 0.5
        high_price = close_price + scale * 0.75
        low_price = open_price - scale * 0.75

        rows.append((
            latest - (count - 1 - i) * step,
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

def _filling_supported(symbol, type_filling):
    """
    Does the broker accept this filling mode for this symbol?

    Real MT5 answers with retcode 10030 (Invalid fill) when it does
    not. The old code hardcoded IOC, so on a FOK-only symbol EVERY
    order failed here - silently, forever. Modelling the rejection is
    what makes the Phase 1 fix testable.
    """

    mask = spec_for(symbol)["filling_mode"]

    if type_filling == ORDER_FILLING_FOK:
        return bool(mask & SYMBOL_FILLING_FOK)

    if type_filling == ORDER_FILLING_IOC:
        return bool(mask & SYMBOL_FILLING_IOC)

    # RETURN is accepted by everything in this model.
    return True


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

    if not _filling_supported(
        request["symbol"], request.get("type_filling", ORDER_FILLING_IOC)
    ):
        return _Result(
            retcode=TRADE_RETCODE_INVALID_FILL,
            comment="Unsupported filling mode",
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
        magic=request.get("magic", 0),
        time=_server_epoch(),
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

def close_position(position_ticket, exit_price=None, profit=12.34,
                   commission=-0.10, swap=0.0):
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
        commission=commission,
        swap=swap,
        comment=position.comment,
        magic=position.magic,
        time=_server_epoch(),
    ))

    world.balance += profit + commission + swap
    world.equity += profit + commission + swap

    return exit_price


def install():
    """Register this module as MetaTrader5 for the rest of the process."""

    sys.modules["MetaTrader5"] = sys.modules[__name__]
