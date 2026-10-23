# -*- coding: utf-8 -*-
"""Help tab text for Span & Slack Analysis (also in ``docs/SPAN_ANALYSIS.md``)."""

HELP_HTML = """
<h2>Span &amp; Slack Analysis</h2>
<p>This tool models a cable laid along a route over a known seabed, in 2D (a vertical plane along the route).
It shows where the cable spans clear of the seabed (too little slack, too much tension), and where surplus cable
can form loops (too much slack at low tension). Change the bottom tension or slack and see the effect at once.
Use it to plan a lay, to set tension or slack by section, and to study a feature such as a crossing or a boulder.
For as-laid data, use the Lay Assessment in the Cable Lay Data Explorer. Both tools use the same seabed model.</p>

<h3>Workflow</h3>
<ol>
<li><b>Route and KP.</b> Select a Cable Route Workbench RPL, a line layer (with its start KP), a CSV profile, a
DXF profile drawing, or a synthetic seabed. For a DXF, choose the layer that holds the seabed line, what X is
(chainage from a start KP, or KP in km or m), whether Y is elevation or depth, and the drawing's vertical
exaggeration. Set a KP window to study part of a long route.</li>
<li><b>Seabed.</b> Tick the MBES rasters (or choose contours). The list follows the project's layers;
<i>Along the route</i> ticks the rasters the route crosses, and a search box appears when there are many.
Under <i>Sampling and filtering</i>, <i>Sample every: auto</i> samples each grid at its cell size, the noise
filter (auto by default) removes sounding noise but keeps crests, and <i>Suit the data</i> puts these settings back
to suit the data (see <i>Seabed data</i> below). Under <i>Additional features</i>, add a boulder, a pipeline crossing, a berm or mattress, a depression, or any other shape by KP. For
a complex shape such as a rock berm, choose <i>Other (custom shape)</i> and give its points as "offset height"
pairs from the KP along the route (e.g. <i>-10 0; -5.5 1.5; -2 1.8; 2 1.8; 5.5 1.5; 10 0</i>), or take them from a
DXF cross-section with <i>Shape from DXF</i>. Every table has <i>Paste</i> (from a spreadsheet), <i>Import</i> and
<i>Export</i> (CSV).</li>
<li><b>Cable and bodies.</b> Use a cable type from the Cable Type Library, types by KP section, the RPL's cable
type per leg, or a Workbench assembly fitted to the RPL (its cable sections and bodies land at KPs through the
RPL slack). Workbench labels such as "SA" resolve through the library: a project mapping, then a library name,
alias or generic type. Where several library types could mean a label, the mapping table in step 3 says so:
choose the type there. The mapping is saved in the QGIS project and the Lay Assessment uses the same one. Under
<i>In-line bodies</i>, add repeaters, joints and branching units by KP: weight in water, rigid length and diameter.
Under <i>Protection</i>,
add protection over a KP range (e.g. polymer half-shells or articulated pipe): its added weight in water, outside
diameter, added EI and its own MBR, from the protection's data sheet.</li>
<li><b>Friction.</b> One axial coefficient for the route (type it or use the slider), or soil zones by KP with a
table of coefficients for each cable type (or body kind) and soil. The model is 2D: the lateral coefficient is used
only for the alter-course note (the turn radius the cable holds); nothing moves the cable sideways.</li>
<li><b>Lay control.</b> Bottom tension (kN), bottom slack (% over the seabed), slack over the route (%, as in an
RPL), or the RPL slack of each leg. Override the value over KP ranges under <i>By KP section</i>, where
<i>Held</i> holds the cable over a range (buried by a plough) or at a point (an anchor: KP to = KP from), at the
tension in Value. <i>Hold plough sections</i> adds them from the RPL's PLDN / PLUP events or the Burial Planner
plan. Set the lay direction. <i>Find the highest
tension within limits</i> opens the Find tension tab.</li>
<li><b>Run.</b> The first run samples the seabed (in the background). After that, a change to the lay control or
the friction updates the results in about a second: move a slider and see the spans grow or shrink. The view stays
where you zoomed it (a run over other KPs shows the whole profile).</li>
<li><b>Read the results.</b> The status bar shows the worst finding by KP: hover it for the findings at a KP,
click it to go to the worst of them. Spans are filled by level: red,
amber, and light grey where the cable is clear of the seabed but under the reporting limits (not reported).
Click a finding or a span to zoom the profile and highlight it on the map; click a column heading to sort a
table. Hover the profile to move a marker along the route on the map; hover or click a body for its details.
Use <i>Pick KP on map</i> to go from the map to the profile.</li>
<li><b>Events.</b> The lane above the profile labels, with a leader down to the cable, the RPL's events (PLDN,
repeaters, crossings...), what is in the model, Route Comments on this route, the Burial Planner plan for this RPL
and your own markers, all at the analysis's KPs. A label is filled where the model carries the item, and hollow and
dashed where it does not (an RPL repeater with no body in the model, a crossing with no seabed feature: these are
also information findings). Comments and plans made on another RPL, start KP or KP convention are left out, never
shifted: the Events tab says how many and why. Right-click the profile to add a marker or a Route Comment; hover a
label for its details; double-click a comment to open it, or a marker to edit it. The <i>Events</i> menu chooses
what shows and how it is labelled.</li>
<li><b>Arrange the view.</b> Drag the handles between the rows to share the height. <i>Fit</i>, or the
<i>A</i> at the bottom left of a zoomed plot, shows the whole profile. <i>True scale (1:1)</i> shows slopes and
spans as they are. <i>Plots</i> shows or hides each plot, the sea level, the previous run and legends, chooses
the slack basis and the cable colour (slack state and loop risk, tension as % of NPTS, or what sets the tension, in
fixed classes), reverses KP (right to left), and restores the <i>Default view</i>. <i>Tooltip</i> turns the tooltip at
the cursor on or off and chooses what it shows (the line below the plots shows everything). <i>Measure</i> gives
two-point lengths, heights and angles, snapped to the seabed or the cable, as in the Depth Profile tool.</li>
<li><b>Look closer.</b> <i>Detail (EI)</i> solves a span or the visible window with bending stiffness: bend radius,
moment and seabed reaction along the cable. <i>Find tension</i> finds the highest bottom tension that keeps the
spans within a target (see below); <i>Use for the lay control</i> sets it. Below it, <i>Spans against tension
(sweep)</i> lays the cable at a range of bottom tensions and plots the span length against tension.</li>
<li><b>Save.</b> Scenarios (all settings and the sampled seabed) save in the project's Span Analysis GeoPackage.
<i>Add to map</i> writes the findings as a line layer. <i>Export</i> writes CSV files, a PNG of the plot, or a DXF
of the scene: the seabed, the cable, spans by level, supports, bodies, holds, features, sea level, the KP axis and the
tension, slack and slope graphs, each on its own layer (SA_SEABED, SA_CABLE, SA_SPAN_RED ...), in metres with
X = KP x 1000 and Y = elevation x the vertical exaggeration you choose.</li>
</ol>
<p><b>Examples.</b> <i>Examples</i> loads a synthetic case and runs it. The first is a verification case: a flat
seabed with a 40 m wide, 2 m deep depression and a light cable (10 N/m). The cable sags into the depression as a
parabola with sag <i>w b&sup2; / 8T</i>. At 2 kN the sag (1 m) is less than the depth, so one span bridges the
depression. Below 1 kN the cable touches the bottom, and two spans form, each <i>&radic;(2 d T / w)</i> from the
rim. The Summary tab compares the model with these values. Move the tension and see the change.</p>

<h3>The model</h3>
<p><b>Rest shape.</b> A cable with tension <i>T</i> and submerged weight <i>w</i> per metre rests on a rigid
seabed. With small slopes, <i>H y'' = w - p</i>, with <i>p</i> &ge; 0 (seabed reaction), <i>y</i> &ge; seabed and
<i>p (y - seabed) = 0</i>. This is the unilateral contact problem of Binda &amp; Contro (1979). The solution is the
lowest curve above the seabed whose curvature does not exceed <i>w / T</i>. A free span is a parabola with sag
<i>w L&sup2; / 8T</i>. The tool solves the problem exactly in one pass (an upper concave hull), so a 100 km route at
1 m solves in about a second. The curvature uses the catenary value <i>w / (T cos&sup2;&phi;)</i>, with &phi; the
cable slope, so spans on slopes stay close to the catenary. All lengths are exact.</p>
<p><b>What the cable can follow.</b> At tension <i>T</i> the cable follows a hollow only if the hollow's radius is
more than <i>T / w</i>. It bridges sharper hollows. A light telecom cable (about 5 N/m) at 5 kN bridges any hollow
sharper than a 1 km radius. This is why low tension (or slack) is needed on rough seabed.</p>
<p><b>Tension and friction (lay history).</b> Along the cable, <i>dT/ds = w dy/ds - f</i>, with Coulomb friction
|<i>f</i>| &le; &mu;<i>N</i> on the seabed and <i>f</i> = 0 in a span (so <i>T - w y</i> is constant in a span). The
seabed reaction <i>N</i> includes the capstan term <i>T &kappa;</i> where the cable bends over a crest. The cable is
laid progressively: at each touchdown it lands at the lay tension, and friction holds the tension of cable already
on the seabed (Zajac 1957, residual tension on a rough bottom). The tension changes only where friction cannot hold
it:</p>
<ul>
<li>a step in lay tension decays at &mu;<i>w</i> per metre (friction reach <i>&Delta;T / &mu;w</i>);</li>
<li>on a slope steeper than the friction angle (tan&theta; &gt; &mu;) the cable slides: tension builds up-slope
(spans at the crest), and where friction would need a negative tension the cable is not held and slack collects
below (loop risk);</li>
<li>over a crest the tension can change by up to a factor e<sup>&mu;&Delta;&theta;</sup> (capstan).</li>
</ul>
<p>Friction makes the result depend on the lay direction and history. The tool gives one consistent answer for the
lay direction you set. Both ends of the profile sit on the seabed. The end laid first keeps the tension it was laid
at, or what a later touchdown pulls it to, as if held by the cable beyond it or an anchor; the end laid last is the
current touchdown.</p>
<p><b>Holds (buried or pinned).</b> A lay control section set to <i>Held</i> holds the cable: buried by a plough
from PLDN to PLUP, clamped, or at an anchor (a point). It is laid at the tension in its Value (e.g. the tension at the
plough as it goes down), and like any touchdown that tension pulls back on the cable laid before it, falling by
&mu;<i>w</i> per metre: 25 kN against 1 kN of surface lay reaches back (25 &minus; 1) / &mu;<i>w</i>, 800 m for
60 N/m at &mu; 0.5. Spans there are labelled <i>pulled back by a hold</i>. Then the held cable follows the seabed (no
span) and does not slide, and the touchdowns after it do not reach back through it: the cable laid after starts
afresh at its own lay control. Slack sections next to a hold do not draw cable from it. The Holds tab gives, for each
hold, the free cable's tension either side, the net axial load it carries (an anchor's load), how far its pull-back
reaches, and the upward pull of the free cable leaving it, which the burial or anchor must resist (no soil
resistance is modelled: compare it with the uplift resistance of the burial). The tension finder and the sweep keep
the holds as set, and the detail model keeps held nodes on the seabed.</p>
<p><b>Slack and tension: one rule.</b> Tension and surplus cable are never both positive. With slack control the
cable laid is compared with the seabed it must cover, within the friction reach. Where the cable is short, it is
pulled taut: the tool finds the tension at which the rest shape uses exactly the cable laid. Where there is
surplus, the tension is zero and the surplus is stored slack. The <i>fill</i> is the slack needed to follow the
seabed (seabed length over route length, Zajac 1957). <i>Surplus</i> = laid &minus; used by the rest shape. Note:
tension from slack is very sensitive. About 0.02% of cable length can change it by 20%, so use tension control to
plan against spans and slack control to plan against loops. Where the cable laid is shorter than the seabed even
pulled straight, it has no finite tension: the tool says so rather than give a number.</p>
<p><b>Reading the Slack plot.</b> By default it shows <i>bottom slack</i>: the cable over the seabed under it. 0 is
a cable lying on the seabed; below 0 the cable is shorter than the seabed (it bridges it: spans, shaded brown);
above 0 it has surplus (green), and the dashed amber line is where the loop check flags at zero tension. Over the
route (the RPL convention, in <i>Plots</i>) a lying cable sits at the fill, not at 0, so a spanning cable can read
positive there. Both are over a running window (a length budget, not the state at one point): read spans on the
profile, and each span's shortfall in metres in the Spans table.</p>
<p><b>Spans and supports.</b> A span runs from lift-off to touchdown. It is reported when it rises more than the
reporting gap and is longer than the reporting length (defaults 0.3 m and 5 m, red at 1 m or 50 m, below the
MBR, or above the contact limit when you set one). Near a touchdown on a flat seabed the gap grows only as
<i>w d&sup2; / 2T</i>, so the cable looks to touch well before the exact touchdown. <i>Span ends where the gap is
under</i> (Reporting limits, default 0: the exact touchdown) counts the cable as touching within that gap: each
end moves in by about &radic;(2 <i>gap T / w</i>), e.g. to compare with a survey's gap detection limit.</p>
<p>The triangles mark where each span meets the seabed. On a crest (a short contact with a span either side, such as
a pipeline or a boulder) one support carries the turn of the cable and the weight of the cable either side: the
seabed reaction is <i>P = T &Delta;&theta;</i>. Where a span lands tangentially on the seabed (a touchdown) there is
no concentrated reaction. Bending stiffness spreads the turn over the bending length &lambda; =
&radic;(<i>EI / T</i>), so the bend radius is about <i>2&lambda; / &Delta;&theta;</i> and the moment
<i>(&Delta;&theta; / 2) &radic;(EI T)</i>. More tension gives fewer spans but a tighter bend at rock edges.</p>
<p><b>Contact (crush) load.</b> Set <i>Contact limit at a support</i> to flag supports where the seabed reaction is
higher. The reaction is a point load from a 2D model on a rigid seabed. Compare it with the crush rating of the
cable (or its protection) over the bearing length its data sheet uses (often per 100 mm). <i>Detail (EI)</i> gives a
closer value at a crossing.</p>
<p><b>Highest tension for spans within limits.</b> Along the route, the tool finds by bisection the highest constant
tension at which no span exceeds the reporting limits (the dotted curve on the tension plot; frictionless, in
windows). With a uniform cable, a constant tension and no friction, spans only grow as tension rises (the
monotonicity result of Binda &amp; Contro 1979), so this value is well defined.</p>
<p><b>Find tension.</b> The Find tension tab answers the reverse question for the whole profile, the visible part, or
each lay control section: the highest bottom tension that meets a target (no reportable span, no red span, no span
at all, or no span higher than <i>h</i> and longer than <i>L</i>; optionally with the bends within the MBR and the
reactions within the contact limit). Each tension is solved in full (friction, slopes, bodies, protection). Spans
shorten as the tension falls, so the answer is a ceiling: lay at or below it. Some spans remain at any tension: a
cable always spans a pipeline it crosses. The table lists them. The floor of the tension window comes from the loop
and slope checks, not from spans; to follow the seabed the cable needs at least the seabed fill as slack.</p>
<p><b>Detail model (EI).</b> The selected window is solved as a tensioned beam on the seabed:
<i>EI y'''' - T y'' = -w + p</i>, with the same contact conditions. This is a linear complementarity problem
(Maier &amp; Andreuzzi 1978), solved by a primal-dual active set method with a banded solver, with projected
over-relaxation (Cryer 1971) as a fall-back. Bodies are rigid. The grid is a quarter of the bending length by
default. Use it where EI matters: power cables (EI 10 to 100 kN&middot;m&sup2; or more, &lambda; of metres), bodies,
crossings and boulders. For light telecom cable &lambda; is well under a metre and the route model is enough.</p>
<p><b>Bodies.</b> A body adds its weight over its length. Its housing holds the cable either side off the seabed
by half the difference in diameter. Its friction coefficient comes from the friction table (body kind).</p>
<p><b>Protection.</b> Over its KP range, protection adds its weight in water to the cable's (a heavier cable spans
less but bears harder on a crest: span length goes as &radic;(<i>T / w</i>)), adds its EI when given, and sets
the MBR to the larger of the cable's and its own. Its kind is a friction table key; with no row for it, the cable's
friction applies. It shows as a grey band along the cable.</p>
<p><b>Cable diameter.</b> The line is the underside of the cable, so the gap is measured from the cable to the
seabed. Diameter has little effect on spans for telecom cables (tens of mm). It sets a body's housing standoff
and the outside of protection.</p>
<p><b>Scale.</b> The route model works at the survey resolution, for tens to hundreds of km. A boulder or a pipeline
crossing is metres long: the tool adds stations through each seabed feature, and the detail model resolves the
bending length. Use a KP window for local studies.</p>
<p><b>Alter courses.</b> The model is 2D. At an alter course, tension pulls the cable towards the inside of the turn.
Lateral friction holds it only on a turn radius of at least <i>T / (&mu;<sub>lat</sub> w)</i>. The tool lists this
radius at each RPL alter course (information only).</p>

<h3>Seabed data: what it can resolve</h3>
<p>The seabed data matters more than the mechanics: a span is only as real as the seabed under it. The tool works
out how far apart the data is around each station (the raster's cell, or the spacing of contour crossings, profile
points or RPL positions) and uses it three ways. Each rule can be changed under <i>Sampling and filtering</i>, and
<i>Suit the data</i> puts them back.</p>
<ul>
<li><b>Spans the data resolves.</b> A span needs at least three data intervals: two to show a sag between its
supports, and each support (a crest) is placed only to within half an interval. A shorter span is
<i>indicative</i>: drawn light blue and listed as information, not red or amber, because it may be longer, shorter
or absent. Spans at a seabed feature or in-line body you entered are exact and always assessed.</li>
<li><b>Stretches the data cannot assess.</b> Where three data intervals are longer than your reporting length, the
data cannot show the shortest spans you report; the stretch is flagged (information), and amber where they are
longer than the red length too (spans there are not assessed: shorter ones may be missed). The reporting limits are
your project's criteria and are never changed for the data.</li>
<li><b>The noise filter (auto).</b> The tool measures the point-to-point variation of the data (a robust standard
deviation from second differences, which sparse features such as a boulder do not move) and averages just enough
data intervals that the remaining noise is at most a quarter of the reporting gap, so a noise peak rarely makes a
reportable span. It never averages more than two data intervals or half the reporting length, so relief at the
scale of the spans reported is kept. On data coarser than that the variation is relief, not noise, and the cells
are already averages: no filter. The run states the value used.</li>
<li><b>Single-point peaks.</b> A point standing well above both neighbours (8 standard deviations) may be a flier in
the soundings or a small object; the cable can rest on it, with a span either side. The run lists their KPs.
Nothing is removed (automatic despiking would also remove real boulders): check them in the survey data.</li>
<li>Coarse grids also blunt crests: on a 5 m grid the bend radius at a crest can read 3 to 5 times the true value.
Contours cut crests to the last contour.</li>
<li>Lengths (fill and slack) use the seabed smoothed over the cable's conformity length 2(EI/w)<sup>1/3</sup>, as
noise adds length.</li>
<li>Where two rasters meet, a datum step reads as relief.</li>
<li>The cable will not lie exactly on the RPL. In deep water it can be tens of metres off. A span on the RPL line is a
likelihood, not a certainty.</li>
<li>Between RPL positions the route follows the geodesic, the path KP is measured along. A projected (e.g. UTM) map
draws long legs the same way; a map drawn in degrees draws them straight in degrees, which on a long leg is a
different line (14 m apart mid-way along a 20 km leg at 57&deg;N).</li>
</ul>

<h3>Assumptions and limits</h3>
<ul>
<li>2D, static, after lay. No current, no vessel motion, no lay dynamics, no lateral movement (except the
alter-course note).</li>
<li>Rigid seabed: no embedment or self-burial. A hold is rigid and fixed: the soil's own resistance (axial and
uplift) is not modelled.</li>
<li>Inextensible cable. Constant tension along a span except for the weight term <i>w &Delta;y</i>.</li>
<li>Small-slope shape with a slope correction. A note marks slopes steeper than 20&deg;.</li>
<li>Coulomb friction, the same static and sliding coefficient. Values for cables are uncertain: run a range.</li>
<li>Loops, hockles and kinks are not modelled. The tool marks the conditions for them: surplus cable at near-zero
tension, and slopes where the cable slides. Torque-unbalanced (armoured) cable is the most at risk (Coyne 1990).</li>
<li>Thresholds are defaults, not standards. Set them for the cable and the project.</li>
<li><b>Validation.</b> The engine is checked against closed-form solutions and published cases (span sag, a
point load in a span, Zajac's residual suspension over a step, the exact heavy-string solution of Fazio &amp;
Iacono, a tensioned beam over a knife edge, friction on a slope, the capstan, the friction anchor length) and
against the plugin's independent drape solver. It is <b>not validated</b> against field data or other software.
Treat the results as planning and screening values. Confirm critical spans by survey and detailed analysis.</li>
</ul>

<h3>References</h3>
<ul>
<li>Binda, L. &amp; Contro, R. (1979). Unilateral contact problems for cables. <i>Engineering Structures</i> 1(3),
125&ndash;130. doi:10.1016/0141-0296(79)90022-1.</li>
<li>Binda, L. &amp; Contro, R. (1981). Analysis of membranes stretched over a unilateral support. <i>Engineering
Transactions</i> 29(3), 491&ndash;501 (free).</li>
<li>Zajac, E. E. (1957). Dynamics and kinematics of the laying and recovery of submarine cable. <i>Bell System
Technical Journal</i> 36(5), 1129&ndash;1208 (free).</li>
<li>Elmendorf, C. H. &amp; Heezen, B. C. (1957). Oceanographic information for engineering submarine cable systems.
<i>Bell System Technical Journal</i> 36(5), 1047&ndash;1093 (free).</li>
<li>Cryer, C. W. (1971). The solution of a quadratic programming problem using systematic overrelaxation.
<i>SIAM J. Control</i> 9(3), 385&ndash;392.</li>
<li>Maier, G. &amp; Andreuzzi, F. (1978). Elastic and elasto-plastic analysis of submarine pipelines as unilateral
contact problems. <i>Computers &amp; Structures</i> 8(3&ndash;4), 421&ndash;431.</li>
<li>Hinterm&uuml;ller, M., Ito, K. &amp; Kunisch, K. (2002). The primal-dual active set strategy as a semismooth
Newton method. <i>SIAM J. Optimization</i> 13(3), 865&ndash;888.</li>
<li>Fazio, R. &amp; Iacono, S. (2020). arXiv:2003.05485, section 3: the heavy string on a flat obstacle (free).</li>
<li>Coyne, J. (1990). Analysis of the formation and elimination of loops in twisted cable. <i>IEEE J. Oceanic
Engineering</i> 15(2), 72&ndash;83.</li>
<li>Griffiths, T. et al. (2024). Experimental data and modelling of cable and pipe axial friction on a rocky
seabed. OMAE2024-127137.</li>
<li>DNV-RP-F105, <i>Free spanning pipelines</i> (span length from lift-off to touchdown); DNV-RP-F109,
<i>On-bottom stability</i> (pipeline friction values).</li>
<li>ITU-T G.972 / G.978: cable tension limits (NPTS, NOTS, NTTS).</li>
</ul>
"""
