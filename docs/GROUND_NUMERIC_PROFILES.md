# Numeric Ground Model datasets

Open **Burial Planner → Planning → Ground Model** and set **Display** to
**Numeric datasets**. A numeric dataset draws one measured quantity — for
example CPT undrained shear strength — by KP along the route and depth below
seabed, coloured by value. The **Soil classes** view and its tools are
unchanged.

A dataset has three parts, one tab each in the **Add…** / **Edit…** dialog:

1. **Measurements**: a table of depth readings for all investigations.
2. **KP ranges**: a layer saying where along the route each investigation applies.
3. **Colours**: a continuous ramp, equal bands, or your own value classes.

Datasets are shared by every plan in the project, like soil classes. Each plan
remembers which dataset it shows and its depth window. Dataset edits are not
part of a plan's history, so a plan rollback does not undo them.

## 1. Measurements

Choose a CSV, TSV, delimited text, XLSX or XLSM file, or a layer/table already
loaded in QGIS. Pick the worksheet and header row, then choose four columns
using the preview. Nothing is chosen for you from the column names:

| Item | What to choose |
|---|---|
| Investigation ID | The column naming each investigation (e.g. the CPT number). |
| Depth (or interval top) | Depth below seabed of each reading, or the top of its interval. |
| Interval base | Only when each reading covers a depth interval; otherwise leave *(single depths)*. |
| Value | The measured value. |

Enter the **Variable** name and **Units** at the top of the dialog (for
example the symbol and unit used in your report). They label the legend, hover
and class names. Each dataset holds one variable; to show another quantity from
the same table, add a second dataset from the same file with a different Value
column.

- **Depth unit** converts m, cm or mm to metres.
- **Single-depth reading thickness** applies only without an interval base: each
  reading is drawn this thick at most, clipped halfway to its neighbours. Set it
  to the reading spacing. Larger gaps stay blank; nothing is interpolated.
- Blank cells, `NA`-style text and any **missing-value codes** you list stay
  missing (grey), never zero.
- Two readings at the same depth, overlapping intervals, a negative depth or
  text in a number column stop the import with the data row number.

The line under the form checks your choices as you make them, for example
`✓ 350 investigation(s), 105000 depth sample(s) (420 missing), depth 0–3 m,
values 0.4–310 kPa`, or names the first problem.

Measurements are copied into the project's burial GeoPackage when you save.
The dataset remembers the source and your column choices. If the source file
or layer changes afterwards, the status line says so, and **Reload** re-reads
it with the same choices (a renamed column is reported rather than guessed).
IDs are matched exactly, including case, spaces and leading zeros.

## 2. KP ranges

Choose how investigations are placed along the route:

- **KP-range layer or table**: any loaded layer or table with an ID field and
  start/end KP fields (geometry is not used). Choose the KP unit and the RPL
  the KPs are quoted on, as for the Exclusions and Risk Profile KP-range tables.
  KPs on another RPL are translated onto the plan's route by seabed position;
  flagged translations are listed.
- **Polygon layer**: each polygon's ID applies where the route crosses it.
  Holes and repeated crossings give separate ranges.

The layer is **read live**: editing it in QGIS (including uncommitted edits)
updates the plot without re-importing. An investigation may have several
disconnected ranges. Ranges that overlap are drawn amber and no value is chosen.
The check line lists IDs in the layer that have no measurements, with a near
match where only case, spaces or punctuation differ.

## 3. Colours

- **Continuous ramp** or **Equal bands** (2–32 bands), with limits from all
  placed measurements or limits you enter.
- **Custom classes**: rows edited like the Exclusions value ranges: **From**
  with ≥ or >, **To** with < or ≤, either side blank for an open-ended class.
  Rows are checked in order and the first matching row's colour is used.
  **Create classes** turns break values into classes (below the first break,
  ≥ lower and < upper between breaks, and above the last), coloured from the
  chosen ramp; adjust the bounds and double-click a colour to change it. The
  summary lists each class with its number and share of samples, samples
  outside every class (drawn dark grey), uncovered ranges and overlapping rows.

## Reading and checking the plot

KP runs horizontally and depth increases downward; the KP axis follows the
bathymetry view and the dashed line is the target burial depth. Set the depth
window with **Depth from / to** and **Apply depth**.

- Colour: a reading. Grey: a missing reading. Dark grey: outside every class.
- Amber hatching: overlapping KP ranges. Blank: no KP range, or no reading at that depth.

Hover reports the investigation, its KP range, the value, class and the depth
the reading is drawn over. Click a range to plot that investigation's readings
with their source rows.

- **Check…** lists every investigation (depth range, samples, missing, min,
  max, KP ranges on this route) with route findings: IDs without measurements,
  investigations without a KP range, ranges beyond the route, overlaps and the
  share of the plan scope covered. **Export this table…** saves it as CSV.
- **Export cells…** writes every plotted cell (investigation, KP from/to,
  depth top/base, value, class, status) to CSV for an independent check.
- **Remove…** deletes the dataset and its stored measurements from the
  project; the source file and KP layer are not touched. Plans that showed it
  are listed first.

Numeric datasets are for display and checking; they do not change soil
classes or feed burial analysis.
