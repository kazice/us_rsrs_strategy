"""模拟盘账本：当前持仓、历史流水、已推送标记。

为什么必须有账本
----------------
回测里「上一期买了什么」只是循环里的一个局部变量 ``prev``，进程退出就没了。实盘要跨天、
跨月记住三件事，才能给出正确的动作：

1. **当前持仓**（代码 / 建仓日 / 建仓价 / 建仓时的复权价）——决定是「换仓」还是「继续持有」，
   也是算持有期收益的基准；
2. **已推送标记**——GitHub Actions 的 schedule 会漂、会重跑、还能手动触发，没有幂等键就会
   同一天推三条一样的消息、把流水写乱；
3. **审计行**——每次决策记下数据截止日与当时的候选，事后「那天为什么选了 MRNA」才有据可查。

状态默认放 ``state/live/``（相对项目根），可用 ``TA_STATE_DIR`` 改到 data 分支里。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

POSITIONS = "positions.json"
PUSHED = "pushed.json"
LEDGER = "ledger.csv"

LEDGER_COLUMNS = ["date", "kind", "action", "from_symbol", "to_symbol",
                  "sector", "price", "adj", "ret_since_entry", "nav", "note"]


def state_dir() -> Path:
    env = os.getenv("TA_STATE_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "state" / "live"


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("%s 解析失败，按空账本处理（原文件保留为 .broken）", path)
        path.rename(path.with_suffix(path.suffix + ".broken"))
        return dict(default)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _append_ledger(row: dict) -> None:
    path = state_dir() / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([row]).reindex(columns=LEDGER_COLUMNS)
    header = not path.exists()
    df.to_csv(path, mode="a", header=header, index=False, encoding="utf-8-sig")


@dataclass
class Position:
    symbol: str = ""
    sector: str = ""
    entry_date: str = ""
    entry_price: float = 0.0      # 建仓时的未复权价（人看的「成本价」）
    entry_adj: float = 0.0        # 建仓时的 adj_close（算收益用，已含分红拆股）
    nav: float = 1.0              # 从首期起的累计净值
    last_mark_date: str = ""
    last_mark_adj: float = 0.0
    moves: int = 0
    history: list[dict] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.symbol


def load_position() -> Position:
    raw = _read_json(state_dir() / POSITIONS, {})
    known = {k: raw.get(k) for k in Position.__dataclass_fields__ if k in raw}
    return Position(**known)


def save_position(pos: Position) -> None:
    _write_json(state_dir() / POSITIONS, asdict(pos))


def load_pushed() -> dict:
    return _read_json(state_dir() / PUSHED, {"rebalance": {}, "week": {}})


def save_pushed(pushed: dict) -> None:
    _write_json(state_dir() / PUSHED, pushed)


def already_pushed(kind: str, day: str) -> bool:
    rec = load_pushed().get(kind, {})
    return rec.get("date") == day


def mark_pushed(kind: str, day: str, extra: dict | None = None) -> None:
    pushed = load_pushed()
    payload = {"date": day}
    payload.update(extra or {})
    pushed[kind] = payload
    save_pushed(pushed)


def plan_action(pos: Position, target_symbol: str) -> tuple[str, str]:
    """返回 (动作类型, 中文说明)。空字符串的 target 表示当期空仓。"""
    cur = "" if pos.is_empty() else pos.symbol
    if not target_symbol:
        return ("清仓观望" if cur else "继续空仓"), "本期无合格标的"
    if not cur:
        return "建仓", f"买入 {target_symbol}"
    if cur == target_symbol:
        return "继续持有", f"继续持有 {cur}"
    return "换仓", f"卖出 {cur} → 买入 {target_symbol}"


def mark_to_market(pos: Position, quotes: dict, factors: dict[str, float]) -> float | None:
    """当前持仓截至此刻的持有期收益。

    用 adj 口径（未复权价 × 复权因子 ÷ 建仓时 adj_close），这样拆股和分红不会被算成
    收益或亏损；未复权价只用于展示成本。
    """
    if pos.is_empty() or not pos.entry_adj:
        return None
    q = quotes.get(pos.symbol)
    f = factors.get(pos.symbol)
    if q is None or f is None:
        return None
    return q.price * f / pos.entry_adj - 1.0


def record(pos: Position, *,
           day: str, kind: str, action: str, signal, holding_return: float | None,
           note: str = "") -> Position:
    """按本期决策更新账本并写一行流水。

    净值按「上一期持仓的实际收益」滚动：每次调仓先把旧仓的持有期收益并入 nav，再决定
    是否换标的。**继续持有也要记一行**——否则那一期的收益会漏进净值，月度净值曲线就断了。
    """
    old_symbol = pos.symbol
    if old_symbol and pos.entry_adj and holding_return is not None:
        pos.nav *= (1.0 + holding_return)

    target = signal.pick
    if target and target != old_symbol:
        f = signal.factors.get(target, 1.0)
        pos.symbol = target
        pos.sector = signal.sector
        pos.entry_date = day
        pos.entry_price = float(signal.price)
        pos.entry_adj = float(signal.price) * float(f)
        pos.moves += 1
        pos.history.append({"date": day, "symbol": target,
                            "entry": round(pos.entry_price, 4)})
        pos.history = pos.history[-40:]
    elif not target:
        pos.symbol = ""
        pos.sector = ""
        pos.entry_date = day
        pos.entry_price = 0.0
        pos.entry_adj = 0.0

    pos.last_mark_date = day
    pos.last_mark_adj = pos.entry_adj
    _append_ledger({
        "date": day, "kind": kind, "action": action,
        "from_symbol": old_symbol, "to_symbol": target, "sector": signal.sector,
        "price": round(float(signal.price), 4) if target else "",
        "adj": round(pos.entry_adj, 4) if target else "",
        "ret_since_entry": "" if holding_return is None else round(holding_return, 4),
        "nav": round(pos.nav, 6), "note": note,
    })
    save_position(pos)
    return pos
