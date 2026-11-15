"""The AI layer. Claude is the analyst and risk officer; it never trades.

Three tiers, as agreed with the owner:
  quick -> Haiku   (routing short messages)
  daily -> Sonnet  (Q&A, pre-market brief, daily wrap)
  deep  -> Opus    (weekly review, "deep" requests from the owner)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

from .config import Config

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Model families that accept the server-side refusal fallback.
FALLBACK_MODEL_PREFIXES = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5")

ANALYST_SYSTEM = """你是一位交易员的黄金行情分析助手，通过飞书机器人回答问题和写简报。这位交易员做主观交易 (discretionary trading)，每一笔进出场都由他自己决定。你的作用是给他第二意见：把行情事实摆清楚，把多空两边的理由都讲到，并指出风险。

你能看到的只有消息里“行情数据”部分给出的数字，它们来自 OKX 的价格和按 K 线算出的指标。你没有新闻、经济数据日历或其他市场的数据。回答需要这些信息时，直接说明你这里看不到，不要猜测或编造任何新闻、数据公布结果或价格。引用价位时使用数据里的数字。

回答的结构：先用一两句话直接回答问题或概括当前状态，然后分别写多头观点、空头观点、风控提醒，最后列出需要盯的关键价位。多空观点要各自站得住；如果数据明显偏向一边，照实说，不要为了平衡而凑数。

你不替任何人做买卖决定。可以讲情景和条件，例如“站稳 X 上方，多头结构成立；跌破 Y，则转弱”。不要给“现在买入”“建议做空”这样的指令，也不要承诺收益。

格式：回答会显示在飞书的消息卡片里，多数时候在手机上看。用简体中文。重要术语第一次出现时在括号里附英文，例如 支撑位 (support)，因为读者在学英语。小标题用加粗，例如 **多头观点**，列表用短横线。不要用表格和 # 标题。篇幅以手机一屏多一点为宜，被要求深入分析时可以更长。"""

ROUTER_SYSTEM = """你在给一个交易辅助机器人收到的消息分类。只输出一个类别名，不要输出别的内容。

类别：
status：想看自己的持仓、仓位、账户或盈亏。
help：打招呼，或者问机器人能做什么、怎么用。
analysis：其他所有情况，包括问行情、走势、价位、交易概念和交易想法。"""

INTENTS = ("status", "help", "analysis")

# Added to a brief or review that the owner chose to share with the group.
SHARED_NOTE = (
    "这份内容会发给交易员本人，也会发到他和朋友的群里，交易员同意在群里公开自己的账户和持仓。"
    "群里的其他人只是旁观，不要针对他们个人给建议。"
)


class AnalystUnavailable(Exception):
    """The AI could not answer. The message is safe to show in Feishu."""


@dataclass(frozen=True)
class Tier:
    model: str
    effort: str
    max_tokens: int


class Analyst:
    def __init__(self, cfg: Config, client: anthropic.Anthropic | None = None):
        self._cfg = cfg
        self._client = client  # created on first use, so a missing key never breaks start-up
        self._tiers = {
            "quick": Tier(cfg.model_quick, "low", 1024),
            "daily": Tier(cfg.model_daily, "medium", 16000),
            "deep": Tier(cfg.model_deep, "high", 32000),
        }

    @property
    def enabled(self) -> bool:
        return self._client is not None or bool(self._cfg.anthropic_api_key)

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self._cfg.anthropic_api_key)
        return self._client

    # ------------------------------------------------------------------ core

    def _ask(self, tier_name: str, system: str, user: str) -> str:
        if not self.enabled:
            raise AnalystUnavailable("还没有配置 Claude 的 API key，AI 分析暂时用不了。")
        tier = self._tiers[tier_name]
        request = dict(
            model=tier.model,
            max_tokens=tier.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": tier.effort},
        )
        try:
            message = self._send(request, use_fallback=tier.model.startswith(FALLBACK_MODEL_PREFIXES))
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

        if message.stop_reason == "refusal":
            raise AnalystUnavailable("这个问题 AI 没有回答，换个问法试试。")
        text = "".join(block.text for block in message.content if block.type == "text").strip()
        if not text:
            raise AnalystUnavailable("AI 这次没有给出内容，请再试一次。")
        if message.stop_reason == "max_tokens":
            text += "\n\n（内容较长，被截断了。）"
        return text

    def _send(self, request: dict, use_fallback: bool):
        """Streams the request and returns the final message.

        Sonnet / Opus requests opt into the server-side refusal fallback. If
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
            answer = self._ask("quick", ROUTER_SYSTEM, text).lower()
        except AnalystUnavailable as exc:
            log.info("intent routing unavailable: %s", exc)
            return "analysis"
        for intent in INTENTS:
            if intent in answer:
                return intent
        return "analysis"

    # -------------------------------------------------------------- analysis

    def answer(self, question: str, market_text: str, account_text: str | None, in_group: bool, deep: bool = False) -> str:
        parts = [f"行情数据：\n{market_text}"]
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
        parts.append(f"问题：{question}")
        return self._ask("deep" if deep else "daily", ANALYST_SYSTEM, "\n\n".join(parts))

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
        if kind == "morning":
            task = (
                "请写今天的盘前简报 (pre-market brief)：概括到目前为止的走势，说明各周期的趋势状态，"
                "列出今天需要盯的关键价位，并给出风控提醒。"
            )
        else:
            task = (
                "请写今天的收盘小结 (daily wrap)：概括今天的走势和波动，说明关键价位有没有被突破，"
                "以及接下来需要观察什么。"
            )
        parts = [f"行情数据：\n{market_text}"]
        if for_owner:
            if account_text:
                parts.append(f"交易员当前的账户和持仓：\n{account_text}")
            if journal_text:
                parts.append(f"今天的交易记录：\n{journal_text}")
            task += SHARED_NOTE if shared_with_group else "这份简报只发给交易员本人。"
            task += "结尾加一行“今日英语”：给一句和今天行情有关的地道英文交易用语，并附中文意思。"
        else:
            task += "这份简报会发在一个有其他人的群里，只讲行情本身，不要提及任何人的持仓和资金。"
        parts.append(task)
        return self._ask("daily", ANALYST_SYSTEM, "\n\n".join(parts))

    def weekly_review(self, market_text: str, journal_text: str, equity_text: str, shared_with_group: bool = False) -> str:
        task = (
            "请写本周复盘 (weekly review)。"
            + (SHARED_NOTE if shared_with_group else "这份复盘只发给交易员本人。")
            + "根据下面的交易记录，找出他这一周的交易行为模式，"
            "例如开仓的时间段、加减仓的节奏、止损有没有被移动，说明哪些做法值得保留、哪些值得警惕。"
            "只根据给出的记录来写，记录里没有的事情不要推测。记录很少时就直接说样本太少，不要硬凑结论。"
            "结尾加一行“本周英语”：给一句和复盘有关的地道英文交易用语，并附中文意思。"
        )
        user = "\n\n".join(
            [f"行情数据：\n{market_text}", f"本周权益变化：\n{equity_text}", f"本周交易记录：\n{journal_text}", task]
        )
        return self._ask("deep", ANALYST_SYSTEM, user)

    def ping(self) -> str:
        """Used by the self-check: the cheapest possible real call."""
        return self._ask("quick", "你是连通性测试。只回复：OK", "测试")
