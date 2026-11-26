"""实盘账本：当前持仓、已推送标记、补选状态、历史流水。

为什么必须有账本
----------------
回测里「上一期买了什么」只是循环里的局部变量，进程退出就没了。实盘要跨天跨月
记住三件事才能给出正确动作：

1. **当前持仓**——决定是「换仓」还是「继续持有」，也是算持有期收益的基准；
2. **已推送标记**——workflow 会重跑、会补跑、还能手动触发，没有幂等键就会同一天
   推三条一样的消息、把流水写乱；
3. **补选状态**——调仓日空仓或止损后，要「逐日重挑到下一个调仓日」，跨天必须落盘。

状态默认放 ``live/state/``，可用 ``TA_STATE_DIR`` 改到别处。
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

POSITIONS = "positions.json"
PUSHED = "pushed.json"
LEDGER = "ledger.csv"
REFILL = "refill.json"

LEDGER_COLUMNS = ["date", "kind", "action", "from_symbol", "to_symbol",
                  "sector", "price", "ret_since_entry", "nav", "note"]


def state_dir() -> Path:
    env = os.getenv("TA_STATE_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent / "state"


@dataclass
class Position:
    symbol: str = ""
    kind: str = "stock"        # stock / etf
    sector: str = ""
    entry_date: str = ""
    entry_adj: float = 0.0     # 建仓日复权收盘（bars.close 本身就是总收益复权）
    entry_k: float = 1.0       # 建仓日 k（未复权 × k = 复权），拆股后要用它折算
    nav: float = 1.0
    stop_retry: int = 0        # 历史字段，保留兼容；止损后重选已不再限制次数

    def is_empty(self) -> bool:
        return not self.symbol


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("%s 解析失败，按空账本处理（原文件留 .broken）", path)
        path.rename(path.with_suffix(path.suffix + ".broken"))
        return dict(default)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def append_ledger(row: dict) -> None:
    path = state_dir() / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    head = not path.exists()
    line = ",".join(_csv(v) for v in (row.get(c, "") for c in LEDGER_COLUMNS))
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        if head:
            fh.write(",".join(LEDGER_COLUMNS) + "\n")
        fh.write(line + "\n")


def _csv(v) -> str:
    if v is None:
        return ""
    s = str(v)
    return f'"{s}"' if ("," in s or '"' in s) else s


def load_position() -> Position:
    raw = _read_json(state_dir() / POSITIONS, {})
    known = {k: raw.get(k) for k in Position.__dataclass_fields__ if k in raw}
    return Position(**known)


def save_position(pos: Position) -> None:
    _write_json(state_dir() / POSITIONS, asdict(pos))


def load_pushed() -> dict:
    return _read_json(state_dir() / PUSHED,
                      {"rebalance": {}, "week": {}, "stop": {}, "refill": {}})


def already_pushed(kind: str, day: str) -> bool:
    return load_pushed().get(kind, {}).get("date") == day


def mark_pushed(kind: str, day: str, extra: dict | None = None) -> None:
    pushed = load_pushed()
    payload = {"date": day}
    payload.update(extra or {})
    pushed[kind] = payload
    _write_json(state_dir() / PUSHED, pushed)


def load_refill() -> dict:
    """空仓后的逐日重挑状态 ``{open, since, until, bull, exclude, origin}``。

    ``bull`` 冻结的是**开启那天**的牛熊分支，补选期间不改判；``exclude`` 在止损重选时
    带上刚割掉的票，重挑下一个顺位。
    """
    return _read_json(state_dir() / REFILL,
                      {"open": False, "exclude": "", "origin": "rebalance"})


def set_refill(since: str, until: str, bull: bool = True,
               exclude: str = "", origin: str = "rebalance") -> None:
    _write_json(state_dir() / REFILL,
                {"open": True, "since": since, "until": until, "bull": bool(bull),
                 "exclude": str(exclude or ""), "origin": str(origin)})


def clear_refill(reason: str = "") -> None:
    if reason:
        log.info("关闭补选：%s", reason)
    _write_json(state_dir() / REFILL, {"open": False})


def plan_action(pos: Position, target: str) -> tuple[str, str]:
    """返回 (动作, 中文说明)。空字符串的 target 表示本期空仓。"""
    cur = "" if pos.is_empty() else pos.symbol
    if not target:
        return ("清仓观望" if cur else "继续空仓"), "本期无合格标的"
    if not cur:
        return "建仓", f"买入 {target}"
    if cur == target:
        return "继续持有", f"继续持有 {cur}"
    return "换仓", f"卖出 {cur} → 买入 {target}"


def holding_return(pos: Position, quote: float, k: float) -> float | None:
    """持有期收益（复权口径）：``quote × k ÷ 建仓复权价 − 1``。

    用复权口径才不会把拆股/分红误算成亏损；拿不到报价就返回 None，宁可漏检一次，
    也不凭旧价割肉。
    """
    if pos.is_empty() or not pos.entry_adj or not quote or not k:
        return None
    ret = float(quote) * float(k) / pos.entry_adj - 1.0
    return ret if math.isfinite(ret) else None


def stop_triggered(pos: Position, quote: float, k: float,
                   threshold: float = 0.10) -> tuple[bool, float | None]:
    """止损是否触发，返回 (触发?, 持有期收益)。看的是相对建仓价的累计跌幅。"""
    ret = holding_return(pos, quote, k)
    if ret is None:
        return False, None
    # 1e-9 吃掉 0.90/1.00-1 = -0.09999999999999998 的浮点误差
    return bool(ret <= -abs(float(threshold)) + 1e-9), ret


def record_stop(pos: Position, *, day: str, action: str, price: float,
                ret: float | None, note: str = "", retry_used: int = 0) -> Position:
    """止损清仓：收益并入 nav、清空持仓、写一行 kind=stop 流水。"""
    old = pos.symbol
    if old and pos.entry_adj and ret is not None:
        pos.nav *= (1.0 + ret)

    pos.symbol = ""
    pos.sector = ""
    pos.kind = "stock"
    pos.entry_date = day
    pos.entry_adj = 0.0
    pos.entry_k = 1.0
    pos.stop_retry = int(retry_used)
    append_ledger({
        "date": day, "kind": "stop", "action": action,
        "from_symbol": old, "to_symbol": "", "sector": "",
        "price": round(float(price), 4) if price and math.isfinite(float(price)) else "",
        "ret_since_entry": "" if ret is None else round(ret, 4),
        "nav": round(pos.nav, 6), "note": note,
    })
    save_position(pos)
    return pos


def record(pos: Position, *, day: str, kind: str, action: str, target: str,
           target_kind: str, sector: str, price: float, k: float,
           ret: float | None, note: str = "") -> Position:
    """按本期决策更新账本。先滚动旧仓收益，再决定是否换标的。

    继续持有也要记一行，否则那一期的收益会漏进净值。
    """
    old = pos.symbol
    if old and pos.entry_adj and ret is not None:
        pos.nav *= (1.0 + ret)

    if target and target != old:
        pos.symbol = target
        pos.kind = target_kind or "stock"
        pos.sector = sector
        pos.entry_date = day
        # bars.close 本身就是总收益复权价，建仓基准直接用它；
        # k 只留着把「实时未复权报价」折成复权口径，不能再乘一次。
        pos.entry_adj = float(price)
        pos.entry_k = float(k)
    elif not target:
        pos.symbol = ""
        pos.sector = ""
        pos.kind = "stock"
        pos.entry_date = day
        pos.entry_adj = 0.0
        pos.entry_k = 1.0

    append_ledger({
        "date": day, "kind": kind, "action": action,
        "from_symbol": old, "to_symbol": pos.symbol, "sector": pos.sector,
        "price": round(float(price), 4) if price else "",
        "ret_since_entry": "" if ret is None else round(ret, 4),
        "nav": round(pos.nav, 6), "note": note,
    })
    save_position(pos)
    return pos
