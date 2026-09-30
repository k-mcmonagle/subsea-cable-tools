# Developer documentation

The top-level [README](../README.md) is the short introduction and tool list. User-facing calculation assumptions are in [KP and bathymetry methods](KP_AND_BATHYMETRY.md); release changes are in [CHANGELOG.md](../CHANGELOG.md).

## Methods

- [KP and bathymetry methods](KP_AND_BATHYMETRY.md) — KP distance, CRS, bathymetry sampling, slope and coverage conventions.

## Design records

- [DECISIONS.md](../DECISIONS.md) — Burial Planner implementation decisions and judgement calls. It is the authoritative record of intended behaviour; the v0.3 specification it cites is not in the repository.

## Model notes (catenary and lay simulator)

- [catenary/MODEL_NOTES.md](../catenary/MODEL_NOTES.md) — Catenary Calculator (V1/V2): model assumptions, equations and limits.
- [catenary/v3/V3_MODEL_NOTES.md](../catenary/v3/V3_MODEL_NOTES.md) — Cable Lay Simulator (3D): assumptions and validation status.
- [catenary/V3_PLAN.md](../catenary/V3_PLAN.md) — Cable Lay Simulator (3D): design and implementation plan.
- [catenary/v3/REFERENCE_DIGEST.md](../catenary/v3/REFERENCE_DIGEST.md) — equations and constants distilled from the lay-physics references.

## History

- [history/CHANGELOG-detailed-to-1.9.md](history/CHANGELOG-detailed-to-1.9.md) — archived engineering-level history up to 1.9.0. New user-facing changes go in [CHANGELOG.md](../CHANGELOG.md).
