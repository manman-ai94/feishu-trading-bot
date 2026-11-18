"""The owner's requests channel."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from bot.journal import Journal
from bot.wishes import dump_text, is_list_request, list_markdown, record, take_ai_wish, wish_body

NOW = 1_800_000_000.0
TZ = ZoneInfo("Asia/Shanghai")


def test_what_counts_as_a_request():
    assert wish_body("记个诉求：跌破 3800 提醒我") == "跌破 3800 提醒我"
    assert wish_body("提个建议 简报短一点") == "简报短一点"
    assert wish_body("诉求：分档挂单") == "分档挂单"
    assert wish_body("建议：盘前简报再短一点") == "盘前简报再短一点"
    assert wish_body("记个诉求") == ""
    assert wish_body("建议止损放哪") is None  # asks for advice
    assert wish_body("诉求清单") is None
    assert is_list_request("诉求清单") and is_list_request("诉求") and not is_list_request("诉求清单在哪")


def test_the_ai_closing_line_is_taken_off():
    assert take_ai_wish("做不到。\n诉求：按时间提醒") == ("做不到。", "按时间提醒")
    assert take_ai_wish("做不到。\n**诉求**：按时间提醒") == ("做不到。", "按时间提醒")
    assert take_ai_wish("诉求：只有这一行") == ("", "只有这一行")
    assert take_ai_wish("他的诉求：在正文中间。\n后面还有") == ("他的诉求：在正文中间。\n后面还有", "")
    assert take_ai_wish("没有那一行") == ("没有那一行", "")


def test_the_same_request_is_kept_once_a_day():
    journal = Journal(":memory:")
    assert record(journal, "分档挂单", "owner", "oc_1", NOW) == 1
    assert record(journal, "分档挂单", "auto", "oc_1", NOW + 3600) is None
    assert record(journal, "分档挂单", "auto", "oc_1", NOW + 90000) == 2
    assert record(journal, "  ", "owner", "oc_1", NOW) is None


def test_the_list_and_the_dump():
    journal = Journal(":memory:")
    assert "最近 30 天没有记下的诉求" in list_markdown(journal, NOW, TZ)
    record(journal, "分档挂单", "owner", "oc_private", NOW - 3600)
    record(journal, "按时间提醒", "auto", "oc_group", NOW - 1800)
    journal.set_wish_status(1, "done", "v0.14.0")
    listing = list_markdown(journal, NOW, TZ)
    assert "#1" in listing and "分档挂单（你记的）（已做好）：v0.14.0" in listing
    assert "按时间提醒（我没做到，自动记的）" in listing
    journal.record_dialog("oc_group", "跌破3800提醒我", "好，XAU 跌破 3,800 时提醒你", NOW - 600)
    journal.record_dialog("oc_private", "按照这个计划开单", "下单预览\n数量 12 XAU", NOW - 300)
    dump = dump_text(journal, NOW, TZ, 1, with_dialog=True, group_chat_id="oc_group")
    assert "[owner/done] 分档挂单" in dump and "[auto/open] 按时间提醒" in dump
    assert "群] 问：跌破3800提醒我" in dump and "私聊] 问：按照这个计划开单" in dump and "下单预览 / 数量 12 XAU" in dump
    assert "对话" not in dump_text(journal, NOW, TZ, 1, with_dialog=False)
