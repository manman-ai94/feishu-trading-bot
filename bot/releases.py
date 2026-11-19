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
    "0.15.0": (
        "账户总风险：所有止损都打到合计亏多少（没止损的按到强平价算，等成交的挂单和突破单也算），开仓预览和“仓位”卡片都写；"
        "超过你定的上限就不执行，只写止损不写数量的自动减到放得下",
        "加仓带止损时，整个仓位合成一张止损；已经有好几张止损的，发“止损 XAU 价格”合成一张",
        "突破单：“涨破 3900 做多 XAU 止损 3870”，价格到了按市价进场，没到一直挂着；“撤单 XAU 3900”按触发价撤",
        "分档挂单：“分档做多 XAU 3850 3830 3810 止损 3790”，最多 5 档；不写数量时几档全成交再打到止损合计亏你定的单笔风险",
        "开仓预览多一行粗估强平价，止损在强平价外面会提醒你",
        "模拟盘自测扩到 13 步，实盘时也能跑：用 .env 里模拟盘的 key，不碰实盘账户",
    ),
    "0.16.0": (
        "定时盯仓：发“每 4 小时盯一下仓位”（几小时你定，最短半小时），持仓期间按时发一张卡片：离止损止盈多远、"
        "浮动盈亏、打到止损亏多少、账户总风险，再加几句点评。只看不动，发“盯仓”看设置，“停止盯仓”关掉，重启不丢",
        "省额度：例行点评用 Sonnet 5.5 低档，一次约 1.5 美分；没止损、离止损或止盈很近、比上次动得大、快公布重要数据时换 Opus 5.5，"
        "一次约 7 美分。发“用量”能看到盯仓本月花了多少",
    ),
    "0.17.0": (
        "可以设第二位主人：在群里发“@交易助手 设为主人 @某人”，他和你权限一样，提问、看账户、下单改单撤单都行；"
        "每张单只认发指令的人自己回的确认码。“取消主人 @某人”拿掉，发“主人”看名单，只有你能设",
        "挂单成交时程序本来就会发“开仓”或“加仓”卡片；AI 以前会说做不到，现在知道了",
    ),
    "0.17.1": (
        "设主人一句话就行：“设置主人 @他”“让他当主人”这类说法都认；也可以写名字（“设置主人 恶霸十七号”），"
        "或者不写人，群里只有一位别人 @过我时就设他。对不上时我列出名单，你回编号。“取消主人”同样不用写名字",
    ),
}


def _key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def news_text(journal, version: str = __version__) -> str:
    """For the start-up card: what changed, until a card for this version has gone out (see `told`)."""
    before = journal.get_setting(RUNNING_VERSION, "")
    if before == version:
        return ""
    # A version skipped on the way (installed straight from an older one) still has its news told.
    skipped = [v for v in sorted(NOTES, key=_key) if _key(before or "0") < _key(v) < _key(version)]
    notes = [note for v in skipped for note in NOTES[v]] + list(NOTES.get(version, ()))
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
