"""控制周期编排。

一个控制步里：先由档位管理器按曲线龄期评估当前档位（正常／简化／保守），
再把热源温度往该档位对应的目标供温逼近，随后一次网跟随热源，二次网按当前
流量推进回水。曲线过期不再让整链停摆：刚过期按简化档定点运行，过期太久
按保守档运行，迁移逐跳落审计。任何一环越界都记进违约项返回，由平台决定
是否停机。
"""

from __future__ import annotations

from ..primary.network import PrimaryNetwork
from ..secondary.loop import SecondaryLoop
from ..source.source import HeatSource
from ..temp.degrade import CurveGovernor

MAX_STEPS = 600


def advance_heating(
    source: HeatSource,
    governor: CurveGovernor,
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

    decision = governor.evaluate(now, dt)
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

    target = governor.target_c()
    source.settle_quality(target, dt)
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
        "tier": decision["tier"],
        "transitions": decision["transitions"],
        "temperature_c": round(source.temperature_c(), 2),
        "return_c": round(loop.return_c(), 2),
    }
