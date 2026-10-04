"""控制周期编排。

一个控制步先按最近一版有效气象的年龄同步曲线档位：正常与简化档使用最近有效曲线，
过期过久才进入保守档。随后实际供温按当前档位的切换方式逼近目标，一次网跟随热源，
二次网按当前流量推进回水。气象缺报只触发降档，控制链仍保持可运行。
"""

from __future__ import annotations

from ..errors import PlantError
from ..primary.network import PrimaryNetwork
from ..secondary.loop import SecondaryLoop
from ..source.source import HeatSource
from ..temp.curve import HeatCurve

MAX_STEPS = 600


def advance_heating(
    source: HeatSource,
    curve: HeatCurve,
    network: PrimaryNetwork,
    loop: SecondaryLoop,
    flow_t_h: float,
    now: float,
    dt: float = 1.0,
) -> dict:
    """推进一个控制周期，返回本周期摘要。"""

    if dt <= 0:
        raise ValueError("步长必须为正")
    steps: list[dict] = []
    violations: list[str] = []

    transitions = curve.sync(now)
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
                source.fire(curve.supply_c())
            except PlantError as error:
                violations.append(error.message)
                return {"ok": False, "steps": steps, "violations": violations, "stage": "temp"}
            steps.append(
                {
                    "component": "source",
                    "action": "fire_conservative",
                    "detail": {"temperature_c": round(source.temperature_c(), 2)},
                }
            )

    completed = curve.settle_active(dt, now)
    if completed is not None:
        steps.append(
            {
                "component": "temp",
                "action": "tier_change",
                "detail": {"severity": "notice", **completed.as_dict()},
            }
        )

    target = curve.supply_c()
    source.settle_quality(target, dt)
    snapshot = curve.snapshot(now)
    steps.append(
        {
            "component": "temp",
            "action": "curve_mode",
            "detail": {
                "tier": snapshot.tier,
                "tier_label": snapshot.tier_label,
                "curve": snapshot.curve,
                "transition_mode": snapshot.transition_mode,
                "target_c": round(curve.target_c(), 2),
                "supply_c": round(target, 2),
                "age_s": round(snapshot.age_s, 3),
                "weather_failed": snapshot.weather_failed,
                "basis": "forecast_age_s",
            },
        }
    )
    steps.append(
        {
            "component": "source",
            "action": "settle_quality",
            "detail": {
                "target_c": round(target, 2),
                "temperature_c": round(source.temperature_c(), 2),
                "hold_s": round(source.quality_hold_s(), 3),
            },
        }
    )
    if source.limits.overtemperature(source.temperature_c()):
        source.trip("overtemperature")
        violations.append("overtemperature")
        return {"ok": False, "steps": steps, "violations": violations, "stage": "source"}

    network.set_supply(source.temperature_c())
    network.set_return(loop.return_c())
    network.set_flow(flow_t_h)
    steps.append(
        {"component": "primary", "action": "follow_source", "detail": network.snapshot().as_dict()}
    )

    balance = loop.step(dt, flow_t_h)
    steps.append({"component": "secondary", "action": "step", "detail": balance})
    if balance["overheat"]:
        violations.append("return_overtemperature")
    if balance["needs_makeup"]:
        violations.append("makeup_required")

    for _ in range(MAX_STEPS):
        if not loop.needs_makeup():
            break
        loop.makeup_step(dt)
    steps.append(
        {
            "component": "secondary",
            "action": "makeup",
            "detail": {"pressure_kpa": round(loop.pressure_kpa(), 2)},
        }
    )

    return {
        "ok": not violations,
        "steps": steps,
        "violations": violations,
        "stage": "done",
        "tier": snapshot.tier,
        "tier_label": snapshot.tier_label,
        "curve": snapshot.curve,
        "temperature_c": round(source.temperature_c(), 2),
        "return_c": round(loop.return_c(), 2),
    }
