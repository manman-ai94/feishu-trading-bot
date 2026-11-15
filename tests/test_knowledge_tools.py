"""The owner's knowledge base and the tools the AI can call."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from bot.instruments import InstrumentResolver
from bot.knowledge import KnowledgeBase, split_chunks, tokens
from bot.okx_client import OkxNetworkError
from bot.tools import CLIENT_TOOLS, MAX_CANDLES, Toolbox

from .helpers import FakeOkx

LA = ZoneInfo("America/Los_Angeles")
WHEN = datetime(2026, 11, 15, 15, 0, tzinfo=LA)


# ---------------------------------------------------------- knowledge base


def test_empty_knowledge_base(tmp_path):
    kb = KnowledgeBase(tmp_path / "knowledge")
    assert kb.summary() == [] and kb.note_count() == 0 and kb.search("止损") == []
    assert "空的" in kb.search_text("止损")
    assert kb.remove_last_note() is None
    assert not (tmp_path / "knowledge").exists()  # nothing is created until a note is added


def test_notes_are_kept_and_found(tmp_path):
    kb = KnowledgeBase(tmp_path / "knowledge")
    assert kb.add_note("我的规则：单笔亏损不超过账户的 1%，止损设好以后不往远处移动。", WHEN) == 1
    assert kb.add_note("段永平：买股票就是买公司，不懂不做。Stop doing list 很重要。", WHEN) == 2

    found = kb.search_text("止损可以往远处移动吗")
    assert "不往远处移动" in found and "段永平" not in found and "出处：notes.md" in found
    assert "不懂不做" in kb.search_text("stop doing list")  # English words match whatever the capitals
    assert "不懂不做" in kb.search_text("段永平怎么看买股票")
    nothing = kb.search_text("天气预报")
    assert "没有找到" in nothing and "notes.md" in nothing

    reopened = KnowledgeBase(tmp_path / "knowledge")  # notes survive a restart
    assert reopened.note_count() == 2 and reopened.summary()[0][0] == "notes.md"


def test_the_newest_note_can_be_taken_back(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.add_note("第一条", WHEN)
    kb.add_note("写错的一条", WHEN)
    assert kb.remove_last_note() == "写错的一条"
    assert kb.note_count() == 1 and "写错" not in kb.search_text("写错的一条") and "第一条" in kb.search_text("第一条")
    assert kb.remove_last_note() == "第一条" and kb.remove_last_note() is None


def test_documents_copied_into_the_folder_are_searched(tmp_path):
    (tmp_path / "books").mkdir()
    (tmp_path / "books" / "duan.md").write_text(
        "# 段永平投资问答\n\n## 关于能力圈\n\n不懂不做。看不懂的生意，再便宜也不碰。\n\n## 关于价格\n\n买的是公司未来现金流的折现，短期价格不重要。\n",
        encoding="utf-8",
    )
    (tmp_path / "rules.txt").write_bytes("黄金日内交易：数据公布前十五分钟不开新仓。".encode("gb18030"))  # saved by Windows Notepad
    (tmp_path / "README.md").write_text("这个文件夹怎么用：能力圈 现金流 数据公布", encoding="utf-8")
    (tmp_path / "_draft.md").write_text("草稿：能力圈", encoding="utf-8")
    (tmp_path / "chart.png").write_bytes(b"\x89PNG")
    kb = KnowledgeBase(tmp_path)

    assert [name for name, _ in kb.summary()] == ["books/duan.md", "rules.txt"]
    found = kb.search_text("能力圈是什么意思")
    assert "不懂不做" in found and "出处：books/duan.md · 关于能力圈" in found and "草稿" not in found and "怎么用" not in found
    assert "不开新仓" in kb.search_text("数据公布前能开仓吗")

    (tmp_path / "rules.txt").write_text("黄金日内交易：数据公布前三十分钟不开新仓，公布后等五分钟。", encoding="utf-8")
    assert "三十分钟" in kb.search_text("数据公布前能开仓吗")  # a changed file is read again


def test_long_documents_are_cut_into_pieces():
    text = "# 第一章\n\n" + "这是一段很长的话，讲的是趋势。" * 200 + "\n\n# 第二章\n\n短的一段。"
    chunks = split_chunks("book.md", text)
    assert len(chunks) > 3 and all(len(chunk.text) <= 700 for chunk in chunks)
    assert chunks[0].heading == "第一章" and chunks[-1].heading == "第二章" and chunks[-1].text == "短的一段。"


def test_tokens_cover_english_words_and_chinese_pairs():
    assert tokens("BTC 现在怎么看 EMA20") == ["btc", "ema20", "现在", "在怎", "怎么", "么看"]


# -------------------------------------------------------------------- tools


def toolbox(tmp_path, knowledge=True):
    okx = FakeOkx()
    kb = KnowledgeBase(tmp_path) if knowledge else None
    return Toolbox(okx, InstrumentResolver(okx), kb, LA), okx, kb


def test_tool_definitions_are_stable_and_step_down(tmp_path):
    box, _, _ = toolbox(tmp_path)
    names = [tool["name"] for tool in box.definitions()]
    assert names == ["get_market", "get_candles", "search_knowledge", "web_search", "web_fetch"]
    assert box.definitions() == box.definitions()  # same order every time, so the prompt cache holds
    assert [tool["type"] for tool in box.definitions()[3:]] == ["web_search_20260209", "web_fetch_20260209"]
    assert [tool.get("type") for tool in box.definitions(1)[3:]] == ["web_search_20250305"]
    assert [tool["name"] for tool in box.definitions(2)] == names[:3] == [tool["name"] for tool in box.definitions(9)]
    without, _, _ = toolbox(tmp_path, knowledge=False)
    assert "search_knowledge" not in [tool["name"] for tool in without.definitions()]
    for tool in CLIENT_TOOLS:  # none of them can change anything
        assert tool["name"].startswith(("get_", "search_"))


def test_get_market_works_for_any_listed_instrument(tmp_path):
    box, _, _ = toolbox(tmp_path)
    text, is_error = box.run("get_market", {"symbol": "比特币"})
    assert not is_error
    for expected in ("品种：BTC-USDT-SWAP", "最新价：2,650.00", "资金费率", "持仓量 (open interest)", "[4 小时]", "[日线]"):
        assert expected in text
    text, is_error = box.run("get_market", {"symbol": "ETH-USDT"})
    assert not is_error and "品种：ETH-USDT" in text and "持仓量" not in text  # spot has no open interest


def test_unknown_instrument_points_the_ai_to_web_search(tmp_path):
    box, _, _ = toolbox(tmp_path)
    text, is_error = box.run("get_market", {"symbol": "AAPL"})
    assert is_error and "AAPL" in text and "联网搜索" in text


def test_get_candles(tmp_path):
    box, _, _ = toolbox(tmp_path)
    text, is_error = box.run("get_candles", {"symbol": "BTC", "bar": "4H", "limit": 3})
    lines = text.splitlines()
    assert not is_error and "BTC-USDT-SWAP 的 4H K 线，共 3 根" in lines[0] and "America/Los_Angeles" in lines[0]
    assert len(lines) == 4 and "开 2,600.00 高 2,601.00 低 2,599.00 收 2,600.50" in lines[1]
    text, _ = box.run("get_candles", {"symbol": "BTC", "bar": "1D", "limit": 100000})
    assert len(text.splitlines()) - 1 <= MAX_CANDLES
    assert box.run("get_candles", {"symbol": "BTC", "bar": "7m"})[1]
    assert box.run("get_candles", {"symbol": "BTC", "bar": "1H", "limit": "many"})[1]


def test_search_knowledge_tool(tmp_path):
    box, _, kb = toolbox(tmp_path)
    kb.add_note("止损设好以后不往远处移动。", WHEN)
    text, is_error = box.run("search_knowledge", {"query": "止损"})
    assert not is_error and "不往远处移动" in text
    assert box.run("search_knowledge", {"query": "  "})[1]


def test_tool_failures_come_back_as_errors_not_exceptions(tmp_path):
    box, okx, _ = toolbox(tmp_path)
    assert box.run("get_market", {})[1] and box.run("get_market", "BTC")[1] and box.run("place_order", {"symbol": "BTC"})[1]
    box.run("get_market", {"symbol": "BTC"})  # the list of instruments is known from here on
    okx.market_error = OkxNetworkError("连不上 OKX：ConnectionError")
    text, is_error = box.run("get_market", {"symbol": "BTC"})
    assert is_error and "取不到 BTC-USDT-SWAP" in text
    text, is_error = box.run("get_candles", {"symbol": "BTC", "bar": "1H"})
    assert is_error and "连不上 OKX" in text
