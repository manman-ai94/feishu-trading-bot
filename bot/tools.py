"""The tools the AI may call while it works on an answer.

Client tools (run here, on the server): market data and candles from OKX, and
a search of the owner's knowledge base. Server tools (run by Anthropic): web
search and web fetch, for news and economic data.

Every tool only reads. None of them touches the account or places an order.
"""
from __future__ import annotations

import logging
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
)


class ToolError(Exception):
    """The tool could not do what was asked. The message goes back to the AI."""


class Toolbox:
    def __init__(self, okx, resolver: InstrumentResolver, knowledge: KnowledgeBase | None, tz: ZoneInfo):
        self._okx = okx
        self._resolver = resolver
        self._knowledge = knowledge
        self._tz = tz

    def definitions(self, web_level: int = 0) -> list[dict]:
        """Tool definitions in a fixed order, so the cached prompt prefix stays the same."""
        tools = [tool for tool in CLIENT_TOOLS if tool["name"] != "search_knowledge" or self._knowledge is not None]
        return tools + list(WEB_TOOL_SETS[min(web_level, len(WEB_TOOL_SETS) - 1)])

    def run(self, name: str, tool_input) -> tuple[str, bool]:
        """Runs one client tool. Returns (text for the AI, whether it is an error)."""
        try:
            if not isinstance(tool_input, dict):
                raise ToolError("工具参数格式不对。")
            if name == "get_market":
                return self._market(tool_input), False
            if name == "get_candles":
                return self._candles(tool_input), False
            if name == "search_knowledge":
                return self._search(tool_input), False
            raise ToolError(f"没有叫 {name} 的工具。")
        except ToolError as exc:
            return str(exc), True
        except (OkxError, OkxNetworkError) as exc:
            return f"OKX 行情读取失败：{exc}", True
        except Exception:
            log.exception("tool %s failed", name)
            return "这个工具出了点问题，这次没有取到数据。", True

    # ------------------------------------------------------------------ tools

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
