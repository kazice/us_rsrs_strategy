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

import pandas as pd

from quant.live import books, calendar, journal, notify, pool, quotes, signal as signal_mod

log = logging.getLogger(__name__)

LAG_WARN_MIN = 10.0        # 报价滞后超过这个数就在推送里明确标注
LAG_REFUSE_MIN = 45.0      # 滞后到这个程度已不能代表 15:30，拒绝出信号


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

    # 与周报共用同一段「本期筛选」：板块 RSRS 前三、换手 TOP3 的 MA10/20/50、各层数量。
    # 调仓那天才是真成交，看不到均线具体值说不过去。
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
    lines += ["", f"**口径提醒**：{guard}盘中最高/最低比回测的完整日振幅略窄；本策略不设止损、"
                 "单票满仓，执行价以你的盘口为准。"]
    return title, "\n".join(lines)


def _ma_txt(mas: dict, w: int) -> str:
    v = mas.get(w)
    return "—" if v is None or v != v else f"{v:.2f}"


def _screen_lines(sig, decided: bool = False) -> list[str]:
    """本期筛选明细：结论 + 板块 RSRS + 换手 TOP3 的均线 + 各层数量。

    周报原先只报持仓，空仓时就两行，读的人分不清「没有合格标的」和「还没建仓」，
    也看不到候选究竟卡在哪一步。这里把每一步的具体数字摊开。

    ``decided=True`` 用在调仓推送：那天是真的要下单，「若今日为调仓日」的假设语气
    要换成「买入」；其余内容与周报完全一致，两种推送看同一张表。
    """
    out = ["", "**本期筛选**"]

    if sig.pick:
        rank = next((c.sector_rank for c in sig.candidates if c.symbol == sig.pick), 1)
        rsrs = float(sig.sector_scores.get(sig.sector, float("nan")))
        lead = "结论：买入" if decided else "结论：若当日为调仓日，买入"
        out.append(f"- {lead} **{sig.pick}**（{sig.sector}）满足全部条件\u2014\u2014"
                   f"板块 RSRS 第 {rank}（{rsrs:.2f}），在换手 TOP3 内且 MA10>MA20>MA50；"
                   f"参考价 ${sig.price:.2f}（盘中时点价，非收盘价）")
    else:
        tail = "，本次调仓空仓" if decided else ""
        out.append("- 结论：前三个板块 \u00d7 换手 TOP3 全部不满足 MA10>MA20>MA50，"
                   f"本期无合格标的{tail}")

    top = signal_mod.top_sectors(sig)
    out.append("- 板块 RSRS 前三：" + " / ".join(f"{s} {v:.2f}" for s, v in top))

    if sig.candidates:
        out += ["", "| 板块序 | 板块 | 代码 | 20日均换手 | MA10 | MA20 | MA50 | 多头排列 |",
                "|---|---|---|---|---|---|---|---|"]
        for c in sig.candidates[:9]:
            out.append(f"| {c.sector_rank} | {c.sector} | {c.symbol} | {c.turnover:.2%} | "
                       f"{_ma_txt(c.ma, 10)} | {_ma_txt(c.ma, 20)} | {_ma_txt(c.ma, 50)} | "
                       f"{'\u2713' if c.ok else '\u2717'} |")

    tbl = sig.pool_table
    if len(tbl):
        cap_ok = tbl["mcap_ok"] == True                    # noqa: E712  列里是 object 布尔
        turn_ok = tbl["turn_ok"] == True                   # noqa: E712
        stack_ok = tbl["stack_ok"] == True                 # noqa: E712
        both = cap_ok & turn_ok
        out.append("")
        out.append(f"- 数量：候选池 {len(tbl)} 只 \u2192 市值+换手双合规 {int(both.sum())} 只 "
                   f"\u2192 MA 多头排列 {int((both & stack_ok).sum())} 只 "
                   f"\u2192 进板块 TOP3 {int((tbl['in_top3'] == True).sum())} 只 "  # noqa: E712
                   f"\u2192 选中 {int((tbl['selected'] == True).sum())} 只")  # noqa: E712
        out.append(f"- 参考：全池 MA 多头排列 {int(stack_ok.sum())} 只"
                   f"（含未过市值/换手门槛的）")
        out.append("- 口径：先按板块 RSRS 取前 3 个板块，"
                   "每个板块内按 20 日均换手降序取前 3 只，"
                   "只在这最多 9 只里判 MA10>MA20>MA50，命中即止")
    return out


def render_week(sig, pos: books.Position, holding_ret: float | None, ctx: dict,
                pctx: dict) -> tuple[str, str]:
    nxt = _next_rebalance(sig.asof.date())
    title = f"[模拟盘] {sig.asof:%m-%d} 周报"
    body = [f"## {sig.asof:%Y-%m-%d} 周报", ""]
    if pos.is_empty():
        body.append("**当前空仓**：账本尚无持仓 \u2014\u2014 本策略只在月末最后交易日建仓，周报只报不平账。")
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
    body += ["", "---", f"报价滞后 {ctx['lag']}；换手率覆盖 {ctx['turn_cov']}。"]
    return title, "\n".join(body)


def main() -> int:
    ap = argparse.ArgumentParser(description="模拟盘信号（收盘前 30 分钟）")
    ap.add_argument("--now", default="", help="模拟当前时刻（默认取系统时钟），时区缺省按 ET")
    ap.add_argument("--kind", default="auto", choices=["auto", "rebalance", "week"],
                    help="auto = 按日历判断；rebalance/week = 强制按该类推送")
    ap.add_argument("--force", action="store_true", help="无视执行窗口与幂等键")
    ap.add_argument("--dry-run", action="store_true", help="只打印不推送、不记账")
    ap.add_argument("--no-journal", action="store_true",
                    help="不写运行流水与决策快照（只在临时排错时用，正常运行不要加）")
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

    # 日志对象先建：后面任何一条出口（含异常）都要留一行流水，
    # 否则「那天为什么没收到信号」只能去翻 90 天就过期的 Actions log。
    j = journal.Journal(kind, today_et, dry_run=args.dry_run,
                        enabled=not args.no_journal)
    try:
        return _run(args, j, now, today_et, occ, kind)
    except SystemExit as exc:
        print(f"::error::{exc}")
        return j.finish(2, "abort", f"system_exit: {exc}")
    except Exception as exc:                            # noqa: BLE001
        log.exception("运行异常")
        print(f"::error::运行异常 {type(exc).__name__}: {exc}")
        return j.finish(2, "error", f"{type(exc).__name__}: {exc}")


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

    # --- 报价 ---
    members = signal_mod.load_members()
    if members.empty:
        print("::error::成分股与行情池交集为空")
        return j.finish(2, "abort", "empty_pool")
    syms = list(dict.fromkeys(signal_mod.SECTOR_ETFS + [signal_mod.BENCH]
                              + list(members["symbol"])))
    quote_map, qnotes = quotes.fetch_quotes(syms)
    if len(quote_map) < len(syms) * 0.5:
        print(f"::error::报价只拿到 {len(quote_map)}/{len(syms)}，数据源异常，中止")
        return j.finish(2, "abort", f"quotes_{len(quote_map)}_of_{len(syms)}")

    # 用「本次判定使用的时刻」而不是墙上时钟：--now 复现某次决策时，报价滞后要相对那个
    # 时刻算，否则复现永远被判定为「数据滞后数小时」而中止
    lag_min, lag_n = quotes.describe_lag(quote_map, now)
    warnings: list[str] = []
    lag_txt = "未知（报价无时间戳）"
    if lag_min is not None:
        lag_txt = (f"{lag_min:.0f} 分钟（{lag_n} 个样本）" if lag_min >= 0
                   else f"—（报价时间戳晚于判定时刻 {-lag_min:.0f} 分钟，复现模式）")
        if lag_min > LAG_WARN_MIN:
            warnings.append(f"报价滞后 {lag_min:.0f} 分钟，换手率可能偏低，请以盘口为准")
        # 硬闸门只在「真的在盘中跑」时生效。用 --now 复现历史/未来时点，报价必然对不上那个
        # 时刻，此时中止没有任何意义，只会让人误以为链路坏了。
        if lag_min > LAG_REFUSE_MIN and not args.now:
            print(f"::error::报价滞后 {lag_min:.0f} 分钟，已超过 {LAG_REFUSE_MIN:.0f} 分钟，"
                  f"不足以支撑收盘前 30 分钟的决策，中止且不推送")
            return j.finish(2, "abort", f"stale_quotes_{lag_min:.0f}min")

    # --- 信号 ---
    sig = signal_mod.compute(pd.Timestamp(today_et), quote_map, qnotes)
    j.pick, j.sector = sig.pick, sig.sector

    pos = books.load_position()
    holding_ret = books.mark_to_market(pos, quote_map, sig.factors)
    action, detail = books.plan_action(pos, sig.pick)
    if kind == "week" and action == "换仓":
        action, detail = "继续持有", f"继续持有 {pos.symbol}（下周才换）"

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
    else:
        title, content = render_week(sig, pos, holding_ret, ctx, pctx)

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
    if kind == "rebalance":
        # 只有调仓日滚动净值；周报只报不平账，避免同一段收益被记两次
        books.record(pos, day=day, kind=kind, action=action, signal=sig,
                     holding_return=holding_ret, note=detail)
        print(f"账本已更新：{detail}，累计净值 {pos.nav:.4f}")
    else:
        print(f"账本不变（{action}）。")
    return j.finish(0, "ok", detail)


if __name__ == "__main__":
    sys.exit(main())
