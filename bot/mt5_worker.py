"""MT5 calls in a process of their own, so a terminal that does not answer cannot stop the rest of the bot.

MetaQuotes' MetaTrader5 package keeps Python's global lock while it waits for the terminal. When the
terminal is closed, not logged in or stuck, each call waits about a minute (IPC timeout, -10005), and
for that minute no other thread of the bot runs: the Feishu connection misses its heartbeats and
drops. So the bot hands the calls to a small child process and waits for the answer without the lock.

`Mt5Process` looks like the MetaTrader5 module to `Mt5Link`. Each call is sent to the child
(`python -m bot.mt5_worker`), which makes it and sends back the result. The package answers a stuck
terminal on its own after about a minute; a child that has not answered after `timeout` seconds is
ended, the call returns None with an IPC error, and the next call starts a new child.

Over the child's stdin and stdout each message is a 4-byte length and a pickle. A request is
(name, args, kwargs); an answer is ("ok", value) or ("error", text). The package's named tuples
travel as ("__row__", fields) and come back as `Row`; numpy arrays as lists (of dicts when the
array has named fields, as copy_rates_* returns).
"""
from __future__ import annotations

import importlib
import logging
import os
import pickle
import queue
import struct
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

ROW = "__row__"
HEADER = struct.Struct(">I")
CALL_TIMEOUT = 75.0  # seconds; longer than the package's own wait (about 60) so its answer comes first
STOP_WAIT = 5.0
# Calls that work before initialize. Any other call to a child that has not connected answers "no connection".
WITHOUT_CONNECTION = {"initialize", "last_error", "version", "shutdown"}
NO_CONNECTION = (-10004, "No IPC connection")
HOME = Path(__file__).resolve().parent.parent  # the folder that holds the bot package


# ------------------------------------------------------------------ messages


def write_message(stream, message) -> None:
    data = pickle.dumps(message, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(HEADER.pack(len(data)) + data)
    stream.flush()


def read_message(stream):
    """The next message, or EOFError when the other side has gone."""
    head = _read_exact(stream, HEADER.size)
    (size,) = HEADER.unpack(head)
    return pickle.loads(_read_exact(stream, size))  # noqa: S301  our own child, over its own pipe


def _read_exact(stream, size: int) -> bytes:
    chunks, left = [], size
    while left:
        chunk = stream.read(left)
        if not chunk:
            raise EOFError("the pipe closed")
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def plain(value):
    """What the package returned, as values any Python reads back: no MetaTrader5 or numpy types."""
    if isinstance(value, (str, bytes, int, float, bool)) or value is None:
        return value
    if hasattr(value, "_asdict"):
        return (ROW, {key: plain(item) for key, item in value._asdict().items()})
    if hasattr(value, "dtype") and hasattr(value, "tolist"):  # numpy: arrays and single numbers
        names = getattr(value.dtype, "names", None)
        items = value.tolist()
        if names and isinstance(items, list):
            return [{name: plain(field) for name, field in zip(names, record)} for record in items]
        return plain(items)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


class Row:
    """One record the package returned (an account, a position, a tick...): read by attribute or by key."""

    __slots__ = ("_fields",)

    def __init__(self, fields: dict):
        object.__setattr__(self, "_fields", fields)

    def __getattr__(self, name: str):
        try:
            return self._fields[name]
        except KeyError:
            raise AttributeError(name) from None

    def __getitem__(self, key):
        return self._fields[key]

    def _asdict(self) -> dict:
        return dict(self._fields)

    def __eq__(self, other) -> bool:
        return isinstance(other, Row) and other._fields == self._fields

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"Row({self._fields!r})"


def rebuild(value):
    """`plain`'s output back into rows and tuples, as the package would have returned them."""
    if isinstance(value, tuple) and len(value) == 2 and value[0] == ROW:
        return Row({key: rebuild(item) for key, item in value[1].items()})
    if isinstance(value, list):
        return tuple(rebuild(item) for item in value)
    if isinstance(value, dict):
        return {key: rebuild(item) for key, item in value.items()}
    return value


# ------------------------------------------------------------------ the bot's side


class Mt5Process:
    """Stands for the MetaTrader5 module; every call runs in the child process."""

    def __init__(
        self, timeout: float = CALL_TIMEOUT, connect_timeout: float | None = None, module: str = "", command=None,
        popen=subprocess.Popen,
    ):
        """`module`: what the child imports instead of MetaTrader5 (the tests' stand-in)."""
        self._timeout = timeout
        self._connect_timeout = connect_timeout if connect_timeout is not None else timeout
        if command:
            self._command, self._env = list(command), None
        else:
            python, self._env = _python()
            self._command = [python, "-m", "bot.mt5_worker"] + (["--module", module] if module else [])
        self._popen = popen
        self._lock = threading.Lock()
        self._child = None
        self._answers: queue.Queue | None = None
        self._connected = False
        self._own_error: tuple[int, str] | None = None  # an error the child could not report: it is gone

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            return self._call(name, args, kwargs)

        call.__name__ = name
        return call

    @property
    def running(self) -> bool:
        return self._child is not None and self._child.poll() is None

    def _call(self, name: str, args: tuple, kwargs: dict):
        with self._lock:
            if name == "last_error" and (self._own_error is not None or not self.running):
                return self._own_error or NO_CONNECTION
            if name not in WITHOUT_CONNECTION and not self._connected:
                self._own_error = NO_CONNECTION
                return None
            if name == "shutdown" and not self.running:
                self._connected = False
                return None
            self._own_error = None
            try:
                answers = self._start()
                write_message(self._child.stdin, (name, args, kwargs))
            except (OSError, ValueError) as exc:  # could not start it, or it has just gone
                self._end(-10001, f"the MT5 worker could not be reached: {exc}")
                return None
            wait = self._connect_timeout if name == "initialize" else self._timeout
            try:
                kind, value = answers.get(timeout=wait)
            except queue.Empty:
                log.warning("mt5: no answer to %s in %.0f s; ending the MT5 worker", name, wait)
                self._end(-10005, f"IPC timeout (no answer in {wait:.0f} s)")
                return None
            if kind == "gone":
                self._end(-10002, f"the MT5 worker stopped ({value})")
                return None
            if kind == "error":
                from .mt5_client import Mt5Error  # noqa: PLC0415  mt5_client imports this module

                raise Mt5Error("mt5_error", f"MT5 调用出错（{name}：{value}）")
            result = rebuild(value)
            if name == "initialize":
                self._connected = bool(result)
            elif name == "shutdown":
                self._connected = False
            return result

    def _start(self) -> queue.Queue:
        if self.running and self._answers is not None:
            return self._answers
        self._stop()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        child = self._popen(
            self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(HOME),
            env=self._env, creationflags=flags,
        )
        answers: queue.Queue = queue.Queue()
        threading.Thread(target=_listen, args=(child.stdout, answers), name="mt5-worker", daemon=True).start()
        self._child, self._answers, self._connected = child, answers, False
        log.info("mt5: started the MT5 worker (pid %s)", getattr(child, "pid", "?"))
        return answers

    def _end(self, code: int, message: str) -> None:
        self._stop()
        self._own_error = (code, message)

    def _stop(self) -> None:
        child, self._child, self._answers, self._connected = self._child, None, None, False
        if child is None:
            return
        try:
            child.kill()
        except OSError:
            pass
        try:
            child.stdin.close()
        except (OSError, AttributeError):
            pass
        # Not its stdout: the listening thread may be reading it, and closing would wait for that read.
        # The thread closes it when the pipe ends.
        try:
            child.wait(timeout=STOP_WAIT)
        except (subprocess.TimeoutExpired, OSError):
            log.warning("mt5: the MT5 worker (pid %s) did not end in %.0f s", getattr(child, "pid", "?"), STOP_WAIT)

    def close(self) -> None:
        with self._lock:
            self._stop()


def _python() -> tuple[str, dict | None]:
    """The interpreter for the child. In a Windows venv, .venv\\Scripts\\python.exe only starts the real
    interpreter as its own child; ending it might leave that one running. So start the real one with
    the venv named, as multiprocessing does on Windows."""
    base = getattr(sys, "_base_executable", "") or sys.executable
    if os.name == "nt" and os.path.normcase(base) != os.path.normcase(sys.executable):
        return base, {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}
    return sys.executable, None


def _listen(stream, answers: queue.Queue) -> None:
    """Puts each answer of one child on its own queue; "gone" when the child's output ends."""
    while True:
        try:
            message = read_message(stream)
        except (EOFError, OSError, ValueError) as exc:
            answers.put(("gone", str(exc) or type(exc).__name__))
            try:
                stream.close()
            except OSError:
                pass
            return
        except Exception as exc:  # noqa: BLE001  an answer that does not unpickle: report it, keep reading
            answers.put(("error", f"{type(exc).__name__}: {exc}"))
            continue
        answers.put(message)


# ------------------------------------------------------------------ the child's side


def serve(module, inbox, outbox) -> None:
    """Answers requests until the bot closes the pipe."""
    while True:
        try:
            name, args, kwargs = read_message(inbox)
        except EOFError:
            return
        try:
            answer = ("ok", plain(getattr(module, name)(*args, **kwargs)))
        except Exception as exc:  # noqa: BLE001  the package refused the call: tell the bot, keep serving
            answer = ("error", f"{type(exc).__name__}: {exc}")
        write_message(outbox, answer)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    name = argv[argv.index("--module") + 1] if "--module" in argv else "MetaTrader5"
    # The pipes are ours alone: anything the package prints goes nowhere, not into the answers.
    inbox = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
    outbox = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, sys.stdin.fileno())
    os.dup2(null, sys.stdout.fileno())
    sys.stdin = sys.stdout = open(os.devnull, "r+", encoding="utf-8")  # noqa: SIM115  for the life of the child
    try:
        module = importlib.import_module(name)
    except ImportError as exc:
        module = _Missing(f"{type(exc).__name__}: {exc}")
    serve(module, inbox, outbox)
    return 0


class _Missing:
    """The package is not installed: every call says so."""

    def __init__(self, reason: str):
        self._reason = reason

    def __getattr__(self, name: str):
        def refuse(*_args, **_kwargs):
            raise ImportError(f"MetaTrader5 is not installed ({self._reason})")

        return refuse


if __name__ == "__main__":
    sys.exit(main())
