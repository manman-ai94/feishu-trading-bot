"""The router decides who gets what. These tests pin down the privacy rules."""
from __future__ import annotations
import pytest

from bot.analyst import AnalystUnavailable
from bot.journal import Journal
from bot.monitor import Monitor
from bot.okx_client import OkxNetworkError
from bot.reports import DISCLAIMER
from bot.router import (
    HELLO_TEXT,
    HELP_TEXT,
    SLOW_SECONDS,
    SLOW_SECONDS_NO_SIGN,
    SLOW_TEXT,
    SLOW_TEXT_DEEP,
    TRADE_OTHER_GROUP_TEXT,
    TRADE_OWNER_ONLY_TEXT,
    WORKING_EMOJI,
    Router,
    is_plain,
    split_title,
)
from bot.service import Reporter
from bot.trading import TradeDesk

from .helpers import BOT, FRIEND, GROUP, OPEN_GROUP, OWNER, Clock, FakeAnalyst, FakeFeishu, FakeOkx, make_cfg, make_message, make_state
from .test_trading import XAU, FakeExchange, FakeResolver


class Bench:
    def __init__(self, state="default", bot_open_id=BOT, analyst_enabled=True, knowledge=None, usage=None, exchange=None, **cfg_overrides):
        self.cfg = make_cfg(**cfg_overrides)
        self.saved: list[tuple[str, str]] = []
        self.restarts = 0
        self.timers: list[dict] = []  # what the router asked to be run later
        self.feishu = FakeFeishu()
        self.okx = FakeOkx(make_state() if state == "default" else state)
        self.analyst = FakeAnalyst(enabled=analyst_enabled)
        self.clock = Clock(1_800_000_000.0)
        journal = self.journal = Journal(":memory:")
        self.reporter = Reporter(self.cfg, self.feishu, self.okx, self.analyst, journal, self.clock)
        self.reporter.monitor = Monitor(self.okx, self.cfg, journal, lambda event: None, self.clock)
        if self.okx.state is not None:
            self.reporter.monitor.poll_once()
        self.exchange = exchange  # with one, trading is on and orders go to it
        self.desk = None
        if exchange is not None or cfg_overrides.get("trading") == "off":
            self.desk = TradeDesk(
                self.cfg, journal, self.okx, exchange, FakeResolver(), self.clock, sleep=lambda seconds: None,
                new_code=lambda: "4821", new_id=lambda: "ftb0001",
            )  # fmt: skip
        self.router = Router(
            self.cfg, self.feishu, self.okx, self.analyst, self.reporter, bot_open_id, self.clock,
            save_setting=lambda name, value: self.saved.append((name, value)), restart=self._restart,
            knowledge=knowledge, usage=usage, trade=self.desk, journal=journal, later=self._later,
        )  # fmt: skip

    def _restart(self):
        self.restarts += 1

    def _later(self, seconds, call):
        timer = dict(seconds=seconds, call=call, cancelled=False)
        self.timers.append(timer)
        return lambda: timer.update(cancelled=True)

    def say(self, text, **kwargs):
        before = len(self.feishu.sent)
        self.router.handle(make_message(text, **kwargs))
        return self.feishu.sent[before:]


LONG_ANSWER = "**方向**：偏多，4H 收在 2,650 上方。\n- 阻力 2,668（4H 前高）\n- 支撑 2,640（日线 EMA50）\n站稳 2,668 看 2,690，跌破 2,640 这个判断失效。"


# ------------------------------------------------------------ who is served


def test_group_message_without_a_mention_of_the_bot_is_ignored():
    bench = Bench()
    assert bench.say("现在黄金怎么看", sender=FRIEND, private=False, mentions=()) == []
    assert bench.say("现在黄金怎么看", sender=FRIEND, private=False, mentions=("ou_someone_else",)) == []
    assert bench.analyst.calls == []


def test_without_a_known_bot_id_any_mention_counts():
    bench = Bench(bot_open_id=None)
    assert bench.say("帮助", sender=OWNER, private=False, mentions=("ou_whoever",))[0].body == HELP_TEXT
    assert bench.say("帮助", sender=OWNER, private=False, mentions=()) == []


def test_stranger_in_private_chat_is_turned_away():
    for bench, expected in ((Bench(), "只响应主人"), (Bench(**OPEN_GROUP), "只在群里回答")):
        [reply] = bench.say("仓位", sender=FRIEND, private=True)
        assert expected in reply.body
        assert " USD" not in reply.everything
        [reply] = bench.say("现在黄金怎么看", sender=FRIEND, private=True)
        assert expected in reply.body
        assert bench.analyst.calls == []


# ------------------------------------- default: the bot obeys the owner only


def test_friend_in_the_group_is_not_served():
    bench = Bench()
    for text in ("仓位", "现在黄金怎么看", "深度 下周怎么看", "帮助"):
        [reply] = bench.say(text, sender=FRIEND, private=False)
        assert reply.how == "reply_text" and "只响应主人" in reply.body
        assert " USD" not in reply.everything and "2,650" not in reply.everything
    assert bench.analyst.calls == []


def test_owner_in_the_group_sees_the_full_account():
    bench = Bench()
    [reply] = bench.say("仓位", sender=OWNER, private=False)
    assert "账户权益" in reply.body and "10,000.00 USD" in reply.body and "数量 2 张" in reply.body
    assert reply.footer is None  # numbers, not advice: no disclaimer under them


def test_owner_question_in_the_group_gives_the_ai_the_account():
    bench = Bench()
    [reply] = bench.say("我的仓位风险大吗", sender=OWNER, private=False)
    [call] = bench.analyst.named("answer")
    assert call["in_group"] is True and "账户权益 10,000.00 USD" in call["account_text"]
    assert reply.footer == DISCLAIMER


def test_owner_can_ask_for_a_deep_answer_in_the_group():
    bench = Bench()
    bench.say("深度 下周怎么看", sender=OWNER, private=False)
    [call] = bench.analyst.named("answer")
    assert call["deep"] is True and call["account_text"] is not None


def test_owner_in_another_group_never_exposes_the_account():
    bench = Bench()
    [reply] = bench.say("仓位", sender=OWNER, private=False, chat_id="oc_other_group")
    assert " USD" not in reply.body and "账户权益" not in reply.body and "张" not in reply.body
    bench.say("现在黄金怎么看", sender=OWNER, private=False, chat_id="oc_other_group")
    [call] = bench.analyst.named("answer")
    assert call["account_text"] is None


def test_unregistered_group_is_turned_away():
    bench = Bench()
    [reply] = bench.say("现在黄金怎么看", sender=FRIEND, private=False, chat_id="oc_other_group")
    assert "还没有登记" in reply.body
    assert bench.analyst.calls == []


def test_no_group_is_served_until_one_is_registered():
    bench = Bench(group_chat_id="")
    [reply] = bench.say("仓位", sender=FRIEND, private=False)
    assert "还没有登记" in reply.body
    [reply] = bench.say("现在黄金怎么看", sender=FRIEND, private=False)
    assert "还没有登记" in reply.body
    assert bench.analyst.calls == []


def test_whoami_works_before_anything_is_configured():
    bench = Bench(owner_open_id="", group_chat_id="")
    [reply] = bench.say("/whoami", sender="ou_new_owner", private=True)
    assert "ou_new_owner" in reply.body and "oc_private" in reply.body
    [reply] = bench.say("/whoami", sender="ou_new_owner", private=False)
    assert GROUP in reply.body
    [reply] = bench.say("仓位", sender="ou_new_owner", private=True)
    assert "/whoami" in reply.body  # told how to register, no account data


def test_the_same_message_delivered_twice_is_answered_once():
    bench = Bench()
    assert len(bench.say("帮助", message_id="om_dup")) == 1
    assert bench.say("帮助", message_id="om_dup") == []


def test_non_text_message_gets_a_hint():
    bench = Bench()
    [reply] = bench.say("", is_text=False)
    assert "只看得懂文字" in reply.body


# ------------------------------------------------------------------- status


def test_owner_in_private_chat_sees_the_full_account():
    bench = Bench()
    [reply] = bench.say("仓位")
    assert reply.how == "reply_markdown"
    assert "账户权益" in reply.body and "10,000.00 USD" in reply.body and "数量 2 张" in reply.body
    assert reply.footer is None
    assert bench.analyst.calls == []  # short keywords need no AI call


# ------------------------------------------ optional: levels-only, open group


def test_friend_in_group_sees_levels_but_no_money():
    bench = Bench(**OPEN_GROUP)
    [reply] = bench.say("仓位", sender=FRIEND, private=False)
    assert "XAU-USDT-SWAP 多单" in reply.body and "2,650.00" in reply.body
    for secret in (" USD", "账户权益", "张", "盈亏", "强平", "杠杆"):
        assert secret not in reply.body
    assert reply.footer == DISCLAIMER


def test_owner_asking_in_the_group_also_gets_the_public_view():
    bench = Bench(group_show_pnl=False)
    [reply] = bench.say("仓位", sender=OWNER, private=False)
    assert " USD" not in reply.body and "账户权益" not in reply.body


def test_group_view_shows_money_only_when_the_owner_opted_in():
    bench = Bench(group_show_pnl=True, group_owner_only=False)
    [reply] = bench.say("仓位", sender=FRIEND, private=False)
    assert "账户权益" in reply.body


def test_status_when_flat_and_when_okx_was_never_read():
    [reply] = Bench(state=make_state([]), **OPEN_GROUP).say("仓位", sender=FRIEND, private=False)
    assert reply.body == "当前没有持仓。"
    [reply] = Bench(state=None).say("仓位")
    assert "还没读到 OKX 账户" in reply.body


# ----------------------------------------------------------------- analysis


def test_group_question_gets_market_only_analysis():
    bench = Bench(**OPEN_GROUP)
    bench.analyst.reply = LONG_ANSWER
    [reply] = bench.say("现在黄金怎么看", sender=FRIEND, private=False)
    [call] = bench.analyst.named("answer")
    assert call["in_group"] is True and call["account_text"] is None and call["deep"] is False
    assert "最新价：2,650.00" in call["market_text"]
    assert reply.title == "行情分析 · XAU-USDT-SWAP" and reply.footer == DISCLAIMER


def test_owner_question_in_a_levels_only_group_never_carries_the_account():
    bench = Bench(group_show_pnl=False)
    bench.say("现在黄金怎么看", sender=OWNER, private=False)
    [call] = bench.analyst.named("answer")
    assert call["account_text"] is None and call["in_group"] is True


def test_owner_private_question_includes_the_account():
    bench = Bench()
    [reply] = bench.say("我的仓位风险大吗")
    [call] = bench.analyst.named("answer")
    assert call["in_group"] is False and "账户权益 10,000.00 USD" in call["account_text"]
    assert reply.footer is None


def test_deep_prefix_is_for_the_owner_only():
    bench = Bench()
    bench.analyst.reply = LONG_ANSWER
    [reply] = bench.say("深度：下周怎么看")
    [call] = bench.analyst.named("answer")
    assert call["deep"] is True and call["question"] == "下周怎么看"
    assert reply.title.startswith("深度分析")

    bench = Bench(**OPEN_GROUP)
    bench.say("深度 下周怎么看", sender=FRIEND, private=False)
    [call] = bench.analyst.named("answer")
    assert call["deep"] is False and call["question"] == "下周怎么看"


def test_ai_router_can_send_a_long_question_to_status_or_help():
    bench = Bench()
    bench.analyst.intent = "status"
    [reply] = bench.say("帮我看一下我现在手上都有什么单子")
    assert "账户权益" in reply.body
    bench.analyst.intent = "help"
    [reply] = bench.say("你这个机器人到底是干什么用的")
    assert reply.body == HELP_TEXT


def test_group_question_limit_applies_to_everyone_but_the_owner():
    bench = Bench(group_daily_question_limit=2, **OPEN_GROUP)
    for index in range(2):
        assert len(bench.say(f"问题{index}：金价怎么看", sender=FRIEND, private=False)) == 1
    [reply] = bench.say("第三个问题：金价怎么看", sender=FRIEND, private=False)
    assert "次数用完了" in reply.body
    assert len(bench.analyst.named("answer")) == 2
    assert bench.say("我自己问：金价怎么看", sender=OWNER, private=False)[0].body == "AI:answer"
    bench.clock.advance(24 * 3600)  # next day the counter starts again
    assert bench.say("新的一天：金价怎么看", sender=FRIEND, private=False)[0].body == "AI:answer"


def test_any_instrument_named_in_the_question_is_fetched_first():
    bench = Bench()  # the watch list holds gold only
    bench.analyst.reply = LONG_ANSWER
    bench.say("比特币现在怎么看")
    bench.say("btc 和 ETH 哪个强")
    bench.say("今天行情如何")
    titles = [item.title for item in bench.feishu.sent if item.how == "reply_markdown"]
    assert titles == ["行情分析 · BTC-USDT-SWAP", "行情分析 · BTC-USDT-SWAP、ETH-USDT-SWAP", "行情分析"]
    first, second, third = (call["market_text"] for call in bench.analyst.named("answer"))
    assert "品种：BTC-USDT-SWAP" in first and "XAU" not in first
    assert "品种：BTC-USDT-SWAP" in second and "品种：ETH-USDT-SWAP" in second
    # Nothing named: the trader's main instrument is offered, marked as possibly beside the point.
    assert "品种：XAU-USDT-SWAP" in third and "没有点名品种" in third


def test_market_data_outage_is_passed_on_so_the_ai_can_say_so():
    bench = Bench()
    bench.okx.market_error = OkxNetworkError("连不上 OKX")
    [reply] = bench.say("现在黄金怎么看")
    [call] = bench.analyst.named("answer")
    assert "品种：XAU-USDT-SWAP" in call["market_text"] and "OKX 行情暂时取不到" in call["market_text"]
    assert reply.body == "AI:answer"


def test_follow_up_questions_carry_the_recent_conversation():
    bench = Bench()
    bench.say("BTC 现在怎么看")
    bench.say("那 ETH 呢")
    bench.say("群里的问题", private=False)  # another chat starts from nothing
    first, second, group = (call["history"] for call in bench.analyst.named("answer"))
    assert first == [] and second == [("BTC 现在怎么看", "AI:answer")] and group == []

    bench.clock.advance(5 * 3600)  # hours later he is still in the same conversation
    bench.say("黄金呢")
    assert len(bench.analyst.named("answer")[-1]["history"]) == 2
    bench.clock.advance(24 * 3600 + 1)  # a day later it is a new one
    bench.say("那白银呢")
    assert bench.analyst.named("answer")[-1]["history"] == []

    for index in range(12):
        bench.say(f"第 {index} 个问题：金价怎么看")
    assert [question for question, _ in bench.analyst.named("answer")[-1]["history"]] == [
        f"第 {index} 个问题：金价怎么看" for index in range(1, 11)
    ]  # the last ten


def test_the_conversation_survives_a_restart_because_the_journal_keeps_it():
    bench = Bench()
    bench.say("BTC 现在怎么看")
    restarted = Router(
        bench.cfg, bench.feishu, bench.okx, bench.analyst, bench.reporter, BOT, bench.clock,
        journal=bench.journal, later=bench._later,
    )  # fmt: skip
    bench.router = restarted
    bench.say("那 ETH 呢")
    assert bench.analyst.named("answer")[-1]["history"] == [("BTC 现在怎么看", "AI:answer")]
    bench.clock.advance(8 * 86400)
    bench.say("新的问题")
    assert bench.journal.recent_dialog("oc_private", 0, 99) == [("新的问题", "AI:answer")]  # a week on, older ones are gone


def test_a_failed_answer_is_not_remembered():
    bench = Bench()
    bench.analyst.error = AnalystUnavailable("连不上 Claude 的接口，请稍后再试。")
    bench.say("BTC 现在怎么看")
    bench.analyst.error = None
    bench.say("再问一次")
    assert bench.analyst.named("answer")[-1]["history"] == []


# -------------------------------------------------------------------- quotes


def test_a_plain_price_request_is_answered_straight_from_okx():
    bench = Bench()
    for text in ("BTC 报价", "btc现在多少钱", "黄金和 ETH 的价格", "/price DOGE"):
        [reply] = bench.say(text)
        assert reply.how == "reply_markdown" and reply.title == "OKX 实时报价" and "2,650.00" in reply.body
    assert "**XAU-USDT-SWAP**" in bench.feishu.sent[2].body and "**ETH-USDT-SWAP**" in bench.feishu.sent[2].body
    assert bench.analyst.calls == []  # no AI involved, so it is instant and free


def test_a_question_about_price_still_goes_to_the_ai():
    bench = Bench()
    for text in ("BTC 价格怎么看", "为什么黄金价格跌了", "报价", "苹果股票多少钱"):
        bench.say(text)
    assert len(bench.analyst.named("answer")) == 4
    assert all(item.title != "OKX 实时报价" for item in bench.feishu.sent)


def test_quote_reports_an_instrument_it_cannot_read():
    bench = Bench()
    bench.say("BTC 报价")  # the list of instruments is known from here on
    bench.okx.market_error = OkxNetworkError("连不上 OKX：Timeout")
    [reply] = bench.say("ETH 报价")
    assert "**ETH-USDT-SWAP** 暂时取不到" in reply.body


def test_friend_in_an_open_group_gets_quotes_with_the_disclaimer():
    bench = Bench(**OPEN_GROUP)
    [reply] = bench.say("BTC 报价", sender=FRIEND, private=False)
    assert reply.title == "OKX 实时报价" and reply.footer == DISCLAIMER and " USD" not in reply.body


def test_ai_outage_is_relayed_in_plain_words():
    bench = Bench()
    bench.analyst.error = AnalystUnavailable("连不上 Claude 的接口，请稍后再试。")
    [reply] = bench.say("现在黄金怎么看")
    assert reply.body == "连不上 Claude 的接口，请稍后再试。"


def test_without_a_claude_key_keywords_still_work():
    bench = Bench(analyst_enabled=False)
    [reply] = bench.say("持仓")
    assert "账户权益" in reply.body
    assert bench.analyst.named("route") == []


def test_unexpected_crash_is_reported_politely():
    bench = Bench()

    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    bench.analyst.answer = boom
    replies = bench.say("现在黄金怎么看")
    assert "出了点问题" in replies[-1].body


# ---------------------------------------------------- the owner's commands


def test_owner_registers_a_group_from_inside_feishu():
    bench = Bench(group_chat_id="")
    [reply] = bench.say("登记本群", sender=FRIEND, private=False, chat_id="oc_new")
    assert "还没有登记" in reply.body and bench.cfg.group_chat_id == ""  # only the owner can

    [reply] = bench.say("登记本群", private=False, chat_id="oc_new")
    assert "已登记本群" in reply.body and "之前登记" not in reply.body
    assert bench.cfg.group_chat_id == "oc_new" and bench.saved == [("GROUP_CHAT_ID", "oc_new")]
    assert bench.reporter.to_group("测试", "内容") and bench.feishu.sent[-1].target == "oc_new"  # reports follow at once
    [reply] = bench.say("仓位", private=False, chat_id="oc_new")
    assert "账户权益" in reply.body  # the registered group gets the full view

    [reply] = bench.say("登记本群", private=False, chat_id="oc_new", message_id="om_again")
    assert "已经登记过了" in reply.body and len(bench.saved) == 1
    [reply] = bench.say("/bindgroup", private=False, chat_id="oc_other")
    assert "之前登记的那个群不再接收" in reply.body and bench.cfg.group_chat_id == "oc_other"
    [reply] = bench.say("登记本群")
    assert "请到要登记的那个群里" in reply.body


def test_a_group_registration_that_cannot_be_saved_is_flagged():
    bench = Bench(group_chat_id="")

    def broken(name, value):
        raise OSError("disk full")

    bench.router._save_setting = broken
    [reply] = bench.say("登记本群", private=False, chat_id="oc_new")
    assert "已登记本群" in reply.body and "重启后需要重新登记" in reply.body and bench.cfg.group_chat_id == "oc_new"


def test_restart_is_for_the_owner_in_private_chat():
    bench = Bench()
    [reply] = bench.say("重启")
    assert "我现在重启" in reply.body and bench.restarts == 1
    [reply] = bench.say("/restart", message_id="om_again")
    assert bench.restarts == 2
    [reply] = bench.say("重启", private=False)
    assert "私聊" in reply.body and bench.restarts == 2
    [reply] = bench.say("重启", sender=FRIEND)
    assert "只响应主人" in reply.body and bench.restarts == 2
    bench.say("重启以后密钥会生效吗")  # a question, not the command
    assert bench.restarts == 2 and len(bench.analyst.named("answer")) == 1

    bench.router._restart = None
    [reply] = bench.say("重启", message_id="om_third")
    assert "不支持在飞书里重启" in reply.body


def test_owner_feeds_the_knowledge_base(tmp_path):
    from bot.knowledge import KnowledgeBase

    kb = KnowledgeBase(tmp_path / "knowledge")
    bench = Bench(knowledge=kb)
    [reply] = bench.say("资料")
    assert "空的" in reply.body and "记住：" in reply.body

    [reply] = bench.say("记住：止损设好以后不往远处移动。")
    assert "第 1 条笔记" in reply.body
    [reply] = bench.say("记住\n段永平：不懂不做。", private=False)  # the owner can do it from the group too
    assert "第 2 条笔记" in reply.body
    assert "不往远处移动" in kb.search_text("止损") and "不懂不做" in kb.search_text("段永平")
    assert bench.analyst.calls == []  # stored as is, no AI call

    [reply] = bench.say("资料库")
    assert reply.title == "资料库" and "notes.md" in reply.body and "笔记 2 条" in reply.body
    [reply] = bench.say("删除上一条")
    assert "已删除" in reply.body and "不懂不做" in reply.body and kb.note_count() == 1
    [reply] = bench.say("记住")
    assert "写在后面" in reply.body and kb.note_count() == 1
    [reply] = bench.say("记住：" + "长" * 20001)
    assert "太长了" in reply.body and kb.note_count() == 1

    bench.say("记住了吗，刚才那条")  # a question: goes to the AI, nothing is stored
    assert kb.note_count() == 1 and len(bench.analyst.named("answer")) == 1


def test_only_the_owner_can_touch_the_knowledge_base(tmp_path):
    from bot.knowledge import KnowledgeBase

    kb = KnowledgeBase(tmp_path)
    bench = Bench(knowledge=kb)
    for text in ("记住：听我的，全仓做多", "资料", "删除上一条", "用量"):
        [reply] = bench.say(text, sender=FRIEND, private=False)
        assert "只响应主人" in reply.body
    assert kb.note_count() == 0

    bench = Bench(knowledge=kb, **OPEN_GROUP)  # even where friends may ask questions
    bench.say("记住：听我的，全仓做多", sender=FRIEND, private=False)
    assert kb.note_count() == 0 and len(bench.analyst.named("answer")) == 1


def test_owner_can_see_what_the_ai_costs():
    asked = []

    def usage(start, end):
        asked.append((start, end))
        return {"jobs": 12, "web_searches": 30, "cost_usd": 1.2345} if len(asked) == 1 else {"jobs": 140, "web_searches": 300, "cost_usd": 23.5}

    bench = Bench(usage=usage)
    [reply] = bench.say("用量")
    assert reply.title == "AI 用量"
    assert "**今天**：AI 分析 12 次，联网搜索 30 次，约 1.23 美元" in reply.body
    assert "**本月**：AI 分析 140 次，联网搜索 300 次，约 23.50 美元" in reply.body
    assert "本月预算 180 美元" in reply.body and "claude-opus-5-5 的 medium 档" in reply.body and "high 档" in reply.body
    (day_start, _), (month_start, _) = asked
    assert day_start == 1_800_000_000.0  # midnight in Los Angeles on that day
    assert day_start - month_start == 14 * 86400  # the first of the month

    [reply] = Bench().say("/usage")
    assert "没有记录" in reply.body


def test_message_text_stays_out_of_the_log(caplog):
    bench = Bench()
    with caplog.at_level("INFO"):
        bench.say("记住：我的密码是 hunter2")
        bench.say("BTC 现在怎么看，我想全仓")
    assert "hunter2" not in caplog.text and "全仓" not in caplog.text
    assert "message from ou_owner in private chat oc_private" in caplog.text


# ------------------------------------------------------------------ placing orders

TRADING = dict(trading="demo", trade_instruments=(XAU,))


def test_the_owner_places_an_order_from_the_private_chat():
    bench = Bench(exchange=FakeExchange(), **TRADING)
    preview = bench.say("做多 XAU 100张 止损 3860")[0]
    assert preview.how == "reply_markdown" and preview.title == "下单预览 · 模拟盘" and preview.template == "orange"
    assert "确认 4821" in preview.body and bench.exchange.changes == []

    done = bench.say("确认 4821")[0]
    assert done.title == "已执行 · 模拟盘" and "已成交 100 张" in done.body
    assert [name for name, _ in bench.exchange.changes] == ["place_order"]
    assert bench.analyst.calls == []  # the AI never sees an order, let alone decides one


def test_the_demo_rehearsal_shows_that_it_is_running():
    from .test_selftest import Demo

    bench = Bench(exchange=Demo(), **TRADING)
    preview = bench.say("模拟盘自测")[0]
    assert preview.title == "模拟盘自测 · 预览" and bench.feishu.reactions == []  # a preview is quick: no sign
    done = bench.say("确认 4821")[0]
    assert done.title == "模拟盘自测 · 通过"
    assert [(what, mark) for what, _, mark in bench.feishu.reactions] == [("added", "Typing"), ("removed", "r1")]
    assert bench.timers[-1]["cancelled"] is True  # the "still running" line was never needed
    assert bench.analyst.calls == []

    # An ordinary order gets no sign: it is answered before one could be seen.
    bench.say("做多 XAU 100张")
    bench.say("确认 4821")
    assert len(bench.feishu.reactions) == 2


def test_the_owner_places_an_order_from_the_registered_group():
    bench = Bench(exchange=FakeExchange(), **TRADING)
    preview = bench.say("做多 XAU 100张 止损 3860", private=False)[0]
    assert preview.title == "下单预览 · 模拟盘" and "确认 4821" in preview.body and "先 @我" in preview.body
    assert bench.exchange.changes == []

    # Someone else in the group types the code he can see: nothing happens.
    friendly = Bench(exchange=FakeExchange(), **TRADING, group_owner_only=False, group_show_pnl=True)
    friendly.say("做多 XAU 100张", private=False)
    assert friendly.say("确认 4821", sender=FRIEND, private=False)[0].body == TRADE_OWNER_ONLY_TEXT
    assert friendly.say("取消", sender=FRIEND, private=False)[0].body != "已取消，没有执行。"
    assert friendly.exchange.changes == []

    done = bench.say("确认 4821", private=False)[0]
    assert done.title == "已执行 · 模拟盘" and [name for name, _ in bench.exchange.changes] == ["place_order"]
    assert bench.analyst.calls == []


def test_a_code_from_the_group_works_in_the_private_chat_and_back():
    bench = Bench(exchange=FakeExchange(), **TRADING)
    bench.say("做多 XAU 100张", private=False)
    assert bench.say("确认 4821")[0].title == "已执行 · 模拟盘"  # one pending order, wherever he answers it


def test_orders_are_not_taken_where_they_should_not_be():
    bench = Bench(exchange=FakeExchange(), **TRADING, group_trading=False)
    assert "GROUP_TRADING" in bench.say("做多 XAU 100张", private=False)[0].body
    hidden = Bench(exchange=FakeExchange(), **TRADING, group_show_pnl=False)
    assert "GROUP_SHOW_PNL" in hidden.say("止损 XAU 3800", private=False)[0].body
    other = Bench(exchange=FakeExchange(), **TRADING)
    assert other.say("做多 XAU 100张", private=False, chat_id="oc_other_group")[0].body == TRADE_OTHER_GROUP_TEXT
    unregistered = Bench(exchange=FakeExchange(), **TRADING, group_chat_id="")
    assert unregistered.say("做多 XAU 100张", private=False)[0].body == TRADE_OTHER_GROUP_TEXT
    for each in (bench, hidden, other, unregistered):
        assert each.exchange.changes == [] and each.analyst.calls == []
        assert each.say("确认 4821")[0].body == "现在没有等待确认的指令。"  # nothing was held for confirmation


def test_words_the_patterns_miss_are_written_out_by_the_ai_and_previewed():
    from bot.models import StopOrder

    from .test_trading import gold

    bench = Bench(exchange=FakeExchange([gold(stops=(StopOrder("a1", 3808.0, 3938.0),))]), **TRADING)
    bench.analyst.command = "止损 XAU 3806"
    [preview] = bench.say("你就根据现在的止损情况，3808挪到3806止损", private=False)
    assert preview.title == "下单预览 · 模拟盘" and "理解为：**止损 XAU 3806**" in preview.body
    [call] = bench.analyst.named("to_command")
    assert "止损 3,808.0" in call["positions_text"]
    assert bench.analyst.named("answer") == [] and bench.exchange.changes == []
    done = bench.say("@交易助手 确认4821执行", private=False)[0]
    assert done.title == "已执行 · 模拟盘" and [name for name, _ in bench.exchange.changes] == ["amend_stop"]


def test_the_ai_is_asked_only_when_the_words_sound_like_an_order():
    bench = Bench(exchange=FakeExchange(), **TRADING)
    bench.say("黄金今晚怎么看")
    bench.say("止损放在哪里比较好？")
    bench.say("做多 XAU 100张")  # read by the patterns: no AI
    assert bench.analyst.named("to_command") == []
    bench.analyst.command = ""  # the AI says it is not an order: the question goes on to be answered
    assert bench.say("我今天的止损被扫了两次，心态有点崩")[-1].body == "AI:answer"
    assert len(bench.analyst.named("to_command")) == 1


def test_the_ai_is_not_asked_where_orders_are_not_taken():
    bench = Bench(exchange=FakeExchange(), **TRADING, group_trading=False)
    bench.analyst.command = "止损 XAU 3806"
    assert "GROUP_TRADING" in bench.say("把我的止损减两点", private=False)[-1].body
    assert bench.analyst.named("to_command") == [] and bench.exchange.changes == []


def test_a_relative_stop_the_patterns_cannot_price_is_priced_from_the_current_stop():
    from bot.models import StopOrder

    from .test_trading import gold

    bench = Bench(exchange=FakeExchange([gold(stops=(StopOrder("a1", 3808.0, 3938.0),))]), **TRADING)
    bench.analyst.command = "止损 XAU 3806"
    [preview] = bench.say("把我的止损减两点", private=False)
    assert "理解为：**止损 XAU 3806**" in preview.body and "止损 3,808.0 → 3,806.0" in preview.body
    # When the AI cannot make it out either, the desk asks for the price and shows where the stop is.
    bench.analyst.command = ""
    [reply] = bench.say("把止损调高一点", private=False)
    assert reply.body.startswith("要写价格") and "止损 3,808.0" in reply.body


def test_nobody_but_the_owner_can_place_an_order():
    bench = Bench(exchange=FakeExchange(), **TRADING)
    assert bench.say("做多 XAU 100张", sender=FRIEND, private=False)[0].body == "我只响应主人的指令。仓位变动和简报我会自动发到群里。"
    assert bench.say("做多 XAU 100张", sender=FRIEND)[0].body != ""  # a stranger's private chat is turned away as always

    open_group = Bench(exchange=FakeExchange(), **TRADING, **OPEN_GROUP)
    for text in ("做多 XAU 100张", "全部平仓", "确认 4821", "停止交易"):
        assert open_group.say(text, sender=FRIEND, private=False)[0].body == TRADE_OWNER_ONLY_TEXT
    assert open_group.exchange.changes == [] and open_group.analyst.calls == []
    # Ordinary questions from the group still reach the AI.
    assert open_group.say("黄金现在怎么看", sender=FRIEND, private=False)[-1].body == "AI:answer"


def test_with_trading_off_an_order_is_refused_and_explained():
    bench = Bench(trading="off")
    reply = bench.say("做多 XAU 100张")[0]
    assert reply.how == "reply_text" and "关着" in reply.body
    assert bench.analyst.calls == []
    assert "下单功能**：已关闭" in bench.say("交易设置")[0].body


def test_a_note_about_trading_is_still_a_note(tmp_path):
    from bot.knowledge import KnowledgeBase

    bench = Bench(exchange=FakeExchange(), knowledge=KnowledgeBase(tmp_path / "kb"), **TRADING)
    assert "记住了" in bench.say("记住：做多 XAU 100张 之前先看美元指数")[0].body
    assert bench.exchange.changes == []


def test_questions_about_trading_go_to_the_ai_as_before():
    bench = Bench(exchange=FakeExchange(), **TRADING)
    assert bench.say("做多 XAU 100张 怎么样")[-1].body == "AI:answer"
    assert bench.exchange.changes == []


def test_help_mentions_trading():
    assert "交易帮助" in HELP_TEXT and "确认码" in HELP_TEXT


# --------------------------------------------------------------- track record


def test_the_owner_can_ask_for_the_track_record():
    bench = Bench()
    now = bench.clock.now
    bench.journal.record_analysis(
        "question", "XAU-USDT-SWAP", "long", 3880.0, [{"side": "long", "trigger": 3900, "target": 3950, "invalid": 3860}], ts=now - 3 * 86400
    )
    bench.journal.record_analysis("question", "BTC-USDT-SWAP", "short", 65000.0, [], ts=now - 50 * 86400)
    [reply] = bench.say("成绩单")
    assert reply.title == "成绩单" and reply.template == "green"
    assert "最近 30 天" in reply.body and "记录了 1 次带点位的分析，共 1 个情景" in reply.body
    [reply] = bench.say("成绩单 90天")
    assert "最近 90 天" in reply.body and "记录了 2 次带点位的分析" in reply.body
    [reply] = bench.say("成绩单 BTC 90")
    assert "最近 90 天 · BTC-USDT-SWAP" in reply.body and "记录了 1 次带点位的分析，共 0 个情景" in reply.body
    assert bench.analyst.calls == []  # read from the journal; the AI is not asked


def test_a_question_about_the_track_record_goes_to_the_ai():
    bench = Bench()
    bench.say("成绩单怎么看")
    assert bench.analyst.named("answer")[0]["question"] == "成绩单怎么看"


def test_the_track_record_can_be_switched_off():
    [reply] = Bench(track_record=False).say("成绩单")
    assert "TRACK_RECORD" in reply.body


def test_the_track_record_is_the_owners_to_ask_for():
    bench = Bench(group_owner_only=False)
    replies = bench.say("成绩单", sender=FRIEND, private=False)
    assert all(reply.title != "成绩单" for reply in replies)


# ------------------------------------------ how an answer is shown and waited for


def test_a_question_gets_an_emoji_while_the_answer_is_written_and_no_waiting_message():
    bench = Bench()
    [reply] = bench.say("现在黄金怎么看", message_id="om_q")  # the answer is the only message
    assert reply.body == "AI:answer"
    assert bench.feishu.reactions == [("added", "om_q", WORKING_EMOJI), ("removed", "om_q", "r1")]
    [timer] = bench.timers
    assert timer["seconds"] == SLOW_SECONDS and timer["cancelled"]  # answered in time: nothing was said


def test_words_come_only_when_the_answer_takes_long():
    bench = Bench()
    seen = []

    def slow_answer(*args, **kwargs):
        bench.timers[-1]["call"]()  # the wait ran past the limit while the AI was still working
        seen.extend(bench.feishu.sent)
        return "AI:answer"

    bench.analyst.answer = slow_answer
    wait, reply = bench.say("现在黄金怎么看", message_id="om_q")
    assert [item.body for item in seen] == [SLOW_TEXT] and wait.target == "om_q"
    assert reply.body == "AI:answer"

    bench.analyst.answer = slow_answer
    wait, _ = bench.say("深度 下周怎么看")
    assert wait.body == SLOW_TEXT_DEEP


def test_without_the_emoji_the_wait_is_acknowledged_sooner():
    bench = Bench()
    bench.feishu.reaction_error = RuntimeError("Feishu error 231021: no permission to operate external chats")
    [reply] = bench.say("现在黄金怎么看")
    assert reply.body == "AI:answer" and bench.feishu.reactions == []
    [timer] = bench.timers
    assert timer["seconds"] == SLOW_SECONDS_NO_SIGN and timer["cancelled"]


def test_the_emoji_comes_off_when_the_ai_fails_too():
    bench = Bench()
    bench.analyst.error = AnalystUnavailable("连不上 Claude 的接口，请稍后再试。")
    bench.say("现在黄金怎么看", message_id="om_q")
    assert bench.feishu.reactions[-1] == ("removed", "om_q", "r1") and bench.timers[0]["cancelled"]

    bench = Bench()
    bench.analyst.answer = lambda *args, **kwargs: 1 / 0
    bench.say("现在黄金怎么看", message_id="om_q")
    assert bench.feishu.reactions[-1] == ("removed", "om_q", "r1") and bench.timers[0]["cancelled"]


def test_commands_and_fixed_replies_carry_no_emoji():
    bench = Bench()
    for text in ("仓位", "帮助", "XAU 报价", "用量", "你好"):
        bench.say(text)
    assert bench.feishu.reactions == [] and bench.timers == []


def test_a_few_sentences_are_sent_as_an_ordinary_message():
    bench = Bench()
    bench.analyst.reply = "美国 CPI 今晚 21:30 公布，市场预期同比 3.1%。"
    [reply] = bench.say("今晚有什么数据")
    assert reply.how == "reply_text" and reply.body == "美国 CPI 今晚 21:30 公布，市场预期同比 3.1%。"

    # In the group the same answer keeps its small print, in a card without a header bar.
    [reply] = bench.say("今晚有什么数据", private=False)
    assert reply.how == "reply_markdown" and reply.title == "" and reply.footer == DISCLAIMER


def test_a_longer_answer_is_a_card_under_the_title_the_ai_gave_it():
    bench = Bench()
    bench.analyst.reply = "标题：黄金：2,650 上方偏多\n\n" + LONG_ANSWER
    [reply] = bench.say("现在黄金怎么看")
    assert reply.how == "reply_markdown" and reply.title == "黄金：2,650 上方偏多" and reply.body == LONG_ANSWER
    assert reply.footer is None

    bench.say("那止损放哪")  # the title is not part of what the AI is shown as its earlier answer
    assert bench.analyst.named("answer")[-1]["history"] == [("现在黄金怎么看", LONG_ANSWER)]

    bench.analyst.reply = LONG_ANSWER  # no title given: the old one stands in
    [reply] = bench.say("黄金再看一次")
    assert reply.title == "行情分析 · XAU-USDT-SWAP"


def test_a_title_on_a_short_answer_is_dropped():
    bench = Bench()
    bench.analyst.reply = "标题：CPI 时间\n今晚 21:30 公布。"
    [reply] = bench.say("CPI 几点")
    assert reply.how == "reply_text" and reply.body == "今晚 21:30 公布。"


def test_split_title_reads_only_a_first_line_that_is_a_title():
    assert split_title("标题：黄金偏多\n\n正文第一行\n第二行") == ("黄金偏多", "正文第一行\n第二行")
    assert split_title("**标题**: 黄金偏多 \n正文") == ("黄金偏多", "正文")
    assert split_title("**标题：黄金偏多**\n正文") == ("黄金偏多", "正文")
    assert split_title("Title: Gold holds 2,650\n正文") == ("Gold holds 2,650", "正文")
    assert split_title("正文里提到标题：这不是标题\n第二行") == ("", "正文里提到标题：这不是标题\n第二行")
    assert split_title("标题：只有这一行") == ("", "只有这一行")
    assert split_title("  普通回答  ") == ("", "普通回答")
    title, body = split_title("标题：" + "很长" * 30 + "\n正文")
    assert len(title) == 30 and title.endswith("…") and body == "正文"


def test_is_plain_means_short_and_free_of_markdown():
    assert is_plain("今晚 21:30 公布美国 CPI。\n预期 3.1%，前值 3.0%。")
    assert is_plain("现在 2,650.5，比 24 小时前高 1.9%。")
    assert not is_plain("**结论**：偏多。")
    assert not is_plain("两条：\n- 阻力 2,668\n- 支撑 2,640")
    assert not is_plain("来源见 [路透](https://example.com/a)")
    assert not is_plain("一句话。" * 60)
    assert not is_plain("一\n二\n三\n四\n五")


# ----------------------------------------------------------------- greetings


def test_a_greeting_gets_one_line_not_the_manual():
    bench = Bench()
    for text in ("你好", "谢谢！", "Hi"):
        [reply] = bench.say(text)
        assert reply.how == "reply_text" and reply.body == "AI:hello"
    assert bench.analyst.named("route") == [] and bench.analyst.named("answer") == []

    bench.analyst.intent = "hello"  # a longer one is recognised by the AI router
    [reply] = bench.say("今天辛苦你了，先这样")
    assert reply.body == "AI:hello" and bench.analyst.named("small_talk")[-1] == {"text": "今天辛苦你了，先这样"}


def test_a_greeting_without_the_ai_gets_a_short_fixed_line():
    bench = Bench(analyst_enabled=False)
    [reply] = bench.say("你好")
    assert reply.body == HELLO_TEXT and bench.analyst.calls == []

    bench = Bench()
    bench.analyst.error = AnalystUnavailable("额度用完了")
    [reply] = bench.say("在吗")
    assert reply.body == HELLO_TEXT


# ------------------------------------------------- answers that fit the question (v0.7.4)


@pytest.mark.parametrize(
    "intent, text",
    [
        ("status", "查看下现在的仓位情况，有没有必要平仓，还是可以再等一等"),
        ("status", "汇报目前持仓情况和风险点"),
        ("status", "我记得我们的账户被设置为止损的你再最后一次确认"),
        ("help", "问题是，群里就只有你一个机器人呀？我要@哪个机器人"),
    ],
)
def test_a_question_is_answered_by_the_ai_even_when_it_looks_like_a_card(intent, text):
    """These got the positions card or the help list in the group, which did not answer them."""
    bench = Bench()
    bench.analyst.intent = intent
    [reply] = bench.say(text, sender=OWNER, private=False)
    assert reply.body == "AI:answer"
    [call] = bench.analyst.named("answer")
    assert "账户权益" in call["account_text"]


def test_the_bots_own_cards_are_in_the_history_without_the_code():
    bench = Bench(exchange=FakeExchange(), **TRADING)
    bench.say("做多 XAU 100张 止损 3860", private=False)
    bench.say("4821", private=False)
    bench.say("刚才成交了吗？止损在哪", private=False)
    history = bench.analyst.named("answer")[-1]["history"]
    assert [question for question, _ in history] == ["做多 XAU 100张 止损 3860", "4821"]
    preview, done = (answer for _, answer in history)
    assert preview.startswith("[程序自动回复的卡片：下单预览 · 模拟盘]") and "4821" not in preview and "确认 ****" in preview
    assert done.startswith("[程序自动回复的卡片：已执行 · 模拟盘]")


def test_the_positions_card_and_small_talk_are_in_the_history():
    bench = Bench()
    bench.say("仓位", private=False)
    bench.analyst.intent = "hello"
    bench.say("这个我去跟 Claude 商量", private=False)
    bench.analyst.intent = "analysis"
    bench.say("那现在止损离现价多远", private=False)
    history = bench.analyst.named("answer")[-1]["history"]
    assert history[0][0] == "仓位" and history[0][1].startswith("[程序自动回复的卡片：当前持仓]")
    assert history[1][0] == "这个我去跟 Claude 商量"


def test_a_friend_looking_at_the_positions_still_gets_the_disclaimer():
    bench = Bench(**OPEN_GROUP)
    assert bench.say("仓位", sender=FRIEND, private=False)[0].footer == DISCLAIMER
    assert Bench().say("仓位", sender=OWNER, private=False)[0].footer is None


def test_a_question_about_closing_is_not_read_as_an_order():
    bench = Bench(exchange=FakeExchange(), **TRADING)
    bench.analyst.command = "平仓 XAU"
    [reply] = bench.say("查看下现在的仓位情况，有没有必要平仓，还是可以再等一等", private=False)
    assert bench.analyst.named("to_command") == [] and reply.body == "AI:answer"
    assert bench.exchange.changes == []


def test_asking_to_look_at_or_analyse_the_orders_reaches_the_ai_with_every_waiting_order():
    from .test_book import MANUAL_LIMIT

    bench = Bench(exchange=FakeExchange(), **TRADING)
    bench.okx.open_orders = [MANUAL_LIMIT]  # placed by hand in OKX, not by the bot
    bench.analyst.command = ""  # the AI's rewrite: looking is not an order
    for text in ("查询我现在的仓位和挂单情况", "帮我分析一下自己的挂单仓位", "看下挂单情况"):
        assert bench.say(text, private=False)[-1].body == "AI:answer", text
    calls = bench.analyst.named("answer")
    assert len(calls) == 3 and all("限价单 买入" in call["account_text"] and "在 OKX 里手动下的" in call["account_text"] for call in calls)
    assert bench.exchange.changes == []
    assert bench.say("挂单")[-1].title != ""  # the bare word still shows the fixed list
