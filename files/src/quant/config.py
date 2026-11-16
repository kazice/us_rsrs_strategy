"""集中配置：yaml + 环境变量覆盖，路径一律转成绝对路径。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"


class ConfigError(RuntimeError):
    pass


def _to_ns(value: Any) -> Any:
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_ns(v) for v in value]
    return value


def _abs_path(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def _load_env_file() -> None:
    """把项目根的 .env 补进环境变量，已有值不覆盖。

    .gitignore 里一直留着 .env 这一条，但代码从来没读过它——于是密钥只能在每个终端里
    临时 `$env:XXX=` 设一次，关掉终端就丢，每次都要重新找。这里补上读取：
    真实环境变量优先（含 setx 的用户级变量），.env 只作兜底。
    格式：每行 KEY=VALUE，支持 # 注释与引号包裹。
    """
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@lru_cache(maxsize=1)
def get_settings(path: str | None = None) -> SimpleNamespace:
    """加载配置。环境变量优先级：ALPACA_KEY_ID / ALPACA_SECRET / TIINGO_TOKEN / TA_PROXY / MARKETDATA_TOKEN。

    环境变量缺失时读项目根 .env 兜底，见 `_load_env_file`。
    """
    _load_env_file()
    settings_path = Path(path) if path else DEFAULT_SETTINGS
    if not settings_path.exists():
        raise ConfigError(f"配置文件不存在: {settings_path}")

    with settings_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    paths = raw.setdefault("paths", {})
    for key in ("data", "reports", "logs"):
        paths[key] = str(_abs_path(paths.get(key, key)))
    # CI 里行情在独立 data 分支的 worktree 上（不在主仓），用环境变量指过去，
    # 免得为跑一次信号改 settings.yaml。优先级高于 yaml。
    data_override = os.getenv("TA_DATA_ROOT", "").strip()
    if data_override:
        paths["data"] = str(_abs_path(data_override))

    raw.setdefault("sources", {}).setdefault("alpaca", {})["key_id"] = os.getenv("ALPACA_KEY_ID", "")
    raw["sources"]["alpaca"]["secret"] = os.getenv("ALPACA_SECRET", "")
    raw.setdefault("sources", {}).setdefault("tiingo", {})["token"] = os.getenv("TIINGO_TOKEN", "")
    raw.setdefault("options", {})["marketdata_token"] = os.getenv("MARKETDATA_TOKEN", "")
    raw["proxy"] = os.getenv("TA_PROXY", "") or None

    cfg = _to_ns(raw)
    cfg.paths.data_path = Path(cfg.paths.data)
    cfg.paths.reports_path = Path(cfg.paths.reports)
    cfg.paths.logs_path = Path(cfg.paths.logs)
    return cfg


def ensure_dirs(cfg: SimpleNamespace | None = None) -> None:
    """创建所有运行期需要的目录。"""
    cfg = cfg or get_settings()
    for base in (cfg.paths.data_path, cfg.paths.reports_path, cfg.paths.logs_path):
        base.mkdir(parents=True, exist_ok=True)
    for sub in (
        "meta/universe",
        "curated/bars_daily",
        "curated/factors",
        "raw/yahoo",
    ):
        (cfg.paths.data_path / sub).mkdir(parents=True, exist_ok=True)
