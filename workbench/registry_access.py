# -*- coding: utf-8 -*-
"""Read-only access to the current project's Cable Route Workbench registry.

Shared by tools that take routes and assemblies from the Workbench (the Lay
Assessment's cable makeup, Span & Slack Analysis). Main thread only.
"""

from __future__ import annotations

from typing import List, Tuple

from ..plugin_log import log_exception


def open_store():
    """The current project's Workbench store, or None when it has none."""
    try:
        from .project_layers import discover_gpkg_path
        from .store import WorkbenchStore

        path = discover_gpkg_path()
        if not path:
            return None
        store = WorkbenchStore(path)
        return store if store.exists() else None
    except Exception:
        log_exception("Opening the Cable Route Workbench registry failed")
        return None


def rpl_choices(store) -> List[Tuple[str, str]]:
    """``[(rpl_id, label)]`` of the Workbench RPLs, newest revision labels shown."""
    routes = {r.get("route_id"): r.get("name") or "" for r in store.list_routes()}
    out = []
    for rpl in store.list_rpls():
        parts = [routes.get(rpl.get("route_id")) or "", rpl.get("name") or ""]
        label = " / ".join(p for p in parts if p)
        rev = rpl.get("rev_label")
        kind = rpl.get("kind")
        label += (f" rev {rev}" if rev else "") + (f" ({kind.replace('_', '-')})" if kind else "")
        out.append((rpl.get("rpl_id"), label))
    return out


def fit_choices(store, rpl_id: str) -> List[Tuple[str, str]]:
    """``[(fit_id, label)]`` of the assemblies fitted to an RPL."""
    names = {a.get("assembly_id"): a.get("name") or "assembly" for a in store.list_assemblies()}
    out = []
    for fit in store.list_fits(rpl_id=rpl_id):
        label = (f"{names.get(fit.get('assembly_id'), 'assembly')} - anchor KP "
                 f"{float(fit.get('anchor_kp_km') or 0.0):.3f}")
        out.append((fit.get("fit_id"), label))
    return out


def load_model(store, rpl_id: str):
    """The RPL's model (points, legs) read from its Workbench layers."""
    from .rpl_layer_io import RplLayerSync

    rpl = store.get_rpl(rpl_id)
    if not rpl:
        raise ValueError("The RPL is no longer in the Workbench.")
    points = store.open_layer(rpl.get("points_layer"))
    lines = store.open_layer(rpl.get("lines_layer"))
    if points is None or lines is None:
        raise ValueError("The RPL's layers could not be opened.")
    return RplLayerSync(points, lines, rpl_id).load_model()
