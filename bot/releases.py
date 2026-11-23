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
        "设主人一句话就行：“设置主人 @他”“让他当主人”这类说法都认；也可以写名字（“设置主人 张小明”），"
        "或者不写人，群里只有一位别人 @过我时就设他。对不上时我列出名单，你回编号。“取消主人”同样不用写名字",
    ),
    "0.18.0": (
        "被拒的单补一句就能下：没带止损被拒后，接着发“止损 3560”就按原单出预览；超过上限被拒后回“按上限”，按最多能开的数量出预览",
        "一句话两张单（例如“减仓 5 个然后止损设在 3730”）：第一张执行完，接着出第二张的预览，也要回确认码",
        "AI 刚给过写法时，说“帮我设置”“就按这个”“帮我挂”就直接出预览，不用自己再抄一遍",
        "收盘提醒：“1小时收盘跌破 3605 提醒我”“4小时收在 3600 上方提醒我”（15 分钟、30 分钟也行），按收盘价判断；同一条提醒不会重复设",
        "定时撤单和条件撤单：“周五 13:30 撤单 XAU”“30分钟后撤单 XAU”“跌破 3530 撤单 XAU 3588”，设的时候回一次确认码，"
        "到时我自己撤并发卡片；只撤单，不会自己开仓。发“提醒”能看到，“取消提醒 编号”取消",
        "“15 分钟后再看一下”：到时我再看一次行情和仓位，回在原来的对话里（最长 4 小时）；开仓、加仓卡片下面会提示怎么开定时盯仓",
        "时间都写洛杉矶和北京两种；每个人可以发“我用北京时间”，北京时间就写在前面。问“现在几点”“1小时K线几点收盘”由程序直接答，不花 AI 的钱",
        "省钱：“简单说”“为什么”这类几个字的追问用 Sonnet 5.5 低档回答，分析、仓位和下单照旧用 Opus 5.5",
        "没持仓时“仓位”卡片写最近一笔平仓：方向、数量、开平仓价和已实现盈亏",
        "指令连引号一起发也认；被拒的话里不再出现“要亏约 0.00”和空着的品种名",
    ),
    "0.18.1": (
        "写名字设主人更稳：名字对得上好几个人时（比如“小明”和“王小明”），先列出来让你回编号，不会把几个人一起设成主人；"
        "写全名只设这一位",
        "装新版本时标诉求那一步的结果能正常显示了（以前中文输出读不出来）",
    ),
    "0.19.0": (
        "每天的交易计划：纽约 17:05（北京早上 6:05 左右）自动写好发给你和群里，写明每个品种只做多、只做空还是观望和关键位；"
        "没有计划不开新仓，和计划方向相反的开仓直接拦下。发“今天的计划”看，“出今天的计划”现在写，“重做计划：原因”一天能重做一次",
        "交易意见先过清单：大级别方向、靠近关键位、盈亏比至少 2 比 1、30 分钟内没有重要数据、今天没停手，都过了才给进场点位，"
        "不过就说观望和哪一条没过；只问行情照常分析",
        "当天连亏 3 笔，或已实现亏损到开盘时权益的 25%，当天停手：不接受开仓和加仓，平仓、撤单、改止损照常，第二天自动恢复。"
        "每天最多开 3 笔新仓（加仓不算）",
        "移动止损：浮盈到 1R 止损移到保本，之后按最好价减 2 倍 1 小时 ATR（不小于 1R）跟着走，只往有利方向，不用回确认码，"
        "每移一次私聊和群里都发卡片写明为什么；你手动把止损改回去，那一单就不再自动移。发“停止移动止损”关掉",
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
