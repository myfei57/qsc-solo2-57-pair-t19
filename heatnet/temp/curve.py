"""供热曲线与气象校验。

曲线分档只按最近一版有效气象的经过时间判定：正常档使用最新气象曲线；刚过期进入
简化档，仍按最近一版有效曲线继续运行；过期过久进入保守档，在最近有效曲线上叠加
固定保守偏置。气象恢复后先进入恢复档，实际供温按固定速率滑回新曲线，到位后才回
正常档。气象缺报是部分失败：它会被记录并禁止因此升档，但不会锁住点火链。
"""

from __future__ import annotations

from ..config import CurveLimits
from ..errors import ForecastError
from .state import (
    CONSERVATIVE,
    CURVE_CONSERVATIVE,
    CURVE_LAST_VALID,
    CURVE_RECOVERY,
    NORMAL,
    RECOVERING,
    SIMPLIFIED,
    CurveSnapshot,
    CurveTransition,
    STALE_RANKS,
    TIER_LABELS,
)
from .table import CurveTable


class HeatCurve:
    """按气象更新并带过期分档的供热曲线。"""

    def __init__(self, limits: CurveLimits) -> None:
        self._limits = limits
        self._table = CurveTable(limits)
        self._outdoor_c = (limits.outdoor_min_c + limits.outdoor_max_c) / 2.0
        self._supply_c = self._table.interpolate(self._outdoor_c)
        self._target_c = self._supply_c
        self._active_c = self._supply_c
        self._updated_at = 0.0
        self._age_s = 0.0
        self._epoch = 0
        self._validated = 0
        self._rejected = 0
        self._tier = NORMAL
        self._curve = CURVE_LAST_VALID
        self._transition_mode = "hold"
        self._weather_failed = False
        self._has_forecast = False

    @property
    def limits(self) -> CurveLimits:
        return self._limits

    @property
    def table(self) -> CurveTable:
        return self._table

    def outdoor_c(self) -> float:
        """返回当前有效气象对应的室外温度。"""

        return self._outdoor_c

    def supply_c(self) -> float:
        """返回当前正在执行的目标供水温度。"""

        return self._active_c

    def target_c(self) -> float:
        """返回当前档位的目标供水温度。"""

        return self._target_c

    def epoch(self) -> int:
        """返回曲线代际，每次成功更新加一。"""

        return self._epoch

    def updated_at(self) -> float:
        """返回最近一次成功更新的时刻。"""

        return self._updated_at

    def age_s(self) -> float:
        """返回最近一版有效气象已经经过的秒数。"""

        return self._age_s

    def tier(self) -> str:
        """返回当前档位标识。"""

        return self._tier

    def curve_kind(self) -> str:
        """返回当前使用的曲线类型。"""

        return self._curve

    def transition_mode(self) -> str:
        """返回当前降档或恢复的切换方式。"""

        return self._transition_mode

    def weather_failed(self) -> bool:
        """最近一次气象推送是否为缺报或校验失败的部分失败。"""

        return self._weather_failed

    def validated(self) -> int:
        """返回累计通过的校验次数。"""

        return self._validated

    def rejected(self) -> int:
        """返回累计被拒绝的校验次数。"""

        return self._rejected

    def validate(self, outdoor_c: float) -> float:
        """校验室外温度是否落在适用区间内，越界即抛出。"""

        value = float(outdoor_c)
        if not self._limits.outdoor_in_range(value):
            self._rejected += 1
            raise ForecastError(
                f"室外温度 {value:.1f}℃ 超出曲线适用区间 "
                f"[{self._limits.outdoor_min_c:.1f}, {self._limits.outdoor_max_c:.1f}]",
                component="temp",
            )
        self._validated += 1
        return value

    def _tier_for_age(self, age_s: float) -> str:
        if age_s > self._limits.conservative_after_s:
            return CONSERVATIVE
        if age_s > self._limits.stale_after_s:
            return SIMPLIFIED
        return NORMAL

    def _conservative_supply(self) -> float:
        return min(
            self._supply_c + self._limits.conservative_offset_c,
            self._limits.supply_high_c + self._limits.conservative_offset_c,
        )

    def _enter(self, tier: str, curve: str, mode: str, now: float, reason: str) -> CurveTransition:
        previous = self._tier
        self._tier = tier
        self._curve = curve
        self._transition_mode = mode
        if tier == NORMAL:
            self._target_c = self._supply_c
        elif tier == SIMPLIFIED:
            self._target_c = self._supply_c
        elif tier == CONSERVATIVE:
            self._target_c = self._conservative_supply()
            if mode == "immediate":
                self._active_c = self._target_c
        return CurveTransition(previous, tier, curve, now, reason, mode, self._age_s)

    def sync(self, now: float) -> list[CurveTransition]:
        """按当前时间推进过期档位，每次最多保留一档，避免跳档打乱时序。"""

        moment = float(now)
        transitions: list[CurveTransition] = []
        if not self._has_forecast:
            return transitions

        self._age_s = max(0.0, moment - self._updated_at)
        due = self._tier_for_age(self._age_s)

        if self._tier == RECOVERING and due == CONSERVATIVE:
            immediate = self._limits.conservative_immediate_now(moment)
            mode = "immediate" if immediate else "ramp"
            transitions.append(
                self._enter(CONSERVATIVE, CURVE_CONSERVATIVE, mode, moment, "expired")
            )
            return transitions

        if self._tier in STALE_RANKS:
            current_rank = STALE_RANKS.index(self._tier)
            due_rank = STALE_RANKS.index(due)
            if due_rank > current_rank:
                next_tier = STALE_RANKS[current_rank + 1]
                if next_tier == SIMPLIFIED:
                    transitions.append(
                        self._enter(SIMPLIFIED, CURVE_LAST_VALID, "hold", moment, "expired")
                    )
                else:
                    immediate = self._limits.conservative_immediate_now(moment)
                    mode = "immediate" if immediate else "ramp"
                    transitions.append(
                        self._enter(CONSERVATIVE, CURVE_CONSERVATIVE, mode, moment, "expired")
                    )

        # 下一档可以立即确定，但不能跳过中间档；这次同步先进入中间档，下一次同步再继续。
        return transitions

    def mark_weather_failure(self, now: float) -> list[CurveTransition]:
        """登记气象部分失败，再按时间降档；该调用永远不会升档。"""

        self._weather_failed = True
        return self.sync(now)

    def apply_forecast(self, outdoor_c: float, now: float) -> dict:
        """校验后覆盖气象曲线，并按当前档位决定立即恢复还是滑回。"""

        value = self.validate(outdoor_c)
        moment = float(now)
        self._weather_failed = False
        self._outdoor_c = value
        self._supply_c = self._table.interpolate(value)
        self._updated_at = moment
        self._age_s = 0.0
        self._epoch += 1

        if self._tier == CONSERVATIVE or self._tier == RECOVERING:
            self._tier = RECOVERING
            self._curve = CURVE_RECOVERY
            self._transition_mode = "ramp"
            self._target_c = self._supply_c
        else:
            self._tier = NORMAL
            self._curve = CURVE_LAST_VALID
            self._transition_mode = "immediate"
            self._target_c = self._supply_c
            self._active_c = self._supply_c
        self._has_forecast = True
        return self.snapshot(moment).as_dict()

    def settle_active(self, dt: float, now: float) -> CurveTransition | None:
        """按降级速率推进实际曲线；恢复到位时切回正常档。"""

        if dt <= 0:
            raise ValueError("步长必须为正")
        step = self._limits.fallback_rate_c_s * dt
        delta = self._target_c - self._active_c
        if abs(delta) <= step:
            self._active_c = self._target_c
        elif delta > 0:
            self._active_c += step
        else:
            self._active_c -= step

        if self._tier == RECOVERING and self._active_c == self._target_c:
            self._tier = NORMAL
            self._curve = CURVE_LAST_VALID
            self._transition_mode = "complete"
            return CurveTransition(
                RECOVERING,
                NORMAL,
                CURVE_LAST_VALID,
                float(now),
                "recovered",
                "complete",
                self._age_s,
            )
        return None

    def is_stale(self, now: float | None = None) -> bool:
        """曲线是否已离开正常新鲜档。"""

        if not self._has_forecast:
            return False
        moment = self._updated_at if now is None else float(now)
        return (moment - self._updated_at) > self._limits.stale_after_s

    def snapshot(self, now: float | None = None) -> CurveSnapshot:
        """返回曲线状态快照。"""

        if now is not None and self._has_forecast:
            self._age_s = max(0.0, float(now) - self._updated_at)
        stale = self.is_stale(now)
        return CurveSnapshot(
            outdoor_c=self._outdoor_c,
            supply_c=self._active_c,
            target_c=self._target_c,
            epoch=self._epoch,
            updated_at=self._updated_at,
            age_s=self._age_s,
            stale=stale,
            runnable=True,
            weather_failed=self._weather_failed,
            tier=self._tier,
            tier_label=TIER_LABELS[self._tier],
            curve=self._curve,
            transition_mode=self._transition_mode,
            basis="forecast_age_s",
            validated=self._validated,
            rejected=self._rejected,
        )
