"""A rehearsal of every kind of order on OKX demo trading, started by the owner with one command.

The checks, previews and requests of the trade desk are tested against a
stand-in for OKX. Whether OKX itself accepts each request the way the desk
builds it is only known once the request has been sent. The rehearsal sends
each kind once, with the smallest size the contract allows, and reads the
account back after every step:

  1. a market order: a long of two lots
  2. a stop-loss and a take-profit on the position
  3. the stop-loss is moved
  4. the position is reduced by one lot (the stops must stay)
  5. the take-profit is taken away (the stop order is cancelled and placed again)
  6. a limit order that rests below the market
  7. that order is cancelled
  8. the position is closed (OKX must take the stop order away with it)

Rules:
  * Only on the demo account. It refuses unless TRADING=demo, both when it is
    asked for and again when the owner confirms.
  * Every step goes through the desk's own planning and sending, so the limits
    in .env apply to it and every request is in the order record.
  * It only starts on an instrument that holds nothing: no position, no order,
    no stop. Whatever is there afterwards is its own, and when a step fails it
    takes that away again and says what, if anything, is left.

It is the desk's own rehearsal and uses the desk's internals on purpose.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Callable

from . import __version__
from .config import TRADE_ACTIONS
from .models import InstrumentSpec, OrderInfo, Position
from .okx_client import OkxError, OkxNetworkError
from .trading import ACTION_NAMES, Command, Plan, Refused, Reply, Step, _amt, _contracts_text, _dec, _plain, _px, _round_to

log = logging.getLogger(__name__)

SETTING = "selftest_last"  # the outcome of the latest run, as JSON
STEPS = 8
LOTS = 2  # the position is this many times the smallest size, so that half of it can be closed
STOP_PCT = Decimal(2)  # how far from the market the stop-loss and the take-profit are placed
MOVED_STOP_PCT = Decimal("1.5")  # where the stop-loss is moved to
RESTING_PCT = Decimal(3)  # how far below the market the limit order waits
CHECK_TRIES = 6  # reads of the account after a step, a second apart, before the step counts as failed
NEVER_RUN = "还没有跑过。模拟盘（TRADING=demo）下私聊发“模拟盘自测”可以跑一遍"


class _Failed(Exception):
    """A step did not do what it should. The message is for the owner."""


@dataclass(frozen=True)
class _View:
    """What the instrument holds right now."""

    positions: list[Position]
    orders: list[OrderInfo]
    algos: list[dict]  # pending stop orders, as OKX lists them
    stops_known: bool = True


class SelfTest:
    def __init__(self, desk, inst_id: str = ""):
        self._desk = desk
        self._cfg = desk._cfg
        self._okx = desk._okx
        self._journal = desk._journal
        self._asked = inst_id
        self._inst_id = ""
        self._spec: InstrumentSpec | None = None
        self._report: list[str] = []
        self._at = ""  # the step that is running, for the report of a failure

    # ------------------------------------------------------------- the record

    def last_text(self) -> str:
        """The latest run, in a line: when, on which version, passed or where it stopped."""
        raw = self._journal.get_setting(SETTING, "")
        if not raw:
            return NEVER_RUN
        try:
            last = json.loads(raw)
            when = datetime.fromtimestamp(float(last["ts"]), tz=self._cfg.tz).strftime("%Y-%m-%d %H:%M")
            version, inst_id = str(last["version"]), str(last["inst_id"])
        except (ValueError, KeyError, TypeError):
            return NEVER_RUN
        if last.get("ok"):
            text = f"{when} 在 v{version} 上通过（{inst_id}，{STEPS} 步）"
        else:
            text = f"{when} 在 v{version} 上没有通过，停在{last.get('at') or '开始之前'}（{inst_id}）"
        if version != __version__:
            text += f"。现在是 v{__version__}，这个版本还没有跑过"
        return text

    def _remember(self, ok: bool) -> None:
        now = self._desk._clock()
        value = {"ts": now, "version": __version__, "inst_id": self._inst_id, "ok": ok, "at": "" if ok else self._at}
        self._journal.set_setting(SETTING, json.dumps(value, ensure_ascii=False))
        outcome = f"{STEPS} 步都通过" if ok else f"没有通过，停在{self._at}"
        # One line for the day's events, not one per step: the steps are in the order record.
        self._journal.record_event("trade", f"[模拟盘] 下单自测：{outcome}", self._inst_id, ts=now)
        log.info("demo self-test on %s: %s", self._inst_id, "passed" if ok else f"failed at step {len(self._report) + 1}")

    # ---------------------------------------------------------------- preview

    def plan(self) -> Plan:
        """Checks that the rehearsal can run and says what it would do. Sends nothing."""
        spec, last = self._ready()
        self._desk._plan(self._opening(spec, last))  # the limits in .env would refuse it here, before anything is sent
        size = spec.min_sz * LOTS
        value = size * spec.coins_per_contract * last
        lines = (
            f"品种 {self._inst_id}，现价 {_px(last, spec)}。用最小的数量：开 {_contracts_text(size, spec)}，"
            f"约 {_amt(value)} {spec.settle_ccy or 'USDT'}（名义价值）",
            f"一共 {STEPS} 步：市价开多、挂止损和止盈、改止损、减掉一半、取消止盈、限价挂单、撤单、平仓",
            "每一步发出去以后，我到 OKX 把结果读回来核对。哪一步不对就停下，把已经开的仓位和挂单收掉，再告诉你停在哪里",
            "全程只在模拟盘，不碰实盘账户。大约要半分钟，会用掉今天 2 次开仓次数（模拟盘的计数）",
        )
        return Plan("selftest", f"模拟盘自测 {self._inst_id}", lines, (), self._inst_id)

    def _ready(self) -> tuple[InstrumentSpec, Decimal]:
        """Everything that must hold before the first order. Raises Refused with the reason."""
        desk, cfg = self._desk, self._cfg
        if desk.mode != "demo":
            raise Refused("自测只在模拟盘上跑（TRADING=demo）。现在不是模拟盘，我不会在实盘账户上做自测。")
        closed = [ACTION_NAMES[action] for action in TRADE_ACTIONS if action not in cfg.trade_actions]
        if closed:
            raise Refused(
                f"自测要把每一种操作都跑一遍，现在没有开放：{'、'.join(closed)}。"
                "请先把服务器 .env 里的 TRADE_ACTIONS 设成 all，再发“模拟盘自测”。"
            )
        if desk._paused():
            raise Refused("交易已暂停（你发过“停止交易”），自测要开仓，所以现在不跑。发“恢复交易”解除后再发“模拟盘自测”。")
        if not (self._asked or cfg.tradable_instruments):
            raise Refused("配置里没有允许下单的品种（TRADE_INSTRUMENTS），自测没有地方跑。")
        self._inst_id = inst_id = self._asked or cfg.tradable_instruments[0]
        self._spec = spec = desk._tradable(inst_id)
        account, _ = desk._account()
        if account.spot_only:
            desk._account_config = None  # read it again next time: the mode may just have been changed
            raise Refused(desk._spot_mode_text())
        view = self._look()
        if view.positions or view.orders or view.algos:
            raise Refused(
                f"模拟账户的 {inst_id} 上现在有仓位、挂单或止盈止损单。自测会开仓再平掉，为了不动到你自己的单子，"
                "请先把它们平掉、撤掉，再发“模拟盘自测”。"
            )
        opens = self._journal.count_opens(desk._day_start(), desk._clock() + 1, "demo")
        if opens + 2 > cfg.trade_max_opens_per_day:
            raise Refused(
                f"自测要开仓 2 次，今天模拟盘已经开了 {opens} 次，每天的上限是 {cfg.trade_max_opens_per_day} 次"
                "（TRADE_MAX_OPENS_PER_DAY）。明天再跑，或者先把上限调高。"
            )
        return spec, _dec(self._okx.ticker(inst_id).last)

    def _opening(self, spec: InstrumentSpec, last: Decimal) -> Command:
        stop = self._below(last, STOP_PCT) if self._cfg.trade_require_stop else None
        return Command("open", inst_id=self._inst_id, direction="long", ord_type="market", size=spec.min_sz * LOTS, unit="contracts", sl=stop)

    # -------------------------------------------------------------------- run

    def run(self) -> Reply:
        """The eight steps, after the owner's confirmation. Raises Refused when it may no longer start."""
        spec, last = self._ready()
        started = self._desk._clock()
        failure = ""
        try:
            self._steps(spec, last)
        except _Failed as failed:
            failure = str(failed)
        except OkxError as exc:  # every read is caught where it is made; this is the net under them
            failure = f"OKX 返回了错误：{exc.message or '没有给出原因'}（代码 {exc.code}）"
        except OkxNetworkError:
            failure = "连不上 OKX。"
        inst_id = self._inst_id
        if not failure:
            self._remember(True)
            seconds = self._desk._clock() - started
            body = [
                f"**{inst_id}：{STEPS} 步都通过**",
                *self._report,
                "",
                f"OKX 模拟盘接受了每一种请求，读回来的结果都和预期一致。{inst_id} 上没有留下仓位、挂单和止盈止损单。"
                + (f"用时 {seconds:.0f} 秒。" if seconds >= 1 else ""),
                "这说明下单的通道是通的，和行情判断无关。实盘发的是同样的请求，成交价和滑点会和模拟盘不同。",
            ]
            return Reply("\n".join(body), title="模拟盘自测 · 通过", template="green")
        self._remember(False)
        body = [
            f"**{inst_id}：停在{self._at}**",
            *self._report,
            f"- ❌ {self._at}：{failure}",
            "",
            "**收尾**",
            *(f"- {line}" for line in self._clean_up()),
            ("后面的步骤没有跑。" if len(self._report) + 1 < STEPS else "") + "把这张卡片发到项目里，Claude 会接着查原因。",
        ]
        return Reply("\n".join(body), title="模拟盘自测 · 没有通过", template="red")

    def _steps(self, spec: InstrumentSpec, last: Decimal) -> None:
        inst_id = self._inst_id
        size, one = spec.min_sz * LOTS, spec.min_sz
        stop, target = self._below(last, STOP_PCT), self._above(last, STOP_PCT)
        moved = self._below(last, MOVED_STOP_PCT)
        resting = self._below(last, RESTING_PCT)

        self._step("市价开多", self._opening(spec, last), lambda view: self._holding(view, size)[1:])
        self._step(
            "挂止损和止盈", Command("stop", inst_id=inst_id, sl=stop, tp=target),
            lambda view: self._stops_are(view, size, stop, target),
        )  # fmt: skip
        self._step("改止损", Command("stop", inst_id=inst_id, sl=moved), lambda view: self._stops_are(view, size, moved, target))
        self._step(
            "减掉一半", Command("close", inst_id=inst_id, size=one, unit="contracts", reduce=True),
            lambda view: self._stops_are(view, size - one, moved, target),
        )  # fmt: skip
        self._step(
            "取消止盈", Command("stop", inst_id=inst_id, clear_tp=True),
            lambda view: self._stops_are(view, size - one, moved, None),
        )  # fmt: skip
        limit_stop = self._below(resting, STOP_PCT) if self._cfg.trade_require_stop else None
        found: list[tuple[OrderInfo | None, str, str]] = []
        self._step(
            "限价挂单",
            Command("open", inst_id=inst_id, direction="long", ord_type="limit", size=one, unit="contracts", px=resting, sl=limit_stop),
            lambda view: found.append(self._resting(view, one, resting)) or found[-1][1:],
        )
        order = found[-1][0]  # the check passed, so this is the order it saw
        self._step(
            "撤单", Command("cancel", inst_id=inst_id, ord_id=order.ord_id),
            lambda view: (f"订单 {order.ord_id} 还挂着", "") if view.orders else ("", "挂单已经不在了"),
        )  # fmt: skip
        self._step("平仓", Command("close", inst_id=inst_id), self._nothing_left)

    def _step(self, name: str, command: Command, check: Callable[[_View], tuple[str, str]]) -> None:
        """Plans, sends and checks one step. Raises _Failed with what went wrong."""
        number = len(self._report) + 1
        self._at = f"第 {number} 步（{name}）"
        try:
            plan = self._desk._plan(command)
        except Refused as refused:
            raise _Failed(f"我自己的检查没有放行，这一步什么都没发给 OKX。原因：{refused}") from None
        except OkxError as exc:
            raise _Failed(f"准备的时候 OKX 返回了错误，这一步什么都没发：{exc.message or '没有给出原因'}（代码 {exc.code}）") from None
        except OkxNetworkError:
            raise _Failed("准备的时候连不上 OKX，这一步什么都没发。") from None
        result, heading, _, lines = self._desk._carry_out(replace(plan, title=f"自测：{plan.title}"), in_diary=False)
        sent = "；".join(lines)
        if result != "ok":
            raise _Failed(f"{heading}。{sent}")
        seen = self._settled(check)
        self._report.append(f"- ✅ 第 {number} 步 {name}：{sent}。核对：{seen}")

    def _settled(self, check: Callable[[_View], tuple[str, str]]) -> str:
        """Reads the account until it shows what the step should have done. OKX can take a moment."""
        problem = ""
        for attempt in range(CHECK_TRIES):
            if attempt:
                self._desk._sleep(1.0)
            try:
                view = self._look()
            except OkxError as exc:
                problem = f"读 OKX 的时候出错：{exc.message or '没有给出原因'}（代码 {exc.code}）"
                continue
            except OkxNetworkError:
                problem = "连不上 OKX，读不到结果"
                continue
            problem, seen = check(view)
            if not problem:
                return seen
        raise _Failed(f"OKX 接受了这个请求，可是读回来的结果不对：{problem}")

    # ----------------------------------------------------------------- checks

    def _look(self) -> _View:
        inst_id = self._inst_id
        state = self._okx.account_state()
        return _View(
            positions=[p for p in state.positions if p.inst_id == inst_id],
            orders=list(self._okx.pending_orders(inst_id)),
            algos=[row for row in self._okx.pending_stops() if row.get("instId") == inst_id],
            stops_known=state.stops_known,
        )

    def _holding(self, view: _View, size: Decimal) -> tuple[Position | None, str, str]:
        """(the position, what is wrong, what was seen)."""
        if len(view.positions) != 1:
            return None, f"应该有 1 个仓位，读到 {len(view.positions)} 个", ""
        position = view.positions[0]
        if position.direction != "long" or _dec(position.qty) != size:
            side = "多" if position.direction == "long" else "空"
            return None, f"应该是多单 {_plain(size)} 张，读到的是{side}单 {position.qty:g} 张", ""
        return position, "", f"多单 {_plain(size)} 张"

    def _stops_are(self, view: _View, size: Decimal, sl: Decimal | None, tp: Decimal | None) -> tuple[str, str]:
        position, problem, seen = self._holding(view, size)
        if position is None:
            return problem, ""
        if not view.stops_known:
            return "止盈止损单暂时读不到", ""
        want = f"止损 {self._show(sl)}，止盈 {self._show(tp)}"
        stops = position.stops
        if len(stops) == 1 and self._same(stops[0].sl_trigger_px, sl) and self._same(stops[0].tp_trigger_px, tp):
            return "", f"{seen}，{want}"
        got = "；".join(f"止损 {self._show(s.sl_trigger_px)}，止盈 {self._show(s.tp_trigger_px)}" for s in stops)
        return f"应该有一张止盈止损单（{want}），读到的是：{got or '没有止盈止损单'}", ""

    def _resting(self, view: _View, size: Decimal, px: Decimal) -> tuple[OrderInfo | None, str, str]:
        mine = [
            order for order in view.orders
            if order.side == "buy" and not order.reduce_only and self._same(order.px, px) and _dec(order.sz) == size
        ]  # fmt: skip
        if len(mine) != 1 or len(view.orders) != 1:
            return None, f"应该有 1 张限价 {self._show(px)} 的买单，读到 {len(view.orders)} 张挂单", ""
        return mine[0], "", f"买单挂在 {self._show(px)}，{_plain(size)} 张，订单号 {mine[0].ord_id}"

    @staticmethod
    def _nothing_left(view: _View) -> tuple[str, str]:
        left = _left(view)
        return (f"应该什么都不剩，读到{left}", "") if left else ("", "仓位、挂单和止盈止损单都不在了")

    def _same(self, value, wanted: Decimal | None) -> bool:
        if wanted is None or not value:
            return wanted is None and not value
        return abs(_dec(value) - wanted) < self._spec.tick_sz / 2

    def _show(self, value) -> str:
        return _px(value, self._spec) if value else "无"

    def _below(self, price: Decimal, pct: Decimal) -> Decimal:
        return _round_to(price * (100 - pct) / 100, self._spec.tick_sz)

    def _above(self, price: Decimal, pct: Decimal) -> Decimal:
        return _round_to(price * (100 + pct) / 100, self._spec.tick_sz)

    # --------------------------------------------------------------- clean-up

    def _clean_up(self) -> list[str]:
        """After a failed step: takes away what the rehearsal left. The instrument held nothing when it began."""
        inst_id = self._inst_id
        notes: list[str] = []
        for name, command in (("撤掉挂单", Command("cancel", inst_id=inst_id)), ("平掉仓位", Command("close", inst_id=inst_id))):
            try:
                plan = self._desk._plan(command)
            except Refused:
                continue  # nothing of that kind is there
            except (OkxError, OkxNetworkError) as exc:
                notes.append(f"{name}：读 OKX 的时候出错，没有做（{exc}）")
                continue
            result, heading, _, lines = self._desk._carry_out(replace(plan, title=f"自测收尾：{plan.title}"), in_diary=False)
            notes.append(f"{name}：{'；'.join(lines)}" if result == "ok" else f"{name}：{heading}。{'；'.join(lines)}")
        try:
            view = self._look()
            if view.algos and not view.positions:
                # A stop order whose position is gone. Closing takes them away, so this should never be needed.
                items = [[inst_id, str(row.get("algoId", ""))] for row in view.algos]
                step = Step("cancel_stops", {"items": items}, "撤掉留下的止盈止损单")
                plan = Plan("stop", "自测收尾：撤掉留下的止盈止损单", (), (step,), inst_id)
                result, heading, _, lines = self._desk._carry_out(plan, in_diary=False)
                notes.append("；".join(lines) if result == "ok" else f"撤掉留下的止盈止损单：{heading}。{'；'.join(lines)}")
                view = self._look()
        except (OkxError, OkxNetworkError) as exc:
            notes.append(f"**最后读不到 OKX（{exc}），不确定 {inst_id} 上还有没有东西。请在 OKX 的模拟交易里看一眼。**")
            return notes
        left = _left(view)
        if left:
            notes.append(f"**模拟账户的 {inst_id} 上还留着{left}，我没能收掉。请在 OKX 的模拟交易里手动处理。**")
        else:
            notes.append(f"已经收拾干净：模拟账户的 {inst_id} 上没有留下仓位、挂单和止盈止损单。")
        return notes


def _left(view: _View) -> str:
    """What the instrument still holds, in words. "" when it holds nothing."""
    parts = []
    if view.positions:
        parts.append(f"仓位 {len(view.positions)} 个")
    if view.orders:
        parts.append(f"挂单 {len(view.orders)} 张")
    if view.algos:
        parts.append(f"止盈止损单 {len(view.algos)} 张")
    return "、".join(parts)
