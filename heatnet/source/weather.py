"""气象推送与按档位点火。

气象推送先更新供热曲线，再让档位管理器评估当前档位，热源按档位给出的目标
点火。曲线校验失败时必须整条推送作废：吞掉校验错误继续点火，热源会按
上一版（甚至过期）的曲线运行，室外温度越冷反而供温越低。校验失败的推送
属于部分失败，不刷新曲线，档位只许往更保守的方向走。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import PlantError
from ..temp.curve import HeatCurve
from ..temp.degrade import CurveGovernor
from .source import HeatSource


@dataclass(frozen=True)
class WeatherPush:
    """一次气象推送。"""

    outdoor_c: float
    issued_at: float
    station: str = "weather-center"

    def as_dict(self) -> dict:
        return {
            "outdoor_c": round(self.outdoor_c, 2),
            "issued_at": round(self.issued_at, 3),
            "station": self.station,
        }


def push_weather(
    source: HeatSource,
    curve: HeatCurve,
    governor: CurveGovernor,
    push: WeatherPush,
    now: float,
) -> dict:
    """按气象推送更新曲线，评估档位并让热源按档位目标点火。"""

    steps: list[dict] = []
    violations: list[str] = []

    try:
        forecast = curve.apply_forecast(push.outdoor_c, now)
    except PlantError as error:
        return {
            "ok": False,
            "steps": steps,
            "violations": [error.message],
            "outdoor_c": round(float(push.outdoor_c), 2),
            "tier": governor.tier(),
        }
    steps.append({"component": "temp", "action": "apply_forecast", "detail": forecast})

    decision = governor.evaluate(now, 0.0)
    steps.append(
        {
            "component": "temp",
            "action": "tier",
            "detail": {
                "tier": decision["tier"],
                "target_c": decision["target_c"],
                "mode": decision["mode"],
                "age_s": decision["age_s"],
                "partial_failure": decision["partial_failure"],
            },
        }
    )

    try:
        fired = source.fire(governor.target_c())
    except PlantError as error:
        violations.append(error.message)
        return {
            "ok": False,
            "steps": steps,
            "violations": violations,
            "outdoor_c": forecast["outdoor_c"],
            "tier": decision["tier"],
            "transitions": decision["transitions"],
        }
    steps.append(
        {
            "component": "source",
            "action": "fire_forecast",
            "detail": {"temperature_c": round(fired, 2), "tier": decision["tier"]},
        }
    )

    return {
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "outdoor_c": forecast["outdoor_c"],
        "supply_c": round(governor.target_c(), 2),
        "epoch": curve.epoch(),
        "tier": decision["tier"],
        "partial_failure": decision["partial_failure"],
        "transitions": decision["transitions"],
    }
