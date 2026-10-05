# Numeric Ground Model profiles

Open **Burial Planner → Planning → Ground Model**, and select **Numeric properties**.
The existing **Soil classes** view and editing tools remain available. Only the
top-level Acquired Data, Assessment and Reporting tabs are disabled while they
are under development.

## 1. Import investigation measurements

Choose **Import profiles…** and a CSV, TSV, delimited text, XLSX or XLSM file.
Select the worksheet and header row, then use the preview and column selectors.
Column names are suggestions only: map your own identifiers and variables.
Identifiers are trimmed and matched exactly, including case and leading zeros.

A long-format file can contain several investigations and variables:

```csv
Investigation,Depth,Property,Value,Units,Quality
CPT-01,0.00,su,15,kPa,
CPT-01,0.02,su,18,kPa,partial
CPT-01,0.04,su,,kPa,missing reading
CPT-02,0.00,su,25,kPa,
```

Map Investigation → Investigation ID, Depth → Depth, Property → Variable,
Value → Value, Units → Units, and Quality → Quality / coverage flags. For a
single-variable file, leave Variable and Units unmapped and type their names
in the two text boxes (for example `su` and `kPa`).

For a wide table, select **Wide format / multiple value columns**, tick each
measurement column and enter its variable name and unit:

```csv
TestID,Depth_cm,ConeResistance,SleeveFriction
BH-01,0,2.1,45
BH-01,2,2.3,48
```

Map TestID and Depth_cm, choose depth units **cm**, then include ConeResistance
as `qc / MPa` and SleeveFriction as `fs / kPa`. Different units remain separate
selections: the tool does not silently combine or convert numeric values.
Depth units m, cm and mm are converted to metres below seabed. Depth must be
nonnegative. Select comma decimals for such deliveries; additional missing
tokens, such as `-9999;-999`, are separated by semicolons.

For interval measurements, map both Depth / interval top and Interval base.
Otherwise, each point has bounded depth support: the **Point sample support**
setting is the maximum full thickness in metres (default 0.02 m), clipped at
neighbouring sample midpoints and at the seabed. Set it to the delivered sample
spacing. Larger gaps remain blank. No linear interpolation or extension to the
next investigation is performed. Duplicate depths, overlapping depth intervals,
invalid numbers and missing IDs reject the import with a row/source diagnostic.
Blank/NA/nonfinite measurement values remain missing, never zero.

Measurements are saved inside the project's burial GeoPackage, independent of
route assignments. Reimporting the same ID, variable and unit replaces that
profile **for every plan using it**; other profiles remain untouched. Use a
distinct investigation ID for a separate revision you want to retain. File,
worksheet, mapping, depth support and flags remain available in source inspection.
Shared source replacements are not undone by rolling back an individual plan.

## 2. Assign investigations to route intervals

Assignments are saved separately for each plan. Importing measurements alone
does not guess where they apply. Choose either:

- **Assign by polygons…**: select a QGIS polygon layer and its investigation ID
  field. The layer is transformed into the selected route's CRS; every route
  crossing is measured using that route's KP distance model and start datum.
  Holes, repeated crossings and disconnected route parts retain separate intervals.
  Empty/invalid geometry rejects assignment. Polygons without IDs or route
  crossings are reported. These are saved assignment snapshots; rerun assignment
  after editing the polygon layer.
- **Assign by KP table…**: map investigation ID, start KP and end KP from a
  CSV/TSV/Excel table. Choose km or m and specify the source KP reference using
  the existing current-route / RPL revision / constant shift / matched-pairs
  controls. Delivered KPs and mapping flags are retained.

```csv
Investigation,FromKP,ToKP
CPT-01,10.000,11.500
CPT-01,12.000,12.500
CPT-02,12.500,14.000
```

Each assignment operation replaces the plan's complete assignment table. Repeated
IDs are allowed over disconnected intervals. **Review assignments…** lists all
intervals, delivery references, unmatched IDs, unassigned sources, out-of-route
intervals and overlapping assignments. Overlaps (including repeated IDs) are
drawn as amber hatching; no investigation wins silently. Adjacent intervals
share a boundary without overlapping. Assignments are half-open at their ends.

Plan duplication copies assignments and display settings while sharing the
original measurements. Assignment/display edits use the normal plan history.
Changing the route with **keep seabed positions** re-references assignments along
with the other plan data, retaining the original delivered KPs and new mapping
flags. **Keep KP numbers** retains their numbers, as for the other plan data.

## 3. Read the numeric view

Select a variable/unit pair, depth limits, colour ramp and continuous colours
or 2–32 equal-width discrete bands. Automatic colour limits use all measured
values for the selected variable in assigned investigations, independent of the
visible KP/depth window. Manual limits clip colours at the chosen endpoints.
Use **Apply display** after editing depth or manual colour limits. Settings are
saved with the plan; panning and zooming never change the colour scale.

KP runs horizontally and depth increases downward. KP remains linked to the
existing bathymetry view, and the dashed target burial-depth line follows the
plan's default and KP-range targets.

- Blank: no route assignment or no measured depth support.
- Grey: missing measurement or no matching source/variable.
- Hatching: retained quality/partial flags; amber hatching marks route overlap.

Hover reports source ID, sample depth, value, units, depth support, assignment
coverage and flags. Click an assigned interval or use **Inspect source…** to
open the original measurements and their depth plot, including nulls and flags.
Overlapping assignments expose each source without choosing a value.

Rendering uses a single graphics item with cached source strips sampled at
screen-pixel centres; work scales with visible sources and screen height rather
than one graphics object per measurement. Features finer than a pixel require
depth zoom to inspect. Hover and the source table always query the original
measurements. Numeric imports are a display/inspection capability; they do not
alter soil classes or automatically derive burial design conclusions.
