# KP and bathymetry methods

These notes describe how Subsea Cable Tools currently measures KP and derives depth and slope values. They describe calculation methods, not a validation of any survey or an engineering acceptance criterion. Check source data, coordinate and vertical reference systems, resolution, coverage and results before using an output for a decision.

## KP and coordinate reference systems

- **Chainage:** The default KP distance is the sum of WGS84 ellipsoidal lengths between stored route vertices, independent of the QGIS project's ellipsoid setting. KPs are in kilometres. Multi-feature routes follow SeqNo or layer order; the plugin does not reorder or node their geometry. An RPL supplies its start KP; its stated intermediate KPs are checked against measured chainage rather than used to locate positions.
- **Grid option:** **Subsea Cable Tools ▸ KP settings…** changes the plugin-wide distance mode to Cartesian measurement in a selected projected CRS. Without a selected CRS, it uses a projected CRS from the route or project settings, or a route UTM zone. Burial plans retain their own KP mode so that a global settings change does not renumber an existing plan. Confirm which distance convention the source RPL uses before comparing KPs.
- **Geometry and CRS:** KP positions follow the route's stored segments. The tools measure against the geometry's layer CRS and transform inputs with different CRSs where supported; **Translate KP Between RPLs (Points)** requires matching layer CRSs. A CRS assignment or transformation error can affect every derived KP.

## Depth sources

- Source settings specify depth or elevation, metres or feet, an optional datum label, sampling mode and an optional native cell size. Values are normalised to metres and positive-down depth. Automatic vertical-convention detection is a convenience; set the convention explicitly for near-zero or mixed-sign data. A datum label records provenance but does **not** transform vertical datums. Check that overlapping surveys use compatible datums.
- Raster profiles use no-data-aware bilinear interpolation at native cell centres by default; raw nearest-cell sampling is available for inspection. Bilinear interpolation requires valid contributing cells and does not restore detail absent from the grid. The larger native cell dimension, converted to metres, is used as a minimum slope-support scale.
- Plugin-created MBES mosaics have a .sources.json companion so profiles can read the original grids, finest first, with their resolution and source boundaries. Keep that file and its source grids available. Older mosaics without provenance need a manually selected native cell size; a fine output pixel does not establish fine survey resolution.
- Contour profiles keep exact route crossings and interpolate linearly between valid bracketing observations. Conflicting depths at the same crossing and missing coverage are treated as gaps. Contours do not reveal terrain between crossings.

## Slope and length

- **Sign and units:** Longitudinal slope is atan2(-change in positive-down depth, horizontal chainage) in degrees. Positive means shoaling with increasing KP; Depth Profile can invert its reported longitudinal sign. Cross tilt is positive when the starboard side is deeper. Burial checks account for travel direction. Horizontal and vertical distances are converted to metres before calculating angles.
- **Longitudinal support:** Automatic raster slope uses a physical baseline of at least two native cells and two median station intervals within each continuous source run. An explicit baseline is fixed in metres; a run too short for the full baseline, or a baseline below two native cells, has no supported slope. At a run's end, the full baseline shifts inward. Missing values and changes of raster source break the series. Contour slopes use intervals between supported crossings.
- **Terraced rasters:** A repeated-terrace pattern can widen the automatic averaging baseline to reduce artificial cell-edge spikes. This is a heuristic about the sampled pattern, not a measurement of native resolution. It can also average real repeated terrain. Inspect the source or choose an explicit baseline when the result matters.
- **Cross slope:** Overall cross tilt is the endpoint difference across the requested width. Maximum local cross slope is a separate value from the resolved transect, subject to coverage and source checks. Either can miss terrain smaller than the survey's effective resolution; overall tilt can be small across a symmetric trench with steep sides.
- **Seabed length:** This is the sum of sampled depth changes and route-chainage steps within continuous valid coverage from one source. Missing sections and source changes are excluded, and covered plan length is reported separately. It is an estimate at the chosen sampling scale, not the unresolved seabed's true length.

## KP Mouse quick profile

Start a range line and press **D** for its depth and slope profile. **Space** or **Freeze** holds the line and plot for inspection. **True scale 1:1** uses physical horizontal and vertical units; vertical exaggeration is optional. Right-click the depth plot to measure between two endpoints or export a PNG or CSV. The endpoint angle is a straight-line measurement, not the maximum slope between those points.

The optional nearest-route-KP ruler labels a physical-distance axis with projected route KPs. Labels can repeat or be unevenly spaced on oblique or curved lines. Exported measurements and slope baselines use explicit units.

## Interpreting the results

The sign conventions, unit conversions, gap handling and baseline calculations above are deliberate and have synthetic tests. The appropriate analysis width depends on the survey, the feature or equipment of interest, and the decision being made. No synthetic test establishes accuracy against an actual survey or another engineering workflow. Compare critical outputs with original survey data, known control cases and an independent method. See the [catenary model notes](../catenary/MODEL_NOTES.md) and [simulator model notes](../catenary/v3/V3_MODEL_NOTES.md) for those tools' separate assumptions.

For background on why raster scale and units matter, see the [GDAL slope documentation](https://gdal.org/en/stable/programs/gdal_raster_slope.html) and [USGS bathymetric uncertainty example](https://pubs.usgs.gov/ds/514/uncertainty.html).
