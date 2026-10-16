# KP method statement

This document states how Subsea Cable Tools defines and calculates KP: the definitions, the assumptions, the
accuracy, and how to match KPs from another system. Every KP tool in the plugin uses this method through one
shared route model. Where a tool differs, this document says so.

## 1. Definitions

- **KP** is the start KP plus the chainage along the route, in kilometres.
- **Chainage** is measured leg by leg between consecutive route vertices, with the distance model in section 2,
  and summed in route order.
- **Route** is the line through the route vertices (RPL positions) in route order:
  - SeqNo order when every feature has a SeqNo, else layer order;
  - legs that touch are joined;
  - the route is never noded, re-ordered or reversed.
- **Start KP** is the KP at the first vertex. It is 0 unless it is given (section 4).
- **DCC** (distance cross course) is the distance from a position to its nearest point on the route.

## 2. Distance models

### 2.1 Geodesic KP (default)

- **Ellipsoid:** WGS84 (a = 6 378 137 m, f = 1/298.257223563). The plugin always uses it, whatever the ellipsoid
  of the QGIS project.
- **Leg length:** the geodesic distance between two consecutive vertices (the shortest path on the ellipsoid).
- **Path between vertices:** the geodesic, whatever CRS the route is stored in. A position at a KP lies on it.
  - A route stored in latitude/longitude does **not** run straight in degrees between its positions.
  - On long legs, the straight line in degrees is far from the geodesic: mid-way along a 20 km leg at 57.5°N
    it is 14 m off; on a 100 km leg it is 350 m off.
- **KP of a position:** the foot of the perpendicular from the position to the route. The DCC is the geodesic
  distance from the position to the foot.
- **Accuracy (synthetic routes, against Karney 2013):**
  - KP at every vertex and at the far end of a 6 274 km route: within 0.001 mm;
  - positions between vertices: within about 1 cm;
  - KP of a position 100 m off the route: within 2 mm;
  - KP of a position 1 km off the route: within about 0.1 m.
- **No build-up:** errors do not accumulate along the route, because each leg is measured on its own.

### 2.2 Grid (Cartesian) KP — optional

Select it in **Subsea Cable Tools ▸ KP settings…** for an RPL that was chained on a map projection.

- **Leg length:** the planar distance between consecutive vertices in one projected CRS (the grid). There is no
  scale-factor or height reduction.
- **Path between vertices:** the straight line in the grid.
- **Choice of grid**, most explicit first:
  1. the grid CRS set in KP settings (or the plan's own, for a Burial Planner plan);
  2. else the route's own CRS, if it is projected;
  3. else the project CRS, if it is projected;
  4. else the UTM zone at the route start. Tools say when they chose it automatically.
- **Validity:** grid KP is valid only inside the grid CRS's area of use. Tools report:
  - the KP basis;
  - a **warning** when route vertices lie outside the area of use;
  - how far grid KP at the route end is from geodesic KP (the grid scale factor).
- **Example:** near a UTM central meridian grid KP reads 0.04 % short. One UTM zone over a 6 000 km route reads
  hundreds of kilometres long.
- **Recommendation:** use grid KP only when the route lies inside one grid, and state that grid's CRS.

## 3. Positions and display

- **Positions** at a KP, slices between KPs, and nearest points all follow the path of section 2:
  - a point every 250 m where the path departs from the stored segment by more than 1 mm;
  - the vertices, and their KPs, are unchanged.
- **Map display:**
  - A map in a projected CRS (for example the route's UTM zone) draws long legs within about 1 m of the geodesic.
  - A map in latitude/longitude draws them straight in degrees, so on long legs it does not show where the KPs
    lie. Use a projected map CRS to inspect long legs.
- **Transit playback** in the Lay Planner deliberately follows the line as drawn. It keeps the geodesic length.

## 4. Start KP

- An RPL from the Cable Route Workbench supplies its start KP: the stated KP of its first position (normally 0).
- Tools show a start KP that is not 0, and the user can set it.
- Processing algorithms take the start KP as a parameter (default 0). They do not read it from the data.

## 5. KPs stated in an RPL

- **Check:** the KPs printed in an RPL are compared with the measured KPs:
  - a difference under 1 m is not shown;
  - 1 m to 5 m is shown as information;
  - 5 m or more is a warning.
  
  An RPL chained on a grid is recognised by comparing it with planar UTM chainage. The check never moves a
  position.
- **Use:** tools that compare RPL documents (Compare RPLs, Event Compare, the assembly fit) use the document's
  KPs. Tools that place or measure positions use measured KPs.
- **Editing a Workbench RPL:**
  - legs from the edited one onwards are measured again (geodesic);
  - the cumulative KPs before the edit stay as they are, so rounding in the document's leg lengths cannot build
    up along the route.

## 6. Assumptions and limits

- **Positions:** taken as WGS84. Other datums are transformed with the QGIS project transformation settings.
  The choice of datum transformation can move positions by up to about 1 m; leg lengths change by far less.
- **Heights:** not used. KP is horizontal distance on the ellipsoid. For the length over the seabed, see
  [KP and bathymetry methods](KP_AND_BATHYMETRY.md).
- **±180° meridian:** routes that cross it are refused, because positions on the crossing leg would be wrong.
  Split the route at 180° and give the second part the start KP of the split.
- **Precision:** KP is calculated in double precision, in metres. Displays round to 0.001 km (1 m).
- **Validation:** automated tests compare against an independent geodesic solver (Karney 2013) on synthetic
  routes:
  - 6 000 km transatlantic;
  - 3 100 km at high latitude;
  - 4 700 km across the equator;
  - grid cases inside and across UTM zones.
  
  This does not replace the stated-KP check against the source RPL.

## 7. Matching KPs from another system

| Other system's KP | Expected agreement |
|---|---|
| Geodesic, WGS84, same positions and start KP | under 1 m (the stated-KP check shows it) |
| Grid, in a known projected CRS | set that CRS in KP settings: the same planar chainage |
| Spherical (great-circle) distance, or another ellipsoid | differs by up to about 0.5 % |
| Another start KP or route order | a constant offset, or jumps where the order differs |

## References

- Karney, C. F. F. (2013). Algorithms for geodesics. *Journal of Geodesy* 87, 43–55. doi:10.1007/s00190-012-0578-z.
- Snyder, J. P. (1987). *Map projections — a working manual*. USGS Professional Paper 1395 (grid scale factor).
