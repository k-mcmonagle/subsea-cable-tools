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

- **A Cable Route Workbench RPL.** KP is measured along it from its start KP (the plugin's KP setting: geodesic
  on WGS84 by default), as every KP tool does. Its alter courses, its slack and cable type per leg, and an
  assembly fitted to it sit at the measured KPs of its positions. The KPs printed in the RPL are checked, not
  used: a note says when they differ by a metre or more. Between positions the route follows the geodesic (the
  path KP is measured along, and the one a projected map draws), so on long legs the seabed is sampled on that
  track, not on a line straight in degrees (14 m apart mid-way along a 20 km leg at 57°N).
- **A line layer**, with the KP at its first vertex.
- **A CSV profile.** Columns are `KP` (km) or `Chainage` (m), and `Depth` (m) or `Elevation` (m). There is no map
  link.
- **A DXF profile drawing** (ASCII DXF; LINE, LWPOLYLINE and POLYLINE on one layer are joined). Set what X is
  (chainage from a start KP, or KP in km or m), whether Y is elevation or depth, the drawing's vertical
  exaggeration and a datum offset. There is no map link.
- **A synthetic seabed**, for studies and training. Set the slope, the sand waves and the random roughness.

A *KP window* limits the analysis to part of a long route. It is cut from the route with the route's own KPs.

### Seabed

The seabed is sampled along the route with the Depth Profile engine, the same as the Lay Assessment.

- **Rasters.** Each grid is sampled at its own cell size, the finest first. The list follows the project's layers
  (in the Layers panel order; web map layers are left out). *Along the route* ticks the rasters the route crosses
  (within the KP window); *All* and *None* act on the rasters listed, and a search box appears when there are more
  than eight. A scenario finds a raster removed and added again by its file, and the seabed status names any that
  are not in the project.
- **Contours.** The route's crossings are used.
- **RPL approximate depth.** A coarse fallback that holds no relief.

The status line repeats the Lay Assessment's sampling notes: coarse grids, contour interval, and rasters meeting
along the route.

- **Noise filter** (auto by default). The running mean the cable rests on. It removes sounding noise and keeps
  crests. Auto measures the data's point-to-point variation σ (a robust standard deviation from second differences:
  `1.4826 median|Δ²z| / √6`, unmoved by sparse features) and averages `n = (4σ / gap)²` data intervals, so the
  remaining noise is at most a quarter of the reporting gap; never more than two intervals or half the reporting
  length (relief at the scale of the spans reported is kept), and none on data coarser than that (its variation is
  relief, its cells already averages). Scenarios saved before auto keep their value.
- **Smoothing for lengths.** Lengths use the seabed smoothed over the cable's conformity length
  `2 (EI / w)^(1/3)` (10 m without EI).
- **What the data resolves.** The data spacing around each station is the raster's cell (or the sampling interval
  where coarser), or the spacing of contour crossings, profile points or RPL positions. A span shorter than three data
  intervals (*Spans need at least*, default 3: two to show a sag, and each support placed only to within half an
  interval) is *indicative*: light blue, listed as information, not red or amber. Spans at a seabed feature or in-line
  body you entered are exact. Where three intervals exceed the reporting length the stretch is flagged *Seabed data
  too coarse* (information; amber where they exceed the red length: span risk there is not assessed). The reporting
  limits are never changed for the data.
- **Single-point peaks** standing 8 σ above both neighbours are listed by KP as possible fliers (or small objects);
  nothing is removed.
- **Suit the data** sets sampling, the noise filter and smoothing to auto and spans to three data intervals.

**Seabed features.** Each feature sits on the seabed at a KP and adds height:

| Feature | Shape |
|---|---|
| Boulder | Half ellipse: height, base length |
| Pipeline / cable crossing | Circle: top above the seabed, outside diameter |
| Berm / mattress | Trapezoid: height, base length, crest length |
| Depression (trough) | Inverted trapezoid: depth, width at the seabed, width at the bottom |
| Other (custom shape) | Points `offset height` from the KP along the route, straight lines between; negative heights cut into the seabed |

The features table is the collapsible *Additional features* group under *Seabed*. A rock berm of several slopes,
a crossing with mattresses or a trench is a custom shape: type its points (`-10 0; -5.5 1.5; -2 1.8; 2 1.8;
5.5 1.5; 10 0`), paste them, or take them from a DXF cross-section (*Shape from DXF*: heights above the straight
line between the shape's ends, offsets from its middle, with a unit scale and vertical exaggeration). Several
features combine: the highest point of the raised ones, the lowest of the depressions. All setup tables have
*Paste* (tab-separated rows from a spreadsheet), *Import* and *Export* (CSV; a header of the column names, or
the same column order).

The profile gains stations through each feature (length / 40, between 2 cm and 0.5 m).

### Cable and bodies

There are five cable sources:

- one library type;
- library types by KP section;
- the RPL's cable type per leg (`CableType`);
- a Workbench assembly fitted to the RPL: its sections and bodies land at KPs through the RPL slack;
- properties you enter.

Workbench labels such as "SA" resolve through the library in this order: the project mapping, a library name,
an alias, then the one type with that generic type. Where several library types could mean a label, the
mapping table in step 3 marks it *ambiguous*. Choose the type there. The mapping is saved in the QGIS project,
and the Lay Assessment uses the same one. An unresolved RPL label uses the default cable type; an unresolved
assembly label uses the assembly's own weight and stiffness, with no NPTS.

The model uses each cable's submerged weight, EI, MBR, diameter and NPTS.

**Bodies** (repeaters, joints, branching units) have a KP, a rigid length, a submerged weight and a diameter. A
body adds its weight over its length. Its housing holds the cable either side off the seabed by half the
difference in diameter.

**Protection** (the collapsible *Protection* group: e.g. polymer half-shells or articulated pipe) has a KP range,
an added weight in water, an outside diameter, an added EI and its own MBR, from its data sheet. Over its range it
adds its weight to the cable's (span length goes as `sqrt(T / w)`, so a heavier cable spans less but bears harder
on a crest), adds its EI when given, and sets the MBR to the larger of the cable's and its own. Its kind is a
friction table key; with no row for it, the cable's friction applies. It shows as a grey band along the cable and on
the DXF layer `SA_PROTECTION`.

**Diameter.** The line is the underside of the cable: gaps are measured from the cable to the seabed. Diameter has
little effect on spans for telecom cables (tens of mm). It sets a body's housing standoff and the outside of
protection.

### Friction

- **Default.** One axial and one lateral Coulomb coefficient (default 0.5). The axial μ has a slider (0 to 1.5;
  type a higher value, e.g. on rock).
- **Soil zones and friction table.** Add soil zones by KP. A table then gives coefficients by cable type or body
  kind and soil. `*` matches any. The most specific row wins.
- **Update the results as values change** (on by default, the same setting as the lay control's): a change of
  μ, the zones or the table solves again in about a second, without sampling the seabed again.

The model is 2D, in the vertical plane along the route: the axial μ carries the lay history (residual tension,
sliding on slopes, loop risk). **The lateral μ is used only for the alter-course note** (the turn radius the cable
holds at its tension); nothing in the model moves the cable sideways.

No published axial coefficient for cables on sand or clay was found. Pipeline guidance gives 0.6 on sand and 0.2
on clay, laterally (DNV-RP-F109). On rock, axial resistance can be many times the interface friction (Griffiths et
al. 2024). Run a low, a best and a high value.

### Lay control

- **Bottom tension** (kN).
- **Bottom slack** (% over the seabed length).
- **Slack over the route** (% over the route length, as in an RPL).
- **The RPL slack of each leg.**

You can override the value by KP section. Set the lay direction: friction holds the tension each stretch was laid
at, so on slopes the result depends on it. *Find the highest tension within limits* opens the Find tension tab
(see the model, section 7).

A section can also **pin** the cable (*Control*: *Pinned*, Value in kN): fix it in place, for any reason. Over a KP
range (buried, e.g. from PLDN to PLUP, or clamped) it is laid at its Value (e.g. the tension at PLDN; blank: the lay
tension there). At a point (*KP to* = *KP from*) it is an anchor, which needs no Value: its tension is the pull of the
cable on it, to compare with the anchor's holding capacity. See the model, section 2a. *Name* (optional, e.g.
*Burial 1* or *First-end anchor*) labels it on the profile and in the Pinned tab. A pinned section up to 10 m beyond
an end of the profile pins that end; one further out is left out, and the run says so.

*Pin burial sections…* gives a quick first set to edit in the table: pinned sections over the burial already
planned, from the RPL's burial events (PLDN / PLUP, Start / End burial and other phrasings, read as the Burial
Planner's *Import plan* reads them, in the lay direction; or its ProtectionMethod column) or from the Burial Planner
plan for the RPL when it shares the analysis's KPs, at a tension you choose. Burial as the cable is laid (plough, or
burial with no tool named) is pinned by default; post-lay burial (PLB, MFE) is not, as the cable lies on the seabed
as laid until it is buried.

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

Where even pulled straight the cable laid is shorter than the seabed it must cover, it has **no finite tension**
(the cable is inextensible). The findings, readouts and exports say so (a red finding, "unbounded" in the CSV),
and the tension plot leaves a gap there rather than drawing the search limit.

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

Long routes march on a reduced grid targeting 20,000 nodes, retaining extra stations on both sides of every
pinned boundary so the entry pull and the restraint are kept distinct. The band is integrated exactly between
nodes, and the frictionless part (`w dy`) is restored between them.

**The ends of the profile.** Both ends sit on the seabed (the rest shape is fixed there). The end laid first keeps the
tension it was laid at, or what a later touchdown pulls it to, as if held by the cable beyond it or an anchor: nothing
slides off it. The end laid last is the current touchdown.

### 2a. Pinned sections: buried or anchored cable

The lay is followed touchdown by touchdown in the lay direction (laid towards decreasing KP, from right to left on
the plot), so each pinned section acts on the cable laid before it and is untouched by the cable laid after it.

A pinned range (lay control *Pinned*) is laid at its own tension. Like any touchdown, that tension pulls back on the
cable laid before it, falling by `mu w` per metre (reach `(T_pinned - T_lay) / (mu w)`: 25 kN against 1 kN of surface
lay reaches 800 m back for 60 N/m at `mu` 0.5). Spans there are labelled *pulled back by a pinned section*. Then the
pinned cable:

- follows the seabed: the rest shape is pinned to the seabed over the range, so it does not span, and a span can
  start at its end (e.g. at PLUP);
- does not slide: friction grips the range, and the segment after it in the lay direction, so later touchdowns do
  not reach back through it. Its far end acts as an anchor for the cable laid after it: that cable starts afresh at
  its own lay control, and a later, higher tension pulls up to the end and stops there;
- gives no cable to a slack section next to it: the pull-taut windows end at a pinned section.

A pinned point is an **anchor**: it holds and does not pull, so its Value is not used. The touchdowns laid after it
pull on it (friction takes `mu w` per metre off the pull on the way), nothing passes it to the cable laid before it,
and its tension is that of the cable pulling on it: compare it with the anchor's holding capacity. Example: laid
towards decreasing KP from an anchor, 1 kN of surface lay and then PLDN at 25 kN 600 m on, on a flat seabed (60 N/m,
`mu` 0.5): the cable between rises from 7 kN at the anchor to 25 kN at the PLDN, and the anchor carries 7 kN (less
over sand waves, whose crests take more by friction). An anchor at the end of the profile laid first changes no
tension, as that end is always held (as by the cable beyond it), but gives its load.

For each pinned section the Pinned tab gives its tension (for an anchor, the pull on it), the free cable's tension on
the side laid before and the side laid after, the net axial load it carries (their difference; a side beyond the
profile counts as zero), how far its pull-back reaches, and the **pull-up** at its ends: where the free cable turns
up out of it (e.g. a PLUP at the foot of a rise), `P = H dtheta - w dx`, which the burial or anchor must resist. No
soil resistance is modelled: compare the pull-up with the uplift resistance of the burial. The tension finder and the
sweep keep the pinned sections as set, and the detail model keeps their nodes on the seabed (bilateral contact). The
highest tension for spans within limits is not given over a pinned section.

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

  It is red when it is 1 m high, 50 m long, below the MBR at a support, or above the contact limit (when set).
  The Lay Assessment measures spans the same way.
- **Span ends.** Near a tangent touchdown the gap grows only as `w d² / 2T`, so the cable looks to touch well
  before the exact touchdown (at 10 kN and 60 N/m it is within 5 cm of the seabed for the last 4 m). *Span ends
  where the gap is under* (default 0: the exact touchdown, the pipeline definition) counts the cable as touching
  within that gap. Each tangent end then moves in by about `sqrt(2 gap T / w)`, e.g. to compare with a survey's
  gap detection limit. Both tools have this setting.
- **Supports** sit where each span meets the seabed. A short contact with a span either side (a crest: no longer
  than `2 lambda`, or three stations) is one support, which carries the turn of the cable and the weight of the
  cable resting on it. Elsewhere the support is the end of the contact next to the span: where the span lands
  tangentially (a touchdown on even seabed) there is no concentrated reaction. The reaction is `P = T dtheta`. The
  bend radius is about `2 lambda / dtheta`, with `lambda = sqrt(EI / T)`. The moment is `dtheta / 2 * sqrt(EI T)`.
  These estimates are shared with the Lay Assessment.
- **Contact limit.** *Contact limit at a support* (default off) flags supports whose reaction is above it, and
  makes their spans red. The reaction is a point load from the 2D model on a rigid seabed: compare it with the
  crush rating of the cable (or its protection) over the bearing length its data sheet uses (often per 100 mm).
  The detail model gives a closer value at a crossing.
- **Highest tension for spans within limits.** Bisection on a constant tension in windows of 1/40 of the route
  (25 to 250 m; one value per window). Spans only grow as tension rises for a uniform cable on a frictionless
  seabed: the matrix of the discrete problem is a Minkowski matrix (Binda & Contro 1979, after Cottle). So the
  bisection is well posed.

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

Under *Find tension*, *Spans against tension (sweep)* lays the route at a series of constant bottom tensions (friction and slopes included). For each tension the
sweep plots and tabulates:

- span count and total length;
- the longest span;
- the largest gap;
- the tightest bend;
- whether the tension is above NPTS.

### 7. Find tension

The reverse question: the highest constant bottom tension that keeps the spans within a target, for the whole
profile, the visible part, or each lay control section. Targets:

- no reportable span (the reporting limits);
- no red span;
- no span at all;
- no span higher than `h` and longer than `L`.

Each can also require the bends at supports within the MBR and the reactions within the contact limit. Each
tension is solved in full (friction, slopes, bodies, protection) on a geometric grid from just above the
conformity tension to the search limit (default 50 kN), then bisected to about 2%. The answer is the highest
tension below which every tension searched meets the target. Spans shorten as the tension falls, so the answer is
a ceiling: lay at or below it. A cable always spans a pipeline it crosses: such spans are listed as remaining at the
lowest tension. The floor of the tension window comes from the loop and slope checks, not from spans; to follow the
seabed the cable needs at least the seabed fill as slack (listed per scope). *Use for the lay control* sets the
answers (rounded down to 0.05 kN) as the bottom tension, or per section.

Check: a 0.5 m pipeline crossed at a span limit of 8 m (armoured cable, 60 N/m) gives 3.93 kN; the ideal parabola
`T = L² w / 2h` gives 3.84 kN (the model's spans are about 1% shorter over the pipe's round top).

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
| Contact load at a support (above the contact limit, when set) | red |
| Steep slope (small-slope model note, above 20°) | info |
| Alter course (lateral) | info |
| Span below the data's resolution (indicative) | info |
| Seabed data too coarse for the reporting length | info; amber beyond the red length |
| Event not in the model (an RPL repeater, joint, BU or equaliser with no body within 50 m; an RPL or burial-plan crossing with no seabed feature) | info |

## Results and links

- **Profile.** Five linked rows. Drag the handles between them to share the height.
  - a status bar (green, blue, amber, red, grey);
  - the events lane (see *Events* below) and the seabed and cable. Spans are filled red or amber by level. Light
    grey marks cable clear of the seabed but under the reporting limits (not reported). Also shown: supports
    (hover for the reaction and bend radius), bodies (hover or click for their details), the detail cable and sea
    level;
  - the residual and lay tension, NPTS, and the highest tension for spans within limits;
  - pinned sections show as a dark band along the cable, with a square at each end (one for an anchor): hover it
    for what it carries;
  - slack (see *Slack* below);
  - the seabed slope against the friction angle.

  The previous run is shown dashed, so a change can be compared. *Fit*, or the *A* at the bottom left of a
  zoomed plot, shows the whole profile; a run over the same KPs keeps the view where you zoomed it. *True scale
  (1:1)* shows slopes and spans as they are. *Plots* shows or hides each row, the sea level, the previous run and
  legends, chooses the slack basis and the cable colour, reverses KP, and restores the *Default view*. *Tooltip*
  turns the tooltip at the cursor on or off and chooses its fields (KP, seabed and cable depth, gap and span,
  residual and lay tension, the highest tension for spans within limits, slack, slope and friction, cable type,
  soil); the line below the plots shows everything. Hover the status bar for the findings at a KP (the stretch
  under the cursor is outlined) and click it to go to the worst of them. *Measure* gives two-point lengths,
  heights and angles, as in the Depth Profile.
- **Slack.** Cable laid and cable used by the rest shape, with the RPL's planned slack per leg, over a running
  window (*Slack window*, 100 m by default, in Reporting limits). Lengths are on the seabed smoothed over the
  cable's conformity length (the legend says both).
  - **Over the seabed (bottom slack, the default).** 0 where the cable lies on the seabed. Below 0 the cable is
    shorter than the seabed under it: it bridges it (spans), shaded brown. Above 0 it has surplus, shaded green.
  - **Over the route** (as RPL slack is given). A cable lying on the seabed sits at the *fill* (the seabed length
    over the route), not at 0: on undulating or sloping seabed a spanning cable still reads positive here.

  The dashed amber line is the loop-risk threshold: where the laid cable passes it at zero tension, the loop check
  flags. A window is a length budget, not the state at a point (part-way along a span a short window can hold
  more cable than the flat floor under it): read spans from the profile, and each span's shortfall (how much
  shorter the cable is than the seabed from lift-off to touchdown, as drawn) from the Spans table.
- **Colour the cable by** (*Plots*), in fixed classes so a colour keeps its meaning as you change the lay control:
  - *Slack state and loop risk*: held by tension; at zero tension with surplus under half the loop limit; with
    surplus over half of it; loop risk (the loop check's limit); not held by friction (slides).
  - *Tension (% of NPTS)*: quarters of NPTS and above NPTS (kN bands where a cable has no NPTS), zero shown grey.
  - *What sets the tension*: the bottom tension, pulled taut (slack short of the seabed), raised by a slope, or
    zero; with pinned sections, also pinned and pulled back by a pinned section.

  Span severity is not offered (spans are already filled), nor bend radius along the cable (the route model has
  no bending stiffness: use Detail (EI)).
- **Events.** A lane above the profile labels what happens where, at the analysis's KPs, with a leader down to
  the cable (filled where the model carries it, hollow and dashed where it does not):
  - *RPL events*: the analysed RPL's Event column (with its Remarks), sorted by the Workbench event rules into cable
    bodies, crossings, operations (PLDN, PLUP, burial), alter courses (off by default) and others; text no rule
    matches is tried against common names (RPT 12, A/C 3). An RPL event that a modelled body or seabed feature
    carries (within 50 m) is drawn once, as that item;
  - *In the model*: bodies, protection ranges and seabed features. A body from an assembly fit says how far it
    moves for each 0.1% of slack laid differently from the RPL plan;
  - *Route Comments* on the same route: points and KP ranges, closed ones optional (grey). Double-click one to open
    it in Route Comments;
  - *Burial plan*: the Burial Planner plan for the same RPL (the issued one, else the latest; *Events ▸ Burial plan*
    chooses): PLDN / PLUP or start / end of burial, burial and skip sections, crossings and other hazards;
  - *My markers*: saved with the scenario.

  Comments and plans made on another RPL, start KP or KP convention, or on the route before it was edited, are
  left out, never shifted; the Events tab says how many and why. Right-click the profile or the lane to add a
  marker or a Route Comment at a KP (or over the KPs in view), or to edit or delete a marker. The *Events* menu
  chooses the sources and groups, the labels (name, or name and KP; 1 to 4 rows), the leaders and faint lines
  through the other plots, and whether RPL bodies and crossings are checked against the model. Labels never
  overlap: an event without room keeps its tick, and hovering lists it. Route Comments and the burial plan are
  read again when they change.
- **Map.** Hovering moves a marker along the route. A click shows the KP on the map. *Pick KP on map* goes from the
  map to the profile. Findings and spans highlight their KP range.
- **Tabs** (click a column heading to sort):
  - Findings;
  - Spans (with bodies in each span, and how much shorter each is than the seabed under it);
  - Bodies;
  - Pinned (when the lay control pins the cable);
  - Events (source, type, KP, in the model or not, details; what was left out and why);
  - Summary (inputs, totals, notes);
  - Find tension, with the tension sweep below it;
  - Detail (EI).
- **Export.** Findings, spans and the profile by station as CSV, the plot as PNG, and the scene as DXF (R12, metres,
  X = KP × 1000, Y = elevation × a chosen vertical exaggeration). The DXF layers are `SA_SEABED`,
  `SA_SEABED_SAMPLED`, `SA_CABLE`, `SA_CABLE_DETAIL`, `SA_SPAN_RED`, `SA_SPAN_AMBER`, `SA_SPAN_INDICATIVE`, `SA_CLEAR_UNDER_LIMITS`,
  `SA_SUPPORTS`, `SA_BODIES`, `SA_PROTECTION`, `SA_PINNED`, `SA_FEATURES`, `SA_SEA_LEVEL`, `SA_FRAME` and `SA_TEXT`. The tension, slack and slope
  graphs sit below the profile, with one layer per curve (the slack graph on the basis the plot shows). The profile
  CSV has the slack over the route (`fill_pct` ... `surplus_pct`) and over the seabed (`bottom_*`), what sets the
tension (`tension_from`) and whether the station is pinned (`pinned`). *Add to map*
  writes the findings as a line layer.

## Examples

*Examples* loads a synthetic case as a new scenario and runs it:

- **Verification: flat seabed with a depression.** A trough 40 m wide and 2 m deep, with a light cable
  (10 N/m). The Summary compares the model with the closed form (`cable_seabed.verification`):
  - the sag is `s = w b² / 8T`;
  - if `s < d`, one span `b` long, with gap `d - s` at the centre;
  - if `s ≥ d`, two spans, each `sqrt(2 d T / w)` from the rim.

  At 2 kN the cable bridges the trough. Below 1 kN it touches the bottom. The model's spans read up to about
  `w d / 2T` shorter, because its tension falls by `w d` into the trough.
- **Boulder on a flat seabed.**
- **Pipeline crossing, armoured cable.** Use Detail (EI) on a span.
- **Pipeline crossing with protection (illustrative values).** The same crossing with 40 m of articulated pipe
  (45 kg/m in water, 150 mm, MBR 4 m; not product data): shorter spans, a larger reaction on the pipeline.
- **Rock berm (custom shape), armoured cable.**
- **Sand waves on a slope, with a repeater.**
- **Burial sections and a first-end anchor (illustrative values).** Laid towards decreasing KP from an anchor at
  the right-hand end, armoured cable, surface lay at 1 kN and two burial sections pinned at 25 kN (the tension at
  PLDN): each PLDN tension pulls back on the cable laid before it and bridges sand-wave troughs there; the anchor
  takes what reaches it (Pinned tab).

## Storage

Scenarios save in one GeoPackage per project, by default `<project folder>/span_analysis/<project>_span_analysis.gpkg`.
The path is kept in the project, and you can open another file or start a new one. Each scenario keeps:

- all its settings (layers by id) and its markers;
- the sampled seabed, so it reopens without sampling again.

## Validation

Unit tests (`tests/test_cable_seabed.py`, `tests/test_span_analysis_qgis.py`, `tests/test_span_events.py`):

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
| Depression: one span `b` with gap `d - w b²/8T`, or two spans `sqrt(2 d T / w)` | closed form | within 3% |
| Spans grow with tension | Binda & Contro 1979 | monotonic |
| Rest shape against the plugin's drape solver (dynamic relaxation, large rotation) at its tension | independent solver | RMS 22 mm; gap within 10% |
| Slack above the fill lies at zero tension with the surplus; below it is pulled taut with no surplus | model rule | yes |
| A cable lying on the seabed has 0% bottom slack; over a 40 m trough it reads below 0 (−2.35% over 100 m) while over the route it reads +0.07% | definition | yes |
| Friction set on prepared stations gives the same answer as preparing them with it (live update) | consistency | exact |
| A section pinned at 25 kN after 1 kN of lay pulls back `mu w` per metre, 800 m (60 N/m, `mu` 0.5); nothing passes it; both lay directions | closed form | within 1% (reach within 2%) |
| Cable leaving a pinned section up a 1:10 ramp at 5 kN: pull-up `H s - w dx` | closed form | 0.438 kN, exact |
| An anchor holds the cable laid before it from a later 25 kN section 200 m on: load `25 - 1 - mu w 199 m`; its tension is the cable's at it; its Value is not used | closed form | 18.03 kN, exact |
| The far end of a pinned section does the same for the cable laid after it | closed form | 18.03 kN, exact |
| Laid towards decreasing KP from an anchor, PLDN at 25 kN 600 m on: the anchor carries `25 - mu w 600 m` | closed form | 7.00 kN, exact |
| No span inside a pinned section; the sweep and the tension finder keep them; the detail model stays on the seabed there | model rule | yes |

## Assumptions and limits

- 2D, static, after lay. No current, vessel motion or lay dynamics. No lateral movement, except the alter-course
  note.
- Rigid seabed: no embedment or self-burial. A pinned section is rigidly fixed: the soil's axial and uplift resistance is
  not modelled.
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
