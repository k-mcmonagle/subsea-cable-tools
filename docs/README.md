# Developer documentation

The top-level [README](../README.md) is the short introduction and tool list. User-facing calculation assumptions are in [KP and bathymetry methods](KP_AND_BATHYMETRY.md); release changes are in [CHANGELOG.md](../CHANGELOG.md).

## Methods

- [RPL event comparison](RPL_EVENT_COMPARISON.md) — pairing design and as-laid events, offset conventions, filters and the report.

- [Numeric Ground Model datasets](GROUND_NUMERIC_PROFILES.md) — measurements, live KP ranges, colour classes, checks and export.

- [KP and bathymetry methods](KP_AND_BATHYMETRY.md) — KP distance, CRS, bathymetry sampling, slope and coverage conventions.

- [Lay Assessment](LAY_ASSESSMENT.md) — suspension, loop-risk and tension checks on lay data; the seabed rest model and its validation.

## Design records

- [DECISIONS.md](../DECISIONS.md) — Burial Planner implementation decisions and judgement calls. It is the authoritative record of intended behaviour; the v0.3 specification it cites is not in the repository.

## Model notes (catenary and lay simulator)

- [catenary/MODEL_NOTES.md](../catenary/MODEL_NOTES.md) — Catenary Calculator (V1/V2): model assumptions, equations and limits.
- [catenary/v3/V3_MODEL_NOTES.md](../catenary/v3/V3_MODEL_NOTES.md) — Cable Lay Simulator (3D): assumptions and validation status.
- [catenary/V3_PLAN.md](../catenary/V3_PLAN.md) — Cable Lay Simulator (3D): design and implementation plan.
- [catenary/v3/REFERENCE_DIGEST.md](../catenary/v3/REFERENCE_DIGEST.md) — equations and constants distilled from the lay-physics references.

## History

- [history/CHANGELOG-detailed-to-1.9.md](history/CHANGELOG-detailed-to-1.9.md) — archived engineering-level history up to 1.9.0. New user-facing changes go in [CHANGELOG.md](../CHANGELOG.md).
