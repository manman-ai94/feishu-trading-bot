from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from bot.analyst import ANALYST_SYSTEM, FALLBACK_BETA, Analyst, AnalystUnavailable

from .helpers import make_cfg


def message(text="分析内容", stop_reason="end_turn", blocks=None):
    content = blocks if blocks is not None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop_reason, content=content)


def api_error(cls, status):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(status, request=request), body=None)


class FakeStream:
    def __init__(self, final):
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._final


class FakeMessages:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeStream(outcome)


class FakeClient:
    def __init__(self, plain=(), beta=()):
        self.messages = FakeMessages(plain)
        self.beta = SimpleNamespace(messages=FakeMessages(beta))


def analyst(plain=(), beta=(), **cfg_overrides):
    client = FakeClient(plain, beta)
    return Analyst(make_cfg(**cfg_overrides), client), client


def test_daily_question_uses_sonnet_with_the_refusal_fallback():
    bot, client = analyst(beta=[message("看多看空都有理由")])
    text = bot.answer("现在黄金怎么看", "最新价：2,650.00", None, in_group=True)
    assert text == "看多看空都有理由"
    [call] = client.beta.messages.calls
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"] == {"effort": "medium"}
    assert call["betas"] == [FALLBACK_BETA] and call["fallbacks"] == "default"
    assert call["system"] == ANALYST_SYSTEM
    assert "thinking" not in call and "temperature" not in call
    assert client.messages.calls == []


def test_deep_question_uses_opus_at_high_effort():
    bot, client = analyst(beta=[message()])
    bot.answer("下周怎么看", "行情", "账户", in_group=False, deep=True)
    [call] = client.beta.messages.calls
    assert call["model"] == "claude-opus-5-5" and call["output_config"] == {"effort": "high"}


def test_routing_uses_haiku_without_the_fallback_option():
    bot, client = analyst(plain=[message("status")])
    assert bot.route_intent("帮我看下手上的单子") == "status"
    [call] = client.messages.calls
    assert call["model"] == "claude-haiku-5-5" and call["output_config"] == {"effort": "low"}
    assert "betas" not in call and "fallbacks" not in call
    assert client.beta.messages.calls == []


def test_fallback_rejected_by_the_api_is_retried_without_it():
    bot, client = analyst(beta=[api_error(anthropic.BadRequestError, 400)], plain=[message("照常回答")])
    assert bot.answer("现在黄金怎么看", "行情", None, in_group=True) == "照常回答"
    [retry] = client.messages.calls
    assert retry["model"] == "claude-sonnet-5-5" and "betas" not in retry


def test_group_prompt_without_the_account_keeps_to_the_market():
    bot, client = analyst(beta=[message()])
    bot.answer("现在黄金怎么看", "最新价：2,650.00", None, in_group=True)
    prompt = client.beta.messages.calls[0]["messages"][0]["content"]
    assert "账户" not in prompt and "不要提及或推测任何人的持仓和资金" in prompt
    assert "问题：现在黄金怎么看" in prompt


def test_group_prompt_with_a_shared_account():
    bot, client = analyst(beta=[message()])
    bot.answer("我的仓位风险大吗", "最新价：2,650.00", "账户权益 10,000.00 USD", in_group=True)
    prompt = client.beta.messages.calls[0]["messages"][0]["content"]
    assert "账户权益 10,000.00 USD" in prompt and "同意在群里公开" in prompt
    assert "不要针对他们个人给建议" in prompt and "不要提及或推测" not in prompt


def test_private_prompt_carries_the_account():
    bot, client = analyst(beta=[message()])
    bot.answer("我的仓位风险大吗", "最新价：2,650.00", "账户权益 10,000.00 USD", in_group=False)
    prompt = client.beta.messages.calls[0]["messages"][0]["content"]
    assert "账户权益 10,000.00 USD" in prompt and "行情数据：\n最新价：2,650.00" in prompt


def test_owner_and_group_briefs_differ():
    bot, client = analyst(beta=[message(), message()])
    bot.brief("evening", "行情", "账户权益 10,000.00 USD", "09:30 开仓", for_owner=True)
    bot.brief("evening", "行情", "账户权益 10,000.00 USD", "09:30 开仓", for_owner=False)
    owner_prompt, group_prompt = (call["messages"][0]["content"] for call in client.beta.messages.calls)
    assert "账户权益" in owner_prompt and "09:30 开仓" in owner_prompt and "今日英语" in owner_prompt
    assert "账户权益" not in group_prompt and "09:30 开仓" not in group_prompt and "今日英语" not in group_prompt
    assert "收盘小结" in owner_prompt and "收盘小结" in group_prompt


def test_shared_brief_and_review_tell_the_ai_who_will_read_them():
    bot, client = analyst(beta=[message(), message(), message()])
    bot.brief("morning", "行情", "账户权益 10,000.00 USD", None, for_owner=True, shared_with_group=True)
    bot.weekly_review("行情", "交易记录", "权益变化", shared_with_group=True)
    bot.weekly_review("行情", "交易记录", "权益变化")
    brief_prompt, shared_review, private_review = (call["messages"][0]["content"] for call in client.beta.messages.calls)
    assert "同意在群里公开" in brief_prompt and "只发给交易员本人" not in brief_prompt and "今日英语" in brief_prompt
    assert "同意在群里公开" in shared_review and "只发给交易员本人" not in shared_review
    assert "只发给交易员本人" in private_review and "同意在群里公开" not in private_review


def test_weekly_review_uses_the_deep_tier():
    bot, client = analyst(beta=[message("复盘")])
    assert bot.weekly_review("行情", "交易记录", "权益变化") == "复盘"
    assert client.beta.messages.calls[0]["model"] == "claude-opus-5-5"


def test_refusal_empty_and_truncated_answers():
    bot, _ = analyst(beta=[message(stop_reason="refusal", blocks=[])])
    with pytest.raises(AnalystUnavailable, match="换个问法"):
        bot.answer("问题", "行情", None, in_group=True)

    bot, _ = analyst(beta=[message(blocks=[SimpleNamespace(type="thinking", thinking="...")])])
    with pytest.raises(AnalystUnavailable, match="没有给出内容"):
        bot.answer("问题", "行情", None, in_group=True)

    bot, _ = analyst(beta=[message("写到一半", stop_reason="max_tokens")])
    assert bot.answer("问题", "行情", None, in_group=True).endswith("（内容较长，被截断了。）")


@pytest.mark.parametrize(
    "error, expected",
    [
        (api_error(anthropic.AuthenticationError, 401), "API key 无效"),
        (api_error(anthropic.PermissionDeniedError, 403), "没有权限"),
        (api_error(anthropic.NotFoundError, 404), "找不到模型 claude-sonnet-5-5"),
        (api_error(anthropic.RateLimitError, 429), "太频繁"),
        (api_error(anthropic.InternalServerError, 529), "（529）"),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages")), "连不上"),
    ],
)
def test_api_errors_become_plain_chinese_messages(error, expected):
    bot, _ = analyst(beta=[error])
    with pytest.raises(AnalystUnavailable, match=expected):
        bot.answer("问题", "行情", None, in_group=True)


def test_missing_key_is_reported_without_building_a_client():
    bot = Analyst(make_cfg(anthropic_api_key=""))
    assert not bot.enabled
    with pytest.raises(AnalystUnavailable, match="还没有配置"):
        bot.ping()


def test_route_intent_defaults_to_analysis():
    bot, _ = analyst(plain=[message("我觉得是别的")])
    assert bot.route_intent("随便说点什么") == "analysis"
    bot, _ = analyst(plain=[api_error(anthropic.RateLimitError, 429)])
    assert bot.route_intent("随便说点什么") == "analysis"


def test_custom_model_outside_the_fallback_families_skips_the_beta():
    bot, client = analyst(plain=[message()], model_daily="claude-haiku-5-5")
    bot.answer("问题", "行情", None, in_group=True)
    assert client.beta.messages.calls == [] and len(client.messages.calls) == 1
