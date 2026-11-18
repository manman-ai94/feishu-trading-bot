"""Pure formatting: turns account data and events into text.

Two versions of everything:
  private -> with balances and PnL amounts. For the owner's direct chat, and
             for the group while GROUP_SHOW_PNL=true (the owner's default).
  public  -> instrument / direction / price levels only. For the group when
             GROUP_SHOW_PNL=false.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import AccountState, Position
from .monitor import Event
from .risk import AccountRisk, PositionRisk, leverage_line, liq_line, position_risk_lines

DISCLAIMER = "仅供参考，不构成投资建议。"


def _px(value: float | None) -> str:
    return "未知" if value is None else f"{value:,.2f}"


def _money(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "未知"
    return f"{value:+,.2f} USD" if signed else f"{value:,.2f} USD"


def _amount(value: float | None, ccy: str, signed: bool = False) -> str:
    """An amount in the currency it is counted in (a USDT contract's PnL is USDT, not USD)."""
    if value is None:
        return "未知"
    return f"{value:+,.2f} {ccy}" if signed else f"{value:,.2f} {ccy}"


def _side(position: Position) -> str:
    return "多" if position.direction == "long" else "空"


def _size(position: Position) -> str:
    text = f"{position.qty:g} 张"
    if position.coins is not None and position.coin_ccy:
        text += f"（合 {position.coins:,.8g} {position.coin_ccy}）"
    return text


def currency_lines(state: AccountState) -> list[str]:
    """One line per currency that holds something: equity, what is free, and the open PnL."""
    # The balance and the positions are two requests a moment apart, and the price moves in
    # between. Where the positions say what a currency's open PnL is, their sum is shown, so
    # the card never carries two different figures for the same thing.
    from_positions: dict[str, float | None] = {}
    for position in state.positions:
        known = from_positions.get(position.pnl_ccy, 0.0)
        from_positions[position.pnl_ccy] = None if known is None or position.upl is None else known + position.upl
    lines = []
    for item in state.snapshot.currencies[:4]:
        if item.eq is None or abs(item.eq_usd if item.eq_usd is not None else item.eq) < 1:
            continue
        parts = [f"权益 {item.eq:,.2f}"]
        if item.avail_eq is not None:
            parts.append(f"可用保证金 {item.avail_eq:,.2f}")
        elif item.avail_bal is not None:
            parts.append(f"可用余额 {item.avail_bal:,.2f}")
        upl = from_positions.get(item.ccy)
        if upl is None:
            upl = item.upl
        if upl:
            parts.append(f"浮动盈亏 {upl:+,.2f}")
        lines.append(f"{item.ccy}：" + "，".join(parts))
    return lines


def _stop(value: float | None) -> str:
    return "无" if value is None else f"{value:,.2f}"


def position_lines(position: Position, with_amounts: bool, risk: PositionRisk | None = None) -> list[str]:
    """`risk` adds how the position stands against the market's own movement; it is shown with the amounts only."""
    lines = [f"**{position.inst_id} {_side(position)}单**"]
    if with_amounts:
        size = f"- 数量 {_size(position)}"
        if position.lever:
            size += f"，杠杆 {position.lever:g} 倍"
        if position.notional_usd is not None:
            size += f"，名义价值 {_money(position.notional_usd)}"
        lines.append(size)
    lines.append(f"- 均价 {_px(position.avg_px)}，标记价 {_px(position.mark_px)}")
    if with_amounts:
        pnl = f"- 浮动盈亏 {_amount(position.upl, position.pnl_ccy, signed=True)}"
        if position.upl_ratio is not None:
            pnl += f"（按保证金算 {position.upl_ratio * 100:+.2f}%）"
        lines.append(pnl)
        if risk is not None and liq_line(risk):
            lines.append(f"- {liq_line(risk)}")
        elif position.liq_px:
            distance = position.liq_distance_pct
            suffix = f"，相距 {distance:.2f}%" if distance is not None else ""
            lines.append(f"- 预估强平价 {_px(position.liq_px)}{suffix}")
    lines.append(f"- 止损 {_stop(position.stop_loss)}，止盈 {_stop(position.take_profit)}")
    if with_amounts and risk is not None:
        lines.extend(f"- {line}" for line in position_risk_lines(risk)[1 if liq_line(risk) else 0 :])
    return lines


def status_markdown(
    state: AccountState, day_change: float | None, private: bool, show_amounts_in_group: bool = False, risk: AccountRisk | None = None,
    exposure: list[str] | None = None,
) -> str:
    """The status card body. `exposure`: what every stop would cost together (exposure.card_lines), shown with the amounts."""
    with_amounts = private or show_amounts_in_group
    lines: list[str] = []
    if with_amounts:
        snapshot = state.snapshot
        lines.append(f"**账户权益** {_money(snapshot.total_eq)}")
        lines.extend(f"- {line}" for line in currency_lines(state))
        if snapshot.upl is not None:
            lines.append(f"- 浮动盈亏 {_money(snapshot.upl, signed=True)}")
        if day_change is not None:
            lines.append(f"- 今日权益变化 {_money(day_change, signed=True)}（含出入金）")
        if snapshot.mgn_ratio is not None:
            lines.append(f"- 保证金率 {snapshot.mgn_ratio * 100:.0f}%（到 100% 开始强平）")
        if risk is not None and leverage_line(risk):
            lines.append(f"- {leverage_line(risk)}")
        lines.append("")
        if exposure:
            lines.extend(exposure)
            lines.append("")
    if not state.positions:
        lines.append("当前没有持仓。")
    for position in state.positions:
        lines.extend(position_lines(position, with_amounts, risk.of(position) if risk is not None else None))
        lines.append("")
    return "\n".join(lines).strip()


def account_text(state: AccountState, day_change: float | None, risk: AccountRisk | None = None, risk_pct: float | None = None) -> str:
    """Plain-text account summary for the AI (owner-only prompts)."""
    snapshot = state.snapshot
    lines = [f"账户权益 {_money(snapshot.total_eq)}"]
    if risk_pct:
        budget = f"，按现在的权益约 {_money(snapshot.total_eq * risk_pct / 100)}" if snapshot.total_eq else ""
        lines.append(
            f"交易员自己定的规则：一笔交易止损触发时最多亏权益的 {risk_pct:g}%{budget}。"
            "这条规则一律按“止损触发时账户比现在少多少”来对照，程序的提醒也是这样算的；"
            "从开仓均价算的盈亏可以作为补充提一句，但说超没超规则时只用从现在算的数，口径前后保持一致。"
        )
    if risk is not None:
        lines.append("（下面的实际杠杆、强平距离折合几天的波幅、止损触发时亏多少，是程序按 OKX 的数字算好的，可以直接引用。）")
    lines.extend(currency_lines(state))
    if snapshot.mgn_ratio is not None:
        lines.append(f"保证金率 {snapshot.mgn_ratio * 100:.0f}%（到 100% 开始强平）")
    if risk is not None and leverage_line(risk):
        lines.append(leverage_line(risk))
    if day_change is not None:
        lines.append(f"今日权益变化 {_money(day_change, signed=True)}（含出入金）")
    if not state.positions:
        lines.append("当前没有持仓。")
    for position in state.positions:
        parts = [
            f"{position.inst_id} {_side(position)}单 {_size(position)}",
            f"均价 {_px(position.avg_px)}",
            f"标记价 {_px(position.mark_px)}",
            f"浮动盈亏 {_amount(position.upl, position.pnl_ccy, signed=True)}",
        ]
        if position.notional_usd is not None:
            parts.append(f"名义价值 {_money(position.notional_usd)}")
        if position.lever:
            parts.append(f"杠杆 {position.lever:g} 倍")
        if position.liq_px:
            parts.append(f"预估强平价 {_px(position.liq_px)}")
        parts.append(f"止损 {_stop(position.stop_loss)}")
        parts.append(f"止盈 {_stop(position.take_profit)}")
        lines.append("，".join(parts))
        measured = risk.of(position) if risk is not None else None
        if measured is not None:
            lines.extend(f"  {line}" for line in position_risk_lines(measured))
    return "\n".join(lines)


def event_markdown(event: Event, private: bool) -> str:
    lines = event.private_lines if private else event.public_lines
    return "\n".join(f"- {line}" for line in lines if line)


def journal_text(events: list[dict], tz: ZoneInfo) -> str:
    if not events:
        return "没有记录。"
    lines = []
    for event in events:
        if event["kind"].startswith("job_"):
            continue
        moment = datetime.fromtimestamp(event["ts"], tz=tz).strftime("%m-%d %H:%M")
        lines.append(f"{moment} {event['summary']}")
    return "\n".join(lines) or "没有记录。"


def equity_text(series: list[tuple[int, float]]) -> str:
    if len(series) < 2:
        return "记录太少，无法比较。"
    values = [value for _, value in series]
    start, end = values[0], values[-1]
    return (
        f"期初 {_money(start)}，期末 {_money(end)}，变化 {_money(end - start, signed=True)}（含出入金）；"
        f"期间最高 {_money(max(values))}，最低 {_money(min(values))}。"
    )
