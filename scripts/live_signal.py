"""模拟盘入口：判定今天该不该跑 → 拉实时报价 → 算当期信号 → 推送 → 记账。

用法
----
python -m scripts.live_signal --dry-run                 # 只看不发，本地验证用
python -m scripts.live_signal                           # 正常跑（GitHub Actions 里就是这个）
python -m scripts.live_signal --now "2026-11-30 15:30"  # 假装现在是某个时刻，便于复现
python -m scripts.live_signal --force --dry-run         # 无视窗口与幂等键，强制算一次

为什么入口要自己做「该不该跑」的判断
------------------------------------
cron 只能写死 UTC 时间，但收盘时间随夏令时/早收日变化，Actions 自身还会漂 5~30 分钟。
所以 workflow 里挂多个 cron（冬令/夏令各一组）作为触发器，**是否真的执行由这里判断**：
取当日收盘时间、倒推 30 分钟，只有落在窗口内才算。这样早收日（13:00 ET）不用改代码，
夏令时切换也不会漏跑或早跑一小时。
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys

import numpy as np
import pandas as pd

from quant.live import books, calendar, journal, notify, pool, quotes, signal as signal_mod

log = logging.getLogger(__name__)

LAG_WARN_MIN = 10.0        # 报价滞后超过这个数就在推送里明确标注
LAG_REFUSE_MIN = 45.0      # 滞后到这个程度已不能代表 15:30，拒绝出信号
DATA_LAG_ABORT_TD = 2      # 行情面板缺到 2 个交易日就中止（缺 1 天只告警）
MARK_OK, MARK_NO = "\u2713", "\u2717"


def _now(args) -> dt.datetime:
    if not args.now:
        return dt.datetime.now(dt.timezone.utc)
    txt = args.now.strip().replace("/", "-")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(txt, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            # 没写时区的按 ET 处理（人写「15:30」想的就是 ET）
            return parsed.replace(tzinfo=calendar.ET).astimezone(dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    raise SystemExit(f"--now 无法解析：{args.now!r}")


def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:+.2f}%"


def _next_rebalance(after: dt.date) -> dt.date:
    """下一个调仓日（月末最后交易日），用于周报里给预期。"""
    d = calendar.next_trading_day(after)
    for _ in range(45):
        if calendar.next_trading_day(d).month != d.month:
            return d
        d = calendar.next_trading_day(d)
    return d


def _pool_lines(pctx: dict) -> list[str]:
    """候选池状态与指数调整。这一段不是为了好看，是为了让「池子过期」看得见。"""
    out = ["", "**候选池**：" + pctx["summary"]]
    ch = pctx["changes"]
    if ch is None or ch.empty:
        out.append("指数调整（近 45 天）：无")
    else:
        add = ch[ch["action"] == "add"]
        dele = ch[ch["action"] == "delete"]
        move = ch[ch["action"] == "move"]
        parts = []
        if len(add):
            parts.append("纳入 " + "、".join(f"{r.symbol}({r.sector_etf})" for r in add.itertuples()))
        if len(dele):
            parts.append("调出 " + "、".join(r.symbol for r in dele.itertuples()))
        if len(move):
            parts.append("换板块 " + "、".join(f"{r.symbol}→{r.sector_etf}" for r in move.itertuples()))
        out.append("指数调整（近 45 天）：" + "；".join(parts))
    for w in pctx["warnings"]:
        out.append(f"⚠ {w}")
    return out


def _pool_context(sig, pos: books.Position, today_et) -> dict:
    """候选池快照年龄 + 近 45 天指数调整 + 持仓是否已被调出。

    「持仓被调出」必须单独说一句：它和「排名掉下去所以换仓」是两回事，前者是硬约束
    （指数基金必须卖），后者只是相对强弱变化。混在一起会让人误判策略在做什么。
    """
    warnings: list[str] = []
    try:
        members = pool.load()
        age = pool.snapshot_age_days(today_et)
    except SystemExit as exc:
        return {"summary": f"读取失败（{exc}）", "changes": None, "warnings": [], "held_out": False}

    summary = f"{len(members)} 只"
    if sig.pool_as_of:
        summary += f"，as of {sig.pool_as_of}"
    if age is not None:
        summary += f"（{age} 天前）"
        if age > 10:
            warnings.append(f"候选池快照已 {age} 天未更新，指数调入调出可能漏记")

    held_out = False
    if not pos.is_empty() and pos.symbol not in set(members["symbol"]):
        held_out = True
        warnings.append(f"当前持仓 {pos.symbol} 已不在板块 ETF 成分中（指数调出），本期必须换出")

    try:
        changes = pool.recent_changes(45, today_et)
    except Exception as exc:                        # noqa: BLE001
        log.warning("读变更日志失败：%s", type(exc).__name__)
        changes = None
    return {"summary": summary, "changes": changes, "warnings": warnings, "held_out": held_out}


def render_rebalance(sig, action: str, action_detail: str, pos: books.Position,
                     holding_ret: float | None, ctx: dict, pctx: dict) -> tuple[str, str]:
    title = f"[模拟盘] {sig.asof:%m-%d} 调仓：{action_detail}"
    lines = [
        f"## {sig.asof:%Y-%m-%d} 调仓信号",
        "",
        f"**动作**：{action} —— {action_detail}",
        f"**板块**：{sig.sector}（RSRS {sig.sector_scores.get(sig.sector, float('nan')):.2f}）",
    ]
    if not pos.is_empty():
        lines.append(f"**上期持仓**：{pos.symbol}（{pos.sector}，{pos.entry_date} 建仓 "
                     f"@ ${pos.entry_price:.2f}），持有期收益 {_fmt_pct(holding_ret)}")

    # 与周报共用同一段「本期筛选」：牛熊 + 板块前三 + 排序分前 3 的趋势五闸 + 各层数量。
    # 调仓那天才是真成交，看不到每道闸的具体数值说不过去。
    lines += _screen_lines(sig, decided=True)

    lines += _pool_lines(pctx)

    lines += ["", "---", "**数据状态**"]
    lines.append(f"- 报价滞后 {ctx['lag']}")
    lines.append(f"- 换手率覆盖 {ctx['turn_cov']}，全市场中位 {ctx['turn_med']}")
    for n in sig.notes:
        lines.append(f"- {n}")
    if ctx.get("warnings"):
        for w in ctx["warnings"]:
            lines.append(f"- ⚠ {w}")

    guard = f"候选池已按 {sig.min_mcap / 1e8:.0f} 亿美元市值门槛过滤（剔除换手率分母失真的小票）；" \
        if sig.min_mcap > 0 else ""
    lines += ["", f"**口径提醒**：{guard}盘中最高/最低比回测的完整日振幅略窄；单票满仓，"
                 f"持仓期跌破买入价 {signal_mod.STOP_LOSS:.0%} 当日止损"
                 f"（割肉日 < 当月 {signal_mod.STOP_REPICK_DOM} 号则重选、排除原票），"
                 "执行价以你的盘口为准。"]
    return title, "\n".join(lines)


def _num(v, digits: int = 2) -> str:
    return "—" if v is None or not np.isfinite(v) else f"{v:.{digits}f}"


def _regime_line(sig) -> str:
    ratio = "" if not np.isfinite(sig.bear_ratio) else f"，21 日跌破占比 {sig.bear_ratio:.0%}"
    if sig.bull:
        return f"- 牛熊：**牛市**（SPY vs MA200{ratio}）→ 走个股链路"
    return f"- 牛熊：**熊市**（SPY vs MA200{ratio}）→ 只做板块 ETF，不碰个股"


def _screen_lines(sig, decided: bool = False) -> list[str]:
    """本期筛选明细：牛熊 + 结论 + 板块排名 + 趋势五闸每一项的取值 + 各层数量。

    周报原先只报持仓，空仓时就两行，读的人分不清「没有合格标的」和「还没建仓」，
    也看不到候选究竟卡在哪一步。这里把每一步的具体数字摊开。

    ``decided=True`` 用在调仓推送：那天是真的要下单，「若今日为调仓日」的假设语气
    要换成「买入」；其余内容与周报完全一致，两种推送看同一张表。
    """
    out = ["", "**本期筛选**", _regime_line(sig)]

    if sig.pick:
        lead = "结论：买入" if decided else "结论：若当日为调仓日，买入"
        if sig.pick_kind == "etf":
            rsrs = float(sig.sector_scores.get(sig.sector, float("nan")))
            out.append(f"- {lead} **{sig.pick}**（板块 ETF，{sig.sector}）\u2014\u2014"
                       f"熊市双榜交集第一名，RSRS {rsrs:.2f}；"
                       f"参考价 ${sig.price:.2f}（盘中时点价，非收盘价）")
        else:
            rank = next((c.sector_rank for c in sig.candidates if c.symbol == sig.pick), 1)
            sc = next((c.score for c in sig.candidates if c.symbol == sig.pick), float("nan"))
            rsrs = float(sig.sector_scores.get(sig.sector, float("nan")))
            out.append(f"- {lead} **{sig.pick}**（{sig.sector}）满足全部条件\u2014\u2014"
                       f"板块 RSRS 第 {rank}（{rsrs:.2f}），排序分 turn20\u00b2/turn120 = {sc:.4f}，"
                       f"过趋势五闸；参考价 ${sig.price:.2f}（盘中时点价，非收盘价）")
    else:
        tail = "，本次调仓空仓" if decided else ""
        if sig.bull:
            out.append("- 结论：前三个板块 \u00d7 排序分前 3 全部未过趋势五闸，"
                       f"当期无合格标的{tail}（牛市当天起逐日补选，见下方说明）")
        else:
            out.append(f"- 结论：熊市双榜（RSRS 前 3 \u2229 120 日斜率前 3）无合格标的，"
                       f"当期空仓{tail}")

    if sig.bull:
        top = signal_mod.top_sectors(sig)
        out.append("- 板块 RSRS 前三：" + " / ".join(f"{s} {v:.2f}" for s, v in top))
    else:
        bs = sig.bear_scores if sig.bear_scores is not None else pd.Series(dtype="float64")
        if len(bs):
            out.append("- 熊市双榜交集（RSRS \u2229 120 日斜率）："
                       + " / ".join(f"{s} {v:.2f}" for s, v in bs.items()))
        else:
            out.append("- 熊市双榜交集为空：RSRS 前 3 与 120 日加权斜率前 3 没有共同板块")

    if sig.candidates:
        out += ["", "| 板块序 | 板块 | 代码 | 排序分 | turn20 | turn120 | MA20 | 20斜率 "
                    "| MA60 | 60斜率 | MA50 | MA200 | ADX | 五闸 |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for c in sig.candidates[:9]:
            mark = MARK_OK if c.ok else MARK_NO
            g = c.gate
            out.append(
                f"| {c.sector_rank} | {c.sector} | {c.symbol} | {c.score:.4f} | "
                f"{c.turnover:.2%} | {_num(c.turnover120, 4)} | "
                f"{_num(g.get('ma20'))} | {_num(g.get('s20'), 3)} | "
                f"{_num(g.get('ma60'))} | {_num(g.get('s60'), 3)} | "
                f"{_num(g.get('ma50'))} | {_num(g.get('ma200'))} | "
                f"{_num(g.get('adx'), 1)} | {mark} |")

    tbl = sig.pool_table
    if len(tbl):
        cap_ok = tbl["mcap_ok"] == True                    # noqa: E712  列里是 object 布尔
        turn_ok = tbl["turn_ok"] == True                   # noqa: E712
        gate_ok = tbl["gate_ok"] == True                   # noqa: E712
        both = cap_ok & turn_ok
        out.append("")
        out.append(f"- 数量：候选池 {len(tbl)} 只 \u2192 市值+换手双合规 {int(both.sum())} 只 "
                   f"\u2192 过趋势五闸 {int((both & gate_ok).sum())} 只 "
                   f"\u2192 进板块 TOP3 {int((tbl['in_top3'] == True).sum())} 只 "  # noqa: E712
                   f"\u2192 选中 {int((tbl['selected'] == True).sum())} 只")  # noqa: E712
        out.append(f"- 参考：全池过趋势五闸 {int(gate_ok.sum())} 只"
                   f"（含未过市值/换手门槛的）")
        out.append("- 口径：先按板块 RSRS 取前 3 个板块，每个板块内按 turn20\u00b2/turn120 "
                   "降序取前 3 只，只在这最多 9 只里判「MA20>MA60、两条均线 5 日斜率为正、"
                   "收盘>MA50、MA50>MA200、ADX(14)>20」，命中即止")
        fails = tbl[tbl["reason"] == "gate_fail"] if "reason" in tbl.columns else tbl.iloc[0:0]
        if len(fails):
            out.append("- 未过闸示例：" + "；".join(
                f"{r.symbol}（{r.gate_why}）" for r in fails.head(3).itertuples()))
    return out


def render_week(sig, pos: books.Position, holding_ret: float | None, ctx: dict,
                pctx: dict) -> tuple[str, str]:
    nxt = _next_rebalance(sig.asof.date())
    title = f"[模拟盘] {sig.asof:%m-%d} 周报"
    body = [f"## {sig.asof:%Y-%m-%d} 周报", ""]
    if pos.is_empty():
        rf = books.load_refill()
        body.append("**当前空仓**：账本尚无持仓 \u2014\u2014 调仓日建仓（牛市空仓后逐日补选），"
                    "周报只报不平账。")
        if rf.get("open"):
            body.append(f"- 牛市补选中：自 {rf.get('since', '?')} 起逐日重挑，"
                        f"窗口到 {rf.get('until', '?')}")
        body.append(f"- 下个调仓日：{nxt:%Y-%m-%d}（收盘前 30 分钟）")
        if sig.pick:
            body.append(f"- 若今日就是调仓日，会买入：{sig.pick}（{sig.sector}）")
    else:
        body += [
            f"**持仓**：{pos.symbol}（{pos.sector}）",
            f"- 建仓：{pos.entry_date} @ ${pos.entry_price:.2f}",
            f"- 持有期收益：{_fmt_pct(holding_ret)}",
            f"- 累计净值：{pos.nav:.4f}",
            f"- 下个调仓日：{nxt:%Y-%m-%d}（收盘前 30 分钟）",
        ]
    body += _screen_lines(sig)
    body += _pool_lines(pctx)
    if ctx.get("warnings"):
        body += [""] + [f"- ⚠ {w}" for w in ctx["warnings"]]
    body += ["", "---", f"报价滞后 {ctx['lag']}；换手率覆盖 {ctx['turn_cov']}。"]
    return title, "\n".join(body)


def render_refill(sig, action: str, action_detail: str, pos: books.Position, ctx: dict,
                  pctx: dict, rf: dict) -> tuple[str, str]:
    """空仓后逐日重挑的「补选成交」推送：和调仓推送同一张筛选表，但把触发条件写清楚。"""
    stop_origin = str(rf.get("origin")) == "stop"
    head = "止损后重选" if stop_origin else "牛市补选"
    title = f"[模拟盘] {sig.asof:%m-%d} {head}：{action_detail}"
    trigger = (f"{rf.get('since', '?')} 触发 10% 止损割肉，明日起逐日重挑，"
               f"**排除 {rf.get('exclude', '?')}**；"
               if stop_origin else
               f"{rf.get('since', '?')} 调仓日一只都没过关，牛市里逐日重挑；")
    lines = [
        f"## {sig.asof:%Y-%m-%d} {head}",
        "",
        f"**动作**：{action} —— {action_detail}",
        f"**触发**：{trigger}"
        f"窗口到下一个调仓日 {rf.get('until', '?')} 为止（仍找不到就整段空仓）",
        f"**板块**：{sig.sector}（RSRS {sig.sector_scores.get(sig.sector, float('nan')):.2f}）",
    ]
    lines += _screen_lines(sig, decided=True)
    lines += _pool_lines(pctx)

    lines += ["", "---", "**数据状态**"]
    lines.append(f"- 报价滞后 {ctx['lag']}")
    lines.append(f"- 换手率覆盖 {ctx['turn_cov']}，全市场中位 {ctx['turn_med']}")
    for n in sig.notes:
        lines.append(f"- {n}")
    for w in ctx.get("warnings") or []:
        lines.append(f"- ⚠ {w}")

    if stop_origin:
        lines += ["", "**口径提醒**：本条对应回测的 `--stop-repick-dom 11`——止损日在该自然月 "
                      f"{signal_mod.STOP_REPICK_DOM} 号之前才逐日重挑，且不再买回刚割的那只；"
                      "单票满仓，执行价以你的盘口为准。"]
    else:
        lines += ["", "**口径提醒**：补选对应回测的 ``--bull-refill``——只在「调仓日空仓」时才逐日"
                      "重挑，不追已经走完的行情；单票满仓，执行价以你的盘口为准。"]
    return title, "\n".join(lines)


def render_stop(day: str, pos: books.Position, holding_ret: float, price: float,
                allow_repick: bool, nxt: dt.date) -> tuple[str, str]:
    """止损推送。说清三件事：割了什么、按什么规则割的、接下来选不选。"""
    title = f"[模拟盘] {day[5:]} 止损：卖出 {pos.symbol}"
    dom = int(day[8:10])
    lines = [
        f"## {day} 止损信号",
        "",
        f"**动作**：止损卖出 —— {pos.symbol} @ ${price:.2f}",
        f"- 建仓：{pos.entry_date} @ ${pos.entry_price:.2f}（{pos.sector}）",
        f"- 持有期收益：**{holding_ret:+.2%}**（阈值 −{signal_mod.STOP_LOSS:.0%}，复权口径）",
        f"- 累计净值：{pos.nav:.4f}（这一段亏损已并入）",
        "",
        f"**触发**：现价相对买入价跌幅达到 {signal_mod.STOP_LOSS:.0%}（回测 `--stop-loss`）。"
        f"回测在**收盘价**上判定，实盘用收盘前 30 分钟现价近似，可能差最后半小时的波动。",
        "",
        "**下一步**：",
    ]
    if allow_repick:
        lines.append(f"- 割肉日 {dom} 号 < {signal_mod.STOP_REPICK_DOM} 号 → 明日起逐日重新选股，"
                     f"**排除 {pos.symbol}**（不再买回同一只），选到就买；"
                     f"到 {nxt:%Y-%m-%d} 调仓日还没选到就空仓")
    else:
        why = (f"割肉日 {dom} 号已到 {signal_mod.STOP_REPICK_DOM} 号之后"
               if dom >= signal_mod.STOP_REPICK_DOM else "本期已用过一次止损重选额度")
        lines.append(f"- {why} → 本期不再选股，空仓到 {nxt:%Y-%m-%d} 调仓日")
    lines += ["", "**口径提醒**：单票 10% 硬止损、不加仓位管理；执行价以你的盘口为准。"]
    return title, "\n".join(lines)


def _run_stop(j, args, today_et: dt.date) -> int:
    """持仓期每个交易日收盘前的止损检查（对应回测 `--stop-loss 0.10 --stop-repick-dom 11`）。

    只做一件事：拿持仓票现价和买入价比。所以这里**不跑全池 RSRS**，一个报价就够，
    避免每天多烧一次 500 只票的行情。
    """
    day = today_et.isoformat()
    pos = books.load_position()
    if pos.is_empty():
        return j.finish(0, "noop", "stop_no_position")

    sym = pos.symbol
    if sym in signal_mod.SECTOR_ETFS:
        # 回测里熊市买的板块 ETF 不参与止损（`not buy_etf` 条件）
        return j.finish(0, "noop", f"stop_skip_etf:{sym}")

    quote_map, _qnotes = quotes.fetch_quotes([sym])
    if sym not in quote_map:
        return _abort(j, args, "abort", "stop_no_quote",
                      f"止损检查拿不到 {sym} 的报价，不凭旧价割肉，本次跳过")

    factors = signal_mod.current_factors([sym], pd.Timestamp(today_et))
    hit, ret, price = books.stop_triggered(pos, quote_map, factors, signal_mod.STOP_LOSS)
    j.extra["stop"] = {"symbol": sym, "hit": bool(hit),
                       "ret": None if ret is None else round(float(ret), 4)}
    if ret is None:
        print(f"{sym} 复权因子缺失，本次不做止损判定（不凭未复权价割肉）")
        return j.finish(0, "noop", "stop_no_factor")
    if not hit:
        print(f"{sym} 持有期收益 {ret:+.2%}，未触及 -{signal_mod.STOP_LOSS:.0%}，继续持有。")
        return j.finish(0, "noop", f"stop_hold_{ret * 100:+.2f}pct")

    nxt = _next_rebalance(today_et)
    allow = (today_et.day < signal_mod.STOP_REPICK_DOM) and int(pos.stop_retry) == 0
    title, content = render_stop(day, pos, float(ret), float(price), allow, nxt)
    content = f"{content}\n\n`run {j.run_id}`"

    print("\n" + "=" * 72)
    print(title)
    print("-" * 72)
    print(content)
    print("=" * 72)

    j.snapshot_stop(quote_map, title=title, content=content,
                    action="止损卖出", detail=f"卖出 {sym} @ ${float(price):.2f}",
                    pos=pos, holding_ret=float(ret),
                    extra={"threshold": signal_mod.STOP_LOSS,
                           "repick_dom": signal_mod.STOP_REPICK_DOM,
                           "allow_repick": bool(allow)})

    if args.dry_run:
        print("\n[dry-run] 未推送、未记账。")
        return j.finish(0, "dryrun", "dry_run")

    ok = notify.send(title, content)
    j.notify = "ok" if ok else "failed"
    j.patch_decision(notify_ok=ok)
    if not ok:
        return j.finish(1, "error", "notify_failed")

    books.mark_pushed("stop", day, {"symbol": sym, "ret": round(float(ret), 4)})
    books.record_stop(pos, day=day, action=f"止损卖出 {sym}", price=float(price),
                      holding_return=float(ret),
                      note=f"{ret:+.2%} 触及 -{signal_mod.STOP_LOSS:.0%}",
                      retry_used=1)
    if allow:
        books.set_refill(since=day, until=nxt.isoformat(), bull=True,
                         exclude=sym, origin="stop")
        print(f"已开启止损重选：明日起逐日重挑（排除 {sym}），窗口到 {nxt:%Y-%m-%d}")
    else:
        books.clear_refill("止损后不再重选，空仓到月末")
    return j.finish(0, "ok", f"stop_sold_{sym}_{ret * 100:+.1f}pct")


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟盘信号（收盘前 30 分钟）")
    ap.add_argument("--now", default="", help="模拟当前时刻（默认取系统时钟），时区缺省按 ET")
    ap.add_argument("--kind", default="auto",
                    choices=["auto", "rebalance", "week", "refill", "stop"],
                    help="auto = 按日历判断；rebalance/week = 强制按该类推送；"
                         "refill = 空仓后逐日重挑（牛市补选 / 止损重选）；"
                         "stop = 只做持仓止损检查（每个交易日收盘前）")
    ap.add_argument("--force", action="store_true", help="无视执行窗口与幂等键")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送、不记账")
    ap.add_argument("--no-journal", action="store_true",
                    help="不写运行流水与决策快照（只在临时排错时用，正常运行不要加）")
    ap.add_argument("--lag-refuse-min", type=float, default=LAG_REFUSE_MIN,
                    help="报价滞后超过多少分钟就中止（实际滞后写在 runs.jsonl 的 lag_min）")
    ap.add_argument("--late-min", type=int, default=40,
                    help="允许迟到多少分钟（吃掉 Actions 的排队延迟）")
    ap.add_argument("--early-min", type=int, default=5,
                    help="允许早到多少分钟（早到会让换手率偏低，不要放大）")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    now = _now(args)
    today_et = now.astimezone(calendar.ET).date()
    occ = calendar.occasion_for(today_et)
    kind = occ.kind if args.kind == "auto" else args.kind
    if kind == "none" and args.kind == "auto":
        if books.load_refill().get("open"):
            # 非推送日也要看一眼：空仓后每个交易日都得重挑一次（--bull-refill / 止损重选）。
            kind = "refill"
        elif not books.load_position().is_empty():
            # 有持仓的每个交易日都要查一次 10% 止损（回测在持仓期逐日盯收盘）。
            kind = "stop"

    # 日志对象先建：后面任何一条出口（含异常）都要留一行流水，
    # 否则「那天为什么没收到信号」只能去翻 90 天就过期的 Actions log。
    j = journal.Journal(kind, today_et, dry_run=args.dry_run,
                        enabled=not args.no_journal)
    try:
        return _run(args, j, now, today_et, occ, kind)
    except SystemExit as exc:
        print(f"::error::{exc}")
        _alert(j, args, "运行中止", f"{exc}")
        return j.finish(2, "abort", f"system_exit: {exc}")
    except Exception as exc:                            # noqa: BLE001
        log.exception("运行异常")
        print(f"::error::运行异常 {type(exc).__name__}: {exc}")
        _alert(j, args, "运行异常", f"{type(exc).__name__}: {exc}")
        return j.finish(2, "error", f"{type(exc).__name__}: {exc}")


def _alert(j, args, title: str, body: str) -> None:
    """该出信号的那次运行失败了：主动推一条短消息。

    原先中止只打一行 ::error:: 并写流水，手机上什么都收不到——月末调仓日就等于
    「静默漏信号」。这里只在已通过窗口/幂等判定（``j.armed``）且非 dry-run 时发，
    判定前的正常退出不会打扰人。告警本身失败只记日志，不影响退出码。
    """
    if args.dry_run or not getattr(j, "armed", False):
        return
    try:
        notify.send(f"[模拟盘·异常] {title}",
                    f"run {j.run_id}\n\n{body}\n\n**本次信号未推送**，请自行看盘口判断。")
    except Exception:                                   # noqa: BLE001
        log.exception("异常告警推送失败")


def _abort(j, args, status: str, reason: str, msg: str) -> int:
    """已 armed 之后的中止出口：打印 → 告警 → 记流水，三件事一起做不会漏。"""
    print(f"::error::{msg}")
    _alert(j, args, "信号中止", msg)
    return j.finish(2, status, reason)


def _run(args, j, now, today_et, occ, kind) -> int:
    """主流程。每个出口都走 ``j.finish``，保证「没发信号」也能被事后查到原因。"""
    print(f"现在：{now.isoformat(timespec='seconds')} = "
          f"{now.astimezone(calendar.ET):%Y-%m-%d %H:%M} ET（{today_et}）")
    print(f"日历：{occ.reason}；收盘 {occ.close_et}；触发 {occ.trigger_utc}；kind={occ.kind}")

    if not args.force:
        if kind == "none":
            print("今天不是调仓日也不是本周最后交易日，退出。")
            return j.finish(0, "noop", f"kind_none: {occ.reason}")
        if not calendar.in_execution_window(now, occ, args.early_min, args.late_min):
            print(f"不在执行窗口（触发时刻 −{args.early_min} / +{args.late_min} 分钟）内，退出。")
            return j.finish(0, "noop", "not_in_window")

    day = today_et.isoformat()
    if not args.force and books.already_pushed(kind, day):
        print(f"{day} 的 {kind} 已推送过，幂等跳过。")
        return j.finish(0, "noop", "already_pushed")

    # 走到这里就是「本该出信号」：此后任何中止都要主动告警
    j.armed = True

    # 止损检查只需要持仓票一个报价，不要把 500 只票全拉一遍
    if kind == "stop":
        return _run_stop(j, args, today_et)

    # --- 报价 ---
    # 成分按「今天」做 point-in-time：快照落后于今天时，已公告但还没进快照的调整会补进来
    members = signal_mod.load_members(pd.Timestamp(today_et))
    if members.empty:
        return _abort(j, args, "abort", "empty_pool", "成分股与行情池交集为空")
    syms = list(dict.fromkeys(signal_mod.SECTOR_ETFS + [signal_mod.BENCH]
                              + list(members["symbol"])))
    quote_map, qnotes = quotes.fetch_quotes(syms)
    if len(quote_map) < len(syms) * 0.5:
        return _abort(j, args, "abort", f"quotes_{len(quote_map)}_of_{len(syms)}",
                      f"报价只拿到 {len(quote_map)}/{len(syms)}，数据源异常，中止")

    # 用「本次判定使用的时刻」而不是墙上时钟：--now 复现某次决策时，报价滞后要相对那个
    # 时刻算，否则复现永远被判定为「数据滞后数小时」而中止
    lag_min, lag_n = quotes.describe_lag(quote_map, now)
    j.extra["lag_min"] = None if lag_min is None else round(lag_min, 1)
    j.extra["quotes"] = len(quote_map)
    warnings: list[str] = []
    lag_txt = "未知（报价无时间戳）"
    if lag_min is not None:
        lag_txt = (f"{lag_min:.0f} 分钟（{lag_n} 个样本）" if lag_min >= 0
                   else f"—（报价时间戳晚于判定时刻 {-lag_min:.0f} 分钟，复现模式）")
        if lag_min > LAG_WARN_MIN:
            warnings.append(f"报价滞后 {lag_min:.0f} 分钟，换手率可能偏低，请以盘口为准")
        # 硬闸门只在「真的在盘中跑」时生效。用 --now 复现历史/未来时点，报价必然对不上那个
        # 时刻，此时中止没有任何意义，只会让人误以为链路坏了。
        if lag_min > args.lag_refuse_min and not args.now:
            return _abort(j, args, "abort", f"stale_quotes_{lag_min:.0f}min",
                          f"报价滞后 {lag_min:.0f} 分钟，已超过 {args.lag_refuse_min:.0f} 分钟，"
                          f"不足以支撑收盘前 30 分钟的决策，中止且不推送")

    # --- 信号 ---
    # 补选窗口内沿用「调仓日定下的分支」：回测 --bull-refill 是在信号日判定的牛市里逐日重挑，
    # 中途 SPY 跌破 MA200 也不改分支（否则补选会突然跳到熊市 ETF 上）。
    # 止损重选不一样：回测的 turnma_pick_on 不看牛熊，永远走个股链路，所以这里强制
    # regime=True，否则熊市里止损后会被换成板块 ETF，与回测直接分叉。
    rf = books.load_refill() if kind == "refill" else {"open": False}
    if kind == "refill":
        regime = True if str(rf.get("origin")) == "stop" else bool(rf.get("bull", True))
    else:
        regime = None
    ex = None
    if kind == "refill" and rf.get("exclude"):
        ex = {str(rf.get("exclude"))}
    try:
        sig = signal_mod.compute(pd.Timestamp(today_et), quote_map, qnotes,
                                 regime=regime, exclude=ex)
    except SystemExit as exc:      # 报价还是上一交易日的快照、行情历史不够等硬性条件
        return _abort(j, args, "abort", "compute_abort", str(exc))
    j.pick, j.sector = sig.pick, sig.sector

    # 口径日与请求日不一致：当日数据还没到（周末/盘前/数据源没翻页），signal 已退回最近一根
    # 真实日线。周报照发（只是看板），但**调仓日**不能按旧口径下单，否则账本会记进一笔
    # 根本没在当天决策过的交易。
    if pd.Timestamp(sig.asof).date() != pd.Timestamp(today_et).date():
        if kind == "rebalance" and not args.now:
            return _abort(j, args, "abort", f"fallback_{sig.asof:%Y%m%d}",
                          f"调仓日 {today_et} 拿不到当日行情（口径退到 {sig.asof:%Y-%m-%d}），"
                          f"不按旧口径下单调仓，中止且不推送")
        warnings.append(f"本信号按 {sig.asof:%Y-%m-%d} 的口径计算（请求 {today_et}，"
                        f"当日数据未到），不能当作 {today_et} 的调仓依据")

    # 行情新鲜度：报价是实时的，但 bars 面板可能停在几天前（data job 挂了）。
    # 那种情况下 RSRS/均线用的是旧收盘，推送里必须看得见；缺得多就直接中止。
    lag_td = int(sig.data_lag_days or 0)
    if lag_td:
        last = f"{sig.data_last_bar:%Y-%m-%d}" if sig.data_last_bar is not None else "?"
        line = f"行情面板最新 {last}，落后信号日 {lag_td} 个交易日"
        if lag_td >= DATA_LAG_ABORT_TD and not args.now:
            return _abort(j, args, "abort", f"stale_data_{lag_td}td",
                          f"{line}（阈值 {DATA_LAG_ABORT_TD}），数据链路已断，中止且不推送")
        # 不到中止阈值就只告警：这句同时会进推送正文（signal 已把它写进 notes）
        print(f"::warning::{line}（阈值 {DATA_LAG_ABORT_TD}），RSRS/均线用的是旧收盘")


    pos = books.load_position()
    holding_ret = books.mark_to_market(pos, quote_map, sig.factors)
    action, detail = books.plan_action(pos, sig.pick)
    if kind == "week" and action == "换仓":
        action, detail = "继续持有", f"继续持有 {pos.symbol}（下周才换）"

    # --- 牛市补选（对应回测 --bull-refill）---
    # 只在「调仓日空仓 + 牛市」时打开，逐日重挑到下一个调仓日为止。等待日不推送、不记账，
    # 只在真的挑到票的那天推一条——与回测「命中日才买入」的语义一致。
    rf = rf if kind == "refill" else books.load_refill()
    if kind == "refill":
        if not pos.is_empty():
            books.clear_refill("已有持仓，补选无意义")
            return j.finish(0, "noop", "refill_has_position")
        if not rf.get("open"):
            return j.finish(0, "noop", "refill_closed")
        until = str(rf.get("until") or "")
        if until and day > until:
            books.clear_refill(f"已到下一个调仓日 {until}")
            return j.finish(0, "noop", "refill_expired")
        if not sig.pick:
            print("补选窗口内今天仍无合格标的，不动、不推送。")
            return j.finish(0, "noop", "refill_wait")

    # --- 候选池状态与指数调整 ---
    pctx = _pool_context(sig, pos, today_et)

    ctx = {
        "lag": lag_txt,
        "turn_cov": f"{len(quote_map)} 只报价",
        "turn_med": f"{sig.turnover_median:.2%}",
        "warnings": warnings,
    }

    if kind == "rebalance":
        title, content = render_rebalance(sig, action, detail, pos, holding_ret, ctx, pctx)
    elif kind == "refill":
        title, content = render_refill(sig, action, detail, pos, ctx, pctx, rf)
    else:
        title, content = render_week(sig, pos, holding_ret, ctx, pctx)

    # 推送正文带 run_id：data 分支回写失败导致重复推送时，能靠它对上是哪次运行
    content = f"{content}\n\n`run {j.run_id}`"

    print("\n" + "=" * 72)
    print(title)
    print("-" * 72)
    print(content)
    print("=" * 72)

    # 快照先落盘、再推送：推送失败时事后仍能看到「当时算出了什么」，
    # 顺序反了就会出现「消息到了手机、仓库里没有痕迹」。
    j.snapshot(sig, quote_map, title=title, content=content,
               action=action, detail=detail, pos=pos, holding_ret=holding_ret)

    if args.dry_run:
        print("\n[dry-run] 未推送、未记账。")
        return j.finish(0, "dryrun", "dry_run")

    ok = notify.send(title, content)
    j.notify = "ok" if ok else "failed"
    j.patch_decision(notify_ok=ok)
    if not ok:
        return j.finish(1, "error", "notify_failed")

    books.mark_pushed(kind, day, {"symbol": sig.pick, "sector": sig.sector,
                                  "action": action})
    if kind in ("rebalance", "refill"):
        # 只有调仓日与补选成交滚动净值；周报只报不平账，避免同一段收益被记两次
        books.record(pos, day=day, kind=kind, action=action, signal=sig,
                     holding_return=holding_ret, note=detail)
        print(f"账本已更新：{detail}，累计净值 {pos.nav:.4f}")
        nxt_reb = _next_rebalance(today_et)
        if sig.pick:
            books.clear_refill("补选成交" if kind == "refill" else "本期已建仓")
        elif sig.bull:
            books.set_refill(since=day, until=nxt_reb.isoformat(), bull=bool(sig.bull))
            print(f"本期空仓 → 打开牛市补选，逐日重挑到 {nxt_reb:%Y-%m-%d}")
        else:
            books.clear_refill("熊市空仓，不做补选")
    else:
        print(f"账本不变（{action}）。")
    return j.finish(0, "ok", detail)


if __name__ == "__main__":
    sys.exit(main())
