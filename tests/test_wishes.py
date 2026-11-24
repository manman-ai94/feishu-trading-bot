"""The owner's requests channel."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from bot.journal import Journal
import pytest

from bot import releases
from bot.wishes import dump_text, is_list_request, list_markdown, mark, record, status_reply, status_request, take_ai_wish, wish_body

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


@pytest.mark.parametrize(
    "text, expected",
    [
        ("诉求 3 做好了", ([3], "done")),
        ("诉求3已做好", ([3], "done")),
        ("把第3条诉求标成做好了", ([3], "done")),
        ("诉求 3,4 做好了", ([3, 4], "done")),
        ("把诉求 2和3 标为已完成", ([2, 3], "done")),
        ("诉求 #7 完成", ([7], "done")),
        ("诉求 5 不做了", ([5], "dropped")),
        ("第5条诉求不要了", ([5], "dropped")),
        ("诉求 3 怎么样了", None),
        ("诉求清单", None),
        ("诉求：分档挂单做好了", None),
    ],
)
def test_marking_a_request(text, expected):
    assert status_request(text) == expected


def test_marked_requests_and_the_card_of_the_version_that_did_them():
    journal = Journal(":memory:")
    for n, text in enumerate(("分档挂单", "突破单", "按时间提醒")):
        record(journal, text, "owner", "oc_1", NOW + n)
    marked, missing = mark(journal, [1, 2, 9], "done", "v0.14.1")
    assert [w["status"] for w in marked] == ["done", "done"] and missing == [9]
    assert status_reply(marked, missing, "done") == "已标成做好了：#1 分档挂单；#2 突破单\n没有找到：#9。发“诉求清单”看编号。"
    mark(journal, [3], "dropped", "你标的")
    assert "#3" in list_markdown(journal, NOW + 10, TZ) and "（不做了）：你标的" in list_markdown(journal, NOW + 10, TZ)

    news = releases.news_text(journal, "0.14.1")
    assert news.startswith("**已更新到 v0.14.1**\n- 每张单都记下来源")
    assert "你提的这些诉求，这个版本做好了：\n- #1 分档挂单\n- #2 突破单" in news and "按时间提醒" not in news
    assert releases.news_text(journal, "0.14.1") == news  # until the card went out
    releases.told(journal, "0.14.1")
    assert releases.news_text(journal, "0.14.1") == ""
    assert "分档挂单" in releases.news_text(journal, "9.9.9")  # 0.15.0 was skipped on the way: its news is told
    releases.told(journal, "0.15.0")
    assert "定时盯仓" in releases.news_text(journal, "9.9.9")  # and so was 0.16.0
    releases.told(journal, "0.16.0")
    assert "设为主人" in releases.news_text(journal, "9.9.9")  # and 0.17.0
    releases.told(journal, "0.17.0")
    assert "张小明" in releases.news_text(journal, "9.9.9")  # and 0.17.1
    releases.told(journal, "0.17.1")
    assert "按上限" in releases.news_text(journal, "9.9.9")  # and 0.18.0
    releases.told(journal, "0.18.0")
    assert "王小明" in releases.news_text(journal, "9.9.9")  # and 0.18.1
    releases.told(journal, "0.18.1")
    assert "移动止损" in releases.news_text(journal, "9.9.9")  # and 0.19.0
    releases.told(journal, "0.19.0")
    assert "MT5" in releases.news_text(journal, "9.9.9")  # and 0.20.0
    releases.told(journal, "0.20.0")
    assert "不再写成 OKX" in releases.news_text(journal, "9.9.9")  # and 0.20.1
    releases.told(journal, "0.20.1")
    assert "不再等它" in releases.news_text(journal, "9.9.9")  # and 0.20.2
    releases.told(journal, "0.20.2")
    assert "不再跟着断线" in releases.news_text(journal, "9.9.9")  # and 0.20.3
    releases.told(journal, "0.20.3")
    assert releases.news_text(journal, "9.9.9") == ""  # a version with no notes and nothing done says nothing


def test_a_version_installed_over_a_skipped_one_tells_both():
    journal = Journal(":memory:")
    news = releases.news_text(journal, "0.15.0")  # straight from 0.13.0, which never set the running version
    assert news.startswith("**已更新到 v0.15.0**\n- 每张单都记下来源")
    assert news.index("每张单都记下来源") < news.index("账户总风险") and "模拟盘自测扩到 13 步" in news
    releases.told(journal, "0.14.1")
    assert "每张单都记下来源" not in releases.news_text(journal, "0.15.0")
