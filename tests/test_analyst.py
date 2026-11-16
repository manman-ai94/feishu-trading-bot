from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from bot.analyst import (
    ANALYST_SYSTEM,
    DEEP_NOTE,
    FALLBACK_BETA,
    SEARCHED_ENOUGH_NOTE,
    SMALL_TALK_SYSTEM,
    TITLE_NOTE,
    Analyst,
    AnalystUnavailable,
)

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
        kwargs["messages"] = list(kwargs["messages"])  # as it was when sent; the caller keeps adding to its list
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


def test_daily_question_uses_opus_at_medium_effort_with_the_refusal_fallback():
    bot, client = analyst(beta=[message("看多看空都有理由")])
    text = bot.answer("现在黄金怎么看", "最新价：2,650.00", None, in_group=True)
    assert text == "看多看空都有理由"
    [call] = client.beta.messages.calls
    assert call["model"] == "claude-opus-5-5"
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
    assert retry["model"] == "claude-opus-5-5" and "betas" not in retry


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
    assert "账户权益 10,000.00 USD" in prompt and "账户和持仓可以在群里谈" in prompt
    assert "不要针对他们个人给建议" in prompt and "不要提及或推测" not in prompt


def test_private_prompt_carries_the_account():
    bot, client = analyst(beta=[message()])
    bot.answer("我的仓位风险大吗", "最新价：2,650.00", "账户权益 10,000.00 USD", in_group=False)
    prompt = client.beta.messages.calls[0]["messages"][0]["content"]
    assert "账户权益 10,000.00 USD" in prompt and "OKX 实时行情数据：\n最新价：2,650.00" in prompt


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
        (api_error(anthropic.NotFoundError, 404), "找不到模型 claude-opus-5-5"),
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


# ------------------------------------------------------ tools, cost, budget


def text_block(text, citations=None):
    return SimpleNamespace(type="text", text=text, citations=citations)


def tool_use(call_id, name, **tool_input):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=tool_input)


def usage(input_tokens=0, output_tokens=0, cache_read=0, cache_write=0, searches=0):
    return SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens, cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write, server_tool_use=SimpleNamespace(web_search_requests=searches, web_fetch_requests=0),
    )  # fmt: skip


def reply(blocks, stop_reason="end_turn", used=None):
    return SimpleNamespace(stop_reason=stop_reason, content=blocks, usage=used)


class FakeToolbox:
    def __init__(self):
        self.runs: list[tuple[str, dict]] = []
        self.results = {"get_market": ("品种：BTC-USDT-SWAP\n最新价：67,000.00", False)}

    def definitions(self, web_level=0, web_searches=3):
        client_tools = [{"name": "get_market"}, {"name": "search_knowledge"}]
        web = [["web_search_20260209"], ["web_search_20250305"], []][min(web_level, 2)]
        return client_tools + [{"type": kind, "name": "web_search", "max_uses": web_searches} for kind in web]

    def run(self, name, tool_input):
        self.runs.append((name, tool_input))
        return self.results.get(name, ("没有这个工具", True))


def tooled(plain, spend=None, **cfg_overrides):
    client, box, reports = FakeClient(plain), FakeToolbox(), []
    bot = Analyst(
        make_cfg(**cfg_overrides), client, toolbox=box, on_usage=lambda purpose, used: reports.append((purpose, used)),
        month_spend=(lambda: spend) if spend is not None else None, clock=lambda: 1_800_000_000.0,
    )  # fmt: skip
    return bot, client, box, reports


def test_the_ai_fetches_what_it_needs_and_then_answers():
    first = reply([text_block("我先查一下 BTC。"), tool_use("call_1", "get_market", symbol="BTC")], "tool_use", usage(1000, 100, cache_write=2000))
    last = reply([text_block("**结论**：BTC 偏多。")], used=usage(300, 400, cache_read=3000))
    bot, client, box, reports = tooled([first, last])

    assert bot.answer("BTC 现在怎么看", "", None, in_group=False) == "**结论**：BTC 偏多。"
    assert box.runs == [("get_market", {"symbol": "BTC"})]
    assert client.beta.messages.calls == []  # no model fallback in the middle of a tool conversation
    one, two = client.messages.calls
    assert one["model"] == "claude-opus-5-5" and one["output_config"] == {"effort": "medium"}
    assert [tool["name"] for tool in one["tools"]] == ["get_market", "search_knowledge", "web_search"]
    assert one["system"] == [{"type": "text", "text": ANALYST_SYSTEM, "cache_control": {"type": "ephemeral"}}]
    assert one["cache_control"] == {"type": "ephemeral"} and "thinking" not in one and "tool_choice" not in one
    assert one["tools"] == two["tools"] and one["system"] == two["system"]  # the cached prefix is unchanged
    assistant, results = two["messages"][-2:]
    assert assistant == {"role": "assistant", "content": first.content}  # sent back exactly as received
    assert results == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "品种：BTC-USDT-SWAP\n最新价：67,000.00"}]}

    [(purpose, used)] = reports  # one piece of work, however many requests
    assert purpose == "question" and used.requests == 2 and used.model == "claude-opus-5-5"
    assert (used.input_tokens, used.output_tokens, used.cache_read_tokens, used.cache_write_tokens) == (1300, 500, 3000, 2000)
    expected = (1300 * 4 + 2000 * 4 * 1.25 + 3000 * 0.20 + 500 * 20) / 1_000_000
    assert abs(used.cost_usd - expected) < 1e-12


def test_the_prompt_tells_the_ai_the_time_and_what_is_already_fetched():
    bot, client, _, _ = tooled([reply([text_block("好")])])
    bot.answer("现在怎么看", "品种：XAU-USDT-SWAP", None, in_group=False, deep=True)
    prompt = client.messages.calls[0]["messages"][-1]["content"]
    assert "现在是 2027-01-15 00:00，星期五（交易员的时区 America/Los_Angeles）" in prompt
    assert "OKX 实时行情数据：\n品种：XAU-USDT-SWAP" in prompt and "深度分析" in prompt and prompt.endswith("问题：现在怎么看")
    assert "没有任何工具" not in prompt and "没有联网搜索" not in prompt
    assert client.messages.calls[0]["output_config"] == {"effort": "high"}

    bare, bare_client = analyst(beta=[message()])  # no toolbox at all
    bare.answer("现在怎么看", "", None, in_group=False)
    prompt = bare_client.beta.messages.calls[0]["messages"][-1]["content"]
    assert "没有任何工具" in prompt and "OKX 实时行情数据" not in prompt


def test_earlier_questions_of_the_chat_are_sent_along():
    bot, client, _, _ = tooled([reply([text_block("ETH 偏弱。")])])
    bot.answer("那 ETH 呢", "", None, in_group=False, history=[("BTC 现在怎么看", "BTC 偏多。")])
    messages = client.messages.calls[0]["messages"]
    assert messages[:2] == [{"role": "user", "content": "BTC 现在怎么看"}, {"role": "assistant", "content": "BTC 偏多。"}]
    assert messages[2]["role"] == "user" and messages[2]["content"].endswith("问题：那 ETH 呢")


def test_a_failed_tool_is_reported_to_the_ai_as_an_error():
    first = reply([tool_use("call_1", "get_orderbook", symbol="BTC")], "tool_use")
    bot, client, _, _ = tooled([first, reply([text_block("取不到盘口数据。")])])
    assert bot.answer("盘口怎么样", "", None, in_group=False) == "取不到盘口数据。"
    [result] = client.messages.calls[1]["messages"][-1]["content"]
    assert result == {"type": "tool_result", "tool_use_id": "call_1", "content": "没有这个工具", "is_error": True}


def test_searches_are_limited_and_a_deep_question_gets_more():
    bot, client, _, _ = tooled([reply([text_block("回答")]), reply([text_block("深度回答")])])
    bot.answer("BTC 现在怎么看", "", None, in_group=False)
    bot.answer("BTC 现在怎么看", "", None, in_group=False, deep=True)
    everyday, deep = client.messages.calls
    assert everyday["tools"][-1] == {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
    assert deep["tools"][-1]["max_uses"] == 6


def test_the_ai_is_told_once_when_its_searches_are_used_up():
    searched = reply([tool_use("call_1", "get_market", symbol="BTC")], "tool_use", usage(searches=3))
    again = reply([tool_use("call_2", "get_market", symbol="ETH")], "tool_use", usage())
    bot, client, _, reports = tooled([searched, again, reply([text_block("回答")], used=usage())])
    assert bot.answer("BTC 和 ETH 怎么看", "", None, in_group=False) == "回答"

    one, two, three = client.messages.calls
    assert one["tools"] == two["tools"] == three["tools"]  # the limit is said in words, so the cached prefix holds
    assert two["messages"][-1]["content"][-1] == {"type": "text", "text": SEARCHED_ENOUGH_NOTE}
    assert [block["type"] for block in three["messages"][-1]["content"]] == ["tool_result"]  # said once, not every round
    assert reports[0][1].web_searches == 3


def test_no_note_about_searches_while_some_are_left():
    first = reply([tool_use("call_1", "get_market", symbol="BTC")], "tool_use", usage(searches=2))
    bot, client, _, _ = tooled([first, reply([text_block("回答")])])
    bot.answer("BTC 现在怎么看", "", None, in_group=False)
    assert [block["type"] for block in client.messages.calls[1]["messages"][-1]["content"]] == ["tool_result"]


def test_a_paused_web_search_is_sent_back_to_continue():
    paused = reply([SimpleNamespace(type="server_tool_use", id="srv_1", name="web_search", input={})], "pause_turn", usage(searches=1))
    bot, client, box, reports = tooled([paused, reply([text_block("今晚 21:30 公布 CPI。")], used=usage(searches=2))])
    assert bot.answer("今晚有什么数据", "", None, in_group=False) == "今晚 21:30 公布 CPI。"
    assert client.messages.calls[1]["messages"][-1] == {"role": "assistant", "content": paused.content}  # no extra user turn
    assert box.runs == [] and reports[0][1].web_searches == 3
    assert abs(reports[0][1].cost_usd - 0.03) < 1e-12


def test_endless_tool_calls_are_cut_off():
    again = [reply([tool_use(f"call_{index}", "get_market", symbol="BTC")], "tool_use") for index in range(8)]
    bot, client, box, reports = tooled(again)
    with pytest.raises(AnalystUnavailable, match="查了很多轮"):
        bot.answer("一直查", "", None, in_group=False)
    assert len(client.messages.calls) == 8 and len(box.runs) == 8
    assert reports == []  # these fake replies carry no usage, so there is nothing to record
    nudges = [call["messages"][-1]["content"][-1] for call in client.messages.calls[1:]]
    assert [item.get("type") == "text" and "不要再调用工具" in item.get("text", "") for item in nudges] == [False] * 6 + [True]


def test_answer_drops_the_remarks_made_between_searches_and_lists_sources():
    cite = [SimpleNamespace(type="web_search_result_location", url="https://example.com/cpi", title="CPI [release] schedule")]
    blocks = [
        text_block("我查一下今晚的数据。"),
        SimpleNamespace(type="server_tool_use", id="srv_1", name="web_search", input={}),
        SimpleNamespace(type="web_search_tool_result", tool_use_id="srv_1", content=[]),
        text_block("**结论**：今晚 21:30 公布美国 CPI，"),
        text_block("市场预期 3.1%。", citations=cite + cite),
        text_block("公布前后波动会放大，注意仓位。"),
    ]
    bot, _, _, _ = tooled([reply(blocks)])
    text = bot.answer("今晚有什么数据", "", None, in_group=False)
    assert text.startswith("**结论**：今晚 21:30 公布美国 CPI，市场预期 3.1%。公布前后波动会放大") and "我查一下" not in text
    assert text.endswith("**来源**\n- [CPI (release) schedule](https://example.com/cpi)")  # each page once


def test_a_few_closing_words_after_the_last_search_do_not_replace_the_answer():
    long_answer = "**结论**：震荡偏多。" + "关键点位和两种情景的说明。" * 20
    blocks = [text_block(long_answer), SimpleNamespace(type="server_tool_use", id="srv_1", name="web_search", input={}), text_block("以上。")]
    bot, _, _, _ = tooled([reply(blocks)])
    assert bot.answer("怎么看", "", None, in_group=False) == long_answer + "以上。"


def test_web_tools_the_account_cannot_use_are_stepped_down_once():
    rejected = api_error(anthropic.BadRequestError, 400)
    rejected.message = "tools.2: web_search_20260209 is not enabled for this organization"
    bot, client, _, _ = tooled([rejected, rejected, reply([text_block("只看行情的回答")]), reply([text_block("第二个回答")])])
    assert bot.can_search_web

    text = bot.answer("今天有什么新闻", "", None, in_group=False)
    assert text.startswith("只看行情的回答") and "没有开通联网搜索" in text
    offered = [[tool.get("type") for tool in call["tools"] if "type" in tool] for call in client.messages.calls]
    assert offered == [["web_search_20260209"], ["web_search_20250305"], []]
    assert not bot.can_search_web

    assert bot.answer("再问一次", "", None, in_group=False) == "第二个回答"  # told once, then it just works without
    assert "没有联网搜索工具" in client.messages.calls[3]["messages"][-1]["content"]


def test_other_bad_requests_are_not_mistaken_for_a_web_tool_problem():
    bot, client, _, _ = tooled([api_error(anthropic.BadRequestError, 400)])
    with pytest.raises(AnalystUnavailable, match="（400）"):
        bot.answer("问题", "", None, in_group=False)
    assert len(client.messages.calls) == 1 and bot.can_search_web


def test_web_search_can_be_switched_off():
    bot, client, _, _ = tooled([reply([text_block("回答")])], web_search=False)
    bot.answer("问题", "", None, in_group=False)
    assert [tool["name"] for tool in client.messages.calls[0]["tools"]] == ["get_market", "search_knowledge"]
    assert not bot.can_search_web and "没有联网搜索工具" in client.messages.calls[0]["messages"][-1]["content"]


def test_over_the_monthly_budget_the_ai_switches_to_its_cheapest_setting():
    bot, client, _, _ = tooled([reply([text_block("省钱的回答")])], spend=180.0, model_daily="claude-sonnet-5-5")
    text = bot.answer("深度问题", "", None, in_group=False, deep=True)
    call = client.messages.calls[0]
    assert call["model"] == "claude-sonnet-5-5" and call["output_config"] == {"effort": "low"} and call["max_tokens"] == 8000
    assert text.startswith("省钱的回答") and "预算上限 180 美元" in text

    bot, client, _, _ = tooled([reply([text_block("正常的回答")])], spend=179.99)
    assert bot.answer("问题", "", None, in_group=False) == "正常的回答"
    assert client.messages.calls[0]["output_config"] == {"effort": "medium"}

    bot, client, _, _ = tooled([reply([text_block("没有预算上限")])], spend=5000.0, ai_monthly_budget_usd=None)
    assert bot.answer("问题", "", None, in_group=False) == "没有预算上限"


def test_sorting_a_message_never_uses_tools_or_the_budget_setting():
    bot, client, _, reports = tooled([reply([text_block("status")], used=usage(200, 5))], spend=999.0)
    assert bot.route_intent("帮我看下手上的单子") == "status"
    [call] = client.messages.calls
    assert call["model"] == "claude-haiku-5-5" and "tools" not in call and call["system"].startswith("你在给")
    purpose, used = reports[0]
    assert purpose == "routing" and abs(used.cost_usd - (200 * 0.10 + 5 * 0.50) / 1_000_000) < 1e-12


def test_briefs_ask_for_news_only_when_the_ai_can_search():
    bot, client, _, _ = tooled([reply([text_block("简报")]), reply([text_block("复盘")])])
    bot.brief("morning", "行情", "账户", None, for_owner=True)
    bot.weekly_review("行情", "交易记录", "权益变化")
    brief_prompt, review_prompt = (call["messages"][-1]["content"] for call in client.messages.calls)
    assert "先搜索过去 24 小时" in brief_prompt and "关键点位（精确数字）" in brief_prompt
    assert "资料库里如果有他自己的交易规则" in review_prompt

    bare, bare_client = analyst(beta=[message()])
    bare.brief("morning", "行情", "账户", None, for_owner=True)
    assert "先搜索" not in bare_client.beta.messages.calls[0]["messages"][-1]["content"]


def test_self_check_of_web_search():
    bot, _, _, _ = tooled([reply([text_block("可用")], used=usage(500, 20, searches=1))])
    bot.check_web_search()  # no exception: a search really ran

    bot, _, _, _ = tooled([reply([text_block("可用")], used=usage(500, 20))])
    with pytest.raises(AnalystUnavailable, match="没有执行搜索"):
        bot.check_web_search()

    rejected = api_error(anthropic.PermissionDeniedError, 403)
    rejected.message = "web search is not enabled"
    bot, _, _, _ = tooled([rejected, rejected, reply([text_block("可用")])])
    with pytest.raises(AnalystUnavailable, match="没有开通联网搜索"):
        bot.check_web_search()

    bot, _, _, _ = tooled([], web_search=False)
    with pytest.raises(AnalystUnavailable, match="WEB_SEARCH=false"):
        bot.check_web_search()


def test_the_instructions_ask_for_short_answers_with_exact_levels():
    for expected in ("精确数字", "失效位", "get_market", "web_search", "search_knowledge", "不要凭印象", "由交易员自己决定", "搜索要精"):
        assert expected in ANALYST_SYSTEM
    assert "教学" not in ANALYST_SYSTEM


def test_the_app_meters_ai_work_and_enforces_the_budget(tmp_path):
    import time

    from bot.app import build_ai
    from bot.journal import Journal

    from .helpers import FakeOkx

    journal = Journal(":memory:")
    bot, resolver, knowledge = build_ai(make_cfg(knowledge_dir=tmp_path, ai_monthly_budget_usd=0.03), FakeOkx(), journal)
    client = FakeClient(
        [
            reply([tool_use("call_1", "get_market", symbol="比特币")], "tool_use", usage(1000, 100)),
            reply([text_block("回答")], used=usage(2000, 500, searches=1)),
            reply([text_block("第二个回答")], used=usage(10, 10)),
        ]
    )
    bot._client = client
    assert resolver.resolve("BTC") == "BTC-USDT-SWAP" and knowledge.summary() == []

    assert bot.answer("比特币怎么看", "", None, in_group=False) == "回答"
    [result] = client.messages.calls[1]["messages"][-1]["content"]
    assert "品种：BTC-USDT-SWAP" in result["content"] and "is_error" not in result  # the real tool ran against OKX
    totals = journal.usage_between(0, time.time() + 10)
    assert totals["jobs"] == 1 and totals["web_searches"] == 1
    assert abs(totals["cost_usd"] - ((3000 * 4 + 600 * 20) / 1_000_000 + 0.01)) < 1e-9

    text = bot.answer("再问一个", "", None, in_group=False)  # 0.034 spent, the budget is 0.03
    assert "预算上限" in text and client.messages.calls[2]["output_config"] == {"effort": "low"}


# ------------------------------------------------- the shape of an answer


def test_the_instructions_let_the_answer_follow_the_question():
    assert "没有固定的格式" in ANALYST_SYSTEM and "篇幅和结构跟着问题走" in ANALYST_SYSTEM
    for old in ("按这个结构写", "**结论**：", "**多头情景**", "**空头情景**"):
        assert old not in ANALYST_SYSTEM  # the one template every answer used to be poured into
    # What the trader asked for from the start still stands.
    for kept in ("精确数字", "不讲教科书", "用词客观", "手机一屏", "由交易员自己决定", "不要写“现在买入”"):
        assert kept in ANALYST_SYSTEM
    assert "结构不变" not in DEEP_NOTE


def test_an_answer_is_asked_for_a_title_and_a_brief_is_not():
    bot, client = analyst(beta=[message(), message()])
    bot.answer("现在黄金怎么看", "最新价：2,650.00", None, in_group=False)
    bot.brief("morning", "行情", None, None, for_owner=False)
    question, brief = (call["messages"][-1]["content"] for call in client.beta.messages.calls)
    assert TITLE_NOTE in question and question.endswith("问题：现在黄金怎么看")
    assert "标题：" not in brief and "不要每天一个模样" in brief


def test_small_talk_is_one_cheap_line():
    bot, client = analyst(plain=[message("不客气。")])
    assert bot.small_talk("谢谢") == "不客气。"
    [call] = client.messages.calls
    assert call["model"] == "claude-haiku-5-5" and call["system"] == SMALL_TALK_SYSTEM and "tools" not in call
    assert call["messages"] == [{"role": "user", "content": "谢谢"}]

    bot, _ = analyst(plain=[api_error(anthropic.RateLimitError, 429)])
    assert bot.small_talk("谢谢") == ""


def test_a_greeting_is_its_own_kind_of_message():
    bot, _ = analyst(plain=[message("hello"), message("help")])
    assert bot.route_intent("今天辛苦了") == "hello"
    assert bot.route_intent("你到底能干什么") == "help"


def test_the_instructions_keep_answers_off_a_template():
    """What made the group answers read like a form: fixed sections, the context notes read back,
    a statement of the bot's own limits, and a pitch for the order phrasing at the end."""
    from bot.analyst import ANALYST_SYSTEM, ROUTER_SYSTEM, SMALL_TALK_SYSTEM
    from bot.track import LEVELS_NOTE

    assert "没有每次都要写的栏目" in ANALYST_SYSTEM and "不要在回答里复述或解释它们" in ANALYST_SYSTEM
    assert "不要在开头声明你的做法" in ANALYST_SYSTEM and "不要主动推荐这些写法" in ANALYST_SYSTEM
    assert "在群里 @交易助手 就是 @你" in ANALYST_SYSTEM and "程序自动回复的卡片" in ANALYST_SYSTEM
    assert "有没有必要平仓" in ROUTER_SYSTEM and "这个我去跟 Claude 商量" in ROUTER_SYSTEM
    assert "表了个态" in SMALL_TALK_SYSTEM
    assert "不要为了写它而在正文里加情景" in LEVELS_NOTE
