"""供热曲线。

曲线按室外温度给出一次网目标供水温度：室外越冷，供温越高。曲线是"按气象更新"
的对象，每次更新都要先校验室外温度是否落在适用区间内，校验通过才允许覆盖旧值。
曲线过期后不再一刀切地锁死热源，而是由
:class:`~heatnet.temp.degrade.CurveGovernor` 按曲线龄期分档：刚过期进简化档
（冻结最后一版有效曲线定点运行），过期太久进保守档（降到保守供温），气象恢复
后逐级爬回正常档；每一档的迁移都落审计。
"""

from __future__ import annotations

from .curve import HeatCurve
from .degrade import (
    TIER_CONSERVATIVE,
    TIER_NORMAL,
    TIER_SIMPLIFIED,
    CurveGovernor,
    TierTransition,
)
from .forecast import ForecastPoint, build_forecast, describe_forecast
from .state import CurveSnapshot
from .table import CurveTable

__all__ = [
    "TIER_CONSERVATIVE",
    "TIER_NORMAL",
    "TIER_SIMPLIFIED",
    "CurveGovernor",
    "CurveSnapshot",
    "CurveTable",
    "ForecastPoint",
    "HeatCurve",
    "TierTransition",
    "build_forecast",
    "describe_forecast",
]
