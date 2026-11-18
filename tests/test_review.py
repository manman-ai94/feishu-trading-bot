"""Each closed position is reviewed from the program's own figures, and the lesson reaches the next answer."""
from __future__ import annotations

import sqlite3
from datetime import timezone

from bot.app import feedback_text
from bot.journal import Journal
from bot.models import Candle, StopOrder
from bot.review import Reviewer, bar_for, excursions, lesson_of, r_multiple, stop_trail, week_text

from .helpers import FakeAnalyst, FakeFeishu, make_cfg, make_position, make_state
from .test_book import XAU, Gold

UTC = timezone.utc
OPEN = 1_800_000_000  # 2027-01-15 08:00 UTC
CLOSE = OPEN + 3 * 3600


def closed_row(**overrides) -> dict:
    row = {
        "instId": XAU, "direction": "long", "type": "2", "openAvgPx": "3850", "closeAvgPx": "3880", "closeTotalPos": "5000",
        "pnl": "150", "fee": "-19.3", "fundingFee": "0", "realizedPnl": "130.7", "ccy": "USDT",
        "cTime": str(OPEN * 1000), "uTime": str(CLOSE * 1000),
    }  # fmt: skip
    row.update(overrides)
    return row


class Market(Gold):
    """Gold with 15-minute candles around the holding: down to 3,835 first, up to 3,895 later."""

    def candles(self, inst_id, bar="1H", limit=200):
        self.bars_asked = bar
        candles = []
        for i in range(-4, 16):
            ts = OPEN + i * 900
            low = 3835.0 if i == 2 else 3845.0
            high = 3895.0 if i == 10 else 3870.0
            candles.append(Candle(ts * 1000, 3850.0, high, low, 3860.0))
        return candles


def holding(stop: float | None, equity: float = 2000.0):
    stops = (StopOrder("a1", sl_trigger_px=stop),) if stop else ()
    return make_state(positions=[make_position(qty=5000.0, avg_px=3850.0, stops=stops)], total_eq=equity)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def bench(reply="和计划比：按计划走。\n教训：**突破单等 15 分钟收盘确认再进。**\n规则：没有违反。", shares=True):
    okx = Market()
    okx.closed = [closed_row()]
    journal = Journal(":memory:")
    journal.record_snapshot(holding(3820.0), OPEN + 30)
    journal.record_snapshot(holding(3835.0), OPEN + 3600)
    journal.record_snapshot(make_state(positions=[]), CLOSE + 5)
    analyst = FakeAnalyst()
    analyst.reply = reply
    sent = []

    class Reporter:
        group_shares_account = shares

        def send_review(self, title, body):
            sent.append((title, body))

    reviewer = Reviewer(okx, journal, analyst, make_cfg(timezone="UTC"), Reporter(), clock=Clock(CLOSE + 10), sleep=lambda s: None)
    return reviewer, journal, analyst, sent, okx


def test_the_figures_come_from_the_records_and_the_lesson_is_kept():
    reviewer, journal, analyst, sent, okx = bench()
    journal.record_analysis("question", XAU, "long", 3848.0, [{"side": "long", "trigger": 3850, "target": 3900, "invalid": 3820}], "黄金怎么看", ts=OPEN - 3600)
    journal.record_order("live", "open", "限价做多 XAU 5 个 3850 止损 3820", "ok", inst_id=XAU, ts=OPEN - 60, source="plan")
    assert reviewer.review(XAU, CLOSE + 10) is True
    [(title, body)] = sent
    assert title == f"复盘：{XAU} 多单"
    for expected in (
        "XAU-USDT-SWAP 多单 5 XAU（5,000 张），平仓单（不是止盈止损单触发的）",
        "开仓均价 3,850.00，平仓均价 3,880.00，01-15 08:00 开仓，01-15 11:00 平仓，持仓 3 小时（开在欧洲盘）",
        "已实现盈亏 +130.70 USDT（价差 +150.00，手续费 -19.30）",
        "第一次看到的止损 3,820.00，动过 1 次：3,820.00 → 3,835.00",
        "按第一次的止损，这笔最多亏约 150.00 USDT，占开仓时权益的 7.5%（交易规则：单笔止损最多亏权益的 1%）",
        "结果 +1.00R",
        "持仓期间最多逆向 15.00（0.50R），最多顺向 45.00（1.50R）（按 15m K 线估算",
        "开仓前最后一次带点位的分析：01-15 07:00，问题“黄金怎么看”，偏多；情景：多头 触发 3,850.00 目标 3,900.00 失效 3,820.00",
        "机器人下的开仓单：限价做多 XAU 5 个 3850 止损 3820（来源：AI 情景）",
        "教训：**突破单等 15 分钟收盘确认再进。**",
    ):
        assert expected in body, expected
    assert okx.bars_asked == "15m"
    [call] = analyst.named("review")
    assert "结果 +1.00R" in call["figures"] and call["shared_with_group"] is True
    [row] = journal.reviews_between(0, CLOSE + 1)
    assert (row["r_multiple"], row["mae_r"], row["mfe_r"], row["stop_moves"]) == (1.0, 0.5, 1.5, 1)
    assert row["lesson"] == "突破单等 15 分钟收盘确认再进。" and row["risk_pct"] == 7.5
    assert row["source"] == "plan"
    assert reviewer.review(XAU, CLOSE + 20) is False and len(sent) == 1  # the same close is reviewed once


def test_without_a_record_of_the_close_nothing_is_sent_and_without_the_ai_the_figures_still_are():
    reviewer, journal, analyst, sent, okx = bench()
    okx.closed = []
    assert reviewer.review(XAU, CLOSE + 10) is False and sent == []
    okx.closed = [closed_row(type="1")]  # a partial close is not the end of the position
    assert reviewer.review(XAU, CLOSE + 10) is False

    from bot.analyst import AnalystUnavailable

    okx.closed = [closed_row()]
    analyst.error = AnalystUnavailable("x")
    assert reviewer.review(XAU, CLOSE + 10) is True
    assert "结果 +1.00R" in sent[0][1] and "这次 AI 没写出复盘" in sent[0][1]
    assert journal.reviews_between(0, CLOSE + 1)[0]["lesson"] == ""


def test_a_position_never_seen_with_a_stop_has_no_r_and_says_so():
    reviewer, journal, analyst, sent, okx = bench()
    journal2 = Journal(":memory:")
    journal2.record_snapshot(holding(None), OPEN + 30)
    reviewer._journal = journal2
    reviewer.review(XAU, CLOSE + 10)
    body = sent[0][1]
    assert "止损：机器人的记录里这个仓位一直没有止损" in body and "R" not in body.split("持仓期间")[0].split("已实现")[1]
    assert "开仓前 48 小时里，AI 没有给过这个品种带点位的分析" in body and "开仓单不是机器人下的" in body
    assert journal2.reviews_between(0, CLOSE + 1)[0]["source"] == "manual"


def test_the_pieces():
    snapshots = [
        {"ts": 1, "total_eq": 900.0, "positions": [{"inst_id": XAU, "direction": "short", "stops": [{"sl_trigger_px": 3900.0}, {"sl_trigger_px": 3890.0}]}]},
        {"ts": 2, "total_eq": 950.0, "positions": [{"inst_id": XAU, "direction": "short", "stops": []}]},
        {"ts": 3, "total_eq": 950.0, "positions": [{"inst_id": "BTC-USDT-SWAP", "direction": "short", "stops": []}]},
    ]
    trail = stop_trail(snapshots, XAU, "short")
    assert (trail.first, trail.last, trail.moves, trail.seen_from, trail.equity_at_open) == (3890.0, None, 1, 1, 900.0)
    assert r_multiple("short", 3850, 3830, 3860) == 2.0 and r_multiple("long", 3850, 3830, 3860) is None  # a stop in profit risks nothing
    assert bar_for(3600) == ("15m", 900) and bar_for(5 * 86400) == ("1H", 3600) and bar_for(100 * 86400) is None
    candles = [Candle(0, 10, 12, 8, 11), Candle(900_000, 11, 15, 10, 14)]
    assert excursions("short", 11.0, candles, 0, 900, 900) == (1.0, 3.0)  # only the first candle overlaps
    assert lesson_of("和计划比：好\n- **教训**：等回踩再进\n规则：无") == "等回踩再进" and lesson_of("没有") == ""


def test_lessons_and_the_track_record_reach_the_ai_only_where_they_may():
    cfg = make_cfg(timezone="UTC")
    journal = Journal(":memory:")
    assert feedback_text(cfg, journal, CLOSE, True) == ""
    journal.record_review(inst_id=XAU, direction="long", closed_at=CLOSE - 86400, r_multiple=-1.0, lesson="亚洲盘不追突破")
    journal.record_analysis("question", XAU, "long", 3850.0, [{"side": "long", "trigger": 3850, "target": 3900, "invalid": 3820, "confidence": 60}], ts=CLOSE - 2 * 86400)
    [call] = journal.open_calls()
    journal.update_call(call["id"], status="failed", closed_ts=CLOSE - 86400, result_r=-1.0)
    private = feedback_text(cfg, journal, CLOSE, True)
    assert "最近几笔平仓复盘记下的教训" in private and "01-14 XAU-USDT-SWAP 多 -1.0R：亚洲盘不追突破" in private
    assert "你自己最近 30 天的成绩单" in private and "命中 0 个，失效 1 个（0%）" in private
    assert "说把握50% 到 64%的 1 个：平均说 60%，实际命中 0 个（0%）" in private and "当时说把握 60%" in private
    shared_without_account = feedback_text(cfg, journal, CLOSE, False)
    assert "成绩单" in shared_without_account and "教训" not in shared_without_account
    assert feedback_text(make_cfg(close_review=False, track_record=False), journal, CLOSE, True) == ""


def test_the_week_in_figures():
    rows = [
        dict(realized=130.0, ccy="USDT", r_multiple=1.0, mae_r=0.5, mfe_r=1.5, stop_moves=1, first_stop=3820.0, opened_at=OPEN),
        dict(realized=-60.0, ccy="USDT", r_multiple=-1.0, mae_r=1.0, mfe_r=0.2, stop_moves=0, first_stop=3820.0, opened_at=OPEN + 86400 - 6 * 3600),
        dict(realized=10.0, ccy="USDT", r_multiple=None, mae_r=None, mfe_r=None, stop_moves=0, first_stop=None, opened_at=None),
    ]
    text = week_text(rows)
    assert "本周复盘过的平仓 3 笔：赚 2 笔，亏 1 笔，已实现合计 +80.00 USDT" in text
    assert "有 R 的 2 笔：平均 +0.00R（这就是期望值 expectancy），最好 +1.00R，最差 -1.00R" in text
    assert "平均最多逆向 0.75R，平均最多顺向 0.85R" in text and "止损动过的 1 笔，记录里没有止损的 1 笔" in text
    assert "按开仓时段（UTC）：欧洲盘 1 笔 +130.00；亚洲盘 1 笔 -60.00" in text and "周五 1 笔 +130.00" in text
    assert "按下单来源" not in text  # none of them tagged
    rows[0]["source"], rows[1]["source"] = "plan", "manual"
    assert "按下单来源：AI 情景 1 笔，赚 1 笔，合计 +130.00 USDT，平均 +1.00R；OKX 里手动下的 1 笔，赚 0 笔，合计 -60.00 USDT，平均 -1.00R；没有来源记录的 1 笔" in week_text(rows)
    assert week_text([]) == "本周没有复盘过的平仓。"


def test_a_close_starts_a_review_and_the_review_goes_where_the_account_may_be_shown():
    from bot.monitor import Event
    from bot.service import Reporter

    feishu = FakeFeishu()
    reporter = Reporter(make_cfg(), feishu, Gold(), FakeAnalyst(), Journal(":memory:"))
    started = []

    class Spy:
        def on_close(self, inst_id):
            started.append(inst_id)

    reporter.reviewer = Spy()
    reporter.notify_event(Event(kind="reduced", title="减仓", inst_id=XAU))
    reporter.notify_event(Event(kind="closed", title="平仓", inst_id=XAU))
    assert started == [XAU]
    reporter.send_review("复盘：XAU 多单", "结果 +1.00R")
    assert [sent.target for sent in feishu.sent if sent.title == "复盘：XAU 多单"] == ["ou_owner", "oc_group"]
    private_only = Reporter(make_cfg(group_show_pnl=False), FakeFeishu(), Gold(), FakeAnalyst(), Journal(":memory:"))
    private_only.send_review("复盘", "x")
    assert [sent.target for sent in private_only._feishu.sent] == ["ou_owner"]


def test_an_older_database_gets_the_new_column(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, analysis_id INTEGER NOT NULL, ts INTEGER NOT NULL,"
               " inst_id TEXT NOT NULL, side TEXT NOT NULL, trigger_px REAL NOT NULL, target_px REAL NOT NULL, invalid_px REAL NOT NULL,"
               " status TEXT NOT NULL DEFAULT 'open', triggered_ts INTEGER, closed_ts INTEGER, checked_ts INTEGER, result_r REAL)")
    db.commit()
    db.close()
    journal = Journal(path)
    journal.record_analysis("question", XAU, "long", 1.0, [{"side": "long", "trigger": 2, "target": 3, "invalid": 1, "confidence": 70}])
    assert journal.open_calls()[0]["confidence"] == 70
    Journal(path)  # opening it again changes nothing


def test_an_older_database_gets_the_source_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE orders (ts INTEGER NOT NULL, mode TEXT NOT NULL, action TEXT NOT NULL, inst_id TEXT NOT NULL DEFAULT '',"
               " summary TEXT NOT NULL, request TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '')")
    db.execute("INSERT INTO orders (ts, mode, action, summary, result) VALUES (1, 'live', 'open', 'old', 'ok')")
    db.commit()
    db.close()
    journal = Journal(path)
    journal.record_order("live", "open", "new", "ok", ts=2, source="own")
    assert [(row["summary"], row["source"]) for row in journal.orders_between(0, 3)] == [("old", ""), ("new", "own")]
