# -*- coding: utf-8 -*-
"""Closed-form uniform-catenary calculator (legacy Catenary Calculator V1).

Pure-Python calculation core, deliberately free of any QGIS / Qt imports so it
can be unit-tested outside the QGIS runtime. The V1 dialog wraps this class.

Model and assumptions
---------------------
* 2D static catenary in a vertical plane, uniform submerged weight ``q`` (N/m).
* The cable departs the touchdown point (TDP) tangentially on a flat, horizontal
  seabed; ``totalHeight`` is the vertical rise from TDP to the exit point
  (water depth for a surface vessel with zero chute height).
* No bending stiffness, no current/drag loading, no chute geometry, no
  elasticity. For chute radius, sloped seabeds, multi-segment assemblies and
  point loads use Catenary Calculator V2 (``catenary_solver``).

Exact identities used (a = H/q):
    x_deck = a*acosh(1 + h/a)         (layback)
    s      = a*sinh(x_deck/a)         (suspended length)
    s^2    = h^2 + 2*a*h              (length identity)
    T_top  = H + q*h                  (top tension)
"""

import math

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


class CatenaryCalculator:
    def __init__(self, config):
        self.config = config
        self.bottomTension = None
        self.topTension = None
        self.exitAngle = None
        self.catenaryLength = None
        self.xDeck = None

    def calculate(self):
        q = self.config['weightInWater']
        totalHeight = self.config['waterDepth']
        param = self.config['inputParameter']
        if param == 'Bottom Tension':
            H = self.config['bottomTension'] * 1000
            self._from_bottom_tension(H, q, totalHeight)
        elif param == 'Top Tension':
            Ts = self.config['topTension'] * 1000
            self._from_top_tension(Ts, q, totalHeight)
        elif param == 'Exit Angle':
            angleRad = math.radians(self.config['exitAngle'])
            self._from_exit_angle(angleRad, q, totalHeight)
        elif param == 'Catenary Length':
            L = self.config['catenaryLength']
            self._from_catenary_length(L, q, totalHeight)
        elif param == 'Layback':
            xDeck = self.config['layback']
            self._from_layback(xDeck, q, totalHeight)
        else:
            raise ValueError('Invalid input parameter')

    def _from_bottom_tension(self, H, q, totalHeight):
        self.xDeck = (H / q) * math.acosh((q * totalHeight / H) + 1)
        self.catenaryLength = (H / q) * math.sinh(q * self.xDeck / H)
        self.exitAngle = math.degrees(math.atan((q * self.catenaryLength) / H))
        self.topTension = math.sqrt(H ** 2 + (q * self.catenaryLength) ** 2) / 1000
        self.bottomTension = H / 1000

    def _from_top_tension(self, Ts_N, q, totalHeight):
        # For the uniform catenary T_top = H + q*h exactly (T = H + q*y), so
        # the infimum of achievable top tension (H -> 0) is q*h. Guard with a
        # clear message instead of a cryptic bracketing failure.
        if Ts_N <= q * totalHeight:
            raise ValueError(
                f'Top tension ({Ts_N / 1000:.2f} kN) must exceed the submerged weight of '
                f'a vertical cable span over the water depth '
                f'(q x depth = {q * totalHeight / 1000:.2f} kN).'
            )
        # Closed form: T_top = H + q*h  =>  H = T_top - q*h (exact).
        H = Ts_N - q * totalHeight
        self._from_bottom_tension(H, q, totalHeight)

    def _from_exit_angle(self, angleRad, q, totalHeight):
        cosTheta = math.cos(angleRad)
        if cosTheta >= 1.0:
            raise ValueError('Exit angle must be > 0 degrees')
        if cosTheta <= 0.0:
            raise ValueError('Exit angle must be < 90 degrees')
        H = (q * totalHeight * cosTheta) / (1 - cosTheta)
        self._from_bottom_tension(H, q, totalHeight)

    def _from_catenary_length(self, S, q, totalHeight):
        if S <= totalHeight:
            raise ValueError('Catenary length must be > water depth')
        # Closed form. For the uniform catenary anchored tangentially at the
        # TDP, s^2 = h^2 + 2*a*h with a = H/q (exact identity), so
        # H = q*(S^2 - h^2) / (2*h).
        # The previous bisection used an upper bracket of q*S, which is below
        # the true root whenever S > (1 + sqrt(2))*h (~2.41x depth) and made
        # the mode fail with "Function does not change sign" in perfectly
        # valid configurations.
        H = q * (S * S - totalHeight * totalHeight) / (2.0 * totalHeight)
        self._from_bottom_tension(H, q, totalHeight)

    def _from_layback(self, xDeck, q, totalHeight):
        if xDeck <= 0:
            raise ValueError('Layback must be > 0')
        def to_solve(H):
            return (H / q) * math.acosh((q * totalHeight / H) + 1) - xDeck
        # xDeck(H) is monotonically increasing in H, so expand the upper
        # bracket until it spans the root. The previous fixed upper bound
        # (q*h*100) failed for layback greater than ~14x the water depth —
        # a realistic shallow-water case.
        lower = 1e-3
        # Flat-catenary estimate: x ~ sqrt(2*a*h) => a ~ x^2/(2h).
        upper = max(q * totalHeight, q * xDeck * xDeck / (2.0 * totalHeight)) * 4.0
        for _ in range(80):
            if to_solve(upper) > 0:
                break
            upper *= 2.0
        else:
            raise ValueError('Layback target could not be bracketed.')
        H = self._find_root_bisection(to_solve, lower, upper)
        self._from_bottom_tension(H, q, totalHeight)

    def _find_root_bisection(self, func, lower, upper, tol=1e-7, max_iter=100):
        a, b = lower, upper
        fa, fb = func(a), func(b)
        if fa * fb > 0:
            raise ValueError('Function does not change sign over interval')
        for _ in range(max_iter):
            c = (a + b) / 2
            fc = func(c)
            if abs(fc) < tol or (b - a) / 2 < tol:
                return c
            if fa * fc < 0:
                b, fb = c, fc
            else:
                a, fa = c, fc
        raise ValueError('Root finding did not converge')

    def get_catenary_shape(self, num_points=100):
        if np is None:
            raise ImportError('NumPy is required for get_catenary_shape.')
        H = self.bottomTension * 1000
        q = self.config['weightInWater']
        x = np.linspace(0, self.xDeck, num_points + 1)
        y = (H / q) * (np.cosh((q * x) / H) - 1)
        return x, y

    def calculate_minimum_radius(self, x, y):
        if np is None:
            raise ImportError('NumPy is required for calculate_minimum_radius.')
        dx = np.gradient(x)
        dy = np.gradient(y)
        ddx = np.gradient(dx)
        ddy = np.gradient(dy)
        curvature = np.abs(dx * ddy - dy * ddx) / np.power(dx * dx + dy * dy, 1.5)
        max_curv = np.max(curvature)
        if max_curv == 0:
            return float('inf')
        return 1 / max_curv
