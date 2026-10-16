# -*- coding: utf-8 -*-
"""Tensioned beam (tension T and bending stiffness EI) on a rigid unilateral seabed.

The detail model. Small slopes, on a uniform grid of spacing ``h``:

    EI y'''' - H y'' = -q + p,      p >= 0,   y >= seabed,   p (y - seabed) = 0

``q`` is the submerged weight per metre and ``p`` the seabed reaction. The
energy ``sum EI/2 (y'')^2 h + H/2 (y')^2 h + q y h`` is discretised with
central differences, so the stiffness matrix ``K`` is symmetric, positive
definite and pentadiagonal. In the gap ``u = y - seabed`` the problem is
the linear complementarity problem (LCP)

    K u + q' = p,   u >= 0,   p >= 0,   u . p = 0

(Binda & Contro 1979 for the string, ``EI = 0``; with ``EI > 0`` it is the
unilateral beam problem of Maier & Andreuzzi 1978). It is solved by a
primal-dual active set method (Hintermueller, Ito & Kunisch 2002) with an
O(n) banded LDL^T solve per step, falling back to projected successive
over-relaxation (Cryer 1971) if the active set cycles. With ``EI > 0`` the
matrix is not a Minkowski matrix, so the Binda & Contro monotonicity (spans
only grow with tension) is not guaranteed.

Bodies (repeaters, joints) are stiff elements with their own weight. The
ends of the window are held at a given shape (two nodes each end).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from .core import check_cancel

RIGID_EI_NM2 = 1.0e8  # 1e5 kN m^2: an in-line body housing, effectively rigid


@dataclass
class BeamResult:
    x_m: np.ndarray
    obstacle_m: np.ndarray       # seabed elevation the cable rests on (positive up)
    y_m: np.ndarray              # cable elevation
    reaction_n: np.ndarray       # nodal seabed reaction (N); nan at the held end nodes
    h_n: np.ndarray              # horizontal tension per node (N)
    ei_nm2: np.ndarray
    curvature: np.ndarray        # 1/m, positive sagging (concave up)
    converged: bool
    iterations: int
    method: str
    spacing_m: float

    @property
    def gap_m(self) -> np.ndarray:
        return self.y_m - self.obstacle_m

    @property
    def moment_nm(self) -> np.ndarray:
        return self.ei_nm2 * self.curvature

    @property
    def shear_n(self) -> np.ndarray:
        return np.gradient(self.moment_nm, self.x_m) if len(self.x_m) > 2 else np.zeros(len(self.x_m))

    @property
    def bend_radius_m(self) -> np.ndarray:
        slope = np.gradient(self.y_m, self.x_m) if len(self.x_m) > 2 else np.zeros(len(self.x_m))
        with np.errstate(divide="ignore", invalid="ignore"):
            radius = (1.0 + slope ** 2) ** 1.5 / np.abs(self.curvature)
        return np.where(np.isfinite(radius), radius, np.inf)


def penta_solve(d0: np.ndarray, d1: np.ndarray, d2: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Solve ``A x = b`` for a symmetric positive-definite pentadiagonal ``A``.

    ``d0`` the diagonal, ``d1[i] = A[i, i+1]``, ``d2[i] = A[i, i+2]``. Banded
    LDL^T, O(n).
    """
    n = len(d0)
    a0 = [float(v) for v in d0]
    a1 = [float(v) for v in d1] + [0.0, 0.0]
    a2 = [float(v) for v in d2] + [0.0, 0.0, 0.0]
    rhs = [float(v) for v in b]
    dd = [0.0] * n
    l1 = [0.0] * (n + 2)
    l2 = [0.0] * (n + 2)
    for i in range(n):
        di = a0[i]
        if i >= 1:
            di -= l1[i - 1] * l1[i - 1] * dd[i - 1]
        if i >= 2:
            di -= l2[i - 2] * l2[i - 2] * dd[i - 2]
        if di <= 0.0:
            raise ValueError("matrix not positive definite")
        dd[i] = di
        if i + 1 < n:
            v = a1[i]
            if i >= 1:
                v -= l2[i - 1] * l1[i - 1] * dd[i - 1]
            l1[i] = v / di
        if i + 2 < n:
            l2[i] = a2[i] / di
    z = [0.0] * n
    for i in range(n):
        v = rhs[i]
        if i >= 1:
            v -= l1[i - 1] * z[i - 1]
        if i >= 2:
            v -= l2[i - 2] * z[i - 2]
        z[i] = v
    for i in range(n):
        z[i] /= dd[i]
    out = [0.0] * n
    for i in range(n - 1, -1, -1):
        v = z[i]
        if i + 1 < n:
            v -= l1[i] * out[i + 1]
        if i + 2 < n:
            v -= l2[i] * out[i + 2]
        out[i] = v
    return np.asarray(out)


def _matvec(d0, d1, d2, u):
    r = d0 * u
    if len(u) > 1:
        r[:-1] += d1 * u[1:]
        r[1:] += d1 * u[:-1]
    if len(u) > 2:
        r[:-2] += d2 * u[2:]
        r[2:] += d2 * u[:-2]
    return r


def assemble(h: float, ei_nm2: np.ndarray, h_n: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Diagonals of the stiffness matrix for nodes on a uniform grid.

    Bending energy ``EI_i / 2 kappa_i^2 h`` at interior nodes (``kappa`` the
    second difference) and tension energy ``H_e / 2 (dy / h)^2 h`` per element
    (``H_e`` the mean of its nodes).
    """
    n = len(ei_nm2)
    d0 = np.zeros(n)
    d1 = np.zeros(max(n - 1, 0))
    d2 = np.zeros(max(n - 2, 0))
    a = np.asarray(ei_nm2, dtype=float)[1:-1] / h ** 3
    if n >= 3:
        d0[:-2] += a
        d0[1:-1] += 4.0 * a
        d0[2:] += a
        d1[:-1] += -2.0 * a
        d1[1:] += -2.0 * a
        d2 += a
    hv = np.asarray(h_n, dtype=float)
    b = 0.5 * (hv[1:] + hv[:-1]) / h
    d0[:-1] += b
    d0[1:] += b
    d1 += -b
    return d0, d1, d2


def solve_tensioned_beam(x_m: np.ndarray, obstacle_m: np.ndarray, w_npm: np.ndarray, h_n: np.ndarray,
                         ei_nm2: np.ndarray, loads_n: Optional[np.ndarray] = None,
                         y_ends: Optional[Tuple[float, float, float, float]] = None,
                         y_start: Optional[np.ndarray] = None, max_iter: int = 100,
                         cancel=None) -> BeamResult:
    """Rest a tensioned beam on the seabed (see the module docstring).

    ``x_m`` uniform, ascending; per node: ``obstacle_m`` seabed elevation,
    ``w_npm`` submerged weight, ``h_n`` horizontal tension, ``ei_nm2``
    bending stiffness, ``loads_n`` point loads (N, down). The two first and
    two last nodes are held at ``y_ends`` (default: on the seabed).
    ``y_start`` is an initial shape (for the first active set).
    """
    x = np.asarray(x_m, dtype=float)
    g = np.asarray(obstacle_m, dtype=float)
    n = len(x)
    if n < 6:
        raise ValueError("the detail window needs at least 6 nodes")
    h = float(x[1] - x[0])
    ei = np.maximum(np.asarray(ei_nm2, dtype=float), 0.0)
    hn = np.maximum(np.asarray(h_n, dtype=float), 1.0)  # at least 1 N: keeps K positive definite
    w = np.asarray(w_npm, dtype=float)
    f = w * h
    f[0] *= 0.5
    f[-1] *= 0.5
    if loads_n is not None:
        f = f + np.asarray(loads_n, dtype=float)
    d0, d1, d2 = assemble(h, ei, hn)
    ends = list(y_ends) if y_ends is not None else [g[0], g[1], g[-2], g[-1]]
    fixed = np.zeros(n, dtype=bool)
    fixed[[0, 1, n - 2, n - 1]] = True
    y_fixed = np.zeros(n)
    y_fixed[[0, 1, n - 2, n - 1]] = ends
    free = ~fixed
    # Reduced system on the free nodes: K_FF y_F + K_FC y_C + f_F = p_F.
    kfc = _matvec(d0, d1, d2, y_fixed)[free]
    fd0 = d0[2:-2].copy()   # free nodes are 2 .. n-3
    fd1 = d1[2:-2].copy()
    fd2 = d2[2:-2].copy()
    gf = g[free]
    qq = _matvec(fd0, fd1, fd2, gf) + kfc + f[free]
    m = len(gf)
    if y_start is not None:
        active = (np.asarray(y_start, dtype=float)[free] - gf) <= 1e-6
    else:
        active = np.zeros(m, dtype=bool)
    scale = float(np.median(fd0)) if m else 1.0
    tol_u = 1e-9
    tol_p = 1e-9 * max(scale, 1.0) * max(h, 1e-3)
    seen = set()
    converged = False
    method = "active set"
    u = np.zeros(m)
    iterations = 0
    for step in range(1, max_iter + 1):
        iterations = step
        check_cancel(cancel)
        a0, a1, a2 = fd0.copy(), fd1.copy(), fd2.copy()
        rhs = -qq.copy()
        if active.any():
            a0[active] = 1.0
            rhs[active] = 0.0
            a1[active[:-1] | active[1:]] = 0.0
            a2[active[:-2] | active[2:]] = 0.0
        u = penta_solve(a0, a1, a2, rhs)
        u[active] = 0.0
        p = _matvec(fd0, fd1, fd2, u) + qq
        new_active = np.where(active, p > -tol_p, u < -tol_u)
        if np.array_equal(new_active, active):
            converged = True
            break
        key = new_active.tobytes()
        if key in seen:
            break  # the active set cycles: finish with projected SOR
        seen.add(key)
        active = new_active
    if not converged:
        method = "projected SOR"
        u = np.maximum(u, 0.0)
        omega = 1.6
        colours = [np.arange(c, m, 3) for c in range(3)]
        for sweep in range(20000):
            if sweep % 50 == 0:
                check_cancel(cancel)
            for idx in colours:
                r = _matvec(fd0, fd1, fd2, u) + qq
                u[idx] = np.maximum(0.0, u[idx] - omega * r[idx] / fd0[idx])
            if sweep % 20 == 0:
                p = _matvec(fd0, fd1, fd2, u) + qq
                worst = np.max(np.where(u > tol_u, np.abs(p), np.maximum(-p, 0.0)))
                if worst <= 1e-6 * max(float(np.max(np.abs(f))), 1.0):
                    converged = True
                    break
        iterations += sweep + 1
    y = y_fixed.copy()
    y[free] = gf + u
    full = _matvec(d0, d1, d2, y) + f
    reaction = np.where(fixed, np.nan, np.maximum(full, 0.0))
    curvature = np.zeros(n)
    curvature[1:-1] = (y[:-2] - 2.0 * y[1:-1] + y[2:]) / h ** 2
    return BeamResult(x, g, y, reaction, hn, ei, curvature, converged, iterations, method, h)


def auto_spacing_m(ei_nm2: float, h_n: float, length_m: float, finest_m: float = 0.02,
                   coarsest_m: float = 1.0, max_nodes: int = 60000) -> float:
    """Grid spacing that resolves the bending length ``sqrt(EI / H)`` (a quarter of it)."""
    lam = math.sqrt(ei_nm2 / h_n) if ei_nm2 > 0 and h_n > 0 else coarsest_m * 4.0
    step = min(max(lam / 4.0, finest_m), coarsest_m)
    return max(step, length_m / max(max_nodes - 1, 1))
