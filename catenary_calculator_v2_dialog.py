# -*- coding: utf-8 -*-
"""
Catenary Calculator Dialog for Subsea Cable Tools QGIS Plugin

Upgraded version:
- Models a full suspended system from TDP (touchdown at seabed) up to the chute exit:
  * Submerged + in-air cable (different weights in each medium)
  * Optional components (bodies like repeaters / joints) as:
      - short heavy sections (delta distributed weight over a length)
      - or point loads (vertical lump load, causes a kink)
  * Optional quarter-circle "chute" geometry (rendered) with radius and exit height above waterline
- No extra dependencies beyond what ships with QGIS (PyQt5, matplotlib, numpy).

Coordinate convention (internal):
- Sea level: y = 0
- Above sea: y > 0
- Below sea: y < 0
- Seabed at y = -water_depth
- TDP starts at (x=0, y=-water_depth)
- Chute exit ends at (x=layback, y=+chute_exit_height)

Plot convention:
- Depth = -y (so seabed is +depth, above sea is negative depth)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple
from typing import TYPE_CHECKING

from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QTextEdit, QWidget, QFormLayout, QSizePolicy, QFileDialog, QDoubleSpinBox,
    QTabWidget, QTableWidget, QTableWidgetItem, QMessageBox, QHeaderView
    , QCheckBox, QColorDialog
)
from qgis.PyQt.QtCore import Qt, QSettings, QTimer
from qgis.PyQt.QtGui import QColor
from matplotlib.figure import Figure
from matplotlib.collections import LineCollection

if TYPE_CHECKING:  # keep Pylance happy
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
else:
    try:
        # QGIS typically ships Qt5Agg
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    except Exception:  # pragma: no cover
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None
import math
import json


# ---------------------------
# Models / parsing helpers
# ---------------------------


@dataclass
class AssemblyItem:
    """Ordered from chute top down along the cable."""

    kind: str  # 'segment' or 'body'
    name: str
    length_m: float  # segment length (body typically 0)
    q_water_npm: float  # absolute weight (N/m) for segments
    q_air_npm: float
    point_load_kN: float  # bodies: +ve = weight down, -ve = buoyancy up
    color_hex: str = ""  # optional per-segment colour (e.g. "#RRGGBB")

@dataclass
class Component:
    """
    A component that modifies the cable system over a section, or at a point.

    Interpretation:
    - s_from_tdp_m: distance along the suspended cable measured from the TDP upward (m).
      (TDP = 0m, chute exit = total_suspended_length)
    - length_m:
        * if > 0: apply distributed delta weights over [s, s+length]
        * if = 0: it's a point event (usually point_load_kN)
    - delta_q_water_npm / delta_q_air_npm:
        * additional distributed weight N/m applied in that section, on top of the base cable weight.
    - point_load_kN:
        * applied once when passing the point (adds to vertical component V), causes a slope kink.
          This is useful for “lumped” weights, but note MBR at a kink is not physically meaningful.
          Prefer short sections if you want curvature to remain realistic.
    """
    name: str
    position_m: float
    length_m: float
    delta_q_water_npm: float
    delta_q_air_npm: float
    point_load_kN: float
    reference: str = "tdp"  # legacy only

    @property
    def is_point(self) -> bool:
        return abs(self.length_m) < 1e-9 and abs(self.point_load_kN) > 1e-12


def _parse_components(text: str) -> List[Component]:
    """
    Parse multiline component definitions.
    Format per line (comma or tab separated):
        name, s_from_tdp_m, length_m, delta_q_water(N/m), delta_q_air(N/m), point_load_kN

    Examples:
        Repeater, 80, 4, 200, 200, 0
        JointHousing, 120, 0, 0, 0, 15

    Notes:
    - Blank lines and lines starting with # are ignored.
    - Missing optional fields default to 0.
    """
    comps: List[Component] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.replace("\t", ",").split(",")]
        # Pad to 6 fields
        while len(parts) < 6:
            parts.append("0")
        name = parts[0] if parts[0] else "Component"
        try:
            s = float(parts[1])
            L = float(parts[2])
            dq_w = float(parts[3])
            dq_a = float(parts[4])
            p_kN = float(parts[5])
        except Exception:
            # Skip malformed lines
            continue

        comps.append(Component(
            name=name,
            position_m=s,
            length_m=L,
            delta_q_water_npm=dq_w,
            delta_q_air_npm=dq_a,
            point_load_kN=p_kN,
            reference="tdp",
        ))
    # Sort by position (in its native reference)
    comps.sort(key=lambda c: c.position_m)
    return comps


# ---------------------------
# Calculator (physics + solver)
# ---------------------------

class CatenarySystemCalculator:
    """
    Numerical integration of a suspended cable with:
    - constant horizontal tension component H (N)
    - vertical component V changes with distributed weight (q) and point loads
    - medium (water/air) chosen by current y sign (y<0 => water)
    - optional components modifying q, and point loads

    We integrate along arc length s from TDP upward:
      start: s=0, x=0, y=-D, V=0
      end:   s=S, x=layback, y=+c
    """

    def __init__(self, config: dict):
        self.cfg = config

        # outputs (set after solve)
        self.H_N: Optional[float] = None
        self.S_total: Optional[float] = None
        self.layback: Optional[float] = None
        self.exit_angle_deg_from_h: Optional[float] = None
        self.top_tension_kN: Optional[float] = None
        self.bottom_tension_kN: Optional[float] = None
        self.min_radius_m: Optional[float] = None

        # sampled shape
        self.x: Optional[np.ndarray] = None
        self.y: Optional[np.ndarray] = None
        self.s: Optional[np.ndarray] = None

        # key points
        self.s_sea_surface: Optional[float] = None
        self.chute_contact_len_m: float = float(self.cfg.get("chute_contact_len_m", 0.0))
        self.free_span_len_m: Optional[float] = None

    # --- Utility conversions

    @staticmethod
    def _unit_to_npm(value: float, unit: str) -> float:
        """
        Convert input weight to N/m:
        - N/m => N/m
        - kg/m => kg/m * g
        - lbf/ft => convert to N/m
        """
        if unit == "N/m":
            return value
        if unit == "kg/m":
            return value * 9.80665
        if unit == "lbf/ft":
            # 1 lbf = 4.448221615 N, 1 ft = 0.3048 m
            return value * 4.448221615 / 0.3048
        raise ValueError("Unknown unit")

    # --- Core: integrate for a given (H, S)

    def _q_effective(self, y: float, s_from_tdp: float, S_free: float, L_chute_contact: float, assembly: List[AssemblyItem], comps: List[Component]) -> float:
        """
        Effective distributed weight q (N/m) at a given position along the cable.
        Base q depends on medium (water vs air) and components add delta q if within their length range.
        """
        # If an Assembly is defined, it provides absolute q by segment.
        # Otherwise fall back to global q + legacy component deltas.
        if assembly:
            # Map current point on free span to distance from chute top.
            # d_from_top = L_chute_contact + (S_free - s_from_tdp)
            d_from_top = L_chute_contact + (S_free - s_from_tdp)
            q_seg = None
            d_cursor = 0.0
            for it in assembly:
                if it.kind != "segment":
                    continue
                d0 = d_cursor
                d1 = d_cursor + max(0.0, it.length_m)
                if d0 <= d_from_top <= d1:
                    q_seg = it.q_water_npm if y < 0 else it.q_air_npm
                    break
                d_cursor = d1

            if q_seg is None or q_seg <= 0:
                q_seg = self.cfg["q_water_npm"] if y < 0 else self.cfg["q_air_npm"]
            return max(float(q_seg), 1e-9)

        base_q = self.cfg["q_water_npm"] if y < 0 else self.cfg["q_air_npm"]
        q = base_q
        for c in comps:
            if c.length_m <= 0:
                continue
            s0 = c.position_m
            s1 = c.position_m + c.length_m
            if s0 <= s_from_tdp <= s1:
                q += c.delta_q_water_npm if y < 0 else c.delta_q_air_npm
        return max(q, 1e-9)

    def _apply_point_loads(self, s_prev: float, s_new: float, V: float, S_free: float, L_chute_contact: float, assembly: List[AssemblyItem], comps: List[Component]) -> float:
        """
        Apply point loads when the integration passes them.
        (Adds to V, in Newtons)
        """
        if assembly:
            # bodies positioned by cumulative segment length from chute top
            d_cursor = 0.0
            for it in assembly:
                if it.kind == "segment":
                    d_cursor += max(0.0, it.length_m)
                    continue
                if it.kind != "body":
                    continue

                d_body = d_cursor
                if d_body < L_chute_contact:
                    # body on the chute-contact portion (not part of free span)
                    continue
                if d_body > (L_chute_contact + S_free):
                    continue

                s_body = S_free - (d_body - L_chute_contact)
                if s_prev < s_body <= s_new:
                    V += it.point_load_kN * 1000.0
            return V

        for c in comps:
            if not c.is_point:
                continue
            sp = c.position_m
            if s_prev < sp <= s_new:
                V += c.point_load_kN * 1000.0
        return V

    def integrate(self, H_N: float, S_free_m: float, ds: float) -> Tuple[float, float, float, float, float, np.ndarray, np.ndarray, np.ndarray]:
        """
        Integrate from TDP to the top over total arc length S_m.
        Returns:
            x_end, y_end, V_end, theta_end_rad, top_tension_N, s_arr, x_arr, y_arr
        """
        D = self.cfg["water_depth_m"]
        comps = self.cfg.get("components", [])
        assembly = self.cfg.get("assembly", [])

        # If we are using Assembly mapping, the chute contact length matters to convert
        # between s (from TDP) and d (from chute top). But the contact length itself depends
        # on the exit angle (theta_end). Do a tiny fixed-point iteration so the mapping is
        # self-consistent.
        R = float(self.cfg.get("chute_radius_m", 0.0))

        def integrate_once(L_chute_contact: float):
            # Start at touchdown (TDP)
            s = 0.0
            x = 0.0
            y = -D
            V = 0.0

            s_list = [0.0]
            x_list = [x]
            y_list = [y]
            sea_cross_s = None

            n_steps = max(1, int(math.ceil(S_free_m / ds)))
            max_steps = int(self.cfg.get("max_integration_steps", 25000))
            if n_steps > max_steps:
                raise ValueError(
                    f"Integration would require {n_steps} steps (S_free={S_free_m:.2f} m, ds={ds:.3f} m), "
                    f"which is too slow for interactive use. Increase ds or use a smaller length/height. "
                    f"(Max allowed steps: {max_steps})"
                )
            ds_eff = S_free_m / n_steps

            # Build a unified list of split events along free-span (s from TDP upward).
            # We split at:
            #  - point loads (apply instantaneous delta-V)
            #  - assembly segment boundaries (q changes)
            #  - legacy component boundaries (q changes)
            # Sea-level changes are handled separately inside integrate_with_sea_split.
            split_events: List[Tuple[float, float]] = []  # (s_event, point_load_N_at_event)

            # 1) Point loads
            if assembly:
                d_cursor = 0.0
                for it in assembly:
                    if it.kind == "segment":
                        d_cursor += max(0.0, it.length_m)
                        continue
                    if it.kind != "body":
                        continue
                    if abs(it.point_load_kN) < 1e-12:
                        continue

                    d_body = d_cursor
                    if d_body < L_chute_contact:
                        continue
                    if d_body > (L_chute_contact + S_free_m):
                        continue

                    s_body = S_free_m - (d_body - L_chute_contact)
                    if 0.0 < s_body < S_free_m:
                        split_events.append((float(s_body), float(it.point_load_kN) * 1000.0))
            else:
                for c in comps:
                    if not c.is_point:
                        continue
                    sp = float(c.position_m)
                    if 0.0 < sp < S_free_m:
                        split_events.append((sp, float(c.point_load_kN) * 1000.0))

            # 2) Assembly segment boundaries (q changes)
            if assembly:
                d_cursor = 0.0
                for it in assembly:
                    if it.kind != "segment":
                        continue
                    d_cursor += max(0.0, it.length_m)
                    # boundary at this cumulative distance from chute top
                    d_b = d_cursor
                    if d_b <= L_chute_contact:
                        continue
                    if d_b >= (L_chute_contact + S_free_m):
                        continue
                    s_b = S_free_m - (d_b - L_chute_contact)
                    if 0.0 < s_b < S_free_m:
                        split_events.append((float(s_b), 0.0))

            # 3) Legacy component boundaries (q changes)
            if not assembly and comps:
                for c in comps:
                    if c.length_m <= 0:
                        continue
                    s0 = float(c.position_m)
                    s1 = float(c.position_m + c.length_m)
                    if 0.0 < s0 < S_free_m:
                        split_events.append((s0, 0.0))
                    if 0.0 < s1 < S_free_m:
                        split_events.append((s1, 0.0))

            # Merge events at same/similar s
            if split_events:
                split_events.sort(key=lambda t: t[0])
                merged: List[Tuple[float, float]] = []
                tol_s = max(1e-9, 0.25 * ds_eff)
                cur_s, cur_load = split_events[0]
                for se, ld in split_events[1:]:
                    if abs(se - cur_s) <= tol_s:
                        cur_load += ld
                    else:
                        merged.append((cur_s, cur_load))
                        cur_s, cur_load = se, ld
                merged.append((cur_s, cur_load))
                split_events = merged

            ev_idx = 0

            for _ in range(n_steps):
                s_prev = s

                def do_substep(ds_local: float, y_for_medium: float, s_local_end: float):
                    nonlocal V, x, y
                    q = self._q_effective(y_for_medium, s_local_end, S_free_m, L_chute_contact, assembly, comps)
                    if ds_local <= 0:
                        return

                    # Midpoint (RK2) in arc-length: improves accuracy vs explicit Euler.
                    V_mid = V + 0.5 * q * ds_local
                    T_mid = math.sqrt(H_N * H_N + V_mid * V_mid)
                    if T_mid <= 0:
                        raise ValueError("Non-physical tension encountered during integration.")
                    x += (H_N / T_mid) * ds_local
                    y += (V_mid / T_mid) * ds_local
                    V = V + q * ds_local

                def integrate_with_sea_split(s_target: float):
                    """Integrate from current s to s_target, splitting once at y=0 if crossed."""
                    nonlocal s, x, y, V, sea_cross_s
                    ds_local = s_target - s
                    if ds_local <= 0:
                        s = s_target
                        return

                    y_before = y
                    V_before = V
                    x_before = x

                    do_substep(ds_local, y_before, s_target)
                    y_after = y

                    if sea_cross_s is None and ((y_before < 0 <= y_after) or (y_before > 0 >= y_after)):
                        # rollback and split
                        y = y_before
                        V = V_before
                        x = x_before

                        if abs(y_after - y_before) < 1e-12:
                            frac = 0.5
                        else:
                            frac = (0.0 - y_before) / (y_after - y_before)
                            frac = float(max(0.0, min(1.0, frac)))

                        ds1 = ds_local * frac
                        ds2 = ds_local - ds1
                        s_mid = s + ds1

                        if ds1 > 0:
                            do_substep(ds1, y_before, s_mid)
                        sea_cross_s = s_mid

                        y_eps = 1e-9 if y_before < 0 else -1e-9
                        if ds2 > 0:
                            do_substep(ds2, y_eps, s_target)

                    s = s_target

                s_full = s + ds_eff

                # Advance through any point-load events in (s, s_full], applying them exactly at event s.
                while ev_idx < len(split_events) and split_events[ev_idx][0] <= s + 1e-12:
                    ev_idx += 1

                while ev_idx < len(split_events) and (s < split_events[ev_idx][0] <= s_full):
                    s_event, load_N = split_events[ev_idx]
                    integrate_with_sea_split(float(s_event))
                    # Apply point-load (if any) exactly at this location.
                    if abs(load_N) > 1e-12:
                        V += float(load_N)
                    ev_idx += 1

                integrate_with_sea_split(s_full)

                s_list.append(s)
                x_list.append(x)
                y_list.append(y)

            theta = math.atan2(V, H_N)
            top_T = math.sqrt(H_N * H_N + V * V)

            return (
                x,
                y,
                V,
                theta,
                top_T,
                np.array(s_list),
                np.array(x_list),
                np.array(y_list),
                sea_cross_s,
            )

        L_guess = float(self.cfg.get("chute_contact_len_m", 0.0))
        result = integrate_once(L_guess)

        if assembly and R > 0:
            for _ in range(6):
                x_end, y_end, V_end, theta_end, top_T, s_arr, x_arr, y_arr, sea_cross_s = result
                L_new = float(R * max(0.0, min(math.pi / 2.0, theta_end)))
                if abs(L_new - L_guess) < 1e-3:
                    L_guess = L_new
                    break
                L_guess = 0.6 * L_guess + 0.4 * L_new
                result = integrate_once(L_guess)

        x_end, y_end, V_end, theta_end, top_T, s_arr, x_arr, y_arr, sea_cross_s = result

        self.s_sea_surface = sea_cross_s
        self.chute_contact_len_m = float(L_guess)
        self.free_span_len_m = float(S_free_m)

        return x_end, y_end, V_end, theta_end, top_T, s_arr, x_arr, y_arr

        # Note: a previous inline integrator implementation used to live here.
        # It was duplicated and unreachable (after return). It has been removed for clarity.

    # --- Root-finding helpers

    @staticmethod
    def _bracket_root(func, x0: float, step: float, max_expand: int = 60) -> Tuple[float, float]:
        """
        Expand a bracket around x0 until func(a) and func(b) have opposite signs.
        """
        a = max(1e-12, x0 - step)
        b = x0 + step

        last_eval_err: Optional[Exception] = None

        def safe_eval(x: float) -> float:
            nonlocal last_eval_err
            try:
                last_eval_err = None
                return float(func(x))
            except Exception as e:
                last_eval_err = e
                return float("nan")

        fa = safe_eval(a)
        fb = safe_eval(b)

        for _ in range(max_expand):
            if fa == 0:
                return a, a
            if fb == 0:
                return b, b
            if (not math.isnan(fa)) and (not math.isnan(fb)) and fa * fb < 0:
                return a, b
            # Expand
            step *= 1.6
            a = max(1e-12, x0 - step)
            b = x0 + step
            fa = safe_eval(a)
            fb = safe_eval(b)

        if last_eval_err is not None:
            raise ValueError(
                "Could not bracket a root because the function failed to evaluate in the search range. "
                f"Last evaluation error: {last_eval_err}"
            )
        raise ValueError(
            "Could not bracket a root for the chosen input values. "
            f"Tried x in roughly [{a:.3g}, {b:.3g}] around x0={x0:.3g}; f(a)={fa:.3g}, f(b)={fb:.3g}."
        )

    @staticmethod
    def _bisect(func, a: float, b: float, tol: float = 1e-6, max_iter: int = 120) -> float:
        fa = func(a)
        fb = func(b)
        if abs(fa) < tol:
            return a
        if abs(fb) < tol:
            return b
        if fa * fb > 0:
            raise ValueError("Root not bracketed.")

        lo, hi = a, b
        flo, fhi = fa, fb
        for _ in range(max_iter):
            mid = 0.5 * (lo + hi)
            fm = func(mid)
            if abs(fm) < tol or abs(hi - lo) < tol:
                return mid
            if flo * fm < 0:
                hi, fhi = mid, fm
            else:
                lo, flo = mid, fm
        return 0.5 * (lo + hi)

    # --- Solve modes

    def solve(self):
        """
        Main entry point.
        Sets outputs + shape arrays.
        """
        D = self.cfg["water_depth_m"]
        c_top = self.cfg["chute_exit_height_m"]
        ds = self.cfg["ds_m"]
        mode = self.cfg["input_mode"]

        R = float(self.cfg.get("chute_radius_m", 0.0))

        # Chute-contact coupling: free-span leaves the chute where its tangent matches chute tangent.
        # Use theta_end (from horizontal, 0..pi/2) to compute:
        # contact length along chute: L = R * theta
        # departure point height: y_dep = c_top - R + R*cos(theta)
        def chute_contact_from_theta(theta_rad: float) -> Tuple[float, float]:
            if R <= 0:
                return 0.0, c_top
            theta_rad = float(max(0.0, min(math.pi / 2.0, theta_rad)))
            Lc = R * theta_rad
            y_dep = c_top - R + R * math.cos(theta_rad)
            return Lc, y_dep

        # Inner solver: given H, find free-span S that hits the chute-dependent departure height.
        def solve_S_free_for_H(H):
            # Minimum possible free-span length is straight vertical to the lowest possible departure.
            y_dep_min = c_top - R if R > 0 else c_top
            S_min = max(1e-3, D + max(y_dep_min, 0.0) + 1e-6)
            S_guess = max(self.cfg.get("S_guess_m", S_min * 1.5), S_min * 1.2)

            def fS(S_free: float) -> float:
                x_end, y_end, _, theta_end, _, _, _, _ = self.integrate(H, S_free, ds)
                Lc, y_dep = chute_contact_from_theta(theta_end)
                self.cfg["chute_contact_len_m"] = Lc
                return y_end - y_dep

            try:
                a, b = self._bracket_root(fS, x0=S_guess, step=max(5.0, 0.2 * S_guess))
                return self._bisect(fS, a, b, tol=1e-4)
            except Exception as e:
                raise ValueError(
                    "Failed to find a free-span length S_free that reaches the chute departure height. "
                    f"This usually means the configuration is infeasible or the solver could not bracket a solution. "
                    f"Try increasing ds (coarser integration), reducing extreme point loads, or checking weights/units. "
                    f"(H={H/1000.0:.3f} kN, initial S_guess={S_guess:.3f} m)\n\nDetails: {e}"
                )

        # Mode 1: Catenary Length (total suspended length S is input). Solve H for y_target.
        if mode == "Catenary Length":
            S_total_in = self.cfg["S_input_m"]

            def fH_total_len(H):
                S_free = solve_S_free_for_H(H)
                x_end, y_end, _, theta_end, _, _, _, _ = self.integrate(H, S_free, ds)
                Lc, _ = chute_contact_from_theta(theta_end)
                return (S_free + Lc) - S_total_in

            H0 = max(1.0, self.cfg.get("H_guess_N", 50_000.0))
            try:
                a, b = self._bracket_root(fH_total_len, x0=H0, step=max(5_000.0, 0.2 * H0))
                H = self._bisect(fH_total_len, a, b, tol=1e-3)
            except Exception as e:
                raise ValueError(
                    "Failed to solve for Bottom Tension (H) that matches the requested total suspended length. "
                    "This can happen if the required length is incompatible with the geometry/weights or if bracketing fails. "
                    f"(Requested S={S_total_in:.3f} m, initial H_guess={H0/1000.0:.3f} kN)\n\nDetails: {e}"
                )
            S_free = solve_S_free_for_H(H)
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S_free, ds)
            Lc, _ = chute_contact_from_theta(theta_end)
            S = S_free + Lc

        # Mode 2: Bottom Tension (horizontal component H is input). Solve S for y_target.
        elif mode == "Bottom Tension":
            H = self.cfg["H_input_N"]
            if H <= 0:
                raise ValueError("Bottom tension must be > 0.")
            S_free = solve_S_free_for_H(H)
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S_free, ds)
            Lc, _ = chute_contact_from_theta(theta_end)
            S = S_free + Lc

        # Mode 3: Layback is input: solve H such that (with S chosen to hit y_target) x_end matches.
        elif mode == "Layback":
            layback_top_target = self.cfg["layback_input_m"]
            if layback_top_target <= 0:
                raise ValueError("Layback must be > 0.")

            def fH_layback_top(H):
                S_free = solve_S_free_for_H(H)
                x_end, y_end, _, theta_end, _, _, _, _ = self.integrate(H, S_free, ds)
                if R > 0:
                    layback_top = x_end + R * math.sin(theta_end)
                else:
                    layback_top = x_end
                return layback_top - layback_top_target

            H0 = max(1.0, self.cfg.get("H_guess_N", 50_000.0))
            try:
                a, b = self._bracket_root(fH_layback_top, x0=H0, step=max(5_000.0, 0.25 * H0))
                H = self._bisect(fH_layback_top, a, b, tol=1e-3)
            except Exception as e:
                raise ValueError(
                    "Failed to solve for Bottom Tension (H) that matches the requested layback. "
                    "This can happen if the layback target is not achievable for the given geometry/weights, "
                    "or if bracketing fails due to non-monotonic behavior (strong buoyancy/point loads). "
                    f"(Requested layback={layback_top_target:.3f} m, initial H_guess={H0/1000.0:.3f} kN)\n\nDetails: {e}"
                )
            S_free = solve_S_free_for_H(H)
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S_free, ds)
            Lc, _ = chute_contact_from_theta(theta_end)
            S = S_free + Lc

        # Mode 4: Exit Angle at chute exit is input (from horizontal). Solve H such that angle matches.
        elif mode == "Exit Angle":
            theta_target_rad = math.radians(self.cfg["exit_angle_from_h_deg"])
            if not (0 < theta_target_rad < math.radians(89.9)):
                raise ValueError("Exit angle must be between 0 and 90 degrees (exclusive).")

            Lc_target, y_dep_target = chute_contact_from_theta(theta_target_rad)

            def fH_exit_angle(H):
                # Solve free span to the fixed y_dep_target then match angle
                def fS(S_free):
                    _, y_end, _, _, _, _, _, _ = self.integrate(H, S_free, ds)
                    return y_end - y_dep_target

                S_min = max(1e-3, D + max(y_dep_target, 0.0) + 1e-6)
                S_guess = max(self.cfg.get("S_guess_m", S_min * 1.5), S_min * 1.2)
                aS, bS = self._bracket_root(fS, x0=S_guess, step=max(5.0, 0.2 * S_guess))
                S_free = self._bisect(fS, aS, bS, tol=1e-4)
                _, _, V_end, theta_end, _, _, _, _ = self.integrate(H, S_free, ds)
                return theta_end - theta_target_rad

            H0 = max(1.0, self.cfg.get("H_guess_N", 50_000.0))
            try:
                a, b = self._bracket_root(fH_exit_angle, x0=H0, step=max(5_000.0, 0.25 * H0))
                H = self._bisect(fH_exit_angle, a, b, tol=1e-6)
            except Exception as e:
                raise ValueError(
                    "Failed to solve for H that matches the requested exit angle. "
                    "This usually means the target angle is not achievable for the given geometry/weights, "
                    "or that strong point loads/buoyancy make the relationship non-monotonic. "
                    f"(Requested exit angle={math.degrees(theta_target_rad):.3f}° from horizontal, initial H_guess={H0/1000.0:.3f} kN)\n\nDetails: {e}"
                )
            # final solve for free span
            def fS_final(S_free):
                _, y_end, _, _, _, _, _, _ = self.integrate(H, S_free, ds)
                return y_end - y_dep_target
            S_min = max(1e-3, D + max(y_dep_target, 0.0) + 1e-6)
            S_guess = max(self.cfg.get("S_guess_m", S_min * 1.5), S_min * 1.2)
            aS, bS = self._bracket_root(fS_final, x0=S_guess, step=max(5.0, 0.2 * S_guess))
            S_free = self._bisect(fS_final, aS, bS, tol=1e-4)
            self.cfg["chute_contact_len_m"] = Lc_target
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S_free, ds)
            S = S_free + Lc_target

        # Mode 5: Top Tension at chute exit is input. Solve H such that top tension matches.
        elif mode == "Top Tension":
            T_top_target_N = self.cfg["Ttop_input_N"]
            if T_top_target_N <= 0:
                raise ValueError("Top tension must be > 0.")

            def fH_top_tension(H):
                S_free = solve_S_free_for_H(H)
                _, _, _, _, T_top, _, _, _ = self.integrate(H, S_free, ds)
                return T_top - T_top_target_N

            H0 = max(1.0, min(T_top_target_N * 0.9, self.cfg.get("H_guess_N", 50_000.0)))
            try:
                a, b = self._bracket_root(fH_top_tension, x0=H0, step=max(5_000.0, 0.25 * H0))
                H = self._bisect(fH_top_tension, a, b, tol=5.0)
            except Exception as e:
                raise ValueError(
                    "Failed to solve for H that matches the requested top tension. "
                    "This can happen if the requested top tension is outside the achievable range for the given geometry/weights. "
                    f"(Requested top tension={T_top_target_N/1000.0:.3f} kN, initial H_guess={H0/1000.0:.3f} kN)\n\nDetails: {e}"
                )
            S_free = solve_S_free_for_H(H)
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S_free, ds)
            Lc, _ = chute_contact_from_theta(theta_end)
            S = S_free + Lc

        else:
            raise ValueError("Invalid solve mode.")

        # Final integration already performed in each branch where needed.
        # For any branch that didn't set arrays, compute now.
        if "s_arr" not in locals():
            # Backward compatibility: no chute coupling
            x_end, y_end, V_end, theta_end, T_top, s_arr, x_arr, y_arr = self.integrate(H, S, ds)

        # Validate final y: handled implicitly by the root-finding conditions.

        # Save outputs
        self.H_N = H
        self.S_total = S
        if R > 0:
            self.layback = x_end + R * math.sin(theta_end)  # layback to chute top
        else:
            self.layback = x_end
        self.exit_angle_deg_from_h = math.degrees(theta_end)
        self.top_tension_kN = T_top / 1000.0
        self.bottom_tension_kN = H / 1000.0

        self.s = s_arr
        self.x = x_arr
        self.y = y_arr

        self.min_radius_m = self._compute_min_radius()

    def _compute_min_radius(self) -> float:
        """
        Compute minimum radius of curvature.
        - Uses numerical curvature on the catenary polyline (excluding the chute arc).
        - Includes chute radius as a candidate minimum.
        - For point loads (kinks), curvature spikes are not physically meaningful; we clip extreme spikes.
        """
        if self.x is None or self.y is None:
            return float("inf")

        # Numerical curvature kappa = |x'y'' - y'x''| / (x'^2 + y'^2)^(3/2)
        x = self.x
        y = self.y

        dx = np.gradient(x)
        dy = np.gradient(y)
        ddx = np.gradient(dx)
        ddy = np.gradient(dy)

        denom = np.power(dx * dx + dy * dy, 1.5)
        denom = np.where(denom < 1e-12, 1e-12, denom)

        kappa = np.abs(dx * ddy - dy * ddx) / denom

        # Clip unreal spikes (point-load kinks etc.)
        # Keep "real" curvature but don't let a single kink dominate.
        kappa_clip = np.clip(kappa, 0, np.percentile(kappa, 99.5))

        max_kappa = float(np.max(kappa_clip))
        if max_kappa <= 1e-12:
            R_cat = float("inf")
        else:
            R_cat = 1.0 / max_kappa

        R_chute = self.cfg["chute_radius_m"]
        if R_chute > 0:
            return float(min(R_cat, R_chute))
        return float(R_cat)


# ---------------------------
# Dialog / UI
# ---------------------------

class CatenaryCalculatorV2Dialog(QDialog):
    ASM_COL_TYPE = 0
    ASM_COL_NAME = 1
    ASM_COL_LENGTH = 2
    ASM_COL_Q_WATER = 3
    ASM_COL_Q_AIR = 4
    ASM_COL_BODY_LOAD = 5
    ASM_COL_COLOR = 6

    _DEFAULT_SEGMENT_COLORS = [
        "#1f77b4",  # tab:blue
        "#ff7f0e",  # tab:orange
        "#2ca02c",  # tab:green
        "#d62728",  # tab:red
        "#9467bd",  # tab:purple
        "#8c564b",  # tab:brown
        "#e377c2",  # tab:pink
        "#7f7f7f",  # tab:gray
        "#bcbd22",  # tab:olive
        "#17becf",  # tab:cyan
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        if np is None:
            QMessageBox.critical(
                self,
                'Missing dependency',
                'NumPy is required for the catenary calculator but could not be imported. '
                'Please install/enable NumPy for your QGIS Python environment.'
            )
            self.setEnabled(False)
            return
        self.setWindowTitle("Subsea Cable Catenary Calculator (Upgraded)")
        self.resize(1500, 920)
        self.setMinimumWidth(1250)
        self.setMinimumHeight(820)

        self.settings = QSettings("subsea_cable_tools", "CatenaryCalculatorUpgraded")

        self._prev_angle_ref = 0
        self._last_calc: Optional[CatenarySystemCalculator] = None

        # Debounce heavy recalculations while editing.
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self.update_plot)

        self.init_ui()
        self.restore_user_settings()
        self.update_input_fields()
        self.update_plot()

    def schedule_update_plot(self):
        """Debounced wrapper around `update_plot` for high-frequency UI signals."""
        # 150ms feels responsive but avoids dozens of solves while typing.
        self._update_timer.start(150)

    def closeEvent(self, a0):
        self.save_user_settings()
        super().closeEvent(a0)

    # ---- Persistent settings

    def save_user_settings(self):
        self.settings.setValue("water_depth", self.water_depth.value())
        self.settings.setValue("chute_exit_height", self.chute_exit_height.value())
        self.settings.setValue("chute_radius", self.chute_radius.value())
        self.settings.setValue("ds_step", self.ds_step.value())

        self.settings.setValue("weight_water", self.weight_water.value())
        self.settings.setValue("weight_air", self.weight_air.value())
        self.settings.setValue("weight_unit", self.weight_unit.currentIndex())

        self.settings.setValue("input_parameter", self.input_parameter.currentIndex())
        self.settings.setValue("bottom_tension", self.bottom_tension.value())
        self.settings.setValue("top_tension", self.top_tension.value())
        self.settings.setValue("exit_angle", self.exit_angle.value())
        self.settings.setValue("angle_reference", self.angle_reference.currentIndex())
        self.settings.setValue("catenary_length", self.catenary_length.value())
        self.settings.setValue("layback", self.layback.value())

        self.settings.setValue("components_text", self.components_text.toPlainText())
        self.settings.setValue("assembly_input_tab", self.assembly_tabs.currentIndex())
        self.settings.setValue("assembly_table_json", self._assembly_table_to_json())
        self.settings.setValue("show_full_assembly_seabed", bool(self.show_full_assembly_seabed.isChecked()))
        self.settings.setValue("show_legend", bool(self.show_legend.isChecked()))
        try:
            col_widths = [int(self.assembly_table.columnWidth(i)) for i in range(self.assembly_table.columnCount())]
            self.settings.setValue("assembly_table_col_widths", json.dumps(col_widths))
        except Exception:
            pass

    def restore_user_settings(self):
        def _get_float(key, default=None):
            val = self.settings.value(key)
            if val is None:
                return default
            try:
                return float(val)
            except Exception:
                return default

        def _get_int(key, default=None):
            val = self.settings.value(key)
            if val is None:
                return default
            try:
                return int(val)
            except Exception:
                return default

        if (v := _get_float("water_depth")) is not None:
            self.water_depth.setValue(v)
        if (v := _get_float("chute_exit_height")) is not None:
            self.chute_exit_height.setValue(v)
        if (v := _get_float("chute_radius")) is not None:
            self.chute_radius.setValue(v)
        if (v := _get_float("ds_step")) is not None:
            self.ds_step.setValue(v)

        if (v := _get_float("weight_water")) is not None:
            self.weight_water.setValue(v)
        if (v := _get_float("weight_air")) is not None:
            self.weight_air.setValue(v)
        if (v := _get_int("weight_unit")) is not None:
            self.weight_unit.setCurrentIndex(v)

        if (v := _get_int("input_parameter")) is not None:
            self.input_parameter.setCurrentIndex(v)
        if (v := _get_float("bottom_tension")) is not None:
            self.bottom_tension.setValue(v)
        if (v := _get_float("top_tension")) is not None:
            self.top_tension.setValue(v)
        if (v := _get_float("exit_angle")) is not None:
            self.exit_angle.setValue(v)
        if (v := _get_int("angle_reference")) is not None:
            self.angle_reference.setCurrentIndex(v)
        if (v := _get_float("catenary_length")) is not None:
            self.catenary_length.setValue(v)
        if (v := _get_float("layback")) is not None:
            self.layback.setValue(v)

        txt = self.settings.value("components_text")
        if txt is not None:
            self.components_text.setPlainText(str(txt))

        tab_idx = self.settings.value("assembly_input_tab")
        if tab_idx is not None:
            try:
                self.assembly_tabs.setCurrentIndex(int(tab_idx))
            except Exception:
                pass

        table_json = self.settings.value("assembly_table_json")
        if table_json is not None:
            self._assembly_table_from_json(str(table_json))

        v = self.settings.value("show_full_assembly_seabed")
        if v is not None:
            try:
                self.show_full_assembly_seabed.setChecked(str(v).lower() in ("1", "true", "yes"))
            except Exception:
                pass

        v = self.settings.value("show_legend")
        if v is not None:
            try:
                self.show_legend.setChecked(str(v).lower() in ("1", "true", "yes"))
            except Exception:
                pass

        col_widths_json = self.settings.value("assembly_table_col_widths")
        if col_widths_json is not None:
            try:
                col_widths = json.loads(str(col_widths_json))
                if isinstance(col_widths, list):
                    for i, w in enumerate(col_widths[: self.assembly_table.columnCount()]):
                        try:
                            self.assembly_table.setColumnWidth(i, int(w))
                        except Exception:
                            pass
            except Exception:
                pass

    # ---- UI init

    def init_ui(self):
        main_layout = QHBoxLayout(self)

        # Left: Inputs
        input_widget = QWidget()
        input_layout = QFormLayout(input_widget)
        input_widget.setMinimumWidth(360)

        # Geometry
        self.water_depth = QDoubleSpinBox()
        self.water_depth.setRange(0, 1e6)
        self.water_depth.setDecimals(1)
        self.water_depth.setValue(100.0)

        self.chute_exit_height = QDoubleSpinBox()
        self.chute_exit_height.setRange(0, 1e5)
        self.chute_exit_height.setDecimals(2)
        self.chute_exit_height.setValue(0.0)  # height above sea level

        self.chute_radius = QDoubleSpinBox()
        self.chute_radius.setRange(0, 1e4)
        self.chute_radius.setDecimals(2)
        self.chute_radius.setValue(0.0)
        self.chute_radius.setToolTip(
            "Chute radius used for geometry AND for optional chute-contact coupling. "
            "Set to 0 to ignore chute contact (free-span goes to the chute top point)."
        )

        self.ds_step = QDoubleSpinBox()
        self.ds_step.setRange(0.05, 10.0)
        self.ds_step.setDecimals(2)
        self.ds_step.setSingleStep(0.1)
        self.ds_step.setValue(0.5)

        # Weights
        self.weight_water = QDoubleSpinBox()
        self.weight_water.setRange(0, 1e6)
        self.weight_water.setDecimals(4)
        self.weight_water.setValue(22.0)

        self.weight_air = QDoubleSpinBox()
        self.weight_air.setRange(0, 1e6)
        self.weight_air.setDecimals(4)
        self.weight_air.setValue(28.0)

        self.weight_unit = QComboBox()
        self.weight_unit.addItems(["N/m", "kg/m", "lbf/ft"])

        # Solve mode
        self.input_parameter = QComboBox()
        self.input_parameter.addItems([
            "Bottom Tension",      # H is input (kN)
            "Top Tension",         # top T is input (kN)
            "Exit Angle",          # at chute exit
            "Catenary Length",     # total suspended length (m) from TDP to chute exit
            "Layback"              # horizontal distance from TDP to chute exit (m)
        ])

        # Inputs depending on mode
        self.bottom_tension = QDoubleSpinBox()
        self.bottom_tension.setRange(0, 1e6)
        self.bottom_tension.setDecimals(3)
        self.bottom_tension.setValue(50.0)

        self.top_tension = QDoubleSpinBox()
        self.top_tension.setRange(0, 1e6)
        self.top_tension.setDecimals(3)
        self.top_tension.setValue(80.0)

        self.exit_angle = QDoubleSpinBox()
        self.exit_angle.setRange(0.01, 89.99)
        self.exit_angle.setDecimals(3)
        self.exit_angle.setValue(25.0)

        self.angle_reference = QComboBox()
        self.angle_reference.addItems(["from horizontal", "from vertical"])

        self.catenary_length = QDoubleSpinBox()
        self.catenary_length.setRange(0, 1e7)
        self.catenary_length.setDecimals(3)
        self.catenary_length.setValue(230.0)

        self.layback = QDoubleSpinBox()
        self.layback.setRange(0, 1e7)
        self.layback.setDecimals(3)
        self.layback.setValue(150.0)

        # Assembly (ordered from chute top down)
        self.assembly_tabs = QTabWidget()

        self.assembly_table = QTableWidget(0, 7)
        self.assembly_table.setHorizontalHeaderLabels([
            "Type",
            "Name",
            "Length (m)",
            "q water (N/m)",
            "q air (N/m)",
            "Body load (kN)",
            "Colour",
        ])
        self.assembly_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.assembly_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.assembly_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.SelectedClicked | QAbstractItemView.EditKeyPressed)
        header = self.assembly_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(QHeaderView.Interactive)
            header.setStretchLastSection(True)
        self.assembly_table.setMinimumHeight(190)

        a_btn_row = QHBoxLayout()
        self.asm_add_seg_btn = QPushButton("Add Segment")
        self.asm_add_body_btn = QPushButton("Add Body")
        self.asm_del_btn = QPushButton("Delete")
        self.asm_up_btn = QPushButton("Move Up")
        self.asm_down_btn = QPushButton("Move Down")
        a_btn_row.addWidget(self.asm_add_seg_btn)
        a_btn_row.addWidget(self.asm_add_body_btn)
        a_btn_row.addWidget(self.asm_del_btn)
        a_btn_row.addWidget(self.asm_up_btn)
        a_btn_row.addWidget(self.asm_down_btn)

        asm_tab = QWidget()
        asm_tab_layout = QVBoxLayout(asm_tab)
        asm_tab_layout.addWidget(QLabel("Assembly is ordered from the chute top down along the cable."))
        asm_tab_layout.addWidget(self.assembly_table)
        asm_tab_layout.addLayout(a_btn_row)
        self.assembly_tabs.addTab(asm_tab, "Assembly")

        # Legacy text editor (kept for power users / backwards compatibility)
        self.components_text = QTextEdit()
        self.components_text.setMinimumHeight(140)
        self.components_text.setPlaceholderText(
            "# Components (optional):\n"
            "# name, s_from_TDP_m, length_m, delta_q_water(N/m), delta_q_air(N/m), point_load_kN\n"
            "# Examples:\n"
            "# Repeater, 80, 4, 200, 200, 0\n"
            "# JointHousing, 120, 0, 0, 0, 15\n"
        )
        text_tab = QWidget()
        text_tab_layout = QVBoxLayout(text_tab)
        text_tab_layout.addWidget(self.components_text)
        self.assembly_tabs.addTab(text_tab, "Legacy Text")

        # Layout entries
        input_layout.addRow(QLabel("<b>Geometry</b>"))
        input_layout.addRow("Water Depth D (m):", self.water_depth)
        input_layout.addRow("Chute Exit Height c above WL (m):", self.chute_exit_height)
        input_layout.addRow("Chute Radius R (m):", self.chute_radius)
        input_layout.addRow("Integration step ds (m):", self.ds_step)

        input_layout.addRow(QLabel("<b>Cable Weight</b>"))
        w_layout = QHBoxLayout()
        w_layout.addWidget(self.weight_water)
        w_layout.addWidget(QLabel("in water"))
        input_layout.addRow("Weight:", w_layout)

        a_layout = QHBoxLayout()
        a_layout.addWidget(self.weight_air)
        a_layout.addWidget(QLabel("in air"))
        a_layout.addWidget(self.weight_unit)
        input_layout.addRow("", a_layout)

        input_layout.addRow(QLabel("<b>Solve Mode</b>"))
        input_layout.addRow("Select Input Parameter:", self.input_parameter)
        input_layout.addRow("Bottom Tension H (kN):", self.bottom_tension)
        input_layout.addRow("Top Tension (kN):", self.top_tension)

        ang_layout = QHBoxLayout()
        ang_layout.addWidget(self.exit_angle)
        ang_layout.addWidget(self.angle_reference)
        input_layout.addRow("Exit Angle at Chute:", ang_layout)

        input_layout.addRow("Total Suspended Length S (m):", self.catenary_length)
        input_layout.addRow("Layback to Chute (m):", self.layback)

        input_layout.addRow(QLabel("<b>Cable Assembly</b>"))
        input_layout.addRow(self.assembly_tabs)

        self.show_full_assembly_seabed = QCheckBox("Show full assembly on seabed")
        self.show_full_assembly_seabed.setToolTip(
            "Extends the plot x-axis and draws any remaining assembly length beyond the suspended span as a straight line on the seabed."
        )
        input_layout.addRow("", self.show_full_assembly_seabed)

        note = QLabel(
            "<i>"
            "Notes:<br>"
            "• The system is solved as a suspended cable from TDP (seabed touchdown) to the chute exit point.<br>"
            "• Submerged vs in-air is determined automatically when the curve crosses sea level (y=0).<br>"
            "• Assembly is defined from the chute top down (Segment rows set q; Body rows add lumped load/buoyancy).<br>"
            "• Point loads create a mathematical kink (angle discontinuity). Prefer short sections if you care about curvature/MBR.<br>"
            "• Chute contact is modeled by enforcing the free-span tangent to match the chute arc tangent; contact length depends on exit angle."
            "</i>"
        )
        note.setWordWrap(True)
        input_layout.addRow(note)

        main_layout.addWidget(input_widget)

        # Right: Outputs + plot
        output_widget = QWidget()
        output_layout = QVBoxLayout(output_widget)

        output_layout.addWidget(QLabel("<b>Results</b>"))
        self.results = QTextEdit()
        self.results.setReadOnly(True)
        self.results.setMinimumHeight(240)
        output_layout.addWidget(self.results)

        self.figure = Figure(figsize=(6, 5))
        self.canvas = FigureCanvas(self.figure)
        self.canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        output_layout.addWidget(self.canvas, stretch=1)

        btns = QHBoxLayout()
        self.export_svg_btn = QPushButton("Export SVG")
        self.export_dxf_btn = QPushButton("Export DXF")
        btns.addWidget(self.export_svg_btn)
        btns.addWidget(self.export_dxf_btn)
        output_layout.addLayout(btns)

        self.show_legend = QCheckBox("Show legend")
        self.show_legend.setChecked(False)
        self.show_legend.setToolTip("Show/hide the plot legend. Keeping it hidden avoids covering the plot.")
        output_layout.addWidget(self.show_legend)

        main_layout.addWidget(output_widget, stretch=1)

        # signals
        for w in [
            self.water_depth, self.chute_exit_height, self.chute_radius, self.ds_step,
            self.weight_water, self.weight_air,
            self.bottom_tension, self.top_tension, self.exit_angle,
            self.catenary_length, self.layback
        ]:
            w.valueChanged.connect(self.schedule_update_plot)

        self.weight_unit.currentIndexChanged.connect(self.schedule_update_plot)
        self.input_parameter.currentIndexChanged.connect(self.update_input_fields)
        self.input_parameter.currentIndexChanged.connect(self.schedule_update_plot)
        self.angle_reference.currentIndexChanged.connect(self.on_angle_reference_changed)
        self.components_text.textChanged.connect(self.schedule_update_plot)
        self.assembly_tabs.currentChanged.connect(self.schedule_update_plot)
        self.assembly_table.cellChanged.connect(self._on_assembly_table_cell_changed)
        self.assembly_table.cellDoubleClicked.connect(self._on_assembly_table_cell_double_clicked)
        self.asm_add_seg_btn.clicked.connect(self._on_asm_add_segment)
        self.asm_add_body_btn.clicked.connect(self._on_asm_add_body)
        self.asm_del_btn.clicked.connect(self._on_asm_delete)
        self.asm_up_btn.clicked.connect(self._on_asm_move_up)
        self.asm_down_btn.clicked.connect(self._on_asm_move_down)

        self.show_full_assembly_seabed.toggled.connect(self.schedule_update_plot)

        self.show_legend.toggled.connect(self.schedule_update_plot)

        self.export_svg_btn.clicked.connect(self.export_svg)
        self.export_dxf_btn.clicked.connect(self.export_dxf)

    def showEvent(self, a0):
        self._prev_angle_ref = self.angle_reference.currentIndex()
        super().showEvent(a0)

    # ---- Angle reference sync

    def on_angle_reference_changed(self):
        self._sync_exit_angle_with_reference()
        self.update_plot()

    def _sync_exit_angle_with_reference(self):
        curr_ref = self.angle_reference.currentIndex()
        prev_ref = getattr(self, "_prev_angle_ref", curr_ref)
        if prev_ref != curr_ref:
            val = self.exit_angle.value()
            self.exit_angle.blockSignals(True)
            self.exit_angle.setValue(90.0 - val)
            self.exit_angle.blockSignals(False)
        self._prev_angle_ref = curr_ref

    # ---- Enable/disable inputs by mode

    def update_input_fields(self):
        mode = self.input_parameter.currentText()

        for w in [self.bottom_tension, self.top_tension, self.exit_angle, self.catenary_length, self.layback]:
            w.setDisabled(True)
        self.angle_reference.setDisabled(False)

        if mode == "Bottom Tension":
            self.bottom_tension.setDisabled(False)
        elif mode == "Top Tension":
            self.top_tension.setDisabled(False)
        elif mode == "Exit Angle":
            self.exit_angle.setDisabled(False)
        elif mode == "Catenary Length":
            self.catenary_length.setDisabled(False)
        elif mode == "Layback":
            self.layback.setDisabled(False)

        self._sync_exit_angle_with_reference()

    # ---- Build config for calculator

    def get_config(self) -> Optional[dict]:
        try:
            D = float(self.water_depth.value())
            if D <= 0:
                raise ValueError("Water depth must be > 0.")

            c = float(self.chute_exit_height.value())
            if c < 0:
                raise ValueError("Chute exit height must be >= 0.")

            R = float(self.chute_radius.value())
            if R < 0:
                raise ValueError("Chute radius must be >= 0.")

            ds = float(self.ds_step.value())
            if ds <= 0:
                raise ValueError("Integration step must be > 0.")

            unit = self.weight_unit.currentText()
            q_w = CatenarySystemCalculator._unit_to_npm(float(self.weight_water.value()), unit)
            q_a = CatenarySystemCalculator._unit_to_npm(float(self.weight_air.value()), unit)

            if q_w <= 0 or q_a <= 0:
                raise ValueError("Cable weights must be > 0.")

            mode = self.input_parameter.currentText()

            # Angle handling: always store "from horizontal"
            if self.angle_reference.currentText() == "from horizontal":
                exit_angle_from_h = float(self.exit_angle.value())
            else:
                exit_angle_from_h = 90.0 - float(self.exit_angle.value())

            assembly: List[AssemblyItem] = []
            comps: List[Component] = []
            if self.assembly_tabs.currentIndex() == 0:
                assembly = self._assembly_from_table()
            else:
                # legacy: still supported but treated as delta-q modifiers
                comps = _parse_components(self.components_text.toPlainText())

            cfg = {
                "water_depth_m": D,
                "chute_exit_height_m": c,
                "chute_radius_m": R,
                "ds_m": ds,
                "max_integration_steps": 25000,

                "q_water_npm": q_w,
                "q_air_npm": q_a,

                "assembly": assembly,
                "components": comps,
                "input_mode": mode,

                # guesses
                "H_guess_N": max(1.0, self.bottom_tension.value() * 1000.0),
                "S_guess_m": max(D + c + 1.0, self.catenary_length.value())
            }

            if mode == "Bottom Tension":
                cfg["H_input_N"] = float(self.bottom_tension.value()) * 1000.0
            elif mode == "Top Tension":
                cfg["Ttop_input_N"] = float(self.top_tension.value()) * 1000.0
            elif mode == "Exit Angle":
                cfg["exit_angle_from_h_deg"] = exit_angle_from_h
            elif mode == "Catenary Length":
                S_in = float(self.catenary_length.value())
                # Hard lower bound: with positive weights the shortest suspended length is essentially
                # a vertical hang from TDP to the lowest possible departure height, plus any chute contact.
                # For the chute quarter-circle: at theta=pi/2 => Lc=R*pi/2 and y_dep=c-R.
                # If c<R, the lowest departure is below sea level; vertical distance is then just D.
                if R > 0:
                    S_min = D + max(c - R, 0.0) + (math.pi / 2.0) * R
                else:
                    S_min = D + c
                if S_in < S_min - 1e-6:
                    raise ValueError(
                        f"Total suspended length S={S_in:.3f} m is too short for the geometry. "
                        f"Minimum feasible S is about {S_min:.3f} m (given D={D:.3f} m, c={c:.3f} m, R={R:.3f} m)."
                    )
                cfg["S_input_m"] = S_in
            elif mode == "Layback":
                cfg["layback_input_m"] = float(self.layback.value())
            else:
                raise ValueError("Invalid solve mode.")

            return cfg

        except Exception as e:
            self.results.setHtml(f'<span style="color:red;">{e}</span>')
            return None

    # ---- Main update

    def update_plot(self):
        cfg = self.get_config()
        if not cfg:
            self.figure.clear()
            self.canvas.draw()
            return

        try:
            calc = CatenarySystemCalculator(cfg)
            calc.solve()
            self._last_calc = calc

            # Update displayed "calculated" fields (soft sync)
            self._sync_calculated_fields(calc)

            # Results
            self._display_results(calc)

            # Plot
            self._plot(calc)

        except Exception as e:
            # Keep errors readable in the results pane.
            msg = str(e)
            msg = msg.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            msg = msg.replace("\n", "<br>")
            self.results.setHtml(f'<span style="color:red;"><b>Error</b><br>{msg}</span>')
            self.figure.clear()
            self.canvas.draw()

    def _sync_calculated_fields(self, calc: CatenarySystemCalculator):
        # Don't fight the user's chosen input; only overwrite the others
        mode = self.input_parameter.currentText()

        if mode != "Bottom Tension" and calc.bottom_tension_kN is not None:
            self.bottom_tension.blockSignals(True)
            self.bottom_tension.setValue(calc.bottom_tension_kN)
            self.bottom_tension.blockSignals(False)

        if mode != "Top Tension" and calc.top_tension_kN is not None:
            self.top_tension.blockSignals(True)
            self.top_tension.setValue(calc.top_tension_kN)
            self.top_tension.blockSignals(False)

        if mode != "Catenary Length" and calc.S_total is not None:
            self.catenary_length.blockSignals(True)
            self.catenary_length.setValue(calc.S_total)
            self.catenary_length.blockSignals(False)

        if mode != "Layback" and calc.layback is not None:
            self.layback.blockSignals(True)
            self.layback.setValue(calc.layback)
            self.layback.blockSignals(False)

        # Exit angle always updated to computed, respecting current reference display
        if calc.exit_angle_deg_from_h is not None:
            self.exit_angle.blockSignals(True)
            if self.angle_reference.currentText() == "from horizontal":
                self.exit_angle.setValue(calc.exit_angle_deg_from_h)
            else:
                self.exit_angle.setValue(90.0 - calc.exit_angle_deg_from_h)
            self.exit_angle.blockSignals(False)

    def _display_results(self, calc: CatenarySystemCalculator):
        D = self.water_depth.value()
        c = self.chute_exit_height.value()
        flop_forward = (calc.S_total - calc.layback) if (calc.S_total is not None and calc.layback is not None) else None
        flop_forward_txt = f"{flop_forward:.3f} m" if flop_forward is not None else "n/a"
        angle_from_vertical = 90.0 - (calc.exit_angle_deg_from_h or 0.0)

        assembly: List[AssemblyItem] = calc.cfg.get("assembly", [])
        asm_seg_total = sum(max(0.0, it.length_m) for it in assembly if it.kind == "segment") if assembly else 0.0
        warn_lines: List[str] = []
        if assembly and calc.S_total is not None:
            # S_total includes chute-contact + free-span. Assembly segments are defined from chute top down.
            # If assembly is shorter than S_total, remaining length uses the global cable weights.
            if calc.S_total > asm_seg_total + 1e-6:
                warn_lines.append(
                    f"Assembly segments total ({asm_seg_total:.2f} m) is shorter than suspended length ({calc.S_total:.2f} m). "
                    "Remaining length uses the global cable weight values."
                )
            # If assembly is much longer than S_total, some defined items are not in the suspended span.
            if asm_seg_total > calc.S_total + 1e-6:
                warn_lines.append(
                    f"Assembly segments total ({asm_seg_total:.2f} m) exceeds suspended length ({calc.S_total:.2f} m). "
                    "Lower assembly items may be on seabed (not in the suspended span) and bodies there will not affect the catenary."
                )

        sea_s = calc.s_sea_surface
        sea_txt = f"{sea_s:.2f} m" if sea_s is not None else "n/a"

        asm_txt = "n/a"
        if assembly:
            asm_txt = f"{asm_seg_total:.2f} m (segments only)"

        warn_txt = ""
        if warn_lines:
            warn_txt = "<br><br><b>Warnings</b><br>" + "<br>".join(f"• {w}" for w in warn_lines)

        txt = (
            f"Water Depth D: {D:.2f} m<br>"
            f"Chute Exit Height c: {c:.2f} m above WL<br><br>"
            f"Bottom Tension (H): {calc.bottom_tension_kN:.3f} kN<br>"
            f"Top Tension: {calc.top_tension_kN:.3f} kN<br>"
            f"Exit Angle: {calc.exit_angle_deg_from_h:.3f}° from horizontal / {angle_from_vertical:.3f}° from vertical<br>"
            f"Total Suspended Length (TDP→chute): {calc.S_total:.3f} m<br>"
            f"Layback (TDP→chute): {calc.layback:.3f} m<br>"
            f"Flop Forward (S - layback): {flop_forward_txt}<br>"
            f"Sea surface crossing at s ≈ {sea_txt}<br>"
            f"Minimum Radius of Curvature (incl chute): {calc.min_radius_m:.3f} m<br>"
            f"Assembly length: {asm_txt}<br>"
            f"{warn_txt}"
        )
        self.results.setHtml(txt)

    def _plot(self, calc: CatenarySystemCalculator):
        self.figure.clear()
        ax = self.figure.add_subplot(111)

        if calc.x is None or calc.y is None:
            self.canvas.draw()
            return

        x = calc.x
        y = calc.y

        # Convert to depth for plotting (depth positive down)
        depth = -y

        D = self.water_depth.value()
        c = self.chute_exit_height.value()
        R = self.chute_radius.value()
        layback = float(calc.layback) if calc.layback is not None else float(x[-1])

        # Cable (color by assembly segment if available)
        assembly: List[AssemblyItem] = calc.cfg.get("assembly", [])
        if assembly and calc.s is not None:
            s = calc.s
            S_free = float(s[-1])
            Lc = float(getattr(calc, "chute_contact_len_m", 0.0))

            # Determine segment index for each midpoint between points
            seg_items = [it for it in assembly if it.kind == "segment"]
            seg_lengths = [max(0.0, it.length_m) for it in seg_items]
            seg_colors = [self._normalize_color_hex(getattr(it, "color_hex", "")) for it in seg_items]
            seg_starts: List[float] = []
            cursor = 0.0
            for L in seg_lengths:
                seg_starts.append(cursor)
                cursor += L

            def segment_index_for_d(d_from_top: float) -> int:
                if not seg_lengths:
                    return 0
                cursor2 = 0.0
                for idx, L in enumerate(seg_lengths):
                    if cursor2 <= d_from_top <= (cursor2 + L):
                        return idx
                    cursor2 += L
                return max(0, len(seg_lengths) - 1)

            # Build colored line segments
            pts = np.column_stack([x, depth])
            segs = [pts[i : i + 2] for i in range(len(pts) - 1)]

            s_mid = 0.5 * (s[:-1] + s[1:])
            d_mid = Lc + (S_free - s_mid)
            idxs = [segment_index_for_d(float(d)) for d in d_mid]

            # Prefer user-selected colours from the assembly table (per segment).
            # If not provided, fall back to tab10.
            try:
                import matplotlib.pyplot as plt  # local import to avoid global pyplot dependency
                cmap = plt.get_cmap("tab10")
            except Exception:
                cmap = None

            colors: List[Any] = []
            for i in idxs:
                if 0 <= int(i) < len(seg_colors) and seg_colors[int(i)]:
                    colors.append(seg_colors[int(i)])
                elif cmap is not None:
                    colors.append(cmap(int(i) % 10))
                else:
                    colors.append("#1f77b4")

            lc = LineCollection(segs, colors=colors, linewidths=2)
            ax.add_collection(lc)
            # Add a legend handle
            ax.plot([], [], color=colors[0] if colors else "k", linewidth=2, label="Cable (by segment)")
        else:
            ax.plot(x, depth, label="Cable", linewidth=2)

        # Sea level and seabed
        ax.axhline(0, linewidth=2, label="Sea Level")
        ax.axhline(D, linewidth=2, label="Seabed")

        # Mark chute top point
        ax.scatter([layback], [-c], s=40, label="Chute Top")

        # Departure point is the last free-span point
        x_dep = float(x[-1])
        y_dep = float(y[-1])
        ax.scatter([x_dep], [-y_dep], s=30, label="Departure (free-span)")

        # Body markers (if any) positioned along free-span by s interpolation
        if assembly and calc.s is not None and calc.x is not None and calc.y is not None:
            s = calc.s
            S_free = float(s[-1])
            Lc = float(getattr(calc, "chute_contact_len_m", 0.0))

            # Bodies placed after cumulative segment length from top
            d_cursor = 0.0
            body_ds: List[float] = []
            for it in assembly:
                if it.kind == "segment":
                    d_cursor += max(0.0, it.length_m)
                elif it.kind == "body":
                    body_ds.append(d_cursor)

            body_points = []
            for d_body in body_ds:
                if d_body < Lc:
                    continue
                if d_body > (Lc + S_free):
                    continue
                s_body = S_free - (d_body - Lc)
                # Interpolate x,y at s_body
                xb = float(np.interp(s_body, s, calc.x))
                yb = float(np.interp(s_body, s, calc.y))
                body_points.append((xb, -yb))

            if body_points:
                bx, bd = zip(*body_points)
                ax.scatter(list(bx), list(bd), marker="D", s=36, color="black", label="Body")

        # Render chute as a full continuous-radius quarter circle, and optionally highlight the contact portion
        chute_x = None
        chute_y = None
        seabed_x = None
        seabed_y = None
        if R > 0:
            theta_end = math.radians(calc.exit_angle_deg_from_h or 0.0)
            theta_end = max(0.0, min(math.pi / 2.0, theta_end))

            x_top = layback
            y_top = c
            center_x = x_top
            center_y = y_top - R

            phi0 = math.pi / 2.0
            phi_full = math.pi
            phis_full = np.linspace(phi0, phi_full, 160)
            chute_x = center_x + R * np.cos(phis_full)
            chute_y = center_y + R * np.sin(phis_full)
            ax.plot(chute_x, -chute_y, linewidth=2, label="Chute (¼-circle)")

            # Highlight the portion assumed in contact (top -> departure tangent)
            phi1 = math.pi / 2.0 + theta_end
            phis_contact = np.linspace(phi0, phi1, 80)
            contact_x = center_x + R * np.cos(phis_contact)
            contact_y = center_y + R * np.sin(phis_contact)
            ax.plot(contact_x, -contact_y, linewidth=3, label="Chute contact")

        # Optional: draw full assembly laid out on seabed beyond TDP

        # Optional: draw full assembly laid out on seabed beyond TDP
        if self.show_full_assembly_seabed.isChecked() and assembly and calc.S_total is not None:
            asm_seg_total = sum(max(0.0, it.length_m) for it in assembly if it.kind == "segment")
            seabed_len = max(0.0, asm_seg_total - float(calc.S_total))
            if seabed_len > 1e-6:
                # Build a polyline from x=0 at TDP to negative x away from vessel
                n_pts = max(2, int(min(600, max(2, seabed_len / max(ds_step := float(self.ds_step.value()), 0.25)))))
                xs = np.linspace(0.0, -seabed_len, n_pts)
                ys = np.full_like(xs, D)
                seabed_x = xs
                seabed_y = ys

                # Color seabed line by segment, using d_from_top = S_total + distance_from_tdp_on_seabed
                seg_items2 = [it for it in assembly if it.kind == "segment"]
                seg_lengths = [max(0.0, it.length_m) for it in seg_items2]
                seg_colors2 = [self._normalize_color_hex(getattr(it, "color_hex", "")) for it in seg_items2]

                def segment_index_for_d(d_from_top: float) -> int:
                    if not seg_lengths:
                        return 0
                    cursor2 = 0.0
                    for idx, L in enumerate(seg_lengths):
                        if cursor2 <= d_from_top <= (cursor2 + L):
                            return idx
                        cursor2 += L
                    return max(0, len(seg_lengths) - 1)

                pts2 = np.column_stack([xs, ys])
                segs2 = [pts2[i : i + 2] for i in range(len(pts2) - 1)]
                x_mid = 0.5 * (xs[:-1] + xs[1:])
                d_mid = float(calc.S_total) + (-x_mid)
                idxs2 = [segment_index_for_d(float(d)) for d in d_mid]

                try:
                    import matplotlib.pyplot as plt
                    cmap2 = plt.get_cmap("tab10")
                except Exception:
                    cmap2 = None

                colors2: List[Any] = []
                for i in idxs2:
                    if 0 <= int(i) < len(seg_colors2) and seg_colors2[int(i)]:
                        colors2.append(seg_colors2[int(i)])
                    elif cmap2 is not None:
                        colors2.append(cmap2(int(i) % 10))
                    else:
                        colors2.append("#7f7f7f")

                lc2 = LineCollection(segs2, colors=colors2, linewidths=2, alpha=0.9)
                ax.add_collection(lc2)
                ax.plot([], [], color=colors2[0] if colors2 else "k", linewidth=2, label="Assembly on seabed")

                # Bodies that are on seabed
                d_cursor = 0.0
                seabed_body_points = []
                for it in assembly:
                    if it.kind == "segment":
                        d_cursor += max(0.0, it.length_m)
                        continue
                    if it.kind != "body":
                        continue
                    d_body = d_cursor
                    if d_body <= float(calc.S_total):
                        continue
                    x_body = -(d_body - float(calc.S_total))
                    if x_body < -seabed_len - 1e-6:
                        continue
                    seabed_body_points.append((x_body, D))

                if seabed_body_points:
                    bx, by = zip(*seabed_body_points)
                    ax.scatter(list(bx), list(by), marker="D", s=36, facecolors="none", edgecolors="black", label="Body (seabed)")

        ax.set_xlabel("Horizontal Distance (m)")
        ax.set_ylabel("Depth (m)")
        ax.set_title("Cable Catenary")
        # Bounds: compute from all drawn geometry so equal-aspect plots still show everything.
        x_candidates: List[float] = [float(np.min(x)), float(np.max(x)), 0.0, float(layback)]
        y_candidates: List[float] = [float(np.min(depth)), float(np.max(depth)), 0.0, float(D), float(-c)]

        if isinstance(chute_x, np.ndarray) and isinstance(chute_y, np.ndarray) and chute_x.size and chute_y.size:
            x_candidates.extend([float(np.min(chute_x)), float(np.max(chute_x))])
            y_candidates.extend([float(np.min(-chute_y)), float(np.max(-chute_y))])

        if isinstance(seabed_x, np.ndarray) and isinstance(seabed_y, np.ndarray) and seabed_x.size and seabed_y.size:
            x_candidates.extend([float(np.min(seabed_x)), float(np.max(seabed_x))])
            y_candidates.extend([float(np.min(seabed_y)), float(np.max(seabed_y))])

        x_min = float(min(x_candidates))
        x_max = float(max(x_candidates))
        y_min = float(min(y_candidates))
        y_max = float(max(y_candidates))

        # Add a small padding so objects don't sit on the frame.
        pad_x = max(1.0, 0.05 * (x_max - x_min))
        pad_y = max(1.0, 0.05 * (y_max - y_min))

        ax.set_xlim(x_min - pad_x, x_max + pad_x)
        ax.set_ylim(y_min - pad_y, y_max + pad_y)

        # Critical: enforce true proportions so the chute radius looks correct.
        # This will often "zoom out" visually when depth span dwarfs horizontal span.
        ax.set_aspect("equal", adjustable="box")
        ax.invert_yaxis()  # conventional: depth downwards
        ax.grid(True, alpha=0.25)

        if self.show_legend.isChecked():
            ax.legend(fontsize="small")

        self.figure.tight_layout()
        self.canvas.draw()

    # ---- Export

    def export_svg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save SVG", "catenary_plot.svg", "SVG Files (*.svg)")
        if path:
            self.figure.savefig(path, format="svg")

    def export_dxf(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save DXF", "catenary.dxf", "DXF Files (*.dxf)")
        if not path:
            return

        calc = self._last_calc
        if not calc or calc.x is None or calc.y is None:
            self.results.setHtml('<span style="color:red;">No catenary data to export.</span>')
            return

        # DXF expects planar coords; export in mm.
        assembly: List[AssemblyItem] = calc.cfg.get("assembly", [])
        D = float(self.water_depth.value())
        c = float(self.chute_exit_height.value())
        R = float(self.chute_radius.value())
        layback = float(calc.layback) if calc.layback is not None else float(calc.x[-1])

        # Scale-dependent defaults for label sizes/offsets (mm)
        x_span_mm = float((np.max(calc.x) - np.min(calc.x)) * 1000.0)
        y_span_mm = float((np.max(calc.y) - np.min(calc.y)) * 1000.0)
        span_mm = max(1.0, x_span_mm, y_span_mm, float(D * 1000.0))
        text_h = max(200.0, 0.015 * span_mm)
        text_off = 1.2 * text_h

        entities: List[str] = []

        def segment_items() -> List[AssemblyItem]:
            return [it for it in assembly if it.kind == "segment"]

        seg_items = segment_items()
        seg_lengths = [max(0.0, it.length_m) for it in seg_items]

        def segment_index_for_d(d_from_top_m: float) -> int:
            if not seg_lengths:
                return 0
            cursor = 0.0
            for idx, L in enumerate(seg_lengths):
                if cursor <= d_from_top_m <= (cursor + L):
                    return idx
                cursor += L
            return max(0, len(seg_lengths) - 1)

        def seg_layer(idx: int) -> str:
            if 0 <= idx < len(seg_items):
                base = f"SEG{idx+1:02d}_{seg_items[idx].name}"
            else:
                base = f"SEG{idx+1:02d}"
            return self._dxf_sanitize_layer(base)

        def seg_label(idx: int) -> str:
            if not (0 <= idx < len(seg_items)):
                return f"Segment {idx+1}"
            it = seg_items[idx]
            qw = float(it.q_water_npm)
            qa = float(it.q_air_npm)
            qw_txt = f"{qw:.3f} N/m" if qw > 0 else "(global)"
            qa_txt = f"{qa:.3f} N/m" if qa > 0 else "(global)"
            return f"{it.name} | qW={qw_txt} qA={qa_txt}"

        # 1) Export cable along the free-span, split by assembly segment where possible.
        if assembly and calc.s is not None and calc.x is not None and calc.y is not None:
            s = calc.s
            S_free = float(s[-1])
            Lc = float(getattr(calc, "chute_contact_len_m", 0.0))

            # Determine segment index for each line segment (between points) using midpoint mapping.
            if len(s) >= 2:
                s_mid = 0.5 * (s[:-1] + s[1:])
                d_mid = Lc + (S_free - s_mid)
                idxs = [segment_index_for_d(float(d)) for d in d_mid]

                # Split into runs of equal idx
                run_start = 0
                curr = idxs[0] if idxs else 0
                for i, idx in enumerate(idxs):
                    if idx != curr:
                        xs = (calc.x[run_start : i + 1] * 1000.0).tolist()
                        ys = (calc.y[run_start : i + 1] * 1000.0).tolist()
                        layer = seg_layer(curr)
                        entities.append(self._dxf_polyline_entity(xs, ys, layer=layer))
                        mid = max(0, len(xs) // 2)
                        entities.append(self._dxf_text_entity(xs[mid], ys[mid] + text_off, seg_label(curr), height=text_h, layer=layer))
                        run_start = i
                        curr = idx

                # last run
                if idxs:
                    xs = (calc.x[run_start:] * 1000.0).tolist()
                    ys = (calc.y[run_start:] * 1000.0).tolist()
                    layer = seg_layer(curr)
                    entities.append(self._dxf_polyline_entity(xs, ys, layer=layer))
                    mid = max(0, len(xs) // 2)
                    entities.append(self._dxf_text_entity(xs[mid], ys[mid] + text_off, seg_label(curr), height=text_h, layer=layer))

            # 2) Export chute geometry (full quadrant) and cable-on-chute (contact arc) split by segment.
            if R > 0 and calc.exit_angle_deg_from_h is not None:
                x_top = layback
                y_top = c
                center_x = x_top
                center_y = y_top - R

                # Full quadrant geometry (upper-left)
                phis_full = np.linspace(math.pi / 2.0, math.pi, 160)
                chute_x = (center_x + R * np.cos(phis_full)) * 1000.0
                chute_y = (center_y + R * np.sin(phis_full)) * 1000.0
                entities.append(self._dxf_polyline_entity(chute_x.tolist(), chute_y.tolist(), layer=self._dxf_sanitize_layer("CHUTE_GEOM")))

                # Chute label (leader-style)
                chute_layer = self._dxf_sanitize_layer("CHUTE")
                x_label = (x_top * 1000.0) + (2.5 * text_off)
                y_label = (y_top * 1000.0) + (2.5 * text_off)
                entities.append(self._dxf_line_entity(x_top * 1000.0, y_top * 1000.0, x_label, y_label, layer=chute_layer))
                entities.append(self._dxf_text_entity(x_label, y_label, f"Chute | R={R:.3f} m | top={c:.3f} m", height=text_h, layer=chute_layer))

                # Contact portion: d in [0, Lc]
                theta_end = max(0.0, min(math.pi / 2.0, math.radians(float(calc.exit_angle_deg_from_h))))
                Lc = float(getattr(calc, "chute_contact_len_m", 0.0))
                if Lc > 1e-9:
                    n = 120
                    d_vals = np.linspace(0.0, Lc, n)
                    phis = (math.pi / 2.0) + (d_vals / R)
                    cx = center_x + R * np.cos(phis)
                    cy = center_y + R * np.sin(phis)

                    # split contact arc by segment index using midpoint distance from top
                    d_mid2 = 0.5 * (d_vals[:-1] + d_vals[1:])
                    idxs2 = [segment_index_for_d(float(d)) for d in d_mid2]
                    run_start = 0
                    curr = idxs2[0] if idxs2 else 0
                    for i, idx in enumerate(idxs2):
                        if idx != curr:
                            xs = (cx[run_start : i + 1] * 1000.0).tolist()
                            ys = (cy[run_start : i + 1] * 1000.0).tolist()
                            entities.append(self._dxf_polyline_entity(xs, ys, layer=seg_layer(curr)))
                            run_start = i
                            curr = idx

                    if idxs2:
                        xs = (cx[run_start:] * 1000.0).tolist()
                        ys = (cy[run_start:] * 1000.0).tolist()
                        entities.append(self._dxf_polyline_entity(xs, ys, layer=seg_layer(curr)))

            # 3) Export any remaining assembly on seabed (if assembly longer than suspended span)
            if calc.S_total is not None:
                asm_seg_total = sum(max(0.0, it.length_m) for it in seg_items)
                seabed_len = max(0.0, asm_seg_total - float(calc.S_total))
                if seabed_len > 1e-6:
                    n_pts = max(2, int(min(800, max(2, seabed_len / max(float(self.ds_step.value()), 0.25)))))
                    xs = np.linspace(0.0, -seabed_len, n_pts)
                    ys = np.full_like(xs, -D)

                    # split by segment index using d_from_top = S_total + distance from TDP on seabed
                    x_mid = 0.5 * (xs[:-1] + xs[1:])
                    d_mid3 = float(calc.S_total) + (-x_mid)
                    idxs3 = [segment_index_for_d(float(d)) for d in d_mid3]
                    run_start = 0
                    curr = idxs3[0] if idxs3 else 0
                    for i, idx in enumerate(idxs3):
                        if idx != curr:
                            px = (xs[run_start : i + 1] * 1000.0).tolist()
                            py = (ys[run_start : i + 1] * 1000.0).tolist()
                            entities.append(self._dxf_polyline_entity(px, py, layer=seg_layer(curr)))
                            run_start = i
                            curr = idx
                    if idxs3:
                        px = (xs[run_start:] * 1000.0).tolist()
                        py = (ys[run_start:] * 1000.0).tolist()
                        entities.append(self._dxf_polyline_entity(px, py, layer=seg_layer(curr)))

            # 4) Export bodies as POINT + TEXT labels
            d_cursor = 0.0
            for it in assembly:
                if it.kind == "segment":
                    d_cursor += max(0.0, it.length_m)
                    continue
                if it.kind != "body":
                    continue

                d_body = float(d_cursor)
                x_body_m: Optional[float] = None
                y_body_m: Optional[float] = None

                S_free = float(calc.s[-1]) if calc.s is not None else 0.0
                Lc = float(getattr(calc, "chute_contact_len_m", 0.0))

                # Body on chute contact
                if R > 0 and d_body < Lc:
                    x_top = layback
                    y_top = c
                    center_x = x_top
                    center_y = y_top - R
                    phi = (math.pi / 2.0) + (d_body / R)
                    x_body_m = center_x + R * math.cos(phi)
                    y_body_m = center_y + R * math.sin(phi)
                # Body on free-span
                elif calc.s is not None and d_body <= (Lc + S_free):
                    s_body = S_free - (d_body - Lc)
                    x_body_m = float(np.interp(s_body, calc.s, calc.x))
                    y_body_m = float(np.interp(s_body, calc.s, calc.y))
                # Body on seabed beyond TDP
                elif calc.S_total is not None and d_body > float(calc.S_total):
                    x_body_m = -(d_body - float(calc.S_total))
                    y_body_m = -D

                if x_body_m is None or y_body_m is None:
                    continue

                layer = self._dxf_sanitize_layer(f"BODY_{it.name}")
                xb = x_body_m * 1000.0
                yb = y_body_m * 1000.0

                # Visible body marker geometry (small square) + point
                body_size = max(250.0, 0.9 * text_h)
                entities.append(self._dxf_point_entity(xb, yb, layer=layer))
                entities.append(self._dxf_rectangle_entity(xb, yb, body_size, body_size, layer=layer))

                # Leader-style label
                x_text = xb + (2.0 * text_off)
                y_text = yb + (1.0 * text_off)
                entities.append(self._dxf_line_entity(xb, yb, x_text, y_text, layer=layer))
                entities.append(self._dxf_text_entity(x_text, y_text, f"{it.name} | load={float(it.point_load_kN):.3f} kN", height=text_h, layer=layer))

        else:
            # No assembly: export a single cable polyline on a single layer.
            x_mm = (calc.x * 1000.0).tolist()
            y_mm = (calc.y * 1000.0).tolist()
            entities.append(self._dxf_polyline_entity(x_mm, y_mm, layer=self._dxf_sanitize_layer("CABLE")))

            # Chute geometry and contact arc (optional)
            if R > 0 and calc.layback is not None and calc.exit_angle_deg_from_h is not None:
                x_top = layback
                y_top = c
                center_x = x_top
                center_y = y_top - R

                phis_full = np.linspace(math.pi / 2.0, math.pi, 160)
                chute_x = (center_x + R * np.cos(phis_full)) * 1000.0
                chute_y = (center_y + R * np.sin(phis_full)) * 1000.0
                entities.append(self._dxf_polyline_entity(chute_x.tolist(), chute_y.tolist(), layer=self._dxf_sanitize_layer("CHUTE_GEOM")))

                chute_layer = self._dxf_sanitize_layer("CHUTE")
                x_label = (x_top * 1000.0) + (2.5 * text_off)
                y_label = (y_top * 1000.0) + (2.5 * text_off)
                entities.append(self._dxf_line_entity(x_top * 1000.0, y_top * 1000.0, x_label, y_label, layer=chute_layer))
                entities.append(self._dxf_text_entity(x_label, y_label, f"Chute | R={R:.3f} m | top={c:.3f} m", height=text_h, layer=chute_layer))

                theta_end = math.radians(float(calc.exit_angle_deg_from_h))
                theta_end = max(0.0, min(math.pi / 2.0, theta_end))
                phi0 = math.pi / 2.0
                phi1 = math.pi / 2.0 + theta_end
                phis = np.linspace(phi0, phi1, 90)
                arc_x = (center_x + R * np.cos(phis)) * 1000.0
                arc_y = (center_y + R * np.sin(phis)) * 1000.0
                entities.append(self._dxf_polyline_entity(arc_x.tolist(), arc_y.tolist(), layer=self._dxf_sanitize_layer("CHUTE_CONTACT")))

        # Reference lines: sea level and seabed
        # Use an x-span that covers all likely exported geometry (cable + chute + optional seabed).
        x_min_m = float(np.min(calc.x))
        x_max_m = float(np.max(calc.x))
        if R > 0:
            x_min_m = min(x_min_m, layback - R)
            x_max_m = max(x_max_m, layback)
        if assembly and calc.S_total is not None:
            seg_items2 = [it for it in assembly if it.kind == "segment"]
            asm_seg_total = sum(max(0.0, it.length_m) for it in seg_items2)
            seabed_len = max(0.0, asm_seg_total - float(calc.S_total))
            if seabed_len > 1e-6:
                x_min_m = min(x_min_m, -seabed_len)

        pad_m = max(5.0, 0.05 * max(1.0, x_max_m - x_min_m))
        x0 = (x_min_m - pad_m) * 1000.0
        x1 = (x_max_m + pad_m) * 1000.0

        sea_layer = self._dxf_sanitize_layer("REF_SEA")
        seabed_layer = self._dxf_sanitize_layer("REF_SEABED")

        # Sea level at y=0
        entities.append(self._dxf_line_entity(x0, 0.0, x1, 0.0, layer=sea_layer))
        entities.append(self._dxf_text_entity(x1, 0.0 + text_off, "Sea level (y=0)", height=text_h, layer=sea_layer))

        # Seabed at y=-D (internal coordinates)
        y_seabed = (-D) * 1000.0
        entities.append(self._dxf_line_entity(x0, y_seabed, x1, y_seabed, layer=seabed_layer))
        entities.append(self._dxf_text_entity(x1, y_seabed + text_off, f"Seabed (y=-{D:.3f} m)", height=text_h, layer=seabed_layer))

        dxf = self._dxf_build(entities)
        with open(path, "w") as f:
            f.write(dxf)

    # ---- Components table helpers

    def _table_get_float(self, table: QTableWidget, row: int, col: int, default: float = 0.0) -> float:
        item = table.item(row, col)
        if item is None:
            return default
        try:
            return float(item.text())
        except Exception:
            return default

    def _table_get_str(self, table: QTableWidget, row: int, col: int, default: str = "") -> str:
        item = table.item(row, col)
        if item is None:
            return default
        val = str(item.text()).strip()
        return val if val else default

    # ---- Assembly table helpers

    def _assembly_from_table(self) -> List[AssemblyItem]:
        items: List[AssemblyItem] = []
        for r in range(self.assembly_table.rowCount()):
            kind_raw = self._table_get_str(self.assembly_table, r, self.ASM_COL_TYPE, default="segment").lower()
            kind = "segment" if kind_raw.startswith("seg") else "body"
            name = self._table_get_str(self.assembly_table, r, self.ASM_COL_NAME, default=("Segment" if kind == "segment" else "Body"))
            length = self._table_get_float(self.assembly_table, r, self.ASM_COL_LENGTH, 0.0)
            q_w = self._table_get_float(self.assembly_table, r, self.ASM_COL_Q_WATER, 0.0)
            q_a = self._table_get_float(self.assembly_table, r, self.ASM_COL_Q_AIR, 0.0)
            p_kN = self._table_get_float(self.assembly_table, r, self.ASM_COL_BODY_LOAD, 0.0)
            color_hex = self._table_get_str(self.assembly_table, r, self.ASM_COL_COLOR, default="")
            color_hex = self._normalize_color_hex(color_hex)

            if kind == "segment":
                if length <= 0:
                    continue
                if q_w <= 0 or q_a <= 0:
                    # allow blank to mean "use global" by keeping 0s
                    pass
                items.append(AssemblyItem(kind=kind, name=name, length_m=length, q_water_npm=q_w, q_air_npm=q_a, point_load_kN=0.0, color_hex=color_hex))
            else:
                items.append(AssemblyItem(kind=kind, name=name, length_m=0.0, q_water_npm=0.0, q_air_npm=0.0, point_load_kN=p_kN, color_hex=""))

        return items

    def _on_asm_add_segment(self):
        self.assembly_table.blockSignals(True)
        try:
            r = self.assembly_table.rowCount()
            self.assembly_table.insertRow(r)
            defaults: List[Tuple[int, Any]] = [
                (0, "Segment"),
                (1, "Cable"),
                (2, 10.0),
                (3, float(self.weight_water.value()) if self.weight_unit.currentText() == "N/m" else 0.0),
                (4, float(self.weight_air.value()) if self.weight_unit.currentText() == "N/m" else 0.0),
                (5, 0.0),
            ]
            for col, val in defaults:
                self.assembly_table.setItem(r, col, QTableWidgetItem(str(val)))

            self._set_assembly_color_cell(r, self._next_default_segment_color_hex())
            self.assembly_table.setCurrentCell(r, 1)
        finally:
            self.assembly_table.blockSignals(False)
        self.update_plot()

    def _on_asm_add_body(self):
        self.assembly_table.blockSignals(True)
        try:
            r = self.assembly_table.rowCount()
            self.assembly_table.insertRow(r)
            defaults: List[Tuple[int, Any]] = [
                (0, "Body"),
                (1, "Body"),
                (2, 0.0),
                (3, 0.0),
                (4, 0.0),
                (5, 5.0),
            ]
            for col, val in defaults:
                self.assembly_table.setItem(r, col, QTableWidgetItem(str(val)))

            self._set_assembly_color_cell(r, "", enabled=False)
            self.assembly_table.setCurrentCell(r, 1)
        finally:
            self.assembly_table.blockSignals(False)
        self.update_plot()

    def _on_asm_delete(self):
        r = self.assembly_table.currentRow()
        if r < 0:
            return
        self.assembly_table.removeRow(r)
        self.update_plot()

    def _asm_swap_rows(self, r1: int, r2: int):
        if r1 < 0 or r2 < 0:
            return
        if r1 >= self.assembly_table.rowCount() or r2 >= self.assembly_table.rowCount():
            return
        self.assembly_table.blockSignals(True)
        try:
            for c in range(self.assembly_table.columnCount()):
                i1 = self.assembly_table.takeItem(r1, c)
                i2 = self.assembly_table.takeItem(r2, c)
                self.assembly_table.setItem(r1, c, i2)
                self.assembly_table.setItem(r2, c, i1)
        finally:
            self.assembly_table.blockSignals(False)

    def _on_asm_move_up(self):
        r = self.assembly_table.currentRow()
        if r <= 0:
            return
        self._asm_swap_rows(r, r - 1)
        self.assembly_table.setCurrentCell(r - 1, 1)
        self.update_plot()

    def _on_asm_move_down(self):
        r = self.assembly_table.currentRow()
        if r < 0 or r >= self.assembly_table.rowCount() - 1:
            return
        self._asm_swap_rows(r, r + 1)
        self.assembly_table.setCurrentCell(r + 1, 1)
        self.update_plot()

    def _assembly_table_to_json(self) -> str:
        data: List[List[str]] = []
        for r in range(self.assembly_table.rowCount()):
            row_vals: List[str] = []
            for c in range(self.assembly_table.columnCount()):
                it = self.assembly_table.item(r, c)
                row_vals.append("") if it is None else row_vals.append(str(it.text()))
            data.append(row_vals)
        return json.dumps(data)

    def _assembly_table_from_json(self, raw: str):
        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                return
        except Exception:
            return
        self.assembly_table.blockSignals(True)
        try:
            self.assembly_table.setRowCount(0)
            for row in data:
                if not isinstance(row, list):
                    continue
                r = self.assembly_table.rowCount()
                self.assembly_table.insertRow(r)
                for c in range(min(len(row), self.assembly_table.columnCount())):
                    self.assembly_table.setItem(r, c, QTableWidgetItem(str(row[c])))

                # Ensure colour cell exists / has reasonable defaults for older saved tables.
                self._ensure_assembly_color_cell(r)
        finally:
            self.assembly_table.blockSignals(False)

    def _normalize_color_hex(self, value: str) -> str:
        s = (value or "").strip()
        if not s:
            return ""
        if not s.startswith("#"):
            s = "#" + s
        if len(s) != 7:
            return ""
        try:
            _ = int(s[1:], 16)
        except Exception:
            return ""
        return s.lower()

    def _is_assembly_row_segment(self, row: int) -> bool:
        kind_raw = self._table_get_str(self.assembly_table, row, self.ASM_COL_TYPE, default="segment").lower()
        return kind_raw.startswith("seg")

    def _next_default_segment_color_hex(self) -> str:
        # Choose based on the count of existing segment rows (not total rows).
        seg_count = 0
        for r in range(self.assembly_table.rowCount()):
            if self._is_assembly_row_segment(r):
                seg_count += 1
        return self._DEFAULT_SEGMENT_COLORS[seg_count % len(self._DEFAULT_SEGMENT_COLORS)]

    def _set_assembly_color_cell(self, row: int, color_hex: str, enabled: bool = True):
        # Show hex + a background swatch. Disable editing; colour is set via dialog.
        item = self.assembly_table.item(row, self.ASM_COL_COLOR)
        if item is None:
            item = QTableWidgetItem("")
            self.assembly_table.setItem(row, self.ASM_COL_COLOR, item)

        color_hex = self._normalize_color_hex(color_hex)
        item.setText(color_hex)
        item.setToolTip("Double-click to pick a colour" if enabled else "(Colour applies to segment rows only)")

        flags = item.flags()
        flags = flags & ~Qt.ItemIsEditable
        if enabled:
            flags = flags | Qt.ItemIsEnabled
        else:
            flags = flags & ~Qt.ItemIsEnabled
        item.setFlags(flags)

        if enabled and color_hex:
            try:
                qcol = QColor(color_hex)
                item.setBackground(qcol)
            except Exception:
                pass
        else:
            item.setBackground(QColor())

    def _ensure_assembly_color_cell(self, row: int):
        # Ensure the colour cell exists and is enabled only for segment rows.
        is_seg = self._is_assembly_row_segment(row)
        current = self._table_get_str(self.assembly_table, row, self.ASM_COL_COLOR, default="")
        current = self._normalize_color_hex(current)
        if is_seg and not current:
            current = self._next_default_segment_color_hex()
        self._set_assembly_color_cell(row, current, enabled=is_seg)

    def _on_assembly_table_cell_changed(self, row: int, col: int):
        # Keep the colour column consistent when users change the Type cell.
        if col == self.ASM_COL_TYPE:
            self.assembly_table.blockSignals(True)
            try:
                self._ensure_assembly_color_cell(row)
            finally:
                self.assembly_table.blockSignals(False)
        self.schedule_update_plot()

    def _on_assembly_table_cell_double_clicked(self, row: int, col: int):
        if col != self.ASM_COL_COLOR:
            return
        if row < 0 or row >= self.assembly_table.rowCount():
            return
        if not self._is_assembly_row_segment(row):
            return

        current = self._table_get_str(self.assembly_table, row, self.ASM_COL_COLOR, default="")
        current = self._normalize_color_hex(current)
        initial = QColor(current) if current else QColor("#1f77b4")
        chosen = QColorDialog.getColor(initial, self, "Select segment colour")
        if not chosen.isValid():
            return

        self.assembly_table.blockSignals(True)
        try:
            self._set_assembly_color_cell(row, chosen.name(), enabled=True)
        finally:
            self.assembly_table.blockSignals(False)
        self.update_plot()


    def _dxf_sanitize_layer(self, name: str) -> str:
        # Conservative layer naming for broad DXF compatibility.
        raw = (name or "0").strip().upper().replace(" ", "_")
        cleaned = "".join(ch for ch in raw if (ch.isalnum() or ch in ("_", "-")))
        return (cleaned[:31] or "0")

    def _dxf_polyline_entity(self, x: List[float], y: List[float], layer: str = "0") -> str:
        layer = self._dxf_sanitize_layer(layer)
        ent = f"0\nPOLYLINE\n8\n{layer}\n66\n1\n70\n0\n"
        for xi, yi in zip(x, y):
            ent += f"0\nVERTEX\n8\n{layer}\n10\n{xi}\n20\n{yi}\n30\n0.0\n"
        ent += "0\nSEQEND\n"
        return ent

    def _dxf_point_entity(self, x: float, y: float, layer: str = "0") -> str:
        layer = self._dxf_sanitize_layer(layer)
        return f"0\nPOINT\n8\n{layer}\n10\n{x}\n20\n{y}\n30\n0.0\n"

    def _dxf_line_entity(self, x1: float, y1: float, x2: float, y2: float, layer: str = "0") -> str:
        layer = self._dxf_sanitize_layer(layer)
        return (
            f"0\nLINE\n8\n{layer}\n"
            f"10\n{x1}\n20\n{y1}\n30\n0.0\n"
            f"11\n{x2}\n21\n{y2}\n31\n0.0\n"
        )

    def _dxf_rectangle_entity(self, x_center: float, y_center: float, width: float, height: float, layer: str = "0") -> str:
        # Axis-aligned rectangle polyline centered at (x_center, y_center)
        layer = self._dxf_sanitize_layer(layer)
        hw = 0.5 * float(width)
        hh = 0.5 * float(height)
        xs = [x_center - hw, x_center + hw, x_center + hw, x_center - hw, x_center - hw]
        ys = [y_center - hh, y_center - hh, y_center + hh, y_center + hh, y_center - hh]
        return self._dxf_polyline_entity(xs, ys, layer=layer)

    def _dxf_text_entity(self, x: float, y: float, text: str, height: float, layer: str = "0") -> str:
        layer = self._dxf_sanitize_layer(layer)
        safe = (text or "").replace("\n", " ").replace("\r", " ")
        # TEXT entity (single-line)
        return (
            f"0\nTEXT\n8\n{layer}\n"
            f"10\n{x}\n20\n{y}\n30\n0.0\n"
            f"40\n{height}\n1\n{safe}\n7\nSTANDARD\n"
        )

    def _dxf_build(self, entities: List[str]) -> str:
        # Minimal ASCII DXF with a single ENTITIES section.
        body = "".join(entities or [])
        return f"0\nSECTION\n2\nENTITIES\n{body}0\nENDSEC\n0\nEOF\n"

    def generate_dxf_polyline(self, x: List[float], y: List[float]) -> str:
        # Backwards-compatible wrapper.
        return self._dxf_build([self._dxf_polyline_entity(x, y, layer="0")])
