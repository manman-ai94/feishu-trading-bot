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
    okx, _ = client(
        {
            "/api/v5/account/balance": [ok([{"totalEq": "10000", "mgnRatio": "12.5"}])],
            "/api/v5/account/positions": [ok([position_row])],
            "/api/v5/trade/orders-algo-pending": [
                ok([{"instId": "XAU-USDT-SWAP", "side": "sell", "posSide": "net", "algoId": "9", "slTriggerPx": "2630", "tpTriggerPx": ""}])
            ],
        }
    )
    state = okx.account_state()
    assert state.snapshot.total_eq == 10000.0
    assert state.positions[0].stop_loss == 2630.0
    assert state.positions[0].take_profit is None


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
    forbidden = ("order", "cancel", "amend", "close", "withdraw", "transfer", "post")
    public_names = [name for name in dir(OkxClient) if not name.startswith("_")]
    assert not [name for name in public_names if any(word in name.lower() for word in forbidden)]
    assert not hasattr(OkxClient, "_post")
