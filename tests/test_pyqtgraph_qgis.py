"""Plot teardown must not send QVariant conversion exceptions into QGIS."""
import sys
from unittest.mock import patch

import pyqtgraph as pg
import pytest
from qgis.PyQt.QtCore import QCoreApplication, QEvent
from qgis.PyQt.QtWidgets import QGraphicsItem, QGraphicsScene

REQUIRES_QGIS = True


def test_plot_scene_and_parent_changes_do_not_raise_in_qt_callbacks():
    errors = []
    with patch.object(sys, 'excepthook', lambda typ, value, tb: errors.append(str(value))):
        scene = QGraphicsScene()
        view = pg.ViewBox(enableMenu=False)
        curve = pg.PlotDataItem([0, 1], [2, 3])
        scene.addItem(view)
        view.addItem(curve)
        assert view.scene() is scene and curve.scene() is scene
        assert curve.parentItem() is view.childGroup
        view.removeItem(curve)
        assert curve.scene() is None and curve.parentItem() is None
        scene.removeItem(view)
        assert view.scene() is None
        view.close()
        for item in (curve, view, scene):
            item.deleteLater()
        # PlotWidget.close() detaches its ViewBox and axes through both patched
        # itemChange implementations (the real QGIS 3.40 reproduction).
        plot = pg.PlotWidget()
        plot.plot([0, 1], [2, 3])
        plot.close()
        plot.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.processEvents()
    assert not errors, errors


def test_plot_callback_workaround_preserves_other_values_and_errors():
    from pyqtgraph import Qt
    helper = getattr(Qt, 'safe_graphics_item_change', None)
    if helper is None:
        pytest.skip('Host pyqtgraph used; bundled workaround not loaded')
    changes = QGraphicsItem.GraphicsItemChange
    marker = object()
    assert helper(lambda change, value: value, changes.ItemParentChange, marker) is marker
    variant = Qt.QtCore.QVariant({'example': 1})
    assert helper(lambda change, value: value, changes.ItemPositionChange, variant) is variant

    def fail(change, value):
        raise TypeError('unrelated callback error')

    with pytest.raises(TypeError, match='unrelated callback error'):
        helper(fail, changes.ItemSceneChange, None)
