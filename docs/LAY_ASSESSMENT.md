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
which carries touchdown (TD) KP, position and depth, bottom tension, bottom slack, and measured and
calculated top tension. They also run on any layer with enough of these columns, for example the
cable lay (telegsum) log for top tension and payout checks. The tab detects each column from its
name (for example `TD KP`, `Bot.Tension`, `Inst.Bot.Slack`, `Meas.Top Tension`, `Calc.Top Tension`,
`TD Depth`, `Ship Speed`, `Payout Speed`, `Solution Valid`). Check the *Columns* box and correct any
column it picked wrongly.

- **KP (touchdown)** is required. Rows without a KP are left out, as are rows whose *Solution valid*
  flag reads 0, no, false or invalid.
- **Tension units** are set once for all tension columns (kN, tonne-force, kgf or lbf).
- **Bottom slack** is the seabed slack in %, where 0 means the cable exactly follows the seabed
  profile. This is the MakaiLay convention: when computed slack reaches zero the model reports
  bottom tension instead.
- **Bottom tension** in MakaiLay data is a *model* output, not a measurement. Top tension is the
  measured quantity. The *Top tension consistency* check compares the two.

**Cable library.** Weights and tension limits come from the cable type library (*Subsea Cable Tools ▸
Cable Type Library…*, or *Cable library…* on the tab). This is a GeoPackage you create and keep on
your computer; the plugin ships no cable data. Each type has:

- name, category (cable or rope) and comma-separated aliases;
- diameter, weight in air and weight in water;
- CBL, NTTS, NOTS, NPTS and MBR.

A record's cable type column is matched to a library name or alias. Records whose type does not match
use the *Default cable type*. Types can be imported from and exported to CSV.

The tension limits follow ITU-T G.972, as summarised in ITU-T G.978 §5.3.3:

| Limit | Meaning | Checked against |
|---|---|---|
| CBL | Cable breaking load (qualification test) | context only |
| NTTS | Transient: could be met accidentally, particularly during recovery | measured top tension (red) |
| NOTS | Operating: could be met during repairs (holding for jointing) | measured top tension (amber) |
| NPTS | Permanent: the cable's state after laying | bottom / residual tension (red) |

**Seabed source** (for the seabed checks):

- *Lay model touchdown depth*: the lay model's own `TD Depth`, binned every *Sample every* metres of
  KP. Nothing else is needed, but it only sees seabed relief at the lay model's resolution.
- *Raster bathymetry*: an MBES grid. Grid XYZ soundings first with *Create Raster from XYZ*.
- *Depth contours*: a line layer and its depth field, used at its crossings with the track.

Rasters and contours are sampled along the **touchdown track** with the Depth Profile engine, using
the same datum handling, coverage rules and finest-raster-first order (see
[KP and bathymetry methods](KP_AND_BATHYMETRY.md)). The track is built from the records' TD
positions, ordered by KP and binned every 2 m, so stops and jitter do not zig-zag it. Plot positions
are mapped back to the data's TD KP. *Seabed KP range* limits the seabed checks to a window, which is
useful on long routes.

## Checks

Each check flags records. Records flagged in a row (in time order) form a range, and ranges closer
than *Merge ranges closer than* join. A range takes the worst level and worst value of its records.
A check that lacks its inputs is skipped, and the status line says why.

| Check | Flags | Level |
|---|---|---|
| Laid under bottom tension | bottom tension above a threshold (default 0.5 kN), i.e. no bottom slack | amber |
| Bottom tension limits | bottom tension above NPTS; optionally above your own target | red / amber |
| Top tension limits | measured top tension above NOTS / NTTS | amber / red |
| Top tension consistency | measured vs calculated top tension; without a calculated column, measured top tension − w·d (below) vs logged bottom tension | amber |
| Loop risk (excess slack) | bottom slack ≥ threshold (default 8%) at near-zero bottom tension (≤ 0.1 kN), optionally only shallower than a depth | amber |
| Payout while stopped | ship speed ≤ threshold while payout speed ≥ threshold (same units) | amber |
| Touchdown moving back | in time order, TD KP falls back over ground already laid by ≥ 5 m | amber |
| Slack off plan | bottom slack differs from planned bottom slack by more than a tolerance | info |
| Suspensions (seabed model) | modelled spans clear of the seabed (below) | amber; red when higher or longer than set |
| Slack vs seabed | per window, the seabed needs more cable than was laid (below) | amber |

All thresholds are editable defaults, not standards. Set them for the cable and the project.

### Top tension and bottom tension

In steady lay, the tension change between touchdown and the sheave equals the cable's submerged
weight times the water depth, plus its in-air weight times the sheave height (Zajac, 1957). This
identity is exact when tangential drag is neglected:

```
T_top = T_bottom + w_water · d + w_air · h_sheave
```

So `T_bottom ≈ T_top(measured) − w_water · d_TD − w_air · h`. The Seabed Profile plots this
estimate (dashed) beside the logged model bottom tension. A persistent difference suggests one of:

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

- `H` at each station is the highest logged bottom tension of the records depositing cable there.
  This is conservative: more tension gives more and longer spans.
- `w` is the library weight in water for each record's cable type.
- Stations where the bottom tension is below *Cable conforms below tension* (default 0.05 kN) lie
  on the seabed and pin the cable. With bottom slack laid, the cable follows the seabed.
- Spans are reported when higher than *Report spans higher than* (default 0.3 m) **and** longer than
  *…and longer than* (default 5 m). They are red when higher than 1 m or longer than 50 m.
- Gaps in the bathymetry are never bridged.

**Validation.** On a synthetic two-ridge seabed, the model was compared with the plugin's drape
solver (dynamic relaxation over a profiled seabed, frictionless, no bending stiffness):

| Case | Spans (drape / this model) | Max gap (drape / this model) | RMS shape difference |
|---|---|---|---|
| H ≈ 1 kN | 4 / 4 | 0.83 / 0.85 m | 4 mm |
| H ≈ 8.5 kN | 5 / 5 | 3.25 / 3.35 m | 7 cm |

Span positions agree to within a metre or two. The small excess is because the parabola slightly
overestimates the exact catenary. The model's cable length matched the drape cable length, and the
unit tests check the mid-span sag against `w L² / 8H`.

### Slack vs seabed

Bottom slack is relative to the lay model's own seabed. Finer bathymetry can show relief the model did
not see, and the cable then needs more length than was laid. Per window (default 200 m):

```
cable laid  = Σ (1 + slack/100) · √(Δx² + Δd_TD²)    over the records (lay model's seabed)
seabed      = Σ √(Δx² + Δd²)                         over the sampled bathymetry
shortfall % = (seabed − cable laid) / plan length · 100
```

Windows with a shortfall above the threshold (default 0.5%) are flagged. Windows with less than 80%
bathymetry coverage are skipped. This check needs bathymetry; it is not run against the lay model's
own depths.

## Results

- **Table:** level, check, KP from / to, length, worst value and a message. Click a row to highlight
  the range along the touchdown track on the map and zoom the Seabed Profile to it. Double-click to
  zoom the map and select the range's records in the table, map and plots.
- **Seabed Profile:** status bar (green clear, amber, red, blue info, grey no data); seabed, the
  modelled cable with spans filled red, and the lay model's touchdown depths; logged and estimated
  bottom tension, with NPTS. Hovering moves the shared crosshair. Click or double-click a point to
  go to the nearest record. *Measure* gives two-click length / height / angle / along-seabed
  measurements, snapped to the seabed or the cable.
- **Add ranges to map:** a temporary line layer styled red / amber / blue. Use *Make Permanent* to
  keep it.
- **Export CSV:** the table.

## Assumptions and limits

- **Model inputs.** Bottom tension, slack and touchdown position are lay-model outputs. In deep water
  the TD position can be tens of metres from the cable's true position, so spans there are a risk
  indication, not a span list.
- **2D small-slope model.** The seabed model ignores bending stiffness (it slightly overstates short
  spans and understates the cable's bridging of very short hollows), seabed friction, lateral
  movement and slope-wise sliding. It uses the tension at deposit. Seabed friction holds that
  tension in after lay, which is why it is used as the residual tension.
- **Loops are not modelled.** The loop flags mark the conditions associated with loops and kinks
  (slack surplus at near-zero tension, payout while stopped, touchdown falling back). Torque-
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
