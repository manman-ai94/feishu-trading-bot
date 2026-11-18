"""What each version changed, in the owner's words, for the start-up card of the first run of it.

The card also lists the owner's requests that the version did: the developer
marks them on the server with `python -m bot wishes --done 3,4`, which notes
them with the version.
"""
from __future__ import annotations

from . import __version__, wishes

RUNNING_VERSION = "running_version"  # the version the last start-up card was for

NOTES: dict[str, tuple[str, ...]] = {
    "0.14.1": (
        "每张单都记下来源：AI 情景、AI 改写的说法、算仓位后下的、自己写的指令、OKX 里手动下的。"
        "复盘、周报和“成绩单”按来源算赚了几笔、平均几 R；同一类单满 5 笔以后，开仓预览会写这类单过去的成绩",
        "监控卡住或者和飞书的连接断了超过 5 分钟，我会自己重启（半小时最多一次），上线卡片会写原因",
        "美国 CPI、非农和美联储议息公布前 30 分钟，你有仓位或挂单时提醒你，写清每个仓位离止损还有几个 ATR；只提醒，不动任何单",
        "你的诉求做好了，上线卡片会告诉你；也可以自己发“诉求 3 做好了”或“诉求 3 不做了”标掉一条",
        "装新版本改成一键部署：先备份、跑测试，启动不正常就自动退回上一版",
    ),
}


def news_text(journal, version: str = __version__) -> str:
    """For the start-up card: what changed, until a card for this version has gone out (see `told`)."""
    if journal.get_setting(RUNNING_VERSION, "") == version:
        return ""
    notes = NOTES.get(version, ())
    lines = [f"**已更新到 v{version}**"] + [f"- {note}" for note in notes]
    done = wishes.done_in(journal, version)
    if done:
        lines.append("")
        lines.append("你提的这些诉求，这个版本做好了：")
        lines.extend(f"- #{wish['id']} {wish['text']}" for wish in done)
    return "\n".join(lines) if notes or done else ""


def told(journal, version: str = __version__) -> None:
    """The start-up card of this version went out: later starts do not repeat the news."""
    journal.set_setting(RUNNING_VERSION, version)
