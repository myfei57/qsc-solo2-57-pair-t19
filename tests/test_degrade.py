"""曲线过期分档：按时间分档、按时段选择降档方式、时序与审计一致。"""

from __future__ import annotations

import dataclasses
import time

import pytest

from heatnet.config import default_config
from heatnet.errors import ForecastError
from heatnet.temp.curve import HeatCurve
from heatnet.temp.degrade import CurveGovernor


def _curve() -> HeatCurve:
    return HeatCurve(default_config().curve)


def _governor(curve: HeatCurve, **overrides) -> CurveGovernor:
    limits = dataclasses.replace(default_config().degrade, **overrides)
    return CurveGovernor(curve, limits)


def _at_hour(hour: int) -> float:
    """返回某个整点时刻（UTC），便于命中或避开回滑时段。"""

    base = 1_757_988_000.0
    midnight = base - time.gmtime(base).tm_hour * 3600.0
    return midnight + hour * 3600.0


def test_tiering_is_by_curve_age():
    curve = _curve()
    governor = _governor(curve)
    t0 = _at_hour(3)
    curve.apply_forecast(-10.0, t0)
    stale_after = default_config().curve.stale_after_s
    degrade_after = default_config().degrade.degrade_after_s

    assert governor.evaluate(t0 + stale_after - 1.0)["tier"] == "normal"
    assert governor.evaluate(t0 + stale_after + 1.0)["tier"] == "simplified"
    assert governor.evaluate(t0 + degrade_after + 1.0)["tier"] == "conservative"


def test_simplified_tier_holds_last_curve_value():
    curve = _curve()
    governor = _governor(curve)
    t0 = _at_hour(3)
    curve.apply_forecast(-10.0, t0)
    held = curve.supply_c()

    decision = governor.evaluate(t0 + 1801.0, 1.0)
    assert decision["tier"] == "simplified"
    assert decision["target_c"] == pytest.approx(held, abs=0.01)
    assert decision["partial_failure"] is True
    assert governor.target_c() == pytest.approx(held)


def test_conservative_switches_immediately_outside_ramp_window():
    curve = _curve()
    governor = _governor(curve)
    t0 = _at_hour(12)  # 正午，不在默认回滑时段 (22, 6) 内
    curve.apply_forecast(-10.0, t0)

    decision = governor.evaluate(t0 + 5401.0, 1.0)
    assert decision["tier"] == "conservative"
    assert decision["mode"] == "step"
    assert decision["target_c"] == pytest.approx(default_config().degrade.conservative_supply_c)


def test_conservative_ramps_inside_ramp_window():
    curve = _curve()
    governor = _governor(curve)
    t0 = _at_hour(3)  # 凌晨，落在默认回滑时段 (22, 6) 内
    curve.apply_forecast(-10.0, t0)
    held = curve.supply_c()
    conservative = default_config().degrade.conservative_supply_c
    rate = default_config().degrade.degrade_rate_c_s

    decision = governor.evaluate(t0 + 5401.0, 0.0)
    assert decision["tier"] == "conservative"
    assert decision["mode"] == "ramp"
    assert decision["target_c"] == pytest.approx(held, abs=0.01)

    moment = t0 + 5401.0
    for _ in range(10):
        moment += 10.0
        decision = governor.evaluate(moment, 10.0)
    expected = held - rate * 100.0
    assert expected > conservative
    assert decision["target_c"] == pytest.approx(expected, abs=0.01)
    assert governor.target_c() == pytest.approx(expected)
    assert decision["ramping"] is True

    while decision["ramping"]:
        moment += 10.0
        decision = governor.evaluate(moment, 10.0)
    assert decision["target_c"] == pytest.approx(conservative)
    assert decision["mode"] == "hold"


def test_recovery_climbs_one_tier_per_evaluation():
    curve = _curve()
    governor = _governor(curve)
    t0 = _at_hour(3)
    curve.apply_forecast(-10.0, t0)
    governor.evaluate(t0 + 5401.0, 0.0)
    assert governor.tier() == "conservative"

    t1 = t0 + 7200.0
    curve.apply_forecast(-5.0, t1)
    first = governor.evaluate(t1, 0.0)
    assert first["tier"] == "simplified"
    assert first["target_c"] == pytest.approx(governor.snapshot(t1)["held_supply_c"])

    second = governor.evaluate(t1 + 1.0, 1.0)
    assert second["tier"] == "normal"
    assert second["target_c"] == pytest.approx(curve.supply_c(), abs=0.01)
    assert governor.target_c() == pytest.approx(curve.supply_c())
    assert second["partial_failure"] is False


def test_partial_failure_only_allows_more_conservative_tiers():
    curve = _curve()
    governor = _governor(curve)
    t0 = _at_hour(3)
    curve.apply_forecast(-10.0, t0)

    moment = t0 + 1801.0
    for _ in range(5):
        moment += 60.0
        decision = governor.evaluate(moment, 60.0)
        assert decision["tier"] == "simplified"
        assert decision["partial_failure"] is True

    with pytest.raises(ForecastError):
        curve.apply_forecast(60.0, moment + 1.0)
    decision = governor.evaluate(moment + 1.0, 1.0)
    assert decision["tier"] == "simplified"

    decision = governor.evaluate(t0 + 5401.0, 1.0)
    assert decision["tier"] == "conservative"
    for _ in range(3):
        decision = governor.evaluate(t0 + 5401.0, 1.0)
        assert decision["tier"] != "normal"


def test_long_gap_degrades_step_by_step_in_order():
    curve = _curve()
    governor = _governor(curve)
    t0 = _at_hour(12)
    curve.apply_forecast(-10.0, t0)

    decision = governor.evaluate(t0 + 10000.0, 1.0)
    assert decision["tier"] == "conservative"
    hops = [(item["from_tier"], item["to_tier"]) for item in decision["transitions"]]
    assert hops == [("normal", "simplified"), ("simplified", "conservative")]
    assert [item["direction"] for item in decision["transitions"]] == ["downgrade", "downgrade"]


def test_platform_cycle_keeps_running_when_curve_expires(platform, clock):
    platform.start()
    platform.push_weather(-8.0)
    platform.startup(-8.0, 60.0)

    clock.advance(1801.0)
    report = platform.cycle(dt=1.0)
    assert report["ok"] is True
    assert report["tier"] == "simplified"
    assert "curve_stale" not in report["violations"]

    clock.advance(3600.0)
    report = platform.cycle(dt=1.0)
    assert report["ok"] is True
    assert report["tier"] == "conservative"


def test_tier_transitions_are_audited_and_match_console(platform, clock):
    platform.start()
    platform.push_weather(-8.0)

    clock.advance(1801.0)
    platform.cycle(dt=1.0)
    clock.advance(3600.0)
    platform.cycle(dt=1.0)
    platform.push_weather(-6.0)
    platform.cycle(dt=1.0)

    entries = [
        entry for entry in platform.audit.tail(100) if entry["action"] == "tier_transition"
    ]
    hops = [(entry["payload"]["from_tier"], entry["payload"]["to_tier"]) for entry in entries]
    assert hops == [
        ("normal", "simplified"),
        ("simplified", "conservative"),
        ("conservative", "simplified"),
        ("simplified", "normal"),
    ]
    assert [entry["payload"]["direction"] for entry in entries] == [
        "downgrade",
        "downgrade",
        "upgrade",
        "upgrade",
    ]
    assert entries[0]["ts"] <= entries[-1]["ts"]

    report = platform.curve_report()
    assert report["tier"] == "normal"
    assert report["governor"]["tier"] == report["tier"]
    assert report["governor"]["partial_failure"] is False
    assert platform.status()["curve"]["tier"] == report["tier"]


def test_console_exposes_current_tier(platform, clock):
    from heatnet.console.api import Api

    api = Api(platform)
    platform.start()
    platform.push_weather(-8.0)
    clock.advance(1801.0)
    platform.cycle(dt=1.0)

    status, payload = api.handle("GET", "/api/status", {}, {})
    assert status == 200
    assert payload["curve"]["tier"] == "simplified"
    assert payload["curve"]["governor"]["held_supply_c"] > 0.0

    status, curve = api.handle("GET", "/api/curve", {}, {})
    assert status == 200
    assert curve["tier"] == "simplified"
    assert curve["governor"]["ramp_windows"]

    status, ready = api.handle("GET", "/api/readiness", {}, {})
    assert status == 200
    assert ready["info"]["curve_tier"] == "simplified"
    assert ready["info"]["curve_partial_failure"] is True
