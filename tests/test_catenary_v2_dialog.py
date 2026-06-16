# -*- coding: utf-8 -*-
"""Functional checks for the Catenary Calculator V2 dialog.

Exercises the dialog logic headlessly (no event loop, nothing shown):
automatic seabed drape in Profile mode, drape-resolved display geometry,
per-segment friction mapping, spans reporting and the MBR check.

Requires a GUI-enabled Q(gs)Application (widgets cannot be created with
``QgsApplication([], False)``); the smoke runner provides one.
"""

from __future__ import annotations

import math
from typing import Callable, List

import numpy as np

from qgis.PyQt.QtWidgets import QApplication, QTableWidgetItem


def _result(name: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f" - {detail}" if detail else ""))
    return ok


def _make_dialog():
    from ..catenary.catenary_calculator_v2_dialog import CatenaryCalculatorV2Dialog

    dlg = CatenaryCalculatorV2Dialog()
    # Force a known configuration regardless of any persisted user settings.
    dlg.water_depth.setValue(120.0)
    dlg.chute_exit_height.setValue(0.0)
    dlg.chute_radius.setValue(0.0)
    dlg.ds_step.setValue(0.5)
    dlg.input_parameter.setCurrentIndex(0)  # Bottom Tension
    dlg.bottom_tension.setValue(50.0)
    dlg.assembly_table.setRowCount(0)
    dlg.show_full_assembly_seabed.setChecked(False)
    dlg.include_bending.setChecked(True)
    dlg.bending_stiffness.setValue(1.0)
    dlg.min_bend_radius.setValue(2.0)
    dlg.seabed_end_condition.setCurrentIndex(0)  # anchored
    dlg.on_bed_extra_len.setValue(200.0)
    dlg.seabed_mode.setCurrentIndex(0)  # Flat
    return dlg


_RIDGE_CSV = "0,120\n100,120\n250,40\n400,120\n700,150\n2000,300"


def _set_profile_ridge(dlg) -> None:
    dlg._load_seabed_csv_text(_RIDGE_CSV)
    dlg.seabed_mode.setCurrentIndex(2)  # Profile


def test_flat_mode_has_no_drape_and_reports_hang():
    dlg = _make_dialog()
    dlg.update_plot()
    assert dlg._last_calc is not None, "single-span solve failed"
    assert dlg._active_drape_result(dlg._last_calc) is None, "drape must not run on a flat bed"
    html = dlg.results.toHtml()
    assert "Hang (ship" in html, "flat-bed results must include the hang row"
    assert "Suspended spans" in html


def test_profile_mode_runs_drape_and_rests_on_bed():
    dlg = _make_dialog()
    _set_profile_ridge(dlg)
    dlg.update_plot()
    calc = dlg._last_calc
    assert calc is not None, "single-span solve failed"
    assert dlg._drape_error is None, f"auto-drape failed: {dlg._drape_error}"
    res = dlg._active_drape_result(calc)
    assert res is not None, "auto-drape did not produce a result in Profile mode"
    assert res.converged, f"drape residual {res.residual_ratio:.2e}"
    # The single-span solve goes through the 40 m crest; the drape must rest
    # on it instead (multiple suspended regions, no displayed penetration).
    assert len(res.spans) >= 2, f"expected multi-span over the ridge, got {len(res.spans)}"
    disp = dlg._display_geometry(calc)
    assert disp is not calc, "display geometry must come from the drape"
    clearance = np.asarray(disp.seabed_clearance_m, dtype=float)
    assert float(np.min(clearance)) >= -1e-6, (
        f"displayed cable penetrates the bed by {-float(np.min(clearance)):.4f} m"
    )
    html = dlg.results.toHtml()
    assert "Seabed drape" in html
    assert "Cable passes through the seabed" not in html, (
        "penetration banner must not show when the drape resolved the contact"
    )
    # Hover cache must expose the interactive-query channels.
    for key in ("curve_clearance_m", "curve_contact", "curve_radius_m"):
        assert dlg._hover_cache.get(key) is not None, f"hover cache missing {key}"


def test_assembly_friction_column_maps_to_mu_array():
    dlg = _make_dialog()
    _set_profile_ridge(dlg)
    # One long segment with explicit mechanical values.
    dlg.assembly_table.blockSignals(True)
    try:
        dlg.assembly_table.insertRow(0)
        for col, val in [
            (0, "Segment"),
            (1, "Main"),
            (2, "5000"),
            (3, "22"),
            (4, "28"),
            (5, ""),
            (6, "0.6"),
            (7, "12.5"),
            (8, "4.5"),
        ]:
            dlg.assembly_table.setItem(0, col, QTableWidgetItem(val))
    finally:
        dlg.assembly_table.blockSignals(False)
    dlg.update_plot()
    calc = dlg._last_calc
    assert calc is not None
    assembly = calc.cfg.get("assembly", [])
    assert assembly and assembly[0].friction_mu == 0.6, "friction column not parsed into AssemblyItem"
    assert assembly[0].bending_stiffness_kNm2 == 12.5, "EI column not parsed into AssemblyItem"
    assert assembly[0].min_bend_radius_m == 4.5, "MBR column not parsed into AssemblyItem"
    mu = dlg._drape_mu_array(calc, 100, 1000.0)
    assert float(np.min(mu)) == 0.6 and float(np.max(mu)) == 0.6, "mu array must use the segment value"
    ei = dlg._drape_EI_array(calc, 100, 1000.0)
    assert float(np.min(ei)) == 12500.0 and float(np.max(ei)) == 12500.0, "EI array must use the segment value"
    # Blank friction falls back to the default.
    dlg.assembly_table.blockSignals(True)
    try:
        item = dlg.assembly_table.item(0, 6)
        item.setText("")
    finally:
        dlg.assembly_table.blockSignals(False)
    dlg.update_plot()
    calc = dlg._last_calc
    mu = dlg._drape_mu_array(calc, 100, 1000.0)
    assert abs(float(np.max(mu)) - dlg._DEFAULT_SEABED_MU) < 1e-12

    # JSON round trip preserves the explicit value.
    dlg.assembly_table.blockSignals(True)
    try:
        dlg.assembly_table.item(0, 6).setText("0.45")
        dlg.assembly_table.item(0, 7).setText("8.5")
        dlg.assembly_table.item(0, 8).setText("3.25")
    finally:
        dlg.assembly_table.blockSignals(False)
    raw = dlg._assembly_table_to_json()
    assert '"friction_mu": 0.45' in raw
    assert '"bending_stiffness_kNm2": 8.5' in raw
    assert '"min_bend_radius_m": 3.25' in raw
    assert dlg._assembly_table_from_json(raw)
    assert dlg._table_get_optional_float(dlg.assembly_table, 0, 6) == 0.45
    assert dlg._table_get_optional_float(dlg.assembly_table, 0, 7) == 8.5
    assert dlg._table_get_optional_float(dlg.assembly_table, 0, 8) == 3.25


def test_wavy_profile_with_stale_water_depth_stays_consistent():
    """Regression for the field report: Water Depth spinbox left at 1000 m
    over a wavy ~150 m profile seeded the TDP search at x = 1011 m; the
    fixed point silently failed back to the seed ("TDP at 1011.00 m from
    chute" with a 253 m layback), the drape then solved against the flat
    profile tail, and the displayed cable cut straight through the
    undulations. The solve must now stay self-consistent and the drape must
    rest the displayed cable on the bed."""
    import math as _math

    dlg = _make_dialog()
    dlg.water_depth.setValue(1000.0)       # stale spinbox value
    dlg.chute_exit_height.setValue(10.0)
    dlg.chute_radius.setValue(3.0)
    dlg.bottom_tension.setValue(4.0)
    rows = "\n".join(
        f"{25.0 * i:g},{150.0 + 10.0 * _math.sin(2.0 * _math.pi * (25.0 * i) / 100.0):.3f}"
        for i in range(0, 53)
    )
    dlg._load_seabed_csv_text(rows)
    dlg.seabed_mode.setCurrentIndex(2)  # Profile
    dlg.update_plot()

    calc = dlg._last_calc
    assert calc is not None, "solve failed"
    residual = abs(float(calc.layback) - float(calc.tdp_x_world))
    assert residual <= 1.0, (
        f"TDP frame inconsistent: layback {calc.layback:.1f} m vs TDP x "
        f"{calc.tdp_x_world:.1f} m"
    )
    assert dlg._drape_error is None, f"auto-drape failed: {dlg._drape_error}"
    res = dlg._active_drape_result(calc)
    assert res is not None
    disp = dlg._display_geometry(calc)
    clearance = np.asarray(disp.seabed_clearance_m, dtype=float)
    assert float(np.min(clearance)) >= -1e-6, (
        f"displayed cable penetrates the bed by {-float(np.min(clearance)):.3f} m"
    )
    # The drape must have been solved against the wavy stretch the cable
    # actually occupies: its on-bed tail must follow the undulations (depth
    # variation well above flat-tail tolerance) — this is what silently broke
    # before the fix.
    contact_y = np.asarray(disp.y, dtype=float)[np.asarray(disp.drape_contact, dtype=bool)]
    if len(contact_y) > 5:
        assert float(np.ptp(contact_y)) > 2.0, "drape tail looks flat: solved in the wrong world frame?"


def test_mbr_violation_raises_banner():
    dlg = _make_dialog()
    dlg.min_bend_radius.setValue(5000.0)  # absurd limit: must trip on any solve
    dlg.update_plot()
    html = dlg.results.toHtml()
    assert "Minimum bend radius violated" in html
    dlg.min_bend_radius.setValue(2.0)
    dlg.update_plot()
    html = dlg.results.toHtml()
    assert "Minimum bend radius violated" not in html


def test_bending_toggle_controls_drape_EI_and_reporting():
    dlg = _make_dialog()
    dlg.include_bending.setChecked(False)
    dlg.update_plot()
    assert "perfectly flexible" in dlg.results.toHtml()
    dlg.include_bending.setChecked(True)
    dlg.update_plot()
    assert "Bending stiffness: EI" in dlg.results.toHtml()


def run_all() -> List[str]:
    if QApplication.instance() is None:
        print("[SKIP] dialog tests need a GUI-enabled QApplication")
        return []

    failures: List[str] = []
    tests: List[Callable[[], None]] = [
        test_flat_mode_has_no_drape_and_reports_hang,
        test_profile_mode_runs_drape_and_rests_on_bed,
        test_wavy_profile_with_stale_water_depth_stays_consistent,
        test_assembly_friction_column_maps_to_mu_array,
        test_mbr_violation_raises_banner,
        test_bending_toggle_controls_drape_EI_and_reporting,
    ]
    for test in tests:
        try:
            test()
            _result(test.__name__, True)
        except Exception as exc:  # pragma: no cover
            _result(test.__name__, False, repr(exc))
            failures.append(test.__name__)
    print(f"\n{len(failures)} failure(s)." if failures else "\nAll checks passed.")
    return failures


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit("Run via tests/run_qgis_smoke_tests.py (needs the plugin package registered).")
