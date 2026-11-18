"""Fakes shared by the tests. Nothing here touches the network."""
from __future__ import annotations

from dataclasses import dataclass

from bot.analyst import AnalystUnavailable
from bot.config import Config
from bot.feishu_client import IncomingMessage
from bot.models import AccountConfig, AccountSnapshot, AccountState, Candle, OpenInterest, Position, StopOrder, Ticker

OWNER = "ou_owner"
FRIEND = "ou_friend"
BOT = "ou_bot"
GROUP = "oc_group"


# The stricter set-up the owner can switch back to: the group sees price
# levels only, and group members may ask the bot about the market.
OPEN_GROUP = dict(group_show_pnl=False, group_owner_only=False)


def make_cfg(**overrides) -> Config:
    values = dict(
        feishu_app_id="cli_test",
        feishu_app_secret="secret",
        owner_open_id=OWNER,
        group_chat_id=GROUP,
        okx_api_key="key",
        okx_secret_key="secret",
        okx_passphrase="pass",
        anthropic_api_key="sk-test",
    )
    values.update(overrides)
    return Config(**values)


def make_position(**overrides) -> Position:
    values = dict(
        inst_id="XAU-USDT-SWAP",
        inst_type="SWAP",
        pos_side="net",
        mgn_mode="cross",
        qty=2.0,
        direction="long",
        avg_px=2650.0,
        mark_px=2660.0,
        upl=20.0,
        upl_ratio=0.0377,
        lever=10.0,
        liq_px=2400.0,
        notional_usd=5320.0,
        stops=(StopOrder("a1", sl_trigger_px=2630.0, tp_trigger_px=2700.0),),
    )
    values.update(overrides)
    return Position(**values)


def make_state(positions=None, **snapshot) -> AccountState:
    values = dict(total_eq=10000.0, upl=20.0, mgn_ratio=12.5)
    values.update(snapshot)
    return AccountState(AccountSnapshot(**values), [make_position()] if positions is None else list(positions))


def make_message(text: str, sender: str = OWNER, private: bool = True, mentions=(BOT,), message_id: str = "", **overrides) -> IncomingMessage:
    values = dict(
        message_id=message_id or f"om_{abs(hash((text, sender, private)))}",
        chat_id="oc_private" if private else GROUP,
        chat_type="p2p" if private else "group",
        sender_open_id=sender,
        text=text,
        mentioned_open_ids=() if private else tuple(mentions),
    )
    values.update(overrides)
    return IncomingMessage(**values)


@dataclass
class Sent:
    how: str  # send_text | reply_text | send_markdown | reply_markdown
    target: str
    body: str
    title: str = ""
    footer: str | None = None
    receive_id_type: str = ""
    template: str = ""

    @property
    def everything(self) -> str:
        return f"{self.title}\n{self.body}\n{self.footer or ''}"


class FakeFeishu:
    def __init__(self):
        self.sent: list[Sent] = []
        self.reactions: list[tuple[str, str, str]] = []  # (added | removed, message id, emoji or reaction id)
        self.reaction_error: Exception | None = None

    def add_reaction(self, message_id, emoji):
        if self.reaction_error:
            raise self.reaction_error
        self.reactions.append(("added", message_id, emoji))
        return f"r{len(self.reactions)}"

    def remove_reaction(self, message_id, reaction_id):
        self.reactions.append(("removed", message_id, reaction_id))

    def send_text(self, receive_id, text, receive_id_type="chat_id"):
        self.sent.append(Sent("send_text", receive_id, text, receive_id_type=receive_id_type))

    def reply_text(self, message_id, text):
        self.sent.append(Sent("reply_text", message_id, text))

    def send_markdown(self, receive_id, title, markdown, receive_id_type="chat_id", template="blue", footer=None):
        self.sent.append(Sent("send_markdown", receive_id, markdown, title, footer, receive_id_type, template))

    def reply_markdown(self, message_id, title, markdown, template="blue", footer=None):
        self.sent.append(Sent("reply_markdown", message_id, markdown, title, footer, template=template))

    def to(self, target: str) -> list[Sent]:
        return [item for item in self.sent if item.target == target]


class FakeOkx:
    """Stands in for OkxClient: an account state plus simple market data."""

    def __init__(self, state: AccountState | None = None):
        self.state = state
        self.error: Exception | None = None
        self.market_error: Exception | None = None
        self.account_reads = 0
        self.triggered: dict[str, list[dict]] = {}  # OKX's triggered TP/SL orders, by order type
        self.closed: list[dict] = []  # OKX's position history
        self.open_orders: list[dict] = []  # OKX's orders-pending rows, whoever placed them
        self.algos: dict[str, list[dict]] = {}  # OKX's orders-algo-pending rows, by the ordType asked for

    def algo_history(self, ord_type: str) -> list[dict]:
        if self.error:
            raise self.error
        return list(self.triggered.get(ord_type, []))

    def position_history(self) -> list[dict]:
        if self.error:
            raise self.error
        return list(self.closed)

    def account_state(self) -> AccountState:
        self.account_reads += 1
        if self.error:
            raise self.error
        return self.state

    def account_config(self) -> AccountConfig:
        if self.error:
            raise self.error
        return AccountConfig("net_mode", ("read_only", "trade"), "203.0.113.7", "2")

    def pending_orders(self, inst_id: str | None = None) -> list:
        return []

    def pending_orders_raw(self) -> list[dict]:
        if self.error:
            raise self.error
        return list(self.open_orders)

    def pending_algos(self, ord_type: str) -> list[dict]:
        if self.error:
            raise self.error
        return list(self.algos.get(ord_type, []))

    def ticker(self, inst_id: str) -> Ticker:
        if self.market_error:
            raise self.market_error
        return Ticker(inst_id, 2650.0, open_24h=2600.0, high_24h=2660.0, low_24h=2590.0)

    def funding_rate(self, inst_id: str) -> float:
        if self.market_error:
            raise self.market_error
        return 0.0001

    def candles(self, inst_id: str, bar: str = "1H", limit: int = 200) -> list[Candle]:
        if self.market_error:
            raise self.market_error
        return [Candle(i * 60_000, 2600.0 + i, 2601.0 + i, 2599.0 + i, 2600.5 + i) for i in range(min(limit, 60))]

    def open_interest(self, inst_id: str) -> OpenInterest | None:
        if self.market_error:
            raise self.market_error
        return OpenInterest(coins=18000.0, usd=1.2e9) if inst_id.endswith("-SWAP") else None

    # What OKX lists: perpetuals and spot pairs, one of them suspended.
    LISTED = {
        "SWAP": ["XAU-USDT-SWAP", "BTC-USDT-SWAP", "BTC-USD-SWAP", "ETH-USDT-SWAP", "DOGE-USDT-SWAP"],
        "SPOT": ["BTC-USDT", "ETH-USDT", "OKB-USDT", "LUNA-USDT"],
    }

    def instruments(self, inst_type: str) -> list[dict]:
        self.instrument_reads = getattr(self, "instrument_reads", 0) + 1
        if self.market_error:
            raise self.market_error
        return [
            {"instId": inst_id, "instType": inst_type, "state": "suspend" if inst_id == "LUNA-USDT" else "live"}
            for inst_id in self.LISTED.get(inst_type, [])
        ]


class FakeAnalyst:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.intent = "analysis"
        self.error: AnalystUnavailable | None = None
        self.reply: str | None = None  # what answer() gives back; "AI:answer" when not set
        self.command = ""  # what to_command() gives back: the owner's words as a fixed-pattern order
        self.calls: list[tuple[str, dict]] = []

    def _record(self, name: str, **details) -> str:
        self.calls.append((name, details))
        if self.error:
            raise self.error
        return f"AI:{name}"

    def named(self, name: str) -> list[dict]:
        return [details for call, details in self.calls if call == name]

    def attach_account(self, account, history):
        self.account_reader, self.history_reader = account, history

    def route_intent(self, text):
        self.calls.append(("route", {"text": text}))
        return self.intent

    def answer(self, question, market_text, account_text, in_group, deep=False, history=None):
        text = self._record(
            "answer", question=question, market_text=market_text, account_text=account_text, in_group=in_group,
            deep=deep, history=list(history or []),
        )  # fmt: skip
        return text if self.reply is None else self.reply

    def to_command(self, text, positions_text, earlier=None):
        self.calls.append(("to_command", {"text": text, "positions_text": positions_text, "earlier": list(earlier or [])}))
        return "" if self.error else self.command

    def small_talk(self, text):
        self.calls.append(("small_talk", {"text": text}))
        return "" if self.error else "AI:hello"

    def brief(self, kind, market_text, account_text, journal_text, for_owner, shared_with_group=False):
        return self._record(
            "brief", kind=kind, market_text=market_text, account_text=account_text, journal_text=journal_text,
            for_owner=for_owner, shared_with_group=shared_with_group,
        )

    def weekly_review(self, market_text, journal_text, equity_text, shared_with_group=False, week_figures=""):
        return self._record(
            "weekly", market_text=market_text, journal_text=journal_text, equity_text=equity_text,
            shared_with_group=shared_with_group, week_figures=week_figures,
        )  # fmt: skip

    def review_trade(self, figures, lessons="", shared_with_group=False):
        text = self._record("review", figures=figures, lessons=lessons, shared_with_group=shared_with_group)
        return text if self.reply is None else self.reply

    def watch_comment(self, facts, close=False):
        text = self._record("watch", facts=facts, close=close)
        return text if self.reply is None else self.reply


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds
