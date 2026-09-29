# -*- coding: utf-8 -*-
"""Lay Simulator / BU Lowering dialog lifecycle (QGIS smoke tests).

* Inputs edited while a solve runs never leave its result unmarked, and
  Solve restarts a superseded static solve so the result finally shown
  matches the final inputs (the old "pending" re-solve was never serviced
  because the result arrives while the worker thread is still running).
* Every solve's worker is released and deleted once its thread has ended.
* ``shutdown()`` (plugin unload) stops a running solve with a bounded wait,
  cancels a map pick, removes the canvas overlay, closes the dialog, is
  idempotent, and a solve still finishing afterwards never reaches it.
* The table resize grips exist and drag on both Qt5 and Qt6 (Qt6 has no
  unscoped ``Qt.SizeVerCursor`` and no ``QMouseEvent.globalY``).

The solver is gated (monkeypatched) so "while it runs" is deterministic.
Settings go to a throwaway INI file: nothing touches the user's profile.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import shutil
import tempfile
import threading
import time
from typing import Callable, List

try:
    from qgis.core import QgsApplication
    from qgis.PyQt import sip
    from qgis.PyQt.QtCore import QCoreApplication, QEvent, QPointF, QSettings, Qt
    from qgis.PyQt.QtGui import QMouseEvent
    HAVE_QGIS = True
except ImportError:  # pure runner without QGIS
    HAVE_QGIS = False

REQUIRES_QGIS = True                 # for the runners' pure/QGIS classification

_SCRATCH_DIR = None                  # per run_all(); holds the throwaway INIs
_SCRATCH_IDS = itertools.count()


def _modules():
    from ..catenary.v3.ui import bu_lowering_dialog as bu_mod
    from ..catenary.v3.ui import dialog as lay_mod
    from ..catenary.v3.ui import solve_controller as sc
    return lay_mod, bu_mod, sc


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _pump(timeout_s: float, until: Callable[[], bool]) -> bool:
    """Process queued signals (and deferred deletes) until ``until()``."""
    deferred = getattr(QEvent, "Type", QEvent).DeferredDelete
    deferred = getattr(deferred, "value", deferred)
    deadline = time.monotonic() + timeout_s
    while True:
        QCoreApplication.processEvents()
        QCoreApplication.sendPostedEvents(None, int(deferred))
        if until():
            return True
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)


@contextlib.contextmanager
def _scratch_settings(*modules):
    """Dialogs built inside read/write a fresh temporary INI instead of the
    user's QGIS settings (factory defaults; nothing persisted — run_all()
    deletes the folder)."""
    path = os.path.join(_SCRATCH_DIR, f"settings_{next(_SCRATCH_IDS)}.ini")
    ini = getattr(QSettings, "Format", QSettings).IniFormat
    saved = [(m, m.QSettings) for m in modules]
    for m in modules:
        m.QSettings = lambda *_a, **_k: QSettings(path, ini)
    try:
        yield
    finally:
        for m, real in saved:
            m.QSettings = real


class _Gate:
    """Replace ``solve_controller.<name>`` with a pass-through that blocks
    until released, recording every config and output."""

    def __init__(self, sc, name: str):
        self._sc, self._name = sc, name
        self._real = getattr(sc, name)
        self.go = threading.Event()
        self.started = threading.Event()
        self.configs: List = []
        self.outputs: List = []
        setattr(sc, name, self)

    def __call__(self, cfg, *args, **kwargs):
        self.configs.append(cfg)
        self.started.set()
        self.go.wait(60)
        out = self._real(cfg, *args, **kwargs)
        self.outputs.append(out)
        return out

    def restore(self):
        setattr(self._sc, self._name, self._real)
        self.go.set()


def _lay_dialog(lay_mod, iface=None):
    dlg = lay_mod.LaySimulatorDialog(None, iface=iface)
    dlg.scenario_choice.setCurrentIndex(dlg.scenario_choice.findData("single_static"))
    dlg.bathy_mode.setCurrentIndex(dlg.bathy_mode.findData("flat"))
    dlg.depth_spin.setValue(60.0)
    dlg.adv_ds.setValue(10.0)            # coarse mesh: quick solves
    return dlg


def _depth(cfg) -> float:
    return float(cfg.bathymetry["depth_m"])


def _dispose(dlg):
    dlg.shutdown()
    dlg.deleteLater()
    _pump(2.0, lambda: sip.isdeleted(dlg))


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_solve_restarts_on_edited_inputs_and_shows_the_final_ones():
    lay_mod, _bu, sc = _modules()
    with _scratch_settings(lay_mod):
        dlg = _lay_dialog(lay_mod)
    gate = _Gate(sc, "run_static")
    try:
        dlg._solve_clicked()
        first = dlg._worker
        assert first is not None and gate.started.wait(10)
        dlg.depth_spin.setValue(80.0)                 # edit mid-solve
        assert dlg._dirty
        assert dlg.run_btn.isEnabled(), "a static solve must be restartable"
        dlg._solve_clicked()                          # restart on the new inputs
        assert dlg._pending
        gate.go.set()
        assert _pump(60.0, lambda: dlg._worker is None and len(gate.outputs) == 2), \
            "the queued re-solve never ran"
        assert [_depth(c) for c in gate.configs] == [60.0, 80.0]
        assert gate.outputs[0].error == "cancelled"  # superseded run was stopped
        assert dlg._last_out is gate.outputs[1], "shown result is not the final solve"
        assert not dlg._last_out.error, dlg._last_out.error
        assert not dlg._dirty and dlg.dirty_label.text() == ""
        assert dlg.run_btn.isEnabled() and not dlg.cancel_btn.isEnabled()
        assert _pump(5.0, lambda: sip.isdeleted(first)), "finished worker leaked"
    finally:
        gate.restore()
        _dispose(dlg)


def test_edits_during_a_solve_leave_its_result_marked_stale():
    lay_mod, _bu, sc = _modules()
    with _scratch_settings(lay_mod):
        dlg = _lay_dialog(lay_mod)
    gate = _Gate(sc, "run_static")
    try:
        dlg._solve_clicked()
        worker = dlg._worker
        assert gate.started.wait(10)
        dlg.depth_spin.setValue(70.0)                 # edit, but no new Solve
        dlg._on_progress(-1.0, "iteration readout")   # progress text in the label
        gate.go.set()
        assert _pump(60.0, lambda: dlg._worker is None and gate.outputs)
        assert [_depth(c) for c in gate.configs] == [60.0], "must not auto-solve"
        assert dlg._last_out is gate.outputs[0] and not dlg._last_out.error
        assert dlg._dirty
        assert dlg.dirty_label.text().startswith("Inputs changed"), dlg.dirty_label.text()
        assert _pump(5.0, lambda: sip.isdeleted(worker)), "finished worker leaked"
    finally:
        gate.restore()
        _dispose(dlg)


def test_shutdown_stops_the_solve_and_clears_the_canvas():
    from qgis.gui import QgsMapCanvas, QgsMapToolPan, QgsRubberBand

    lay_mod, _bu, sc = _modules()

    class _Iface:
        def __init__(self):
            self.canvas = QgsMapCanvas()

        def mapCanvas(self):
            return self.canvas

    iface = _Iface()
    canvas = iface.canvas
    pan = QgsMapToolPan(canvas)
    canvas.setMapTool(pan)

    def bands():
        return [i for i in canvas.scene().items() if isinstance(i, QgsRubberBand)]

    with _scratch_settings(lay_mod):
        dlg = _lay_dialog(lay_mod, iface=iface)
    dlg.show_on_map.setChecked(True)
    dlg._solve_clicked()                              # real solve -> overlay
    assert _pump(60.0, lambda: dlg._worker is None)
    assert not dlg._last_out.error, dlg._last_out.error
    assert bands(), "the solved scene should be drawn on the canvas"
    shown = dlg._last_out

    saved_wait = lay_mod._SHUTDOWN_WAIT_MS
    lay_mod._SHUTDOWN_WAIT_MS = 200                   # don't sit out 3 s here
    gate = _Gate(sc, "run_static")
    try:
        dlg.depth_spin.setValue(90.0)
        dlg._solve_clicked()
        worker = dlg._worker
        assert gate.started.wait(10)
        dlg._pick_position_only()                     # a map pick in progress
        assert dlg._pick_tool is not None and canvas.mapTool() is not pan

        dlg.shutdown()                                # solve still blocked
        assert dlg._worker is None and dlg._pick_tool is None
        assert dlg._map_overlay is None and not bands(), "overlay left on the canvas"
        assert canvas.mapTool() is pan, "the previous map tool was not restored"
        assert worker.parent() is not dlg, "running worker would die with the dialog"
        dlg.shutdown()                                # idempotent

        gate.go.set()                                 # the orphan finishes...
        assert worker.wait(30000)
        assert _pump(5.0, lambda: sip.isdeleted(worker)), "orphaned worker leaked"
        assert dlg._last_out is shown, "a result reached the shut-down dialog"
        assert not bands()
    finally:
        gate.restore()
        lay_mod._SHUTDOWN_WAIT_MS = saved_wait
        dlg.deleteLater()
        _pump(2.0, lambda: sip.isdeleted(dlg))
        canvas.setMapTool(pan)


def test_bu_lowering_shutdown_stops_the_run():
    _lay, bu_mod, sc = _modules()
    with _scratch_settings(bu_mod):
        dlg = bu_mod.BULoweringDialog(None, iface=None)
    saved_wait = bu_mod._SHUTDOWN_WAIT_MS
    bu_mod._SHUTDOWN_WAIT_MS = 200
    gate = _Gate(sc, "run_operation")
    try:
        dlg._run_quick()
        worker = dlg._worker
        assert worker is not None and gate.started.wait(10)
        assert not dlg.run_btn.isEnabled() and dlg.cancel_btn.isEnabled()
        dlg.shutdown()
        dlg.shutdown()                                # idempotent
        assert dlg._worker is None
        assert worker.parent() is not dlg
        gate.go.set()
        assert worker.wait(60000)
        assert _pump(5.0, lambda: sip.isdeleted(worker)), "orphaned worker leaked"
        assert dlg._last_out is None, "a result reached the shut-down dialog"
    finally:
        gate.restore()
        bu_mod._SHUTDOWN_WAIT_MS = saved_wait
        dlg.deleteLater()
        _pump(2.0, lambda: sip.isdeleted(dlg))


def test_bu_lowering_run_releases_its_worker():
    _lay, bu_mod, sc = _modules()
    with _scratch_settings(bu_mod):
        dlg = bu_mod.BULoweringDialog(None, iface=None)
    try:
        dlg._run_quick()
        worker = dlg._worker
        assert worker is not None
        assert _pump(120.0, lambda: dlg._worker is None), "run never finished"
        assert dlg._last_out is not None and not dlg._last_out.error, dlg._last_out.error
        assert dlg.run_btn.isEnabled() and not dlg.cancel_btn.isEnabled()
        assert _pump(5.0, lambda: sip.isdeleted(worker)), "finished worker leaked"
    finally:
        _dispose(dlg)


def test_table_resize_grips_exist_and_drag():
    lay_mod, _bu, _sc = _modules()
    with _scratch_settings(lay_mod):
        dlg = _lay_dialog(lay_mod)
    try:
        grips = dlg.findChildren(lay_mod._TableResizeGrip)
        assert len(grips) >= 4, f"only {len(grips)} table grips were added"
        size_ver = getattr(Qt, "CursorShape", Qt).SizeVerCursor
        assert all(g.cursor().shape() == size_ver for g in grips)

        grip = next(g for g in grips if g._table is dlg.asm_table)
        start_h = dlg.asm_table.height()
        etype = getattr(QEvent, "Type", QEvent)
        left = getattr(Qt, "MouseButton", Qt).LeftButton
        no_mod = getattr(Qt, "KeyboardModifier", Qt).NoModifier

        def event(kind, global_y):
            return QMouseEvent(kind, QPointF(5.0, 5.0), QPointF(50.0, float(global_y)),
                               left, left, no_mod)

        grip.mousePressEvent(event(etype.MouseButtonPress, 100))
        grip.mouseMoveEvent(event(etype.MouseMove, 180))
        grip.mouseReleaseEvent(event(etype.MouseButtonRelease, 180))
        want = max(60, start_h + 80)
        assert dlg.asm_table._manual_height
        assert dlg.asm_table.minimumHeight() == want == dlg.asm_table.maximumHeight()
    finally:
        _dispose(dlg)


def run_all() -> List[bool]:
    global _SCRATCH_DIR
    if not HAVE_QGIS or not isinstance(QgsApplication.instance(), QgsApplication):
        print("[SKIP] dialog lifecycle tests need a GUI QgsApplication "
              "(run via tests/run_qgis_smoke_tests.py)")
        return []
    _SCRATCH_DIR = tempfile.mkdtemp(prefix="sct_lay_settings_")
    results: List[bool] = []
    try:
        for test in (
            test_solve_restarts_on_edited_inputs_and_shows_the_final_ones,
            test_edits_during_a_solve_leave_its_result_marked_stale,
            test_shutdown_stops_the_solve_and_clears_the_canvas,
            test_bu_lowering_shutdown_stops_the_run,
            test_bu_lowering_run_releases_its_worker,
            test_table_resize_grips_exist_and_drag,
        ):
            try:
                test()
                print(f"[PASS] {test.__name__}")
                results.append(True)
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"[FAIL] {test.__name__} - {exc!r}")
                results.append(False)
    finally:
        shutil.rmtree(_SCRATCH_DIR, ignore_errors=True)
        _SCRATCH_DIR = None
    return results


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs QGIS and the plugin package).")
