# Catenary V3 — 3D Cable Lay Simulator: Design & Implementation Plan

Status: **implemented in 1.7.0** (2026-07-06) — all phases delivered as the
"Cable Lay Simulator (3D)" (`catenary/v3/`); the open questions in §9 were
resolved as recommended (software renderer, V1 removed, V2 retained,
rate-dependent drag included). This document is kept as the design record;
current assumptions/validation live in `catenary/v3/V3_MODEL_NOTES.md`.
Companion reading:
`catenary/v3/REFERENCE_DIGEST.md` (equations and constants distilled from the
reference papers) and `catenary/MODEL_NOTES.md` (V2 scope and its
"recommended next steps", which this plan supersedes for items 1, 4, 5).

## 1. Goals

1. **3D static + quasi-static cable model** — the V2 physics generalized to
   three dimensions over real bathymetry, with an interactive 3D view
   (orbit/pan/zoom) alongside trusted 2D profile/plan views.
2. **Hydrodynamic drag** — current loading on the suspended cable, and the
   classic steady-lay solution (ship speed + pay-out rate → critical angle,
   layback, touchdown, bottom tension) validated against Zajac 1957.
3. **Operation simulation (quasi-static time stepping)** — scripted vessel
   moves + pay-out schedules solved as a sequence of equilibria:
   - **Branching-unit deployment**: Y-topology (trunk to ship, two legs to
     seabed), BU descent path, leg tensions, touchdown.
   - **Final bight lay-down**: a bight lowered from a stepping vessel onto
     the seabed after a surface joint.
4. **No new dependencies**: NumPy (ships with QGIS), bundled pyqtgraph,
   Qt via `qgis.PyQt`. Engine code stays pure Python + NumPy (no Qt), like
   `drape_solver.py`.
5. **Ease of use**: progressive disclosure (simple defaults first), scenario
   presets, a timeline scrubber for simulations, honest warnings, and the
   same input conventions as V2 (assembly table, units, QSettings persistence).
6. **Engineering-grade honesty**: every mode documents its assumptions and
   validation status in a V3 model-notes file; results carry convergence
   diagnostics like V2.

Out of scope for V3 (stated up front, as in MODEL_NOTES): wave loading,
vessel seakeeping/added-mass dynamics, cable torsion/loop formation, axial
stick-slip on the drum. Quasi-static means no inertial transients — rate
effects enter only through drag.

## 2. What V3 builds on

- `drape_solver.py` already implements the right machinery in 2D: lumped
  nodes, tension-only axial springs with rest-length correction
  (inextensible limit), penalty seabed contact, Coulomb stick-slip
  friction, optional EI via discrete three-node moments, dynamic relaxation
  with kinetic damping. **V3's engine is this solver generalized to 3D**,
  plus drag, plus multi-chain topology, plus time stepping.
- `catenary_solver.py` (single span) remains the exact 2D reference and the
  seed/warm-start generator.
- The V2 dialog gives the UI conventions (collapsible sections, debounced
  solve, assembly table + JSON, results HTML, hover readouts, SVG/DXF
  export) — and its pain points tell us what to structure differently
  (see §6).

## 3. Physics specification

### 3.1 Coordinates and conventions
- Right-handed local frame: `x, y` horizontal (metres, route-local or
  projected CRS), `z` vertical, 0 at sea surface, negative down
  (consistent with V2's y-down-negative convention).
- Seabed is a surface `z_bed(x, y)`: flat, planar slope, extruded 2D
  profile (V2 parity), regular grid (imported or sampled from a QGIS
  raster), delivered to the engine as plain arrays.

### 3.2 Static 3D equilibrium (Mode A — parity with V2, in 3D)
Same force system as the 2D drape, vectorized in R^3:
- axial tension-only springs, outer rest-length correction to the
  inextensible limit (optionally finite EA later — the machinery allows it);
- distributed weight per segment (water/air by node depth), point loads
  and bodies from the assembly;
- unilateral contact on `z_bed(x,y)`: penalty normal force along the local
  surface normal (from the bilinear gradient), Coulomb stick-slip friction
  with 2D in-plane anchor points;
- bending: 3D generalization of the discrete three-node moment — the joint
  angle between adjacent segment tangents defines curvature
  `kappa = 2*theta/(L1+L2)`; the restoring moment acts in the plane spanned
  by the two tangents (moment axis = their cross product). Reduces exactly
  to the 2D formulation in-plane.
- current drag (§3.3) with stationary cable.

### 3.3 Hydrodynamic drag
Morison-type quadratic drag per unit length on each segment, tangent `t`:
```
u_rel = u_water(z) - v_cable          # relative water velocity
u_t   = (u_rel · t) t ;  u_n = u_rel - u_t
f_n   = 0.5 * rho * Cd_n * d   * |u_n| * u_n
f_t   = 0.5 * rho * Cd_t * pi*d * |u_t| * u_t
```
- `rho = 1025 kg/m^3` default; `d`, `Cd_n`, `Cd_t` per assembly segment
  (defaults `Cd_n = 1.2`, overridable; Zajac measured 1.11 smooth PE,
  1.55 jute-served; `Cd_t` default small, ~0.01, often negligible).
- Current profile: uniform vector, or piecewise-linear speed/direction vs
  depth (table input).
- Bodies (repeaters/BUs) get a lumped drag `0.5*rho*Cd*A*|u|u` with an
  input drag area `Cd*A`.

### 3.4 Steady-state lay (Mode B — Zajac)
In the ship frame the laying configuration is stationary: apparent uniform
flow `-V_ship` (plus real current, giving 3D cross-current deflection) and
material pay-out `V_c` along the local tangent contribute to `u_rel`:
```
v_cable(node) = V_c * t(node)      # transport along the stationary shape
```
plus the centrifugal correction `T_eff = T - rho_c * V_c^2` in the force
balance. The DR engine converges to the stationary configuration directly;
outputs: exit angle, layback, TDP position, suspended length, tension
distribution, bottom tension from slack bookkeeping.

Cross-checks (automated tests): flat bed + no bottom tension must
reproduce Zajac's straight line at the critical angle
`cos(alpha) = sqrt(1 + (H/V)^4/4) - (H/V)^2/2`, `H = sqrt(2w/(Cd_n rho d))`;
`T_ship = T_0 + w*h` with `Cd_t = 0`; slack ↔ bottom tension trend per
Zajac eq. (24)–(25); cross-current lateral touchdown offset vs Zajac's
closed-form perturbation (eq. 50–52, worked example: 1 kn current over the
top 600 ft of 6,000 ft at 6 kn → 253 ft lateral offset). A closed-form
"quick answer" panel shows the Zajac numbers next to the numerical solve —
agreement is itself a live diagnostic.

Slack/route advisories from the same theory (cheap, high value):
required pay-out over slopes `V_c - V = H*beta/2` (speed-independent),
the **free-span suspension criterion `V < H/gamma`** flagged along a
profiled route, and snap-tension estimates on pay-out stoppage
`T_p = sqrt(EA*rho_c) * dV` (needs an EA input; the drape machinery
already anticipates finite EA). H is also exposed as a per-cable
calibration input (measurable at sea from the stern angle: `alpha = H/V`).

### 3.5 Quasi-static operation simulation (Mode C)
A **timeline** of operator actions, solved as a sequence of static
equilibria, each warm-started from the last (DR excels at this):
- vessel track: waypoints or heading/speed steps; chute/sheave position
  derived from vessel position + offsets;
- pay-out / haul-in schedule: rest-length growth/shrink at the top
  segment, node insertion/removal at ~2x/0.5x nominal spacing;
- lowering line: a separate chain (rope properties) attached to a cable
  node or body, with its own pay-out;
- step drag: node velocity approximated as `dX/dt` between steps enters
  the drag term, so lowering speed matters (faster lowering → more trail).
  Added mass/inertia neglected — documented as the quasi-static limit,
  valid for the slow speeds of BU/bight work.

Friction lay-history dependence is a *feature* here: stepping the vessel
naturally produces the path-dependent laid geometry (V2's caveat about
non-unique friction equilibria becomes the simulation's core capability).

**Topology generalization**: the engine works on a set of chains + shared
junction nodes:
- *BU deployment*: three chains (trunk, leg 1, leg 2) meeting at the BU
  junction node carrying the BU weight/buoyancy/drag. Initial state: legs
  laid on the bed along given azimuths, trunk up to the ship. Timeline
  steps the ship and pays out trunk; outputs per-leg tension, BU depth/
  attitude proxy, touchdown positions, leg uplift checks.
- *Final bight*: one chain, both ends anchored on the bed (the two laid
  ends), apex held by a lowering line from the vessel; vessel steps along
  the planned bight centreline paying out the line until touchdown tension
  reaches ~0 and the bight rests on the bed. Outputs: laid bight shape, min
  bend radius through the evolution, peak tensions, seabed footprint.

### 3.6 Validation & honesty plan
- Regression: 3D engine with all loads in one vertical plane must
  reproduce `drape_solver.py` and the closed-form catenary within the same
  tolerances as V2's tests.
- Zajac steady-lay identities (§3.4).
- JMSE shallow-water fixtures (93 m depth table in the digest): layback,
  exit angle, MBR for H = 1200/2000/4000 kg with in-air segment.
- BU junction: static three-leg vector equilibrium vs hand calculation;
  symmetric bight vs two mirrored half-catenaries; held-bight tension rise
  vs Zajac Appendix E (catenary matched to a stationary lay configuration —
  the closest published analysis to the final-bight problem).
- Quasi-static validity: Zajac shows stepping is justified when manoeuvre
  time >> 2h/c1 (~20 s in deep water; c1 = sqrt(EA/rho_c)) — documented as
  the mode's validity bound.
- New `V3_MODEL_NOTES.md` in the same style as MODEL_NOTES.md: what each
  mode computes, assumptions, what it must not be used for, validation
  status (including "not verified against OrcaFlex/MakaiLay or field data"
  until that comparison exists).

## 4. 3D viewport (no new dependencies)

**Decision to confirm**: software-projected renderer (recommended) vs
re-vendoring `pyqtgraph.opengl`.

- The bundled pyqtgraph was vendored *without* its `opengl` subpackage.
  Restoring it is not a new dependency, but it imports **PyOpenGL**, which
  ships with QGIS on Windows (verified: PyOpenGL 3.1.5 in QGIS 3.30) but is
  not guaranteed on Linux/macOS builds, and GL is unreliable over
  RDP/VMs — common on vessels.
- **Recommendation**: a self-contained software-projected 3D view
  (~500–700 lines): NumPy rotation/projection matrices → one batched
  polyline/mesh draw on a QGraphicsView/pyqtgraph canvas per frame.
  Turntable orbit (drag), zoom (wheel), pan (middle-drag), depth
  exaggeration slider. Scene content is small (cable polylines ≤ ~2,000
  segments, seabed grid ~100×100 wireframe/shaded quads, vessel glyph,
  markers) — trivially interactive in NumPy. Painter-sort the seabed quads;
  draw cable last. Deterministic everywhere, one code path to test.
- The scene is described backend-agnostically (polylines, meshes, glyphs,
  colors-by-tension), so a GL backend can be added later without touching
  callers.
- Keep synchronized 2D **profile** (along-route) and **plan** views using
  the existing matplotlib-shim plot widget — engineers read numbers off 2D;
  3D is for insight and QA. Hover readout (tension/depth/KP/clearance)
  works in all three views from one shared geometry cache.

## 5. QGIS integration (the differentiator)

- **Bathymetry from the project**: pick a DEM/bathy raster layer + a route
  (line layer or drawn) → sample a grid corridor around the route for the
  engine. Manual flat/slope/profile/CSV entry remains (V2 parity, and for
  offline use).
- **Results back to the map**: export TDP track, laid geometry, BU/leg
  footprints as memory layers or GeoPackage; simulation timeline exportable
  as CSV (time, ship position, tensions, TDP KP).
- Engine stays QGIS-free; a thin `qgis_adapters.py` does all sampling and
  layer I/O.

## 6. Architecture

```
catenary/
  v3/
    __init__.py
    engine/                # pure Python + NumPy, no Qt/QGIS imports
      cable_system.py      # chains, junctions, bodies, assembly mapping
      bathymetry.py        # flat/slope/profile/grid surfaces, gradients
      hydrodynamics.py     # drag laws, current profiles, Zajac closed forms
      solver3d.py          # DR core (static + warm-started stepping)
      steady_lay.py        # ship-frame steady lay mode + closed-form checks
      timeline.py          # operation script, pay-out bookkeeping, stepping
      results.py           # typed result/diagnostics dataclasses
    ui/
      dialog.py            # thin shell: assembles panels, owns nothing else
      sections/…           # one widget per input section; declarative
                           #   widget→QSettings registry (kills the ~200-line
                           #   save/restore boilerplate)
      solve_controller.py  # config → engine → results; runs in a QgsTask /
                           #   worker thread (V2 blocks the UI thread)
      view3d.py            # software-projected viewport
      views2d.py           # profile/plan via existing plot shim
      results_panel.py     # HTML report + warnings, as V2
      exporters.py         # DXF/SVG/CSV/GeoPackage
    V3_MODEL_NOTES.md
```

Lessons applied from the V2 review: no 4,300-line monolith; a single typed
`DisplayGeometry` contract instead of a duck-typed SimpleNamespace; the
assembly model (table ↔ JSON) extracted once and **shared with V2**; the
arc-length→segment mapping utility written once; enum compat routed through
`qgis_compat`; solver helpers made public instead of the dialog reaching
into `_private` methods.

## 7. UI sketch

- Left panel (collapsible sections, V2 conventions):
  1. **Mode** — Static hang | Steady lay | Operation sim.
  2. **Geometry/Environment** — water depth/bathymetry source, current
     profile table, water density.
  3. **Assembly** — same table + JSON as V2 (shared widget), plus new
     per-segment `d`, `Cd_n`, `Cd_t` columns; bodies gain drag area.
  4. **Vessel** — chute height/radius, chute friction mu, (steady lay:)
     ship speed, pay-out/slack %; (op sim:) track waypoints + speeds,
     lowering line properties.
  5. **Scenario** (op sim only) — preset picker: *Straight lay*, *BU
     deployment*, *Final bight*, each pre-filling the timeline; editable
     step table.
  6. **Solver** — node count, tolerance, defaults for EI/MBR (as V2).
- Right panel: tabbed **3D / Profile / Plan** views, timeline scrubber +
  play button under them (op sim), results HTML below, warnings banner,
  hover readout, exports.
- Solves run in a background task with progress; the scrubber replays
  cached step results instantly.

## 8. Phasing (each phase shippable)

- **Phase 0 — housekeeping**: retire V1 UI (action/dialog; keep
  `simple_catenary.py` as a test oracle), extract the shared assembly
  model + DXF exporter from the V2 dialog, add `catenary/v3/REFERENCE_DIGEST.md`
  (done). Version bump + changelog.
- **Phase 1 — engine core**: `solver3d` + `bathymetry` + `cable_system`;
  tests: 2D-plane regression vs `drape_solver`, closed-form catenary,
  contact/friction parity, 3D slope/side-slope cases.
- **Phase 2 — drag + steady lay**: `hydrodynamics` + `steady_lay`; Zajac
  and JMSE validation tests; closed-form quick-answer helpers.
- **Phase 3 — V3 dialog v1**: static + steady-lay modes, 3D/profile/plan
  views, background solve, results/warnings, exports, QGIS bathymetry
  sampling. First public release of V3 (beta flag in the tool name).
- **Phase 4 — operation sim**: `timeline` + stepping + scrubber; straight
  lay + **BU deployment** scenario; validation set.
- **Phase 5 — final bight** scenario + polish: presets, docs
  (`V3_MODEL_NOTES.md`), README/changelog, release. Deprecation notice on
  V2? (No — V2 remains the fast 2D tool; V3 complements it.)

## 9. Open questions (for discussion)

1. **Naming**: "Catenary Calculator V3" vs something like "Cable Lay
   Simulator (3D)" — recommendation: *Cable Lay Simulator (3D)*, since it
   goes well beyond a calculator; keep `v3` in module paths.
2. **V1 removal**: remove the toolbar action/dialog in the next release
   (it has carried a removal notice since 1.6)? Recommendation: yes,
   Phase 0.
3. **Renderer**: confirm software-projected 3D (recommended) over
   re-vendoring `pyqtgraph.opengl` + PyOpenGL detection.
4. **V2 relationship**: V3 as a separate tool alongside V2 (recommended —
   V2 stays the quick, mature 2D answer; V3 the 3D/dynamic tool) vs
   eventually folding V2 into V3.
5. **Vessel step drag**: is rate-dependent drag during stepping (quasi-
   static velocity estimate) wanted in the first op-sim release, or start
   with rate-independent equilibria and add rate effects after?
