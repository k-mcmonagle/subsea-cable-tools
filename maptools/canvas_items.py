# -*- coding: utf-8 -*-
"""Disposal of map-canvas overlay items shared by the map tools."""

try:  # sip is available in QGIS Python env; guard for static analysis
    from qgis.PyQt import sip  # type: ignore
    _sip_isdeleted = sip.isdeleted
except ImportError:  # pragma: no cover
    def _sip_isdeleted(_obj):
        return False


def remove_canvas_item(item) -> None:
    """Hide ``item`` and detach it from its map-canvas scene.

    ``QgsVertexMarker`` (and ``QgsRubberBand`` in older builds) is a plain
    QGraphicsItem with no ``deleteLater()``, so removing it from the scene is
    the portable way to make it disappear for good. Safe to call twice, on
    ``None`` and on an item whose C++ object is already gone.
    """
    if item is None or _sip_isdeleted(item):
        return
    try:
        item.hide()
        scene = item.scene()
        if scene is not None:
            scene.removeItem(item)
    except RuntimeError:  # the canvas is being torn down with the item
        pass
