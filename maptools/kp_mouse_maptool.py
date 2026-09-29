# kp_mouse_maptool.py
# -*- coding: utf-8 -*-
"""
KPMouseMapTool
Integrated into the Subsea Cable Tools plugin.
This tool displays the closest point on a selected line to the mouse pointer,
draws a dashed line connecting them, and shows distance and KP (chainage) data.
Right-clicking can copy KP info to the clipboard (configurable in tool settings).
"""

import logging
import os
from typing import Optional, Tuple

from qgis.PyQt.QtCore import Qt, QCoreApplication, QSettings, QTimer
from qgis.PyQt.QtGui import QIcon, QColor, QCursor
from qgis.PyQt.QtWidgets import (QMessageBox, QToolTip,
                                 QApplication, QDialog, QVBoxLayout,
                                 QComboBox, QLabel, QDialogButtonBox,
                                 QToolButton, QMenu, QCheckBox, QLineEdit, QHBoxLayout, QPushButton,
                                 QTableWidget, QTableWidgetItem)
from qgis.core import (QgsWkbTypes, QgsGeometry, QgsProject, QgsDistanceArea,
                       QgsPointXY, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       QgsCsException, QgsFeatureRequest,
                       Qgis, QgsVectorLayer, QgsField, QgsFeature)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
from ..qgis_compat import QAction, DIALOG_ACCEPTED, qt_exec, DISTANCE_METERS, FIELD_TYPE_DOUBLE, FIELD_TYPE_INT, FIELD_TYPE_LONG_LONG, FIELD_TYPE_STRING, GEOMETRY_LINE, GEOMETRY_POINT, GEOMETRY_POLYGON, LAYER_RASTER, LAYER_VECTOR, MESSAGE_CRITICAL, MESSAGE_INFO, MESSAGE_SUCCESS, MESSAGE_WARNING, TOOLBUTTON_POPUP_MODE_MENU_BUTTON, BUTTON_BOX_OK, BUTTON_BOX_CANCEL, BUTTON_BOX_CLOSE, BUTTON_BOX_ACCEPT_ROLE, get_event_global_pos, ITEM_DATA_USER_ROLE, ITEM_FLAG_EDITABLE, ITEM_FLAG_USER_CHECKABLE, CHECK_STATE_CHECKED, CHECK_STATE_UNCHECKED, SELECTION_MODE_NONE, HEADER_RESIZE_MODE_STRETCH
import math

from ..kp_geo_utils import KPHit, RouteFrame, geometry_is_finite, ordered_route_features
from ..kp_range_utils import (KP_MODE_CARTESIAN, KP_MODE_GEODESIC, describe_kp_mode,
                              kp_distance_mode, kp_grid_crs_setting, make_distance_area,
                              make_kp_distance_area, set_kp_distance_settings)
from ..plugin_log import log_exception
from .canvas_items import _sip_isdeleted, remove_canvas_item

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # maptools/ -> plugin root


def plugin_menu_name() -> str:
    """The plugin's menu title, translated exactly as the plugin shell does
    (``SubseaCableTools`` context), so the tool's entry joins that menu."""
    return QCoreApplication.translate('SubseaCableTools', '&Subsea Cable Tools')


def _layer_has_features(layer) -> bool:
    """True when ``layer`` yields at least one feature (reads one, no attributes)."""
    request = QgsFeatureRequest().setLimit(1).setNoAttributes()
    try:
        return next(iter(layer.getFeatures(request)), None) is not None
    except RuntimeError:  # layer wrapper deleted
        return False


def build_reference_route(layer, map_crs, use_cartesian: bool):
    """KP route model of ``layer`` in ``map_crs`` for the KP Mouse tool.

    Returns ``(route, distance_area, geometries)``: the features' geometries
    in the plugin's route order (``ordered_route_features``: RPL ``SeqNo``
    when every feature has one, else layer order — the order the Processing
    tools and the KP Plotter use), transformed to the map CRS, and a
    :class:`RouteFrame` over them measured with the plugin-wide KP distance
    (geodesic, or Cartesian grid). KP is continuous across features in that
    order; features without geometry add no length. Raises
    ``QgsCsException`` or ``ValueError`` when a feature cannot be
    transformed or measured — skipping it would shift every later KP.
    """
    distance = make_kp_distance_area(
        map_crs, QgsProject.instance().transformContext(),
        mode=KP_MODE_CARTESIAN if use_cartesian else KP_MODE_GEODESIC)
    transform = None
    if layer.crs() != map_crs:
        transform = QgsCoordinateTransform(layer.crs(), map_crs, QgsProject.instance())
    geoms = []
    for feature in ordered_route_features(layer.getFeatures()):
        geom = QgsGeometry(feature.geometry())
        if transform is not None:
            geom.transform(transform)
            # transform() writes inf instead of raising for points outside
            # the map CRS's domain.
            if not geometry_is_finite(geom):
                raise ValueError("feature %s lies outside the map CRS" % feature.id())
        geoms.append(geom)
    return RouteFrame.from_source(geoms, distance).build_indexes(), distance, geoms


class KPMouseMapTool(QgsMapTool):
    """
    Map tool that tracks the mouse pointer and shows:
      - The closest point on a selected line,
      - A dashed line between the mouse pointer and that point,
      - The distance (in the selected unit) and chainage (KP).
    Also allows copying the current data via a right-click.
    """
    def __init__(
        self,
        canvas,
        layer,
        iface,
        measurementUnit="m",
        showReverseKP=False,
        useCartesian=False,
        showDepth=False,
        depthSources=None,
        showDepthProfile=False,
        copyIncludeRKP=False,
        copyIncludeDCC=False,
        copyIncludeLatLon=False,
        copyLatLonFormat="DD",
        copyLatLonStyle="LABELLED",
    ):
        super().__init__(canvas)
        self.canvas = canvas
        self.iface = iface
        self.layer = layer
        self.measurementUnit = measurementUnit
        self.showReverseKP = showReverseKP
        self.useCartesian = useCartesian
        self.showDepth = showDepth
        # Depth sources: [(layer, field)] — several rasters/contour sets can
        # cover different parts of a route. The sampler is built lazily.
        self.depthSources = [pair for pair in (depthSources or []) if pair and pair[0] is not None]
        self.showDepthProfile = bool(showDepthProfile)
        self._depth_sampler = None
        self.depth_profile_window = None

        # Right-click copy behaviour (KP is always included)
        self.copyIncludeRKP = bool(copyIncludeRKP)
        self.copyIncludeDCC = bool(copyIncludeDCC)
        self.copyIncludeLatLon = bool(copyIncludeLatLon)
        self.copyLatLonFormat = (copyLatLonFormat or "DD").upper()
        self.copyLatLonStyle = (copyLatLonStyle or "LABELLED").upper()

        # Route model (geometries in the map CRS + chainage/spatial index),
        # built once here and on layer/CRS changes; see recalculate_geometries.
        self._route = None
        self.features_geoms = []
        self.total_length_meters = 0.0
        # Map CRS <-> WGS84 transforms for range/bearing, built once per CRS.
        self._wgs84_crs = None
        self._wgs84_xforms = None
        # (origin, range_m) of the range ring currently drawn.
        self._ring_key = None
        self.recalculate_geometries()
        self.canvas.destinationCrsChanged.connect(self._on_map_crs_changed)

        # Visual helpers
        self.rubberBand = QgsRubberBand(self.canvas, GEOMETRY_LINE)
        self.rubberBand.setColor(QColor(255, 0, 0))
        self.rubberBand.setWidth(2)
        self.rubberBand.setLineStyle(Qt.PenStyle.DashLine)

        self.closestPointMarker = QgsVertexMarker(self.canvas)
        self.closestPointMarker.setColor(QColor(0, 255, 0))
        self.closestPointMarker.setIconType(QgsVertexMarker.ICON_CROSS)
        self.closestPointMarker.setIconSize(10)
        self.closestPointMarker.setPenWidth(3)

        # State storage
        self.last_mouse_point = None
        self.last_distance = None
        self.last_chainage = None
        self.last_reverse_chainage = None
        self.last_message = ""
        self.last_global_pos = None
        self.last_closest_point = None

        # Timers
        self.mouse_stop_timer = QTimer(self.canvas)
        self.mouse_stop_timer.setSingleShot(True)
        self.mouse_stop_timer.timeout.connect(self.start_persistent_tooltip)
        self.persistent_tooltip_timer = QTimer(self.canvas)
        self.persistent_tooltip_timer.timeout.connect(self.show_persistent_tooltip)

        # Cursor
        self.canvas.setCursor(Qt.CursorShape.CrossCursor)

        # Range/Bearing resources
        self.range_bearing_origin = None
        self.rangeBearingLine = QgsRubberBand(self.canvas, GEOMETRY_LINE)
        self.rangeBearingLine.setColor(QColor(0, 170, 255))
        self.rangeBearingLine.setWidth(2)
        self.rangeBearingCircle = QgsRubberBand(self.canvas, GEOMETRY_POLYGON)
        self.rangeBearingCircle.setColor(QColor(0, 170, 255, 60))
        self.rangeBearingCircle.setFillColor(QColor(0, 170, 255, 40))
        self.rangeBearingCircle.setWidth(1)
        self.rangeBearingOriginMarker = QgsVertexMarker(self.canvas)
        self.rangeBearingOriginMarker.setColor(QColor(0, 170, 255))
        self.rangeBearingOriginMarker.setIconType(QgsVertexMarker.ICON_BOX)
        self.rangeBearingOriginMarker.setIconSize(10)
        self.rangeBearingOriginMarker.setPenWidth(2)
        self.rangeBearingOriginMarker.hide()
        # Mirrors the depth-profile plot cursor on the range line.
        self.profileCursorMarker = QgsVertexMarker(self.canvas)
        self.profileCursorMarker.setColor(QColor(216, 27, 96))
        self.profileCursorMarker.setIconType(QgsVertexMarker.ICON_CIRCLE)
        self.profileCursorMarker.setIconSize(12)
        self.profileCursorMarker.setPenWidth(3)
        self.profileCursorMarker.hide()

    def set_layer(self, layer):
        """Set the layer and recalculate geometries for the tool."""
        self.layer = layer
        self.recalculate_geometries()

    def recalculate_geometries(self):
        """Rebuild the route model for the current layer and map CRS.

        The reference line is transformed to the map CRS and indexed once
        (``RouteFrame``: per-segment chainage + a segment spatial index), so
        each mouse move is an indexed nearest-segment lookup instead of a
        walk over every feature and vertex. KP follows the plugin-wide KP
        setting (geodesic, or Cartesian grid — which also works on a
        geographic map CRS via the grid CRS).
        """
        map_crs = self.canvas.mapSettings().destinationCrs()
        mode = KP_MODE_CARTESIAN if self.useCartesian else KP_MODE_GEODESIC
        self._route = None
        self.features_geoms = []
        self.total_length_meters = 0.0
        if self.layer is None:
            self.distanceArea = make_kp_distance_area(
                map_crs, QgsProject.instance().transformContext(), mode=mode)
            return
        try:
            route, distance, geoms = build_reference_route(self.layer, map_crs, self.useCartesian)
        except (QgsCsException, ValueError):
            log_exception("KP Mouse Tool: reference line could not be transformed/measured "
                          "in the map CRS")
            self.distanceArea = make_kp_distance_area(
                map_crs, QgsProject.instance().transformContext(), mode=mode)
            self.iface.messageBar().pushMessage(
                "KP Mouse Tool",
                "The reference line could not be transformed to the map CRS, so KPs "
                "cannot be shown (see the Subsea Cable Tools log).",
                level=MESSAGE_WARNING, duration=6)
            return
        self.distanceArea = distance
        self.features_geoms = geoms
        self._route = route
        self.total_length_meters = route.total_length_m

    def _on_map_crs_changed(self):
        """The cached route and transforms are in the old map CRS: rebuild.

        A range/bearing origin picked in the old CRS is meaningless now, so
        the measurement is cleared.
        """
        self._wgs84_crs = self._wgs84_xforms = None
        self._ring_key = None
        self.recalculate_geometries()
        if self.range_bearing_origin is not None:
            self.range_bearing_origin = None
            if self.rangeBearingOriginMarker is not None:
                self.rangeBearingOriginMarker.hide()
            self._clear_range_bearing_graphics()
            self._hide_profile_window()
        window = self.depth_profile_window
        if window is not None:
            # The profile window holds the old route and sampler CRS.
            window.cleanup()
            self.depth_profile_window = None
        self._depth_sampler = None

    def _locate(self, map_point) -> Optional[KPHit]:
        """Nearest route position to ``map_point`` (map CRS), or ``None``.

        ``kp_km`` is the continuous KP over the features in layer order and
        ``dcc_m`` the KP-distance (geodesic or grid) from the point to the
        snapped position ``snapped_xy`` — the same chainage every other KP
        tool reads (planar fraction of a segment x its measured length).
        """
        if self._route is None:
            return None
        hit = self._route.kp_at_point(QgsPointXY(map_point))
        return hit if hit.snapped_xy is not None else None

    def activate(self):
        super().activate()
        self.last_message = ""
        self.last_global_pos = None

    def _ensure_timers(self):
        """Recreate the tooltip timers if they have been torn down.

        cleanup_resources() sets them to None, but the canvas can keep
        delivering events to this tool afterwards (e.g. when another feature
        is being edited while the tool is restored as the active map tool),
        which used to raise AttributeError on every mouse move.
        """
        if self.mouse_stop_timer is None:
            self.mouse_stop_timer = QTimer(self.canvas)
            self.mouse_stop_timer.setSingleShot(True)
            self.mouse_stop_timer.timeout.connect(self.start_persistent_tooltip)
        if self.persistent_tooltip_timer is None:
            self.persistent_tooltip_timer = QTimer(self.canvas)
            self.persistent_tooltip_timer.timeout.connect(self.show_persistent_tooltip)

    def canvasMoveEvent(self, event):
        window = self.depth_profile_window
        profile_frozen = window is not None and window.isVisible() and window.frozen
        # Stop any timers and hide tooltips when the mouse moves.
        self._ensure_timers()
        self.persistent_tooltip_timer.stop()
        QToolTip.hideText()

        # Start the timer to detect when the mouse stops.
        self.mouse_stop_timer.start(500)

        # Convert the mouse event position to map coordinates.
        mousePoint = self.toMapCoordinates(event.pos())

        hit = self._locate(mousePoint)
        if hit is None or self.rubberBand is None or self.closestPointMarker is None:
            return
        closest_point_on_line = hit.snapped_xy

        # Update the rubber band and marker (one redraw, not one per vertex).
        self.rubberBand.setToGeometry(
            QgsGeometry.fromPolylineXY([QgsPointXY(mousePoint), closest_point_on_line]), None)
        self.closestPointMarker.setCenter(closest_point_on_line)
        self.last_closest_point = QgsPointXY(closest_point_on_line)

        # Distance between the mouse and the closest point, in display units.
        converted_distance = self._convert_distance(hit.dcc_m)
        chainage_km = hit.kp_km

        # Save these values for use in the right-click copy functionality.
        self.last_mouse_point = mousePoint
        self.last_distance = converted_distance
        self.last_chainage = chainage_km
        
        # Create a message and display it in the status bar and as a tooltip.
        message = f"KP: {chainage_km:.3f}"

        if self.showReverseKP:
            reverse_chainage_km = (self.total_length_meters / 1000.0) - chainage_km
            self.last_reverse_chainage = reverse_chainage_km
            message += f"\nrKP: {reverse_chainage_km:.3f}"
        else:
            self.last_reverse_chainage = None
        
        message += f"\nDCC: {converted_distance:.2f} {self.measurementUnit}"

        self.iface.mainWindow().statusBar().showMessage(message.replace('\n', ' | '))
        
        # Store the message and position for the timers.
        self.last_message = message
        self.last_global_pos = get_event_global_pos(event)

        # Freeze only the range line/profile; KP placement keeps tracking the map.
        if self.range_bearing_origin is not None and not profile_frozen:
            range_distance_m, bearing_deg = self._compute_range_bearing(self.range_bearing_origin, mousePoint)
            # Convert distance to display unit
            display_range = self._convert_distance(range_distance_m)
            bearing_text = f"{bearing_deg:06.2f}° {self._bearing_to_compass(bearing_deg)}"
            self.last_message += f"\nRange: {display_range:.2f} {self.measurementUnit}\nBearing: {bearing_text}"
            # Update graphical overlays
            self._update_range_bearing_graphics(mousePoint, range_distance_m)
            # Live depth profile along the range line (throttled internally).
            self._update_profile_window(mousePoint)
        elif self.range_bearing_origin is None:
            # Clear any existing range/bearing graphics if user cleared origin
            self._clear_range_bearing_graphics()
        if profile_frozen and not self._track_frozen_range_line(mousePoint, window):
            window.hide_cursor()

        # Show the standard, transient tooltip immediately (with augmented message if any)
        QToolTip.showText(self.last_global_pos, self.last_message, self.canvas)

    def start_persistent_tooltip(self):
        """Called when the mouse stop timer fires. Starts the persistent tooltip timer."""
        if self.persistent_tooltip_timer is not None and self.last_message and self.last_global_pos:
            self.persistent_tooltip_timer.start(100)

    def show_persistent_tooltip(self):
        """Called by the repeating timer to keep the tooltip visible."""
        if self.iface.mainWindow().isActiveWindow() and self.canvas.underMouse() and self.last_message and self.last_global_pos:
            QToolTip.showText(self.last_global_pos, self.last_message, self.canvas)

    def canvasLeaveEvent(self, event):
        """Stop timers and hide tooltip when mouse leaves the canvas."""
        if self.persistent_tooltip_timer is not None:
            self.persistent_tooltip_timer.stop()
        if self.mouse_stop_timer is not None:
            self.mouse_stop_timer.stop()
        QToolTip.hideText()

    def canvasPressEvent(self, event):
        window = self.depth_profile_window
        if (window is not None and window.isVisible() and window.frozen
                and window.pin_check.isChecked() and event.button() == Qt.MouseButton.LeftButton):
            return
        # Left click toggles range/bearing measurement: start -> stop -> start ...
        if event.button() == Qt.MouseButton.LeftButton:
            if self.range_bearing_origin is None:
                # Start measurement
                map_pt = self.toMapCoordinates(event.pos())
                self.range_bearing_origin = map_pt
                self.rangeBearingOriginMarker.setCenter(map_pt)
                self.rangeBearingOriginMarker.show()
                self._clear_range_bearing_graphics()  # will update on move
                self._show_profile_window()
                self.iface.mainWindow().statusBar().showMessage(
                    "Range/Bearing active. Move mouse; click again to clear.", 3000)
            else:
                # Stop measurement
                self.range_bearing_origin = None
                self.rangeBearingOriginMarker.hide()
                self._clear_range_bearing_graphics()
                self._hide_profile_window()
                self.iface.mainWindow().statusBar().showMessage(
                    "Range/Bearing cleared. Click to start again.", 3000)
            return

        # Right click now opens context menu with placement options
        if event.button() == Qt.MouseButton.RightButton:
            click_point = self.toMapCoordinates(event.pos())
            menu = QMenu(self.canvas)

            act_place = menu.addAction("Place Point")
            act_place_kp = menu.addAction("Place Point at Nearest KP")
            # Add range ring placement option if range/bearing is active
            if self.range_bearing_origin is not None and self.last_mouse_point is not None:
                act_place_ring = menu.addAction("Place Range Ring")
            else:
                act_place_ring = None
            # Add depth sampling options if depth layers are configured
            if self.showDepth and self.depthSources:
                act_sample_depth = menu.addAction("Sample Depth at Point")
                act_profile = menu.addAction("Live Depth Profile Along Range Line (D)")
                act_profile.setCheckable(True)
                act_profile.setChecked(self.showDepthProfile)
            else:
                act_sample_depth = None
                act_profile = None
            # Optional: keep original copy behaviour
            if self.last_mouse_point is not None and self.last_chainage is not None:
                act_copy = menu.addAction("Copy KP Info to Clipboard")
            else:
                act_copy = None

            # Go to KP...
            if self.features_geoms and self.total_length_meters and self.total_length_meters > 0:
                act_goto_kp = menu.addAction("Go to KP...")
            else:
                act_goto_kp = None

            chosen = qt_exec(menu, QCursor.pos())
            if not chosen:
                return

            if chosen == act_place:
                self._place_point(click_point, snapped_to_kp=False)
            elif chosen == act_place_kp:
                target_point = self.last_closest_point or click_point
                self._place_point(target_point, snapped_to_kp=True)
            elif act_place_ring and chosen == act_place_ring:
                self._place_range_ring()
            elif act_sample_depth and chosen == act_sample_depth:
                self._sample_and_display_depth(click_point)
            elif act_profile and chosen == act_profile:
                self._toggle_profile_window()
            elif act_goto_kp and chosen == act_goto_kp:
                self._show_go_to_kp_dialog()
            elif act_copy and chosen == act_copy:
                self._copy_kp_to_clipboard()

    def _show_go_to_kp_dialog(self):
        """Open the Go to KP dialog and pan canvas to the entered KP."""
        try:
            if not self.features_geoms or not self.total_length_meters or self.total_length_meters <= 0:
                self.iface.messageBar().pushMessage(
                    "Info",
                    "Go to KP is available after configuring a reference line.",
                    level=MESSAGE_INFO,
                    duration=3,
                )
                return

            min_kp = 0.0
            max_kp = float(self.total_length_meters) / 1000.0
            initial = float(self.last_chainage) if self.last_chainage is not None else None

            dialog = GoToKPDialog(self.iface.mainWindow(), min_kp, max_kp, initial_kp_km=initial)
            if not dialog.exec():
                return

            kp_km = dialog.chosen_kp_km()
            if kp_km is None:
                return

            target_point = self._point_at_kp_km(float(kp_km))
            if target_point is None:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Go to KP",
                    "Could not compute a point at that KP on the configured reference line.",
                )
                return

            self.canvas.setCenter(target_point)
            self.canvas.refresh()
        except Exception as e:  # noqa: BLE001 - report any failure to the user
            log_exception("KP Mouse Tool: Go to KP failed")
            self.iface.messageBar().pushMessage("Error", f"Go to KP failed: {e}", level=MESSAGE_CRITICAL, duration=4)

    def _point_at_kp_km(self, kp_km: float) -> Optional[QgsPointXY]:
        """Point on the reference line at ``kp_km`` (past the end: the last vertex)."""
        if self._route is None or kp_km is None or float(kp_km) < 0:
            return None
        return self._route.point_at_kp(float(kp_km), clamp=True)

    def _copy_kp_to_clipboard(self):
        """Copy KP info to clipboard using user-configured content."""
        if self.last_mouse_point is None or self.last_chainage is None:
            return
        try:
            parts = []
            parts.append(f"KP {self.last_chainage:.3f}")

            # rKP (compute even if showReverseKP isn't enabled)
            if self.copyIncludeRKP and self.total_length_meters and self.last_chainage is not None:
                rkp_val = self.last_reverse_chainage
                if rkp_val is None:
                    rkp_val = (self.total_length_meters / 1000.0) - float(self.last_chainage)
                parts.append(f"rKP {float(rkp_val):.3f}")

            # DCC
            if self.copyIncludeDCC and self.last_distance is not None:
                parts.append(f"DCC {self.last_distance:.2f} {self.measurementUnit}")

            # Lat/Lon
            if self.copyIncludeLatLon:
                wgs84_point = self._to_wgs84(self.last_mouse_point)
                lat = float(wgs84_point.y())
                lon = float(wgs84_point.x())
                parts.append(self._format_lat_lon(lat, lon, self.copyLatLonFormat, self.copyLatLonStyle))

            clipboard_text = "\n".join(parts)
            QApplication.clipboard().setText(clipboard_text)

            extras = self.copyIncludeRKP or self.copyIncludeDCC or self.copyIncludeLatLon
            feedback = "KP info copied to clipboard" if extras else "KP copied to clipboard"
            QToolTip.showText(QCursor.pos(), feedback, self.canvas)
            self.iface.mainWindow().statusBar().showMessage(feedback, 2000)
            self.iface.messageBar().pushMessage("Info", feedback, level=MESSAGE_INFO, duration=2)
        except Exception as e:  # noqa: BLE001 - report any failure to the user
            log_exception("KP Mouse Tool: copy to clipboard failed")
            self.iface.messageBar().pushMessage("Error", f"Copy failed: {e}", level=MESSAGE_CRITICAL, duration=4)

    def _format_lat_lon(self, lat: float, lon: float, fmt: str, style: str) -> str:
        fmt = (fmt or "DD").upper()
        style = (style or "LABELLED").upper()

        if fmt == "DDM":
            lat_s = self._format_ddm(lat, is_lat=True)
            lon_s = self._format_ddm(lon, is_lat=False)
            return self._format_lat_lon_pair(lat_s, lon_s, style)

        if fmt == "DMS":
            lat_s = self._format_dms(lat, is_lat=True)
            lon_s = self._format_dms(lon, is_lat=False)
            return self._format_lat_lon_pair(lat_s, lon_s, style)

        if fmt in ("DD_HEM", "DDH", "DD_HEMISPHERE"):
            lat_s = self._format_dd_hem(lat, is_lat=True)
            lon_s = self._format_dd_hem(lon, is_lat=False)
            return self._format_lat_lon_pair(lat_s, lon_s, style)

        # Default: decimal degrees (signed)
        lat_s = f"{lat:.6f}"
        lon_s = f"{lon:.6f}"
        return self._format_lat_lon_pair(lat_s, lon_s, style)

    def _format_lat_lon_pair(self, lat_s: str, lon_s: str, style: str) -> str:
        style = (style or "LABELLED").upper()
        if style == "SPACE":
            return f"{lat_s} {lon_s}"
        if style == "COMMA":
            return f"{lat_s}, {lon_s}"
        # Default: labelled
        return f"Lat {lat_s}, Lon {lon_s}"

    def _format_dd_hem(self, value: float, is_lat: bool) -> str:
        """Decimal degrees with hemisphere suffix (N/S/E/W) instead of signed +/-."""
        hemi = "N" if is_lat else "E"
        if value < 0:
            hemi = "S" if is_lat else "W"
        v = abs(float(value))
        return f"{v:.6f}{hemi}"

    def _format_ddm(self, value: float, is_lat: bool) -> str:
        """Degrees + decimal minutes, with hemisphere suffix (N/S/E/W)."""
        hemi = "N" if is_lat else "E"
        if value < 0:
            hemi = "S" if is_lat else "W"
        v = abs(float(value))
        deg = int(v)
        minutes = (v - deg) * 60.0
        # Round to 3 decimals of minutes, with carry
        minutes = round(minutes, 3)
        if minutes >= 60.0:
            deg += 1
            minutes = 0.0

        if is_lat:
            return f"{deg:02d}°{minutes:06.3f}'{hemi}"
        return f"{deg:03d}°{minutes:06.3f}'{hemi}"

    def _format_dms(self, value: float, is_lat: bool) -> str:
        """Degrees + minutes + seconds, with hemisphere suffix (N/S/E/W)."""
        hemi = "N" if is_lat else "E"
        if value < 0:
            hemi = "S" if is_lat else "W"
        v = abs(float(value))
        deg = int(v)
        minutes_full = (v - deg) * 60.0
        minute = int(minutes_full)
        seconds = (minutes_full - minute) * 60.0
        seconds = round(seconds, 2)
        if seconds >= 60.0:
            minute += 1
            seconds = 0.0
        if minute >= 60:
            deg += 1
            minute = 0

        if is_lat:
            return f"{deg:02d}°{minute:02d}'{seconds:05.2f}\"{hemi}"
        return f"{deg:03d}°{minute:02d}'{seconds:05.2f}\"{hemi}"

    # --- Point placement & layer helpers (moved from dialog) ---
    def _ensure_points_layer(self):
        """Ensure there is a memory point layer to receive placed points.

        Fields: name (string), kp (double), rkp (double), dcc (double), lat (double), lon (double), ref_line (string), comment (string)
        """
        project = QgsProject.instance()
        layer_name = "KP Points"
        for lyr in project.mapLayers().values():
            if lyr.name() == layer_name and lyr.type() == LAYER_VECTOR and lyr.geometryType() == GEOMETRY_POINT:
                provider = lyr.dataProvider()
                existing = {f.name().lower() for f in lyr.fields()}
                new_fields = []
                if 'ref_line' not in existing:
                    new_fields.append(QgsField("ref_line", FIELD_TYPE_STRING))
                if 'comment' not in existing:
                    new_fields.append(QgsField("comment", FIELD_TYPE_STRING))
                if new_fields:
                    provider.addAttributes(new_fields)
                    lyr.updateFields()
                return lyr
        crs = self.canvas.mapSettings().destinationCrs()
        layer = QgsVectorLayer(f"Point?crs={crs.authid()}", layer_name, "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("name", FIELD_TYPE_STRING),
            QgsField("kp", FIELD_TYPE_DOUBLE),
            QgsField("rkp", FIELD_TYPE_DOUBLE),
            QgsField("dcc", FIELD_TYPE_DOUBLE),
            QgsField("lat", FIELD_TYPE_DOUBLE),
            QgsField("lon", FIELD_TYPE_DOUBLE),
            QgsField("ref_line", FIELD_TYPE_STRING),
            QgsField("comment", FIELD_TYPE_STRING),
        ])
        layer.updateFields()
        project.addMapLayer(layer)
        return layer

    def _place_point(self, point_xy: QgsPointXY, snapped_to_kp: bool):
        """Add a point feature at the given map coordinate.

        Always records KP / rKP / DCC / lat / lon using the most recent mouse-calculated values.
        snapped_to_kp indicates whether the geometry itself has been snapped to the nearest point
        on the reference line (so DCC is forced to 0.0) or is the original click position (DCC
        reflects perpendicular distance like the tooltip).
        """
        try:
            layer = self._ensure_points_layer()
            pr = layer.dataProvider()
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(point_xy))

            ll_point = self._to_wgs84(point_xy)

            kp_val = None
            rkp_val = None
            dcc_val = None
            if self.last_chainage is not None:
                kp_val = float(self.last_chainage)
                if self.total_length_meters:
                    rkp_val = (self.total_length_meters / 1000.0) - kp_val
                # DCC: 0 if snapped (nearest KP), else last perpendicular distance
                if snapped_to_kp:
                    dcc_val = 0.0
                else:
                    dcc_val = self.last_distance if self.last_distance is not None else None

            name_val = "Point" if kp_val is None else f"KP {kp_val:.3f}"

            dlg = KPPointDialog(self.iface.mainWindow(), kp=kp_val, rkp=rkp_val, dcc=dcc_val,
                                 ref_line=self.layer.name() if self.layer else "", comment="")
            if qt_exec(dlg) != DIALOG_ACCEPTED:
                return
            comment_text = dlg.get_comment()

            attr_values = {
                'name': name_val,
                'kp': kp_val,
                'rkp': rkp_val,
                'dcc': dcc_val,
                'lat': ll_point.y(),
                'lon': ll_point.x(),
                'ref_line': self.layer.name() if self.layer else None,
                'comment': comment_text or None,
            }
            for field in layer.fields():
                fname = field.name()
                if fname in attr_values:
                    feat.setAttribute(fname, attr_values[fname])
            pr.addFeatures([feat])
            layer.updateExtents()
            self.iface.layerTreeView().refreshLayerSymbology(layer.id())
            self.canvas.refresh()
            msg = "Point placed at nearest KP" if snapped_to_kp else "Point placed"
            self.iface.mainWindow().statusBar().showMessage(msg, 3000)
            self.iface.messageBar().pushMessage("Success", msg, level=MESSAGE_SUCCESS, duration=2)
        except Exception as e:  # noqa: BLE001 - report any failure to the user
            log_exception("KP Mouse Tool: placing a point failed")
            self.iface.messageBar().pushMessage("Error", f"Failed to place point: {e}", level=MESSAGE_CRITICAL, duration=4)

    def _ensure_lines_layer(self):
        """Ensure there is a memory line layer to receive placed lines.

        Fields: name (string), range (double), bearing (double), range_unit (string), 
                origin_lat (double), origin_lon (double), target_lat (double), target_lon (double), 
                ref_line (string), comment (string)
        """
        project = QgsProject.instance()
        layer_name = "KP Range Lines"
        for lyr in project.mapLayers().values():
            if lyr.name() == layer_name and lyr.type() == LAYER_VECTOR and lyr.geometryType() == GEOMETRY_LINE:
                provider = lyr.dataProvider()
                existing = {f.name().lower() for f in lyr.fields()}
                new_fields = []
                if 'ref_line' not in existing:
                    new_fields.append(QgsField("ref_line", FIELD_TYPE_STRING))
                if 'comment' not in existing:
                    new_fields.append(QgsField("comment", FIELD_TYPE_STRING))
                if new_fields:
                    provider.addAttributes(new_fields)
                    lyr.updateFields()
                return lyr
        crs = self.canvas.mapSettings().destinationCrs()
        layer = QgsVectorLayer(f"LineString?crs={crs.authid()}", layer_name, "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("name", FIELD_TYPE_STRING),
            QgsField("range", FIELD_TYPE_DOUBLE),
            QgsField("bearing", FIELD_TYPE_DOUBLE),
            QgsField("range_unit", FIELD_TYPE_STRING),
            QgsField("origin_lat", FIELD_TYPE_DOUBLE),
            QgsField("origin_lon", FIELD_TYPE_DOUBLE),
            QgsField("target_lat", FIELD_TYPE_DOUBLE),
            QgsField("target_lon", FIELD_TYPE_DOUBLE),
            QgsField("ref_line", FIELD_TYPE_STRING),
            QgsField("comment", FIELD_TYPE_STRING),
        ])
        layer.updateFields()
        project.addMapLayer(layer)
        return layer

    def _ensure_polygons_layer(self):
        """Ensure there is a memory polygon layer to receive placed polygons.

        Fields: name (string), radius (double), radius_unit (string), center_lat (double), center_lon (double), 
                ref_line (string), comment (string)
        """
        project = QgsProject.instance()
        layer_name = "KP Range Rings"
        for lyr in project.mapLayers().values():
            if lyr.name() == layer_name and lyr.type() == LAYER_VECTOR and lyr.geometryType() == GEOMETRY_POLYGON:
                provider = lyr.dataProvider()
                existing = {f.name().lower() for f in lyr.fields()}
                new_fields = []
                if 'ref_line' not in existing:
                    new_fields.append(QgsField("ref_line", FIELD_TYPE_STRING))
                if 'comment' not in existing:
                    new_fields.append(QgsField("comment", FIELD_TYPE_STRING))
                if new_fields:
                    provider.addAttributes(new_fields)
                    lyr.updateFields()
                return lyr
        crs = self.canvas.mapSettings().destinationCrs()
        layer = QgsVectorLayer(f"Polygon?crs={crs.authid()}", layer_name, "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("name", FIELD_TYPE_STRING),
            QgsField("radius", FIELD_TYPE_DOUBLE),
            QgsField("radius_unit", FIELD_TYPE_STRING),
            QgsField("center_lat", FIELD_TYPE_DOUBLE),
            QgsField("center_lon", FIELD_TYPE_DOUBLE),
            QgsField("ref_line", FIELD_TYPE_STRING),
            QgsField("comment", FIELD_TYPE_STRING),
        ])
        layer.updateFields()
        project.addMapLayer(layer)
        return layer

    def _place_range_ring(self):
        """Place the current range ring (line and circle) as permanent features."""
        if self.range_bearing_origin is None or self.last_mouse_point is None:
            return

        try:
            # Calculate range and bearing
            range_distance_m, bearing_deg = self._compute_range_bearing(self.range_bearing_origin, self.last_mouse_point)
            display_range = self._convert_distance(range_distance_m)

            # Transform points to WGS84 for lat/lon storage
            origin_ll = self._to_wgs84(self.range_bearing_origin)
            target_ll = self._to_wgs84(self.last_mouse_point)

            # Create line geometry
            line_geom = QgsGeometry.fromPolylineXY([self.range_bearing_origin, self.last_mouse_point])

            # The same geodesic ring the rubber band shows.
            if range_distance_m <= 0:
                return
            circle_geom = self._range_ring_geometry(self.range_bearing_origin, range_distance_m)

            # Create line feature
            line_layer = self._ensure_lines_layer()
            line_feat = QgsFeature(line_layer.fields())
            line_feat.setGeometry(line_geom)

            bearing_text = f"{bearing_deg:06.2f}°"
            line_name = f"Range Line {display_range:.2f} {self.measurementUnit}"

            # Show dialog for user to add comment
            dlg = KPRangeRingDialog(self.iface.mainWindow(), 
                                   range_val=display_range, 
                                   bearing=bearing_deg, 
                                   range_unit=self.measurementUnit,
                                   ref_line=self.layer.name() if self.layer else "")
            if qt_exec(dlg) != DIALOG_ACCEPTED:
                return
            comment_text = dlg.get_comment()

            line_attr_values = {
                'name': line_name,
                'range': display_range,
                'bearing': bearing_deg,
                'range_unit': self.measurementUnit,
                'origin_lat': origin_ll.y(),
                'origin_lon': origin_ll.x(),
                'target_lat': target_ll.y(),
                'target_lon': target_ll.x(),
                'ref_line': self.layer.name() if self.layer else None,
                'comment': comment_text or None,
            }
            for field in line_layer.fields():
                fname = field.name()
                if fname in line_attr_values:
                    line_feat.setAttribute(fname, line_attr_values[fname])

            # Create circle feature
            circle_layer = self._ensure_polygons_layer()
            circle_feat = QgsFeature(circle_layer.fields())
            circle_feat.setGeometry(circle_geom)

            circle_name = f"Range Ring {display_range:.2f} {self.measurementUnit}"

            circle_attr_values = {
                'name': circle_name,
                'radius': display_range,
                'radius_unit': self.measurementUnit,
                'center_lat': origin_ll.y(),
                'center_lon': origin_ll.x(),
                'ref_line': self.layer.name() if self.layer else None,
                'comment': comment_text or None,
            }
            for field in circle_layer.fields():
                fname = field.name()
                if fname in circle_attr_values:
                    circle_feat.setAttribute(fname, circle_attr_values[fname])

            # Add features to layers
            line_layer.dataProvider().addFeatures([line_feat])
            circle_layer.dataProvider().addFeatures([circle_feat])

            line_layer.updateExtents()
            circle_layer.updateExtents()

            self.iface.layerTreeView().refreshLayerSymbology(line_layer.id())
            self.iface.layerTreeView().refreshLayerSymbology(circle_layer.id())
            self.canvas.refresh()

            msg = f"Range ring placed: {display_range:.2f} {self.measurementUnit} at {bearing_text}"
            self.iface.mainWindow().statusBar().showMessage(msg, 3000)
            self.iface.messageBar().pushMessage("Success", msg, level=MESSAGE_SUCCESS, duration=3)

        except Exception as e:  # noqa: BLE001 - report any failure to the user
            log_exception("KP Mouse Tool: placing a range ring failed")
            self.iface.messageBar().pushMessage("Error", f"Failed to place range ring: {e}", level=MESSAGE_CRITICAL, duration=4)

    # --- Depth sampling / live profile helpers ---
    def _get_depth_sampler(self):
        """Lazily build the multi-layer depth sampler in the project CRS."""
        if self._depth_sampler is None and self.depthSources:
            from .kp_depth_utils import DepthSampler
            self._depth_sampler = DepthSampler(
                self.canvas.mapSettings().destinationCrs(), self.depthSources)
        return self._depth_sampler

    def _ensure_profile_window(self):
        """Create (or return) the live profile window when it is enabled."""
        if not (self.showDepth and self.showDepthProfile and self.depthSources):
            return None
        sampler = self._get_depth_sampler()
        if sampler is None or not sampler.has_sources():
            return None
        if self.depth_profile_window is None:
            from .kp_depth_profile_window import KPDepthProfileWindow
            window = KPDepthProfileWindow(self.iface.mainWindow(), self.measurementUnit)
            # The tool's own route model: profile KP labels read the same KP.
            window.configure(sampler, self.distanceArea, self._route)
            window.cursorMoved.connect(self._on_profile_cursor)
            self.depth_profile_window = window
        return self.depth_profile_window

    def _show_profile_window(self):
        window = self._ensure_profile_window()
        if window is not None:
            window.user_closed = False
            window.clear_profile()
            window.show()

    def _toggle_profile_window(self):
        """Toggle the live depth profile (right-click menu / D key).

        The choice persists in QSettings so it survives tool restarts.
        """
        if not (self.showDepth and self.depthSources):
            self.iface.messageBar().pushMessage(
                "KP Mouse Tool",
                "Configure depth layers first (tool menu → Configure…) to use "
                "the live depth profile.",
                level=MESSAGE_INFO, duration=5)
            return
        self.showDepthProfile = not self.showDepthProfile
        QSettings("SubseaCableTools", "KPMouseTool").setValue(
            "showDepthProfile", self.showDepthProfile)
        if self.showDepthProfile:
            if self.range_bearing_origin is not None:
                self._show_profile_window()
            else:
                self.iface.mainWindow().statusBar().showMessage(
                    "Live depth profile on — left-click to start a "
                    "range/bearing measurement.", 4000)
        else:
            self._hide_profile_window()
            self.iface.mainWindow().statusBar().showMessage(
                "Live depth profile off.", 2000)

    def _hide_profile_window(self):
        window = self.depth_profile_window
        if window is not None and not _sip_isdeleted(window):
            window.clear_profile()
            window.hide()

    def _on_profile_cursor(self, point):
        """Mirror the profile plot cursor with a marker on the range line."""
        marker = self.profileCursorMarker
        if marker is None or _sip_isdeleted(marker):
            return
        if point is None:
            marker.hide()
            return
        marker.setCenter(QgsPointXY(point))
        marker.show()

    def _track_frozen_range_line(self, mouse_point: QgsPointXY, window) -> bool:
        """Drive the frozen profile's cursor from the map pointer.

        When the pointer is within a few pixels of the frozen range line,
        its projection onto the line becomes the plot cursor (and marker).
        Returns True while tracking.
        """
        endpoints = (window._profile or {}).get('endpoints')
        length = (window._profile or {}).get('length_m', 0)
        if not endpoints or length <= 0:
            return False
        (ax, ay), (bx, by) = endpoints
        dx, dy = bx - ax, by - ay
        planar_sq = dx * dx + dy * dy
        if planar_sq <= 0:
            return False
        t = ((mouse_point.x() - ax) * dx + (mouse_point.y() - ay) * dy) / planar_sq
        if not 0.0 <= t <= 1.0:
            return False
        px, py = ax + t * dx, ay + t * dy
        units_per_px = self.canvas.mapUnitsPerPixel() or 1.0
        if math.hypot(mouse_point.x() - px, mouse_point.y() - py) / units_per_px > 12.0:
            return False
        window.show_cursor_at_distance(t * length)
        return True

    def _update_profile_window(self, mouse_point: QgsPointXY):
        window = self.depth_profile_window
        if (window is not None and not window.user_closed and window.isVisible()
                and self.range_bearing_origin is not None):
            window.schedule(QgsPointXY(self.range_bearing_origin),
                            QgsPointXY(mouse_point))

    # --- Range/Bearing helper functionality (moved from dialog) ---
    def _convert_distance(self, distance_meters: float) -> float:
        if self.measurementUnit == "m":
            return distance_meters
        if self.measurementUnit == "km":
            return distance_meters / 1000.0
        if self.measurementUnit == "nautical miles":
            return distance_meters / 1852.0
        if self.measurementUnit == "miles":
            return distance_meters / 1609.34
        return distance_meters

    def _wgs84_transforms(self):
        """``(map CRS -> WGS84, WGS84 -> map CRS)``, or ``(None, None)`` when
        the map is WGS84. Built once per map CRS — constructing transforms
        on every mouse move was a large share of the range/bearing cost."""
        crs = self.canvas.mapSettings().destinationCrs()
        if self._wgs84_xforms is None or self._wgs84_crs != crs:
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            if crs == wgs84:
                self._wgs84_xforms = (None, None)
            else:
                project = QgsProject.instance()
                self._wgs84_xforms = (QgsCoordinateTransform(crs, wgs84, project),
                                      QgsCoordinateTransform(wgs84, crs, project))
            self._wgs84_crs = crs
        return self._wgs84_xforms

    def _to_wgs84(self, point: QgsPointXY) -> QgsPointXY:
        to_wgs84 = self._wgs84_transforms()[0]
        return to_wgs84.transform(QgsPointXY(point)) if to_wgs84 is not None else QgsPointXY(point)

    def _spheroid(self) -> QgsDistanceArea:
        """WGS84 spheroid for ring geometry, whatever the KP distance mode
        (a planar Cartesian distance area has no ellipsoid to project on)."""
        spheroid = getattr(self, '_wgs84_spheroid', None)
        if spheroid is None:
            spheroid = QgsDistanceArea()
            spheroid.setEllipsoid("WGS84")
            self._wgs84_spheroid = spheroid
        return spheroid

    def _compute_range_bearing(self, origin: QgsPointXY, target: QgsPointXY):
        try:
            o_ll = self._to_wgs84(origin)
            t_ll = self._to_wgs84(target)
            lat1 = math.radians(o_ll.y())
            lat2 = math.radians(t_ll.y())
            dlon = math.radians(t_ll.x() - o_ll.x())
            x = math.sin(dlon) * math.cos(lat2)
            y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
            bearing = math.degrees(math.atan2(x, y))
            bearing = (bearing + 360.0) % 360.0
            distance_m = self.distanceArea.measureLine(origin, target)
            return distance_m, bearing
        except QgsCsException:
            # Outside the CRS's valid area: planar map units (debug-logged,
            # this runs on every mouse move).
            log_exception("KP Mouse Tool: range/bearing fell back to planar map units",
                          level=logging.DEBUG)
            dx = target.x() - origin.x()
            dy = target.y() - origin.y()
            distance_m = math.hypot(dx, dy)
            bearing = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
            return distance_m, bearing

    def _bearing_to_compass(self, bearing_deg: float) -> str:
        dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        idx = int((bearing_deg + 11.25) // 22.5) % 16
        return dirs[idx]

    def _range_ring_geometry(self, origin: QgsPointXY, range_m: float,
                             segments: int = 180) -> QgsGeometry:
        """Polygon of the range ring of ``range_m`` metres around ``origin``.

        A true geodesic circle on the WGS84 spheroid, transformed to the map
        CRS in one call — correct for any map CRS (Web Mercator, UTM,
        geographic, non-metre units), where a flat circle drifts off the
        cursor. The planar approximation is only a fallback for points
        outside the CRS's valid area.
        """
        to_wgs84, from_wgs84 = self._wgs84_transforms()
        try:
            origin_ll = to_wgs84.transform(QgsPointXY(origin)) if to_wgs84 is not None else QgsPointXY(origin)
            spheroid = self._spheroid()
            ring = [spheroid.computeSpheroidProject(origin_ll, range_m, 2.0 * math.pi * i / segments)
                    for i in range(segments + 1)]
            geom = QgsGeometry.fromPolygonXY([ring])
            if from_wgs84 is not None:
                geom.transform(from_wgs84)
            # A ring reaching outside the map CRS's domain (e.g. past the
            # pole in Web Mercator) comes back with inf coordinates.
            if geometry_is_finite(geom):
                return geom
        except QgsCsException:
            log_exception("KP Mouse Tool: geodesic range ring failed; using a planar ring",
                          level=logging.DEBUG)
        if self.canvas.mapSettings().destinationCrs().isGeographic():
            lat_rad = math.radians(origin.y())
            radius_x = range_m / (111320.0 * max(math.cos(lat_rad), 1e-6))
            radius_y = range_m / 111320.0
        else:
            radius_x = radius_y = range_m
        ring = [QgsPointXY(origin.x() + radius_x * math.sin(2 * math.pi * i / segments),
                           origin.y() + radius_y * math.cos(2 * math.pi * i / segments))
                for i in range(segments + 1)]
        return QgsGeometry.fromPolygonXY([ring])

    def _update_range_bearing_graphics(self, mousePoint: QgsPointXY, range_meters: float):
        line, circle, origin = self.rangeBearingLine, self.rangeBearingCircle, self.range_bearing_origin
        if line is None or circle is None or origin is None:
            return
        # One geometry update per band (addPoint redrew the band per vertex).
        line.setToGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(origin), QgsPointXY(mousePoint)]), None)
        if range_meters <= 0:
            circle.reset(GEOMETRY_POLYGON)
            self._ring_key = None
            return
        # Keep the drawn ring while its radius changes by under half a pixel
        # (e.g. moving around the circle): 181 spheroid projections per move
        # otherwise, for no visible change.
        units = math.hypot(mousePoint.x() - origin.x(), mousePoint.y() - origin.y())
        pixels = units / (self.canvas.mapUnitsPerPixel() or 1.0)
        key = (origin.x(), origin.y())
        if (self._ring_key is not None and self._ring_key[0] == key and pixels > 0
                and abs(self._ring_key[1] - range_meters) <= 0.5 * range_meters / pixels):
            return
        circle.setToGeometry(self._range_ring_geometry(origin, range_meters), None)
        self._ring_key = (key, range_meters)

    def _clear_range_bearing_graphics(self):
        # Runs on every mouse move without a range origin: only touch the
        # bands when something is drawn.
        for band, kind in ((self.rangeBearingLine, GEOMETRY_LINE),
                           (self.rangeBearingCircle, GEOMETRY_POLYGON)):
            if band is not None and not _sip_isdeleted(band) and band.numberOfVertices():
                band.reset(kind)
        self._ring_key = None

    def _sample_depth_at_point(self, point: QgsPointXY):
        """Sample depth at the given point from the configured depth layers.

        Returns depth value as float, or None if not available or outside every
        layer's extent. Rasters win by resolution; contour layers contribute the
        nearest feature's depth value. Sampling errors propagate to the caller,
        which reports them (they are not "no depth here").
        """
        if not self.showDepth or not self.depthSources:
            return None
        sampler = self._get_depth_sampler()
        if sampler is None:
            return None
        return sampler.sample_point(QgsPointXY(point))

    def _sample_and_display_depth(self, point: QgsPointXY):
        """Sample depth at given point and display result in a message."""
        if not self.showDepth or not self.depthSources:
            msg = "Depth sampling is not configured. Please configure depth layers in the KP Mouse Tool settings."
            self.iface.mainWindow().statusBar().showMessage(msg, 4000)
            self.iface.messageBar().pushMessage("Info", msg, level=MESSAGE_WARNING, duration=5)
            return
            
        try:
            depth = self._sample_depth_at_point(point)
            if depth is not None:
                msg = f"Depth at point: {depth:.2f} m"
                self.iface.mainWindow().statusBar().showMessage(msg, 4000)
                self.iface.messageBar().pushMessage("Depth", msg, level=MESSAGE_INFO, duration=5)
                QToolTip.showText(QCursor.pos(), msg, self.canvas)
            else:
                msg = "No depth value available at this location (outside layer extent?)"
                self.iface.mainWindow().statusBar().showMessage(msg, 4000)
                self.iface.messageBar().pushMessage("Info", msg, level=MESSAGE_WARNING, duration=5)
        except Exception as e:  # noqa: BLE001 - report any failure to the user
            log_exception("KP Mouse Tool: depth sampling failed")
            msg = f"Error sampling depth: {e}"
            self.iface.messageBar().pushMessage("Error", msg, level=MESSAGE_CRITICAL, duration=4)

    def deactivate(self):
        if self.mouse_stop_timer is not None:
            self.mouse_stop_timer.stop()
        if self.persistent_tooltip_timer is not None:
            self.persistent_tooltip_timer.stop()
        QToolTip.hideText()
        if self.rubberBand is not None:
            self.rubberBand.reset(GEOMETRY_LINE)
        if self.closestPointMarker is not None:
            self.closestPointMarker.hide()
        self._clear_range_bearing_graphics()
        self._hide_profile_window()
        if self.rangeBearingOriginMarker is not None:
            self.rangeBearingOriginMarker.hide()
        if self.iface and self.iface.mainWindow():
            self.iface.mainWindow().statusBar().clearMessage()
        QgsMapTool.deactivate(self)

    def cleanup_resources(self):
        """Release timers, canvas items, the profile window and the route.

        Canvas items are detached from the scene (``QgsVertexMarker`` has no
        ``deleteLater()`` on QGIS 3, so the old calls silently left every
        marker in the canvas). Safe to call more than once.
        """
        for timer_attr in ('mouse_stop_timer', 'persistent_tooltip_timer'):
            timer = getattr(self, timer_attr, None)
            if timer is not None and not _sip_isdeleted(timer):
                timer.stop()
                try:
                    timer.timeout.disconnect()
                except TypeError:  # nothing connected
                    pass
                timer.deleteLater()
            setattr(self, timer_attr, None)
        QToolTip.hideText()
        try:
            self.canvas.destinationCrsChanged.disconnect(self._on_map_crs_changed)
        except (TypeError, RuntimeError):  # already disconnected / canvas gone
            pass
        for attr in ('rubberBand', 'closestPointMarker', 'rangeBearingLine',
                     'rangeBearingCircle', 'rangeBearingOriginMarker', 'profileCursorMarker'):
            remove_canvas_item(getattr(self, attr, None))
            setattr(self, attr, None)
        self._route = None
        self.features_geoms = []
        self.total_length_meters = 0
        self._ring_key = None
        self.range_bearing_origin = None
        window = getattr(self, 'depth_profile_window', None)
        if window is not None and not _sip_isdeleted(window):
            window.cleanup()
        self.depth_profile_window = None
        self._depth_sampler = None
        self.depthSources = []

    def keyPressEvent(self, event):  # type: ignore
        if event.key() == Qt.Key.Key_Escape and self.range_bearing_origin is not None:
            self.range_bearing_origin = None
            if self.rangeBearingOriginMarker is not None:
                self.rangeBearingOriginMarker.hide()
            self._clear_range_bearing_graphics()
            self._hide_profile_window()
            self.iface.mainWindow().statusBar().showMessage("Range/Bearing cleared (ESC).", 2000)
        elif event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            window = self.depth_profile_window
            if window is not None and window.isVisible():
                window.set_frozen(not window.frozen)
                QToolTip.hideText()
        elif event.key() == Qt.Key.Key_D:
            self._toggle_profile_window()

class KPPointDialog(QDialog):
    """Dialog to confirm/edit attributes for a placed KP point."""
    def __init__(self, parent, kp=None, rkp=None, dcc=None, ref_line="", comment=""):
        super().__init__(parent)
        self.setWindowTitle("Add KP Point")
        layout = QVBoxLayout(self)

        def add_row(label_text, value, editable=False):
            row = QHBoxLayout()
            lab = QLabel(label_text)
            edit = QLineEdit()
            if value is not None:
                edit.setText(f"{value:.3f}" if isinstance(value, float) else str(value))
            edit.setReadOnly(not editable)
            row.addWidget(lab)
            row.addWidget(edit)
            layout.addLayout(row)
            return edit

        self.edit_kp = add_row("KP", kp)
        self.edit_rkp = add_row("rKP", rkp)
        self.edit_dcc = add_row("DCC", dcc)
        self.edit_ref = add_row("Ref Line", ref_line)
        self.edit_comment = add_row("Comment", comment, editable=True)

        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_comment(self):
        return self.edit_comment.text().strip()


class KPRangeRingDialog(QDialog):
    """Dialog to confirm/edit attributes for a placed range ring."""
    def __init__(self, parent, range_val=None, bearing=None, range_unit="km", ref_line="", comment=""):
        super().__init__(parent)
        self.setWindowTitle("Add Range Ring")
        layout = QVBoxLayout(self)

        def add_row(label_text, value, editable=False):
            row = QHBoxLayout()
            lab = QLabel(label_text)
            edit = QLineEdit()
            if value is not None:
                if isinstance(value, float):
                    edit.setText(f"{value:.3f}")
                else:
                    edit.setText(str(value))
            edit.setReadOnly(not editable)
            row.addWidget(lab)
            row.addWidget(edit)
            layout.addLayout(row)
            return edit

        self.edit_range = add_row("Range", range_val)
        self.edit_bearing = add_row("Bearing", bearing)
        self.edit_unit = add_row("Unit", range_unit)
        self.edit_ref = add_row("Ref Line", ref_line)
        self.edit_comment = add_row("Comment", comment, editable=True)

        buttons = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def get_comment(self):
        return self.edit_comment.text().strip()


class KPConfigDialog(QDialog):
    """A dialog for configuring the KP Mouse Tool settings."""
    def __init__(
        self,
        parent=None,
        current_layer=None,
        current_unit="km",
        show_reverse_kp=False,
        current_use_cartesian=False,
        show_depth=False,
        depth_sources=None,
        copy_include_rkp=False,
        copy_include_dcc=False,
        copy_include_latlon=False,
        copy_latlon_format="DD",
        copy_latlon_style="LABELLED",
    ):
        super().__init__(parent)
        self.setWindowTitle("Configure KP Mouse Tool")
        layout = QVBoxLayout(self)

        # Layer selection
        self.layer_label = QLabel("Select Reference Line Layer:")
        self.layer_combo = QComboBox()
        layout.addWidget(self.layer_label)
        layout.addWidget(self.layer_combo)

        # Metrics display
        self.metrics_label = QLabel("Layer Metrics:")
        self.metrics_text = QLabel("")
        layout.addWidget(self.metrics_label)
        layout.addWidget(self.metrics_text)

        # Unit selection
        self.unit_label = QLabel("Select Measurement Unit:")
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["m", "km", "nautical miles", "miles"])
        layout.addWidget(self.unit_label)
        layout.addWidget(self.unit_combo)

        # Reverse KP checkbox
        self.reverse_kp_checkbox = QCheckBox("Show Reverse KP")
        self.reverse_kp_checkbox.setChecked(show_reverse_kp)
        layout.addWidget(self.reverse_kp_checkbox)

        # Cartesian checkbox
        self.cartesian_checkbox = QCheckBox(
            "Cartesian (grid) KP — plugin-wide setting")
        self.cartesian_checkbox.setToolTip(
            "Measure KP as planar grid distances instead of geodesic (WGS84). This is "
            "the plugin-wide KP setting: KP tools, docks and new Burial Planner plans "
            "all follow it. The grid CRS is chosen in Subsea Cable Tools ▸ KP settings "
            "(default: the project CRS when projected, else the route's UTM zone).")
        self.cartesian_checkbox.setChecked(current_use_cartesian)
        layout.addWidget(self.cartesian_checkbox)

        # Depth options
        self.depth_checkbox = QCheckBox("Enable depth sampling (right-click menu)")
        self.depth_checkbox.setChecked(show_depth)
        layout.addWidget(self.depth_checkbox)

        self.depth_layers_label = QLabel(
            "Depth layers (check to use; rasters and contour line layers can be "
            "combined to cover the whole route — higher resolution rasters win "
            "where they overlap):")
        self.depth_layers_label.setWordWrap(True)
        layout.addWidget(self.depth_layers_label)
        self.depth_table = QTableWidget(0, 2)
        self.depth_table.setHorizontalHeaderLabels(["Layer", "Depth field (contours)"])
        source_options_btn = QPushButton("Bathymetry source conventions…")
        from ..bathymetry_sampling import configure_layers
        source_options_btn.clicked.connect(lambda: configure_layers(self))
        self.depth_table.verticalHeader().setVisible(False)
        self.depth_table.setSelectionMode(SELECTION_MODE_NONE)
        self.depth_table.setMinimumHeight(120)
        layout.addWidget(self.depth_table)
        layout.addWidget(source_options_btn)

        self.profile_hint_label = QLabel(
            "Tip: while the tool is active, toggle the live depth profile "
            "along the range/bearing line from the right-click menu or by "
            "pressing D.")
        self.profile_hint_label.setWordWrap(True)
        layout.addWidget(self.profile_hint_label)

        # Copy-to-clipboard options
        self.copy_label = QLabel("Right-click copy contents:")
        layout.addWidget(self.copy_label)

        self.copy_kp_label = QLabel("- KP is always included (formatted as: 'KP 123.456')")
        self.copy_kp_label.setWordWrap(True)
        layout.addWidget(self.copy_kp_label)

        self.copy_rkp_checkbox = QCheckBox("Include Reverse KP (rKP)")
        self.copy_rkp_checkbox.setChecked(bool(copy_include_rkp))
        layout.addWidget(self.copy_rkp_checkbox)

        self.copy_dcc_checkbox = QCheckBox("Include DCC")
        self.copy_dcc_checkbox.setChecked(bool(copy_include_dcc))
        layout.addWidget(self.copy_dcc_checkbox)

        self.copy_latlon_checkbox = QCheckBox("Include Lat/Lon")
        self.copy_latlon_checkbox.setChecked(bool(copy_include_latlon))
        layout.addWidget(self.copy_latlon_checkbox)

        self.copy_latlon_format_label = QLabel("Lat/Lon format:")
        self.copy_latlon_format_combo = QComboBox()
        self.copy_latlon_format_combo.addItem("DD (decimal degrees)", "DD")
        self.copy_latlon_format_combo.addItem("DD (decimal degrees + N/S/E/W)", "DD_HEM")
        self.copy_latlon_format_combo.addItem("DDM (degrees decimal minutes)", "DDM")
        self.copy_latlon_format_combo.addItem("DMS (degrees minutes seconds)", "DMS")
        layout.addWidget(self.copy_latlon_format_label)
        layout.addWidget(self.copy_latlon_format_combo)

        self.copy_latlon_style_label = QLabel("Lat/Lon output style:")
        self.copy_latlon_style_combo = QComboBox()
        self.copy_latlon_style_combo.addItem("Labelled (Lat …, Lon …)", "LABELLED")
        self.copy_latlon_style_combo.addItem("Paste-friendly (lat, lon)", "COMMA")
        self.copy_latlon_style_combo.addItem("Paste-friendly (lat lon)", "SPACE")
        layout.addWidget(self.copy_latlon_style_label)
        layout.addWidget(self.copy_latlon_style_combo)

        # Note about calculations
        self.note_label = QLabel(
            "Note: KP is geodesic on WGS84 by default. Cartesian (grid) uses planar "
            "distances in the grid CRS set in Subsea Cable Tools ▸ KP settings "
            f"(currently: {describe_kp_mode(KP_MODE_CARTESIAN)})."
        )
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)

        # Buttons
        self.button_box = QDialogButtonBox(BUTTON_BOX_OK | BUTTON_BOX_CANCEL)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.line_layers = [
            l for l in QgsProject.instance().mapLayers().values()
            if l.type() == LAYER_VECTOR and l.geometryType() == GEOMETRY_LINE
        ]
        
        for layer in self.line_layers:
            self.layer_combo.addItem(layer.name(), layer.id())

        # Depth layers: raster and vector line layers
        self.depth_layers = [
            l for l in QgsProject.instance().mapLayers().values()
            if (l.type() == LAYER_RASTER) or (l.type() == LAYER_VECTOR and l.geometryType() == GEOMETRY_LINE)
        ]
        self._populate_depth_table(depth_sources or [])

        if current_layer:
            idx = self.layer_combo.findData(current_layer.id())
            if idx != -1:
                self.layer_combo.setCurrentIndex(idx)

        if current_unit:
            idx = self.unit_combo.findText(current_unit)
            if idx != -1:
                self.unit_combo.setCurrentIndex(idx)

        self.layer_combo.currentIndexChanged.connect(self.update_metrics)
        self.depth_checkbox.toggled.connect(self.update_depth_ui)
        self.copy_latlon_checkbox.toggled.connect(self.update_copy_ui)
        self.update_metrics()
        self.update_depth_ui()
        self.update_copy_ui()

        # Restore Lat/Lon format selection
        if copy_latlon_format:
            idx = self.copy_latlon_format_combo.findData(str(copy_latlon_format).upper())
            if idx != -1:
                self.copy_latlon_format_combo.setCurrentIndex(idx)

        if copy_latlon_style:
            idx = self.copy_latlon_style_combo.findData(str(copy_latlon_style).upper())
            if idx != -1:
                self.copy_latlon_style_combo.setCurrentIndex(idx)

    def _populate_depth_table(self, depth_sources):
        """One row per candidate layer: checkable name + field combo for contours."""
        selected = {str(layer_id): str(field or "")
                    for layer_id, field in (depth_sources or [])}
        self.depth_table.setRowCount(len(self.depth_layers))
        for row, layer in enumerate(self.depth_layers):
            is_vector = layer.type() == LAYER_VECTOR
            kind = "contours" if is_vector else "raster"
            item = QTableWidgetItem("%s (%s)" % (layer.name(), kind))
            item.setFlags((item.flags() | ITEM_FLAG_USER_CHECKABLE) & ~ITEM_FLAG_EDITABLE)
            item.setCheckState(CHECK_STATE_CHECKED if layer.id() in selected
                               else CHECK_STATE_UNCHECKED)
            item.setData(ITEM_DATA_USER_ROLE, layer.id())
            self.depth_table.setItem(row, 0, item)
            if is_vector:
                combo = QComboBox()
                for field in layer.fields():
                    if field.type() in (FIELD_TYPE_INT, FIELD_TYPE_DOUBLE, FIELD_TYPE_LONG_LONG):
                        combo.addItem(field.name())
                wanted = selected.get(layer.id(), "")
                if wanted:
                    idx = combo.findText(wanted)
                    if idx != -1:
                        combo.setCurrentIndex(idx)
                self.depth_table.setCellWidget(row, 1, combo)
            else:
                placeholder = QTableWidgetItem("—")
                placeholder.setFlags(placeholder.flags() & ~ITEM_FLAG_EDITABLE)
                self.depth_table.setItem(row, 1, placeholder)
        try:
            self.depth_table.horizontalHeader().setSectionResizeMode(0, HEADER_RESIZE_MODE_STRETCH)
        except Exception:
            pass
        self.depth_table.resizeColumnToContents(1)

    def _selected_depth_sources(self):
        """Return [(layer_id, field)] for the checked rows."""
        sources = []
        for row in range(self.depth_table.rowCount()):
            item = self.depth_table.item(row, 0)
            if item is None or item.checkState() != CHECK_STATE_CHECKED:
                continue
            layer_id = item.data(ITEM_DATA_USER_ROLE)
            if not layer_id:
                continue
            combo = self.depth_table.cellWidget(row, 1)
            field = combo.currentText().strip() if isinstance(combo, QComboBox) else ""
            sources.append((str(layer_id), field))
        return sources

    def update_metrics(self):
        layer_id = self.layer_combo.currentData()
        if not layer_id:
            self.metrics_text.setText("No layer selected.")
            return

        layer = QgsProject.instance().mapLayer(layer_id)
        if not layer:
            self.metrics_text.setText("Layer not found.")
            return

        num_features = layer.featureCount()
        if num_features == 0:
            self.metrics_text.setText("Layer has no features.")
            return

        project_crs = QgsProject.instance().crs()
        transform_context = QgsProject.instance().transformContext()
        layer_crs = layer.crs()
        transform = None
        if layer_crs != project_crs:
            transform = QgsCoordinateTransform(layer_crs, project_crs, QgsProject.instance())

        total_length_ell_m = 0.0
        total_length_planar_m = 0.0
        total_vertices = 0
        unmeasured = 0

        d_ell = make_distance_area(project_crs, transform_context, mode="ellipsoidal")
        # Grid length works on any project CRS (grid CRS from KP settings).
        planar_ok = True
        d_planar = make_kp_distance_area(project_crs, transform_context,
                                         mode=KP_MODE_CARTESIAN)

        for feature in layer.getFeatures():
            geom = QgsGeometry(feature.geometry())
            if geom and not geom.isEmpty():
                # A feature that cannot be transformed/measured is counted and
                # reported below, not silently left out of the lengths.
                try:
                    if transform is not None:
                        geom.transform(transform)
                    ell_m = float(d_ell.measureLength(geom))
                    planar_m = float(d_planar.measureLength(geom)) if planar_ok else 0.0
                except QgsCsException:
                    ell_m = planar_m = float("nan")
                if not (geometry_is_finite(geom) and math.isfinite(ell_m) and math.isfinite(planar_m)):
                    unmeasured += 1
                    continue
                total_length_ell_m += ell_m
                total_length_planar_m += planar_m

                if geom.isMultipart():
                    for part in geom.asMultiPolyline():
                        total_vertices += len(part)
                else:
                    total_vertices += len(geom.asPolyline())

        length_ell_km = total_length_ell_m / 1000.0
        if planar_ok:
            length_planar_km = total_length_planar_m / 1000.0
            planar_line = f"Length (cartesian): {length_planar_km:.3f} km"
        else:
            planar_line = "Length (cartesian): n/a (project CRS is geographic)"

        text = f"Length (ellipsoidal): {length_ell_km:.3f} km\n{planar_line}\nAC Count: {total_vertices}"
        if unmeasured:
            text += (f"\nWarning: {unmeasured} feature(s) could not be transformed to the "
                     "project CRS and are not included.")
        self.metrics_text.setText(text)

        # Cartesian (grid) works on any project CRS: a geographic project is
        # measured in the grid CRS (KP settings) or the route's UTM zone.

    def update_depth_ui(self):
        """Enable the depth layer table/profile option with the main checkbox."""
        enabled = self.depth_checkbox.isChecked()
        self.depth_layers_label.setEnabled(enabled)
        self.depth_table.setEnabled(enabled)
        self.profile_hint_label.setEnabled(enabled)

    def update_copy_ui(self):
        enabled = self.copy_latlon_checkbox.isChecked()
        self.copy_latlon_format_label.setEnabled(enabled)
        self.copy_latlon_format_combo.setEnabled(enabled)
        self.copy_latlon_style_label.setEnabled(enabled)
        self.copy_latlon_style_combo.setEnabled(enabled)

    def get_settings(self):
        layer_id = self.layer_combo.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        unit = self.unit_combo.currentText()
        show_reverse_kp = self.reverse_kp_checkbox.isChecked()
        use_cartesian = self.cartesian_checkbox.isChecked()
        show_depth = self.depth_checkbox.isChecked()
        depth_sources = self._selected_depth_sources()
        copy_include_rkp = self.copy_rkp_checkbox.isChecked()
        copy_include_dcc = self.copy_dcc_checkbox.isChecked()
        copy_include_latlon = self.copy_latlon_checkbox.isChecked()
        copy_latlon_format = self.copy_latlon_format_combo.currentData() or "DD"
        copy_latlon_style = self.copy_latlon_style_combo.currentData() or "LABELLED"
        return (
            layer,
            unit,
            show_reverse_kp,
            use_cartesian,
            show_depth,
            depth_sources,
            copy_include_rkp,
            copy_include_dcc,
            copy_include_latlon,
            copy_latlon_format,
            copy_latlon_style,
        )


class GoToKPDialog(QDialog):
    """Small dialog to enter a KP (km) and pan the map to that location."""

    def __init__(
        self,
        parent,
        min_kp_km: float,
        max_kp_km: float,
        initial_kp_km: Optional[float] = None,
    ):
        super().__init__(parent)
        self._min_kp_km = float(min_kp_km)
        self._max_kp_km = float(max_kp_km)

        self.setWindowTitle("Go to KP")
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)

        self.kp_label = QLabel("KP (km):")
        self.kp_input = QLineEdit(self)
        self.kp_input.setPlaceholderText("e.g. 12.345")
        if initial_kp_km is not None:
            try:
                self.kp_input.setText(f"{float(initial_kp_km):.3f}")
            except (TypeError, ValueError):  # no usable initial KP: leave blank
                pass

        kp_row = QHBoxLayout()
        kp_row.addWidget(self.kp_label)
        kp_row.addWidget(self.kp_input)
        layout.addLayout(kp_row)

        self.range_label = QLabel(
            f"Available KP range: {self._min_kp_km:.3f} to {self._max_kp_km:.3f} km"
        )
        layout.addWidget(self.range_label)

        self.buttons = QDialogButtonBox(BUTTON_BOX_CANCEL)
        self.go_button = QPushButton("Go to")
        self.buttons.addButton(self.go_button, BUTTON_BOX_ACCEPT_ROLE)
        self.buttons.rejected.connect(self.reject)
        self.go_button.clicked.connect(self._on_go)
        layout.addWidget(self.buttons)

        self._chosen_kp_km = None  # type: Optional[float]

        self.kp_input.returnPressed.connect(self.go_button.click)

        self.setFixedWidth(360)

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parentWidget()
        if parent is not None:
            parent_center = parent.frameGeometry().center()
            frame = self.frameGeometry()
            frame.moveCenter(parent_center)
            self.move(frame.topLeft())
        else:
            screen = QApplication.primaryScreen()
            if screen is not None:
                screen_center = screen.availableGeometry().center()
                frame = self.frameGeometry()
                frame.moveCenter(screen_center)
                self.move(frame.topLeft())

    def _on_go(self):
        raw = (self.kp_input.text() or "").strip()
        try:
            value = float(raw)
        except ValueError:
            QMessageBox.warning(self, "Go to KP", "Please enter a valid numeric KP value.")
            return

        if value < self._min_kp_km or value > self._max_kp_km:
            QMessageBox.warning(
                self,
                "Go to KP",
                f"KP must be between {self._min_kp_km:.3f} and {self._max_kp_km:.3f} km.",
            )
            return

        self._chosen_kp_km = float(value)
        self.accept()

    def chosen_kp_km(self) -> Optional[float]:
        return self._chosen_kp_km


class KPMouseTool:
    """
    This class wraps the map tool functionality with a toolbar button and menu item.
    It allows the user to toggle tracking on/off and configure settings.

    ``menu_name`` is the plugin menu to join (the plugin shell's ``self.menu``);
    by default the same translated title the shell uses.
    """
    def __init__(self, iface, menu_name=None):
        self.iface = iface
        self.menu_name = menu_name or plugin_menu_name()
        self.mapTool = None
        self.referenceLayer = None
        self.measurementUnit = "km"
        self.showReverseKP = False
        self.useCartesian = False
        self.showDepth = False
        # Depth sources as [(layer_id, field)]; resolved to live layers lazily.
        self.depthSourceRefs = []
        self.showDepthProfile = False
        # Right-click copy settings (KP is always included)
        self.copyIncludeRKP = False
        self.copyIncludeDCC = False
        self.copyIncludeLatLon = False
        self.copyLatLonFormat = "DD"
        self.copyLatLonStyle = "LABELLED"
        self.toolButton = None
        self.toolButtonAction = None
        self.toolButtonMenu = None
        self.actionConfig = None
        self.actionGoToKP = None
        self.load_settings()

    def _safe_layer_id(self, layer) -> Optional[str]:
        """Return a layer id if the layer wrapper is still valid, else None."""
        if layer is None or _sip_isdeleted(layer):
            return None
        try:
            return layer.id()
        except RuntimeError:  # wrapped C++ object deleted
            return None

    def _get_reference_layer(self) -> Optional[QgsVectorLayer]:
        """Return the current reference layer if it's still alive and in the project."""
        layer = self.referenceLayer
        layer_id = self._safe_layer_id(layer)
        if not layer_id:
            self.referenceLayer = None
            return None

        project_layer = QgsProject.instance().mapLayer(layer_id)
        if project_layer is None:
            self.referenceLayer = None
            return None

        # Keep our cached reference pointing at the live project instance.
        self.referenceLayer = project_layer
        if isinstance(project_layer, QgsVectorLayer):
            return project_layer
        return None

    def initGui(self):
        """Initialize the UI elements for the KP Mouse Tool."""
        self.toolButton = QToolButton(self.iface.mainWindow())
        # Dedicated icon, else the main plugin icon (both files in the plugin dir).
        icon_path = os.path.join(PLUGIN_DIR, 'kp_mouse_tool_icon.png')
        if not os.path.exists(icon_path):
            icon_path = os.path.join(PLUGIN_DIR, 'icon.png')
        self.toolButton.setIcon(QIcon(icon_path))
        self.toolButton.setCheckable(True)
        self.toolButton.toggled.connect(self.toggle_tool)
        self.toolButton.setToolTip("Enable/Disable KP Mouse Tool")
        self.toolButton.setPopupMode(TOOLBUTTON_POPUP_MODE_MENU_BUTTON)

        self.toolButtonMenu = QMenu(self.toolButton)
        self.actionConfig = QAction("Configure...", self.iface.mainWindow())
        self.actionConfig.triggered.connect(self.show_config_dialog)
        self.toolButtonMenu.addAction(self.actionConfig)

        self.actionGoToKP = QAction("Go to KP...", self.iface.mainWindow())
        self.actionGoToKP.triggered.connect(self.show_go_to_kp_dialog)
        self.toolButtonMenu.addAction(self.actionGoToKP)

        self.toolButton.setMenu(self.toolButtonMenu)

        self.toolButtonAction = self.iface.addToolBarWidget(self.toolButton)
        self._update_go_to_kp_enabled()

        QgsProject.instance().layersRemoved.connect(self._on_layers_removed)

        self.iface.addPluginToMenu(self.menu_name, self.actionConfig)

    def unload(self):
        """Remove UI elements, disconnect signals, and clean up resources when the plugin is unloaded.

        Everything initGui created is deleted — the toolbar widget action,
        the tool button (with its menu) and both actions, which are parented
        to the main window and would otherwise pile up on every plugin
        reload. Safe to call twice.
        """
        iface = self.iface
        if self.mapTool is not None:
            if iface is not None and iface.mapCanvas().mapTool() == self.mapTool:
                iface.mapCanvas().unsetMapTool(self.mapTool)
            self.mapTool.cleanup_resources()
            self.mapTool = None

        try:
            QgsProject.instance().layersRemoved.disconnect(self._on_layers_removed)
        except TypeError:  # initGui never ran / already unloaded
            pass

        button = self.toolButton
        if button is not None and not _sip_isdeleted(button):
            try:
                button.toggled.disconnect(self.toggle_tool)
            except TypeError:
                pass
        if self.toolButtonAction is not None and not _sip_isdeleted(self.toolButtonAction):
            if iface is not None:
                iface.removeToolBarIcon(self.toolButtonAction)
            # Deleting the toolbar's widget action also deletes the button.
            self.toolButtonAction.deleteLater()
        if button is not None and not _sip_isdeleted(button):
            button.deleteLater()
        self.toolButtonAction = None
        self.toolButton = None
        self.toolButtonMenu = None

        if self.actionConfig is not None and not _sip_isdeleted(self.actionConfig):
            if iface is not None:
                iface.removePluginMenu(self.menu_name, self.actionConfig)
            self.actionConfig.deleteLater()
        self.actionConfig = None
        if self.actionGoToKP is not None and not _sip_isdeleted(self.actionGoToKP):
            self.actionGoToKP.deleteLater()
        self.actionGoToKP = None

        # Clean up references
        self.referenceLayer = None
        self.measurementUnit = None
        self.showReverseKP = None
        self.iface = None

    def toggle_tool(self, checked):
        """Handle the toggling of the map tool."""
        if checked:
            layer = self._get_reference_layer()
            if not layer:
                self.iface.messageBar().pushMessage(
                    "Info", "KP Mouse Tool: Please configure a reference layer.", level=MESSAGE_INFO
                )
                self.show_config_dialog()
                layer = self._get_reference_layer()
                if not layer:
                    self.toolButton.setChecked(False)
                    return

            # Verify a feature exists: fetch one, not the whole layer.
            if not _layer_has_features(layer):
                QMessageBox.information(
                    self.iface.mainWindow(), "KP Mouse Tool", "No features found in the reference layer!"
                )
                self.toolButton.setChecked(False)
                return

            # The profile toggle is flipped inside the map tool (menu / D key)
            # and persisted; re-read it so a rebuilt tool keeps the user's state.
            self.showDepthProfile = QSettings("SubseaCableTools", "KPMouseTool").value(
                "showDepthProfile", self.showDepthProfile, type=bool)
            # Pick up a change made in KP settings since the tool was set up.
            self.useCartesian = kp_distance_mode() == KP_MODE_CARTESIAN
            self.mapTool = KPMouseMapTool(
                self.iface.mapCanvas(),
                layer,
                self.iface,
                self.measurementUnit,
                self.showReverseKP,
                self.useCartesian,
                self.showDepth,
                self._resolve_depth_sources(),
                self.showDepthProfile,
                self.copyIncludeRKP,
                self.copyIncludeDCC,
                self.copyIncludeLatLon,
                self.copyLatLonFormat,
                self.copyLatLonStyle,
            )
            self.iface.mapCanvas().setMapTool(self.mapTool)
        else:
            # Deactivating the tool
            if self.mapTool:
                if self.iface.mapCanvas().mapTool() == self.mapTool:
                    self.iface.mapCanvas().unsetMapTool(self.mapTool)
                # Clean up the map tool resources
                self.mapTool.cleanup_resources()
                self.mapTool = None

            self._update_go_to_kp_enabled()

    def _resolve_depth_sources(self):
        """Resolve stored (layer_id, field) refs to live [(layer, field)] pairs."""
        sources = []
        for layer_id, field in (self.depthSourceRefs or []):
            layer = QgsProject.instance().mapLayer(layer_id)
            if layer is not None:
                sources.append((layer, field))
        return sources

    def show_config_dialog(self):
        """Show the configuration dialog."""
        dialog = KPConfigDialog(
            self.iface.mainWindow(),
            self._get_reference_layer(),
            self.measurementUnit,
            self.showReverseKP,
            self.useCartesian,
            self.showDepth,
            self.depthSourceRefs,
            self.copyIncludeRKP,
            self.copyIncludeDCC,
            self.copyIncludeLatLon,
            self.copyLatLonFormat,
            self.copyLatLonStyle,
        )
        if qt_exec(dialog):
            (
                layer,
                unit,
                show_reverse_kp,
                use_cartesian,
                show_depth,
                depth_sources,
                copy_include_rkp,
                copy_include_dcc,
                copy_include_latlon,
                copy_latlon_format,
                copy_latlon_style,
            ) = dialog.get_settings()
            if layer:
                self.referenceLayer = layer
                self.measurementUnit = unit
                self.showReverseKP = show_reverse_kp
                self.useCartesian = use_cartesian
                # The checkbox is the plugin-wide KP setting.
                set_kp_distance_settings(
                    KP_MODE_CARTESIAN if use_cartesian else KP_MODE_GEODESIC,
                    kp_grid_crs_setting())
                self.showDepth = show_depth
                self.depthSourceRefs = list(depth_sources or [])
                self.copyIncludeRKP = bool(copy_include_rkp)
                self.copyIncludeDCC = bool(copy_include_dcc)
                self.copyIncludeLatLon = bool(copy_include_latlon)
                self.copyLatLonFormat = str(copy_latlon_format or "DD").upper()
                self.copyLatLonStyle = str(copy_latlon_style or "LABELLED").upper()
                self.save_settings()
                self.iface.messageBar().pushMessage(
                    "Success", f"KP Mouse Tool configured with layer '{layer.name()}'", level=MESSAGE_SUCCESS
                )
                if self.toolButton.isChecked():
                    self.toggle_tool(True)  # Re-enable with new settings
                self._update_go_to_kp_enabled()
            else:
                self.iface.messageBar().pushMessage(
                    "Warning", "No valid reference layer selected.", level=MESSAGE_WARNING
                )
                self._update_go_to_kp_enabled()

    def _on_layers_removed(self, layer_ids):
        if not layer_ids:
            return
        layer_id = self._safe_layer_id(self.referenceLayer)
        if not layer_id or layer_id in set(layer_ids):
            # Removed, or the wrapper is already deleted.
            self.referenceLayer = None
        self._update_go_to_kp_enabled()

    def _reference_layer_ready(self) -> bool:
        layer = self._get_reference_layer()
        if layer is None:
            return False
        try:
            if not layer.isValid() or layer.geometryType() != GEOMETRY_LINE:
                return False
            count = layer.featureCount()
        except RuntimeError:
            # wrapped C/C++ object deleted
            self.referenceLayer = None
            return False
        if count < 0:  # provider cannot count cheaply: look for one feature
            return _layer_has_features(layer)
        return count > 0

    def _update_go_to_kp_enabled(self):
        if self.actionGoToKP is None or _sip_isdeleted(self.actionGoToKP):
            return
        self.actionGoToKP.setEnabled(self._reference_layer_ready())

    def _reference_route(self) -> Optional[RouteFrame]:
        """The reference line's KP route in the map CRS — built exactly as
        the map tool builds it, so Go to KP and the tooltip agree. ``None``
        (reported) when the line cannot be transformed to the map CRS."""
        layer = self._get_reference_layer()
        if layer is None:
            return None
        try:
            route, _distance, _geoms = build_reference_route(
                layer, self.iface.mapCanvas().mapSettings().destinationCrs(), self.useCartesian)
        except (QgsCsException, ValueError):
            log_exception("KP Mouse Tool: reference line could not be transformed to the map CRS")
            return None
        return route

    def _reference_kp_range(self) -> Optional[Tuple[float, float]]:
        if not self._reference_layer_ready():
            return None
        route = self._reference_route()
        if route is None:
            return None
        return (0.0, max(0.0, route.total_length_km))

    def _point_at_kp_km(self, kp_km: float) -> Optional[QgsPointXY]:
        """Return the point on the reference line at the provided KP (km)."""
        if not self._reference_layer_ready() or float(kp_km) < 0:
            return None
        route = self._reference_route()
        return route.point_at_kp(float(kp_km), clamp=True) if route is not None else None

    def show_go_to_kp_dialog(self):
        if not self._reference_layer_ready():
            self.iface.messageBar().pushMessage(
                "Info",
                "Go to KP is available after configuring a reference line layer.",
                level=MESSAGE_INFO,
                duration=3,
            )
            self._update_go_to_kp_enabled()
            return

        kp_range = self._reference_kp_range()
        if kp_range is None:
            self.iface.messageBar().pushMessage(
                "Warning",
                "Reference layer is not ready. Please reconfigure.",
                level=MESSAGE_WARNING,
                duration=3,
            )
            self._update_go_to_kp_enabled()
            return

        min_kp, max_kp = kp_range
        initial = None
        if self.mapTool is not None and getattr(self.mapTool, "last_chainage", None) is not None:
            initial = float(self.mapTool.last_chainage)

        dialog = GoToKPDialog(self.iface.mainWindow(), min_kp, max_kp, initial_kp_km=initial)
        if qt_exec(dialog):
            kp_km = dialog.chosen_kp_km()
            if kp_km is None:
                return

            point = self._point_at_kp_km(float(kp_km))
            if point is None:
                QMessageBox.warning(
                    self.iface.mainWindow(),
                    "Go to KP",
                    "Could not compute a point at that KP on the configured reference line.",
                )
                return

            canvas = self.iface.mapCanvas()
            canvas.setCenter(point)
            canvas.refresh()

    def save_settings(self):
        """Save settings to QSettings."""
        settings = QSettings("SubseaCableTools", "KPMouseTool")
        ref_id = self._safe_layer_id(self.referenceLayer)
        if ref_id:
            settings.setValue("referenceLayerId", ref_id)
        else:
            settings.remove("referenceLayerId")
        settings.setValue("measurementUnit", self.measurementUnit)
        settings.setValue("showReverseKP", self.showReverseKP)
        settings.setValue("useCartesian", self.useCartesian)
        settings.setValue("showDepth", self.showDepth)
        settings.setValue("copyIncludeRKP", self.copyIncludeRKP)
        settings.setValue("copyIncludeDCC", self.copyIncludeDCC)
        settings.setValue("copyIncludeLatLon", self.copyIncludeLatLon)
        settings.setValue("copyLatLonFormat", self.copyLatLonFormat)
        settings.setValue("copyLatLonStyle", self.copyLatLonStyle)
        # showDepthProfile is written by the map tool's toggle (menu / D key),
        # not here, so a stale copy can never clobber the user's choice.
        import json
        settings.setValue("depthLayersJson", json.dumps([
            {"id": layer_id, "field": field or ""}
            for layer_id, field in (self.depthSourceRefs or [])]))
        # Legacy single-layer keys superseded by depthLayersJson.
        settings.remove("depthLayerId")
        settings.remove("depthField")

    def load_settings(self):
        """Load settings from QSettings."""
        settings = QSettings("SubseaCableTools", "KPMouseTool")
        layer_id = settings.value("referenceLayerId")
        if layer_id:
            self.referenceLayer = QgsProject.instance().mapLayer(layer_id)
        self.measurementUnit = settings.value("measurementUnit", "km")
        self.showReverseKP = settings.value("showReverseKP", False, type=bool)
        self.useCartesian = kp_distance_mode() == KP_MODE_CARTESIAN
        self.showDepth = settings.value("showDepth", False, type=bool)
        # New clipboard settings (default: only KP)
        self.copyIncludeRKP = settings.value("copyIncludeRKP", False, type=bool)
        self.copyIncludeDCC = settings.value("copyIncludeDCC", False, type=bool)
        self.copyIncludeLatLon = settings.value("copyIncludeLatLon", False, type=bool)
        self.copyLatLonFormat = str(settings.value("copyLatLonFormat", "DD") or "DD").upper()
        self.copyLatLonStyle = str(settings.value("copyLatLonStyle", "LABELLED") or "LABELLED").upper()
        self.showDepthProfile = settings.value("showDepthProfile", False, type=bool)
        self.depthSourceRefs = []
        raw_sources = settings.value("depthLayersJson", "")
        if raw_sources:
            import json
            try:
                for entry in json.loads(str(raw_sources)):
                    if isinstance(entry, dict) and entry.get("id"):
                        self.depthSourceRefs.append(
                            (str(entry["id"]), str(entry.get("field") or "")))
            except (TypeError, ValueError):
                self.depthSourceRefs = []
        else:
            # Migrate the pre-multi-layer settings.
            depth_layer_id = settings.value("depthLayerId")
            if depth_layer_id:
                self.depthSourceRefs = [(str(depth_layer_id),
                                         str(settings.value("depthField", "") or ""))]
