# -*- coding: utf-8 -*-
"""Checks for the Planner playback clock and map-overlay lifecycle.

``SimulationController`` (constant and task-paced playback, seeking, stepping
between schedule boundaries, clamping at the plan end) and
``PlannerMapOverlay`` (the canvas ``extentsChanged`` connection exists only
while it has items and is dropped by an idempotent ``clear``).

Requires the QGIS API (run via tests/run_qgis_smoke_tests.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..planner.timeline_engine import TaskSpec, compute_schedule


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    msg = f"[{tag}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    return ok


_ANCHOR = datetime(2026, 1, 1)


def _schedule():
    # a: 0-2 h, b: 2-12 h (sequential on one resource)
    tasks = [
        TaskSpec("a", 0, resource_id="v1", duration_hours=2),
        TaskSpec("b", 1, resource_id="v1", duration_hours=10, predecessor_task_id="a"),
    ]
    return compute_schedule(_ANCHOR, tasks)


class _Elapsed:
    """Deterministic stand-in for QElapsedTimer (milliseconds per restart)."""

    def __init__(self, step_ms: int):
        self.step_ms = step_ms

    def start(self):
        pass

    def restart(self):
        return self.step_ms


def test_sim_controller_constant_and_seek() -> bool:
    from ..planner.sim_controller import SimulationController

    sim = SimulationController()
    times, playing = [], []
    sim.timeChanged.connect(times.append)
    sim.playingChanged.connect(playing.append)
    result = _schedule()
    sim.set_result(result)
    ok = sim.current_time == result.span_start == _ANCHOR
    sim.seek_fraction(0.5)
    ok = ok and sim.current_time == _ANCHOR + timedelta(hours=6)
    sim.seek_fraction(7.0)  # clamped
    ok = ok and sim.current_time == result.span_end

    # play restarts from the start once at the end, then advances at the rate
    sim.set_speed(3600.0)  # 1 simulated hour per real second
    sim.play()
    ok = ok and sim.is_playing() and playing[-1] is True
    ok = ok and sim.current_time == _ANCHOR
    sim.elapsed = _Elapsed(500)
    sim._tick()
    ok = ok and sim.current_time == _ANCHOR + timedelta(minutes=30)
    # a huge tick clamps to the plan end and stops playback
    sim.elapsed = _Elapsed(10 ** 7)
    sim._tick()
    ok = ok and sim.current_time == result.span_end and not sim.is_playing()
    ok = ok and playing[-1] is False and times[-1] == result.span_end
    sim.shutdown()
    return _result("sim controller: constant rate, seek, clamp at end", ok,
                   f"time={sim.current_time}")


def test_sim_controller_boundaries_and_task_pace() -> bool:
    from ..planner.sim_controller import SimulationController

    sim = SimulationController()
    sim.set_result(_schedule())
    boundaries = [_ANCHOR, _ANCHOR + timedelta(hours=2), _ANCHOR + timedelta(hours=12)]
    ok = sim._boundaries() == boundaries
    sim.step_boundary(1)
    ok = ok and sim.current_time == boundaries[1]
    sim.step_boundary(1)
    sim.step_boundary(1)  # past the last boundary: stays at the end
    ok = ok and sim.current_time == boundaries[2]
    sim.step_boundary(-1)
    ok = ok and sim.current_time == boundaries[1]

    # task pace: every interval takes the same wall time (2 s), however long
    sim.seek_fraction(0.0)
    sim.set_task_pace(2.0)
    sim.play()
    sim.elapsed = _Elapsed(1000)  # half of the 2 h interval
    sim._tick()
    ok = ok and sim.current_time == _ANCHOR + timedelta(hours=1)
    sim._tick()  # reaches the 2 h boundary
    sim._tick()  # half of the 10 h interval
    ok = ok and sim.current_time == _ANCHOR + timedelta(hours=7)

    # clearing the result pauses and forgets the clock
    sim.set_result(None)
    ok = ok and sim.current_time is None and not sim.is_playing()
    sim.shutdown()
    return _result("sim controller: boundary stepping + task pace", ok)


def _extents_receivers(canvas) -> int:
    return canvas.receivers(canvas.extentsChanged)


def test_map_overlay_signal_lifecycle() -> bool:
    """Regression: the overlay connected canvas.extentsChanged and never
    disconnected, so a closed planner kept a slot on the map canvas."""
    from qgis.core import QgsPointXY
    from qgis.gui import QgsMapCanvas

    from ..planner.map_overlay import PlannerMapOverlay

    canvas = QgsMapCanvas()
    base = _extents_receivers(canvas)
    overlay = PlannerMapOverlay(canvas)
    ok = _extents_receivers(canvas) == base  # nothing until items exist
    overlay.show_point("vessel", QgsPointXY(1.0, 2.0), label_text="Vessel")
    overlay.show_point("barge", QgsPointXY(3.0, 4.0))
    connected = _extents_receivers(canvas)
    ok = ok and connected == base + 1  # one connection, not one per item
    items_before = len(canvas.scene().items())
    overlay.clear()
    ok = ok and _extents_receivers(canvas) == base
    ok = ok and len(canvas.scene().items()) <= items_before - 8
    overlay.clear()  # idempotent
    ok = ok and _extents_receivers(canvas) == base
    overlay.show_point("vessel", QgsPointXY(1.0, 2.0))  # reusable after clear
    ok = ok and _extents_receivers(canvas) == base + 1
    canvas.setExtent(canvas.extent())  # labels reposition without error
    overlay.clear()
    ok = ok and _extents_receivers(canvas) == base
    return _result("planner map overlay disconnects on clear (idempotent)", ok,
                   f"base={base} connected={connected}")


def run_all() -> list:
    return [
        test_sim_controller_constant_and_seek(),
        test_sim_controller_boundaries_and_task_pace(),
        test_map_overlay_signal_lifecycle(),
    ]


if __name__ == "__main__":
    results = run_all()
    raise SystemExit(0 if all(results) else 1)
