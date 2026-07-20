# Cable Lay Simulator (3D) — Model Notes, Assumptions and Limitations

This document states precisely what the Cable Lay Simulator (catenary V3)
computes, the assumptions behind each mode, and what it must **not** be used
for. It is written for installation engineers reviewing whether the tool is
appropriate for a given task. Companion documents:
`catenary/MODEL_NOTES.md` (the 2D V2 calculators),
`catenary/V3_PLAN.md` (design) and `catenary/v3/REFERENCE_DIGEST.md` (the equations
and constants distilled from the reference papers, with sources).

> **Status / validation.** The engine is validated against closed-form
> solutions (uniform catenary; Zajac 1957 straight-line critical angle,
> top-tension theorem and cross-current behaviour), against the proven V2
> multi-span drape solver on identical 2D problems, and against physical
> invariants (global drag force balance, junction equilibrium, monotonic BU
> descent, bight settle) by the automated suites
> `tests/test_v3_solver3d.py`, `tests/test_v3_steady_lay.py` and
> `tests/test_v3_timeline.py`. It has **not** been verified against
> commercial lay-simulation software (OrcaFlex, MakaiLay) or measured field
> data. Treat results as planning-grade estimates, not engineering sign-off
> values. The tool is released as a **beta**.

---

## Common scope (all modes)

* Frame: `x, y` horizontal metres (local frame; the vessel starts at the
  origin), `z` vertical, 0 at the sea surface, negative down.
* **Quasi-static**: every displayed state is a static equilibrium. Cable
  inertia, wave loading, vessel motions (heave/surge) and VIV are **not
  modelled**. Rate effects enter only through drag.
* Cable model: perfectly flexible (EI = 0) tension-only chain in the
  inextensible limit (residual numerical stretch < 0.002 %); optional
  per-segment EI as discrete three-node moments (same formulation and the
  same honesty caveats as V2 — the bending boundary layer
  `lambda = sqrt(EI/T)` is usually below the node spacing for telecom
  cable, where the flexible limit is also the physically correct one).
* Weight: per-segment submerged weight below `z = 0`, in-air weight above
  (blank in-air weight = submerged value). Negative weights are distributed
  buoyancy. Bodies are point loads (negative = buoyant) with optional
  lumped drag area Cd·A.
* **Hydrodynamic drag** (the headline addition over V2): Morison-type
  quadratic drag per unit length, decomposed about the local tangent:
  `f_n = 0.5*rho*Cd_n*d*|u_n|*u_n`, `f_t = 0.5*rho*Cd_t*pi*d*|u_t|*u_t`,
  with `u_rel = u_water(z) - v_cable`. Current is a piecewise-linear
  profile of speed/direction vs depth. Typical `Cd_n`: ~1.1 (smooth PE)
  to ~1.55 (rough served cable) — Zajac's measured values; enter supplier
  data when you have it. `Cd_t` is small and often negligible for shape,
  noticeable for tension at high pay-out rates.
* Seabed: unilateral penalty contact on `z_bed(x, y)` (flat, planar slope,
  extruded profile, or a grid sampled from a QGIS raster) with Coulomb
  stick-slip friction in the tangent plane (per-segment mu). **Equilibria
  with friction are non-unique (lay-history dependent)** — in the
  operation simulator that history is simulated explicitly, which is the
  point; in the static mode the returned state is one admissible
  equilibrium.
* Units: SI internally; tensions displayed in kN, speeds in knots where
  labelled, angles in degrees.

## Mode 1 — Static hang

Three configurations share this mode:

**Single cable span** — 3D generalisation of the V2 calculation: the
suspended span is first placed with the exact 2D catenary (the chosen
solve-mode input: bottom tension, top tension, exit angle, layback or
suspended length), then the full 3D lumped-node system (span + on-bed
tail, anchored) is relaxed to equilibrium over the real bathymetry with
current drag.

**Branching unit (held)** — the static equilibrium of a BU suspended at a
chosen hold depth: trunk from the chute to the BU junction (length = hold
distance plus the trunk-slack input), both legs pre-laid along their
azimuths with anchored far ends. Outputs trunk/leg tensions, the achieved
BU position (the legs pull the BU off the nominal hold point — the
achieved depth is reported, not assumed) and clearance to the bed. Uses
exactly the same geometry inputs and solver as the deployment scenario,
so a static check at any hold depth is consistent with the time-stepped
lowering.

**Final bight (held)** — the static equilibrium of a bight held at a
chosen apex depth on the lowering rope (rope length derived from the hold
depth, clamped to what the bight length can physically reach). Outputs
the hook load, rope tension at the vessel, achieved apex depth/clearance
and the cable tensions. Note the flexible model produces a tight kink at
the sling point — set an MBR limit to have it flagged; a real bight would
be rigged with a quadrant or spreader, which is not modelled.

Static-hold caveats: friction equilibria remain history dependent (the
hold state is reached from the nominal initial geometry, not from a
simulated lay); for the sequence of states during an actual lowering, use
the operation simulation — the two share one engine and one set of
inputs, so results are directly comparable.

* With no current and a flat bed this reproduces the V2 result (validated
  ~1 % on top tension, ~2.5 % on TDP position).
* With current, `H = const` no longer holds (this was V2's documented
  limitation); the drag-loaded 3D equilibrium is solved directly.
* The anchor placement comes from the no-current catenary estimate; on
  extreme bathymetry combined with strong current, review the on-bed tail
  length input.

## Mode 2 — Steady lay (Zajac's stationary model)

The stationary configuration of a cable laid at constant ship speed and
pay-out rate, solved in the vessel frame by RK4 integration of the 3D
force-balance ODEs from the touchdown point to the chute, including the
material-transport (centrifugal) correction `T - rho_c*Vc^2` and the
apparent flow `current - V_ship + Vc*t`.

* Touchdown condition per Zajac: tangential departure with bottom tension
  `T0 > 0`, or the straight-line critical-angle landing in the `T0 = 0`
  limit (handled analytically — the transverse ODE is singular below
  `T = rho_c*Vc^2`).
* Validated: critical angle matches `cos(a) = sqrt(1 + (H/V)^4/4) -
  (H/V)^2/2` to ~1e-9; `T_ship = T0 + w*h` with `Cd_t = 0`; the exact
  catenary at `V = 0`; cross-current lateral touchdown offset (the cable
  lands downstream of the ship track).
* The results pane shows **Zajac closed-form quick answers** next to the
  numeric solve (hydrodynamic constant H, critical angle, straight-line
  layback, slope pay-out increments, the suspension-criterion speed limit,
  and the `w*h` theorem) — disagreement between the two is itself a
  diagnostic.
* Bottom tension is an *input class*, not a slack computation: steady
  positive slack corresponds to `T0 ~ 0`; sustained negative slack is a
  transient (tension climbing with time, Zajac eq. 25) that this
  stationary model does not represent.
* The exit angle is a poorly conditioned control variable for fast lays of
  light cable (it stays near the critical angle regardless of `T0`) — the
  solver warns when a target cannot be met crisply.

## Mode 3 — Operation simulation

Scripted vessel moves and pay-out solved as a sequence of warm-started 3D
equilibria (quasi-static stepping). Rate-dependent drag is included by an
inner iteration: each step is solved, node velocities are estimated from
material-consistent displacements over the step, and the step is re-solved
with those velocities in the drag term.

Validity: manoeuvre times must be long against the longitudinal wave
round-trip `2h/c1` (~20 s in deep water; `c1 = sqrt(EA/rho_c)`), which is
the regime of BU deployments and bight lay-downs (Zajac Sec. IV). Snap
loads from brake seizure or heave are **not** in the model — the Zajac
impedance estimate `T_p = sqrt(EA*rho_c) * dV` in the digest gives the
order of magnitude if you need it.

Scenarios:

* **Straight lay (transient)** — vessel steams and pays out against an
  anchored far end. Validated to satisfy the top-tension theorem at every
  step and to approach the steady-lay ODE after ~10 depths of advance
  (residual bottom tension from the start-up is real lay-history physics
  and decays slowly through friction).
* **Branching-unit deployment** — Y-topology: trunk chain from the chute
  to a junction node carrying the BU weight/drag area, two pre-laid legs
  from the junction to anchored far ends on the bed. The vessel steams
  ahead paying out trunk; outputs per-leg tensions, BU descent path and
  touchdown. Validated: monotonic descent, landing on the bed, junction
  force balance (implied by convergence), leg symmetry.
* **Final bight lay-down** — the joined cable runs from laid end A up to a
  bight apex held by a lowering rope at the vessel and back to laid end B;
  the vessel steps (default perpendicular to the A–B axis) paying out
  rope. The rope is **auto-released when the hook load** (tension at its
  attached end) falls below the threshold; the bight then settles on the
  bed. Validated: apex descent, auto-release, > 90 % of the bight in
  contact with low residual tension afterwards.
* **BU deployment — full (two-sheave)** — the complete integration from
  the jointing set-up: both pre-laid legs held over the **port and
  starboard sheaves** (offsets rotate with the heading), joints paid
  overboard, one leg **transferred** to the other sheave (its top is
  walked between the sheave positions across the phase, so the solver
  never sees a step change), the BU **overboarded** as a discrete event
  (junction + trunk spawned, legs re-topped onto the junction), then
  lowered and laid ahead. A proportional **tension-balance controller**
  redistributes the scheduled leg payout each substep so the sheave
  tensions stay matched (a secant trim on the deployed lengths provides
  the initial balance), and a **schedule optimiser** places the whole
  set-up so the BU lands on a target position using preview-quality runs
  (translation of the operation; limits checked and reported as
  warnings). The edited/optimised phase schedule is exportable as an
  operational CSV (time / vessel position / payout per line / tensions).
  Modelling caveats: the BU is a point body (no attitude/rotation), the
  in-air weight during the splash transit is not modelled (the junction
  spawns just below the surface at its submerged weight), and deck
  handling is length bookkeeping only — legs enter the model at the
  sheave. Validated: sheave-frame geometry, symmetric-hold balance,
  monotonic descent and landing, per-chain length conservation against
  the applied payout integral, controller effectiveness on a cross-slope
  bed, optimiser landing accuracy on a sloping bed.

No open-access reference covers BU deployment or final-bight procedure
directly (see the digest); these simulations rest on the validated solver
physics plus limiting-case checks. Zajac's Appendix E (bight held while
the ship steams on) is the closest published analysis and matches the
qualitative behaviour.

### Model quality selector (operation runs)

* **Full** — the dynamic-relaxation solver at normal settings (the
  engineering answer).
* **Draft** — the same solver with a coarser mesh, looser tolerance and
  larger substeps (~5–10× faster); for iterating on a schedule.
* **Quick (BU scenarios only)** — the analytic tri-catenary model
  (`engine/quick_bu.py`): every suspended line is a closed-form planar
  catenary (taut lines become stiff axial constraints; touchdown is a
  tangency condition) and the BU position is the root of the three-line
  force balance, so a whole deployment solves in under a second.
  Assumptions dropped vs the full solver: seabed friction and lay
  history, hydrodynamic drag/current, in-line bodies, multi-segment
  weight variation (a length-weighted mean is used), and bed relief under
  a suspended span (touchdown depth only). Validated against the full
  solver on a held-BU case (position within metres, trunk tension within
  ~10 %); always confirm a final schedule at Full quality. The quick
  run's shapes seed a later full run, so confirming is fast.

## What is supported, partially supported, and not supported

| Capability | Status | Notes |
|---|---|---|
| 3D static suspension over real bathymetry | **Supported** | DR solver; validated vs closed form and V2 drape. |
| Current / hydrodynamic drag | **Supported** | Morison-type; piecewise-linear current profile; validated force balance and Zajac limits. |
| Steady-state lay with ship speed & pay-out | **Supported** | Vessel-frame ODE; Zajac-validated. |
| Cross-currents (3D lay plane deflection) | **Supported** | Lateral touchdown offset reported. |
| Multi-segment assemblies + bodies | **Supported** | V2-compatible JSON, plus diameter/Cd columns. |
| Seabed friction | **Supported** | Coulomb stick-slip, per segment; history-dependent by nature. |
| Branching units (Y-topology) | **Supported (beta)** | Junction node + three chains; no BU attitude/rotation model (point body). |
| Full two-sheave BU deployment | **Supported (beta)** | Phase schedule with transfer/overboard events, payout balance controller, landing-target optimiser; point-body BU, no splash-transit air weight. |
| Final bight lay-down | **Supported (beta)** | Apex held by one lowering line; splice joint not structurally modelled. |
| Bending stiffness / MBR | **Partial** | Same discrete-moment model and caveats as V2; MBR limit check with warning banner. |
| Chute/capstan friction | **Partial** | Capstan factor applied to the reported machinery tension; the chute arc geometry itself is not modelled (V2 models the quarter-circle wrap in 2D). |
| Axial elasticity | **Not supported** | Inextensible limit (numerical EA is an implementation device, not the cable's). |
| Waves, vessel motions, snap loads, VIV | **Not supported** | Quasi-static only. |
| Torsion, loop/kink formation | **Not supported** | Zajac's warning about stopping in deep water stands — the tool cannot predict kinking. |
| Cable burial, plough forces | **Not supported** | See the Processing importers for as-laid/plough data. |

## Practical guidance

* **Drag inputs matter**: enter the real cable diameter; with `d = 0` a
  segment gets no drag. `H = sqrt(2w/(Cd*rho*d))` is shown in the quick
  answers — compare it with the stern-angle estimate `alpha ~ H/V` from
  your own lay records to calibrate `Cd_n` per cable.
* **Chute friction**: mis-estimating capstan mu flips between TDP
  compression and 3x overtension in deep water (JMSE 2020 case study, see
  digest) — the machinery tension line makes the assumed mu explicit.
* Node spacing (`target ds`) trades accuracy for speed; contact and
  touchdown positions resolve to about one node spacing. Operation-mode
  runtime is roughly seconds per simulated minute at default settings.
* **Solver performance**: cold settles seed from analytic catenary /
  bed-following shapes and run a coarse-mesh pre-pass before the full-
  resolution solve; grid bathymetry gradients come from precomputed
  half-step tables that reproduce the previous central-difference field
  exactly. Because friction equilibria are lay-history dependent, the
  analytic seeds select the branch closer to a gently-laid cable (lower
  trapped residual bottom tension) — physics anchor tests (Zajac
  top-tension theorem, catenary limits) pin the behaviour, and
  `tests/bench_v3_solver.py --compare` gates every solver change against
  a result fingerprint baseline.
* The 2D V2 calculator remains the fast, mature tool for plane static
  problems (chute wrap geometry, surface-piercing buoyancy analysis,
  detailed multi-span drape reporting); V3 complements it with drag, 3D
  and operations.
