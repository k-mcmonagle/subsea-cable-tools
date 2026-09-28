"""Which RPL an imported file's KPs refer to, and translation onto the plan.

The Cable Workbench is the RPL register: every revision of every route is
there. Imports (plan tables, events CSV) pick the RPL their KPs were
quoted against — the plan's own RPL by default — and KPs on any other RPL
are translated to the plan's route by seabed position (the same geometry
mapping the Ground Model and BAS imports use).
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from qgis.core import QgsProject
from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMessageBox, QWidget

REGISTER = "__register__"


def rpl_choices(model) -> List[Tuple[str, str]]:
    """``[(label, rpl_id)]``: the plan's RPL first, then same-route
    revisions, then every other Workbench RPL."""
    from .rereference_qgis import rpl_label
    store = getattr(model, "workbench_store", None)
    plan_rpl = str(getattr(model, "resolved_rpl_id", "") or model.plan.get("rpl_id") or "")
    label = model.plan.get("rpl_name") or "route"
    if model.plan.get("rpl_revision"):
        label += f" — {model.plan.get('rpl_revision')}"
    out = [(f"This plan's RPL ({label})", "")]
    if store is None:
        return out
    try:
        rows = list(store.list_rpls())
    except Exception:
        rows = []
    current = next((r for r in rows if str(r.get("rpl_id") or "") == plan_rpl), None)
    route_id = (current or {}).get("route_id") or ""
    rows = [r for r in rows if str(r.get("rpl_id") or "") != plan_rpl]
    rows.sort(key=lambda r: (0 if route_id and r.get("route_id") == route_id else 1,
                             (r.get("name") or "").lower(), r.get("rev_label") or ""))
    out += [(rpl_label(r), str(r.get("rpl_id") or "")) for r in rows]
    return out


def kp_transform(model, rpl_id: str):
    """``(map_range, kp_map)`` translating KPs on ``rpl_id`` to the plan route.

    ``map_range(start, end) -> (start, end, flags)``. ``rpl_id`` "" (the
    plan's own RPL) returns ``(None, None)``. Raises ValueError.
    """
    if not rpl_id:
        return None, None
    from .rereference_qgis import geometry_map, rpl_label, rpl_route
    store = getattr(model, "workbench_store", None)
    if store is None or model.route is None:
        raise ValueError("The plan route and the Cable Workbench are needed to "
                         "translate KPs from another RPL.")
    rpl = store.get_rpl(rpl_id)
    if rpl is None:
        raise ValueError("The selected RPL is no longer in the Workbench.")
    src_route, _distance, _positions = rpl_route(store, rpl, QgsProject.instance(),
                                                 model.kp_mode())
    kp_map = geometry_map(src_route, model.route, source_label=rpl_label(rpl),
                          target_label=model.plan.get("rpl_name") or "plan route")
    return kp_map.map_range, kp_map


class RplReferencePicker(QWidget):
    """"KPs referenced to: [RPL ▾]" with a *Register another RPL…* entry."""

    changed = pyqtSignal()

    def __init__(self, model, parent=None, label: str = "KPs referenced to:"):
        super().__init__(parent)
        self.model = model
        self._cache: Dict[str, Tuple] = {}
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(label))
        self.combo = QComboBox()
        self.combo.setToolTip(
            "The RPL the file's KPs were quoted against. KPs on another RPL "
            "(an older revision, or a different route) are translated to this "
            "plan's route by seabed position; stretches where the routes "
            "diverge are flagged.")
        layout.addWidget(self.combo, 1)
        self.reload()
        self.combo.activated.connect(self._activated)

    def reload(self, select: str = "") -> None:
        self.combo.blockSignals(True)
        self.combo.clear()
        for text, rpl_id in rpl_choices(self.model):
            self.combo.addItem(text, rpl_id)
        self.combo.addItem("Register another RPL…", REGISTER)
        index = self.combo.findData(select) if select else 0
        self.combo.setCurrentIndex(max(0, index))
        self.combo.blockSignals(False)
        self._last = self.combo.currentData() or ""

    def _activated(self, _index: int) -> None:
        if self.combo.currentData() == REGISTER:
            new_id = self._register()
            self.reload(new_id or self._last)
        self._last = self.combo.currentData() or ""
        self.changed.emit()

    def _register(self) -> str:
        store = getattr(self.model, "workbench_store", None)
        if store is None:
            QMessageBox.information(self, "Register RPL",
                                    "Open the Cable Workbench first to create its store.")
            return ""
        try:
            from ..qgis_compat import qt_exec
            from ..workbench.rpl_import_wizard import RplImportWizard
            wizard = RplImportWizard(store, None, parent=self)
            imported: List[str] = []
            wizard.imported.connect(imported.append)
            qt_exec(wizard)
            return imported[-1] if imported else ""
        except Exception as exc:
            QMessageBox.warning(self, "Register RPL",
                                f"Could not open the RPL import wizard:\n{exc}")
            return ""

    def rpl_id(self) -> str:
        return self.combo.currentData() or ""

    def label(self) -> str:
        return self.combo.currentText()

    def transform(self) -> Tuple[Optional[Callable], object]:
        """``(map_range, kp_map)`` or ``(None, None)``; cached per RPL."""
        rpl_id = self.rpl_id()
        if rpl_id not in self._cache:
            self._cache[rpl_id] = kp_transform(self.model, rpl_id)
        return self._cache[rpl_id]
