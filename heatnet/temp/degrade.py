"""曲线过期分档与降档时序。

分档按**时间**（曲线龄期，即距最近一次成功气象更新的秒数），不按偏差：
气象缺报时没有新量测，偏差无从计算；按时间分档确定、可复现、可审计。

三档（档号越大越保守）：

- ``normal``       正常档：曲线在新鲜度窗口内，跟随实时曲线供温；
- ``simplified``   简化档：刚过期，冻结最后一版有效曲线的供温定点运行；
- ``conservative`` 保守档：过期太久，降到配置给定的保守供温。

时序约束（降档与恢复都不能乱）：

- 降档逐级进行，每一跳按顺序落审计，不允许跳档；
- 气象缺报属于部分失败：曲线不新鲜时只许往更保守的档降，不许往回跳；
- 气象恢复（曲线重新新鲜）后每次评估只许升一档，自己逐级爬回正常档；
- 降到保守档时是立刻切保守值还是按速率滑过去，按时段定：当前 UTC 小时
  落在配置的回滑时段内就滑过去，否则立刻切；方式在降档进入时锁存，
  滑到一半跨出时段也不改成立即切换。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from ..config import DegradeLimits
from .curve import HeatCurve

TIER_NORMAL = "normal"
TIER_SIMPLIFIED = "simplified"
TIER_CONSERVATIVE = "conservative"

TIER_RANK = {TIER_NORMAL: 0, TIER_SIMPLIFIED: 1, TIER_CONSERVATIVE: 2}
_RANK_TIER = {rank: tier for tier, rank in TIER_RANK.items()}

# 降档/恢复原因，随迁移记录落审计。
REASON_STALE = "curve_stale"
REASON_STALE_TOO_LONG = "stale_too_long"
REASON_RECOVERED = "forecast_recovered"

# 目标应用方式：跟随实时曲线 / 定点保持 / 按速率滑向保守值 / 立刻切到保守值。
_MODE_TRACK = "track"
_MODE_HOLD = "hold"
_MODE_RAMP = "ramp"
_MODE_STEP = "step"


@dataclass(frozen=True)
class TierTransition:
    """一次档位迁移，逐跳记录，供审计落盘。"""

    from_tier: str
    to_tier: str
    direction: str
    reason: str
    at: float

    def as_dict(self) -> dict:
        return {
            "from_tier": self.from_tier,
            "to_tier": self.to_tier,
            "direction": self.direction,
            "reason": self.reason,
            "at": round(self.at, 3),
        }


class CurveGovernor:
    """按曲线龄期管理运行档位，给出本周期热源应追随的目标供温。"""

    def __init__(self, curve: HeatCurve, limits: DegradeLimits) -> None:
        self._curve = curve
        self._limits = limits
        self._guard = threading.RLock()
        self._tier = TIER_NORMAL
        self._held_supply_c = curve.supply_c()
        self._tier_target_c = curve.supply_c()
        self._effective_target_c = curve.supply_c()
        self._mode = _MODE_TRACK
        self._transitions: list[TierTransition] = []

    def tier(self) -> str:
        """返回当前运行档位。"""

        with self._guard:
            return self._tier

    def target_c(self) -> float:
        """返回当前生效的目标供温（含回滑中间值）。"""

        with self._guard:
            return self._effective_target_c

    def partial_failure(self, now: float) -> bool:
        """气象是否处于缺报（部分失败）：曲线已过期，只许降档不许升档。"""

        with self._guard:
            return self._age_s(float(now)) > self._curve.limits.stale_after_s

    def evaluate(self, now: float, dt: float = 0.0) -> dict:
        """评估一次档位并推进目标供温，返回本周期的档位决定。

        曲线不新鲜（气象缺报，部分失败）时只许往更保守的档降；曲线新鲜时
        每次评估只许升一档，逐级爬回正常档。
        """

        with self._guard:
            moment = float(now)
            age_s = self._age_s(moment)
            fresh = age_s <= self._curve.limits.stale_after_s
            transitions: list[TierTransition] = []
            if not fresh:
                desired = self._tier_for_age(age_s)
                while TIER_RANK[desired] > TIER_RANK[self._tier]:
                    transitions.append(self._step_down_locked(moment))
            elif self._tier != TIER_NORMAL:
                transitions.append(self._step_up_locked(moment))
            self._advance_target_locked(dt)
            return {
                "tier": self._tier,
                "target_c": round(self._effective_target_c, 2),
                "tier_target_c": round(self._tier_target_c, 2),
                "held_supply_c": round(self._held_supply_c, 2),
                "mode": self._mode,
                "ramping": self._mode == _MODE_RAMP,
                "partial_failure": not fresh,
                "age_s": round(age_s, 3),
                "transitions": [item.as_dict() for item in transitions],
            }

    def snapshot(self, now: float) -> dict:
        """返回档位状态快照，供控制台展示并与曲线快照一起核对。"""

        with self._guard:
            age_s = self._age_s(float(now))
            return {
                "tier": self._tier,
                "target_c": round(self._effective_target_c, 2),
                "tier_target_c": round(self._tier_target_c, 2),
                "held_supply_c": round(self._held_supply_c, 2),
                "mode": self._mode,
                "ramping": self._mode == _MODE_RAMP,
                "partial_failure": age_s > self._curve.limits.stale_after_s,
                "age_s": round(age_s, 3),
                "stale_after_s": self._curve.limits.stale_after_s,
                "degrade_after_s": self._limits.degrade_after_s,
                "conservative_supply_c": self._limits.conservative_supply_c,
                "degrade_rate_c_s": self._limits.degrade_rate_c_s,
                "ramp_windows": [[start, end] for start, end in self._limits.ramp_windows],
                "transitions": len(self._transitions),
                "recent_transitions": [item.as_dict() for item in self._transitions[-5:]],
            }

    # ---- 内部 ---------------------------------------------------------

    def _age_s(self, now: float) -> float:
        """曲线龄期；还没有任何成功更新时按初始标定值对待，龄期为零。"""

        if self._curve.epoch() == 0:
            return 0.0
        return max(0.0, float(now) - self._curve.updated_at())

    def _tier_for_age(self, age_s: float) -> str:
        if age_s <= self._curve.limits.stale_after_s:
            return TIER_NORMAL
        if age_s <= self._limits.degrade_after_s:
            return TIER_SIMPLIFIED
        return TIER_CONSERVATIVE

    def _record_locked(self, to_tier: str, direction: str, reason: str, now: float) -> TierTransition:
        transition = TierTransition(self._tier, to_tier, direction, reason, float(now))
        self._transitions.append(transition)
        self._tier = to_tier
        return transition

    def _step_down_locked(self, now: float) -> TierTransition:
        """往更保守的方向降一档；进入保守档时按时段锁存切换方式。"""

        to_tier = _RANK_TIER[TIER_RANK[self._tier] + 1]
        if to_tier == TIER_SIMPLIFIED:
            self._held_supply_c = self._curve.supply_c()
            self._tier_target_c = self._held_supply_c
            self._effective_target_c = self._held_supply_c
            self._mode = _MODE_HOLD
            return self._record_locked(to_tier, "downgrade", REASON_STALE, now)
        self._tier_target_c = self._limits.conservative_supply_c
        hour = time.gmtime(float(now)).tm_hour
        if self._limits.in_ramp_window(hour):
            self._mode = _MODE_RAMP
        else:
            self._mode = _MODE_STEP
            self._effective_target_c = self._tier_target_c
        return self._record_locked(to_tier, "downgrade", REASON_STALE_TOO_LONG, now)

    def _step_up_locked(self, now: float) -> TierTransition:
        """气象恢复后往回升一档；升档立即采用该档目标，物理温度由热源自身速率跟上。"""

        to_tier = _RANK_TIER[TIER_RANK[self._tier] - 1]
        if to_tier == TIER_SIMPLIFIED:
            self._tier_target_c = self._held_supply_c
            self._mode = _MODE_HOLD
        else:
            self._tier_target_c = self._curve.supply_c()
            self._mode = _MODE_TRACK
        self._effective_target_c = self._tier_target_c
        return self._record_locked(to_tier, "upgrade", REASON_RECOVERED, now)

    def _advance_target_locked(self, dt: float) -> None:
        """按当前应用方式推进生效目标：跟随、保持、回滑或已完成切换。"""

        if self._mode == _MODE_TRACK:
            self._tier_target_c = self._curve.supply_c()
            self._effective_target_c = self._tier_target_c
            return
        if self._mode != _MODE_RAMP:
            return
        step = self._limits.degrade_rate_c_s * max(0.0, float(dt))
        delta = self._tier_target_c - self._effective_target_c
        if abs(delta) <= step:
            self._effective_target_c = self._tier_target_c
            self._mode = _MODE_HOLD
        elif step > 0.0:
            self._effective_target_c += step if delta > 0.0 else -step
