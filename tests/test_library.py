"""The reference library that ships with the code: public-source write-ups the AI can cite.

Every piece the search hands to the AI has to carry its own source line, so the
files are written one short section per topic; these tests keep them that way.
"""
from pathlib import Path

import pytest

from bot.analyst import ANALYST_SYSTEM
from bot.knowledge import CHUNK_CHARS, KnowledgeBase, split_chunks

LIBRARY = Path(__file__).resolve().parent.parent / "knowledge" / "library"
SEARCHED = sorted(path for path in LIBRARY.glob("*.md") if not path.name.startswith("_"))


def test_the_library_ships_with_the_code():
    assert [path.name for path in SEARCHED] == ["duan-yongping-principles.md", "gold-drivers-reference.md"]
    assert (LIBRARY / "_sources-duan-yongping.md").exists() and (LIBRARY / "_sources-gold-drivers.md").exists()


@pytest.mark.parametrize("path", SEARCHED, ids=lambda path: path.name)
def test_every_piece_carries_its_source(path):
    chunks = split_chunks(path.name, path.read_text(encoding="utf-8"))
    assert len(chunks) >= 10
    for chunk in chunks:
        assert chunk.heading, chunk.text[:40]
        assert len(chunk.text) <= CHUNK_CHARS, chunk.heading  # one section, one piece
        assert "\n出处：" in chunk.text, chunk.heading
        assert "http" not in chunk.text, chunk.heading  # the addresses live in the _sources file


def test_library_files_are_searched_and_the_source_list_is_not():
    kb = KnowledgeBase(LIBRARY.parent)
    names = [name for name, _ in kb.summary()]
    assert "library/duan-yongping-principles.md" in names
    assert not any("_sources" in name for name in names)


@pytest.mark.parametrize(
    "query, heading",
    [
        ("段永平怎么看杠杆", "杠杆和借钱"),
        ("做空", "做空，和他做空百度的教训"),
        ("止损", "发现错了马上停"),
        ("黄金", "对黄金的看法"),
        ("看K线做短线", "看图看线和短线交易"),
        ("期权 卖put", "期权和卖 put 怎么用"),
        ("仓位 回撤", "仓位和回撤"),
    ],
)
def test_a_question_finds_the_matching_section(query, heading):
    found = KnowledgeBase(LIBRARY).search(query, limit=2)
    assert any(heading in chunk.heading for chunk in found), [chunk.heading for chunk in found]


def test_what_the_ai_gets_names_the_file_the_section_and_the_source():
    text = KnowledgeBase(LIBRARY).search_text("黄金")
    assert "【出处：duan-yongping-principles.md · 段永平：对黄金的看法】" in text
    assert "二手" in text and "没有查到他公开给过黄金的买卖点位" in text


def test_the_write_up_says_what_it_can_and_cannot_be_used_for():
    text = (LIBRARY / "duan-yongping-principles.md").read_text(encoding="utf-8")
    for expected in ("都是转述，不是原话", "不能当作任何价位的依据", "不要替他对眼下的行情表态"):
        assert expected in text


def test_the_ai_is_told_how_to_use_investor_material():
    for expected in ("投资人的观点只用来对照纪律和风险", "不是价位的依据", "不要替他们对眼下的行情表态"):
        assert expected in ANALYST_SYSTEM
