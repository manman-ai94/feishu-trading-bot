"""The AI layer. Claude is the analyst and risk officer; it never trades.

Three tiers:
  quick -> MODEL_QUICK, low effort            (sorting short messages)
  daily -> MODEL_DAILY at EFFORT_DAILY        (questions, pre-market brief, daily wrap)
  deep  -> MODEL_DEEP at EFFORT_DEEP          (weekly review, "深度" requests from the owner)

For daily and deep work the AI is given tools (see tools.py): OKX market data
for any instrument, raw candles, the owner's knowledge base, and web search
for news and economic data. It calls them as it sees fit and this module runs
the loop until it has an answer. Every piece of work is metered, so the owner
can see what it costs and a monthly budget can hold it back.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import anthropic

from .config import Config
from .tools import WEB_TOOL_SETS, Toolbox

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Model families that accept the server-side refusal fallback.
FALLBACK_MODEL_PREFIXES = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5")

MAX_STEPS = 8  # rounds of tool calls in one piece of work
MAX_SOURCES = 5
BUDGET_TIER_TOKENS = 8000

# USD per million tokens: (input, output, cache read). Writing to the cache costs 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-2": (8.0, 40.0, 0.20),
}
UNKNOWN_MODEL_PRICE = PRICES["claude-opus-5-5"]
WEB_SEARCH_USD = 0.01  # per search

ANALYST_SYSTEM = """你是“交易助手”，一位交易员在飞书里用的行情分析助手。他做主观交易 (discretionary trading)，主要做黄金和加密货币，每一笔进出场都由他自己决定。他要的是拿来就能用的东西：结论、精确的价位、触发条件，而不是教科书式的讲解。

数据从哪里来：
- 消息里的“OKX 实时行情数据”是程序刚从 OKX 读到的数字。问到的品种不在里面时，用 get_market 去取，OKX 上交易的品种基本都能查，例如 BTC、ETH、SOL、XAU。需要精确的波段高低点时，用 get_candles 看 K 线。
- 新闻、经济数据和公布时间、OKX 上没有的市场（美股、美元指数、美债收益率等），用 web_search 查，并说明消息的时间。网页上的价格可能有延迟，OKX 上有的品种一律以 OKX 的数字为准。
- 交易员自己的交易规则、笔记，以及他收集的投资人理念和方法，在资料库里，用 search_knowledge 查。问题涉及他的规则和方法、某位投资人的做法，或者值得拿他自己的规则来对照时，先查一下，引用时说明出自资料库。
- 所有价位、数据和新闻都必须来自上面这些来源。没取到的就说没取到，不要凭印象写价格、新闻或数据结果。

怎么回答：
先查完需要的数据，再一次写出完整的回答。不要写“我先查一下”这类过程话，也不要寒暄和铺垫。全文尽量短，以手机一屏左右为宜。
只是问一个事实（某个数据几点公布、某个词是什么意思、某个价格是多少），用两三句话直接回答，不要套下面的结构。
问某个品种的走势或“怎么看”时，按这个结构写：
**结论**：一句话说明偏多、偏空还是震荡，以及最主要的依据。
**关键点位**：阻力位 (resistance) 和支撑位 (support) 各一到两个，写精确数字，并在括号里注明依据，例如 4H 前高、日线 EMA50。
**多头情景**：触发条件（站稳哪个价位）、目标位、失效位。
**空头情景**：触发条件（跌破哪个价位）、目标位、失效位。
**风险和事件**：最多两条，带时间，时间按交易员所在的时区写。
数据明显偏向一边就照实说，不要为了平衡硬凑另一边。用词客观，不夸张。

边界：
你给的是分析和价位，买不买、卖不卖由交易员自己决定。用“站稳 X 上方看 Y，跌破 Z 则这个判断失效”这样的条件句来表达，不要写“现在买入”“马上做空”这样的指令，不承诺收益，也不替他决定仓位大小。他给出止损价和愿意承担的亏损金额、让你算仓位时，可以帮他算。

格式：
回答显示在飞书的消息卡片里，多数时候在手机上看。用简体中文。重要术语第一次出现时可以在括号里附英文，因为读者在学英语，每条回答三四处就够。小标题用加粗，列表用短横线，不要用表格和 # 标题。价格按该品种通常的小数位写。"""

ROUTER_SYSTEM = """你在给一个交易辅助机器人收到的消息分类。只输出一个类别名，不要输出别的内容。

类别：
status：想看自己的持仓、仓位、账户或盈亏。
help：打招呼，或者问机器人能做什么、怎么用。
analysis：其他所有情况，包括问行情、走势、价位、新闻、交易概念和交易想法。"""

INTENTS = ("status", "help", "analysis")
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# Added to a brief or review that the owner chose to share with the group.
SHARED_NOTE = (
    "这份内容会发给交易员本人，也会发到他和朋友的群里，交易员同意在群里公开自己的账户和持仓。"
    "群里的其他人只是旁观，不要针对他们个人给建议。"
)
NO_TOOLS_NOTE = "这次没有任何工具可用，只能根据上面给出的数据回答，缺的数据直接说明。"
NO_WEB_NOTE = "这次没有联网搜索工具，新闻和经济数据查不到，需要时直接说明看不到。"
DEEP_NOTE = (
    "这次交易员要的是深度分析：可以多查几轮数据和资料，把各周期的结构、关键事件和两种情景讲透，"
    "篇幅可以到两三屏，结构不变。"
)


class AnalystUnavailable(Exception):
    """The AI could not answer. The message is safe to show in Feishu."""


@dataclass(frozen=True)
class Tier:
    model: str
    effort: str
    max_tokens: int
    tools: bool = False


@dataclass
class Usage:
    """What one piece of AI work used, summed over every request it took."""

    model: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0

    def add(self, raw) -> None:
        if raw is None:
            return
        self.requests += 1
        self.input_tokens += int(getattr(raw, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(raw, "output_tokens", 0) or 0)
        self.cache_read_tokens += int(getattr(raw, "cache_read_input_tokens", 0) or 0)
        self.cache_write_tokens += int(getattr(raw, "cache_creation_input_tokens", 0) or 0)
        server = getattr(raw, "server_tool_use", None)
        if server is not None:
            self.web_searches += int(getattr(server, "web_search_requests", 0) or 0)

    @property
    def cost_usd(self) -> float:
        """An estimate at list prices. The bill in the Claude Console is what counts."""
        price_in, price_out, price_read = next(
            (price for prefix, price in PRICES.items() if self.model.startswith(prefix)), UNKNOWN_MODEL_PRICE
        )
        tokens = (
            self.input_tokens * price_in
            + self.cache_write_tokens * price_in * 1.25
            + self.cache_read_tokens * price_read
            + self.output_tokens * price_out
        )
        return tokens / 1_000_000 + self.web_searches * WEB_SEARCH_USD

    def as_row(self) -> dict:
        return dict(
            model=self.model,
            requests=self.requests,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            web_searches=self.web_searches,
            cost_usd=self.cost_usd,
        )


class Analyst:
    def __init__(
        self,
        cfg: Config,
        client: anthropic.Anthropic | None = None,
        toolbox: Toolbox | None = None,
        on_usage: Callable[[str, Usage], None] | None = None,
        month_spend: Callable[[], float] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        """`on_usage(purpose, usage)` is called after every piece of work.
        `month_spend()` returns this month's estimated cost in USD, for the budget."""
        self._cfg = cfg
        self._client = client  # created on first use, so a missing key never breaks start-up
        self._toolbox = toolbox
        self._on_usage = on_usage
        self._month_spend = month_spend
        self._clock = clock
        # Which set of web tools to offer; steps down when the API turns one away.
        self._web_level = 0 if cfg.web_search else len(WEB_TOOL_SETS) - 1
        self._tiers = {
            "quick": Tier(cfg.model_quick, "low", 1024),
            "daily": Tier(cfg.model_daily, cfg.effort_daily, 16000, tools=True),
            "deep": Tier(cfg.model_deep, cfg.effort_deep, 32000, tools=True),
        }

    @property
    def enabled(self) -> bool:
        return self._client is not None or bool(self._cfg.anthropic_api_key)

    @property
    def can_search_web(self) -> bool:
        return self._toolbox is not None and bool(WEB_TOOL_SETS[self._web_level])

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self._cfg.anthropic_api_key)
        return self._client

    # ------------------------------------------------------------------ core

    def _over_budget(self) -> bool:
        budget = self._cfg.ai_monthly_budget_usd
        if budget is None or self._month_spend is None:
            return False
        try:
            return self._month_spend() >= budget
        except Exception:
            log.exception("could not read this month's AI spend")
            return False

    def _ask(self, tier_name: str, system: str, user: str, purpose: str = "", history=None) -> str:
        if not self.enabled:
            raise AnalystUnavailable("还没有配置 Claude 的 API key，AI 分析暂时用不了。")
        tier = self._tiers[tier_name]
        saving = tier_name != "quick" and self._over_budget()
        if saving:
            tier = Tier(self._cfg.model_daily, "low", BUDGET_TIER_TOKENS, tools=tier.tools)
        messages: list[dict] = []
        for question, answer in history or ():
            messages.append({"role": "user", "content": question})
            messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": user})

        usage = Usage(tier.model)
        web_before = self.can_search_web
        try:
            message = self._converse(tier, system, messages, usage)
        except anthropic.AuthenticationError as exc:
            raise AnalystUnavailable("Claude 的 API key 无效，请检查 .env 里的 ANTHROPIC_API_KEY。") from exc
        except anthropic.PermissionDeniedError as exc:
            raise AnalystUnavailable("这把 Claude API key 没有权限使用所选的模型。") from exc
        except anthropic.NotFoundError as exc:
            raise AnalystUnavailable(f"找不到模型 {tier.model}，请检查 .env 里的模型名称。") from exc
        except anthropic.RateLimitError as exc:
            raise AnalystUnavailable("Claude 那边提示请求太频繁或额度用完了，请稍后再试。") from exc
        except anthropic.APIStatusError as exc:
            log.warning("Claude API error %s: %s", exc.status_code, exc.message)
            raise AnalystUnavailable(f"Claude 接口返回了错误（{exc.status_code}），请稍后再试。") from exc
        except anthropic.APIConnectionError as exc:
            raise AnalystUnavailable("连不上 Claude 的接口，请稍后再试。") from exc
        finally:
            self._report(purpose or tier_name, usage)

        if message.stop_reason == "refusal":
            raise AnalystUnavailable("这个问题 AI 没有回答，换个问法试试。")
        text = _final_text(message)
        if not text:
            raise AnalystUnavailable("AI 这次没有给出内容，请再试一次。")
        if message.stop_reason == "max_tokens":
            text += "\n\n（内容较长，被截断了。）"
        if web_before and not self.can_search_web:
            text += "\n\n（这把 Claude API key 所在的组织没有开通联网搜索，这次没有查新闻。在 Claude Console 的设置里打开 Web search 后重启机器人即可。）"
        if saving:
            text += f"\n\n（本月 AI 用量已到预算上限 {self._cfg.ai_monthly_budget_usd:g} 美元，这条回答用的是省钱档。）"
        return text

    def _report(self, purpose: str, usage: Usage) -> None:
        if usage.requests == 0:
            return
        log.info(
            "AI %s: %s, %d request(s), %d in / %d out / %d cached tokens, %d web search(es), about $%.3f",
            purpose, usage.model, usage.requests, usage.input_tokens + usage.cache_write_tokens,
            usage.output_tokens, usage.cache_read_tokens, usage.web_searches, usage.cost_usd,
        )  # fmt: skip
        if self._on_usage is not None:
            try:
                self._on_usage(purpose, usage)
            except Exception:
                log.exception("could not record AI usage")

    def _converse(self, tier: Tier, system: str, messages: list[dict], usage: Usage):
        """Sends the conversation, runs the tools the AI asks for, and returns its final message."""
        toolbox = self._toolbox if tier.tools else None
        if toolbox is None:
            message = self._send(
                dict(model=tier.model, max_tokens=tier.max_tokens, system=system, messages=messages,
                     output_config={"effort": tier.effort}),
                use_fallback=tier.model.startswith(FALLBACK_MODEL_PREFIXES),
            )  # fmt: skip
            usage.add(getattr(message, "usage", None))
            return message

        # The instructions never change, so they are cached; the automatic
        # breakpoint then follows the conversation as the tool rounds add to it.
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        start = len(messages)
        for step in range(MAX_STEPS):
            while True:
                request = dict(
                    model=tier.model,
                    max_tokens=tier.max_tokens,
                    system=system_blocks,
                    messages=messages,
                    tools=toolbox.definitions(self._web_level),
                    output_config={"effort": tier.effort},
                    cache_control={"type": "ephemeral"},
                )
                try:
                    message = self._send(request, use_fallback=False)
                    break
                except (anthropic.BadRequestError, anthropic.PermissionDeniedError) as exc:
                    # A web tool this organisation cannot use: offer an older one, then none.
                    if len(messages) != start or not self.can_search_web or not _about_tools(exc):
                        raise
                    self._web_level += 1
                    log.warning("the API turned the web tools away (%s); stepping down to set %d", exc.message, self._web_level)
            usage.add(getattr(message, "usage", None))

            if message.stop_reason == "pause_turn":  # a long server-side search: send it back to continue
                messages.append({"role": "assistant", "content": message.content})
                continue
            if message.stop_reason != "tool_use":
                return message

            messages.append({"role": "assistant", "content": message.content})
            results: list[dict] = []
            for block in message.content:
                if block.type != "tool_use":
                    continue
                text, is_error = toolbox.run(block.name, block.input)
                result = {"type": "tool_result", "tool_use_id": block.id, "content": text}
                if is_error:
                    result["is_error"] = True
                results.append(result)
            if step == MAX_STEPS - 2:
                results.append({"type": "text", "text": "查询次数快到上限了。请不要再调用工具，直接根据已经拿到的信息写出最终回答。"})
            messages.append({"role": "user", "content": results})
        raise AnalystUnavailable("这个问题查了很多轮还没有结论，请把问题问得具体一点再试。")

    def _send(self, request: dict, use_fallback: bool):
        """Streams the request and returns the final message.

        Requests without tools opt into the server-side refusal fallback. If
        the API rejects that option, the request is sent again without it.
        """
        client = self._get_client()
        if use_fallback:
            try:
                with client.beta.messages.stream(**request, betas=[FALLBACK_BETA], fallbacks="default") as stream:
                    return stream.get_final_message()
            except anthropic.BadRequestError as exc:
                log.warning("request with refusal fallback was rejected (%s); retrying without it", exc.message)
        with client.messages.stream(**request) as stream:
            return stream.get_final_message()

    # --------------------------------------------------------------- routing

    def route_intent(self, text: str) -> str:
        """Classify a free-text message. Falls back to 'analysis' on any trouble."""
        try:
            answer = self._ask("quick", ROUTER_SYSTEM, text, purpose="routing").lower()
        except AnalystUnavailable as exc:
            log.info("intent routing unavailable: %s", exc)
            return "analysis"
        for intent in INTENTS:
            if intent in answer:
                return intent
        return "analysis"

    # -------------------------------------------------------------- analysis

    def _now_line(self) -> str:
        now = datetime.fromtimestamp(self._clock(), tz=self._cfg.tz)
        return f"现在是 {now.strftime('%Y-%m-%d %H:%M')}，{WEEKDAYS[now.weekday()]}（交易员的时区 {self._cfg.timezone}）。"

    def _tools_note(self) -> str | None:
        if self._toolbox is None:
            return NO_TOOLS_NOTE
        return None if self.can_search_web else NO_WEB_NOTE

    def answer(
        self,
        question: str,
        market_text: str,
        account_text: str | None,
        in_group: bool,
        deep: bool = False,
        history: list[tuple[str, str]] | None = None,
    ) -> str:
        """`history` is the last few (question, answer) pairs of the same chat, oldest first."""
        parts = [self._now_line()]
        if market_text:
            parts.append(f"OKX 实时行情数据：\n{market_text}")
        if in_group and account_text:
            parts.append(
                "这条回答会发在交易员和他朋友的群里，交易员同意在群里公开自己的账户和持仓。"
                f"他当前的账户和持仓：\n{account_text}\n"
                "可以结合持仓讲风险。群里的其他人只是旁观，不要针对他们个人给建议。"
            )
        elif in_group:
            parts.append(
                "这条回答会发在一个有其他人的群里。只讲行情本身，不要提及或推测任何人的持仓和资金，"
                "也不要针对提问者个人给建议。"
            )
        elif account_text:
            parts.append(f"提问的是交易员本人。他当前的账户和持仓：\n{account_text}\n可以结合持仓讲风险。")
        note = self._tools_note()
        if note:
            parts.append(note)
        if deep:
            parts.append(DEEP_NOTE)
        parts.append(f"问题：{question}")
        tier = "deep" if deep else "daily"
        return self._ask(tier, ANALYST_SYSTEM, "\n\n".join(parts), purpose="deep question" if deep else "question", history=history)

    def brief(
        self,
        kind: str,
        market_text: str,
        account_text: str | None,
        journal_text: str | None,
        for_owner: bool,
        shared_with_group: bool = False,
    ) -> str:
        """for_owner: the version with the account in it. shared_with_group: that
        same version is also posted in the group, because the owner chose to."""
        search = self.can_search_web
        if kind == "morning":
            task = "请写今天的盘前简报 (pre-market brief)。"
            if search:
                task += "先搜索过去 24 小时影响这些品种的重要新闻，以及今天和明天的重要经济数据和事件。"
            task += (
                "内容：一句话结论、各周期的趋势状态、今天需要盯的关键点位（精确数字）、多空两种情景，"
                "以及风险和事件（带时间）。保持简短。"
            )
        else:
            task = "请写今天的收盘小结 (daily wrap)。"
            if search:
                task += "先搜索今天影响这些品种的重要新闻和已经公布的经济数据。"
            task += "内容：今天的走势和波动、关键点位有没有被突破、驱动行情的消息，以及接下来需要观察什么。保持简短。"
        parts = [self._now_line(), f"OKX 实时行情数据：\n{market_text}"]
        if for_owner:
            if account_text:
                parts.append(f"交易员当前的账户和持仓：\n{account_text}")
            if journal_text:
                parts.append(f"今天的交易记录：\n{journal_text}")
            task += SHARED_NOTE if shared_with_group else "这份简报只发给交易员本人。"
            task += "结尾加一行“今日英语”：给一句和今天行情有关的地道英文交易用语，并附中文意思。"
        else:
            task += "这份简报会发在一个有其他人的群里，只讲行情本身，不要提及任何人的持仓和资金。"
        note = self._tools_note()
        if note:
            parts.append(note)
        parts.append(task)
        return self._ask("daily", ANALYST_SYSTEM, "\n\n".join(parts), purpose=f"{kind} brief")

    def weekly_review(self, market_text: str, journal_text: str, equity_text: str, shared_with_group: bool = False) -> str:
        task = (
            "请写本周复盘 (weekly review)。"
            + (SHARED_NOTE if shared_with_group else "这份复盘只发给交易员本人。")
            + "根据下面的交易记录，找出他这一周的交易行为模式，"
            "例如开仓的时间段、加减仓的节奏、止损有没有被移动，说明哪些做法值得保留、哪些值得警惕。"
            "只根据给出的记录来写，记录里没有的事情不要推测。记录很少时就直接说样本太少，不要硬凑结论。"
        )
        if self._toolbox is not None:
            task += "资料库里如果有他自己的交易规则，拿来对照他这一周的做法。"
        task += "结尾加一行“本周英语”：给一句和复盘有关的地道英文交易用语，并附中文意思。"
        parts = [
            self._now_line(),
            f"OKX 实时行情数据：\n{market_text}",
            f"本周权益变化：\n{equity_text}",
            f"本周交易记录：\n{journal_text}",
        ]
        note = self._tools_note()
        if note:
            parts.append(note)
        parts.append(task)
        return self._ask("deep", ANALYST_SYSTEM, "\n\n".join(parts), purpose="weekly review")

    def ping(self) -> str:
        """Used by the self-check: the cheapest possible real call."""
        return self._ask("quick", "你是连通性测试。只回复：OK", "测试", purpose="self-check")

    def check_web_search(self) -> None:
        """Used by the self-check: one real search. Raises AnalystUnavailable when it cannot be done."""
        if self._toolbox is None or not self._cfg.web_search:
            raise AnalystUnavailable("联网搜索没有打开（WEB_SEARCH=false）。")
        if not self.enabled:
            raise AnalystUnavailable("还没有配置 Claude 的 API key。")
        usage = Usage(self._cfg.model_daily)
        tier = Tier(self._cfg.model_daily, "low", 4000, tools=True)
        messages = [{"role": "user", "content": "这是连通性测试。请用 web_search 搜索一次 gold price，然后只回复两个字：可用"}]
        try:
            self._converse(tier, "你是连通性测试，按要求做，不要多写。", messages, usage)
        except anthropic.APIError as exc:
            raise AnalystUnavailable(f"联网搜索的测试请求失败：{getattr(exc, 'message', exc)}") from exc
        finally:
            self._report("self-check", usage)
        if not self.can_search_web:
            raise AnalystUnavailable("这把 Claude API key 所在的组织没有开通联网搜索。在 Claude Console 的设置里打开 Web search 即可。")
        if usage.web_searches == 0:
            raise AnalystUnavailable("测试请求成功了，但模型没有执行搜索，联网搜索的状态不确定。")


def _about_tools(exc: anthropic.APIStatusError) -> bool:
    text = str(getattr(exc, "message", "") or exc).lower()
    return any(word in text for word in ("web_search", "web_fetch", "web search", "web fetch", "tool"))


def _final_text(message) -> str:
    """The answer, without the remarks the AI makes between tool calls, plus the pages it cited."""
    blocks = list(message.content)
    last_tool = max(
        (index for index, block in enumerate(blocks) if block.type not in ("text", "thinking", "redacted_thinking")),
        default=-1,
    )
    every = [block for block in blocks if block.type == "text"]
    tail = [block for block in blocks[last_tool + 1 :] if block.type == "text"]
    tail_size = len("".join(block.text for block in tail).strip())
    head_size = len("".join(block.text for block in blocks[: last_tool + 1] if block.type == "text").strip())
    # A few closing words after a long text and one last search are not the answer: then keep everything.
    chosen = tail if tail_size and (tail_size >= 40 or head_size < 200) else every
    text = "".join(block.text for block in chosen).strip()
    if not text:
        return ""
    sources: list[tuple[str, str]] = []
    for block in chosen:
        for citation in getattr(block, "citations", None) or ():
            url = getattr(citation, "url", None)
            if url and all(url != known for _, known in sources):
                title = (getattr(citation, "title", None) or url).replace("[", "(").replace("]", ")").strip()
                sources.append((title[:60], url))
    if sources:
        text += "\n\n**来源**\n" + "\n".join(f"- [{title}]({url})" for title, url in sources[:MAX_SOURCES])
    return text
