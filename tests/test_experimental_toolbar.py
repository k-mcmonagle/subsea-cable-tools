# -*- coding: utf-8 -*-
"""QGIS GUI checks for the shared Experimental tools toolbar menu."""

from __future__ import annotations

from qgis.PyQt.QtWidgets import QWidget

from ..qgis_compat import TOOLBUTTON_POPUP_MODE_INSTANT
from ..subsea_cable_tools import SubseaCableTools


class _Canvas:
    def mapTool(self):
        return None

    def unsetMapTool(self, _tool):
        pass


class _Iface:
    def __init__(self):
        self.window = QWidget()
        self.canvas = _Canvas()
        self.toolbar_icons = []
        self.toolbar_widgets = []
        self.toolbar_widget_actions = []
        self.removed_toolbar_actions = []
        self.plugin_menu_actions = []

    def mainWindow(self):
        return self.window

    def mapCanvas(self):
        return self.canvas

    def addToolBarIcon(self, action):
        self.toolbar_icons.append(action)

    def addToolBarWidget(self, widget):
        token = object()
        self.toolbar_widgets.append(widget)
        self.toolbar_widget_actions.append(token)
        return token

    def removeToolBarIcon(self, action):
        self.removed_toolbar_actions.append(action)

    def addPluginToMenu(self, menu, action):
        self.plugin_menu_actions.append((menu, action))

    def removePluginMenu(self, menu, action):
        try:
            self.plugin_menu_actions.remove((menu, action))
        except ValueError:
            pass


def test_experimental_actions_share_one_toolbar_dropdown():
    iface = _Iface()
    plugin = SubseaCableTools(iface)
    plugin.initGui()
    shared_toolbar_action = plugin.experimental_toolbar_action

    try:
        experimental_actions = [
            plugin.workbench_action,
            plugin.planner_action,
            plugin.explorer_action,
            plugin.lay_simulator_action,
        ]
        assert all(action not in iface.toolbar_icons
                   for action in experimental_actions)
        assert iface.toolbar_widgets[-1] is plugin.experimental_tool_button
        assert plugin.experimental_tool_button.popupMode() == \
            TOOLBUTTON_POPUP_MODE_INSTANT
        assert plugin.experimental_menu.actions() == experimental_actions
        assert [action.text() for action in experimental_actions] == [
            "Cable Route Workbench",
            "Planner",
            "Cable Lay Data Explorer",
            "Cable Lay Simulator (3D)",
        ]
        menu_actions = [action for _menu, action in iface.plugin_menu_actions]
        assert all(action in menu_actions for action in experimental_actions)
    finally:
        plugin.unload()

    assert shared_toolbar_action in iface.removed_toolbar_actions
    assert plugin.experimental_toolbar_action is None
    assert plugin.experimental_tool_button is None


def run_all():
    try:
        test_experimental_actions_share_one_toolbar_dropdown()
    except Exception as exc:
        print("[FAIL] experimental toolbar dropdown - %r" % (exc,))
        return ["experimental toolbar dropdown"]
    print("[PASS] experimental toolbar dropdown")
    return []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(
        "Run via tests/run_qgis_smoke_tests.py (needs QGIS Python).")
