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
<li><b>Route and KP.</b> Select a Cable Route Workbench RPL, a line layer (with its start KP), a CSV profile, or a
synthetic seabed. Set a KP window to study part of a long route.</li>
<li><b>Seabed.</b> Tick the MBES rasters (or choose contours). <i>Sample every: auto</i> samples each grid at its
cell size. The noise filter (default 2 m) removes sounding noise but keeps crests. Add seabed features
(boulder, pipeline crossing, berm or mattress, depression) by KP if you need them.</li>
<li><b>Cable and bodies.</b> Use a cable type from the Cable Type Library, types by KP section, the RPL's cable
type per leg, or a Workbench assembly fitted to the RPL (its cable sections and bodies land at KPs through the
RPL slack). Workbench labels such as "SA" resolve through the library: a project mapping, then a library name,
alias or generic type. Where several library types could mean a label, the mapping table in step 3 says so:
choose the type there. The mapping is saved in the QGIS project and the Lay Assessment uses the same one. Add
repeaters, joints and branching units by KP: weight in water, rigid length and diameter.</li>
<li><b>Friction.</b> One axial coefficient for the route, or soil zones by KP with a table of coefficients for each
cable type (or body kind) and soil.</li>
<li><b>Lay control.</b> Bottom tension (kN), bottom slack (% over the seabed), slack over the route (%, as in an
RPL), or the RPL slack of each leg. Override the value by KP section. Set the lay direction.</li>
<li><b>Run.</b> The first run samples the seabed (in the background). After that, a change to the lay control
updates the results in about a second: move the slider and see the spans grow or shrink.</li>
<li><b>Read the results.</b> The status bar shows the worst finding by KP. Spans are filled by level: red,
amber, and light grey where the cable is clear of the seabed but under the reporting limits (not reported).
Click a finding or a span to zoom the profile and highlight it on the map; click a column heading to sort a
table. Hover the profile to move a marker along the route on the map; hover or click a body for its details.
Use <i>Pick KP on map</i> to go from the map to the profile.</li>
<li><b>Arrange the view.</b> Drag the handles between the rows to share the height. <i>True scale (1:1)</i>
shows slopes and spans as they are; <i>Sea level</i> adds the water surface; <i>Reverse KP</i> runs KP from right
to left; <i>Body labels</i> names the bodies. <i>Measure</i> gives two-point lengths, heights and angles,
snapped to the seabed or the cable, as in the Depth Profile tool.</li>
<li><b>Look closer.</b> <i>Detail (EI)</i> solves a span or the visible window with bending stiffness: bend radius,
moment and seabed reaction along the cable. <i>Tension sweep</i> lays the cable at a range of bottom tensions and
plots the span length against tension.</li>
<li><b>Save.</b> Scenarios (all settings and the sampled seabed) save in the project's Span Analysis GeoPackage.
<i>Add to map</i> writes the findings as a line layer. <i>Export</i> writes CSV files.</li>
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
lay direction you set.</p>
<p><b>Slack and tension: one rule.</b> Tension and surplus cable are never both positive. With slack control the
cable laid is compared with the seabed it must cover, within the friction reach. Where the cable is short, it is
pulled taut: the tool finds the tension at which the rest shape uses exactly the cable laid. Where there is
surplus, the tension is zero and the surplus is stored slack. The <i>fill</i> is the slack needed to follow the
seabed (seabed length over route length, Zajac 1957). <i>Surplus</i> = laid &minus; used by the rest shape. Note:
tension from slack is very sensitive. About 0.02% of cable length can change it by 20%, so use tension control to
plan against spans and slack control to plan against loops.</p>
<p><b>Spans and supports.</b> A span runs from lift-off to touchdown. It is reported when it rises more than the
reporting gap and is longer than the reporting length (defaults 0.3 m and 5 m, red at 1 m or 50 m or below the
MBR). At each support the seabed reaction is <i>P = T &Delta;&theta;</i>. Bending stiffness spreads the turn over the
bending length &lambda; = &radic;(<i>EI / T</i>), so the bend radius is about <i>2&lambda; / &Delta;&theta;</i> and the
moment <i>(&Delta;&theta; / 2) &radic;(EI T)</i>. More tension gives fewer spans but a tighter bend at rock edges.</p>
<p><b>Highest tension for spans within limits.</b> Along the route, the tool finds by bisection the highest constant
tension at which no span exceeds the reporting limits. With a uniform cable, a constant tension and no friction,
spans only grow as tension rises (the monotonicity result of Binda &amp; Contro 1979), so this value is well
defined.</p>
<p><b>Detail model (EI).</b> The selected window is solved as a tensioned beam on the seabed:
<i>EI y'''' - T y'' = -w + p</i>, with the same contact conditions. This is a linear complementarity problem
(Maier &amp; Andreuzzi 1978), solved by a primal-dual active set method with a banded solver, with projected
over-relaxation (Cryer 1971) as a fall-back. Bodies are rigid. The grid is a quarter of the bending length by
default. Use it where EI matters: power cables (EI 10 to 100 kN&middot;m&sup2; or more, &lambda; of metres), bodies,
crossings and boulders. For light telecom cable &lambda; is well under a metre and the route model is enough.</p>
<p><b>Bodies.</b> A body adds its weight over its length. Its housing holds the cable either side off the seabed
by half the difference in diameter. Its friction coefficient comes from the friction table (body kind).</p>
<p><b>Scale.</b> The route model works at the survey resolution, for tens to hundreds of km. A boulder or a pipeline
crossing is metres long: the tool adds stations through each seabed feature, and the detail model resolves the
bending length. Use a KP window for local studies.</p>
<p><b>Alter courses.</b> The model is 2D. At an alter course, tension pulls the cable towards the inside of the turn.
Lateral friction holds it only on a turn radius of at least <i>T / (&mu;<sub>lat</sub> w)</i>. The tool lists this
radius at each RPL alter course (information only).</p>

<h3>Seabed data: what limits the answer</h3>
<ul>
<li>The seabed resolution matters more than the mechanics. Coarse grids blunt crests: on a 5 m grid the bend radius
at a crest can read 3 to 5 times the true value. Contours cut crests to the last contour.</li>
<li>Sounding noise adds false relief. The noise filter removes it from the seabed the cable rests on; lengths use the
seabed smoothed over the cable's conformity length 2(EI/w)<sup>1/3</sup>.</li>
<li>Where two rasters meet, a datum step reads as relief.</li>
<li>The cable will not lie exactly on the RPL. In deep water it can be tens of metres off. A span on the RPL line is a
likelihood, not a certainty.</li>
</ul>

<h3>Assumptions and limits</h3>
<ul>
<li>2D, static, after lay. No current, no vessel motion, no lay dynamics, no lateral movement (except the
alter-course note).</li>
<li>Rigid seabed: no embedment or self-burial.</li>
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
