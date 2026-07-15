# -*- coding: utf-8 -*-
"""Background loading of QGIS layers into :class:`LayDataset` objects.

Uses a :class:`QgsTask` so a large cable-lay layer is read on a worker thread,
keeping the Explorer window responsive and cancellable. The task is built from
main-thread *snapshots* (a ``QgsVectorLayerFeatureSource`` plus the layer's
fields / CRS / transform context) so the actual feature iteration on the worker
thread never touches the live ``QgsVectorLayer``. This pattern is stable across
QGIS 3 and QGIS 4.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from qgis.core import QgsProject, QgsTask, QgsVectorLayerFeatureSource

from ..laydata import LayDataset


def _task_flag(name: str, default: int = 0):
    """Scoped-enum-safe access to a ``QgsTask.Flag`` value (Qt5/Qt6)."""
    enum = getattr(QgsTask, "Flag", QgsTask)
    return getattr(enum, name, default)


_CAN_CANCEL = _task_flag("CanCancel")


def build_spec(layer) -> dict:
    """Snapshot everything a worker thread needs to read ``layer`` (main thread)."""
    return {
        "layer_id": layer.id(),
        "name": layer.name(),
        "source": QgsVectorLayerFeatureSource(layer),
        "field_names": [field.name() for field in layer.fields()],
        "crs": layer.crs(),
        "is_spatial": layer.isSpatial(),
        "feature_count": max(int(layer.featureCount()), 0),
        "transform_context": QgsProject.instance().transformContext(),
    }


class LayerLoadTask(QgsTask):
    """Reads one or more layer snapshots into datasets on a worker thread."""

    def __init__(self, specs: List[dict], description: str = "Loading data layers"):
        super().__init__(description, _CAN_CANCEL)
        self._specs = specs
        self.datasets: Dict[str, LayDataset] = {}
        self.error: Optional[str] = None

    def run(self) -> bool:  # executed on a background thread
        try:
            total = sum(max(int(s["feature_count"]), 1) for s in self._specs) or 1
            done = 0
            for spec in self._specs:
                count = max(int(spec["feature_count"]), 1)

                def _progress(idx, _done=done, _count=count, _total=total):
                    self.setProgress(min(100.0, (_done + min(idx, _count)) / _total * 100.0))

                dataset = LayDataset.from_feature_source(
                    spec["source"],
                    field_names=spec["field_names"],
                    layer_crs=spec["crs"],
                    is_spatial=spec["is_spatial"],
                    transform_context=spec["transform_context"],
                    layer_name=spec["name"],
                    feature_count=spec["feature_count"],
                    progress=_progress,
                    is_canceled=self.isCanceled,
                )
                if dataset is None or self.isCanceled():
                    return False
                self.datasets[spec["layer_id"]] = dataset
                done += count
            self.setProgress(100.0)
            return True
        except Exception as exc:  # pragma: no cover - surfaced via taskTerminated
            self.error = str(exc)
            return False
