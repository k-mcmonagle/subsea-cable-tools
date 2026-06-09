# Catenary Calculator — Model Notes, Assumptions and Limitations

This document states precisely what the two catenary tools in Subsea Cable
Tools compute, the assumptions behind them, and what they must **not** be used
for. It is written for installation engineers reviewing whether the tool is
appropriate for a given task.

> **Status / validation.** The calculation cores are validated against
> closed-form catenary solutions and internal consistency identities by the
> automated test suites (`tests/test_simple_catenary.py`,
> `tests/test_catenary_solver.py`). They have **not** been independently
> verified against commercial lay-simulation software (e.g. OrcaFlex,
> MakaiLay) or against measured field data. Treat results as planning-grade
> estimates, not engineering sign-off values, until such a comparison has been
> done for your cable and vessel configuration.

---

## Common scope (both calculators)

Both tools solve the **2D static equilibrium of a single suspended span** in a
vertical plane, from a tangential touchdown point (TDP) on the seabed up to
the vessel chute:

* Force balance: constant horizontal component `H`; vertical component `V(s)`
  accumulates distributed weight (and, in V2, point loads).
* Perfectly flexible cable: **no bending stiffness** (EI = 0) and **no axial
  elasticity** (inextensible).
* **No hydrodynamic loading**: no current drag, no vessel motion, no wave
  loading, no lay-speed effects. `H` is constant along the span only because
  fluid forces are neglected.
* Tangential touchdown: the cable leaves the seabed with zero bending moment,
  tangent to the local bed.
* Units: SI internally (N, m); tensions reported in kN; weights entered in
  N/m, kg/m (×9.80665) or lbf/ft (×4.4482/0.3048).

### Coordinate and sign conventions

* `y = 0` at sea surface, negative downward (depth = −y).
* V1: x = 0 at the TDP, increasing toward the vessel.
* V2 internal frame: x = 0 at the TDP increasing toward the chute; the
  "world" frame puts the chute at `x_world = 0` with `x_world` increasing
  toward and beyond the TDP (`x_world = tdp_x_world − x`).
* Seabed slope sign (V2): positive slope = bed deepens away from the chute,
  giving `V(0) = H·tan α ≥ 0` and bottom tension `T_TDP = H / cos α`.

---

## Catenary Calculator (Legacy V1) — `simple_catenary.py`

Closed-form uniform catenary. Exact identities used (`a = H/q`,
`h` = water depth):

| Quantity | Formula |
|---|---|
| Layback | `x = a·acosh(1 + h/a)` |
| Suspended length | `s = a·sinh(x/a)`, equivalently `s² = h² + 2ah` |
| Top tension | `T_top = H + q·h` (exact) |
| Exit angle | `tan θ = q·s / H` |

Additional V1 restrictions: flat horizontal seabed, uniform weight, no chute
radius (exit at the waterline-height reference), no point loads, no
air/water weight distinction above the surface.

## Catenary Calculator V2 — `catenary_solver.py`

Numerical integration (midpoint scheme, step `ds`) of the same statics, adding:

* **Multi-segment assemblies**: ordered segments with independent water/air
  weights, plus in-line **bodies** (repeaters, joints) as point loads.
  A blank/zero segment weight inherits the global fallback weight (a
  diagnostic warning names affected segments); a **negative segment weight is
  honoured as distributed buoyancy** — the segment bows the cable upward,
  matching the behaviour of negative point loads and legacy components.
* **Point loads / clump weights / discrete buoyancy** via legacy "components"
  (negative point loads = buoyancy). Distributed buoyancy may drive the net
  weight negative over a span (cable bows upward).
* **Chute geometry**: quarter-circle chute of radius `R`; contact length
  `R·θ` and departure-point geometry are iterated to consistency.
* **Air/water transition** at `y = 0` with the correct per-medium weight and
  an exact integration split at the crossing.
* **Sloped / profiled seabed**: planar slope or piecewise-linear depth
  profile; TDP boundary condition `V(0) = H·tan α_TDP`; the TDP horizontal
  position is converged by an Aitken-accelerated fixed point. Bottom-tension
  input means the **actual tension at the TDP** (`H = T·cos α` internally).
* **Solution-quality reporting**: convergence flags from actual residuals,
  half-step refinement deltas, free-span seabed clearance and penetration
  detection, slope sliding-stability advisories.

### Seabed-penetration semantics (important)

On a profiled seabed, the single-span solution can pass *through* a high spot
between the TDP and the chute. Physically the cable would rest on that high
spot — a **multi-span contact problem the single-span solve cannot
represent**. The solver keeps the self-consistent single-span geometry and
**reports** the penetration (banner, plot highlight, diagnostics). Do not
read tensions or layback from a solve flagged with penetration — run the
**Drape Check** instead (below), which resolves the contact properly.

### Surface-piercing semantics

A strongly buoyant section can lift a bight of cable above sea level in the
unconstrained static solution. Physically that section would **float at the
surface** with part of its buoyancy unused. The solver detects any
above-surface region other than the legitimate final run up to the chute,
reports the affected span and an estimated **redundant (excess) buoyancy**
(integral of net upward distributed force plus upward point loads over the
region, in kN), and the plot clamps the displayed cable to the surface there
(orange marker). Tensions and geometry in and beyond a flagged floating
region are **not physical** — reduce buoyancy by roughly the redundant
figure, or treat the system as a surface-floating arrangement outside this
model's scope. (A true free-surface flotation equilibrium solver is not
implemented.)

## Multi-span Drape Check — `drape_solver.py`

A second, independent solver for the **full static drape of the cable over
the seabed profile**, used from the V2 dialog's "Drape Check & Query"
section. Method: lumped-node static equilibrium found by dynamic relaxation
with kinetic damping —

* the cable is a chain of ~250–500 segments with stiff tension-only axial
  springs, driven to the inextensible limit by outer rest-length correction
  (residual stretch < 0.002 %);
* unilateral seabed contact via a penalty normal force on the bed polyline
  (equilibrium penetration ≈ millimetres);
* **Coulomb friction** (μ) via a stick-slip anchor model along the bed;
* boundary conditions: the chute departure point is held fixed (taken from
  the last single-span solve); the bottom end is **anchored** at a chosen
  point on the bed or **free** (free requires μ > 0 — a frictionless bed
  cannot hold bottom tension, and the solver rejects that combination);
* per-segment weights and point loads are mapped from the assembly, including
  buoyant (negative-weight) sections.

Validation (automated, `tests/test_drape_solver.py`): top tension within 1 %
and touchdown position within 2.5 % of the closed-form catenary on a flat
bed; frictionless flat bed transfers H unchanged to the anchor (2 %);
multi-span drape over a ridge with no penetration; friction never increases
the anchor-end tension; bend radius at the near-TDP region recovers a = H/q.

Drape-check caveats:

* The drape holds **total length and the chute departure point fixed**;
  tensions are redistributed by bed contact and will generally differ from
  the single-span solve (that redistribution is the point of the check).
* The chute arc itself is not part of the drape chain.
* With friction, static equilibria are **non-unique** (lay-history
  dependent); the returned state is one admissible equilibrium reached from
  the single-span shape — treat friction-sensitive outputs as indicative.
* Contact lift-off/touchdown points are resolved to ~one node spacing
  (≈ L/400).
* Runtime is a few seconds; it runs on demand, not live.

### Query system

"Query at s from chute" reports tension, angle, bend radius, depth, seabed
clearance and contact state at any arc position, from the drape result when
one has been run (covering the on-bed section too), otherwise from the
single-span solution.

---

## What is supported, partially supported, and not supported

| Capability | Status | Notes |
|---|---|---|
| 2D static single-span catenary (flat bed) | **Supported** | V1 closed form; V2 numerical. Validated against closed form to <0.02 m on standard cases. |
| Chute/quadrant geometry | **Supported (V2)** | Quarter-circle wrap model. |
| Multi-segment cable + repeaters/bodies | **Supported (V2)** | Static point loads only; min-radius output is not meaningful at point-load kinks (warned). |
| Discrete & distributed buoyancy | **Supported (V2)** | Negative point loads, negative legacy components, and negative assembly-segment weights all act as buoyancy. |
| Surface-floating buoyant sections | **Partial (V2)** | Detected and flagged with a redundant-buoyancy estimate; the display clamps to the surface. No flotation equilibrium solver. |
| Sloped / profiled seabed (single tangential TDP) | **Supported (V2)** | Penetration through high spots is detected and flagged; resolve it with the Drape Check. |
| Multi-span / intermediate seabed contact | **Supported (V2 Drape Check)** | Static lumped-node contact solver; on-demand, a few seconds per run. |
| Seabed friction | **Partial (V2 Drape Check)** | Coulomb stick-slip in the drape solver only; equilibria are lay-history dependent (non-unique). The single-span solve remains frictionless with advisory slope warnings. |
| End anchoring | **Supported (V2 Drape Check)** | Anchored or free bottom end (free requires friction). |
| Point queries (T, angle, radius, clearance) | **Supported (V2)** | "Query at s from chute"; uses the drape result when available. |
| 3D route geometry (out-of-plane lay, route curvature) | **Not supported** | Plane model only. KP/route tools are separate and geodesic-aware, but the catenary itself is 2D. |
| Current / hydrodynamic drag | **Not supported** | `H = const` assumption breaks under drag; results in strong currents will be wrong, especially exit angle and TDP position. |
| Dynamic / transient lay simulation (vessel motion, pay-out rate, touchdown dynamics) | **Not supported** | Requires a time-domain FE/lumped-mass model — out of scope for this architecture. |
| Buoy/mid-water arch systems, branching units as multi-leg systems | **Not supported** | Single span, single boundary at each end only. A branching unit can be approximated as a point load *only* if the other legs' tensions are known and resolved manually into a vertical force. |
| Bending stiffness / minimum-bend-radius mechanics | **Not supported** | Curvature output is geometric only; near point loads and the TDP it is not a structural MBR check. |
| Axial elasticity / elongation | **Not supported** | Inextensible cable assumed; high-tension HV cable stretch is not modelled. |

---

## Recommended next steps for extended capability (in order of value/risk)

1. **Uniform current drag (quasi-static)** (medium effort): straightforward to
   add to the drape solver (extra distributed force per node); the single-span
   solver would need its `H = const` root-finding layer reworked.
2. **Axial elasticity** (low effort, low risk): the drape solver already has
   the machinery (set a finite physical EA instead of the inextensible
   correction); the single-span integrator would scale `ds` by `(1 + T/EA)`.
3. **Free-surface flotation equilibrium** (medium effort): mirror of the
   seabed contact — add a `y ≤ 0` unilateral constraint with the surplus
   buoyancy reacted at the surface; the drape solver's penalty machinery can
   host it.
4. **3D static (route-plane decomposition)** (high effort): only worthwhile
   together with current modelling. Planned as a V3.
5. **Dynamic lay simulation** (very high effort): not recommended as an
   extension of this code base; integrate with a dedicated tool instead.
