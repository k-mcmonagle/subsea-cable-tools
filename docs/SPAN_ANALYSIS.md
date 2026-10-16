# Span & Slack Analysis

Where will a planned cable span clear of the seabed, and where can surplus cable form loops? *Span & Slack
Analysis* (*Subsea Cable Tools ▸ Span & Slack Analysis*, beta) rests a cable on the seabed profile along a route
and shows the effect of the bottom tension or slack at once. Use it to:

- plan a lay;
- set tension or slack by section;
- study a feature such as a pipeline crossing or a boulder.

For as-laid data, use the [Lay Assessment](LAY_ASSESSMENT.md) in the Cable Lay Data Explorer. Both tools use the
same seabed engine (`cable_seabed`), terms, default thresholds and colours.

The results are planning and screening values from a 2D static model. The engine is checked against closed forms
and published cases (below). It is **not validated** against field data or other software.

## Inputs

### Route and KP

- **A Cable Route Workbench RPL.** KP is measured along it from its start KP, as the Workbench does. Its alter
  courses and its slack per leg are available.
- **A line layer**, with the KP at its first vertex.
- **A CSV profile.** Columns are `KP` (km) or `Chainage` (m), and `Depth` (m) or `Elevation` (m). There is no map
  link.
- **A synthetic seabed**, for studies and training. Set the slope, the sand waves and the random roughness.

A *KP window* limits the analysis to part of a long route.

### Seabed

The seabed is sampled along the route with the Depth Profile engine, the same as the Lay Assessment.

- **Rasters.** Each grid is sampled at its own cell size, the finest first.
- **Contours.** The route's crossings are used.
- **RPL approximate depth.** A coarse fallback that holds no relief.

The status line repeats the Lay Assessment's sampling notes: coarse grids, contour interval, and rasters meeting
along the route.

- **Noise filter** (default 2 m). The running mean the cable rests on. It removes sounding noise and keeps crests.
- **Smoothing for lengths.** Lengths use the seabed smoothed over the cable's conformity length
  `2 (EI / w)^(1/3)` (10 m without EI).

**Seabed features.** Each feature sits on the seabed at a KP and adds height:

| Feature | Shape |
|---|---|
| Boulder | Half ellipse: height, base length |
| Pipeline / cable crossing | Circle: top above the seabed, outside diameter |
| Berm / mattress | Trapezoid: height, base length, crest length |

The profile gains stations through each feature (length / 40, between 2 cm and 0.5 m).

### Cable and bodies

There are four cable sources:

- one library type;
- library types by KP section;
- a Workbench assembly fitted to the RPL: its sections and bodies land at KPs through the RPL slack, and its
  cable types resolve through the library and the project mapping;
- properties you enter.

The model uses each cable's submerged weight, EI, MBR, diameter and NPTS.

**Bodies** (repeaters, joints, branching units) have a KP, a rigid length, a submerged weight and a diameter. A
body adds its weight over its length. Its housing holds the cable either side off the seabed by half the
difference in diameter.

### Friction

- **Default.** One axial and one lateral Coulomb coefficient (default 0.5).
- **Soil zones and friction table.** Add soil zones by KP. A table then gives coefficients by cable type or body
  kind and soil. `*` matches any. The most specific row wins.

No published axial coefficient for cables on sand or clay was found. Pipeline guidance gives 0.6 on sand and 0.2
on clay, laterally (DNV-RP-F109). On rock, axial resistance can be many times the interface friction (Griffiths et
al. 2024). Run a low, a best and a high value.

### Lay control

- **Bottom tension** (kN).
- **Bottom slack** (% over the seabed length).
- **Slack over the route** (% over the route length, as in an RPL).
- **The RPL slack of each leg.**

You can override the value by KP section. Set the lay direction: friction holds the tension each stretch was laid
at, so on slopes the result depends on it.

## The model

### 1. Deposit tension: one rule for slack and tension

Tension and surplus cable are never both positive.

- **Tension sections** lay at their bottom tension.
- **Slack sections** lay the cable their slack gives. The cable laid is compared with the seabed it must cover
  within the friction reach `H / (mu w)` (the Lay Assessment's method).
  - Where the cable is short by more than 0.1%, it is pulled taut. The tension is the one at which the rest shape
    uses exactly the cable laid. It falls by `mu w` per metre away from the stretch.
  - Elsewhere the tension is zero and the surplus is stored slack.

The **fill** is the slack needed to follow the seabed: seabed length over route length (Zajac 1957).
**Surplus** = laid − used by the rest shape.

Tension from slack is very sensitive: about 0.02% of cable length can change it by 20%. Plan against spans with
tension control, and against loops with slack control.

### 2. Residual tension: lay history and Coulomb friction

Along the cable:

```
dT/ds = w dy/ds - f,   |f| <= mu N   on the seabed;    f = 0 in a span (T - w y constant)
N = w cos(theta) + T kappa   (kappa: curvature over a crest, the capstan term)
```

The cable is laid progressively. At each touchdown it lands at the deposit tension, and friction holds the tension
of cable already on the seabed (Zajac 1957: residual tension on a rough bottom). Each new touchdown drags the
tension behind it into the band friction allows, and leaves the rest. This is a rigid-plastic march in the lay
direction. It gives:

- **Steps.** A step in lay tension decays at `mu w` per metre (anchor length `dT / (mu w)`).
- **Steep slopes.** On a slope steeper than the friction angle (`tan theta > mu`) the cable slides. Tension builds
  up-slope, which gives spans at the crest. Where friction would need a negative tension, the cable is not held
  and slack collects below (loop risk).
- **Crests.** The tension changes by up to `exp(mu dtheta)` over a crest (capstan).

Long routes march on at most 20,000 nodes. The band is integrated exactly between nodes, and the frictionless
part (`w dy`) is restored between them.

### 3. Rest shape: the unilateral contact problem

With small slopes:

```
H y'' = q - p,   p >= 0,   y >= seabed,   p (y - seabed) = 0
```

This is the problem of Binda & Contro (1979). Its solution is the lowest curve above the seabed whose curvature
does not exceed `q / H`. It is solved exactly in one O(n) pass: the upper concave hull of `seabed − Φ`, with
`Φ'' = q / H`.

- **Curvature.** The catenary value `w / (T cos² φ)`, with φ the cable's own slope from the previous iteration.
  Spans on slopes stay close to the catenary. Lengths are exact.
- **Bodies.** They add weight, and their housing stands off the seabed.
- **Pinned stations.** Stations below *Cable follows the seabed below* (0.05 kN) pin the cable.

Steps 2 and 3 are iterated: the reactions that carry friction come from the shape. Two to six iterations are
typical.

At tension `T` the cable follows a hollow only if the hollow's radius exceeds `T / w`.

### 4. Spans, supports and allowable tension

- **Span.** A span runs from lift-off to touchdown (the pipeline definition, DNV-RP-F105). It is reported when:
  - it rises more than *Report spans higher than* (0.3 m), and
  - it is longer than *…and longer than* (5 m).

  It is red when it is 1 m high, 50 m long, or below the MBR at a support. The Lay Assessment measures spans the
  same way.
- **Supports.** The reaction is `P = T dtheta`. The bend radius is about `2 lambda / dtheta`, with
  `lambda = sqrt(EI / T)`. The moment is `dtheta / 2 * sqrt(EI T)`. These estimates are shared with the Lay
  Assessment.
- **Highest tension for spans within limits.** Bisection on a constant tension in 250 m windows. Spans only grow as
  tension rises for a uniform cable on a frictionless seabed: the matrix of the discrete problem is a Minkowski
  matrix (Binda & Contro 1979, after Cottle). So the bisection is well posed.

### 5. Detail model (bending stiffness)

A window is solved as a tensioned beam:

```
EI y'''' - H y'' = -q + p
```

The contact conditions are the same. The model uses central differences on a uniform grid (default: a quarter of
`sqrt(EI / T)`).

- **Solver.** The linear complementarity problem (Maier & Andreuzzi 1978) is solved by a primal-dual active set
  method with an O(n) banded LDLᵀ solve. If the active set cycles, projected SOR (Cryer 1971) takes over.
- **Bodies** are rigid (EI 1e5 kN·m²).
- **Window ends** are held at the route model's shape.
- **Outputs.** Bend radius, moment, shear and nodal reaction along the cable.

With EI the matrix is not a Minkowski matrix, so the monotonicity above is not guaranteed in the detail model.

### 6. Tension sweep

The route is laid at a series of constant bottom tensions (friction and slopes included). For each tension the
sweep plots and tabulates:

- span count and total length;
- the longest span;
- the largest gap;
- the tightest bend;
- whether the tension is above NPTS.

### Alter courses

The model is 2D. At an alter course, lateral friction holds the cable only on a turn radius of at least
`T / (mu_lat w)`. Over about `T dpsi / (mu_lat w)` the tension pulls the cable towards the inside of the bend. The
tool lists these values at each RPL vertex as information.

## Findings

| Check | Level |
|---|---|
| Span | amber; red when high, long or below the MBR |
| Tension above NPTS | red |
| Cable short of the seabed (slack control) | info (pulled taut); red when infeasible or above NPTS |
| Slope steeper than friction | info |
| Cable not held (slides) | amber (loop risk below) |
| Loop risk (surplus slack ≥ 8% over the slack window at tension ≤ 0.1 kN) | amber |
| Body clear of the seabed | amber |
| Steep slope (small-slope model note, above 20°) | info |
| Alter course (lateral) | info |

## Results and links

- **Profile.** Five linked rows:
  - a status bar (green, blue, amber, red, grey);
  - the seabed and cable, with spans in red, supports (hover for the reaction and bend radius), bodies and the
    detail cable;
  - the residual and lay tension, NPTS, and the highest tension for spans within limits;
  - slack laid, fill and used, with the surplus shaded;
  - the seabed slope against the friction angle.

  The previous run is shown dashed, so a change can be compared.
- **Map.** Hovering moves a marker along the route. A click shows the KP on the map. *Pick KP on map* goes from the
  map to the profile. Findings and spans highlight their KP range.
- **Tabs:**
  - Findings;
  - Spans (with bodies in each span);
  - Bodies;
  - Detail (EI);
  - Tension sweep;
  - Summary (inputs, totals, notes).
- **Export.** Findings, spans and the profile by station as CSV, and the plot as PNG. *Add to map* writes the
  findings as a line layer.

## Storage

Scenarios save in one GeoPackage per project, by default `<project folder>/span_analysis/<project>_span_analysis.gpkg`.
The path is kept in the project, and you can open another file or start a new one. Each scenario keeps:

- all its settings (layers by id);
- the sampled seabed, so it reopens without sampling again.

## Validation

Unit tests (`tests/test_cable_seabed.py`, `tests/test_span_analysis_qgis.py`):

| Case | Source | Result |
|---|---|---|
| Span sag `w L²/8H`, and `+ P L/4H` for a mid-span load | closed form | exact |
| Residual suspension over a 210 ft step: half-span 480 ft at a vertex tension of 409 lb | Zajac 1957 §5.3 | 493 ft (parabola +3%); top tension 557 lb vs the quoted 525 lb (Zajac's nomograph) |
| Heavy string from a point 1 above a flat bed, `w/H = 0.1`: touchdown 4.4357 | Fazio & Iacono 2020 | 4.465 (parabola +0.7%) |
| Tensioned beam over a knife edge: R 20 m, M 0.5 kN·m, P 1 kN | closed form | within 1% |
| Beam with EI → 0 equals the string solution | consistency | < 1 mm |
| Friction on a 20° slope, mu 0.3: `w (sin − mu cos)` = 6.01 N/m | closed form | within 1%; laid up-slope it slides |
| Friction anchor length `dT / (mu w)`; capstan `exp(mu dtheta)` | closed form | within 2% |
| Boulder: spans either side of `sqrt(2 h T / w)` | closed form | within 1% |
| Spans grow with tension | Binda & Contro 1979 | monotonic |
| Rest shape against the plugin's drape solver (dynamic relaxation, large rotation) at its tension | independent solver | RMS 22 mm; gap within 10% |
| Slack above the fill lies at zero tension with the surplus; below it is pulled taut with no surplus | model rule | yes |

## Assumptions and limits

- 2D, static, after lay. No current, vessel motion or lay dynamics. No lateral movement, except the alter-course
  note.
- Rigid seabed: no embedment or self-burial.
- Inextensible cable.
- Small-slope shape with a slope correction. Lengths are exact.
- Coulomb friction with one coefficient for static and sliding.
- Loops, hockles and kinks are not modelled, only the conditions for them. Torque-unbalanced (armoured) cable is
  the most at risk (Coyne 1990).
- **Seabed resolution limits the answer more than the mechanics do.** The cable will not lie exactly on the RPL.
  A span on the RPL line is a likelihood, not a certainty.
- Thresholds are defaults, not standards.

## References

- Binda, L. & Contro, R. (1979). Unilateral contact problems for cables. *Engineering Structures* 1(3), 125–130.
  doi:10.1016/0141-0296(79)90022-1.
- Binda, L. & Contro, R. (1981). Analysis of membranes stretched over a unilateral support. *Engineering
  Transactions* 29(3), 491–501 (free).
- Zajac, E. E. (1957). Dynamics and kinematics of the laying and recovery of submarine cable. *Bell System
  Technical Journal* 36(5), 1129–1208 (free).
- Elmendorf, C. H. & Heezen, B. C. (1957). Oceanographic information for engineering submarine cable systems.
  *Bell System Technical Journal* 36(5), 1047–1093 (free).
- Cryer, C. W. (1971). The solution of a quadratic programming problem using systematic overrelaxation. *SIAM J.
  Control* 9(3), 385–392.
- Maier, G. & Andreuzzi, F. (1978). Elastic and elasto-plastic analysis of submarine pipelines as unilateral
  contact problems. *Computers & Structures* 8(3–4), 421–431.
- Hintermüller, M., Ito, K. & Kunisch, K. (2002). The primal-dual active set strategy as a semismooth Newton
  method. *SIAM J. Optimization* 13(3), 865–888.
- Fazio, R. & Iacono, S. (2020). arXiv:2003.05485, section 3: the heavy string on a flat obstacle (free).
- Coyne, J. (1990). Analysis of the formation and elimination of loops in twisted cable. *IEEE J. Oceanic
  Engineering* 15(2), 72–83.
- Griffiths, T. et al. (2024). Experimental data and modelling of cable and pipe axial friction on a rocky seabed.
  OMAE2024-127137.
- DNV-RP-F105 *Free spanning pipelines*; DNV-RP-F109 *On-bottom stability*.
- ITU-T G.972 / G.978: cable tension limits (NPTS, NOTS, NTTS).
