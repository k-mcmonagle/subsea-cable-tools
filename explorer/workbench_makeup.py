# -*- coding: utf-8 -*-
"""Cable type along the route from the Cable Route Workbench.

Two sources, both read-only and on the main thread:

* an RPL's legs (the ``CableType`` of each line segment, by KP), and
* an assembly fitted to an RPL (``wb_fit``): its sections' cable types at
  the KPs the fit lands them on, through the RPL's per-segment slack.

Each becomes a :class:`laydata.lay_assessment.KpMakeup`. The lay data's
touchdown KPs must be quoted on the same RPL.
"""

from __future__ import annotations

from typing import Optional

from ..laydata.lay_assessment import KpMakeup
from ..workbench.registry_access import (  # noqa: F401 (re-exported)
    fit_choices,
    load_model,
    open_store,
    rpl_choices,
)


def rpl_makeup(store, rpl_id: str) -> KpMakeup:
    """Cable type per RPL leg, by KP."""
    model = load_model(store, rpl_id)
    ranges = []
    for i, segment in enumerate(model.segments):
        label = (segment.attrs or {}).get("CableType")
        k0, k1 = model.points[i].dist_cum_km, model.points[i + 1].dist_cum_km
        if label is not None and k0 is not None and k1 is not None:
            ranges.append((k0, k1, label))
    return KpMakeup.from_ranges(ranges, "Workbench RPL legs")


def fit_makeup(store, fit_id: str) -> KpMakeup:
    """Cable type per assembly section, at the KPs its fit lands them on."""
    from ..workbench import assembly_model as am
    from ..workbench.fit import FitAnchor, fit_assembly

    fit = next((f for f in store.list_fits() if f.get("fit_id") == fit_id), None)
    if fit is None:
        raise ValueError("The assembly fit is no longer in the Workbench.")
    header, items = store.get_assembly(fit.get("assembly_id") or "")
    if header is None:
        raise ValueError("The fitted assembly is no longer in the Workbench.")
    assembly = am.assembly_from_rows(header, items)
    model = load_model(store, fit.get("rpl_id") or "")
    anchor = FitAnchor(float(fit.get("anchor_kp_km") or 0.0), float(fit.get("anchor_cable_dist_m") or 0.0),
                       1 if int(fit.get("direction") or 1) >= 0 else -1)
    result = fit_assembly(assembly, model, anchor)
    ranges = [(span.kp_start_km, span.kp_end_km, span.item.cable_type)
              for span in result.sections if span.kp_start_km is not None and span.kp_end_km is not None]
    return KpMakeup.from_ranges(ranges, f"Workbench assembly {header.get('name') or ''}".strip())


def makeup_for(store, source: str, rpl_id: Optional[str], fit_id: Optional[str]) -> Optional[KpMakeup]:
    if store is None:
        return None
    if source == "rpl" and rpl_id:
        return rpl_makeup(store, rpl_id)
    if source == "fit" and fit_id:
        return fit_makeup(store, fit_id)
    return None
