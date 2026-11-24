"""A tiny stand-in for the MetaTrader5 package, loaded by a real `python -m bot.mt5_worker` child in the tests."""
from __future__ import annotations

import os
import time
from collections import namedtuple

Account = namedtuple("Account", "login equity margin_mode currency")
Position = namedtuple("Position", "ticket symbol type volume price_open sl")
Request = namedtuple("Request", "action symbol volume")
SendResult = namedtuple("SendResult", "retcode order comment request")

_state = {"connected": False, "error": (1, "Success"), "hang": 0.0}


class _Dtype:
    names = ("time", "open", "close")


class Rates:
    """Shaped like the numpy array copy_rates_* returns: named fields, tolist() gives tuples."""

    dtype = _Dtype()

    def tolist(self):
        return [(1_700_000_000, 3800.0, 3801.5), (1_700_003_600, 3801.5, 3799.0)]


def initialize(**kwargs):
    _state["connected"] = True
    _state["kwargs"] = kwargs
    return True


def shutdown():
    _state["connected"] = False


def last_error():
    return _state["error"]


def account_info():
    if _state["hang"]:
        time.sleep(_state["hang"])
    return Account(1, 10000.0, 2, "USD")


def positions_get(symbol=None):
    return (Position(7, "XAUUSD", 1, 0.1, 3810.0, 3830.0),)


def copy_rates_from_pos(symbol, frame, start, count):
    return Rates()


def symbol_info(name):
    if name != "XAUUSD":
        _state["error"] = (-1, "unknown symbol")
        return None
    return Account(0, 0.0, 0, "USD")


def order_send(request):
    return SendResult(10009, 42, "done", Request(request["action"], request["symbol"], request["volume"]))


def hang(seconds):
    _state["hang"] = float(seconds)
    return True


def sleep(seconds):
    time.sleep(seconds)
    return True


def noisy():
    print("something the package printed")
    os.write(1, b"and something written straight to the handle\n")
    return 5


def boom():
    raise ValueError("bad arguments")


def quit_now():
    os._exit(3)


def pid():
    return os.getpid()
