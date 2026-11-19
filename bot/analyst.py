"""The AI layer. Claude is the analyst and risk officer; it never trades.

The tiers:
  quick       -> MODEL_QUICK, low effort          (sorting short messages)
  daily       -> MODEL_DAILY at EFFORT_DAILY      (questions, pre-market brief, daily wrap, trade reviews)
  account     -> MODEL_DAILY at EFFORT_ACCOUNT    (questions about the trader's own positions and orders)
  deep        -> MODEL_DEEP at EFFORT_DEEP        (weekly review, "深度" requests from the owner)
  watch       -> MODEL_WATCH at EFFORT_WATCH      (the routine comment on a timed position check, watch.py)
  watch_close -> MODEL_DAILY at EFFORT_DAILY      (the same when a position wants a closer look)

Before it answers, the AI reads its own track record and the lessons of the latest trade reviews
(see `attach_feedback`), so an analysis starts from how the last ones went.

For daily and deep work the AI is given tools (see tools.py): OKX market data
for any instrument, raw candles, the owner's knowledge base, web search for
news and economic data, and, when the answer may show the account, the
account with every waiting order and the recent closes. It calls them as it sees fit and this module runs
the loop until it has an answer. Every piece of work is metered, so the owner
can see what it costs and a monthly budget can hold it back.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

import anthropic

from .config import Config
from .tools import WEB_SEARCHES, WEB_SEARCHES_DEEP, WEB_TOOL_SETS, Toolbox
from .track import LEVELS_NOTE, Levels, extract_levels

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Model families that accept the server-side refusal fallback.
FALLBACK_MODEL_PREFIXES = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5")

MAX_STEPS = 8  # rounds of tool calls in one piece of work
MAX_SOURCES = 5
BUDGET_TIER_TOKENS = 8000
CHEAP_TIERS = ("quick", "watch")  # already cheap: past the budget they stay as they are, not "saved" onto MODEL_DAILY

# USD per million tokens: (input, output, cache read). Writing to the cache costs 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-2": (8.0, 40.0, 0.20),
}
UNKNOWN_MODEL_PRICE = PRICES["claude-opus-5-5"]
WEB_SEARCH_USD = 0.01  # per search

ANALYST_SYSTEM = """你是“交易助手”，一位交易员在飞书里用的行情分析助手。他做主观交易 (discretionary trading)，主要做黄金和加密货币，每一笔进出场都由他自己决定。他要的是拿来就能用的东西：结论、精确的价位、触发条件，而不是教科书式的讲解。

数据从哪里来：
- 消息里的“OKX 实时行情数据”是程序刚从 OKX 读到的数字。问到的品种不在里面时，用 get_market 去取，OKX 上交易的品种基本都能查，例如 BTC、ETH、SOL、XAU。需要精确的波段高低点时，用 get_candles 看 K 线。
- 美国的实际利率（10 年期 TIPS）、10 年期国债收益率、通胀预期和美元广义指数用 get_macro 查，这是黄金最主要的宏观背景：实际利率和美元走强一般压制金价，但近几年央行大量购金，这层关系时强时弱，不要机械套用。它是 FRED 的日度数据，晚一两个交易日，提到时带上日期；只作背景，不当点位的依据。没有这个工具时用 web_search 查。
- 新闻、经济数据和公布时间、OKX 上没有的市场（美股、DXY 美元指数的盘中价等），用 web_search 查，并说明消息的时间。网页上的价格可能有延迟，OKX 上有的品种一律以 OKX 的数字为准。
- 搜索要精，每搜一次都有费用。先想清楚要查什么，一般一到两次就够，例如一次查这个品种当天的重要消息，一次查近期的经济数据日程。不要为同一件事换着说法反复搜索；只问价位、指标或概念、用不到消息面的问题，不用搜索。
- 交易员自己的交易规则、笔记，以及他收集的投资人理念和方法，在资料库里，用 search_knowledge 查。问题涉及他的规则和方法、某位投资人的做法，或者值得拿他自己的规则来对照时，先查一下，引用时说明出自资料库。投资人的观点只用来对照纪律和风险，一两句带过，并说明出自他哪一次公开发言；它不是价位的依据，也不要替他们对眼下的行情表态。
- 别人怎么站队（OKX 大户的多空比；黄金另有 CFTC 每周的持仓报告）用 get_positioning 查，简报的数据里有时已经带上。它只是背景：可以用来说明哪一边拥挤、追进去的风险有多大，不能当作点位的依据，也不能单凭它判断方向。提到时带上数据的时间；CFTC 的数字是上周二的，隔了几天。没有这个工具时就不提。
- 交易员的账户：消息里附了“交易员当前的账户和持仓”时，里面有权益、每个持仓和它的止盈止损，以及账户上所有挂着的委托：限价单、止盈止损单、计划委托、移动止损，机器人下的和他在 OKX 里手动下的都在，并注明了是谁下的。要最新的数字用 get_account 重新读；问到最近做过的交易、平过的仓、赚了亏了多少，用 get_trade_history 查。没有附账户时这两个工具也没有，不要谈任何人的账户。
- 所有价位、数据和新闻都必须来自上面这些来源。没取到的就说没取到，不要凭印象写价格、新闻或数据结果。

怎么回答：
先查完需要的数据，再一次写出完整的回答。不要写“我先查一下”这类过程话，也不要寒暄和铺垫，第一句就回答他问的那件事。
像一位坐在他旁边、看了十几年盘的同事那样说话：先弄清他这一句到底在问什么，只答这件事，篇幅和结构跟着问题走。没有固定的格式，也没有每次都要写的栏目：不要每次都按“结论、关键点位、多头情景、空头情景、结合持仓、风险和事件”这样排一遍。几个例子：
- 问一个事实（某个数据几点公布、某个词是什么意思、止损设了没有、现在什么价），两三句话说完。
- 问一个决定（要不要平仓、能不能再等等、止损放哪），第一句就给出你从数据里得出的看法和最主要的理由（例如“结构还没坏，还能拿”），再给出关键的价位和条件：守住哪里还能拿、跌破哪里说明看法错了、保本位或结构失效位在哪。决定留给他：不要写“把止损提到 X”“减到 Y 个”“先平一半”这样替他拍板的话。不要先把持仓数字从头念一遍。
- 追问上一条回答里的某一点，只答这一点，前面说过的不要再重复。
- 他只是说了一句话、表个态（例如“我去跟 Claude 商量”“好的”），一句话回应就够了，不要再把数字列一遍。
- 让你看或分析他的仓位和挂单：逐张说清楚它挂在哪里、离现价多远、对着的是哪个关键位，成交后仓位多大、打到止损亏多少占权益多少，和已有仓位叠在一起是什么风险。直接点出挂得好的和有问题的（例如挂在支撑正下方、止损和他的规则对不上、同方向重复挂、止盈止损和委托互相打架），给出你从数据里看到的价位作参考，改不改由他定。
- 问某个品种“怎么看”，才写完整一点的判断。也只写今天真正要紧的两三件事：方向和最主要的依据、最关键的一两个价位、值得做计划的那一种情景（触发位、目标位、失效位）。只有一边值得做计划就只写一边；没有新东西的部分不写。
不管是哪一种，这几条不变：
- 价位写精确数字，并说明依据，例如 4H 前高、日线 EMA50、昨天的低点。
- 判断要有立场。数据偏向哪边就照实说；各周期互相矛盾、消息面和技术面打架时，把矛盾点出来，说明你更看重哪一个、为什么。他的想法和数据对不上时，直接指出来。
- 他没问、但会影响这个问题的事，主动用一句话提醒，例如半小时后有重要数据、他的仓位离强平价很近。
- 问题有歧义、不问清楚就会答偏时，先反问一句。
- 全文尽量短，以手机一屏以内为宜。不讲教科书，不重复他已经知道、或者你在前面的对话里已经说过的东西：前面已经指出过的风险（例如止损超出他自己的规则），后面的回答里不要再说一遍，除非数字明显变了或者他问到。用词客观，不夸张。
- 消息里附带的说明（群里能不能公开账户、有哪些工具、资料库）是给你的背景，不要在回答里复述或解释它们。资料库里没查到相关内容就不提资料库。

边界：
你给的是分析和价位，买不买、卖不卖由交易员自己决定。用“站稳 X 上方看 Y，跌破 Z 则这个判断失效”这样的条件句来表达，不要写“现在买入”“马上做空”这样的指令，不承诺收益，也不替他决定仓位大小。他给出止损价和愿意承担的亏损金额、让你算仓位时，可以帮他算；账户信息里写了他定的单笔风险比例时，按那个比例算。这条边界不需要说出来：不要在开头声明你的做法，也不要在结尾加“由你决定”“仅供参考”之类的话，群里的消息程序会自动带上免责说明。

你自己：
交易员可以把别人也设成主人（例如和他一起看这个账户的朋友），权限和他一样，在群里或私聊里问你时照样回答，“他”“你”指的就是正在问的这个人；账户还是同一个。
你就是群里和私聊里的“交易助手”这个机器人，下单、提醒、简报都是你所在的这个程序做的。不要把自己和“机器人”说成两个东西，不要让他“去 @机器人”或“发给机器人”。他问到你自己（该 @ 谁、确认码怎么回、为什么刚才没执行），就照实回答：在群里 @交易助手 就是 @你。前面的对话里标着“程序自动回复的卡片”的，是这个程序不经过你自动回的，你可以照着它说明刚才发生了什么。
程序认出下单改单的话会自己出预览，轮不到你回答。如果他要下单改单的话还是到了你这里，说明程序没认出来，就给他一句能用的标准写法，例如“止损 XAU 3806”“平仓 XAU 一半”“撤单 XAU”“限价做多 XAU 3850 止损 3820 止盈 3950”；程序会先回预览，他回四位确认码（只发数字也行）才下到 OKX。不要说没有权限。他没要下单时，不要主动推荐这些写法。价格由他定，你可以把你分析出的关键位告诉他作参考。要等价格先突破才进场的单（涨过某价再做多、跌破某价再做空）可以挂突破单，写法“涨破 3900 做多 XAU 止损 3870”“跌破 3800 做空 XAU 止损 3830”。想在几个价位分批挂的，可以分档挂单，写法“分档做多 XAU 3850 3830 3810 止损 3790”“分3档做多 XAU 3850到3810 止损 3790”，最多 5 档。
这些在飞书里都能做，不要说只能去 OKX 的 App 里操作：开仓、平仓、撤单（也能按价格撤，例如“撤单 XAU 3788”）、改止盈止损。改挂单的价格要先撤单再重新挂，两步都能在这里做。价格提醒也能设：写法是“跌破 3800 提醒我”“XAU 涨到 3900 提醒我”，到了程序会马上告诉他，只提醒不下单；发“提醒”看已设的，“取消提醒 编号”取消。程序认出这些话会自己办，轮不到你；到了你这里就告诉他写法。
仓位一有变动程序就自动发卡片，到群里和他的私聊，通常半分钟内：挂单成交就是一张“开仓”或“加仓”卡片，还有减仓、平仓、止盈止损变动，不用另外设。他问挂单成交了能不能通知他，就告诉他已经会自动通知，不要说做不到。
持仓期间想让你每隔几小时盯一下仓位，也能做：写法是“每 4 小时盯一下仓位”（几小时他定，最短半小时），程序会在有持仓时按时发一张卡片，带离止损止盈多远、浮动盈亏和几句点评，只看不动；发“停止盯仓”关掉，发“盯仓”看设置。到了你这里就告诉他写法。
他要的事这个程序确实做不到，直说现在做不到、眼下可以怎么变通，然后在全文最后另起一行写“诉求：”，后面用一句话说他想要什么。程序会把这一行拿掉，记进诉求清单交给开发的人。做得到的事不写这一行。

格式：
回答显示在飞书里，多数时候在手机上看。用简体中文。重要术语第一次出现时可以在括号里附英文，因为读者在学英语，每条回答三四处就够。内容多、需要分段时可以用加粗的小标题和短横线列表，短回答用普通的句子就行；不要用表格和 # 标题。价格按该品种通常的小数位写。"""

ROUTER_SYSTEM = """你在给一个交易辅助机器人收到的消息分类。只输出一个类别名，不要输出别的内容。

类别：
status：只是要看自己的持仓、账户或盈亏的数字，没有问任何别的，例如“仓位”“看下持仓”“现在盈亏多少”。只要还问了判断、风险、是非、原因或要不要做什么（例如“有没有必要平仓”“风险点在哪”“止损设了没有”“帮我确认一下”），就不是 status，是 analysis。
hello：只是打招呼、道谢、寒暄，或者只是告诉一声、表个态，并没有提出问题，例如“你好”“在吗”“谢谢”“好的”“知道了”“这个我去跟 Claude 商量”。
help：只问机器人整体有哪些功能、怎么用，例如“你能做什么”“有哪些命令”。问某一件具体的事怎么做（确认码怎么回、该 @ 谁、为什么没执行），是 analysis。
analysis：其他所有情况，包括问行情、走势、价位、新闻、交易概念、交易想法，以及对机器人自己的提问和抱怨。"""

COMMAND_SYSTEM = """交易员在飞书里给交易机器人发了一句话。机器人只认固定写法的指令，你的事是把这句话改写成一条固定写法的指令。只输出一行：那条指令，或者 NONE。不要输出别的。

固定写法（品种用英文代码，如 XAU、BTC、ETH；价格写纯数字，不带逗号）：
- 止损 XAU 3806
- 止盈 XAU 3950
- 止损 XAU 3806 止盈 3950
- 取消止损 XAU、取消止盈 XAU、取消止盈止损 XAU
- 平仓 XAU（整个仓位）、平仓 XAU 一半、平仓 XAU 30%、减仓 XAU 100张
- 全部平仓
- 撤单 XAU、撤单 全部、撤单 XAU 3788（撤掉挂在 3788 的单）
- 做多 XAU 100张、做空 XAU 0.5个、做多 XAU 500U（数量一定带单位：张、个、U。他说“手”时，黄金 1 手 = 1 个，写成“个”）
- 限价做多 XAU 100张 3850
- 限价做多 XAU 100张 3850 止损 3820 止盈 3950（开仓随单带止损止盈，在后面加上）
- 做多 XAU 止损 3816、限价做多 XAU 3850 止损 3820（不写数量，机器人按他定的单笔风险算数量。只在他说了按风险、按规则、按仓位规则开这类话时用，或者照前面的计划下单而他没说数量时用）
- 涨破 3900 做多 XAU 止损 3870、跌破 3800 做空 XAU 止损 3830 止盈 3700（突破单：价格先涨过或跌破那个价，再按市价进场）
- 分档做多 XAU 3850 3830 3810 止损 3790、分3档做空 XAU 3900到3940 止损 3960（分档挂单：几个价位各挂一张一样多的限价单，带同一个止损，最多 5 档）

规则：
- 只有他明确要机器人现在去做一件事时才改写。提问、讨论、征求意见、说已经发生的事、假设，一律输出 NONE。
- 他在调整计划、提要求时是在跟交易助手商量，输出 NONE。例如“把把握提到 60%”“要确保单子尽可能挂上”“止损再放远一点比较好”“挂单的位置再想想”。只有“下单”“挂上吧”“按这个开”“就这么挂”这类要现在动手的话才改写。
- 价格提醒（“跌破 3800 提醒我”）和记诉求不是下单，输出 NONE。
- 要看、查或分析持仓、挂单、账户，不是下单，输出 NONE（会交给分析）。
- 相对的说法按“现在的持仓”换算成具体价格。例如止损在 3808 时，“止损减两点”“止损往下挪 2”就是“止损 XAU 3806”，“止损上移 5 个点”就是“止损 XAU 3813”。一个“点”按 1 个价格单位算。
- 他没说品种、而现在只有一个持仓时，用那个持仓的品种。
- 缺少必要的信息（品种不明、价格既没给也换算不出、数量不明）时输出 NONE。不要编造他没说的价格或数量。

照前面的计划下单：他说“按你说的挂”“按刚才的计划”“情景一挂上”“就这么挂”这类话时，价格从“前面的对话”里交易助手最近一条写了交易情景的回答里取：入场价作限价，止损或失效位作止损，目标位作止盈（有几个目标位时用第一个）。
- 只用那条回答里写明的数字，不要自己算，也不要改。
- 回答里有几个情景，而他没说是哪一个，输出 NONE。
- 那个情景缺入场价或止损价，输出 NONE。
- 入场价写成一个区间时，他说了分几档就写成分档挂单（分N档做多 品种 区间一端到另一端 止损 价格），没说就输出 NONE。
- 入场要等价格先过去（“突破、站上某价再做多”“跌破某价再做空”）的，写成突破单：涨破 触发价 做多 品种 止损 价格 止盈 价格。
- 他没说数量时不写数量，机器人按他的单笔风险规则算。"""

SMALL_TALK_SYSTEM = """你是“交易助手”，一位交易员在飞书里用的行情分析助手。对方只是打了个招呼、道了声谢、寒暄了一句，或者告诉你一声、表了个态。像熟悉的同事那样用一句自然的中文回应，不超过 30 个字。不要介绍你的功能，不要主动讲行情，不要复述数字，不要用表情符号，也不要连着反问。"""

INTENTS = ("status", "hello", "help", "analysis")
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

# Added to a brief or review that the owner chose to share with the group.
SHARED_NOTE = (
    "这份内容会发给交易员本人，也会发到他和朋友的群里，交易员同意在群里公开自己的账户和持仓。"
    "群里的其他人只是旁观，不要针对他们个人给建议。"
)
NO_TOOLS_NOTE = "这次没有任何工具可用，只能根据上面给出的数据回答，缺的数据直接说明。"
NO_WEB_NOTE = "这次没有联网搜索工具，新闻和经济数据查不到，需要时直接说明看不到。"
SEARCHED_ENOUGH_NOTE = "这个问题的联网搜索次数已经用完。请不要再搜索，新闻和事件用已经查到的内容；行情数据还缺的话可以继续用其他工具。"
# Only for answers to a question: a brief or a review has its own fixed title.
TITLE_NOTE = (
    "回答超过五六行时，在最前面单独写一行“标题：……”，用不超过 16 个字说出这条回答的要点，例如“黄金：4,050 上方偏多”。"
    "程序会把这一行放进卡片的标题栏，正文里不用再重复它。两三句话就能说完的回答不要写标题，也不要用加粗和列表。"
)
# Words that make a question about the trader's own positions and orders: answered at EFFORT_ACCOUNT.
ACCOUNT_WORDS = (
    "仓位", "持仓", "挂单", "委托", "止损", "止盈", "平仓", "加仓", "减仓", "开仓", "账户", "盈亏", "浮亏", "浮盈",
    "爆仓", "强平", "保证金", "权益", "这单", "那单", "这笔", "拿着", "拿住", "我的单",
)  # fmt: skip

REVIEW_TASK = """请复盘这笔刚平掉的交易。上面的数字都是程序从 OKX 的记录、机器人的快照和 K 线里算出来的：直接用，不要重算，也不要再列一遍。
只写三行，每行以固定的词开头，每行一两句：
和计划比：开仓前的分析或他的止损计划，和实际走势、出场方式比怎么样。没有开仓前的分析，就拿止损位置、最大逆向和最大顺向来说，例如“最多顺向 2.1R 却以 -1R 出场”。
教训：一条下次用得上的具体做法，带条件或数字，例如“亚洲盘的突破等 15 分钟收盘确认再进”。不要写“控制风险”“保持纪律”这类空话。这笔做得好，就写值得保留的那个做法。
规则：对照他自己的交易规则，说有没有违反、违反了哪条。规则在资料库里，用 search_knowledge 查“交易规则”；资料库里没有，就只对照程序给出的单笔止损比例那一条，并说明他还没写交易规则。
不要搜索网页，不写标题，不寒暄，不要替他决定下一笔怎么做。"""

DEEP_NOTE = (
    "这次交易员要的是深度分析：可以多查几轮数据和资料，把各周期的结构、关键事件和值得做计划的情景讲透，"
    "篇幅可以到两三屏。"
)


WATCH_SYSTEM = """你是“交易助手”，一位交易员在飞书里用的行情分析助手。他持仓期间让你每隔几小时盯一下仓位。程序已经把每个持仓离止损、止盈多远，浮动盈亏多少，账户总风险，都算好放在卡片上了；你写的是卡片最下面的一小段点评。

怎么写：
- 例行的一次写两三句，不超过 120 字。第一句直接说仓位眼下怎么样：结构还在不在、动能偏哪边，和上一张卡片比有什么变化。
- 接着给接下来最值得盯的一两个价位和条件，价位写精确数字并说明依据（例如 1 小时 EMA50、4 小时前低），用“站稳 X 上方……；跌破 Y……”这样的条件句。
- 需不需要他现在做点什么：多数时候是不用动，就直说。离止损很近、离止盈很近、快公布重要数据、或者走势和这笔单的方向对不上时，点出来，给出你从数据里看到的参考价位（例如保本位、结构失效位）。决定留给他：不要写“把止损提到 X”“减一半”“马上平仓”这样替他拍板的话。
- 消息最后写了“这次要多留意”时，先回应那一点，可以写到四五句、200 字以内。
- 卡片上的数字他已经看到了，不要再列一遍，也不要重算。只用消息里给的数据；新闻和消息面这次没有，不要提，也不要编。
- 不寒暄，不写“仅供参考”“由你决定”。不用表格、标题和加粗，不分段。用简体中文，可以给一个术语附英文。"""

WATCH_TASK = "请写这一次的盯仓点评。"


class AnalystUnavailable(Exception):
    """The AI could not answer. The message is safe to show in Feishu."""


@dataclass(frozen=True)
class Tier:
    model: str
    effort: str
    max_tokens: int
    tools: bool = False
    web_searches: int = WEB_SEARCHES  # how many searches one request may run


@dataclass
class Usage:
    """What one piece of AI work used, summed over every request it took."""

    model: str
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_searches: int = 0

    def add(self, raw) -> None:
        if raw is None:
            return
        self.requests += 1
        self.input_tokens += int(getattr(raw, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(raw, "output_tokens", 0) or 0)
        self.cache_read_tokens += int(getattr(raw, "cache_read_input_tokens", 0) or 0)
        self.cache_write_tokens += int(getattr(raw, "cache_creation_input_tokens", 0) or 0)
        server = getattr(raw, "server_tool_use", None)
        if server is not None:
            self.web_searches += int(getattr(server, "web_search_requests", 0) or 0)

    @property
    def cost_usd(self) -> float:
        """An estimate at list prices. The bill in the Claude Console is what counts."""
        price_in, price_out, price_read = next(
            (price for prefix, price in PRICES.items() if self.model.startswith(prefix)), UNKNOWN_MODEL_PRICE
        )
        tokens = (
            self.input_tokens * price_in
            + self.cache_write_tokens * price_in * 1.25
            + self.cache_read_tokens * price_read
            + self.output_tokens * price_out
        )
        return tokens / 1_000_000 + self.web_searches * WEB_SEARCH_USD

    def as_row(self) -> dict:
        return dict(
            model=self.model,
            requests=self.requests,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            web_searches=self.web_searches,
            cost_usd=self.cost_usd,
        )


class Analyst:
    def __init__(
        self,
        cfg: Config,
        client: anthropic.Anthropic | None = None,
        toolbox: Toolbox | None = None,
        on_usage: Callable[[str, Usage], None] | None = None,
        month_spend: Callable[[], float] | None = None,
        clock: Callable[[], float] = time.time,
        on_levels: Callable[[str, str, Levels], None] | None = None,
    ):
        """`on_usage(purpose, usage)` is called after every piece of work.
        `month_spend()` returns this month's estimated cost in USD, for the budget.
        `on_levels(purpose, question, levels)` receives the levels of an analysis
        that gave scenarios, for the track record (track.py)."""
        self._cfg = cfg
        self._client = client  # created on first use, so a missing key never breaks start-up
        self._toolbox = toolbox
        self._on_usage = on_usage
        self._month_spend = month_spend
        self._clock = clock
        self._on_levels = on_levels if cfg.track_record else None
        # Which set of web tools to offer; steps down when the API turns one away.
        self._web_level = 0 if cfg.web_search else len(WEB_TOOL_SETS) - 1
        self._feedback: Callable[[bool], str] | None = None
        self._tiers = {
            "quick": Tier(cfg.model_quick, "low", 1024),
            "daily": Tier(cfg.model_daily, cfg.effort_daily, 16000, tools=True),
            "account": Tier(cfg.model_daily, cfg.effort_account, 16000, tools=True),
            "judge": Tier(cfg.model_daily, "low", 2000),  # scores answers in `python -m bot eval`
            "deep": Tier(cfg.model_deep, cfg.effort_deep, 32000, tools=True, web_searches=WEB_SEARCHES_DEEP),
            "watch": Tier(cfg.model_watch, cfg.effort_watch, 3000),
            "watch_close": Tier(cfg.model_daily, cfg.effort_daily, 8000),
        }

    def attach_account(self, account, history) -> None:
        """Lets the AI read the trader's account and closes with its tools, in answers that may show them."""
        if self._toolbox is not None:
            self._toolbox.attach_account(account, history)

    def attach_feedback(self, feedback: Callable[[bool], str]) -> None:
        """`feedback(with_account)` gives the AI's own track record and, when the answer may show the
        account, the lessons of the latest trade reviews. Read before every answer and brief."""
        self._feedback = feedback

    def _feedback_text(self, with_account: bool) -> str:
        if self._feedback is None:
            return ""
        try:
            return self._feedback(with_account) or ""
        except Exception:  # an answer is never held up for it
            log.exception("could not read the track record and lessons for the AI")
            return ""

    @property
    def enabled(self) -> bool:
        return self._client is not None or bool(self._cfg.anthropic_api_key)

    @property
    def can_search_web(self) -> bool:
        return self._toolbox is not None and bool(WEB_TOOL_SETS[self._web_level])

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self._cfg.anthropic_api_key)
        return self._client

    # ------------------------------------------------------------------ core

    def _over_budget(self) -> bool:
        budget = self._cfg.ai_monthly_budget_usd
        if budget is None or self._month_spend is None:
            return False
        try:
            return self._month_spend() >= budget
        except Exception:
            log.exception("could not read this month's AI spend")
            return False

    def _ask(self, tier_name: str, system: str, user: str, purpose: str = "", history=None, account: bool = False) -> str:
        return self._ask_with_levels(tier_name, system, user, purpose, history, account)[0]

    def _analyse(
        self, tier_name: str, user: str, purpose: str, question: str = "", history=None, record: bool = True, account: bool = False
    ) -> str:
        """An analysis in the analyst's voice. When the track record is kept, the AI adds
        its levels in a block for the program, which is cut off here and passed on."""
        keep = record and self._on_levels is not None
        system = ANALYST_SYSTEM + LEVELS_NOTE if keep else ANALYST_SYSTEM
        text, levels = self._ask_with_levels(tier_name, system, user, purpose, history, account)
        if keep and levels is not None:
            try:
                self._on_levels(purpose, question, levels)
            except Exception:
                log.exception("could not keep the levels of this analysis")
        return text

    def _ask_with_levels(
        self, tier_name: str, system: str, user: str, purpose: str = "", history=None, account: bool = False
    ) -> tuple[str, Levels | None]:
        """`account`: the answer may show the trader's account, so the AI may read it with its tools."""
        if not self.enabled:
            raise AnalystUnavailable("还没有配置 Claude 的 API key，AI 分析暂时用不了。")
        tier = self._tiers[tier_name]
        saving = tier_name not in CHEAP_TIERS and self._over_budget()
        if saving:
            tier = Tier(self._cfg.model_daily, "low", BUDGET_TIER_TOKENS, tools=tier.tools)
        messages: list[dict] = []
        for question, answer in history or ():
            messages.append({"role": "user", "content": question})
            messages.append({"role": "assistant", "content": answer})
        messages.append({"role": "user", "content": user})

        usage = Usage(tier.model)
        web_before = self.can_search_web
        try:
            message = self._converse(tier, system, messages, usage, account)
        except anthropic.AuthenticationError as exc:
            raise AnalystUnavailable("Claude 的 API key 无效，请检查 .env 里的 ANTHROPIC_API_KEY。") from exc
        except anthropic.PermissionDeniedError as exc:
            raise AnalystUnavailable("这把 Claude API key 没有权限使用所选的模型。") from exc
        except anthropic.NotFoundError as exc:
            raise AnalystUnavailable(f"找不到模型 {tier.model}，请检查 .env 里的模型名称。") from exc
        except anthropic.RateLimitError as exc:
            raise AnalystUnavailable("Claude 那边提示请求太频繁或额度用完了，请稍后再试。") from exc
        except anthropic.APIStatusError as exc:
            log.warning("Claude API error %s: %s", exc.status_code, exc.message)
            raise AnalystUnavailable(f"Claude 接口返回了错误（{exc.status_code}），请稍后再试。") from exc
        except anthropic.APIConnectionError as exc:
            raise AnalystUnavailable("连不上 Claude 的接口，请稍后再试。") from exc
        finally:
            self._report(purpose or tier_name, usage)

        if message.stop_reason == "refusal":
            raise AnalystUnavailable("这个问题 AI 没有回答，换个问法试试。")
        text, levels = extract_levels(_final_text(message))  # the block for the program is never shown
        if not text:
            raise AnalystUnavailable("AI 这次没有给出内容，请再试一次。")
        if message.stop_reason == "max_tokens":
            text += "\n\n（内容较长，被截断了。）"
        if web_before and not self.can_search_web:
            text += "\n\n（这把 Claude API key 所在的组织没有开通联网搜索，这次没有查新闻。在 Claude Console 的设置里打开 Web search 后重启机器人即可。）"
        if saving:
            text += f"\n\n（本月 AI 用量已到预算上限 {self._cfg.ai_monthly_budget_usd:g} 美元，这条回答用的是省钱档。）"
        return text, levels

    def _report(self, purpose: str, usage: Usage) -> None:
        if usage.requests == 0:
            return
        log.info(
            "AI %s: %s, %d request(s), %d in / %d out / %d cached tokens, %d web search(es), about $%.3f",
            purpose, usage.model, usage.requests, usage.input_tokens + usage.cache_write_tokens,
            usage.output_tokens, usage.cache_read_tokens, usage.web_searches, usage.cost_usd,
        )  # fmt: skip
        if self._on_usage is not None:
            try:
                self._on_usage(purpose, usage)
            except Exception:
                log.exception("could not record AI usage")

    def _converse(self, tier: Tier, system: str, messages: list[dict], usage: Usage, account: bool = False):
        """Sends the conversation, runs the tools the AI asks for, and returns its final message."""
        toolbox = self._toolbox if tier.tools else None
        if toolbox is None:
            message = self._send(
                dict(model=tier.model, max_tokens=tier.max_tokens, system=system, messages=messages,
                     output_config={"effort": tier.effort}),
                use_fallback=tier.model.startswith(FALLBACK_MODEL_PREFIXES),
            )  # fmt: skip
            usage.add(getattr(message, "usage", None))
            return message

        # The instructions never change, so they are cached; the automatic
        # breakpoint then follows the conversation as the tool rounds add to it.
        system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        start = len(messages)
        told_to_stop_searching = False
        for step in range(MAX_STEPS):
            while True:
                request = dict(
                    model=tier.model,
                    max_tokens=tier.max_tokens,
                    system=system_blocks,
                    messages=messages,
                    tools=toolbox.definitions(self._web_level, tier.web_searches, account),
                    output_config={"effort": tier.effort},
                    cache_control={"type": "ephemeral"},
                )
                try:
                    message = self._send(request, use_fallback=False)
                    break
                except (anthropic.BadRequestError, anthropic.PermissionDeniedError) as exc:
                    # A web tool this organisation cannot use: offer an older one, then none.
                    if len(messages) != start or not self.can_search_web or not _about_tools(exc):
                        raise
                    self._web_level += 1
                    log.warning("the API turned the web tools away (%s); stepping down to set %d", exc.message, self._web_level)
            usage.add(getattr(message, "usage", None))

            if message.stop_reason == "pause_turn":  # a long server-side search: send it back to continue
                messages.append({"role": "assistant", "content": message.content})
                continue
            if message.stop_reason != "tool_use":
                return message

            messages.append({"role": "assistant", "content": message.content})
            results: list[dict] = []
            for block in message.content:
                if block.type != "tool_use":
                    continue
                text, is_error = toolbox.run(block.name, block.input, account)
                result = {"type": "tool_result", "tool_use_id": block.id, "content": text}
                if is_error:
                    result["is_error"] = True
                results.append(result)
            if step == MAX_STEPS - 2:
                results.append({"type": "text", "text": "查询次数快到上限了。请不要再调用工具，直接根据已经拿到的信息写出最终回答。"})
            elif usage.web_searches >= tier.web_searches and not told_to_stop_searching:
                # The limit in the tool definition counts per request, and every round is a
                # new request. Changing the definition would throw away the cached prompt,
                # so the AI is told instead.
                told_to_stop_searching = True
                results.append({"type": "text", "text": SEARCHED_ENOUGH_NOTE})
            messages.append({"role": "user", "content": results})
        raise AnalystUnavailable("这个问题查了很多轮还没有结论，请把问题问得具体一点再试。")

    def _send(self, request: dict, use_fallback: bool):
        """Streams the request and returns the final message.

        Requests without tools opt into the server-side refusal fallback. If
        the API rejects that option, the request is sent again without it.
        """
        client = self._get_client()
        if use_fallback:
            try:
                with client.beta.messages.stream(**request, betas=[FALLBACK_BETA], fallbacks="default") as stream:
                    return stream.get_final_message()
            except anthropic.BadRequestError as exc:
                log.warning("request with refusal fallback was rejected (%s); retrying without it", exc.message)
        with client.messages.stream(**request) as stream:
            return stream.get_final_message()

    # --------------------------------------------------------------- routing

    def route_intent(self, text: str) -> str:
        """Classify a free-text message. Falls back to 'analysis' on any trouble."""
        try:
            answer = self._ask("quick", ROUTER_SYSTEM, text, purpose="routing").lower()
        except AnalystUnavailable as exc:
            log.info("intent routing unavailable: %s", exc)
            return "analysis"
        for intent in INTENTS:
            if intent in answer:
                return intent
        return "analysis"

    def to_command(self, text: str, positions_text: str, earlier: list[tuple[str, str]] | None = None) -> str:
        """The owner's words as one fixed-pattern order command, for the trade desk to read and preview.
        "" when they are not an order, or the AI cannot be asked. The AI only writes the command out;
        the desk checks it, shows it and waits for the owner's code as for any other.
        `earlier`: the chat's last few (question, answer) pairs, oldest first, for "按刚才说的挂"."""
        user = f"现在的持仓：\n{positions_text}\n\n他发的话：{text}"
        if earlier:
            turns = "\n\n".join(f"他问：{question}\n交易助手答：{answer}" for question, answer in earlier)
            user = f"前面的对话（从早到晚）：\n{turns}\n\n{user}"
        try:
            answer = self._ask("quick", COMMAND_SYSTEM, user, purpose="reading an order")
        except AnalystUnavailable as exc:
            log.info("reading an order with the AI is unavailable: %s", exc)
            return ""
        lines = [line.strip().strip("`“”\"'。 ") for line in answer.strip().splitlines() if line.strip()]
        if not lines or lines[0].upper().startswith("NONE"):
            return ""
        return lines[0]

    def small_talk(self, text: str) -> str:
        """One line in reply to a greeting or a thank-you. "" when the AI cannot be asked."""
        try:
            return self._ask("quick", SMALL_TALK_SYSTEM, text, purpose="small talk")
        except AnalystUnavailable as exc:
            log.info("small talk unavailable: %s", exc)
            return ""

    # -------------------------------------------------------------- analysis

    def _now_line(self) -> str:
        now = datetime.fromtimestamp(self._clock(), tz=self._cfg.tz)
        return f"现在是 {now.strftime('%Y-%m-%d %H:%M')}，{WEEKDAYS[now.weekday()]}（交易员的时区 {self._cfg.timezone}）。"

    def _tools_note(self) -> str | None:
        if self._toolbox is None:
            return NO_TOOLS_NOTE
        return None if self.can_search_web else NO_WEB_NOTE

    def answer(
        self,
        question: str,
        market_text: str,
        account_text: str | None,
        in_group: bool,
        deep: bool = False,
        history: list[tuple[str, str]] | None = None,
    ) -> str:
        """`history` is the last few (question, answer) pairs of the same chat, oldest first."""
        parts = [self._now_line()]
        if market_text:
            parts.append(f"OKX 实时行情数据：\n{market_text}")
        if in_group and account_text:
            parts.append(
                "（背景：这是交易员和朋友的群，他的账户和持仓可以在群里谈；群里其他人只是旁观，不要针对他们个人给建议。）\n"
                f"交易员当前的账户和持仓：\n{account_text}"
            )
        elif in_group:
            parts.append(
                "（背景：这是一个有其他人的群。只讲行情本身，不要提及或推测任何人的持仓和资金，"
                "也不要针对提问者个人给建议。）"
            )
        elif account_text:
            parts.append(f"交易员当前的账户和持仓：\n{account_text}")
        feedback = self._feedback_text(account_text is not None)
        if feedback:
            parts.append(feedback)
        note = self._tools_note()
        if note:
            parts.append(note)
        if deep:
            parts.append(DEEP_NOTE)
        parts.append(TITLE_NOTE)
        parts.append(f"问题：{question}")
        tier = "deep" if deep else ("account" if account_text is not None and about_account(question) else "daily")
        return self._analyse(
            tier, "\n\n".join(parts), "deep question" if deep else "question", question, history, account=account_text is not None
        )

    def brief(
        self,
        kind: str,
        market_text: str,
        account_text: str | None,
        journal_text: str | None,
        for_owner: bool,
        shared_with_group: bool = False,
    ) -> str:
        """for_owner: the version with the account in it. shared_with_group: that
        same version is also posted in the group, because the owner chose to."""
        search = self.can_search_web
        if kind == "morning":
            task = "请写今天的盘前简报 (pre-market brief)。"
            if search:
                task += "先搜索过去 24 小时影响这些品种的重要新闻，以及今天和明天的重要经济数据和事件。"
            task += (
                "开头用一句话说今天最要紧的事，然后写：各周期的趋势状态、今天需要盯的关键点位（精确数字）、"
                "值得做计划的情景（触发条件、目标位、失效位），以及风险和事件（带时间）。"
                "写法跟着当天的行情走，没什么可说的部分一句带过，不要每天一个模样。保持简短。"
            )
        else:
            task = "请写今天的收盘小结 (daily wrap)。"
            if search:
                task += "先搜索今天影响这些品种的重要新闻和已经公布的经济数据。"
            task += (
                "开头用一句话说今天最值得记住的事，然后写：今天的走势和波动、关键点位有没有被突破、驱动行情的消息，"
                "以及接下来需要观察什么。写法跟着当天的行情走，平淡的一天就写得短。保持简短。"
            )
        parts = [self._now_line(), f"OKX 实时行情数据：\n{market_text}"]
        if for_owner:
            if account_text:
                parts.append(f"交易员当前的账户和持仓：\n{account_text}")
            if journal_text:
                parts.append(f"今天的交易记录：\n{journal_text}")
            task += SHARED_NOTE if shared_with_group else "这份简报只发给交易员本人。"
            task += "结尾加一行“今日英语”：给一句和今天行情有关的地道英文交易用语，并附中文意思。"
        else:
            task += "这份简报会发在一个有其他人的群里，只讲行情本身，不要提及任何人的持仓和资金。"
        feedback = self._feedback_text(for_owner)
        if feedback:
            parts.append(feedback)
        note = self._tools_note()
        if note:
            parts.append(note)
        parts.append(task)
        # Two versions of one brief may be written (owner and group): only the owner's is kept, so it counts once.
        return self._analyse("daily", "\n\n".join(parts), f"{kind} brief", record=for_owner, account=for_owner)

    def weekly_review(
        self, market_text: str, journal_text: str, equity_text: str, shared_with_group: bool = False, week_figures: str = ""
    ) -> str:
        task = (
            "请写本周复盘 (weekly review)。"
            + (SHARED_NOTE if shared_with_group else "这份复盘只发给交易员本人。")
            + "根据下面的数字和交易记录，找出他这一周的交易行为模式，"
            "例如开仓的时间段、加减仓的节奏、止损有没有被移动、赚到的占持仓期间最大浮盈的多少，说明哪些做法值得保留、哪些值得警惕。"
            "R、期望值和分时段的数字是程序算好的，直接用，不要重算。"
            "只根据给出的记录来写，记录里没有的事情不要推测。记录很少时就直接说样本太少，不要硬凑结论。"
        )
        if self._toolbox is not None:
            task += "每笔平仓的细节可以用 get_trade_history 查。资料库里如果有他自己的交易规则，拿来对照他这一周的做法。"
        task += "结尾加一行“本周英语”：给一句和复盘有关的地道英文交易用语，并附中文意思。"
        parts = [
            self._now_line(),
            f"OKX 实时行情数据：\n{market_text}",
            f"本周权益变化：\n{equity_text}",
        ]
        if week_figures:
            parts.append(f"本周的平仓（程序从复盘记录里算的）：\n{week_figures}")
        parts.append(f"本周交易记录：\n{journal_text}")
        feedback = self._feedback_text(True)
        if feedback:
            parts.append(feedback)
        note = self._tools_note()
        if note:
            parts.append(note)
        parts.append(task)
        return self._ask("deep", ANALYST_SYSTEM, "\n\n".join(parts), purpose="weekly review", account=True)

    def review_trade(self, figures: str, lessons: str = "", shared_with_group: bool = False) -> str:
        """Three lines on a position just closed: against the plan, one lesson, the trader's rules."""
        parts = [self._now_line(), f"刚平掉的这笔交易：\n{figures}"]
        if lessons:
            parts.append(lessons + "\n这些教训已经记下了：不要重复写同一条；这笔又犯了同样的错，就直说又犯了。")
        parts.append(REVIEW_TASK + (SHARED_NOTE if shared_with_group else ""))
        return self._ask("daily", ANALYST_SYSTEM, "\n\n".join(parts), purpose="trade review", account=True)

    def watch_comment(self, facts: str, close: bool = False) -> str:
        """A few sentences under a timed position check (watch.py). `close`: a position wants a closer look,
        so the stronger model writes them. No tools: everything it needs is in `facts`."""
        tier = "watch_close" if close else "watch"
        user = f"{self._now_line()}\n\n{facts}\n\n{WATCH_TASK}"
        return self._ask(tier, WATCH_SYSTEM, user, purpose="position watch" + (" (close look)" if close else ""))

    def grade(self, system: str, user: str) -> str:
        """One answer scored against its points, for `python -m bot eval`. No tools; metered as "eval judge"."""
        return self._ask("judge", system, user, purpose="eval judge")

    def ping(self) -> str:
        """Used by the self-check: the cheapest possible real call."""
        return self._ask("quick", "你是连通性测试。只回复：OK", "测试", purpose="self-check")

    def check_web_search(self) -> None:
        """Used by the self-check: one real search. Raises AnalystUnavailable when it cannot be done."""
        if self._toolbox is None or not self._cfg.web_search:
            raise AnalystUnavailable("联网搜索没有打开（WEB_SEARCH=false）。")
        if not self.enabled:
            raise AnalystUnavailable("还没有配置 Claude 的 API key。")
        usage = Usage(self._cfg.model_daily)
        tier = Tier(self._cfg.model_daily, "low", 4000, tools=True)
        messages = [{"role": "user", "content": "这是连通性测试。请用 web_search 搜索一次 gold price，然后只回复两个字：可用"}]
        try:
            self._converse(tier, "你是连通性测试，按要求做，不要多写。", messages, usage)
        except anthropic.APIError as exc:
            raise AnalystUnavailable(f"联网搜索的测试请求失败：{getattr(exc, 'message', exc)}") from exc
        finally:
            self._report("self-check", usage)
        if not self.can_search_web:
            raise AnalystUnavailable("这把 Claude API key 所在的组织没有开通联网搜索。在 Claude Console 的设置里打开 Web search 即可。")
        if usage.web_searches == 0:
            raise AnalystUnavailable("测试请求成功了，但模型没有执行搜索，联网搜索的状态不确定。")


def about_account(question: str) -> bool:
    """Whether a question is about the trader's own positions and orders."""
    return any(word in question for word in ACCOUNT_WORDS)


def _about_tools(exc: anthropic.APIStatusError) -> bool:
    text = str(getattr(exc, "message", "") or exc).lower()
    return any(word in text for word in ("web_search", "web_fetch", "web search", "web fetch", "tool"))


def _final_text(message) -> str:
    """The answer, without the remarks the AI makes between tool calls, plus the pages it cited."""
    blocks = list(message.content)
    last_tool = max(
        (index for index, block in enumerate(blocks) if block.type not in ("text", "thinking", "redacted_thinking")),
        default=-1,
    )
    every = [block for block in blocks if block.type == "text"]
    tail = [block for block in blocks[last_tool + 1 :] if block.type == "text"]
    tail_size = len("".join(block.text for block in tail).strip())
    head_size = len("".join(block.text for block in blocks[: last_tool + 1] if block.type == "text").strip())
    # A few closing words after a long text and one last search are not the answer: then keep everything.
    chosen = tail if tail_size and (tail_size >= 40 or head_size < 200) else every
    text = "".join(block.text for block in chosen).strip()
    if not text:
        return ""
    sources: list[tuple[str, str]] = []
    for block in chosen:
        for citation in getattr(block, "citations", None) or ():
            url = getattr(citation, "url", None)
            if url and all(url != known for _, known in sources):
                title = (getattr(citation, "title", None) or url).replace("[", "(").replace("]", ")").strip()
                sources.append((title[:60], url))
    if sources:
        text += "\n\n**来源**\n" + "\n".join(f"- [{title}]({url})" for title, url in sources[:MAX_SOURCES])
    return text
