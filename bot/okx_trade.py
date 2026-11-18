"""The part of the OKX client that can change things: place, cancel, close.

Kept apart from okx_client.py on purpose. The bot creates one of these only
when the owner has switched trading on (TRADING=demo or TRADING=live), and
only the trade desk (trading.py) ever holds it. Everything else works with
the read-only client, which has none of these methods.

Nothing here moves funds: there is no withdrawal and no transfer.
"""
from __future__ import annotations

import json
import logging

import requests

from .okx_client import TIMESTAMP_ERROR_CODES, OkxClient, OkxError, OkxNetworkError

log = logging.getLogger(__name__)

ORDER_TAG = "ftbot"  # marks the bot's orders in OKX's order history
MARKET_PRICE = "-1"  # as the order price of a take-profit or stop-loss: close at market when triggered


class OkxTradeClient(OkxClient):
    trigger_px_type = "last"  # what price fires the stops it places: last, mark or index (TRADE_STOP_TRIGGER)

    # ------------------------------------------------------------------ core

    def _post(self, path: str, body: dict | list, _retried: bool = False) -> list:
        """Sends a signed POST. Returns `data` on success, raises OkxError when OKX says no.

        OkxNetworkError means the outcome is unknown: the request may or may not
        have reached OKX. Callers must find out before they try again.
        """
        text = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        try:
            response = self._session.post(
                self._base_url + path,
                data=text.encode("utf-8"),
                headers=self._headers("POST", path, True, text),
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            raise OkxNetworkError(f"连不上 OKX：{type(exc).__name__}") from exc

        try:
            payload = response.json()
        except ValueError:
            raise OkxError("bad_response", f"OKX 返回了无法解析的内容（HTTP {response.status_code}）", response.status_code)

        code = str(payload.get("code", ""))
        data = payload.get("data") or []
        if code in TIMESTAMP_ERROR_CODES and not _retried:
            # The local clock is off, so OKX refused the request outright: nothing was done.
            self.sync_clock()
            return self._post(path, body, _retried=True)
        # OKX reports the reason for a refused order per item, in sCode and sMsg.
        for item in data:
            item_code = str(item.get("sCode", "0") or "0") if isinstance(item, dict) else "0"
            if item_code != "0":
                raise OkxError(item_code, str(item.get("sMsg", "")) or str(payload.get("msg", "")), response.status_code)
        if code != "0":
            raise OkxError(code, str(payload.get("msg", "")), response.status_code)
        return data

    # ---------------------------------------------------------------- orders

    def place_order(
        self,
        *,
        inst_id: str,
        td_mode: str,
        side: str,
        ord_type: str,
        sz: str,
        cl_ord_id: str,
        px: str | None = None,
        pos_side: str | None = None,
        reduce_only: bool = False,
        sl_trigger_px: str | None = None,
        tp_trigger_px: str | None = None,
    ) -> str:
        """Places one order and returns OKX's order id.

        `side` is "buy" or "sell"; `ord_type` is "market" or "limit". `pos_side`
        ("long" / "short") is only sent for accounts in hedge mode. A stop-loss or
        take-profit given here is attached to the order and closes at market.
        """
        body: dict = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": sz,
            "clOrdId": cl_ord_id,
            "tag": ORDER_TAG,
        }
        if px is not None:
            body["px"] = px
        if pos_side:
            body["posSide"] = pos_side
        if reduce_only:
            body["reduceOnly"] = True
        attached = _stop_fields(sl_trigger_px, tp_trigger_px, prefix="", px_type=self.trigger_px_type)
        if attached:
            body["attachAlgoOrds"] = [attached]
        data = self._post("/api/v5/trade/order", body)
        return str(data[0].get("ordId", "")) if data else ""

    def cancel_order(self, inst_id: str, ord_id: str) -> None:
        self._post("/api/v5/trade/cancel-order", {"instId": inst_id, "ordId": ord_id})

    def close_position(self, inst_id: str, mgn_mode: str, pos_side: str | None = None, cl_ord_id: str = "") -> None:
        """Closes a whole position at market.

        Orders that would stand in the way (its own take-profit and stop-loss,
        pending closing orders) are cancelled by OKX first (autoCxl).
        """
        body: dict = {"instId": inst_id, "mgnMode": mgn_mode, "autoCxl": True, "tag": ORDER_TAG}
        if pos_side:
            body["posSide"] = pos_side
        if cl_ord_id:
            body["clOrdId"] = cl_ord_id
        self._post("/api/v5/trade/close-position", body)

    # ------------------------------------------------ take-profit / stop-loss

    def place_stop(
        self,
        *,
        inst_id: str,
        td_mode: str,
        side: str,
        cl_ord_id: str,
        pos_side: str | None = None,
        sl_trigger_px: str | None = None,
        tp_trigger_px: str | None = None,
    ) -> str:
        """A take-profit and/or stop-loss for the whole position. Returns the algo order id.

        `side` is the side that closes the position: "sell" for a long, "buy"
        for a short. It closes at market when the last price reaches the trigger.
        """
        if not sl_trigger_px and not tp_trigger_px:
            raise ValueError("a stop needs a stop-loss or a take-profit price")
        body: dict = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": "oco" if sl_trigger_px and tp_trigger_px else "conditional",
            "closeFraction": "1",  # the whole position, whatever its size is by then
            "algoClOrdId": cl_ord_id,
            "tag": ORDER_TAG,
        }
        if pos_side:
            body["posSide"] = pos_side
        else:
            body["reduceOnly"] = True  # OKX requires it with closeFraction in net mode
        body.update(_stop_fields(sl_trigger_px, tp_trigger_px, prefix="", px_type=self.trigger_px_type))
        data = self._post("/api/v5/trade/order-algo", body)
        return str(data[0].get("algoId", "")) if data else ""

    def place_trigger(
        self,
        *,
        inst_id: str,
        td_mode: str,
        side: str,
        sz: str,
        trigger_px: str,
        cl_ord_id: str,
        pos_side: str | None = None,
        sl_trigger_px: str | None = None,
        tp_trigger_px: str | None = None,
    ) -> str:
        """A trigger order (计划委托): when the price (TRADE_STOP_TRIGGER kind) reaches `trigger_px`, OKX places a market
        order of `sz` contracts. A stop-loss / take-profit given here comes with the order once it fills.
        Returns the algo order id. Cancelled like a stop, through cancel-algos."""
        body: dict = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": "trigger",
            "sz": sz,
            "triggerPx": trigger_px,
            "orderPx": MARKET_PRICE,
            "triggerPxType": self.trigger_px_type,
            "algoClOrdId": cl_ord_id,
            "tag": ORDER_TAG,
        }
        if pos_side:
            body["posSide"] = pos_side
        attached = _stop_fields(sl_trigger_px, tp_trigger_px, prefix="", px_type=self.trigger_px_type)
        if attached:
            body["attachAlgoOrds"] = [attached]
        data = self._post("/api/v5/trade/order-algo", body)
        return str(data[0].get("algoId", "")) if data else ""

    def amend_stop(self, inst_id: str, algo_id: str, sl_trigger_px: str | None = None, tp_trigger_px: str | None = None) -> None:
        """Moves the trigger prices of an existing take-profit / stop-loss order."""
        body = {"instId": inst_id, "algoId": algo_id}
        body.update(_stop_fields(sl_trigger_px, tp_trigger_px, prefix="new", px_type=self.trigger_px_type))
        self._post("/api/v5/trade/amend-algos", body)

    def cancel_stops(self, items: list[tuple[str, str]]) -> None:
        """Cancels take-profit / stop-loss orders, given as (instrument, algo order id)."""
        if items:
            self._post("/api/v5/trade/cancel-algos", [{"instId": inst_id, "algoId": algo_id} for inst_id, algo_id in items])


def _stop_fields(sl_trigger_px: str | None, tp_trigger_px: str | None, prefix: str, px_type: str = "last") -> dict:
    """The request fields of a stop-loss and/or take-profit that closes at market when `px_type`
    (last, mark or index price) reaches it.

    With prefix "new" these are the fields of an amendment (newSlTriggerPx, ...).
    """

    def name(field: str) -> str:
        return prefix + field[0].upper() + field[1:] if prefix else field

    fields: dict = {}
    if sl_trigger_px:
        fields[name("slTriggerPx")] = sl_trigger_px
        fields[name("slOrdPx")] = MARKET_PRICE
        fields[name("slTriggerPxType")] = px_type
    if tp_trigger_px:
        fields[name("tpTriggerPx")] = tp_trigger_px
        fields[name("tpOrdPx")] = MARKET_PRICE
        fields[name("tpTriggerPxType")] = px_type
    return fields
