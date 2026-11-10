# Translate KPs and ranges between RPLs

Open **Processing Toolbox → Subsea Cable Tools → RPL Tools → Translate KPs and Ranges Between RPLs**.
The KP Mouse Tool also offers **Translate KPs to Another RPL…** on right-click. This opens the same algorithm,
prefilling the old route, its KP basis and the clicked KP.

## A highlighted or grouped range layer

1. Select the old and new RPL line layers. Set each route's start KP and distance mode to match the basis
   used to create the input. Defaults follow KP settings; start KP defaults to zero.
2. Choose **KP fields in a layer or table** and your highlighted layer.
3. Select `start_kp` and `end_kp`. This mode uses the numbers, regardless of the input geometry's shape or
   vertex order. It works with grouped multipart features and tables without geometry.
4. Request **Accepted ranges on new RPL** to rebuild the highlights. Request **Input geometry with
   translation results** to retain the existing shapes instead, or request both for comparison.
5. Inspect the results table, especially rows marked `review` or `invalid`.

Group labels and all other original fields are carried into each output. Grouping is not recalculated.
Existing `start_kp` and `end_kp` remain the old values; the new values are in `new_start_kp` and `new_end_kp`.
Use those new fields in subsequent KP operations. Result-name collisions receive numbered suffixes,
reported in the Processing log. A new output layer does not inherit the original layer's renderer or layer-tree group.

## Single KPs, lists and geometry

For a single-KP field, leave **End KP field** empty. Input can be a point layer, line layer or non-spatial table.
KP-field mode deliberately ignores geometry, including missing geometry.

For pasted input, choose **Pasted KP list**, then explicitly choose **Single KPs** or **Start / end KP ranges**.
One record goes on each line. Set **Pasted list has a header row** when appropriate. Examples:

```text
1.250
2+345
7.125
```

```text
start,end,group
1.250,2.345,Sand
4.000,5.500,Rock
7.125,7.125,Crossing
```

Equal range endpoints become points. Negative KPs and `12+345` notation are supported. Use decimal points
and no thousands separators. Columns can be tab, comma, semicolon, pipe or whitespace separated; use CSV
quotes for labels containing commas. Additional columns are preserved. Header/type choices are explicit so
an invalid first row is not mistaken for a header, or a numeric point label for an end KP. Bad rows stay in Results.

Choose **Geometry projected onto old RPL** when the positions or line shapes are authoritative and no KP fields
are available. It derives KPs from single points or single-part line endpoints on the old route. It also samples
line interiors for offset and direction checks. It then translates those old KPs using exactly the same calculation
as numeric input. It does not project an off-route geometry directly onto the new RPL: use **Nearest KP** for that.
Multipart inputs must use explicit KP fields because their endpoint order may be ambiguous.

Numeric input is just as well defined as geometry input when its KP basis is known: the old RPL supplies the
positions. Any rounding in the delivered KPs remains a limit on the original boundary positions.

## Calculation and review

Each boundary follows **old KP → position on old RPL → nearest position on new RPL → new KP** using
the shared `RouteFrame` model. See [KP method](KP_METHOD.md). This uses measured chainage, not the KPs
printed at intermediate RPL positions. Geodesic mode uses WGS84. Grid mode accepts separate explicit projected
CRSs for the two RPLs; blank uses the existing KP-settings fallback. Source and destination layer CRSs may differ.
Route feature order follows complete `SeqNo` values, otherwise layer order. Routes must be continuous:
disconnected parts are rejected so rebuilt ranges cannot acquire fictitious connecting legs.

Results retain full calculation precision. Endpoint correspondence is preserved for descending input ranges;
the resulting line geometry follows increasing new KP. Results includes every processed row and its original
attributes, source/target route references, endpoint DCC in metres, sampled maximum separation, range length change,
return-projection KP error, status and flags. `input_id` identifies the input feature or pasted data-row number.

| Status | Meaning |
|---|---|
| `accepted` | No configured diagnostic failed; optional new-route geometry is produced. |
| `review` | Candidate KPs are retained, but no new-route geometry is produced. |
| `invalid` | Invalid/missing KP, out-of-extent KP, unsupported geometry or calculation failure; reason retained. |

Optional original-geometry output retains all processed rows, including review/invalid rows.

Default checks sample every 50 m and at KP-path vertices, including both boundaries:

- Separation above 25 m, or input geometry more than 25 m from the old route.
- Range length change above 10%.
- New positions projected back to the old route differ by more than 1 m in KP.
- Reversed, collapsed or non-monotone mappings; adjacent sample KP changes greater than five times the
  source interval (with a 1 m minimum jump threshold).
- Off-route positions snapping to the new route's terminal endpoints.

Thresholds and spacing are parameters. The maximum separation is **sampled**, not a guaranteed continuous
maximum. Acceptance does not prove unique engineering correspondence at crossings or nearby parallel branches.
A substantial reroute may have no unique equivalent range. Review those cases against the route revisions;
this tool does not invent anchors, interpolate across divergence, extrapolate or silently clip out-of-range KPs.
Floating-point roundoff at an exact route endpoint is handled without changing the input KP.
