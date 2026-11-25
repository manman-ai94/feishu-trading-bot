"""v0.22.0: the MT5 account spoken of in its own words, its rules told to the AI, the plan's reasons kept, and
a rehearsal on MT5 that sends nothing."""
from __future__ import annotations

import json

import httpx

from bot.analyst import ANALYST_SYSTEM, Analyst
from bot.discipline import InstrumentPlan, parse_plan
from bot.feishu_client import FeishuClient
from bot.instruments import InstrumentResolver
from bot.journal import Journal
from bot.models import Position, size_text
from bot.mt5_client import Mt5Client, Mt5Link, broker_wording
from bot.trading import TradeDesk
from tests.fake_mt5 import FakeMarket, FakeMt5
from tests.helpers import Clock, make_cfg

GOLD = "XAU-USD-SWAP"
START = 1_800_000_000.0


def reader(fake: FakeMt5 | None = None, clock: Clock | None = None) -> tuple[Mt5Client, FakeMt5]:
    clock = clock or Clock(START)
    fake = fake or FakeMt5(clock)
    return Mt5Client(FakeMarket(), Mt5Link(module=fake, clock=clock), {"XAU": "XAUUSD"}, clock=clock), fake


# ------------------------------------------------------------------ words


def test_a_position_on_mt5_is_told_in_lots_and_ounces_and_on_okx_as_before():
    mt5 = Position(GOLD, "SWAP", "short", "cross", 0.07, "short", coins=7.0, coin_ccy="XAU", unit="手")
    assert size_text(mt5) == "0.07 手（7 盎司）"
    okx = Position("XAU-USDT-SWAP", "SWAP", "long", "cross", 7000, "long", coins=7.0, coin_ccy="XAU")
    assert size_text(okx) == "7 XAU"
    bare = Position("BTC-USDT-SWAP", "SWAP", "long", "cross", 3, "long")
    assert size_text(bare) == "3 张"


def test_mt5_positions_count_lots():
    client, fake = reader()
    fake.open_ticket(1, 0.07, 3635.92, sl=3637.99)
    [position] = client.positions()
    assert position.unit == "手" and size_text(position) == "0.07 手（7 盎司）"


def test_what_goes_out_on_mt5_names_gold_as_the_terminal_does():
    worded = broker_wording({"XAU": "XAUUSD"})
    text = "XAU-USD-SWAP 空单 0.07 手，标记价 3,634.88；OKX 大户看 XAU-USDT-SWAP"
    assert worded(text) == "XAUUSD 空单 0.07 手，现价 3,634.88；OKX 大户看 XAU-USDT-SWAP"


def test_the_feishu_client_puts_the_wording_on_texts_and_cards():
    sent = []

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = FeishuClient("cli_test", "secret", "http://feishu.test", http=httpx.Client(transport=httpx.MockTransport(answer)))
    client.wording = broker_wording({"XAU": "XAUUSD"})
    client.send_text("oc_1", "XAU-USD-SWAP 标记价 3,600")
    client.reply_markdown("om_1", "XAU-USD-SWAP 盯仓", "标记价 3,600", footer="XAU-USD-SWAP")
    assert json.loads(sent[0]["content"]) == {"text": "XAUUSD 现价 3,600"}
    card = sent[1]["content"]
    assert "XAU-USD-SWAP" not in card and "标记价" not in card and "XAUUSD 盯仓" in card and "现价 3,600" in card


def test_the_total_risk_on_mt5_is_in_usd():
    from decimal import Decimal

    from bot.exposure import Exposure, Piece, card_lines

    mt5 = Position(GOLD, "SWAP", "long", "cross", 0.02, "long", ccy="USD", unit="手")
    assert mt5.pnl_ccy == "USD"
    exposure = Exposure([Piece("XAUUSD 多单", Decimal(100), GOLD, "long", True)])
    text = "\n".join(card_lines(exposure, Decimal(1000), 25, mt5.pnl_ccy))
    assert "USD" in text and "USDT" not in text


# ------------------------------------------------------------------ the AI


def test_the_ai_is_given_the_accounts_own_leverage_stop_out_lot_and_swap():
    client, fake = reader()
    fake.leverage = 1000
    rules = client.rules_for_ai()
    assert "账户杠杆 1:1000" in rules and "不要假设别的杠杆" in rules
    assert "降到 100% 发追加保证金通知" in rules and "降到 50% 开始强制平仓" in rules
    assert "XAUUSD：1 手 = 100 盎司，最小 0.01 手" in rules
    assert "多单 -57.22 USD、空单 +23.55 USD" in rules and "周三那晚收三天" in rules
    fake.leverage = 500
    assert "1:1000" in client.rules_for_ai()  # kept for a few hours: the cached instructions stay the same


def test_the_rules_go_into_the_analysts_instructions_and_a_failure_leaves_them_out():
    analyst = Analyst(make_cfg(broker="mt5"), client=object())
    analyst.broker_rules = lambda: "\n\nMT5 账户规则：账户杠杆 1:1000"
    assert analyst._for_broker(ANALYST_SYSTEM).endswith("账户杠杆 1:1000")

    def broken():
        raise RuntimeError("terminal gone")

    analyst.broker_rules = broken
    assert analyst._for_broker(ANALYST_SYSTEM).endswith("现在都指 MT5。")


def test_a_terminal_that_cannot_be_read_gives_no_rules_rather_than_an_error():
    client, fake = reader()
    fake.logged_in = False
    assert client.rules_for_ai() == ""


# ------------------------------------------------------------------ the plan's reasons


def test_the_plan_keeps_its_reason_and_the_ai_is_told_it():
    from bot.discipline import Discipline

    [item] = parse_plan('{"items":[{"inst":"XAU-USDT-SWAP","bias":"none","support":[3537.9],"resistance":[3652],"why":"价格在区间中间，明天有 PCE"}]}')
    assert item.why == "价格在区间中间，明天有 PCE" and item.as_row()["why"] == item.why
    assert "why" not in InstrumentPlan("XAU-USDT-SWAP", "long").as_row()  # plans without a reason read as before
    clock = Clock(START)
    journal = Journal(":memory:")
    discipline = Discipline(make_cfg(), journal, okx=None, clock=clock)
    discipline.keep(discipline.today(), (item,), "今天观望。")
    notes = discipline.notes_for_ai(with_status=False)
    assert "写计划时的理由：XAU-USDT-SWAP：价格在区间中间，明天有 PCE" in notes


def test_a_plan_written_before_reasons_were_kept_gives_the_start_of_its_text():
    from bot.discipline import Discipline

    clock = Clock(START)
    discipline = Discipline(make_cfg(), Journal(":memory:"), okx=None, clock=clock)
    discipline.keep(discipline.today(), (InstrumentPlan("XAU-USDT-SWAP", "none"),), "今天观望：方向的依据互相打架。\n\n下面是价位。")
    assert "写计划时的原文（开头一段）：今天观望：方向的依据互相打架。 下面是价位。" in discipline.notes_for_ai(with_status=False)


# ------------------------------------------------------------------ the rehearsal on MT5


def desk(trading: str = "live", fake: FakeMt5 | None = None) -> tuple[TradeDesk, FakeMt5]:
    clock = Clock(START)
    client, fake = reader(fake or FakeMt5(clock), clock)
    cfg = make_cfg(broker="mt5", trading=trading, watch_instruments=(GOLD,), trade_instruments=(GOLD,), plan_required=False)
    return TradeDesk(cfg, Journal(":memory:"), reader=client, trader=None, resolver=InstrumentResolver(client, clock), clock=clock), fake


def test_the_rehearsal_on_mt5_checks_every_kind_of_order_and_sends_none():
    bench, fake = desk(trading="off")
    fake.open_ticket(1, 0.07, 3810.0, sl=3830.0)
    reply = bench.handle("MT5 自测")
    assert reply.title == "MT5 自测（只核对，不下单）" and reply.template == "green"
    for name in ("市价开多", "市价开空", "限价买单", "限价卖单", "涨破做多", "跌破做空", "平掉现有空单的一部分（0.01 手）"):
        assert name in reply.text
    assert "**不通过**" not in reply.text and "一张单也没有发出去" in reply.text
    assert fake.requests == [] and len(fake.checks) == 7  # order_check only: nothing was sent
    assert {request["type"] for request in fake.checks} == {0, 1, 2, 3, 4, 5}
    assert "TRADING=off" in reply.text
    assert "全部通过" in "\n".join(bench.status_lines())


def test_the_rehearsal_says_what_mt5_would_refuse():
    fake = FakeMt5(Clock(START))
    fake.algo_trading = False
    bench, fake = desk(fake=fake)
    reply = bench.handle("模拟盘自测")
    assert reply.template == "red" and "**不通过**：终端的“算法交易”(Algo Trading)" in reply.text
    assert "自动交易被终端关闭了" in reply.text or "10027" in reply.text
    assert fake.requests == []
    fake.algo_trading = True
    fake.check_fail = {4: 10016}
    again = bench.handle("mt5自测")
    assert "**不通过**：突破单：涨破做多 (buy stop)" in again.text and "（10016）" in again.text


def test_the_help_on_mt5_is_written_in_lots():
    bench, _ = desk()
    text = bench.handle("交易帮助").text
    assert "做多 XAU 0.05手" in text and "100张" not in text and "USDT 本位" not in text and "order_check" in text
