"""供热曲线状态快照与降级档位。"""

from __future__ import annotations

from dataclasses import dataclass

NORMAL = "normal"
SIMPLIFIED = "simplified"
CONSERVATIVE = "conservative"
RECOVERING = "recovering"

# 顺序就是保守程度；恢复档是从保守档返回正常档的过渡，不参与过期降档排序。
STALE_RANKS = (NORMAL, SIMPLIFIED, CONSERVATIVE)
ACTIVE_TIERS = (NORMAL, SIMPLIFIED, CONSERVATIVE, RECOVERING)

TIER_LABELS = {
    NORMAL: "正常曲线",
    SIMPLIFIED: "简化运行",
    CONSERVATIVE: "保守曲线",
    RECOVERING: "恢复中",
}

CURVE_LAST_VALID = "last_valid"
CURVE_CONSERVATIVE = "conservative"
CURVE_RECOVERY = "fresh_forecast"


@dataclass(frozen=True)
class CurveTransition:
    """一次曲线档位切换。"""

    previous_tier: str
    tier: str
    curve: str
    at: float
    reason: str
    mode: str
    age_s: float

    def as_dict(self) -> dict:
        return {
            "previous_tier": self.previous_tier,
            "tier": self.tier,
            "tier_label": TIER_LABELS[self.tier],
            "curve": self.curve,
            "reason": self.reason,
            "mode": self.mode,
            "at": round(self.at, 3),
            "age_s": round(self.age_s, 3),
            "basis": "forecast_age_s",
        }


@dataclass(frozen=True)
class CurveSnapshot:
    """一次曲线读取的结果。"""

    outdoor_c: float
    supply_c: float
    target_c: float
    epoch: int
    updated_at: float
    age_s: float
    stale: bool
    runnable: bool
    weather_failed: bool
    tier: str
    tier_label: str
    curve: str
    transition_mode: str
    basis: str
    validated: int
    rejected: int

    def as_dict(self) -> dict:
        return {
            "outdoor_c": round(self.outdoor_c, 2),
            "supply_c": round(self.supply_c, 2),
            "target_c": round(self.target_c, 2),
            "epoch": self.epoch,
            "updated_at": round(self.updated_at, 3),
            "age_s": round(self.age_s, 3),
            "stale": self.stale,
            "runnable": self.runnable,
            "weather_failed": self.weather_failed,
            "tier": self.tier,
            "tier_label": self.tier_label,
            "curve": self.curve,
            "transition_mode": self.transition_mode,
            "basis": "forecast_age_s",
            "validated": self.validated,
            "rejected": self.rejected,
        }
