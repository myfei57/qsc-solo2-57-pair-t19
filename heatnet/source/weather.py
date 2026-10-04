"""气象推送与按曲线点火。

气象推送先更新供热曲线，再让热源按新曲线点火。曲线校验失败时整条气象推送作废：
不允许用越界温度覆盖最后一版有效曲线。缺报/越界属于部分失败，曲线仍可根据过期
时间降入简化或保守档；只有有效气象才能把档位带回恢复或正常。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ForecastError, PlantError
from ..temp.curve import HeatCurve
from .source import HeatSource


@dataclass(frozen=True)
class WeatherPush:
    """一次气象推送。"""

    outdoor_c: float | None
    issued_at: float
    station: str = "weather-center"

    def as_dict(self) -> dict:
        return {
            "outdoor_c": None if self.outdoor_c is None else round(self.outdoor_c, 2),
            "issued_at": round(self.issued_at, 3),
            "station": self.station,
        }


def push_weather(source: HeatSource, curve: HeatCurve, push: WeatherPush, now: float) -> dict:
    """按气象推送更新曲线并让热源点火；气象部分失败时只降档不锁链。"""

    steps: list[dict] = []
    violations: list[str] = []
    outdoor = None if push.outdoor_c is None else float(push.outdoor_c)

    try:
        if outdoor is None:
            raise ForecastError("气象缺报：未收到室外温度", component="temp")
        forecast = curve.apply_forecast(outdoor, now)
    except PlantError as error:
        transitions = curve.mark_weather_failure(now)
        for transition in transitions:
            detail = transition.as_dict()
            severity = "warning" if detail["tier"] in ("simplified", "conservative") else "notice"
            steps.append(
                {
                    "component": "temp",
                    "action": "tier_change",
                    "detail": {"severity": severity, **detail},
                }
            )
            if detail["tier"] == "conservative" and detail["mode"] == "immediate":
                try:
                    fired = source.fire(curve.supply_c())
                except PlantError as source_error:
                    violations.append(error.message)
                    violations.append(source_error.message)
                    return {
                        "ok": False,
                        "steps": steps,
                        "violations": violations,
                        "outdoor_c": None if outdoor is None else round(outdoor, 2),
                    }
                steps.append(
                    {
                        "component": "source",
                        "action": "fire_conservative",
                        "detail": {"temperature_c": round(fired, 2)},
                    }
                )
        violations.append(error.message)
        snapshot = curve.snapshot(now)
        return {
            "ok": False,
            "steps": steps,
            "violations": violations,
            "outdoor_c": None if outdoor is None else round(outdoor, 2),
            "supply_c": round(snapshot.supply_c, 2),
            "tier": snapshot.tier,
            "tier_label": snapshot.tier_label,
            "curve": snapshot.curve,
            "weather_failed": True,
        }
    steps.append({"component": "temp", "action": "apply_forecast", "detail": forecast})

    try:
        source.reset_quality()
        if curve.tier() == "recovering":
            source.set_target(curve.target_c())
            fired = source.temperature_c()
            action = "target_recovery"
        else:
            fired = source.fire(curve.supply_c())
            action = "fire_forecast"
    except PlantError as error:
        violations.append(error.message)
        return {
            "ok": False,
            "steps": steps,
            "violations": violations,
            "outdoor_c": forecast["outdoor_c"],
        }
    steps.append(
        {
            "component": "source",
            "action": action,
            "detail": {"temperature_c": round(fired, 2)},
        }
    )

    snapshot = curve.snapshot(now)
    return {
        "ok": True,
        "steps": steps,
        "violations": violations,
        "outdoor_c": forecast["outdoor_c"],
        "supply_c": round(curve.supply_c(), 2),
        "target_c": round(curve.target_c(), 2),
        "epoch": curve.epoch(),
        "tier": snapshot.tier,
        "tier_label": snapshot.tier_label,
        "curve": snapshot.curve,
        "transition_mode": snapshot.transition_mode,
        "weather_failed": False,
    }
