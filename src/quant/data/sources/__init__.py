"""行情数据源：统一接口 + 主备降级。"""

from quant.data.sources.base import BarSource, empty_bars  # noqa: F401
from quant.data.sources.registry import build_chain, fetch_with_fallback  # noqa: F401
