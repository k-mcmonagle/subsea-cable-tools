# -*- coding: utf-8 -*-
"""Pure-helper tests for the V3 time-series tab.

Covers the snapshot flattening, the panel builder (which panels/traces exist
for a given run), the layout signature used to decide between an in-place
update and a rebuild, and the Y-axis fit used by the "Fit Y" action. No Qt
widgets are constructed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load_view():
    pkg = types.ModuleType("sct_v3_tsui")
    pkg.__path__ = [str(ROOT / "catenary" / "v3" / "ui")]
    sys.modules["sct_v3_tsui"] = pkg
    mod = None
    for name in ("views2d", "timeseries_view"):
        spec = importlib.util.spec_from_file_location(
            f"sct_v3_tsui.{name}", ROOT / "catenary" / "v3" / "ui" / f"{name}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"sct_v3_tsui.{name}"] = mod
        spec.loader.exec_module(mod)
    return mod


TS = _load_view()


class _Chain:
    def __init__(self, name, top, contact, tension):
        self.name = name
        self.top_tension_kN = float(top)
        self.contact = contact
        self.tension_kN = tension


class _Snap:
    """Minimal stand-in for ``timeline.Snapshot``."""

    def __init__(self, i, with_bu=True, laid=True):
        self.t_s = float(10 * i)
        contact = [False, False, True, True] if laid else [False] * 4
        self.chains = [
            _Chain("leg1", 40.0 + i, contact, [30.0 + i] * 4),
            _Chain("leg2", 41.0 + 0.5 * i, contact, [31.0 + i] * 4),
            _Chain("trunk", 60.0 + 2.0 * i, [False] * 4, [0.0] * 4),
        ]
        self.payout_mps = {"leg1": 0.1 * i, "leg2": 0.05 * i, "trunk": 0.2}
        self.junction_xyz = {"BU": (10.0 * i, 0.0, -20.0 * i)} if with_bu else {}
        self.vessel_xy = (0.0, 0.0)
        self.label = "overboard" if i == 2 else ""

    def chain(self, name):
        for c in self.chains:
            if c.name == name:
                return c
        return None


def test_snapshots_to_series_shapes_and_values():
    snaps = [_Snap(i) for i in range(5)]
    ser = TS.snapshots_to_series(snaps)
    assert list(ser["t"]) == [0.0, 10.0, 20.0, 30.0, 40.0]
    assert ser["names"] == ["leg1", "leg2", "trunk"]
    assert ser["top_tension"]["leg1"][2] == 42.0
    # TDP tension is read at the first bed-contact node.
    assert ser["tdp_tension"]["leg1"][2] == 32.0
    # The suspended trunk has no bed contact -> all NaN.
    assert np.all(np.isnan(ser["tdp_tension"]["trunk"]))
    assert ser["leg_imbalance"][2] == 42.0 - 42.0
    assert ser["bu_z"][3] == -60.0
    assert ser["layback_bu"][3] == 30.0
    assert ser["labels"][2] == "overboard"


def test_build_panels_drops_empty_panels_and_traces():
    # Fully suspended run, no BU junction: no TDP and no descent panel.
    snaps = [_Snap(i, with_bu=False, laid=False) for i in range(4)]
    panels = TS.build_panels(snaps and TS.snapshots_to_series(snaps))
    labels = [p["ylabel"] for p in panels]
    assert labels == ["Top tension (kN)", "Payout (m/s)"]

    full = TS.build_panels(TS.snapshots_to_series([_Snap(i) for i in range(4)]))
    assert [p["ylabel"] for p in full] == [
        "Top tension (kN)", "Bottom (TDP) tension (kN)",
        "Payout (m/s)", "BU z / layback (m)",
    ]
    top = full[0]
    assert [s["label"] for s in top["series"]] == ["leg1", "leg2", "trunk", "|leg1-leg2|"]
    # The suspended trunk is absent from the TDP panel.
    assert [s["label"] for s in full[1]["series"]] == ["leg1", "leg2"]
    # Chains keep one colour across panels.
    assert full[0]["series"][0]["color"] == full[1]["series"][0]["color"]


def test_build_panels_empty_input():
    assert TS.build_panels(None) == []
    assert TS.build_panels(TS.snapshots_to_series([])) == []


def test_panels_signature_tracks_layout_only():
    ser_a = TS.snapshots_to_series([_Snap(i) for i in range(4)])
    ser_b = TS.snapshots_to_series([_Snap(i) for i in range(6)])
    sig_a = TS.panels_signature(TS.build_panels(ser_a))
    # More samples, same traces -> same signature (update in place, keep zoom).
    assert sig_a == TS.panels_signature(TS.build_panels(ser_b))
    # A new panel appearing changes it (rebuild).
    ser_c = TS.snapshots_to_series([_Snap(i, with_bu=False) for i in range(4)])
    assert sig_a != TS.panels_signature(TS.build_panels(ser_c))


def test_fit_range_uses_visible_window_only():
    t = np.arange(0.0, 100.0, 10.0)
    y = t.copy()
    lo, hi = TS.fit_range(t, [y], 20.0, 50.0, pad=0.0)
    assert (lo, hi) == (20.0, 50.0)
    # Padding is a fraction of the visible span.
    lo, hi = TS.fit_range(t, [y], 20.0, 50.0, pad=0.1)
    assert lo == 17.0 and hi == 53.0
    # Reversed window is accepted.
    assert TS.fit_range(t, [y], 50.0, 20.0, pad=0.0) == (20.0, 50.0)


def test_fit_range_ignores_nan_and_handles_flat_or_empty():
    t = np.arange(5.0)
    y = np.array([np.nan, 2.0, 3.0, np.nan, 1.0])
    assert TS.fit_range(t, [y], 0.0, 4.0, pad=0.0) == (1.0, 3.0)
    # Nothing finite in the window -> no limits (leave the axis alone).
    assert TS.fit_range(t, [np.full(5, np.nan)], 0.0, 4.0) is None
    # Window outside the data.
    assert TS.fit_range(t, [y], 50.0, 60.0) is None
    # Flat trace gets an absolute margin instead of a zero-height axis.
    lo, hi = TS.fit_range(t, [np.full(5, 7.0)], 0.0, 4.0)
    assert lo < 7.0 < hi
    # Mismatched arrays are skipped, not fatal.
    assert TS.fit_range(t, [np.zeros(3), y], 0.0, 4.0, pad=0.0) == (1.0, 3.0)


def test_format_value():
    assert TS.format_value(1.23456, "kN") == "1.235 kN"
    assert TS.format_value(1234.5678, "kN") == "1,234.57 kN"
    assert TS.format_value(float("nan")) == "-"
    assert TS.format_value(None) == "-"


# ---------------------------------------------------------------------------

def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"[PASS] {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL] {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[ERROR] {t.__name__}: {type(exc).__name__}: {exc}")
    print()
    print("All checks passed." if failed == 0 else f"{failed} test(s) failed.")
    return failed


if __name__ == "__main__":
    sys.exit(run_all())
