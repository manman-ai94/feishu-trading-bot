"""The tools the AI may call while it works on an answer.

Client tools (run here, on the server): market data and candles from OKX, a
search of the owner's knowledge base, how the crowd is positioned (crowd.py),
and US real rates and the dollar from FRED (macro.py). Server tools (run by Anthropic): web search and web fetch, for
news and economic data.

When the answer may show the trader's account (his private chat, or the group when he shares it),
two more: the account with every waiting order read fresh from OKX, and the positions he closed.

Every tool only reads. None of them touches the account or places an order.
"""
from __future__ import annotations

import logging
from typing import Callable
from zoneinfo import ZoneInfo

from .instruments import InstrumentResolver
from .knowledge import KnowledgeBase
from .market import build_market_context, candles_text
from .okx_client import OkxError, OkxNetworkError

log = logging.getLogger(__name__)

BARS = ("5m", "15m", "30m", "1H", "2H", "4H", "1D", "1W")
MAX_CANDLES = 150

# Newest first. The bot steps down this list when the API turns a version away.
WEB_TOOL_SETS: tuple[tuple[dict, ...], ...] = (
    ({"type": "web_search_20260209", "name": "web_search"}, {"type": "web_fetch_20260209", "name": "web_fetch"}),
    ({"type": "web_search_20250305", "name": "web_search"},),
    (),
)

# Limits on the web tools. Every search is billed, and its results stay in the
# conversation as input tokens, so searches are most of what a question costs.
WEB_SEARCHES = 3  # searches in one request for an everyday question or a brief
WEB_SEARCHES_DEEP = 6  # for a "深度" question and the weekly review
WEB_FETCHES = 2  # pages opened in one request
WEB_FETCH_TOKENS = 10000  # how much of an opened page the AI is given

CLIENT_TOOLS: tuple[dict, ...] = (
    {
        "name": "get_market",
        "description": (
            "Real-time market data for one instrument on OKX: last price, 24-hour range and change, funding rate, "
            "open interest, and for the 15m, 1H, 4H and 1D timeframes the trend, EMA20/50/200, RSI14, ATR14 and the "
            "recent high and low. Call it for any instrument the question is about whose data is not already in the "
            "message. `symbol` may be a ticker (BTC, ETH, SOL, XAU), a Chinese name (比特币, 黄金) or a full OKX "
            "instrument id (BTC-USDT-SWAP, ETH-USDT)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Ticker, name or OKX instrument id."}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_candles",
        "description": (
            "Raw candles (open, high, low, close) for one OKX instrument, oldest first. Use it when exact swing highs "
            "and lows are needed for precise support, resistance, target or invalidation levels, or to look further "
            "back than get_market does."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Ticker, name or OKX instrument id."},
                "bar": {"type": "string", "enum": list(BARS), "description": "Candle size."},
                "limit": {"type": "integer", "description": f"How many candles, newest ones, up to {MAX_CANDLES}. Default 60."},
            },
            "required": ["symbol", "bar"],
        },
    },
    {
        "name": "search_knowledge",
        "description": (
            "Search the trader's own knowledge base: his trading rules, notes, and material he collected about "
            "investors and methods he wants to learn from. Call it when the question is about his rules or methods, "
            "about an investor's approach, or when an answer should be checked against his own rules."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Keywords, in the language of the material."}},
            "required": ["query"],
        },
    },
    {
        "name": "get_positioning",
        "description": (
            "How other traders are positioned in one instrument: the long/short ratio of OKX's top traders, by number "
            "of accounts and by size of positions, now and 24 hours ago; and for gold the latest weekly CFTC report of "
            "managed-money positions in COMEX gold futures. Background for judging crowding and risk only: never the "
            "basis of a price level and never a signal on its own. Say when the data is from."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Ticker, name or OKX instrument id."}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_macro",
        "description": (
            "The US rates and dollar that drive gold, from FRED's daily data: the 10-year real yield (TIPS), the "
            "10-year Treasury yield, 10-year breakeven inflation and the broad trade-weighted dollar index, each with "
            "its date and its change over a week and a month. Background for gold (and risk appetite in general), "
            "never the basis of a price level. FRED runs a day or two behind, so say the date of what you quote."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
)
# Offered once the toolbox has the trader's checklist (discipline.py).
SETUP_TOOL = {
    "name": "check_setup",
    "description": (
        "The trader's own checklist for one trade idea, run by the program: today's plan and its direction, the "
        "4-hour and daily trend, how close the entry is to a key level, reward to risk (at least 2:1), major US data "
        "within half an hour, and whether the day still allows a new position. Call it before writing any concrete "
        "trade idea (an entry with a stop and a target). If any item fails, do not give the idea: answer 观望 and say "
        "which item fails."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker, name or OKX instrument id."},
            "side": {"type": "string", "enum": ["long", "short"]},
            "entry": {"type": "number", "description": "Entry price; the middle of an entry zone."},
            "stop": {"type": "number", "description": "Stop-loss (invalidation) price."},
            "target": {"type": "number", "description": "The first target price."},
        },
        "required": ["symbol", "side", "entry", "stop", "target"],
    },
}
# Offered only for an answer that may show the trader's account, and only once the toolbox has a way to read it.
ACCOUNT_TOOLS: tuple[dict, ...] = (
    {
        "name": "get_account",
        "description": (
            "The trader's own OKX account, read fresh: equity and margin, every position with its stop-loss, "
            "take-profit and risk figures, and every order waiting on the account (limit orders, take-profit / "
            "stop-loss orders, trigger orders, trailing stops), whether the bot placed it or he placed it by hand "
            "in the OKX app, each with its distance from the market and what its stop would cost. The message "
            "already carries a copy read moments ago; call this when fresher numbers matter, for example after "
            "he says he just changed something."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_trade_history",
        "description": (
            "Positions the trader closed recently, from OKX's own records, newest first: when, which instrument "
            "and side, open and close price, how it closed (stop-loss, take-profit, his own close, liquidation) and "
            "the realised P&L with fees and funding. Use it for questions about his recent trades, results or habits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "How many days back, 1 to 90. Default 7."}},
        },
    },
)
# Offered only when the toolbox was given what the tool reads from.
OPTIONAL_TOOLS = {"search_knowledge": "_knowledge", "get_positioning": "_crowd", "get_macro": "_macro"}


class ToolError(Exception):
    """The tool could not do what was asked. The message goes back to the AI."""


class Toolbox:
    def __init__(
        self, okx, resolver: InstrumentResolver, knowledge: KnowledgeBase | None, tz: ZoneInfo, crowd=None, macro=None
    ):
        self._okx = okx
        self._resolver = resolver
        self._knowledge = knowledge
        self._tz = tz
        self._crowd = crowd  # crowd.Crowd, or None when CROWD_DATA is off
        self._macro = macro  # macro.Macro, or None when MACRO_DATA is off
        self._account: Callable[[], str | None] | None = None
        self._history: Callable[[int], str] | None = None
        self._setup: Callable[[str, str, float, float, float, bool], str] | None = None

    def attach_account(self, account: Callable[[], str | None], history: Callable[[int], str]) -> None:
        """How the account tools read: `account()` the account and its orders, fresh; `history(days)` the closes.
        Given once the reporter that owns the account reads exists."""
        self._account, self._history = account, history

    def attach_discipline(self, check: Callable[[str, str, float, float, float, bool], str]) -> None:
        """`check(inst_id, side, entry, stop, target, amounts)`: the checklist's verdict, in words."""
        self._setup = check

    def definitions(self, web_level: int = 0, web_searches: int = WEB_SEARCHES, account: bool = False) -> list[dict]:
        """Tool definitions in a fixed order, so the cached prompt prefix stays the same.
        `account`: this answer may show the trader's account, so the account tools are offered."""
        tools = [
            tool
            for tool in CLIENT_TOOLS
            if tool["name"] not in OPTIONAL_TOOLS or getattr(self, OPTIONAL_TOOLS[tool["name"]]) is not None
        ]
        if self._setup is not None:
            tools.append(SETUP_TOOL)
        if account and self._account is not None:
            tools.extend(ACCOUNT_TOOLS)
        for tool in WEB_TOOL_SETS[min(web_level, len(WEB_TOOL_SETS) - 1)]:
            if tool["name"] == "web_search":
                tools.append({**tool, "max_uses": web_searches})
            else:
                tools.append({**tool, "max_uses": WEB_FETCHES, "max_content_tokens": WEB_FETCH_TOKENS})
        return tools

    def run(self, name: str, tool_input, account: bool = False) -> tuple[str, bool]:
        """Runs one client tool. Returns (text for the AI, whether it is an error).
        `account` as for `definitions`: the account tools run only when they were offered."""
        try:
            if not isinstance(tool_input, dict):
                raise ToolError("工具参数格式不对。")
            if name in ("get_account", "get_trade_history"):
                if not account or self._account is None or self._history is None:
                    raise ToolError("这次不能看交易员的账户。")
                if name == "get_account":
                    return self._account() or "这次没读到账户。", False
                return self._history(self._days(tool_input)), False
            if name == "get_market":
                return self._market(tool_input), False
            if name == "get_candles":
                return self._candles(tool_input), False
            if name == "search_knowledge":
                return self._search(tool_input), False
            if name == "get_positioning":
                return self._positioning(tool_input), False
            if name == "get_macro":
                return self._macro_text(), False
            if name == "check_setup":
                return self._check_setup(tool_input, account), False
            raise ToolError(f"没有叫 {name} 的工具。")
        except ToolError as exc:
            return str(exc), True
        except (OkxError, OkxNetworkError) as exc:
            return f"OKX 行情读取失败：{exc}", True
        except Exception:
            log.exception("tool %s failed", name)
            return "这个工具出了点问题，这次没有取到数据。", True

    # ------------------------------------------------------------------ tools

    @staticmethod
    def _days(tool_input: dict) -> int:
        days = tool_input.get("days", 7)
        if isinstance(days, bool) or not isinstance(days, int):
            raise ToolError("days 要是整数。")
        return max(1, min(days, 90))

    def _instrument(self, tool_input: dict) -> str:
        symbol = tool_input.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ToolError("缺少 symbol。")
        inst_id = self._resolver.resolve(symbol)
        if inst_id is None:
            raise ToolError(f"OKX 上没有找到 {symbol.strip()} 这个品种。它可能不在 OKX 交易，可以改用联网搜索查它的价格。")
        return inst_id

    def _market(self, tool_input: dict) -> str:
        inst_id = self._instrument(tool_input)
        context = build_market_context(self._okx, inst_id)
        if not context.usable:
            raise ToolError(f"暂时取不到 {inst_id} 的行情：{'；'.join(context.errors) or '未知原因'}")
        return context.to_text()

    def _candles(self, tool_input: dict) -> str:
        inst_id = self._instrument(tool_input)
        bar = tool_input.get("bar")
        if bar not in BARS:
            raise ToolError(f"bar 只能是 {'、'.join(BARS)} 之一。")
        limit = tool_input.get("limit", 60)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ToolError("limit 要是整数。")
        candles = self._okx.candles(inst_id, bar, max(1, min(limit, MAX_CANDLES)))
        return candles_text(inst_id, bar, candles, self._tz)

    def _search(self, tool_input: dict) -> str:
        query = tool_input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("缺少 query。")
        if self._knowledge is None:
            raise ToolError("资料库没有启用。")
        return self._knowledge.search_text(query.strip())

    def _positioning(self, tool_input: dict) -> str:
        inst_id = self._instrument(tool_input)
        if self._crowd is None:
            raise ToolError("群体持仓数据没有打开。")
        text = self._crowd.text(inst_id)
        if not text:
            raise ToolError(f"{inst_id} 没有群体持仓数据。OKX 只给合约提供大户多空比，CFTC 周报目前只接了黄金。")
        return text

    def _check_setup(self, tool_input: dict, account: bool) -> str:
        if self._setup is None:
            raise ToolError("这次没有清单可用。")
        inst_id = self._instrument(tool_input)
        side = tool_input.get("side")
        if side not in ("long", "short"):
            raise ToolError("side 只能是 long 或 short。")
        prices = []
        for name in ("entry", "stop", "target"):
            value = tool_input.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ToolError(f"{name} 要是正数。")
            prices.append(float(value))
        return self._setup(inst_id, side, *prices, account)

    def _macro_text(self) -> str:
        if self._macro is None:
            raise ToolError("利率和美元数据没有打开。")
        text = self._macro.text()
        if not text:
            raise ToolError("这次没有从 FRED 读到利率和美元数据。需要的话可以用联网搜索查，并说明数据时间。")
        return text
