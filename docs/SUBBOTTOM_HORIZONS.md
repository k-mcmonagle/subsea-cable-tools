# Sub-bottom horizon sections

Open **Burial Planner → Ground Model → Horizons… → Import DXF…**.
Horizon lines overlay soil classes, numeric datasets and CPTs on the existing
KP / depth-below-seabed plot. They do not assign soil classes or change burial
calculations.

## Import

1. **Drawing & layers:** open an ASCII DXF section and tick the interpreted
   horizon layers. The drawing preview highlights selected layers. Each layer
   becomes a named horizon; separate entities remain separate segments, even
   when they overlap in KP. Grid and annotation layers should be left unticked.
2. **Coordinates:** set X and Y units independently. Header units are only a
   suggestion. Set the drawing X origin and its known KP, an optional horizontal
   scale multiplier, and direction. For absolute KP in kilometres, choose
   kilometres with both origins zero. For metre chainage starting at KP 12.5,
   choose metres and set KP at origin to 12.5.
3. Choose **Depth below seabed**, or **Relative to a vertical datum**. Set the Y
   origin, its depth in metres (positive down), vertical exaggeration to remove,
   and whether Y increases upwards. For ordinary elevations, use a datum
   reference, tick *Y increases upwards*, and leave both vertical origins zero.
4. For datum-relative lines, name the datum and select either a seabed DXF layer
   or the plan's current sampled bathymetry. Confirm the references match. A
   seabed DXF layer uses the same coordinate settings as the horizons. Seabed at
   −40 m and a horizon at −43 m give 3 m below seabed. Missing reference coverage
   and bathymetry source seams remain gaps; there is no extrapolation. Negative
   results are shown and flagged, rather than clamped to the seabed.
5. **KP reference:** use the existing current-route, RPL revision, constant-shift
   or matched-KP controls to place the source KPs on this plan.
6. **Preview:** inspect the resulting lines and target burial depth. Diagnostics
   describe skipped geometry, missing coverage and out-of-scope KPs. Changing
   any import setting disables Import until the next preview. **Import** adds
   the previewed lines to the Horizons dialog; **Apply** saves them to the plan.

## Manage and edit

Tick a horizon to show it, change its name/colour, or remove it. **Edit vertices…**
accepts one `KP (km), depth below seabed (m)` pair per line, with blank lines
between segments. KPs within a segment must run consistently in one direction.
Original DXF vertices and import settings remain saved alongside the edited
lines. Apply creates one change-log entry; use the existing change-log rollback
in **Review & Export** to undo it. Cancel discards the dialog's changes.

Changing the route or its KP numbering hides affected overlays and flags their
reference. Re-import for the new route. Resampling the seabed flags horizons
converted with the previous bathymetry; their saved depths remain fixed until
re-imported. The file used for import is not required to reopen saved horizons.

## Scope and project compatibility

Supported geometry is model-space `LINE`, straight `LWPOLYLINE`, and straight 2D
`POLYLINE`/`VERTEX` in an X/Y section. Binary DXF, blocks, curves/bulges, closed
polygons, rotated object coordinates, 3D geometry and folded/vertical section
lines are unsupported and reported. Export those interpretations as ordinary
2D straight polylines first. Geographic X/Y/Z horizon surfaces and SEG-Y are
outside this importer.

There are no new dependencies or changes to the GeoPackage schema. Horizons
use an optional, versioned `subbottom_horizons` entry in existing plan settings.
Existing plans without it read as having no horizons; opening/cancelling the
dialog does not write that entry. Apply preserves other plan settings and soil
units through the existing transactional save and change log. Plan duplication
copies the overlays, while edits remain scoped to the selected plan.

To keep these embedded overlays small, the importer accepts at most 50 MiB of
DXF, 50,000 source/converted vertices per import, 256 horizons per plan, and
100,000 saved vertices per plan counting originals. It reports limits rather
than silently simplifying interpretations.
