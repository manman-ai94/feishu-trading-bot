"""Timed position checks: "每 4 小时盯一下仓位" (watch.py)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot.analyst import AnalystUnavailable
from bot.journal import Journal
from bot.models import StopOrder
from bot.watch import (
    DEFAULT_HOURS,
    SETTING,
    PositionWatch,
    WatchCommand,
    cost_usd,
    model_name,
    read_command,
)

from .helpers import Clock, FakeAnalyst, FakeOkx, make_cfg, make_position, make_state

CHAT = "oc_private_chat"
NOW = 1_800_000_000.0  # 2027-01-15 08:00 UTC: no data release within hours of it


@pytest.mark.parametrize(
    "text, hours",
    [
        ("每 4 小时盯一下仓位", 4),
        ("每4个小时帮我盯一下仓位", 4),
        ("每两个小时看一下仓位", 2),
        ("每隔3小时检查一下持仓", 3),
        ("每小时盯一次仓", 1),
        ("每半小时盯一下", 0.5),
        ("每 30 分钟盯一下仓位", 0.5),
        ("每一个半小时盯仓", 1.5),
        ("每十二小时盯一下持仓", 12),
        ("4小时一次盯仓", 4),
        ("每 4h 盯仓", 4),
        ("每天盯一下仓位", 24),
        ("能不能每 4 小时帮我盯一下仓位？", 4),
    ],
)
def test_reads_how_often_to_watch(text, hours):
    assert read_command(text) == WatchCommand("set", hours)


@pytest.mark.parametrize("text", ["帮我盯一下仓位", "帮我盯着仓位", "盯一下我的仓位", "你帮我盯仓"])
def test_without_an_interval_the_usual_one_is_used(text):
    assert read_command(text) == WatchCommand("set", None)


@pytest.mark.parametrize("text", ["盯仓", "盯仓状态", "还在盯吗？", "盯盘"])
def test_asks_how_it_is_set(text):
    assert read_command(text) == WatchCommand("status")


@pytest.mark.parametrize("text", ["停止盯仓", "不用盯了", "别盯了", "关掉盯仓", "盯仓关掉", "暂停盯仓"])
def test_turns_it_off(text):
    assert read_command(text) == WatchCommand("stop")


def test_an_interval_too_short_or_too_long_is_explained():
    short = read_command("每 10 分钟盯一下仓位")
    assert short.kind == "set" and "最短每 30 分钟" in short.bad
    assert "最长每 24 小时" in read_command("每 48 小时盯一下仓位").bad


@pytest.mark.parametrize(
    "text",
    [
        "盯仓会不会很费额度",
        "每 4 小时盯仓要花多少钱",
        "为什么每4小时盯仓做不到",
        "跌破 3800 提醒我",
        "仓位",
        "止损 XAU 3768",
        "每4小时提醒我喝水",
        "黄金怎么看",
        "帮我看一下仓位",
        "看看持仓",
        "我持仓的时候，跟他说每 4 个小时或者多久帮我盯一下仓位，他做不到，这个应该怎么解决？如果这样盯仓位的话，会不会很费额度？",
    ],
)
def test_other_messages_are_left_alone(text):
    assert read_command(text) is None


def test_model_names_and_what_a_comment_costs():
    assert model_name("claude-sonnet-5-5") == "Sonnet 5.5" and model_name("claude-opus-5-5") == "Opus 5.5"
    assert model_name("my-model") == "my-model"
    assert cost_usd("claude-haiku-5-5", False) < 0.001 < cost_usd("claude-sonnet-5-5", False) < 0.03
    assert 0.03 < cost_usd("claude-opus-5-5", True) < 0.1


# --------------------------------------------------------------------- watch


def gold(**overrides):
    values = dict(
        avg_px=3788.0, mark_px=3801.2, upl=158.4, coins=12.0, coin_ccy="XAU", liq_px=3650.0, qty=12.0,
        stops=(StopOrder("a1", sl_trigger_px=3768.0, tp_trigger_px=3900.0),),
    )  # fmt: skip
    values.update(overrides)
    return make_position(**values)


class Desk:
    def __init__(self, positions=None, clock=NOW, **cfg_overrides):
        self.cfg = make_cfg(**cfg_overrides)
        self.journal = Journal(":memory:")
        self.okx = FakeOkx()
        self.analyst = FakeAnalyst()
        self.clock = Clock(clock)
        self.state = make_state([gold()] if positions is None else positions, total_eq=1773.0)
        self.cards: list[dict] = []
        self.send_error: Exception | None = None
        self.watch = self.make()

    def make(self) -> PositionWatch:
        return PositionWatch(
            self.journal, self.okx, self.analyst, self.cfg, lambda: self.state,
            lambda state: ["**账户总风险（所有止损都打到）**：约 240 USDT，占权益 13.5%（你定的上限 25%）"], self._send, self.clock,
        )  # fmt: skip

    def _send(self, chat_id, private, title, markdown, template):
        if self.send_error:
            raise self.send_error
        self.cards.append(dict(chat_id=chat_id, private=private, title=title, body=markdown, template=template))

    def ask(self, text, private=True, shows=True):
        reply = self.watch.handle(text, CHAT if private else "oc_group", private, shows)
        if reply is not None and reply.then is not None:
            reply.then()
        return reply


def test_setting_it_sends_the_first_card_at_once():
    desk = Desk()
    reply = desk.ask("每 4 小时盯一下仓位")
    assert "每 4 小时盯一次仓位" in reply.text and "第一张现在就发" in reply.text and "只看不动" in reply.text
    assert "Sonnet 5.5（low 档）" in reply.text and "Opus 5.5" in reply.text and "美分" in reply.text
    [card] = desk.cards
    assert card["chat_id"] == CHAT and card["private"] and card["template"] == "turquoise"
    assert card["title"] == "盯仓：XAU 多单 浮动 +158.40 USDT"
    body = card["body"]
    assert "**XAU-USDT-SWAP 多单 12 XAU**，开仓均价 3,788.00，标记价 3,801.20" in body
    assert "- 浮动盈亏 +158.40 USDT（占权益 +8.9%）" in body
    assert "- 止损 3,768.00：离标记价 33.20（0.87%，约" in body
    assert "打到止损这笔亏约 240.00 USDT，占权益 13.5%" in body
    assert "- 止盈 3,900.00：离标记价 98.80（2.60%" in body and "到止盈这笔赚约 1,344.00 USDT" in body
    assert "- 预估强平价 3,650.00，离标记价 151.20（3.98%）" in body
    assert "**账户权益**：1,773.00 USD" in body and "账户总风险" in body
    assert "**点评**（Sonnet 5.5）：AI:watch" in body and "下一张大约" in body and "没有碰任何单" in body
    assert "比上次盯仓" not in body  # nothing to compare the first card with
    [call] = desk.analyst.named("watch")
    assert call["close"] is False
    assert "【程序算好、已经显示在卡片上的数字】" in call["facts"] and "【OKX 实时行情数据】" in call["facts"]
    assert "【最近的 1 小时 K 线】" in call["facts"] and "单笔风险：权益的 1%" in call["facts"]
    assert "**" not in call["facts"].split("【OKX")[0]


def test_cards_come_at_the_interval_and_compare_with_the_last_one():
    desk = Desk()
    desk.ask("每 4 小时盯一下仓位")
    desk.clock.advance(3 * 3600)
    assert desk.watch.check_once() is False and len(desk.cards) == 1
    desk.clock.advance(3600)
    desk.state = make_state([gold(mark_px=3803.2, upl=182.4)], total_eq=1797.0)
    assert desk.watch.check_once() is True
    body = desk.cards[-1]["body"]
    assert "- 比上次盯仓（4 小时前）的标记价 3,801.20 涨了 2.00（约 1.0 个 1 小时 ATR）" in body
    assert desk.cards[-1]["template"] == "turquoise"


def test_a_restart_keeps_the_setting():
    desk = Desk()
    desk.ask("每 2 小时盯一下仓位")
    again = desk.make()  # what the bot builds after a restart, on the same database
    desk.clock.advance(2 * 3600)
    assert again.check_once() is True and len(desk.cards) == 2
    assert "每 2 小时一次" in again.status_text()


def test_nothing_is_sent_without_a_position():
    desk = Desk(positions=[])
    reply = desk.ask("每 4 小时盯一下仓位")
    assert "现在没有持仓，有持仓了才发" in reply.text and reply.then is None
    desk.clock.advance(5 * 3600)
    assert desk.watch.check_once() is False and desk.cards == []
    desk.state = make_state([gold()])  # a position opens: the first card comes an interval later, not at once
    desk.clock.advance(60)
    assert desk.watch.check_once() is False
    desk.clock.advance(4 * 3600)
    assert desk.watch.check_once() is True


def test_stopping_it():
    desk = Desk()
    desk.ask("每 4 小时盯一下仓位")
    reply = desk.ask("停止盯仓")
    assert "停了" in reply.text and "不花 AI 的钱" in reply.text
    assert desk.journal.get_setting(SETTING, "") == ""
    desk.clock.advance(5 * 3600)
    assert desk.watch.check_once() is False and len(desk.cards) == 1
    assert "本来就没开" in desk.ask("别盯了").text
    assert "现在没开定时盯仓" in desk.ask("盯仓").text


def test_status_says_when_the_next_card_comes():
    desk = Desk()
    desk.ask("每 4 小时盯一下仓位")
    text = desk.ask("盯仓").text
    local = datetime.fromtimestamp(NOW + 4 * 3600, tz=desk.cfg.tz).strftime("%H:%M")
    assert f"每 4 小时一次，发在私聊。下一张大约 {local}" in text and "停止盯仓" in text


def test_without_an_interval_it_watches_every_four_hours():
    desk = Desk()
    reply = desk.ask("帮我盯着仓位")
    assert f"每 {DEFAULT_HOURS:g} 小时" in reply.text and "你没写间隔" in reply.text


def test_a_group_that_does_not_show_the_account_gets_nothing():
    desk = Desk()
    reply = desk.ask("每 4 小时盯一下仓位", private=False, shows=False)
    assert "卡片私聊发给你" in reply.text
    [card] = desk.cards
    assert card["chat_id"] == "" and not card["private"]


@pytest.mark.parametrize(
    "position, reason",
    [
        (gold(mark_px=3769.5, upl=-222.0), "离止损只剩 1.50"),
        (gold(stops=()), "没有止损"),
        (gold(mark_px=3899.5), "离止盈不到半个"),
        (gold(mark_px=3765.0), "已经越过止损"),
    ],
)
def test_the_stronger_model_writes_when_a_position_wants_a_closer_look(position, reason):
    desk = Desk(positions=[position])
    desk.ask("每 4 小时盯一下仓位")
    [card] = desk.cards
    assert card["title"].startswith("盯仓（要留意）") and card["template"] == "orange"
    assert "**点评**（Opus 5.5）" in card["body"]
    [call] = desk.analyst.named("watch")
    assert call["close"] is True and reason in call["facts"]


def test_a_big_move_since_the_last_card_wants_a_closer_look():
    desk = Desk()
    desk.ask("每 4 小时盯一下仓位")
    desk.clock.advance(4 * 3600)
    desk.state = make_state([gold(mark_px=3811.2)])
    desk.watch.check_once()
    assert desk.analyst.named("watch")[-1]["close"] is True
    assert "比上次盯仓（4 小时前）涨了 5.0 个 1 小时 ATR" in desk.analyst.named("watch")[-1]["facts"]


def test_a_data_release_before_the_next_card_wants_a_closer_look():
    cpi = datetime(2026, 12, 10, 13, 30, tzinfo=timezone.utc).timestamp()  # 08:30 in New York
    desk = Desk(clock=cpi - 2 * 3600)
    desk.ask("每 4 小时盯一下仓位")
    [card] = desk.cards
    assert "美国 CPI（通胀）在 05:30 公布，还有约 2.0 小时" in card["body"]
    assert desk.analyst.named("watch")[0]["close"] is True


def test_unread_stops_are_not_called_missing():
    desk = Desk(positions=[gold(stops=())])
    desk.state.stops_known = False
    desk.ask("每 4 小时盯一下仓位")
    assert "止损这次没读到" in desk.cards[0]["body"] and "没有止损" not in desk.cards[0]["body"]
    assert desk.analyst.named("watch")[0]["close"] is False


def test_the_card_goes_out_without_a_comment_when_the_ai_fails():
    desk = Desk()
    desk.analyst.error = AnalystUnavailable("Claude 那边提示请求太频繁或额度用完了，请稍后再试。")
    desk.ask("每 4 小时盯一下仓位")
    [card] = desk.cards
    assert "这次 AI 点评没写出来：Claude 那边提示请求太频繁" in card["body"] and "浮动盈亏" in card["body"]


def test_a_card_that_could_not_be_sent_is_tried_again_in_five_minutes():
    desk = Desk()
    desk.send_error = RuntimeError("feishu down")
    desk.ask("每 4 小时盯一下仓位")
    assert desk.cards == []
    desk.send_error = None
    desk.clock.advance(4 * 60)
    assert desk.watch.check_once() is False
    desk.clock.advance(60)
    assert desk.watch.check_once() is True


def test_a_card_is_kept_with_the_chat_so_a_follow_up_makes_sense():
    desk = Desk()
    desk.ask("每 4 小时盯一下仓位")
    [(question, answer)] = desk.journal.recent_dialog(CHAT, NOW - 60, 5)
    assert question == "（定时盯仓）" and answer.startswith("[程序按设定定时发的盯仓卡片：盯仓：XAU 多单")
    assert "**" not in answer


def test_a_broken_setting_counts_as_off():
    desk = Desk()
    desk.journal.set_setting(SETTING, "{not json")
    assert desk.watch.watching() is None and desk.watch.check_once() is False
