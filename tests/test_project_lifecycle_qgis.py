"""Exercise the legacy fallback against real QGIS project clearing."""
from types import SimpleNamespace

from qgis.core import QgsProject, QgsVectorLayer

from ..project_lifecycle import project_teardown_slots

REQUIRES_QGIS = True


def test_legacy_teardown_precedes_layer_deletion_and_disconnects():
    project = QgsProject()  # Do not disturb the user's singleton project.
    layer = QgsVectorLayer('LineString?crs=EPSG:4326', 'route', 'memory')
    project.addMapLayer(layer)
    # Hide the newer signal even when running this regression on QGIS 3.34+.
    legacy = SimpleNamespace(removeAll=project.removeAll, cleared=project.cleared)
    seen = []

    def teardown():
        seen.append([item.id() for item in project.mapLayers().values()])

    route_id = layer.id()
    pairs = project_teardown_slots(legacy, teardown)
    for signal, slot in pairs:
        signal.connect(slot)
    try:
        project.clear()
        assert seen == [[route_id], []]
        project.clear()  # Empty projects must release non-layer state too.
        assert seen == [[route_id], [], []]
    finally:
        for signal, slot in pairs:
            signal.disconnect(slot)
    project.clear()
    assert seen == [[route_id], [], []]


def test_maptools_with_legacy_project_signals():
    from unittest.mock import patch
    from .. import project_lifecycle
    from . import test_maptools_project_switch

    with patch.object(project_lifecycle, "project_teardown_signal_names",
                      return_value=("removeAll", "cleared")):
        assert all(test_maptools_project_switch.run_all())


def test_docks_with_legacy_project_signals():
    from unittest.mock import patch
    from .. import project_lifecycle
    from . import (test_workbench_project_switch, test_planner_project_switch,
                   test_explorer_project_switch, test_burial_project_switch,
                   test_plugin_lifecycle)

    with patch.object(project_lifecycle, "project_teardown_signal_names",
                      return_value=("removeAll", "cleared")):
        for module in (test_plugin_lifecycle, test_workbench_project_switch,
                       test_planner_project_switch, test_explorer_project_switch,
                       test_burial_project_switch):
            assert all(module.run_all()), module.__name__
