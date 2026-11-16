from __future__ import annotations

import pytest
import requests

from bot.okx_client import OkxClient, OkxError, OkxNetworkError, attach_stops, iso_timestamp, sign

from .helpers import make_position


class FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Answers by request path (without the query string), in order."""

    def __init__(self, routes: dict[str, list]):
        self.routes = {path: list(answers) for path, answers in routes.items()}
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, headers=None, timeout=None):
        self.requests.append((url, headers or {}))
        path = url.split("okx.test", 1)[1].split("?", 1)[0]
        answers = self.routes[path]
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, FakeResponse) else FakeResponse(answer)


def ok(data):
    return {"code": "0", "msg": "", "data": data}


XAU_RULES = ok(
    [{"instId": "XAU-USDT-SWAP", "instType": "SWAP", "ctVal": "0.001", "ctMult": "1", "ctValCcy": "XAU", "settleCcy": "USDT",
      "ctType": "linear", "lotSz": "1", "minSz": "1", "tickSz": "0.1", "state": "live"}]
)  # fmt: skip


def client(routes, **kwargs) -> tuple[OkxClient, FakeSession]:
    session = FakeSession(routes)
    return OkxClient("key", "secret", "pass", "https://okx.test", session=session, **kwargs), session


def test_sign_matches_an_independent_hmac():
    # Expected value produced with: openssl dgst -sha256 -hmac <secret> -binary | base64
    signature = sign(
        "22582BD0CFF14C41EDBF1AB98506286D", "2020-12-08T09:08:57.715Z", "get", "/api/v5/account/balance?ccy=BTC"
    )
    assert signature == "HiZhvSfMtWJA3uUIVXV3a/bSXNPCWvYFXoGCVS8V4zY="


def test_iso_timestamp_has_milliseconds_and_z():
    assert iso_timestamp(1607418537.715) == "2020-12-08T09:08:57.715Z"
    assert iso_timestamp(0) == "1970-01-01T00:00:00.000Z"


def test_private_request_is_signed_over_path_and_query():
    okx, session = client({"/api/v5/account/positions": [ok([])]})
    okx.positions("SWAP")
    url, headers = session.requests[0]
    assert url == "https://okx.test/api/v5/account/positions?instType=SWAP"
    assert headers["OK-ACCESS-KEY"] == "key"
    assert headers["OK-ACCESS-PASSPHRASE"] == "pass"
    expected = sign("secret", headers["OK-ACCESS-TIMESTAMP"], "GET", "/api/v5/account/positions?instType=SWAP")
    assert headers["OK-ACCESS-SIGN"] == expected
    assert "x-simulated-trading" not in headers


def test_public_request_carries_no_credentials():
    okx, session = client({"/api/v5/market/ticker": [ok([{"instId": "XAU-USDT-SWAP", "last": "2650.5", "open24h": "2600"}])]})
    ticker = okx.ticker("XAU-USDT-SWAP")
    assert ticker.last == 2650.5
    assert ticker.change_24h_pct == pytest.approx(1.9423, abs=1e-3)
    assert not any(name.startswith("OK-ACCESS") for name in session.requests[0][1])


def test_demo_mode_sends_the_simulated_trading_header():
    okx, session = client({"/api/v5/account/balance": [ok([{"totalEq": "100"}])]}, demo=True)
    assert okx.balance().total_eq == 100.0
    assert session.requests[0][1]["x-simulated-trading"] == "1"


def test_private_request_without_keys_fails_before_sending():
    session = FakeSession({})
    okx = OkxClient(base_url="https://okx.test", session=session)
    with pytest.raises(OkxError) as raised:
        okx.balance()
    assert raised.value.code == "no_keys"
    assert session.requests == []


def test_error_code_becomes_okx_error():
    okx, _ = client({"/api/v5/account/balance": [{"code": "50111", "msg": "Invalid OK-ACCESS-KEY", "data": []}]})
    with pytest.raises(OkxError) as raised:
        okx.balance()
    assert raised.value.code == "50111"
    assert "Invalid OK-ACCESS-KEY" in str(raised.value)


def test_timestamp_error_resyncs_the_clock_and_retries_once():
    okx, session = client(
        {
            "/api/v5/account/balance": [{"code": "50102", "msg": "Timestamp request expired", "data": []}, ok([{"totalEq": "7"}])],
            "/api/v5/public/time": [ok([{"ts": "1700000000000"}])],
        }
    )
    assert okx.balance().total_eq == 7.0
    assert [url.split("okx.test")[1] for url, _ in session.requests] == [
        "/api/v5/account/balance",
        "/api/v5/public/time",
        "/api/v5/account/balance",
    ]
    # The second attempt is stamped with the server's clock, not the local one.
    assert session.requests[2][1]["OK-ACCESS-TIMESTAMP"].startswith("2023-11-14T22:13:20")


def test_timestamp_error_twice_is_reported():
    expired = {"code": "50102", "msg": "Timestamp request expired", "data": []}
    okx, _ = client({"/api/v5/account/balance": [expired], "/api/v5/public/time": [ok([{"ts": "1700000000000"}])]})
    with pytest.raises(OkxError) as raised:
        okx.balance()
    assert raised.value.code == "50102"


def test_network_failure_and_unreadable_answer():
    okx, _ = client({"/api/v5/account/balance": [requests.ConnectionError("no route")]})
    with pytest.raises(OkxNetworkError):
        okx.balance()

    okx, _ = client({"/api/v5/account/balance": [FakeResponse(ValueError("not json"), 403)]})
    with pytest.raises(OkxError) as raised:
        okx.balance()
    assert raised.value.code == "bad_response"
    assert raised.value.http_status == 403


def test_candles_come_back_oldest_first_with_the_confirm_flag():
    rows = [
        ["3000", "12", "13", "11", "12.5", "9", "0", "0", "0"],  # newest, still forming
        ["2000", "11", "12", "10", "11.5", "8", "0", "0", "1"],
        ["1000", "10", "11", "9", "10.5", "7", "0", "0", "1"],
        ["bad"],
    ]
    okx, session = client({"/api/v5/market/candles": [ok(rows)]})
    candles = okx.candles("XAU-USDT-SWAP", "1H", 500)
    assert [candle.ts for candle in candles] == [1000, 2000, 3000]
    assert [candle.confirmed for candle in candles] == [True, True, False]
    assert candles[0].close == 10.5
    assert "limit=300" in session.requests[0][0]  # OKX allows at most 300 per request


def test_account_state_attaches_pending_stops():
    position_row = {"instId": "XAU-USDT-SWAP", "instType": "SWAP", "posSide": "net", "mgnMode": "cross", "pos": "2", "avgPx": "2650"}
    okx, session = client(
        {
            "/api/v5/account/balance": [ok([{"totalEq": "10000", "mgnRatio": "12.5"}])],
            "/api/v5/account/positions": [ok([position_row])],
            "/api/v5/trade/orders-algo-pending": [
                ok([{"instId": "XAU-USDT-SWAP", "side": "sell", "posSide": "net", "algoId": "9", "slTriggerPx": "2630", "tpTriggerPx": ""}])
            ],
            "/api/v5/public/instruments": [XAU_RULES],
        }
    )
    state = okx.account_state()
    assert state.snapshot.total_eq == 10000.0
    assert state.positions[0].stop_loss == 2630.0
    assert state.positions[0].take_profit is None
    assert state.positions[0].coins == 0.002 and state.positions[0].coin_ccy == "XAU"  # 2 contracts of 0.001 XAU
    okx.account_state()
    assert len([url for url, _ in session.requests if "/public/instruments" in url]) == 1  # the contract size is remembered


def test_a_spot_mode_account_reports_what_is_free_as_a_balance():
    """Spot mode has no margin, so OKX leaves availEq empty and says what is free in availBal."""
    balance = {
        "totalEq": "80107.15", "adjEq": "", "upl": "", "mgnRatio": "",
        "details": [
            {"ccy": "USDT", "eq": "5000", "eqUsd": "5001.2", "availEq": "", "availBal": "5000", "upl": "", "mgnRatio": ""},
            {"ccy": "BTC", "eq": "1", "eqUsd": "64684.5", "availEq": "", "availBal": "1", "upl": "", "mgnRatio": ""},
        ],
    }  # fmt: skip
    okx, _ = client(
        {
            "/api/v5/account/balance": [ok([balance])],
            "/api/v5/account/positions": [ok([])],
            "/api/v5/trade/orders-algo-pending": [ok([])],
        }
    )
    usdt = okx.account_state().snapshot.currencies[1]
    assert usdt.ccy == "USDT" and usdt.avail_eq is None and usdt.avail_bal == 5000.0


def test_a_futures_mode_account_reports_margin_per_currency():
    """An account that keeps one margin pool per currency leaves the account-level fields empty."""
    balance = {
        "totalEq": "5021.5", "adjEq": "", "upl": "", "mgnRatio": "", "imr": "",
        "details": [
            {"ccy": "BTC", "eq": "0.00001", "eqUsd": "0.65", "availEq": "0.00001", "upl": "0", "mgnRatio": ""},
            {"ccy": "USDT", "eq": "5024.4", "eqUsd": "5020.85", "availEq": "3100.25", "upl": "412.5", "mgnRatio": "6.25"},
        ],
    }  # fmt: skip
    position_row = {
        "instId": "XAU-USDT-SWAP", "instType": "SWAP", "posSide": "net", "mgnMode": "cross", "pos": "5000", "avgPx": "3800",
        "markPx": "3882.5", "upl": "412.5", "uplRatio": "1.0855", "lever": "50", "liqPx": "3100.4", "notionalUsd": "19398.7", "ccy": "USDT",
    }  # fmt: skip
    okx, _ = client(
        {
            "/api/v5/account/balance": [ok([balance])],
            "/api/v5/account/positions": [ok([position_row])],
            "/api/v5/trade/orders-algo-pending": [ok([])],
            "/api/v5/public/instruments": [XAU_RULES],
        }
    )
    state = okx.account_state()
    snapshot = state.snapshot
    assert snapshot.upl is None and snapshot.mgn_ratio == 6.25  # taken from the currency that has a position
    assert [item.ccy for item in snapshot.currencies] == ["USDT", "BTC"]  # largest first
    assert snapshot.currencies[0].avail_eq == 3100.25 and snapshot.currencies[0].upl == 412.5
    [position] = state.positions
    assert position.coins == 5.0 and position.coin_ccy == "XAU" and position.pnl_ccy == "USDT"


def test_positions_survive_when_the_contract_size_cannot_be_read():
    position_row = {"instId": "XAU-USDT-SWAP", "instType": "SWAP", "posSide": "net", "mgnMode": "cross", "pos": "3"}
    okx, _ = client(
        {
            "/api/v5/account/balance": [ok([{}])],
            "/api/v5/account/positions": [ok([position_row])],
            "/api/v5/trade/orders-algo-pending": [ok([])],
            "/api/v5/public/instruments": [{"code": "51001", "msg": "Instrument ID does not exist", "data": []}],
        }
    )
    [position] = okx.account_state().positions
    assert position.qty == 3.0 and position.coins is None


def test_margin_ratio_falls_back_to_the_cross_position():
    position_row = {"instId": "XAU-USDT-SWAP", "instType": "SWAP", "posSide": "net", "mgnMode": "cross", "pos": "3", "mgnRatio": "5.25"}
    balance = {"totalEq": "900", "mgnRatio": "", "details": [{"ccy": "USDT", "eq": "900.5", "eqUsd": "900"}]}
    okx, _ = client(
        {
            "/api/v5/account/balance": [ok([balance])],
            "/api/v5/account/positions": [ok([position_row])],
            "/api/v5/trade/orders-algo-pending": [ok([])],
            "/api/v5/public/instruments": [XAU_RULES],
        }
    )
    assert okx.account_state().snapshot.mgn_ratio == 5.25


def test_account_state_keeps_positions_when_stops_cannot_be_read():
    position_row = {"instId": "XAU-USDT-SWAP", "posSide": "net", "mgnMode": "cross", "pos": "-1"}
    okx, _ = client(
        {
            "/api/v5/account/balance": [ok([{}])],
            "/api/v5/account/positions": [ok([position_row])],
            "/api/v5/trade/orders-algo-pending": [{"code": "51000", "msg": "Parameter ordType error", "data": []}],
        }
    )
    state = okx.account_state()
    assert len(state.positions) == 1
    assert state.positions[0].direction == "short"


def test_attach_stops_in_net_mode_matches_the_closing_side():
    long_position = make_position(stops=())
    orders = [
        {"instId": "XAU-USDT-SWAP", "side": "sell", "posSide": "net", "algoId": "1", "slTriggerPx": "2630"},
        {"instId": "XAU-USDT-SWAP", "side": "buy", "posSide": "net", "algoId": "2", "slTriggerPx": "2700"},  # opens, not closes
        {"instId": "BTC-USDT-SWAP", "side": "sell", "posSide": "net", "algoId": "3", "slTriggerPx": "1"},
        {"instId": "XAU-USDT-SWAP", "side": "sell", "posSide": "net", "algoId": "4"},  # no trigger price
    ]
    [result] = attach_stops([long_position], orders)
    assert [stop.algo_id for stop in result.stops] == ["1"]


def test_attach_stops_in_hedge_mode_matches_pos_side_and_skips_known_ids():
    short_position = make_position(pos_side="short", direction="short")  # already carries stop "a1"
    orders = [
        {"instId": "XAU-USDT-SWAP", "side": "buy", "posSide": "short", "algoId": "a1", "slTriggerPx": "2700"},
        {"instId": "XAU-USDT-SWAP", "side": "buy", "posSide": "short", "algoId": "b2", "tpTriggerPx": "2500"},
        {"instId": "XAU-USDT-SWAP", "side": "sell", "posSide": "long", "algoId": "c3", "slTriggerPx": "2600"},
    ]
    [result] = attach_stops([short_position], orders)
    assert [stop.algo_id for stop in result.stops] == ["a1", "b2"]


def test_the_client_has_no_way_to_trade_or_move_funds():
    look_ups = {"order", "pending_orders"}  # these two only read: see the test below
    forbidden = ("order", "cancel", "amend", "close", "place", "withdraw", "transfer", "post")
    public_names = [name for name in dir(OkxClient) if not name.startswith("_") and name not in look_ups]
    assert not [name for name in public_names if any(word in name.lower() for word in forbidden)]
    assert not hasattr(OkxClient, "_post")


def test_orders_key_permissions_and_contract_rules_are_read_with_get():
    # FakeSession has no post(): anything but a GET would fail here.
    order_row = {
        "ordId": "77", "clOrdId": "ftb1", "instId": "BTC-USDT-SWAP", "side": "buy", "posSide": "net", "ordType": "limit",
        "px": "64000", "sz": "2", "accFillSz": "0.5", "avgPx": "", "state": "partially_filled", "reduceOnly": "false", "cTime": "1700000000000",
    }  # fmt: skip
    okx, session = client(
        {
            "/api/v5/trade/orders-pending": [ok([order_row])],
            "/api/v5/trade/order": [ok([dict(order_row, state="filled", accFillSz="2", avgPx="63990.5")])],
            "/api/v5/account/config": [ok([{"posMode": "net_mode", "perm": "read_only,trade", "ip": "203.0.113.7", "acctLv": "2"}])],
            "/api/v5/account/leverage-info": [ok([{"lever": "10", "mgnMode": "cross"}])],
            "/api/v5/public/instruments": [
                ok([{"instId": "BTC-USDT-SWAP", "instType": "SWAP", "ctVal": "0.01", "ctMult": "1", "ctValCcy": "BTC",
                     "settleCcy": "USDT", "ctType": "linear", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1", "state": "live"}])
            ],
        }
    )  # fmt: skip
    [pending] = okx.pending_orders("BTC-USDT-SWAP")
    assert (pending.ord_id, pending.side, pending.px, pending.sz, pending.filled_sz, pending.avg_px) == ("77", "buy", 64000.0, 2.0, 0.5, None)
    assert session.requests[0][0] == "https://okx.test/api/v5/trade/orders-pending?instType=SWAP&instId=BTC-USDT-SWAP"
    filled = okx.order("BTC-USDT-SWAP", cl_ord_id="ftb1")
    assert filled.state == "filled" and filled.avg_px == 63990.5 and not filled.reduce_only
    assert session.requests[1][0].endswith("/api/v5/trade/order?instId=BTC-USDT-SWAP&clOrdId=ftb1")

    config = okx.account_config()
    assert config.permissions == ("read_only", "trade") and config.can_trade and not config.can_withdraw
    assert not config.hedge_mode and config.ip == "203.0.113.7"
    assert okx.leverage("BTC-USDT-SWAP", "cross") == 10.0

    spec = okx.instrument_spec("BTC-USDT-SWAP")
    assert str(spec.coins_per_contract) == "0.01" and str(spec.lot_sz) == "0.01" and str(spec.tick_sz) == "0.1"
    assert spec.ct_type == "linear" and spec.settle_ccy == "USDT"
    assert "OK-ACCESS-KEY" not in session.requests[-1][1]  # the contract rules are public


def test_missing_order_or_contract_rules_are_errors():
    okx, _ = client({"/api/v5/trade/order": [ok([])], "/api/v5/public/instruments": [ok([{"instId": "X-USDT-SWAP", "ctVal": ""}])]})
    with pytest.raises(OkxError):
        okx.order("BTC-USDT-SWAP", ord_id="1")
    with pytest.raises(OkxError, match="合约规格"):
        okx.instrument_spec("X-USDT-SWAP")


def test_instruments_and_open_interest_are_public_requests():
    okx, session = client(
        {
            "/api/v5/public/instruments": [ok([{"instId": "BTC-USDT-SWAP", "state": "live"}])],
            "/api/v5/public/open-interest": [ok([{"instId": "BTC-USDT-SWAP", "oi": "100", "oiCcy": "18000.5", "oiUsd": "1200000000"}])],
        }
    )
    assert okx.instruments("SWAP") == [{"instId": "BTC-USDT-SWAP", "state": "live"}]
    interest = okx.open_interest("BTC-USDT-SWAP")
    assert interest.coins == 18000.5 and interest.usd == 1.2e9
    assert okx.open_interest("BTC-USDT") is None  # spot: nothing to ask
    urls = [url for url, _ in session.requests]
    assert urls == [
        "https://okx.test/api/v5/public/instruments?instType=SWAP",
        "https://okx.test/api/v5/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP",
    ]
    assert all("OK-ACCESS-KEY" not in headers for _, headers in session.requests)


def test_open_interest_without_data_or_fields():
    okx, _ = client({"/api/v5/public/open-interest": [ok([]), ok([{"instId": "XAU-USDT-SWAP", "oi": "5"}])]})
    assert okx.open_interest("XAU-USDT-SWAP") is None
    interest = okx.open_interest("XAU-USDT-SWAP")
    assert interest.coins is None and interest.usd is None


def test_account_state_says_when_the_stops_are_not_known():
    routes = {
        "/api/v5/account/balance": [ok([{"totalEq": "1000", "details": []}])],
        "/api/v5/account/positions": [ok([{"instId": "XAU-USDT-SWAP", "instType": "SWAP", "pos": "2", "posSide": "net", "mgnMode": "cross", "avgPx": "2650"}])],
        "/api/v5/public/instruments": [XAU_RULES],
        "/api/v5/trade/orders-algo-pending": [ok([])],
    }  # fmt: skip
    okx, _ = client(routes)
    assert okx.account_state().stops_known is True

    routes["/api/v5/trade/orders-algo-pending"] = [requests.ConnectionError("down")]
    okx, _ = client(routes)
    state = okx.account_state()
    assert state.stops_known is False and len(state.positions) == 1 and state.positions[0].stop_loss is None
