"""Turns what people type ("BTC", "比特币", "btc-usdt") into an OKX instrument.

The list of instruments comes from OKX's public endpoint and is kept for a
few hours. When the list cannot be read, a plain ticker is guessed as
`<TICKER>-USDT-SWAP`; the market-data call that follows then says whether the
guess exists.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable

from .okx_client import OkxError, OkxNetworkError

log = logging.getLogger(__name__)

# Names people use in chat -> the base currency OKX uses.
ALIASES = {
    "黄金": "XAU",
    "金价": "XAU",
    "gold": "XAU",
    "xauusd": "XAU",
    "白银": "XAG",
    "silver": "XAG",
    "比特币": "BTC",
    "大饼": "BTC",
    "bitcoin": "BTC",
    "以太坊": "ETH",
    "以太": "ETH",
    "ethereum": "ETH",
    "索拉纳": "SOL",
    "solana": "SOL",
    "狗狗币": "DOGE",
    "dogecoin": "DOGE",
    "瑞波": "XRP",
    "ripple": "XRP",
    "币安币": "BNB",
    "莱特币": "LTC",
    "litecoin": "LTC",
    "艾达币": "ADA",
    "波卡": "DOT",
}

# Tickers that are safe to recognise in lower case too. Anything else must be
# typed in capitals, because many tickers are also ordinary words.
WELL_KNOWN = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "LTC", "XAU", "XAG", "AVAX", "SHIB", "PEPE", "BCH"}

# Capitalised words in a trading chat that are not a request for that coin.
NOT_TICKERS = {
    "AI", "OK", "OKX", "USD", "USDT", "USDC", "RSI", "EMA", "MA", "ATR", "MACD", "KDJ", "BOLL", "API", "ID", "APP",
    "BOT", "ETF", "CPI", "PPI", "GDP", "PCE", "PMI", "FED", "FOMC", "NFP", "ADP", "ISM", "CEO", "IPO", "USA", "US",
    "UK", "EU", "CN", "AM", "PM", "UTC", "PE", "PB", "ROE", "VS", "TP", "SL", "PNL", "OI", "ATH", "ATL", "DCA",
    "THE", "AND", "FOR", "NOT", "ONE", "ALL", "NOW", "BUY", "SELL", "LONG", "SHORT", "UP", "DOWN", "HIGH", "LOW",
    "OPEN", "CLOSE", "TOP", "NEWS", "VPS", "MT5", "MT4", "PDF", "URL", "DXY", "SPX", "NDX", "VIX",
}  # fmt: skip

QUOTES = ("USDT", "USD", "USDC")
# Which market stands for a ticker when several exist: lower is better.
_PREFERENCE = {("USDT", "SWAP"): 0, ("USDT", "SPOT"): 1, ("USD", "SWAP"): 2, ("USDC", "SWAP"): 3, ("USDC", "SPOT"): 4}
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]{1,11}(?:[-/][A-Za-z0-9]{2,6}){0,2}")
_TICKER_SHAPE = re.compile(r"[A-Z][A-Z0-9]{1,9}")


class InstrumentResolver:
    def __init__(self, okx, clock: Callable[[], float] = time.time, ttl_seconds: float = 6 * 3600):
        self._okx = okx
        self._clock = clock
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._loaded_at: float | None = None
        self._ids: set[str] = set()
        self._by_base: dict[str, str] = {}

    # ------------------------------------------------------------------ list

    def _ensure_loaded(self) -> bool:
        """True when a list of instruments is at hand (fresh or not)."""
        with self._lock:
            now = self._clock()
            if self._loaded_at is not None and now - self._loaded_at < self._ttl:
                return bool(self._ids)
            ids: set[str] = set()
            try:
                for inst_type in ("SWAP", "SPOT"):
                    for row in self._okx.instruments(inst_type):
                        inst_id = str(row.get("instId", "")).upper()
                        if inst_id and row.get("state", "live") == "live":
                            ids.add(inst_id)
            except (OkxError, OkxNetworkError) as exc:
                log.warning("could not read the list of OKX instruments: %s", exc)
                # Keep whatever was known and try again in a few minutes.
                self._loaded_at = now - self._ttl + 300
                return bool(self._ids)
            best: dict[str, tuple[int, str]] = {}
            for inst_id in ids:
                parts = inst_id.split("-")
                if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != "SWAP"):
                    continue
                rank = _PREFERENCE.get((parts[1], "SWAP" if len(parts) == 3 else "SPOT"))
                if rank is not None and (parts[0] not in best or rank < best[parts[0]][0]):
                    best[parts[0]] = (rank, inst_id)
            self._ids = ids
            self._by_base = {base: inst_id for base, (_, inst_id) in best.items()}
            self._loaded_at = now
            return bool(ids)

    # --------------------------------------------------------------- resolve

    def resolve(self, symbol: str) -> str | None:
        """One name -> one instrument id, or None when OKX has nothing like it."""
        raw = symbol.strip()
        if not raw:
            return None
        alias = ALIASES.get(raw.lower())
        upper = alias or raw.upper().replace("/", "-").replace("_", "-").replace(" ", "")
        for ending in ("永续", "合约", "现货"):
            upper = upper.removesuffix(ending)
        if upper.endswith("-PERP"):
            upper = upper[: -len("-PERP")]
        have_list = self._ensure_loaded()
        if not have_list:
            return self._guess(upper)
        if upper in self._ids:
            return upper
        if upper in self._by_base:
            return self._by_base[upper]
        for quote in QUOTES:  # BTCUSDT, BTC-USDT-PERP and the like
            if upper.endswith(quote) and len(upper) > len(quote):
                base = upper[: -len(quote)].rstrip("-")
                if base in self._by_base:
                    return self._by_base[base]
        return None

    @staticmethod
    def _guess(upper: str) -> str | None:
        parts = upper.split("-")
        if len(parts) in (2, 3) and parts[0] and parts[1] in QUOTES and (len(parts) == 2 or parts[2] == "SWAP"):
            return upper
        return f"{upper}-USDT-SWAP" if _TICKER_SHAPE.fullmatch(upper) else None

    def find_in_text(self, text: str, limit: int = 3) -> list[str]:
        """The instruments a message talks about, in the order they appear."""
        found: list[tuple[int, str]] = []
        lowered = text.lower()
        for alias, base in ALIASES.items():
            if alias.isascii():  # an English name must stand alone, not sit inside a longer word
                hit = re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered)
                position = hit.start() if hit else -1
            else:
                position = lowered.find(alias)
            if position < 0:
                continue
            inst_id = self.resolve(base)
            if inst_id:
                found.append((position, inst_id))
        for match in _TOKEN.finditer(text):
            token = match.group(0)
            upper = token.upper()
            if upper in NOT_TICKERS:
                continue
            compound = "-" in token or "/" in token
            if not compound and token != upper and upper not in WELL_KNOWN:
                continue  # an ordinary word in lower case
            inst_id = self.resolve(token)
            if inst_id and (self._ids or upper in WELL_KNOWN or compound):
                found.append((match.start(), inst_id))
        ordered: list[str] = []
        for _, inst_id in sorted(found):
            if inst_id not in ordered:
                ordered.append(inst_id)
        return ordered[:limit]
