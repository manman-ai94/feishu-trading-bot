"""Where an order came from, and how each kind has done.

Every order the bot sends is tagged with its source, and every closed position
that is reviewed carries the source of the order that opened it. That is the
start of the flywheel: after enough trades the figures say which way of
deciding works for the owner, and which does not.

  plan     the owner said "按刚才的计划 / 按你说的情景" and the AI wrote the order out
  words    the AI wrote the order out from the owner's own words
  sized    a command the owner typed after asking "算仓位" for the same instrument
  own      a command the owner typed in full
  signal   a strategy's signal (kept for the strategy that is not built yet)
  manual   not placed by the bot: the owner opened it in the OKX app
"""
from __future__ import annotations

SOURCE_NAMES = {
    "plan": "AI 情景",
    "words": "AI 改写的说法",
    "sized": "算仓位后下的",
    "own": "自己写的指令",
    "signal": "策略信号",
    "manual": "OKX 里手动下的",
}
PLAN_WORDS = (
    "按刚才", "按你说的", "按你刚才", "按计划", "按这个计划", "按你的计划", "照计划", "情景", "按照", "照着",
    "就这么", "按上面", "按这个做", "按这个来",
)  # fmt: skip
SIZING_SECONDS = 30 * 60  # a command within this long of "算仓位" for the same instrument counts as sized
MIN_TRADES = 5  # fewer closed trades than this say nothing yet, and are not shown in a preview
RECORD_DAYS = 180


def name(source: str) -> str:
    return SOURCE_NAMES.get(source, "来源不明")


def of_words(said: str) -> str:
    """The source of an order the AI wrote out from these words: a plan the owner pointed back to, or his words."""
    return "plan" if any(word in said for word in PLAN_WORDS) else "words"


def _grouped(reviews: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for row in reviews:
        groups.setdefault(row.get("source") or "", []).append(row)
    return groups


def _figures(rows: list[dict]) -> str:
    realized = [r["realized"] for r in rows if r.get("realized") is not None]
    wins = sum(1 for value in realized if value > 0)
    rs = [r["r_multiple"] for r in rows if r.get("r_multiple") is not None]
    text = f"{len(rows)} 笔，赚 {wins} 笔"
    if realized:
        ccy = next((r["ccy"] for r in rows if r.get("ccy")), "")
        text += f"，合计 {sum(realized):+,.2f} {ccy}".rstrip()
    if rs:
        text += f"，平均 {sum(rs) / len(rs):+.2f}R"
    return text


def preview_line(reviews: list[dict], source: str) -> str:
    """One line for an opening preview: how orders of this kind have done. Empty until there are MIN_TRADES with R."""
    rows = [r for r in reviews if (r.get("source") or "") == source and r.get("r_multiple") is not None]
    if not source or len(rows) < MIN_TRADES:
        return ""
    rs = [r["r_multiple"] for r in rows]
    wins = sum(1 for value in rs if value > 0)
    return f"这类单（{name(source)}）最近 {RECORD_DAYS} 天平仓 {len(rows)} 笔，平均 {sum(rs) / len(rs):+.2f}R，赚 {wins} 笔"


def week_line(reviews: list[dict]) -> str:
    """The week's closed positions by where their opening order came from. Empty when none is tagged."""
    groups = _grouped(reviews)
    if set(groups) <= {""}:
        return ""
    parts = [f"{name(source)} {_figures(rows)}" for source, rows in sorted(groups.items(), key=lambda item: -len(item[1])) if source]
    if "" in groups:
        parts.append(f"没有来源记录的 {len(groups[''])} 笔")
    return "按下单来源：" + "；".join(parts)


def record_block(reviews: list[dict], days: int) -> str:
    """For the 成绩单 reply: the closed trades in these days, by source. Empty when there are none."""
    if not reviews:
        return ""
    groups = _grouped(reviews)
    lines = [f"**最近 {days} 天平仓的单，按下单来源**"]
    for source, rows in sorted(groups.items(), key=lambda item: -len(item[1])):
        label = name(source) if source else "没有来源记录（v0.14.1 之前的单）"
        lines.append(f"- {label}：{_figures(rows)}")
    if all(len([r for r in rows if r.get("r_multiple") is not None]) < MIN_TRADES for rows in groups.values()):
        lines.append(f"每类不到 {MIN_TRADES} 笔有 R 的单，还看不出哪种做法更好。")
    return "\n".join(lines)
