# Developer documentation

The top-level [README](../README.md) is the short introduction and tool list. User-facing calculation assumptions are in [KP and bathymetry methods](KP_AND_BATHYMETRY.md); release changes are in [CHANGELOG.md](../CHANGELOG.md).

## Methods

- [RPL event comparison](RPL_EVENT_COMPARISON.md) — pairing design and as-laid events, offset conventions, filters and the report.

- [Sub-bottom horizons](SUBBOTTOM_HORIZONS.md) — DXF section import, coordinate calibration, overlays and editing.

- [Numeric Ground Model datasets](GROUND_NUMERIC_PROFILES.md) — measurements, live KP ranges, colour classes, checks and export.

- [CPTs from AGS files](GEOTECHNICAL_CPT.md) — AGS 3 / AGS 4 CPT import with a position and summary table, placement along the route, traces, logs and the derived parameters.

- [KP method statement](KP_METHOD.md) — how KP is defined and calculated (geodesic and grid), assumptions, accuracy and matching other systems.
- [KP and bathymetry methods](KP_AND_BATHYMETRY.md) — KP distance, CRS, bathymetry sampling, slope and coverage conventions.

- [Lay Assessment](LAY_ASSESSMENT.md) — suspension, loop-risk and tension checks on lay data; the seabed rest model and its validation.

- [Route Comments](ROUTE_COMMENTS.md) — capturing comments from the KP Mouse Tool, route references and their conditions, the register GeoPackage, import and export.

- [Span & Slack Analysis](SPAN_ANALYSIS.md) — planning a lay against spans and loops: the shared cable-on-seabed engine (rest shape, friction and lay history, slack rule, detail model with EI), validation and references.

## Design records

- [DECISIONS.md](../DECISIONS.md) — Burial Planner implementation decisions and judgement calls. It is the authoritative record of intended behaviour; the v0.3 specification it cites is not in the repository.

## Model notes (catenary)

- [catenary/MODEL_NOTES.md](../catenary/MODEL_NOTES.md) — Catenary Calculator V2 and its Steady Lay tab: model assumptions, equations and limits.
- [catenary/REFERENCE_DIGEST.md](../catenary/REFERENCE_DIGEST.md) — equations and constants distilled from the lay-physics references.

## History

- [history/CHANGELOG-detailed-to-1.9.md](history/CHANGELOG-detailed-to-1.9.md) — archived engineering-level history up to 1.9.0. New user-facing changes go in [CHANGELOG.md](../CHANGELOG.md).
