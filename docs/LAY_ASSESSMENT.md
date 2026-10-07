# Lay Assessment (Cable Lay Data Explorer)

Where might the laid cable be suspended, where might it have formed loops, and did tension stay
within the cable's limits? The Explorer's **Lay Assessment** tab answers these from imported lay data
and lists the answers as KP ranges. Select a range to highlight it on the map and in the **Seabed
Profile** dock. That dock shows where the cable is modelled to rest relative to the seabed, with a
red/amber/green status bar and two-click measurements.

The results are screening indicators built on the lay software's model outputs. They show where to
look (for example, in a post-lay survey); they do not prove a suspension or a loop.

## Inputs

**Lay data.** The checks work best on the *3D model solutions* layer (*Import 3D Model Solutions*),
which carries touchdown (TD) KP, position and depth, bottom tension, bottom slack, layback, and
measured and calculated top tension. They also run on any layer with enough of these columns, for
example the cable lay (telegsum) log for top tension and payout checks. The tab detects each column
from its name (for example `TD KP`, `Bot.Tension`, `Inst.Bot.Slack`, `Meas.Top Tension`,
`Calc.Top Tension`, `TD Depth`, `Layback`, `Ship Speed`, `Payout Speed`, `Solution Valid`). Check the
*Columns* box and correct any column it picked wrongly.

- **KP (touchdown)** is required. Rows without a KP are left out, as are rows whose *Solution valid*
  flag reads 0, no, false or invalid.
- **Tension units** are set once for all tension columns (kN, tonne-force, kgf or lbf).
- **Bottom slack** is the seabed slack in %, where 0 means the cable exactly follows the seabed
  profile. This is the MakaiLay convention: when computed slack reaches zero the model reports
  bottom tension instead.
- **Bottom tension** in MakaiLay data is a *model* output, not a measurement. Top tension is the
  measured quantity. The *Top tension consistency* check compares the two.

### Cable library

Weights and limits come from the **cable type library** (*Subsea Cable Tools ▸ Cable Type Library…*,
or *Cable library…* on the tab). This is a GeoPackage you create and keep; the plugin ships no cable
data. The library can sit on a shared drive and serve several projects and users. Saving checks that
nobody else changed it since you opened it, and asks before overwriting. Each type has:

- name, category (cable or rope), manufacturer, generic type (LW, LWP, SA, DA, RA …) and aliases;
- diameter, weight in air and weight in water;
- CBL, NTTS, NOTS, NPTS, MBR and bending stiffness EI.

The tension limits follow ITU-T G.972, as summarised in ITU-T G.978 §5.3.3:

| Limit | Meaning | Checked against |
|---|---|---|
| CBL | Cable breaking load (qualification test) | context only |
| NTTS | Transient: could be met accidentally, particularly during recovery | cable tension in the water (red) |
| NOTS | Operating: could be met during repairs (holding for jointing) | cable tension in the water (amber) |
| NPTS | Permanent: the cable's state after laying | bottom / residual tension (red) |

### Cable types along the route

*Cable types from* sets where each KP's cable type label comes from:

- **Cable type column in the lay data**: the label logged at touchdown.
- **Workbench RPL**: the `CableType` of each RPL leg.
- **Workbench assembly fitted to an RPL**: each section's cable type, at the KPs the fit lands it on
  (through the RPL's per-segment slack).

The Workbench sources need the lay data's touchdown KPs to be quoted on that RPL.

Labels such as "LW" are generic, but a library may hold several LW cables from different
manufacturers. The *Library type (this project)* table lists every label found and the type it means
in this project. The mapping is saved in the QGIS project, so the shared library stays generic. A label
resolves in this order:

1. this project's mapping;
2. a library name equal to the label;
3. the one type listing it as an alias;
4. the one type with that generic type.

A label that several types could mean is reported as *ambiguous* and must be mapped. A label that
matches nothing uses the *Default cable type*, and the status line says so. Labels compare as
upper-case letters and digits, so `LW-P` matches `lwp`.

**The cable in the water.** The cable hanging between the sheave and touchdown is not the touchdown
cable during a transition. The cable `s` metres above touchdown lands later at
`KP + direction · s / (1 + slack)`. The type there comes from the same makeup. The hanging length
comes from:

1. the layback, `√(layback² + (depth + sheave height)²)`, when logged;
2. otherwise the catenary's vertical balance, `√(T_top² − T_bottom²) / w`;
3. otherwise the depth.

Where a type change hangs in the water, the cable is sampled along its length:

- **Tension.** The tension at each height runs down from the measured top tension, losing `w · dz`
  per metre of depth (depth is proportional to cable length for the straight cable of slack lay).
- **Limit checks.** The type with the highest tension / NOTS governs the top-tension limit check. For
  example, an LW cable just below a joint to heavier armoured cable can be over its NOTS while the
  armoured cable at the sheave is not.
- **Weight.** The top-tension identity below uses the mean weight of the hanging mix.

### Seabed source

For the seabed checks:

- *Lay model touchdown depth*: the lay model's own `TD Depth`, binned every *Sample every* metres of
  KP. Nothing else is needed, but it only sees seabed relief at the lay model's resolution, so the
  length check (which compares against independent bathymetry) does not run on it.
- *Raster bathymetry*: an MBES grid. Grid XYZ soundings first with *Create Raster from XYZ*.
- *Depth contours*: a line layer and its depth field, used at its crossings with the track.

Rasters and contours are sampled along the **touchdown track** with the Depth Profile engine, using
the same datum handling, coverage rules and finest-raster-first order (see
[KP and bathymetry methods](KP_AND_BATHYMETRY.md)). The track is built from the records' TD
positions, ordered by KP and binned every 2 m, so stops and jitter do not zig-zag it. Plot positions
are mapped back to the data's TD KP. *Seabed KP range* limits the seabed checks to a window, which is
useful on long routes. All seabed work runs in a background task.

## Checks

Each record check flags records. Records flagged in a row (in time order) form a range, and ranges
closer than *Merge ranges closer than* join. A range takes the worst level and worst value of its
records. A check that lacks its inputs is skipped, and the status line says why.

| Check | Flags | Level |
|---|---|---|
| Laid under bottom tension | bottom tension above a threshold (default 0.5 kN), i.e. no bottom slack | amber |
| Bottom tension limits | bottom tension above NPTS; optionally above your own target | red / amber |
| Top tension limits | the governing cable in the water above its NOTS / NTTS (see above) | amber / red |
| Top tension consistency | measured vs calculated top tension; without a calculated column, measured top tension − w·d (below) vs logged bottom tension | amber |
| Loop risk (excess slack) | bottom slack ≥ threshold (default 8%) at near-zero bottom tension (≤ 0.1 kN), optionally only shallower than a depth | amber |
| Payout while stopped | ship speed ≤ threshold while payout speed ≥ threshold (same units) | amber |
| Touchdown moving back | in time order, TD KP falls back over ground already laid by ≥ 5 m | amber |
| Slack off plan | bottom slack differs from planned bottom slack by more than a tolerance | info |
| Suspensions (seabed model) | modelled spans clear of the seabed (below) | amber; red when higher or longer than set |
| Cable length vs seabed (friction) | laid cable short of the seabed within friction reach (pulled taut); surplus at zero tension (loops) | amber; red above NPTS or impossible |

All thresholds are editable defaults, not standards. Set them for the cable and the project.

### Top tension and bottom tension

In steady lay, the tension change between touchdown and the sheave equals the cable's submerged
weight times the water depth, plus its in-air weight times the sheave height (Zajac, 1957). This
identity is exact when tangential drag is neglected:

```
T_top = T_bottom + w_water · d + w_air · h_sheave
```

So `T_bottom ≈ T_top(measured) − w_water · d_TD − w_air · h`. Here `w_water` is the mean weight of
the hanging cable. The Seabed Profile plots this estimate (dashed) beside the logged model bottom
tension. A persistent difference suggests one of:

- the cable weight used by the lay model differs from the cable's real weight;
- the dynamometer has an offset;
- drag or current effects.

In each case the model's bottom tension and slack are less trustworthy there.

### Seabed model: where the cable rests

Cable resting on the seabed under horizontal tension `H`, with submerged weight `w` per metre,
satisfies (small slopes):

```
H · y'' = w − p,      p ≥ 0 (seabed contact pressure),      y ≥ seabed
```

The cable therefore lies on the *least* curve above the seabed whose curvature never exceeds
`c = w / H`. A free span is a parabola of that curvature, with mid-span sag `w L² / 8H`. Wherever the
seabed's hollows are sharper than `c`, the cable bridges them. Writing `Φ'' = c`, the curve `y − Φ`
is the upper concave hull of `seabed − Φ`. This is a classic obstacle problem, solved exactly in one
O(n) pass, so 100 km at 1 m resolution takes about a second.

- **Smoothing.** The seabed is first averaged over the cable's **conformity length**,
  `2 (EI / w)^(1/3)`, the bending length of a cable lying under its own weight. A cable cannot follow
  shorter features, and sounding noise on them would otherwise add seabed length: 0.2 m of noise at
  1 m spacing adds over 2%. The length comes from the library's bending stiffness, is 10 m when EI is
  unknown, and can be set (*Seabed smoothing*, 0 = none).
- **Tension.** `H` at each station is the highest logged bottom tension of the records depositing
  cable there, raised where the length check finds the cable short (below). `w` is the lightest cable
  there. Both choices are conservative: they give more spans.
- **Conforming cable.** Stations below *Cable conforms below tension* (default 0.05 kN) lie on the
  seabed and pin the cable.
- **Reporting.** Spans are reported when higher than *Report spans higher than* (default 0.3 m)
  **and** longer than *…and longer than* (default 5 m). They are red when higher than 1 m or longer
  than 50 m. Each says whether its tension came from the lay model or from the cable being short.
- **Gaps.** Gaps in the bathymetry are never bridged.

### Cable length vs seabed: friction keeps it local

Comparing laid cable with seabed length over a whole route, or over an arbitrary window, is too
idealised. Spare cable in one place cannot fill a hollow somewhere else, because the seabed holds it.
A cable on the seabed only slides where its tension changes by more than the friction `μ · w` per
metre. So a tension `H` can draw cable from at most the **friction reach** `ℓ = H / (μ · w)` either
side:

```
cable laid   = Σ (1 + slack/100) · √(Δx² + Δd_TD²)      records: slack is over the lay model's seabed
seabed       = Σ √(Δx² + Δd²)                           smoothed sampled seabed
balance %    = (cable laid − seabed) / plan · 100        within x ± ℓ(H)
```

At near-zero tension the reach shrinks to the conformity length, so the comparison is local. Where
the balance is below *Flag shortfall above* (default 0.5%), the laid cable is short of the seabed and
must be pulled taut. The model then finds the tension at which the cable's rest shape uses exactly
the cable laid within its reach. The window grows with the tension, so this is solved by bisection.
The cable then rests at the higher of that tension and the logged one, and the suspension model draws
its spans. Stretches whose friction windows overlap are reported as one.

The finding is red when that tension exceeds NPTS, or when the cable could not cover the seabed even
fully taut, which points to a data problem. A surplus above *Flag surplus above* (default 8%) at zero
tension is flagged as loop / snaking risk.

The seabed friction coefficient `μ` (axial, default 0.5) depends on the soil and the cable's outer
serving. A **higher** `μ` keeps cable more local, which is conservative for suspensions. *Longest
friction reach* (default 1000 m) caps the window.

The Seabed Profile's lowest strip plots this balance (red below zero), and the tension strip shows
where the cable is pulled taut above the logged tension.

### Validation

On a synthetic two-ridge seabed, the model was compared with the plugin's drape solver (dynamic
relaxation over a profiled seabed, frictionless, no bending stiffness):

| Case | Spans (drape / this model) | Max gap (drape / this model) | RMS shape difference | Tension from cable length (this model / drape) |
|---|---|---|---|---|
| ~1 kN | 4 / 4 | 0.83 / 0.85 m | 4 mm | 0.967 / 0.976 kN |
| ~8.5 kN | 5 / 5 | 3.25 / 3.35 m | 7 cm | 7.74 / 8.51 kN* |

\* With the drape solver's numerically stretched cable length. That solver stretches its cable by
about 0.02% at this tension, and with the unstretched length this model gives 10.4 kN.

- **Span positions and heights are robust.** They agree to within a metre or two, and gaps run about
  3% high because the parabola slightly overestimates the exact catenary.
- **Tension from cable length is very sensitive.** About 0.02% of cable length moves it by about 20%.
  The tension the length check finds therefore says how hard the cable is pulled taut; it is not a
  design value. The same sensitivity applies to errors in the bathymetry and the logged slack.

The unit tests check:
- the mid-span sag against `w L² / 8H`;
- that friction keeps slack local: a 2 km lay whose first half has spare cable but whose second half
  has none is flagged in the second half, although the route as a whole has more cable than seabed;
- the tension at a joint hanging in the water.

## Results

- **Table:** level, check, KP from / to, length, worst value and a message. Click a row to highlight
  the range along the touchdown track on the map and zoom the Seabed Profile to it. Double-click to
  zoom the map and select the range's records in the table, map and plots.
- **Seabed Profile:**
  - a status bar (green clear, amber, red, blue info, grey no data);
  - the sampled and smoothed seabed, the modelled cable with spans filled red, and the lay model's
    touchdown depths;
  - bottom tension: logged, estimated from top tension, where the cable is pulled taut, and NPTS;
  - the cable-vs-seabed balance.

  Hovering moves the shared crosshair. Click or double-click a point to go to the nearest record.
  *Measure* gives two-click length / height / angle / along-seabed measurements, snapped to the
  seabed or the cable.
- **Add ranges to map:** a temporary line layer styled red / amber / blue. Use *Make Permanent* to
  keep it.
- **Export CSV:** the table.

## Assumptions and limits

- **Model inputs.** Bottom tension, slack and touchdown position are lay-model outputs. In deep water
  the TD position can be tens of metres from the cable's true position, so spans there are a risk
  indication, not a span list.
- **2D small-slope model.**
  - Bending stiffness enters only through the smoothing length.
  - Within a span the tension is taken as constant, and friction's decay of the pulled-taut tension
    away from a span is ignored (conservative).
  - Lateral movement and slope-wise sliding are not modelled.
  - The deposit tension is used as the residual tension, because seabed friction holds it in after
    lay.
- **Bodies.** Repeaters, BUs and joint boxes are not modelled as point weights. A heavy body pulls a
  span down, so ignoring it is conservative for gaps.
- **Cable in the water.** The hanging length and its depth-versus-length split are estimates, so
  type changes are located to within a fraction of the hanging length.
- **Loops are not modelled.** The loop flags mark the conditions associated with loops and kinks
  (surplus cable at near-zero tension, payout while stopped, touchdown falling back). Torque-
  unbalanced (armoured) cable is the most susceptible.
- **Thresholds are defaults** to adjust, not standards.
- **Sample rate.** Transient (NTTS) checks are only as good as the logging rate; a 1 Hz log can miss
  short peaks.

## References

- ITU-T Recommendation G.978 (05/2025), *Characteristics of optical fibre submarine cables*, §5.3
  (mechanical characteristics; definitions in ITU-T G.972).
- E. E. Zajac, *Dynamics and kinematics of the laying and recovery of submarine cable*, Bell System
  Technical Journal, 1957.
- Makai Ocean Engineering, MakaiLay documentation: bottom slack / bottom tension solutions; "too
  little slack can lead to suspensions… too much can cause loops and eventually kinks".
