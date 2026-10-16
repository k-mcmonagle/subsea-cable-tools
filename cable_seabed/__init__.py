# -*- coding: utf-8 -*-
"""Cable on the seabed: the shared 2D mechanics engine.

numpy only (no Qt, no QGIS), so every calculation is unit-testable. Two
tools use it:

* the Cable Lay Data Explorer's *Lay Assessment* (as-laid data), and
* *Span & Slack Analysis* (planning and engineering studies).

Modules:

``core``      constants, cancel / progress hooks, KP-range findings, status bins
``shape``     the cable's rest shape on a rigid seabed (small-slope obstacle
              problem, Binda & Contro 1979), smoothing and lengths
``spans``     spans, span supports (reaction, bend radius), allowable tension
``friction``  residual tension: lay history held by Coulomb seabed friction
``beam``      tensioned beam (T and EI) on a unilateral seabed, solved as an LCP
``features``  seabed features (boulder, pipeline, berm) added to a profile
``planning``  the Span & Slack Analysis model: inputs, run, findings, sweep

The method is documented in ``docs/SPAN_ANALYSIS.md`` and
``docs/LAY_ASSESSMENT.md``.
"""
