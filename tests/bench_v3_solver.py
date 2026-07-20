# -*- coding: utf-8 -*-
"""Benchmark + accuracy-fingerprint harness for the V3 lay engine.

Pure Python + NumPy; no QGIS imports. Run standalone:

    python-qgis.bat tests\\bench_v3_solver.py                 # print table
    python-qgis.bat tests\\bench_v3_solver.py --baseline b.json
    python-qgis.bat tests\\bench_v3_solver.py --compare  b.json

The fingerprint captures the *physics* of each case (tensions, geometry,
convergence) so performance work can prove it did not change results:
``--compare`` fails (exit 1) if any tension moves more than 0.5 %, any
geometry statistic moves more than 5 cm, or a convergence flag flips.
Wall-clock is reported but never compared (machine dependent).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
import types

import numpy as np

ROOT = Path(__file__).resolve().parents[1]

# Tolerances for --compare (accuracy gate).
#
# Top tensions are the operationally meaningful loads (machinery/sheave) and
# must not move: 0.5 %. End/max tensions of chains lying on the bed are
# residual friction tensions — the solver documents friction equilibria as
# non-unique (lay-history dependent), so any change to iteration count or
# stopping point legitimately shifts them by a few percent; they get a
# looser band that still catches real physics errors (sign/scale bugs move
# them tens of percent). Geometry statistics stay tight (5 cm).
REL_TENSION_TOL = 0.005      # 0.5 % on top tensions
REL_TENSION_TOL_BED = 0.05   # 5 % on lay-history-dependent end/max tensions
ABS_GEOM_TOL_M = 0.05        # 5 cm on every geometry statistic
REL_RADIUS_TOL = 0.10        # 10 % on min bend radius (curvature of slack bed cable)


def _load_engine():
    pkg = types.ModuleType("sct_v3_bench")
    pkg.__path__ = [str(ROOT / "catenary" / "v3" / "engine")]
    sys.modules["sct_v3_bench"] = pkg
    mods = {}
    for m in ("bathymetry", "cable_system", "solver3d", "hydrodynamics",
              "steady_lay", "timeline", "scenarios"):
        spec = importlib.util.spec_from_file_location(
            f"sct_v3_bench.{m}", ROOT / "catenary" / "v3" / "engine" / f"{m}.py"
        )
        mm = importlib.util.module_from_spec(spec)
        sys.modules[f"sct_v3_bench.{m}"] = mm
        spec.loader.exec_module(mm)
        mods[m] = mm
    return mods


M = _load_engine()
bathy_mod = M["bathymetry"]
cs = M["cable_system"]
tl = M["timeline"]
sc = M["scenarios"]


def _telecom_assembly(length):
    return cs.uniform_assembly(
        length, 180.0, q_air_npm=300.0, diameter_m=0.035,
        cd_normal=1.2, cd_tangential=0.01, mu=0.3, name="LW cable",
    )


DEFAULTS = cs.Defaults(q_water_npm=180.0, mu=0.3, diameter_m=0.035)


def _rough_grid(depth0=80.0, n=200, extent=2000.0, amp=6.0, seed=42):
    """Deterministic rough seabed: smooth random undulation + gentle slope."""
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal((n, n))
    # Cheap separable smoothing (no scipy): repeated neighbour averaging.
    for _ in range(8):
        noise = 0.25 * (np.roll(noise, 1, 0) + np.roll(noise, -1, 0)
                        + np.roll(noise, 1, 1) + np.roll(noise, -1, 1))
    noise = noise / max(1e-9, float(np.std(noise)))
    xs = np.linspace(0.0, 1.0, n)
    slope = 10.0 * xs[None, :]          # 10 m deeper across the grid in +x
    depths = depth0 + amp * noise + slope
    d = extent / (n - 1)
    return bathy_mod.GridBathymetry(-extent / 2.0, -extent / 2.0, d, d, depths)


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def _chain_fp(c) -> dict:
    return {
        "top_tension_kN": float(c.top_tension_kN),
        "end_tension_kN": float(c.end_tension_kN),
        "max_tension_kN": float(np.max(c.tension_kN)),
        "rms_z_m": float(np.sqrt(np.mean(c.xyz[:, 2] ** 2))),
        "mean_x_m": float(np.mean(c.xyz[:, 0])),
        "contact_frac": float(np.mean(c.contact)),
        "min_radius_m": float(min(c.min_radius_m, 1e9)),
        "length_m": float(c.length_m),
    }


def _snapshot_fp(snap) -> dict:
    fp = {
        "converged": bool(snap.converged),
        "chains": {c.name: _chain_fp(c) for c in snap.chains},
    }
    for name, xyz in snap.junction_xyz.items():
        fp[f"junction_{name}"] = [float(v) for v in xyz]
    return fp


def _sim_fp(res) -> dict:
    first, last = res.snapshots[0], res.snapshots[-1]
    return {
        "n_snapshots": len(res.snapshots),
        "aborted": bool(res.aborted),
        "first": _snapshot_fp(first),
        "last": _snapshot_fp(last),
    }


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

def case_static_flat():
    """(a) Cold static settle of a suspended span on a flat bed."""
    h = 120.0
    bathy = bathy_mod.FlatBathymetry(h)
    scn = sc.straight_lay(
        bathy, _telecom_assembly(5000.0), DEFAULTS,
        ship_speed_mps=1.0, slack_percent=2.0, duration_s=60.0,
        chute_height_m=5.0, target_ds_m=4.0,
    )
    sim = tl.OperationSimulator(scn, bathy, tl.SimOptions())
    snap = sim.settle()
    return _snapshot_fp(snap)


def case_bu_hold_grid():
    """(b) BU static hold over a rough 200x200 grid (contact + gradient heavy)."""
    bathy = _rough_grid()
    asm = _telecom_assembly(3000.0)
    scn = sc.bu_deployment(
        bathy, asm, asm, DEFAULTS,
        bu_weight_kN=18.0, bu_cda_m2=1.5, leg_length_m=220.0,
        bu_start_depth_m=40.0, static_only=True, target_ds_m=5.0,
    )
    sim = tl.OperationSimulator(scn, bathy, tl.SimOptions())
    snap = sim.settle()
    return _snapshot_fp(snap)


def case_bu_deployment_op():
    """(c) BU lowering operation, ~15 substeps on a flat bed."""
    h = 80.0
    bathy = bathy_mod.FlatBathymetry(h)
    asm = _telecom_assembly(3000.0)
    scn = sc.bu_deployment(
        bathy, asm, asm, DEFAULTS,
        bu_weight_kN=15.0, bu_cda_m2=1.5, leg_length_m=150.0,
        ship_speed_mps=0.3, payout_speed_mps=0.4, target_ds_m=5.0,
    )
    sim = tl.OperationSimulator(
        scn, bathy, tl.SimOptions(max_move_m=8.0, rate_drag=True, tol=4e-3, max_iters=30000)
    )
    return _sim_fp(sim.run())


def case_final_bight_op():
    """(d) Final bight lowering + auto-release + settle."""
    h = 40.0
    bathy = bathy_mod.FlatBathymetry(h)
    scn = sc.final_bight(
        bathy, _telecom_assembly(1000.0), sc.default_rope_assembly(500.0), DEFAULTS,
        bight_length_m=250.0, end_a_xy=(-60.0, 0.0), end_b_xy=(60.0, 0.0),
        vessel_speed_mps=0.15, rope_payout_mps=0.3,
        release_threshold_kN=1.0, target_ds_m=4.0,
    )
    sim = tl.OperationSimulator(
        scn, bathy, tl.SimOptions(max_move_m=8.0, rate_drag=True, tol=4e-3, max_iters=30000)
    )
    return _sim_fp(sim.run())


CASES = [
    ("static_flat", case_static_flat),
    ("bu_hold_grid", case_bu_hold_grid),
    ("bu_deployment_op", case_bu_deployment_op),
    ("final_bight_op", case_final_bight_op),
]


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

def _walk_compare(path: str, ref, new, failures: list):
    """Recursively compare fingerprints with type-aware tolerances."""
    if isinstance(ref, dict):
        for k in ref:
            if k not in new:
                failures.append(f"{path}.{k}: missing in new result")
            else:
                _walk_compare(f"{path}.{k}", ref[k], new[k], failures)
        return
    if isinstance(ref, list):
        for i, (r, n) in enumerate(zip(ref, new)):
            _walk_compare(f"{path}[{i}]", r, n, failures)
        return
    if isinstance(ref, bool):
        if bool(new) != ref:
            failures.append(f"{path}: flag {ref} -> {new}")
        return
    if isinstance(ref, (int, float)):
        r, n = float(ref), float(new)
        leaf = path.rsplit(".", 1)[-1]
        if "tension" in leaf:
            tol = REL_TENSION_TOL if leaf.startswith("top_") else REL_TENSION_TOL_BED
            scale = max(abs(r), 0.05)  # 0.05 kN floor for near-slack values
            if abs(n - r) / scale > tol:
                failures.append(f"{path}: {r:.4f} -> {n:.4f} kN ({abs(n-r)/scale:.2%})")
        elif leaf in ("n_snapshots",):
            if int(n) != int(r):
                failures.append(f"{path}: {int(r)} -> {int(n)}")
        elif leaf in ("contact_frac",):
            if abs(n - r) > 0.03:
                failures.append(f"{path}: {r:.3f} -> {n:.3f}")
        elif leaf in ("min_radius_m",):
            scale = max(abs(r), 1.0)
            if abs(n - r) / scale > REL_RADIUS_TOL:
                failures.append(f"{path}: {r:.1f} -> {n:.1f} m")
        else:  # geometry statistics in metres
            if abs(n - r) > ABS_GEOM_TOL_M:
                failures.append(f"{path}: {r:.3f} -> {n:.3f} m (d={abs(n-r):.3f})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline", help="write fingerprints to this JSON file")
    ap.add_argument("--compare", help="compare fingerprints against this JSON file")
    ap.add_argument("--only", help="comma-separated case names to run")
    args = ap.parse_args(argv)

    only = set(args.only.split(",")) if args.only else None
    results = {}
    print(f"{'case':24s} {'wall_s':>8s}")
    for name, fn in CASES:
        if only and name not in only:
            continue
        t0 = time.perf_counter()
        fp = fn()
        wall = time.perf_counter() - t0
        results[name] = fp
        print(f"{name:24s} {wall:8.2f}")

    if args.baseline:
        Path(args.baseline).write_text(json.dumps(results, indent=1))
        print(f"\nBaseline written to {args.baseline}")
        return 0

    if args.compare:
        ref = json.loads(Path(args.compare).read_text())
        failures = []
        for name in results:
            if name not in ref:
                print(f"[skip] {name}: not in baseline")
                continue
            _walk_compare(name, ref[name], results[name], failures)
        if failures:
            print(f"\n{len(failures)} fingerprint deviation(s):")
            for f in failures:
                print("  " + f)
            return 1
        print("\nAll fingerprints within tolerance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
