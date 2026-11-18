"""The trading client: what exactly is sent to OKX, and how its answers are read."""
from __future__ import annotations

import json

import pytest
import requests

from bot.okx_client import OkxClient, OkxError, OkxNetworkError, sign
from bot.okx_trade import ORDER_TAG, OkxTradeClient

from .test_okx_client import FakeResponse, FakeSession, ok


class PostingSession(FakeSession):
    """FakeSession that also takes POST requests and remembers their bodies."""

    def __init__(self, routes: dict[str, list]):
        super().__init__(routes)
        self.posts: list[tuple[str, dict | list, dict, bytes]] = []

    def post(self, url, data=None, headers=None, timeout=None):
        path = url.split("okx.test", 1)[1]
        self.posts.append((path, json.loads(data.decode("utf-8")), headers or {}, data))
        answers = self.routes[path]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, FakeResponse) else FakeResponse(answer)


def trader(routes, **kwargs) -> tuple[OkxTradeClient, PostingSession]:
    session = PostingSession(routes)
    return OkxTradeClient("key", "secret", "pass", "https://okx.test", session=session, **kwargs), session


ORDER = "/api/v5/trade/order"
PLACED = ok([{"ordId": "7001", "clOrdId": "ftb1", "sCode": "0", "sMsg": ""}])


def test_only_the_trading_client_can_change_anything():
    changing = {"place_order", "place_trigger", "cancel_order", "close_position", "place_stop", "amend_stop", "cancel_stops"}
    assert changing <= set(dir(OkxTradeClient))
    assert not changing & set(dir(OkxClient))
    # Neither of them can move funds.
    for name in dir(OkxTradeClient):
        assert not any(word in name.lower() for word in ("withdraw", "transfer"))


def test_a_post_is_signed_over_the_exact_body_that_is_sent():
    okx, session = trader({ORDER: [PLACED]})
    okx.place_order(inst_id="XAU-USDT-SWAP", td_mode="cross", side="buy", ord_type="market", sz="100", cl_ord_id="ftb1")

    path, body, headers, raw = session.posts[0]
    assert path == ORDER
    assert headers["OK-ACCESS-SIGN"] == sign("secret", headers["OK-ACCESS-TIMESTAMP"], "POST", ORDER, raw.decode("utf-8"))
    assert headers["OK-ACCESS-KEY"] == "key" and headers["Content-Type"] == "application/json"
    assert "x-simulated-trading" not in headers


def test_demo_orders_carry_the_simulated_trading_header():
    okx, session = trader({ORDER: [PLACED]}, demo=True)
    okx.place_order(inst_id="XAU-USDT-SWAP", td_mode="cross", side="buy", ord_type="market", sz="1", cl_ord_id="ftb1")
    assert session.posts[0][2]["x-simulated-trading"] == "1"


def test_market_order_fields():
    okx, session = trader({ORDER: [PLACED]})
    assert okx.place_order(inst_id="XAU-USDT-SWAP", td_mode="cross", side="buy", ord_type="market", sz="100", cl_ord_id="ftb1") == "7001"
    assert session.posts[0][1] == {
        "instId": "XAU-USDT-SWAP", "tdMode": "cross", "side": "buy", "ordType": "market", "sz": "100",
        "clOrdId": "ftb1", "tag": ORDER_TAG,
    }  # fmt: skip


def test_limit_order_with_attached_stops_in_hedge_mode():
    okx, session = trader({ORDER: [PLACED]})
    okx.place_order(
        inst_id="BTC-USDT-SWAP", td_mode="isolated", side="sell", ord_type="limit", sz="3", cl_ord_id="ftb2",
        px="68000", pos_side="short", sl_trigger_px="69000", tp_trigger_px="65000",
    )  # fmt: skip
    body = session.posts[0][1]
    assert body["px"] == "68000" and body["posSide"] == "short" and body["tdMode"] == "isolated"
    assert "reduceOnly" not in body
    assert body["attachAlgoOrds"] == [
        {
            "slTriggerPx": "69000", "slOrdPx": "-1", "slTriggerPxType": "last",
            "tpTriggerPx": "65000", "tpOrdPx": "-1", "tpTriggerPxType": "last",
        }
    ]  # fmt: skip


def test_reduce_only_order_for_a_partial_close():
    okx, session = trader({ORDER: [PLACED]})
    okx.place_order(inst_id="XAU-USDT-SWAP", td_mode="cross", side="sell", ord_type="market", sz="50", cl_ord_id="ftb3", reduce_only=True)
    body = session.posts[0][1]
    assert body["reduceOnly"] is True and "attachAlgoOrds" not in body and "posSide" not in body


def test_a_refused_order_raises_with_okx_own_reason():
    refused = {"code": "1", "msg": "All operations failed", "data": [{"ordId": "", "sCode": "51008", "sMsg": "Insufficient USDT margin"}]}
    okx, _ = trader({ORDER: [refused]})
    with pytest.raises(OkxError) as caught:
        okx.place_order(inst_id="XAU-USDT-SWAP", td_mode="cross", side="buy", ord_type="market", sz="100", cl_ord_id="ftb1")
    assert caught.value.code == "51008" and "Insufficient" in caught.value.message


def test_a_refusal_without_details_still_raises():
    okx, _ = trader({ORDER: [{"code": "50011", "msg": "Too many requests", "data": []}]})
    with pytest.raises(OkxError) as caught:
        okx.place_order(inst_id="XAU-USDT-SWAP", td_mode="cross", side="buy", ord_type="market", sz="100", cl_ord_id="ftb1")
    assert caught.value.code == "50011"


def test_a_wrong_clock_is_corrected_and_the_order_sent_once_more():
    stale = {"code": "50102", "msg": "Timestamp request expired", "data": []}
    okx, session = trader({ORDER: [stale, PLACED], "/api/v5/public/time": [ok([{"ts": "1800000000000"}])]})
    assert okx.place_order(inst_id="XAU-USDT-SWAP", td_mode="cross", side="buy", ord_type="market", sz="1", cl_ord_id="ftb1") == "7001"
    assert len(session.posts) == 2
    # The second refusal is final: no endless loop.
    okx, session = trader({ORDER: [stale], "/api/v5/public/time": [ok([{"ts": "1800000000000"}])]})
    with pytest.raises(OkxError):
        okx.place_order(inst_id="XAU-USDT-SWAP", td_mode="cross", side="buy", ord_type="market", sz="1", cl_ord_id="ftb1")
    assert len(session.posts) == 2


def test_a_lost_connection_is_reported_as_unknown_and_never_resent():
    okx, session = trader({ORDER: [requests.ConnectionError("reset")]})
    with pytest.raises(OkxNetworkError):
        okx.place_order(inst_id="XAU-USDT-SWAP", td_mode="cross", side="buy", ord_type="market", sz="1", cl_ord_id="ftb1")
    assert len(session.posts) == 1


def test_an_unreadable_answer_is_an_error():
    okx, _ = trader({ORDER: [FakeResponse(ValueError("not json"), 502)]})
    with pytest.raises(OkxError) as caught:
        okx.place_order(inst_id="XAU-USDT-SWAP", td_mode="cross", side="buy", ord_type="market", sz="1", cl_ord_id="ftb1")
    assert caught.value.code == "bad_response" and caught.value.http_status == 502


def test_cancel_order_and_close_position_bodies():
    okx, session = trader(
        {
            "/api/v5/trade/cancel-order": [ok([{"ordId": "7001", "sCode": "0"}])],
            "/api/v5/trade/close-position": [ok([{"instId": "XAU-USDT-SWAP", "posSide": "net"}])],
        }
    )
    okx.cancel_order("XAU-USDT-SWAP", "7001")
    okx.close_position("XAU-USDT-SWAP", "cross", cl_ord_id="ftb9")
    okx.close_position("BTC-USDT-SWAP", "isolated", pos_side="long")
    assert session.posts[0][1] == {"instId": "XAU-USDT-SWAP", "ordId": "7001"}
    assert session.posts[1][1] == {"instId": "XAU-USDT-SWAP", "mgnMode": "cross", "autoCxl": True, "tag": ORDER_TAG, "clOrdId": "ftb9"}
    assert session.posts[2][1] == {"instId": "BTC-USDT-SWAP", "mgnMode": "isolated", "autoCxl": True, "tag": ORDER_TAG, "posSide": "long"}


ALGO = "/api/v5/trade/order-algo"


def test_a_stop_for_the_whole_position_in_net_mode():
    okx, session = trader({ALGO: [ok([{"algoId": "9001", "sCode": "0"}])]})
    assert okx.place_stop(inst_id="XAU-USDT-SWAP", td_mode="cross", side="sell", cl_ord_id="ftb4", sl_trigger_px="3800") == "9001"
    assert session.posts[0][1] == {
        "instId": "XAU-USDT-SWAP", "tdMode": "cross", "side": "sell", "ordType": "conditional", "closeFraction": "1",
        "algoClOrdId": "ftb4", "tag": ORDER_TAG, "reduceOnly": True,
        "slTriggerPx": "3800", "slOrdPx": "-1", "slTriggerPxType": "last",
    }  # fmt: skip


def test_stop_loss_and_take_profit_together_make_an_oco_order():
    okx, session = trader({ALGO: [ok([{"algoId": "9002", "sCode": "0"}])]})
    okx.place_stop(
        inst_id="BTC-USDT-SWAP", td_mode="cross", side="buy", cl_ord_id="ftb5", pos_side="short",
        sl_trigger_px="69000", tp_trigger_px="65000",
    )  # fmt: skip
    body = session.posts[0][1]
    assert body["ordType"] == "oco" and body["posSide"] == "short" and "reduceOnly" not in body
    assert body["tpTriggerPx"] == "65000" and body["tpOrdPx"] == "-1" and body["slTriggerPx"] == "69000"


def test_a_breakout_is_a_trigger_order_at_market_with_its_stop_attached():
    okx, session = trader({ALGO: [ok([{"algoId": "9100", "sCode": "0"}])]})
    algo = okx.place_trigger(
        inst_id="XAU-USDT-SWAP", td_mode="cross", side="buy", sz="1000", trigger_px="3900", cl_ord_id="ftb7", sl_trigger_px="3870",
    )  # fmt: skip
    assert algo == "9100"
    assert session.posts[0][1] == {
        "instId": "XAU-USDT-SWAP", "tdMode": "cross", "side": "buy", "ordType": "trigger", "sz": "1000", "triggerPx": "3900",
        "orderPx": "-1", "triggerPxType": "last", "algoClOrdId": "ftb7", "tag": ORDER_TAG,
        "attachAlgoOrds": [{"slTriggerPx": "3870", "slOrdPx": "-1", "slTriggerPxType": "last"}],
    }  # fmt: skip


def test_a_stop_needs_at_least_one_price():
    okx, session = trader({ALGO: [ok([])]})
    with pytest.raises(ValueError):
        okx.place_stop(inst_id="XAU-USDT-SWAP", td_mode="cross", side="sell", cl_ord_id="ftb6")
    assert session.posts == []


def test_amending_and_cancelling_stops():
    okx, session = trader(
        {
            "/api/v5/trade/amend-algos": [ok([{"algoId": "9001", "sCode": "0"}])],
            "/api/v5/trade/cancel-algos": [ok([{"algoId": "9001", "sCode": "0"}])],
        }
    )
    okx.amend_stop("XAU-USDT-SWAP", "9001", sl_trigger_px="3820")
    okx.cancel_stops([("XAU-USDT-SWAP", "9001"), ("XAU-USDT-SWAP", "9002")])
    okx.cancel_stops([])
    assert session.posts[0][1] == {
        "instId": "XAU-USDT-SWAP", "algoId": "9001",
        "newSlTriggerPx": "3820", "newSlOrdPx": "-1", "newSlTriggerPxType": "last",
    }  # fmt: skip
    assert session.posts[1][1] == [{"instId": "XAU-USDT-SWAP", "algoId": "9001"}, {"instId": "XAU-USDT-SWAP", "algoId": "9002"}]
    assert len(session.posts) == 2


def test_one_failed_item_of_a_batch_raises():
    mixed = {"code": "2", "msg": "Bulk operation partially succeeded", "data": [
        {"algoId": "9001", "sCode": "0", "sMsg": ""},
        {"algoId": "9002", "sCode": "51400", "sMsg": "Cancellation failed as the order does not exist"},
    ]}  # fmt: skip
    okx, _ = trader({"/api/v5/trade/cancel-algos": [mixed]})
    with pytest.raises(OkxError) as caught:
        okx.cancel_stops([("XAU-USDT-SWAP", "9001"), ("XAU-USDT-SWAP", "9002")])
    assert caught.value.code == "51400"
