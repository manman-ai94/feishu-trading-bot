"""Turning what people type into an OKX instrument."""
from __future__ import annotations

import pytest

from bot.instruments import InstrumentResolver
from bot.okx_client import OkxNetworkError

from .helpers import Clock, FakeOkx


def resolver(ttl_seconds: float = 3600.0):
    okx, clock = FakeOkx(), Clock(1_800_000_000.0)
    return InstrumentResolver(okx, clock, ttl_seconds), okx, clock


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("BTC", "BTC-USDT-SWAP"),  # the USDT perpetual stands for a bare ticker
        (" btc ", "BTC-USDT-SWAP"),
        ("比特币", "BTC-USDT-SWAP"),
        ("黄金", "XAU-USDT-SWAP"),
        ("Gold", "XAU-USDT-SWAP"),
        ("BTCUSDT", "BTC-USDT-SWAP"),
        ("BTC永续", "BTC-USDT-SWAP"),
        ("eth-usdt", "ETH-USDT"),  # a full id is taken as typed, here the spot pair
        ("BTC/USDT", "BTC-USDT"),
        ("btc-usd-swap", "BTC-USD-SWAP"),
        ("OKB", "OKB-USDT"),  # no perpetual: the spot pair
        ("LUNA", None),  # listed but suspended
        ("AAPL", None),  # not on OKX at all
        ("", None),
    ],
)
def test_resolve(typed, expected):
    subject, _, _ = resolver()
    assert subject.resolve(typed) == expected


def test_the_list_is_read_once_and_refreshed_after_a_while():
    subject, okx, clock = resolver(ttl_seconds=3600)
    for name in ("BTC", "ETH", "DOGE", "nothing"):
        subject.resolve(name)
    assert okx.instrument_reads == 2  # perpetuals and spot, once each
    clock.advance(3601)
    subject.resolve("BTC")
    assert okx.instrument_reads == 4


def test_without_the_list_a_plain_ticker_is_guessed():
    subject, okx, clock = resolver()
    okx.market_error = OkxNetworkError("连不上 OKX")
    assert subject.resolve("BTC") == "BTC-USDT-SWAP"
    assert subject.resolve("以太坊") == "ETH-USDT-SWAP"
    assert subject.resolve("sol-usdt") == "SOL-USDT"
    assert subject.resolve("今天 怎么样") is None and subject.resolve("follow-up") is None
    assert subject.find_in_text("BTC 和 FOO 怎么看") == ["BTC-USDT-SWAP"]  # only well-known tickers are guessed in a sentence

    okx.market_error = None
    assert subject.resolve("OKB") == "OKB-USDT-SWAP"  # still guessing: the list is not retried at once
    clock.advance(301)
    assert subject.resolve("OKB") == "OKB-USDT"


def test_a_failed_refresh_keeps_the_old_list():
    subject, okx, clock = resolver(ttl_seconds=3600)
    assert subject.resolve("OKB") == "OKB-USDT"
    clock.advance(3601)
    okx.market_error = OkxNetworkError("连不上 OKX")
    assert subject.resolve("OKB") == "OKB-USDT"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("BTC 现在怎么看", ["BTC-USDT-SWAP"]),
        ("btc呢，深一点讲", ["BTC-USDT-SWAP"]),
        ("现在黄金怎么看", ["XAU-USDT-SWAP"]),
        ("ETH 和比特币哪个强", ["ETH-USDT-SWAP", "BTC-USDT-SWAP"]),  # in the order they appear
        ("比特币、大饼、BTC 是一个东西吗", ["BTC-USDT-SWAP"]),
        ("the gold price today", ["XAU-USDT-SWAP"]),
        ("golden cross 是什么意思", []),  # "gold" inside another word
        ("RSI 和 EMA20 怎么用，CPI 几点公布", []),  # trading terms are not tickers
        ("okb 怎么样", []),  # lower case is only trusted for the well-known ones
        ("OKB 怎么样", ["OKB-USDT"]),
        ("帮我看 eth-usdt 现货", ["ETH-USDT"]),
        ("今天行情如何", []),
    ],
)
def test_find_in_text(text, expected):
    subject, _, _ = resolver()
    assert subject.find_in_text(text) == expected


def test_find_in_text_respects_the_limit():
    subject, _, _ = resolver()
    assert subject.find_in_text("BTC ETH DOGE 黄金", limit=2) == ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
