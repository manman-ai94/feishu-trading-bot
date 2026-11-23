"""A stand-in for MetaQuotes' MetaTrader5 package: one hedging account on a broker whose server clock
runs OFFSET hours ahead of UTC, with gold (XAUUSD, 100 oz a lot), and order_send that fills at the
bid and ask, keeps tickets, history orders and deals the way the terminal reports them."""
from __future__ import annotations

from types import SimpleNamespace as NS

OFFSET = 2 * 3600  # the trade server's clock is UTC+2


class FakeMt5:
    def __init__(self, clock, bid: float = 3800.0, ask: float = 3800.3, logged_in: bool = True, real: bool = True):
        self.clock = clock
        self.logged_in = logged_in
        self.real = real
        self.algo_trading = True
        self.balance = 10_000.0
        self.leverage = 100
        self.margin_mode = 2  # hedging
        self.symbols = {"XAUUSD": dict(bid=bid, ask=ask, contract=100.0, step=0.01, min=0.01, digits=2, point=0.01)}
        self.positions: list[NS] = []
        self.orders: list[NS] = []
        self.history_orders: list[NS] = []
        self.deals: list[NS] = []
        self.rates: list[dict] = []
        self.requests: list[dict] = []
        self.fail: list[int] = []  # retcodes the next order_send calls return
        self.silent = False  # order_send returns None (the terminal did not answer)
        self._ticket = 1000
        self.tick_moves = True  # each tick read is a new one: the market is open
        self._tick_msc = 0
        self.initialized = 0

    # ------------------------------------------------------------------ helpers

    def server_ms(self) -> int:
        return int((self.clock() + OFFSET) * 1000)

    def _next(self) -> int:
        self._ticket += 1
        return self._ticket

    def set_price(self, bid: float, ask: float | None = None) -> None:
        self.symbols["XAUUSD"].update(bid=bid, ask=ask if ask is not None else bid + 0.3)
        for row in self.positions:
            row.price_current = bid if row.type == 0 else self.symbols["XAUUSD"]["ask"]
            row.profit = self._profit(row.type, row.price_open, row.price_current, row.volume)

    def _profit(self, kind: int, open_px: float, close_px: float, volume: float) -> float:
        return round((close_px - open_px) * volume * 100 * (1 if kind == 0 else -1), 2)

    def open_ticket(self, kind: int, volume: float, price: float, sl: float = 0.0, tp: float = 0.0, swap: float = 0.0, ago: float = 0) -> NS:
        ms = self.server_ms() - int(ago * 1000)
        bid = self.symbols["XAUUSD"]["bid"]
        current = bid if kind == 0 else self.symbols["XAUUSD"]["ask"]
        row = NS(
            ticket=self._next(), time=ms // 1000, time_msc=ms, time_update=ms // 1000, time_update_msc=ms, type=kind,
            magic=0, identifier=0, reason=0, volume=volume, price_open=price, sl=sl, tp=tp, price_current=current,
            swap=swap, profit=self._profit(kind, price, current, volume), symbol="XAUUSD", comment="", external_id="",
        )  # fmt: skip
        row.identifier = row.ticket
        self.positions.append(row)
        self._deal(0 if kind == 0 else 1, 0, volume, price, 0.0, row.ticket, 0, ms, order=self._next())
        return row

    def _deal(self, kind, entry, volume, price, profit, position_id, reason, ms, order, comment="", commission=0.0, swap=0.0):
        self.deals.append(NS(
            ticket=self._next(), order=order, time=ms // 1000, time_msc=ms, type=kind, entry=entry, magic=0,
            position_id=position_id, reason=reason, volume=volume, price=price, commission=commission, swap=swap,
            profit=profit, fee=0.0, symbol="XAUUSD", comment=comment, external_id="",
        ))  # fmt: skip

    def close_ticket(self, row: NS, price: float, reason: int = 0, volume: float | None = None, swap: float = 0.0, at_ms: int | None = None) -> None:
        """The broker closes (part of) a ticket: a stop-loss (reason 4), a take-profit (5), a stop out (6)."""
        volume = row.volume if volume is None else volume
        ms = at_ms if at_ms is not None else self.server_ms()
        profit = self._profit(row.type, row.price_open, price, volume)
        self.balance += profit + swap
        self._deal(1 if row.type == 0 else 0, 1, volume, price, profit, row.ticket, reason, ms, order=self._next(), swap=swap)
        row.volume = round(row.volume - volume, 8)
        if row.volume <= 0:
            self.positions.remove(row)

    # ------------------------------------------------------------------ the package

    def initialize(self, **kwargs) -> bool:
        self.initialized += 1
        return True

    def last_error(self):
        return (1, "Success")

    def shutdown(self) -> None:
        pass

    def account_info(self):
        if not self.logged_in:
            return None
        profit = sum(row.profit for row in self.positions)
        swap = sum(row.swap for row in self.positions)
        equity = self.balance + profit + swap
        margin = sum(row.volume * 100 * row.price_open / self.leverage for row in self.positions)
        return NS(
            login=1, trade_mode=2 if self.real else 0, leverage=self.leverage, margin_so_mode=0, trade_allowed=True,
            trade_expert=True, margin_mode=self.margin_mode, balance=self.balance, credit=0.0, profit=profit + swap,
            equity=equity, margin=margin, margin_free=equity - margin,
            margin_level=equity / margin * 100 if margin else 0.0, margin_so_call=100.0, margin_so_so=50.0,
            currency="USD", server="Broker-Live", company="Broker",
        )  # fmt: skip

    def terminal_info(self):
        return NS(connected=True, trade_allowed=self.algo_trading)

    def _info(self, name: str):
        spec = self.symbols[name]
        return NS(
            name=name, visible=True, digits=spec["digits"], point=spec["point"], trade_tick_size=spec["point"],
            trade_contract_size=spec["contract"], volume_min=spec["min"], volume_max=100.0, volume_step=spec["step"],
            trade_mode=4, currency_base="XAU", currency_profit="USD", filling_mode=1, bid=spec["bid"], ask=spec["ask"],
        )  # fmt: skip

    def symbol_info(self, name: str):
        return self._info(name) if name in self.symbols else None

    def symbols_get(self, group: str = "*"):
        prefix = group.rstrip("*")
        return tuple(self._info(name) for name in self.symbols if name.startswith(prefix))

    def symbol_select(self, name: str, enable: bool = True) -> bool:
        return name in self.symbols

    def symbol_info_tick(self, name: str):
        if name not in self.symbols:
            return None
        if self.tick_moves or not self._tick_msc:
            self._tick_msc = self.server_ms()
        spec = self.symbols[name]
        return NS(time=self._tick_msc // 1000, bid=spec["bid"], ask=spec["ask"], last=0.0, volume=0, time_msc=self._tick_msc, flags=0)

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        return self.rates[-count:] if self.rates else []

    def positions_get(self, symbol: str | None = None, **kwargs):
        return tuple(row for row in self.positions if symbol is None or row.symbol == symbol)

    def orders_get(self, **kwargs):
        return tuple(self.orders)

    def history_orders_get(self, *args, ticket: int | None = None, **kwargs):
        if ticket is not None:
            return tuple(row for row in self.history_orders if row.ticket == ticket)
        start, end = args
        return tuple(row for row in self.history_orders if start <= row.time_setup <= end)

    def history_deals_get(self, start, end, **kwargs):
        return tuple(row for row in self.deals if start <= row.time <= end)

    def order_send(self, request: dict):
        self.requests.append(dict(request))
        if self.silent:
            return None
        if self.fail:
            return NS(retcode=self.fail.pop(0), order=0, deal=0, comment="refused")
        action = request["action"]
        ms = self.server_ms()
        spec = self.symbols.get(request.get("symbol", "XAUUSD"), self.symbols["XAUUSD"])
        if action == 1:  # a deal at market
            buy = request["type"] == 0
            price = spec["ask"] if buy else spec["bid"]
            order = self._next()
            comment = request.get("comment", "")
            if "position" in request:
                row = next(row for row in self.positions if row.ticket == request["position"])
                profit = self._profit(row.type, row.price_open, price, request["volume"])
                self.balance += profit
                self._deal(0 if buy else 1, 1, request["volume"], price, profit, row.ticket, 3, ms, order, comment)
                row.volume = round(row.volume - request["volume"], 8)
                if row.volume <= 0:
                    self.positions.remove(row)
            else:
                row = self.open_ticket(0 if buy else 1, request["volume"], price, request.get("sl", 0.0), request.get("tp", 0.0))
                self.deals.pop()
                self._deal(0 if buy else 1, 0, request["volume"], price, 0.0, row.ticket, 3, ms, order, comment)
                row.comment = comment
            self.history_orders.append(NS(
                ticket=order, time_setup=ms // 1000, time_setup_msc=ms, type=request["type"], state=4, symbol=request["symbol"],
                volume_initial=request["volume"], volume_current=0.0, price_open=0.0, sl=request.get("sl", 0.0),
                tp=request.get("tp", 0.0), comment=comment, position_id=request.get("position", 0),
            ))  # fmt: skip
            return NS(retcode=10009, order=order, deal=0, volume=request["volume"], price=price, comment="done")
        if action == 5:  # a pending order
            order = self._next()
            row = NS(
                ticket=order, time_setup=ms // 1000, time_setup_msc=ms, type=request["type"], state=1, symbol=request["symbol"],
                volume_initial=request["volume"], volume_current=request["volume"], price_open=request["price"],
                sl=request.get("sl", 0.0), tp=request.get("tp", 0.0), comment=request.get("comment", ""),
            )  # fmt: skip
            self.orders.append(row)
            return NS(retcode=10008, order=order, deal=0, comment="placed")
        if action == 6:  # stop-loss / take-profit of a position
            row = next(row for row in self.positions if row.ticket == request["position"])
            if row.sl == request["sl"] and row.tp == request["tp"]:
                return NS(retcode=10025, order=0, deal=0, comment="no changes")
            row.sl, row.tp = request["sl"], request["tp"]
            return NS(retcode=10009, order=0, deal=0, comment="done")
        if action == 7:  # change a pending order
            row = next(row for row in self.orders if row.ticket == request["order"])
            row.sl, row.tp, row.price_open = request["sl"], request["tp"], request["price"]
            return NS(retcode=10009, order=row.ticket, deal=0, comment="done")
        if action == 8:  # cancel a pending order
            row = next(row for row in self.orders if row.ticket == request["order"])
            self.orders.remove(row)
            row.state = 2
            self.history_orders.append(row)
            return NS(retcode=10009, order=row.ticket, deal=0, comment="done")
        raise AssertionError(f"unexpected action {action}")


class FakeMarket:
    """OKX's public data, as far as the MT5 client passes things on to it."""

    def __init__(self):
        self.asked: list[tuple] = []

    def instruments(self, inst_type: str) -> list[dict]:
        if inst_type == "SWAP":
            return [{"instId": "XAU-USDT-SWAP", "state": "live"}, {"instId": "BTC-USDT-SWAP", "state": "live"}]
        return [{"instId": "BTC-USDT", "state": "live"}]

    def ticker(self, inst_id: str):
        self.asked.append(("ticker", inst_id))
        from bot.models import Ticker

        return Ticker(inst_id, 95000.0)

    def candles(self, inst_id, bar="1H", limit=200):
        self.asked.append(("candles", inst_id))
        return []

    def top_trader_ratio(self, inst_id, by="account", period="1H"):
        self.asked.append(("ratio", inst_id))
        return [(1, 1.5)]

    def funding_rate(self, inst_id):
        return 0.0001

    def sync_clock(self) -> float:
        return 0.0
