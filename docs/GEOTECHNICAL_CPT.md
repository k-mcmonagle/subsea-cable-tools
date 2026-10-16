# CPTs from AGS files

Open **Burial Planner → Planning → Ground Model** and set **Display** to
**CPTs**. CPT imports belong to the project: every plan sees them, each plan
measures their KP on its own route, and they are not part of a plan's
history. The same files can be read outside the Burial Planner with the
Processing algorithm **Geotechnical ▸ Import AGS CPT Data**.

All derived values are screening values (beta — check outputs). The
contractor's interpretation and laboratory results take precedence.

## 1. Importing

**Import AGS…** opens three tabs, each with a check line that reports what
was read.

### AGS files

Add files or a whole folder. Files are recognised by their content, so a file
without an `.ags` extension is fine.

| Edition | CPT readings | Test details | Location | Interpretation |
|---|---|---|---|---|
| AGS 3.x | `STCN` | — | `HOLE` | — |
| AGS 4.0–4.1 | `SCPT` | `SCPG` (area ratio `SCPG_CAR`) | `LOCA` | `SCPP` (per layer) |
| AGS 4.2 | `CPTT` | `CPTG` (`CPTG_CAR`, zero reading location, termination) | `LOCA` | `CPTP` (per depth) |

- **Units.** Readings are converted to m for depth, MPa for qc and qt, and
  kPa for fs and pore pressure. The unit stated in the file is used. Where a
  heading has no unit, the edition's default is assumed (MN/m² and kN/m² in
  AGS 3, MPa in AGS 4) and a warning is shown.
- **One file, many tests.** A file may hold one CPT or many. Several pushes at
  one location are kept apart: by test number in AGS 4, or where the depths
  restart in AGS 3.
- **Depths.** Repeated depths and negative pore pressures are kept as
  delivered.
- **Damaged lines.** A value with an unescaped quote (such as `51°46'47.4"`)
  is recovered from the quoted commas and noted.
- **AGS 3 pore pressure.** AGS 3 does not say where its pore pressures were
  measured. `STCN_PWP1` is read as **u2** (shoulder, the usual piezocone
  position) unless you choose u1 or u3. Check the contractor's report.
- **Naming.** Choose how each CPT is named. The automatic choice uses the
  **file name** when every file holds one location, and the **AGS location
  ID** otherwise. Location IDs such as `0001` are often not the CPT name; the
  file name is.

### Positions and summary (optional)

Offshore deliveries often keep positions, water depth, penetration and the
interpreted layering in a separate table, one row per CPT. Choose the CSV or
XLSX file, the worksheet and the header row. The preview starts at the
fullest of the first rows, so title rows above the header are skipped. Then
choose the columns:

| Item | What it reads |
|---|---|
| CPT name * | Joined to the AGS CPT names, exactly. Optionally also ignoring case, spaces and punctuation, when that is unambiguous (reported). |
| Latitude / longitude | Decimal degrees or degrees and minutes (seconds): `52° 20.220' N`, `N 52 20.220`, `52:20:13.2S`, `-52.337`. |
| or easting / northing | With the CRS you choose. |
| Water depth, penetration, date, time, summary | As given. A water depth's sign is ignored. |

A table column name that appears twice is refused: rename one of the columns.
| Horizon descriptions, classes, interface depths | The **first** column of each numbered family (`Horizon description 1`, `Class 1`, `Interface depth (m) 1`). The others are found from the header. Horizon *k* runs from interface *k−1* (the seabed for the first) to interface *k* (the penetration for the last). |

Nothing is chosen for you unless you press **Suggest columns from headers**.
Check every suggestion.

**Where a position comes from**, in order:

1. the table;
2. AGS latitude / longitude, taken as WGS84 (`LOCA_LLZ` is kept but not used
   to transform);
3. AGS easting / northing, in the CRS the file states as an EPSG code
   (`LOCA_NSRI`, AGS 4.2), or else in the CRS you choose;
4. AGS local x / y (`LOCA_LOCX` / `LOCY`), in the CRS you choose. Some AGS 3
   deliveries put grid coordinates there.

**Water depth** comes from the table, else `LOCA_WDEP`, else `CPTG_WAT` for a
test zeroed at the seabed, else a negative ground level.

**Strata** come from the table's horizons, else from the AGS `GEOL` group.

### Check and register

Every CPT is listed with:

- its position and where it came from;
- water depth, penetration, readings and channels;
- its KP and offset on this plan's route.

Give the import a name and, optionally, Input Data Register details:
originator, revision or report, status, received date, quality and notes.

## 2. Managing

The CPT table lists each CPT with:

- **KP** and **offset** (port / starboard of increasing KP) on this plan's
  route;
- whether it is **placed**;
- water depth, penetration, readings, maximum qc and the summary.

Hover a name for the file and its warnings.

| Action | What it does |
|---|---|
| **Edit…** | Changes an import's files, table, columns or register details. |
| **Reload** | Re-reads the files with the same choices (the status line says when a file has changed). |
| **Remove…** | Deletes the import from the project; the files are not touched. |

An import whose register **Status** is *superseded* stays listed, marked
*superseded import*, but is not placed, drawn or used by colour sections.
When two current imports contain a CPT of the same name, colour sections
name each one with its import, for example `CPT01 [Rev 1 CPTs]`.
| **Settings…** | Placement for this plan, and derived-parameter assumptions for the project. |

**Storage.** CPTs are stored in the project's burial-plans GeoPackage:

- the readings as delivered (in the units above) and positions in WGS84;
- a **CPT locations** point layer (`bp_cpt_locations`, in the Burial Planner
  group), labelled by name.

Derived values are not stored: they are computed when shown, so changing a
setting changes every plot at once.

The locations layer is an ordinary layer. Identify it, label it, or register
it under **Inputs** for a Risk Profile check, for example CPTs whose
penetration stopped short of the target depth.

## 3. Placement on the route

A CPT is **placed** when it lies within **Use CPTs within** (250 m by default)
of the plan's route. Its KP is that of the nearest point on the route.

A placed CPT represents the route up to **Each CPT represents up to** (100 m
by default) either side of its KP, and never past halfway to the next placed
CPT, so ranges never overlap. Nothing is interpolated between CPTs: a
stretch with no CPT stays blank.

Placement is measured again whenever the plan's route changes.

## 4. On the plots and the map

- **Ground Model plot.** Tick **CPT traces** in any display (soil classes,
  numeric datasets or CPTs). Each placed CPT is drawn at its KP as a trace of
  the value you choose (qc, qt, fs, u, Rf, qnet, Bq, Ic, Su, Dr), down to its
  deepest reading, with an SBTn colour strip and its name. A trace's full
  width is the value shown in the legend.
  - Hover a trace for the readings at that depth.
  - Click it to open the CPT's log.
- **Bathymetry profile.** Placed CPTs are marked on the seabed and named in
  the readout (**Overlays ▸ CPT locations**).
- **Map.** Selecting a CPT in the table rings it on the map (panning there if
  it is out of view) and marks its KP on the route and the Ground Model plot.
  Selecting a CPT on the map selects it in the table.

## 5. CPT logs

**Logs…**, a double-click or a click on a trace opens the CPT log window.

**Tracks.** Side by side on one depth axis (depth below seabed, downward):

- qc and qt;
- fs;
- u and u₀;
- Rf;
- Ic, with the SBTn zone boundaries;
- Su, with the BS 5930 / ISO 14688-2 strength terms behind it;
- and, from **Tracks ▾**, Dr, Bq, inclination and σ'v0.

**Beside the tracks** are the strata (classes coloured as in **Classes…**,
descriptions wrapped) and the SBTn strip. The plan's target burial depth is
drawn across every track.

**Su band.** Su is drawn twice: with Nkt 15 (solid) and with the lower
estimate at Nkt 20 (dashed).

**Delivered interpretation.** Values delivered in the AGS file (`SCPP` /
`CPTP` Su, Dr, Ic) are drawn as orange steps for comparison.

**Using the log window:**

- **Crosshair.** One crosshair spans every track. The readout lists the
  stratum and each shown CPT's values at that depth.
- **Pan and zoom.** Drag to pan and use the wheel to zoom. Depth zoom is
  shared by every track; value zoom is per track. **Fit** resets the view.
- **Scale.** Any track can use a logarithmic scale (**Tracks ▾**). Right-click
  a track for pyqtgraph's axis options.
- **Compare ▾.** Overlays up to eight other CPTs, one colour each.
- **◀ ▶.** Steps along the route.
- **SBT chart.** Plots the readings on the Robertson normalised chart (Qtn
  against Fr, Ic boundaries).
- **Readings.** Lists every measured and derived value.
- **Export.** **Export CSV…** and **Save image…** save the readings and the
  plot.

## 6. Colour sections

**Colour section…** adds a numeric dataset from a CPT value (see
[Numeric datasets](GROUND_NUMERIC_PROFILES.md)). Each CPT's value is averaged
over 0.05 m depth intervals, which you can change. CPTs often repeat a depth
at 1 cm resolution.

The dataset is placed at the CPT positions, read live. It copies the
**Use CPTs within** and **Each CPT represents up to** values when it is
created; change them in the dataset's **KP ranges** tab. **Reload** brings
its values up to date after a re-import or a settings change.

Su and qc datasets start with the BS 5930 strength terms or the indicative
qc density terms as colour classes. Edit them before saving if you need
others.

## 7. Derived parameters

These follow Robertson & Cabal, *Guide to Cone Penetration Testing* (7th
edition, 2022). The project settings (**Settings…**) are shown in brackets
with their defaults.

| Value | Formula |
|---|---|
| qt | qc + u (1 − a). u is u2 when delivered (**pore pressure for qt**). a is from the file, else the **cone area ratio** (0.75). |
| Rf | fs / qt × 100 % |
| σv0, u0 | Σ γ Δz and γw z for readings **zeroed at the seabed** (offshore practice). For a test zeroed on deck (`CPTG_RLOC = S`), the water column γw × water depth is added to both. γ is the **constant unit weight** (18 kN/m³), or **estimated** from the CPT as γ/γw = 0.27 log Rf + 0.36 log(qt/pa) + 1.236, limited to 14–22 kN/m³. γw is 10.05 kN/m³ (sea water). |
| qnet, Bq, Fr | qt − σv0; (u − u0) / qnet; fs / qnet × 100 % |
| Qtn, Ic | Qtn = (qnet/pa)(pa/σ'v0)ⁿ with n = 0.381 Ic + 0.05 σ'v0/pa − 0.15 ≤ 1, iterated. Ic = √((3.47 − log Qtn)² + (log Fr + 1.22)²). pa = 100 kPa. |
| SBTn zone | 7 \| 6 \| 5 \| 4 \| 3 \| 2 at Ic 1.31, 2.05, 2.60, 2.95, 3.60. Zones 1, 8 and 9 need the full chart and are not assigned. |
| Su | qnet / **Nkt** (15), and a lower estimate qnet / **Nkt for the lower Su** (20), where qnet > 0. By default it is left out where the reading is coarse-grained (Ic < 2.6); where Ic cannot be found it is still given. |
| Dr | √(Qtn / 350), for young clean sand. Only where Ic < 2.6 by default. |

**Limits to keep in mind:**

- **Undefined values.** Where fs is zero (common when fs is delivered to
  0.001 MPa), Rf, Fr and Ic are undefined, so Su is given without the
  fine-grained check and Dr is not given. The import warns when that applies
  to many readings.
- **At the seabed.** Near the seabed σ'v0 tends to zero, so Ic is undefined
  at 0 m.
- **Not analysis inputs.** CPT values are for display and checking. They do
  not change soil classes or feed the Exclusions or Plan Builder.
