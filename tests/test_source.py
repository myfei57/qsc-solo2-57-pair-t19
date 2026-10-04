"""热源调质调量、气象推送与停机复位。"""

from __future__ import annotations

import dataclasses
import threading

import pytest

from heatnet.config import default_config
from heatnet.errors import ForecastError, InterlockError
from heatnet.primary.network import PrimaryNetwork
from heatnet.secondary.loop import SecondaryLoop
from heatnet.sequence.heating import advance_heating
from heatnet.source.adjust import apply_adjustment
from heatnet.source.source import HeatSource
from heatnet.source.weather import WeatherPush, push_weather
from heatnet.temp.curve import HeatCurve


def _source() -> HeatSource:
    return HeatSource(default_config().source)


def _curve() -> HeatCurve:
    return HeatCurve(default_config().curve)


def _immediate_config():
    config = default_config()
    return dataclasses.replace(
        config,
        curve=dataclasses.replace(
            config.curve, conservative_immediate_start_h=0, conservative_immediate_end_h=0
        ),
    )


def _ramped_config():
    config = default_config()
    return dataclasses.replace(
        config,
        curve=dataclasses.replace(
            config.curve, conservative_immediate_start_h=6, conservative_immediate_end_h=22
        ),
    )


def test_stale_curve_keeps_running_then_enters_conservative_tier():
    config = _immediate_config()
    source = HeatSource(config.source)
    curve = HeatCurve(config.curve)
    network = PrimaryNetwork(config.primary)
    loop = SecondaryLoop(config.secondary)
    push_weather(source, curve, WeatherPush(-10.0, 1000.0), 1000.0)
    first_supply = curve.supply_c()

    report = advance_heating(source, curve, network, loop, 1000.0, 2801.0, 1.0)
    assert report["ok"] is True
    assert report["tier"] == "simplified"
    assert curve.supply_c() == pytest.approx(first_supply)
    assert curve.is_stale(2801.0) is True

    advance_heating(source, curve, network, loop, 1000.0, 4601.0, 1.0)
    assert curve.tier() == "conservative"
    assert curve.curve_kind() == "conservative"
    assert curve.supply_c() == pytest.approx(first_supply + config.curve.conservative_offset_c)
    assert source.temperature_c() == pytest.approx(curve.supply_c())


def test_ramped_conservative_downgrade_moves_at_configured_rate():
    config = _ramped_config()
    source = HeatSource(config.source)
    curve = HeatCurve(config.curve)
    network = PrimaryNetwork(config.primary)
    loop = SecondaryLoop(config.secondary)
    push_weather(source, curve, WeatherPush(-10.0, 1000.0), 1000.0)
    start = curve.supply_c()

    advance_heating(source, curve, network, loop, 1000.0, 2801.0, 1.0)
    report = advance_heating(source, curve, network, loop, 1000.0, 4601.0, 1.0)
    assert report["tier"] == "conservative"
    assert report["curve"] == "conservative"
    assert curve.transition_mode() == "ramp"
    assert curve.supply_c() == pytest.approx(start + config.curve.fallback_rate_c_s)


def test_conservative_recovery_slides_back_to_normal_curve():
    config = _immediate_config()
    source = HeatSource(config.source)
    curve = HeatCurve(config.curve)
    network = PrimaryNetwork(config.primary)
    loop = SecondaryLoop(config.secondary)
    push_weather(source, curve, WeatherPush(-10.0, 1000.0), 1000.0)
    advance_heating(source, curve, network, loop, 1000.0, 2801.0, 1.0)
    advance_heating(source, curve, network, loop, 1000.0, 4601.0, 1.0)
    conservative_supply = curve.supply_c()

    recovered = push_weather(source, curve, WeatherPush(-12.0, 4700.0), 4700.0)
    assert recovered["ok"] is True
    assert recovered["tier"] == "recovering"
    assert recovered["curve"] == "fresh_forecast"
    assert source.temperature_c() == pytest.approx(conservative_supply)
    assert curve.target_c() < conservative_supply

    now = 4701.0
    while curve.tier() == "recovering":
        report = advance_heating(source, curve, network, loop, 1000.0, now, 1.0)
        now += 1.0
    assert report["tier"] == "normal"
    assert curve.supply_c() == pytest.approx(curve.target_c())
    assert any(step["action"] == "tier_change" for step in report["steps"])


def test_weather_failure_only_allows_downgrade_and_never_upgrade():
    config = _immediate_config()
    source = HeatSource(config.source)
    curve = HeatCurve(config.curve)
    push_weather(source, curve, WeatherPush(-10.0, 1000.0), 1000.0)

    failed = push_weather(source, curve, WeatherPush(None, 2801.0), 2801.0)
    assert failed["ok"] is False
    assert failed["weather_failed"] is True
    assert failed["tier"] == "simplified"
    assert failed["outdoor_c"] is None
    assert curve.weather_failed() is True
    assert curve.epoch() == 1
    assert curve.rejected() == 0

    failed_again = push_weather(source, curve, WeatherPush(60.0, 4601.0), 4601.0)
    assert failed_again["tier"] == "conservative"
    assert curve.tier() == "conservative"

    recovered = push_weather(source, curve, WeatherPush(-12.0, 4700.0), 4700.0)
    assert recovered["ok"] is True
    assert recovered["tier"] == "recovering"
    assert curve.weather_failed() is False


def test_adjust_writes_temperature_and_flow_together():
    source = _source()
    result = apply_adjustment(source, 90.0, 2000.0, "manual")
    assert result.ok is True
    assert result.temperature_c == pytest.approx(90.0)
    assert result.flow_t_h == pytest.approx(2000.0)
    snapshot = source.snapshot()
    assert snapshot.adjustments == 1
    assert snapshot.temperature_c == pytest.approx(90.0)


def test_invalid_adjustment_keeps_previous_state():
    source = _source()
    apply_adjustment(source, 90.0, 2000.0, "manual")
    rejected = apply_adjustment(source, 120.0, 2000.0, "manual")
    assert rejected.ok is False
    assert rejected.violations
    assert source.snapshot().temperature_c == pytest.approx(90.0)
    assert source.snapshot().flow_t_h == pytest.approx(2000.0)


def test_concurrent_adjust_never_tears_the_pair():
    source = _source()
    pairs = ((95.0, 2000.0), (85.0, 3000.0))
    seed = source.snapshot()
    allowed = set(pairs) | {(seed.temperature_c, seed.flow_t_h)}
    observed: list[tuple[float, float]] = []
    stop = threading.Event()

    def writer(temperature_c: float, flow_t_h: float) -> None:
        for _ in range(300):
            source.adjust(temperature_c, flow_t_h)

    def sampler() -> None:
        while not stop.is_set():
            snapshot = source.snapshot()
            observed.append((snapshot.temperature_c, snapshot.flow_t_h))

    watcher = threading.Thread(target=sampler)
    watcher.start()
    workers = [threading.Thread(target=writer, args=pair) for pair in pairs]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    stop.set()
    watcher.join()

    mixed = [pair for pair in observed if pair not in allowed]
    assert mixed == []


def test_quality_must_be_confirmed_before_quantity():
    source = _source()
    with pytest.raises(InterlockError):
        source.set_flow(1500.0)
    for _ in range(400):
        source.settle_quality(90.0, 1.0)
        if source.quality_confirmed():
            break
    assert source.quality_confirmed() is True
    assert source.set_flow(1500.0) == pytest.approx(1500.0)
    assert source.state() == "online"


def test_weather_push_updates_curve_and_fires_source():
    source = _source()
    curve = _curve()
    report = push_weather(source, curve, WeatherPush(-12.0, 1000.0), 1000.0)
    assert report["ok"] is True
    assert report["epoch"] == 1
    assert curve.outdoor_c() == pytest.approx(-12.0)
    assert source.target_c() == pytest.approx(curve.supply_c())
    assert [step["action"] for step in report["steps"]] == ["apply_forecast", "fire_forecast"]


def test_out_of_range_forecast_is_rejected_without_touching_curve():
    source = _source()
    curve = _curve()
    push_weather(source, curve, WeatherPush(-8.0, 1000.0), 1000.0)
    before_epoch = curve.epoch()
    before_supply = curve.supply_c()

    with pytest.raises(ForecastError):
        curve.validate(60.0)
    assert curve.rejected() == 1

    report = push_weather(source, curve, WeatherPush(60.0, 1100.0), 1100.0)
    assert report["ok"] is False
    assert curve.epoch() == before_epoch
    assert curve.supply_c() == pytest.approx(before_supply)
    assert "超出曲线适用区间" in report["violations"][0]


def test_trip_and_reset():
    source = _source()
    source.trip("overtemperature")
    assert source.state() == "tripped"
    assert source.trips() == 1
    with pytest.raises(InterlockError):
        source.adjust(90.0, 1200.0)
    with pytest.raises(InterlockError):
        source.fire(90.0)
    assert source.reset() == "idle"
    assert source.trip_reason() == ""
    with pytest.raises(InterlockError):
        source.reset()
