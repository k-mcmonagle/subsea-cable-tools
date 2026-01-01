# RPL / Cable Assembly Manager — Design Notes (Draft)

## Current State (Excel Import)
The Processing algorithm in `processing/import_excel_rpl_algorithm.py` imports an Excel RPL into **two layers**:

- **Points** (events / bodies / positions)
  - `PosNo`, `Event`, `DistCumulative`, `CableDistCumulative`, `ApproxDepth`, `Remarks`, `Latitude`, `Longitude`, `SourceFile`
- **Lines** (segments between consecutive points)
  - `FromPos`, `ToPos`, `Bearing`, `DistBetweenPos`, `Slack`, `CableDistBetweenPos`, `CableCode`, `CableType`, etc.

The import alternates rows: point row, then line row, then point row… and creates each line geometry as a straight segment between consecutive point coordinates.

This output is a good *starting place* for a managed RPL system because it already expresses the two key entities we must keep synchronized: ordered **nodes** and **segments**.

## Slack definitions (proposed)
Your description matches a common offshore cable interpretation:

- **Seabed length** is the 3D length along the seabed (i.e. along the route with vertical variation from bathymetry).
- **Area slack** is additional cable length relative to seabed length.
- **Bottom slack** is effectively ~0% (cable is assumed to lie on the seabed; no “free-hanging” slack to manage separately in this RPL context).

### Canonical formula (segment-level)
Let:
- $L_{sb}$ = seabed 3D length for a segment (meters)
- $s_{area}$ = area slack fraction (e.g. 0.02 for 2%)
- $L_{c}$ = cable length for a segment (meters)

Then:
- $L_{c} = L_{sb} \cdot (1 + s_{area})$
- $s_{area} = (L_{c}/L_{sb}) - 1$

The system should treat **one of these as authoritative** per segment:
- **Slack locked** (user edits slack; cable length is derived), or
- **Cable length locked** (user edits cable length; slack is derived).

## Key Implementation Decision: Canonical Truth
To keep point and line layers synchronized, we need a single canonical model.

Proposed:
- **Nodes (points)** are canonical for positions + ordering.
- **Segments (lines)** are derived from the ordered nodes, but keep the segment attributes.

This aligns with the imported data structure and supports an interactive editor where dragging a node updates adjacent segments.

## “Convert Imported RPL → Managed RPL” (Phase 1)
Add a tool (Processing algorithm or UI action) that takes:
- input points layer (from Excel import)
- input lines layer (from Excel import)
- optional output GeoPackage path

And produces a managed RPL container with:
- a points layer with added keys:
  - `node_id` (uuid/string)
  - `seq` (integer order)
  - `rpl_id` (optional)
- a segments layer with added keys:
  - `seg_id` (uuid/string)
  - `from_node_id`, `to_node_id`
  - `seq`
  - `length_mode` (e.g. `SLACK_LOCKED` default)

It should also run validation:
- points count >= 2
- line count == points-1 (or can be rebuilt)
- monotonic ordering rules (prefer `PosNo` when present, else feature order + geometry heuristics)

## Seabed 3D length computation (later phase)
To compute $L_{sb}$ we need bathymetry inputs (raster(s) and/or contours) and a sampling method.

Proposed approach:
- Densify each segment polyline to a configured step (e.g. 10–50 m along-geodesic).
- Sample depth at each vertex from raster(s) (or fallback to nearest contour interpolation).
- Compute 3D length between successive samples:
  - $\Delta s_{3d} = \sqrt{(\Delta x)^2 + (\Delta y)^2 + (\Delta z)^2}$
- Sum to get $L_{sb}$ per segment.

Store:
- `SeabedLen_m` (segment)
- `CableLen_m` (segment) (derived from slack or source fields)
- Update cumulative fields:
  - `DistCumulative` (prefer seabed chainage km)
  - `CableDistCumulative` (cable chainage km)

## Editing behaviour (initial default)
Initial safe behaviour for MVP:
- Dragging a node moves only that node.
- Adjacent segments are rebuilt geometrically.
- Per-segment `Slack` stays fixed (SLACK_LOCKED), so `CableLen_m` updates to match the new seabed length.

Later we can add propagation modes (anchor upstream/downstream, distribute, etc.).
