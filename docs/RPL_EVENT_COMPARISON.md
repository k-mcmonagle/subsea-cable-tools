# RPL event comparison (design vs as-laid)

Where did each event end up? The Cable Route Workbench's **Compare RPLs ▸ Events** tab and the
**Compare Design vs As-Laid Routes** Processing tool pair the events of RPL A (normally the design)
with the events of RPL B (normally the as-laid) and measure every pair against route A. Both use the
same matcher and the same report, so their results agree.

## Opening it

- Select a cable segment in the Workbench, open **Compare RPLs**, then **Events**. The newest design
  revision is compared with the newest as-laid revision by default; either side can be any RPL in the
  project (other segments are listed after this segment's revisions).
- Or right-click an RPL in the Workbench tree and choose **Compare with…**. This opens the comparison
  in its own window, with an as-laid RPL placed on the B side.

## How events are paired

An *event* is an RPL position whose Event field is not blank. Each event is classified by the
project's event rules (repeater, BU, equaliser, joint, transition, crossing, alter course, ...).

1. **Exact names**: names that are identical once case, spaces and punctuation are ignored
   (`BU-1` = `bu 1`). Names that occur once on each side become anchors.
2. **Similar names**: typos and appended notes are tolerated (`RPTR 1` ↔ `Repeater 1 S/N 4471`,
   `RPTR 2` ↔ `RTPR 2`). A *different number* is never treated as a typo: `RPT 12` does not pair with
   `RPT 13`.
3. **Same type nearby**: two events of the same type within the search radius (*Match within*,
   default 1000 m) pair even when the names share nothing.

Rules that apply throughout:
- A repeater never pairs with a joint, or with any other type that disagrees.
- Pairs keep route order, so an extra or missing event costs a gap instead of shifting every later
  pair. Untick *Keep route order* for RPLs whose events are not in the same order.
- An as-laid RPL recorded in the opposite direction is detected. ΔKP is then not reported, because
  the two chainages run in opposite directions.

Every pair records how it was made: *Exact name*, *Similar name*, *Same type, nearby* or *Manual*.
Similar-name and nearby pairs are shaded amber in the table and are worth a check.

## Correcting pairs

- Click a B event to pick its partner from a dropdown. The list is ordered nearest first and shows
  the distance and any event that already has a partner.
- Choose *(no match)* to leave an event unpaired.
- For an *Only in B* row, click its A cell to pick the A event it belongs to.

Corrections are saved with the QGIS project and re-applied the next time the same two RPLs are
compared. *Re-match* re-runs the suggestion with new settings and keeps your corrections; *Clear
corrections* forgets them.

## Choosing what to report

- *Show* presets: all events, cable bodies, repeaters, branching units, joints, transitions,
  crossings, or all except alter courses.
- *Types* lets you tick individual types.
- The text box takes plain text or a regular expression.
- Unticking a row leaves it out of the summary, exports and report.

## Offsets

All offsets are measured on route A (A's positions joined in order). The chainage is A's own KP when
every position has one, otherwise the distance along the positions.

| Measure | Definition | Sign |
|---|---|---|
| Along-track | KP on route A of B's event minus A's event KP | + ahead (towards increasing KP), − behind |
| Cross-course | perpendicular distance from route A | + starboard (right when facing increasing KP), − port |
| Radial | straight distance between the two events, with the true bearing A → B | always + |
| ΔKP | B's own RPL KP minus A's (chainage difference) | + B further along its route |

The Workbench measures on a local WGS84 tangent plane around each event, which is accurate well
below a metre at installation offsets. The Processing tool uses the plugin's ellipsoidal route frame,
the same one used by Nearest KP and the KP Mouse tool.

*Target radius* counts the events within that radial distance of the design position. It is drawn as
the dashed red ring on the radial plots and as dashed ±limits on the along-route chart.

## Outputs

- **Export CSV**: one row per selected event with the offsets, both positions and the match method.
- **Add offset lines to map**: a line from each A event to its B partner, carrying the offsets.
- **Report** (HTML, self-contained; print to PDF from the browser). It contains:
  - summary statistics: radial mean, RMS, 95th percentile and maximum, along/cross mean, and the
    count within the target;
  - a radial plot of all selected events;
  - cross-course and along-track offsets against KP;
  - statistics by event type;
  - the full event table;
  - one card per event, with its own radial plot.

Radial plots are route-relative (up = ahead, right = starboard) or north-up (*Plot* option).
Colours identify up to three event types; further types are drawn as "Other".
