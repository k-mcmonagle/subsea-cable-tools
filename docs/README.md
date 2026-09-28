# Developer documentation

Index of the design and history documents for Subsea Cable Tools. User-facing documentation is the top-level [README](../README.md); user-facing changes are in [CHANGELOG.md](../CHANGELOG.md).

## Design records

- [DECISIONS.md](../DECISIONS.md) — Burial Planner implementation decisions and judgement calls, one line of rationale each. It is the authoritative record of intended behaviour; the v0.3 specification it cites is not in the repository.

## Model notes (catenary and lay simulator)

- [catenary/MODEL_NOTES.md](../catenary/MODEL_NOTES.md) — Catenary Calculator (V1/V2): what the 2D model does and does not represent, equations, conventions and limits.
- [catenary/v3/V3_MODEL_NOTES.md](../catenary/v3/V3_MODEL_NOTES.md) — Cable Lay Simulator (3D): assumptions, validation status and what the tool must not be used for.
- [catenary/V3_PLAN.md](../catenary/V3_PLAN.md) — Cable Lay Simulator (3D): design and implementation plan.
- [catenary/v3/REFERENCE_DIGEST.md](../catenary/v3/REFERENCE_DIGEST.md) — equations and constants distilled from the lay-physics references.

## Reviews

Point-in-time reviews. Their file and line references describe the code as it was when written.

- [reviews/2026-09-slope-profile-review.md](reviews/2026-09-slope-profile-review.md) — slope and profile reliability review, 8 September 2026 (implemented).

## History

- [history/CHANGELOG-detailed-to-1.9.md](history/CHANGELOG-detailed-to-1.9.md) — the archived engineering-level change history up to 1.9.0, plus unreleased work to 28 September 2026. Not maintained; new entries go in [CHANGELOG.md](../CHANGELOG.md) (user-facing) and commit messages or DECISIONS.md (engineering detail).

## Adding documents

- Design decisions: add to DECISIONS.md, or a new `docs/` page linked from here.
- Reviews: `docs/reviews/YYYY-MM-<topic>.md`, with a status line under the title once acted on.
