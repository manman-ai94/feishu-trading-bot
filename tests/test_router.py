"""The router decides who gets what. These tests pin down the privacy rules."""
from __future__ import annotations

from bot.analyst import AnalystUnavailable
from bot.journal import Journal
from bot.monitor import Monitor
from bot.okx_client import OkxNetworkError
from bot.reports import DISCLAIMER
from bot.router import HELP_TEXT, Router
from bot.service import Reporter

from .helpers import BOT, FRIEND, GROUP, OPEN_GROUP, OWNER, Clock, FakeAnalyst, FakeFeishu, FakeOkx, make_cfg, make_message, make_state


class Bench:
    def __init__(self, state="default", bot_open_id=BOT, analyst_enabled=True, **cfg_overrides):
        self.cfg = make_cfg(**cfg_overrides)
        self.feishu = FakeFeishu()
        self.okx = FakeOkx(make_state() if state == "default" else state)
        self.analyst = FakeAnalyst(enabled=analyst_enabled)
        self.clock = Clock(1_800_000_000.0)
        journal = Journal(":memory:")
        self.reporter = Reporter(self.cfg, self.feishu, self.okx, self.analyst, journal, self.clock)
        self.reporter.monitor = Monitor(self.okx, self.cfg, journal, lambda event: None, self.clock)
        if self.okx.state is not None:
            self.reporter.monitor.poll_once()
        self.router = Router(self.cfg, self.feishu, self.okx, self.analyst, self.reporter, bot_open_id, self.clock)

    def say(self, text, **kwargs):
        before = len(self.feishu.sent)
        self.router.handle(make_message(text, **kwargs))
        return self.feishu.sent[before:]


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
    assert reply.footer == DISCLAIMER


def test_owner_question_in_the_group_gives_the_ai_the_account():
    bench = Bench()
    _, reply = bench.say("我的仓位风险大吗", sender=OWNER, private=False)
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
    wait, reply = bench.say("现在黄金怎么看", sender=FRIEND, private=False)
    assert "请稍等" in wait.body
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
    _, reply = bench.say("我的仓位风险大吗")
    [call] = bench.analyst.named("answer")
    assert call["in_group"] is False and "账户权益 10,000.00 USD" in call["account_text"]
    assert reply.footer is None


def test_deep_prefix_is_for_the_owner_only():
    bench = Bench()
    _, reply = bench.say("深度：下周怎么看")
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
        assert len(bench.say(f"问题{index}：金价怎么看", sender=FRIEND, private=False)) == 2
    [reply] = bench.say("第三个问题：金价怎么看", sender=FRIEND, private=False)
    assert "次数用完了" in reply.body
    assert len(bench.analyst.named("answer")) == 2
    assert len(bench.say("我自己问：金价怎么看", sender=OWNER, private=False)) == 2
    bench.clock.advance(24 * 3600)  # next day the counter starts again
    assert len(bench.say("新的一天：金价怎么看", sender=FRIEND, private=False)) == 2


def test_instrument_is_picked_from_the_question():
    bench = Bench(watch_instruments=("XAU-USDT-SWAP", "BTC-USDT-SWAP"))
    bench.say("比特币现在怎么看")
    bench.say("btc 呢，深一点讲")
    bench.say("今天行情如何")
    titles = [item.title for item in bench.feishu.sent if item.how == "reply_markdown"]
    assert titles == ["行情分析 · BTC-USDT-SWAP", "行情分析 · BTC-USDT-SWAP", "行情分析 · XAU-USDT-SWAP"]


def test_market_data_outage_is_explained_without_calling_the_ai():
    bench = Bench()
    bench.okx.market_error = OkxNetworkError("连不上 OKX")
    _, reply = bench.say("现在黄金怎么看")
    assert "暂时拿不到 XAU-USDT-SWAP 的行情数据" in reply.body
    assert bench.analyst.named("answer") == []


def test_ai_outage_is_relayed_in_plain_words():
    bench = Bench()
    bench.analyst.error = AnalystUnavailable("连不上 Claude 的接口，请稍后再试。")
    _, reply = bench.say("现在黄金怎么看")
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
