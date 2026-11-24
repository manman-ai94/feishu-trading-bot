"""The MT5 package in a child process: a terminal that does not answer must not stop the bot's other threads."""
from __future__ import annotations

import threading
import time
from collections import namedtuple

import pytest

from bot import mt5_client, mt5_worker
from bot.mt5_client import Mt5Error, Mt5Link
from bot.mt5_worker import NO_CONNECTION, Mt5Process, Row, plain, rebuild

FAKE = "tests.mt5_worker_fake"


@pytest.fixture
def worker():
    made = []

    def make(**kwargs):
        process = Mt5Process(module=FAKE, **{"timeout": 10.0, "connect_timeout": 10.0, **kwargs})  # the bot's own way to start it
        made.append(process)
        return process

    yield make
    for process in made:
        process.close()


def test_named_tuples_and_record_arrays_come_back_as_rows_and_dicts():
    Tick = namedtuple("Tick", "bid ask")
    Result = namedtuple("Result", "retcode request")

    class Dtype:
        names = ("time", "close")

    class Rates:
        dtype = Dtype()

        def tolist(self):
            return [(1, 2.5), (3, 4.5)]

    class Scalar:
        dtype = object()

        def tolist(self):
            return 7

    sent = plain({"tick": Tick(1.0, 2.0), "result": Result(10009, Tick(3.0, 4.0)), "rates": Rates(), "n": Scalar(), "pair": (-1, "x")})
    back = rebuild(sent)
    assert back["tick"].bid == 1.0 and back["tick"]._asdict() == {"bid": 1.0, "ask": 2.0}
    assert back["result"].request.ask == 4.0 and back["result"]["retcode"] == 10009
    assert back["rates"] == ({"time": 1, "close": 2.5}, {"time": 3, "close": 4.5})
    assert back["n"] == 7 and back["pair"] == (-1, "x")
    assert getattr(back["tick"], "missing", "default") == "default"
    assert isinstance(back["tick"], Row)


def test_calls_run_in_the_child_and_answer_like_the_package(worker):
    mt5 = worker()
    assert mt5.account_info() is None and mt5.last_error() == NO_CONNECTION  # nothing before initialize
    assert mt5.initialize(timeout=20_000, path="C:/terminal64.exe") is True
    account = mt5.account_info()
    assert (account.equity, account.margin_mode, account.currency) == (10000.0, 2, "USD")
    [position] = mt5.positions_get(symbol="XAUUSD")
    assert (position.ticket, position.volume, position.sl) == (7, 0.1, 3830.0)
    rates = mt5.copy_rates_from_pos("XAUUSD", 16385, 0, 2)
    assert len(rates) == 2 and rates[0]["open"] == 3800.0 and rates[-1]["close"] == 3799.0
    result = mt5.order_send({"action": 1, "symbol": "XAUUSD", "volume": 0.1})
    assert result.retcode == 10009 and result.request.volume == 0.1
    assert mt5.symbol_info("EURUSD") is None and mt5.last_error() == (-1, "unknown symbol")
    assert mt5.noisy() == 5  # what the package prints does not get into the answers
    with pytest.raises(Mt5Error, match="bad arguments"):
        mt5.boom()
    assert mt5.account_info().login == 1  # still serving after an exception


def test_a_child_that_does_not_answer_is_ended_and_the_next_call_starts_a_new_one(worker):
    mt5 = worker(timeout=1.0)
    assert mt5.initialize() is True
    first = mt5.pid()
    started = time.monotonic()
    assert mt5.sleep(30) is None
    assert time.monotonic() - started < 10
    code, message = mt5.last_error()
    assert code == -10005 and "IPC timeout" in message
    assert not mt5.running
    assert mt5.account_info() is None and mt5.last_error() == NO_CONNECTION  # must connect again
    assert mt5.initialize() is True
    assert mt5.pid() != first and mt5.account_info().equity == 10000.0


def test_a_child_that_dies_is_reported_and_replaced(worker):
    mt5 = worker()
    assert mt5.initialize() is True
    assert mt5.quit_now() is None
    code, message = mt5.last_error()
    assert code == -10002 and "stopped" in message
    assert mt5.initialize() is True and mt5.account_info().login == 1


def test_other_threads_keep_running_while_a_call_waits(worker):
    """The point of the child: the package used to hold the lock for a minute and the Feishu heartbeat stopped."""
    mt5 = worker(timeout=1.5)
    assert mt5.initialize() is True
    ticks = []
    done = threading.Event()

    def heartbeat():
        while not done.is_set():
            ticks.append(time.monotonic())
            time.sleep(0.05)

    beat = threading.Thread(target=heartbeat)
    beat.start()
    try:
        assert mt5.sleep(30) is None
    finally:
        done.set()
        beat.join()
    assert len(ticks) >= 15


def test_the_link_waits_a_minute_after_a_terminal_that_does_not_answer(worker):
    now = [1_800_000_000.0]
    link = Mt5Link(module=worker(timeout=1.0), clock=lambda: now[0])
    assert link.call("account_info").equity == 10000.0
    link.call("hang", 30)
    with pytest.raises(Mt5Error, match="没有回应") as caught:
        link.call("account_info")
    assert caught.value.code == "mt5_down"  # not "log in": the terminal did not answer at all
    now[0] += 30
    with pytest.raises(Mt5Error, match="没有回应"):
        link.call("account_info")  # inside the minute: no new attempt
    now[0] += 31
    assert link.call("account_info").equity == 10000.0  # a new child, connected again


def test_without_the_package_the_link_says_it_is_not_installed(monkeypatch):
    monkeypatch.setattr(mt5_client.importlib.util, "find_spec", lambda name: None)
    with pytest.raises(Mt5Error, match="还没装 MetaTrader5") as caught:
        Mt5Link().call("account_info")
    assert caught.value.code == "mt5_missing"


def test_the_link_runs_the_package_in_a_child_with_the_terminal_wait_added(monkeypatch):
    monkeypatch.setattr(mt5_client.importlib.util, "find_spec", lambda name: object())
    link = Mt5Link(connect_ms=20_000)
    process = link.mt5
    assert isinstance(process, Mt5Process)
    assert process._connect_timeout == 20 + mt5_client.CALL_TIMEOUT and process._timeout == mt5_client.CALL_TIMEOUT


def test_in_a_windows_venv_the_child_is_the_real_interpreter_told_of_the_venv(monkeypatch):
    """.venv\\Scripts\\python.exe only starts the real interpreter: ending it might leave a stuck child behind."""
    monkeypatch.setattr(mt5_worker.os, "name", "nt")
    monkeypatch.setattr(mt5_worker.sys, "executable", r"C:\bot\.venv\Scripts\python.exe")
    monkeypatch.setattr(mt5_worker.sys, "_base_executable", r"C:\Python312\python.exe", raising=False)
    python, env = mt5_worker._python()
    assert python == r"C:\Python312\python.exe" and env["__PYVENV_LAUNCHER__"] == r"C:\bot\.venv\Scripts\python.exe"
    monkeypatch.setattr(mt5_worker.sys, "_base_executable", r"C:\bot\.venv\Scripts\python.exe", raising=False)
    assert mt5_worker._python() == (r"C:\bot\.venv\Scripts\python.exe", None)  # not a venv: as it is
