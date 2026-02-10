# kp_mouse_maptool.py
# -*- coding: utf-8 -*-
"""
KPMouseMapTool
Integrated into the Subsea Cable Tools plugin.
This tool displays the closest point on a selected line to the mouse pointer,
draws a dashed line connecting them, and shows distance and KP (chainage) data.
Right-clicking can copy KP info to the clipboard (configurable in tool settings).
"""

from typing import Optional, Tuple

from qgis.PyQt.QtCore import Qt, QSettings, QTimer, QVariant
from qgis.PyQt.QtGui import QIcon, QColor, QCursor
from qgis.PyQt.QtWidgets import (QAction, QMessageBox, QToolTip,
                                 QApplication, QDialog, QVBoxLayout,
                                 QComboBox, QLabel, QDialogButtonBox,
                                 QToolButton, QMenu, QCheckBox, QLineEdit, QHBoxLayout, QPushButton)
from qgis.core import (QgsWkbTypes, QgsGeometry, QgsProject, QgsDistanceArea,
                       QgsPointXY, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       Qgis, QgsVectorLayer, QgsField, QgsFeature, QgsRaster, QgsSpatialIndex)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker
import math
try:  # sip is available in QGIS Python env; guard for static analysis
    import sip  # type: ignore
    _sip_isdeleted = sip.isdeleted
except Exception:  # pragma: no cover
    sip = None
    def _sip_isdeleted(obj):
        return False


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
        depthLayer=None,
        depthField="",
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
        self.depthLayer = depthLayer
        self.depthField = depthField

        # Right-click copy behaviour (KP is always included)
        self.copyIncludeRKP = bool(copyIncludeRKP)
        self.copyIncludeDCC = bool(copyIncludeDCC)
        self.copyIncludeLatLon = bool(copyIncludeLatLon)
        self.copyLatLonFormat = (copyLatLonFormat or "DD").upper()
        self.copyLatLonStyle = (copyLatLonStyle or "LABELLED").upper()

        # Distance / chainage preparation
        self.distanceArea = QgsDistanceArea()
        project_crs = self.canvas.mapSettings().destinationCrs()
        self.distanceArea.setSourceCrs(project_crs, QgsProject.instance().transformContext())
        # Guard: planar/cartesian measurements in a geographic CRS would return degrees.
        # We disable cartesian here to prevent silently wrong results (the config dialog
        # also disables the option when the project CRS is geographic).
        if self.useCartesian and project_crs.isGeographic():
            self.useCartesian = False
            try:
                self.iface.messageBar().pushMessage(
                    "KP Mouse Tool",
                    "Cartesian distance requires a projected project CRS; falling back to ellipsoidal.",
                    level=Qgis.Warning,
                    duration=5,
                )
            except Exception:
                pass
        if self.useCartesian:
            if hasattr(self.distanceArea, "setEllipsoidalMode"):
                self.distanceArea.setEllipsoidalMode(False)
        else:
            ellipsoid = QgsProject.instance().ellipsoid() or 'WGS84'
            self.distanceArea.setEllipsoid(ellipsoid)
            if hasattr(self.distanceArea, "setEllipsoidalMode"):
                self.distanceArea.setEllipsoidalMode(True)

        # Cache line geometries
        self.features_geoms = []
        self.segment_lengths = []
        total_length = 0.0
        if self.layer is not None:
            layer_crs = self.layer.crs()
            transform = None
            if layer_crs != project_crs:
                transform = QgsCoordinateTransform(layer_crs, project_crs, QgsProject.instance())
            for f in self.layer.getFeatures():
                g = QgsGeometry(f.geometry())
                if transform:
                    g.transform(transform)
                self.features_geoms.append(g)
                seg_len = self.distanceArea.measureLength(g)
                self.segment_lengths.append(seg_len)
                total_length += seg_len
        self.total_length_meters = total_length

        # Visual helpers
        self.rubberBand = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.rubberBand.setColor(QColor(255, 0, 0))
        self.rubberBand.setWidth(2)
        self.rubberBand.setLineStyle(Qt.DashLine)

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
        self.canvas.setCursor(Qt.CrossCursor)

        # Range/Bearing resources
        self.range_bearing_origin = None
        self.rangeBearingLine = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.rangeBearingLine.setColor(QColor(0, 170, 255))
        self.rangeBearingLine.setWidth(2)
        self.rangeBearingCircle = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        self.rangeBearingCircle.setColor(QColor(0, 170, 255, 60))
        self.rangeBearingCircle.setFillColor(QColor(0, 170, 255, 40))
        self.rangeBearingCircle.setWidth(1)
        self.rangeBearingOriginMarker = QgsVertexMarker(self.canvas)
        self.rangeBearingOriginMarker.setColor(QColor(0, 170, 255))
        self.rangeBearingOriginMarker.setIconType(QgsVertexMarker.ICON_BOX)
        self.rangeBearingOriginMarker.setIconSize(10)
        self.rangeBearingOriginMarker.setPenWidth(2)
        self.rangeBearingOriginMarker.hide()

    def set_layer(self, layer):
        """Set the layer and recalculate geometries for the tool."""
        self.layer = layer
        self.recalculate_geometries()

    def recalculate_geometries(self):
        """Recalculate the cached geometries and segment lengths for the current layer."""
        if not self.layer:
            self.features_geoms = []
            self.segment_lengths = []
            self.total_length_meters = 0
            return

        # Set up distance measurements (ellipsoidal or planar).
        self.distanceArea = QgsDistanceArea()
        project_crs = self.canvas.mapSettings().destinationCrs()
        self.distanceArea.setSourceCrs(project_crs, QgsProject.instance().transformContext())
        if self.useCartesian:
            # Planar/cartesian in project CRS (units depend on CRS).
            if hasattr(self.distanceArea, "setEllipsoidalMode"):
                self.distanceArea.setEllipsoidalMode(False)
        else:
            ellipsoid = QgsProject.instance().ellipsoid() or "WGS84"
            self.distanceArea.setEllipsoid(ellipsoid)
            if hasattr(self.distanceArea, "setEllipsoidalMode"):
                self.distanceArea.setEllipsoidalMode(True)

        # Cache geometries and their lengths in project CRS
        self.features_geoms = []
        self.segment_lengths = []
        layer_crs = self.layer.crs()
        project_crs = self.canvas.mapSettings().destinationCrs()
        transform = None
        if layer_crs != project_crs:
            transform = QgsCoordinateTransform(layer_crs, project_crs, QgsProject.instance())

        total_length = 0
        for feature in self.layer.getFeatures():
            geom = QgsGeometry(feature.geometry())
            if transform:
                geom.transform(transform)
            
            self.features_geoms.append(geom)
            segment_length = self.distanceArea.measureLength(geom)
            self.segment_lengths.append(segment_length)
            total_length += segment_length
        
        self.total_length_meters = total_length

    def activate(self):
        super().activate()
        self.last_message = ""
        self.last_global_pos = None

    def canvasMoveEvent(self, event):
        # Stop any timers and hide tooltips when the mouse moves.
        self.persistent_tooltip_timer.stop()
        QToolTip.hideText()

        # Start the timer to detect when the mouse stops.
        self.mouse_stop_timer.start(500)

        # Convert the mouse event position to map coordinates.
        mousePoint = self.toMapCoordinates(event.pos())
        mouse_geom = QgsGeometry.fromPointXY(mousePoint)

        if not self.features_geoms:
            return

        min_dist = float('inf')
        closest_point_on_line = None
        closest_feature_idx = -1

        # Find the closest feature and point on that feature
        for i, feature_geom in enumerate(self.features_geoms):
            if feature_geom.isEmpty():
                continue

            # Use a robust method to find the closest point.
            # nearestPoint is used for broader QGIS version compatibility.
            closest_pt_geom = feature_geom.nearestPoint(mouse_geom)
            if closest_pt_geom.isEmpty():
                continue
            
            closest_pt = closest_pt_geom.asPoint()
            dist = self.distanceArea.measureLine(mousePoint, closest_pt)

            if dist < min_dist:
                min_dist = dist
                closest_point_on_line = closest_pt
                closest_feature_idx = i

        if closest_point_on_line is None:
            return

        # Update the rubber band and marker.
        self.rubberBand.reset(QgsWkbTypes.LineGeometry)
        self.rubberBand.addPoint(mousePoint)
        self.rubberBand.addPoint(closest_point_on_line)
        self.closestPointMarker.setCenter(closest_point_on_line)
        self.last_closest_point = QgsPointXY(closest_point_on_line)

        # Calculate distance between the mouse and the closest point.
        distance_meters = min_dist
        if self.measurementUnit == "m":
            converted_distance = distance_meters
        elif self.measurementUnit == "km":
            converted_distance = distance_meters / 1000.0
        elif self.measurementUnit == "nautical miles":
            converted_distance = distance_meters / 1852.0
        elif self.measurementUnit == "miles":
            converted_distance = distance_meters / 1609.34
        else:
            converted_distance = distance_meters

        # Calculate KP (chainage) along the line.
        chainage_meters = 0
        # Sum lengths of previous features (if the line is split into multiple features)
        if closest_feature_idx > 0:
            chainage_meters = sum(self.segment_lengths[:closest_feature_idx])

        # Add distance along the current feature by finding the nearest segment
        closest_feature_geom = self.features_geoms[closest_feature_idx]
        
        # Handle multi-part geometries by getting all points
        all_parts = []
        if closest_feature_geom.isMultipart():
            all_parts = closest_feature_geom.asMultiPolyline()
        else:
            all_parts = [closest_feature_geom.asPolyline()]

        min_dist_to_segment = float('inf')
        dist_along_feature = 0
        
        temp_dist_along_feature = 0
        final_dist_along_segment = 0

        for part in all_parts:
            part_dist_along = 0
            for i in range(len(part) - 1):
                p1 = QgsPointXY(part[i])
                p2 = QgsPointXY(part[i+1])
                
                segment_geom = QgsGeometry.fromPolylineXY([p1, p2])
                
                # Use distance() which is more reliable than nearestPoint() for this check
                dist_to_mouse = segment_geom.distance(mouse_geom)

                if dist_to_mouse < min_dist_to_segment:
                    min_dist_to_segment = dist_to_mouse
                    
                    # Project the closest point on the line onto the current segment
                    # to find the distance along it.
                    projected_point = segment_geom.interpolate(segment_geom.lineLocatePoint(QgsGeometry.fromPointXY(closest_point_on_line)))
                    dist_along_segment = self.distanceArea.measureLine(p1, projected_point.asPoint())
                    
                    # This is the cumulative length of segments before the closest one in this feature
                    dist_along_feature = temp_dist_along_feature + dist_along_segment

                temp_dist_along_feature += self.distanceArea.measureLine(p1, p2)

        chainage_meters += dist_along_feature
        chainage_km = chainage_meters / 1000.0

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
        self.last_global_pos = event.globalPos()

        # Append range & bearing info if origin set
        if self.range_bearing_origin is not None:
            range_distance_m, bearing_deg = self._compute_range_bearing(self.range_bearing_origin, mousePoint)
            # Convert distance to display unit
            display_range = self._convert_distance(range_distance_m)
            bearing_text = f"{bearing_deg:06.2f}° {self._bearing_to_compass(bearing_deg)}"
            self.last_message += f"\nRange: {display_range:.2f} {self.measurementUnit}\nBearing: {bearing_text}"
            # Update graphical overlays
            self._update_range_bearing_graphics(mousePoint, range_distance_m)
        else:
            # Clear any existing range/bearing graphics if user cleared origin
            self._clear_range_bearing_graphics()

        # Show the standard, transient tooltip immediately (with augmented message if any)
        QToolTip.showText(self.last_global_pos, self.last_message, self.canvas)

    def start_persistent_tooltip(self):
        """Called when the mouse stop timer fires. Starts the persistent tooltip timer."""
        if self.last_message and self.last_global_pos:
            self.persistent_tooltip_timer.start(100)

    def show_persistent_tooltip(self):
        """Called by the repeating timer to keep the tooltip visible."""
        if self.iface.mainWindow().isActiveWindow() and self.canvas.underMouse() and self.last_message and self.last_global_pos:
            QToolTip.showText(self.last_global_pos, self.last_message, self.canvas)

    def canvasLeaveEvent(self, event):
        """Stop timers and hide tooltip when mouse leaves the canvas."""
        self.persistent_tooltip_timer.stop()
        self.mouse_stop_timer.stop()
        QToolTip.hideText()

    def canvasPressEvent(self, event):
        # Left click toggles range/bearing measurement: start -> stop -> start ...
        if event.button() == Qt.LeftButton:
            if self.range_bearing_origin is None:
                # Start measurement
                map_pt = self.toMapCoordinates(event.pos())
                self.range_bearing_origin = map_pt
                self.rangeBearingOriginMarker.setCenter(map_pt)
                self.rangeBearingOriginMarker.show()
                self._clear_range_bearing_graphics()  # will update on move
                self.iface.mainWindow().statusBar().showMessage(
                    "Range/Bearing active. Move mouse; click again to clear.", 3000)
            else:
                # Stop measurement
                self.range_bearing_origin = None
                self.rangeBearingOriginMarker.hide()
                self._clear_range_bearing_graphics()
                self.iface.mainWindow().statusBar().showMessage(
                    "Range/Bearing cleared. Click to start again.", 3000)
            return

        # Right click now opens context menu with placement options
        if event.button() == Qt.RightButton:
            click_point = self.toMapCoordinates(event.pos())
            menu = QMenu(self.canvas)

            act_place = menu.addAction("Place Point")
            act_place_kp = menu.addAction("Place Point at Nearest KP")
            # Add range ring placement option if range/bearing is active
            if self.range_bearing_origin is not None and self.last_mouse_point is not None:
                act_place_ring = menu.addAction("Place Range Ring")
            else:
                act_place_ring = None
            # Add depth sampling option if depth layer is configured
            if self.showDepth and self.depthLayer is not None:
                act_sample_depth = menu.addAction("Sample Depth at Point")
            else:
                act_sample_depth = None
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

            chosen = menu.exec_(QCursor.pos())
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
                    level=Qgis.Info,
                    duration=3,
                )
                return

            min_kp = 0.0
            max_kp = float(self.total_length_meters) / 1000.0
            initial = None
            if self.last_chainage is not None:
                try:
                    initial = float(self.last_chainage)
                except Exception:
                    initial = None

            dialog = GoToKPDialog(self.iface.mainWindow(), min_kp, max_kp, initial_kp_km=initial)
            if not dialog.exec_():
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
        except Exception as e:
            try:
                self.iface.messageBar().pushMessage("Error", f"Go to KP failed: {e}", level=Qgis.Critical, duration=4)
            except Exception:
                pass

    def _point_at_kp_km(self, kp_km: float) -> Optional[QgsPointXY]:
        """Return the point on the cached reference line at the provided KP (km)."""
        try:
            target_m = float(kp_km) * 1000.0
        except Exception:
            return None

        if target_m < 0:
            return None

        cumulative = 0.0
        last_point = None

        for geom in self.features_geoms:
            if geom is None or geom.isEmpty():
                continue

            try:
                if geom.isMultipart():
                    parts = list(geom.asMultiPolyline())
                else:
                    parts = [geom.asPolyline()]
            except Exception:
                continue

            for part in parts:
                if not part or len(part) < 2:
                    continue

                for i in range(len(part) - 1):
                    p1 = part[i]
                    p2 = part[i + 1]
                    try:
                        seg_len = float(self.distanceArea.measureLine(p1, p2))
                    except Exception:
                        continue
                    if seg_len <= 0:
                        continue

                    next_cum = cumulative + seg_len
                    last_point = QgsPointXY(p2)

                    if next_cum >= target_m:
                        ratio = (target_m - cumulative) / seg_len
                        x = float(p1.x()) + ratio * (float(p2.x()) - float(p1.x()))
                        y = float(p1.y()) + ratio * (float(p2.y()) - float(p1.y()))
                        return QgsPointXY(x, y)

                    cumulative = next_cum

        return last_point

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
                source_crs = self.canvas.mapSettings().destinationCrs()
                dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
                transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())
                wgs84_point = transform.transform(self.last_mouse_point)
                lat = float(wgs84_point.y())
                lon = float(wgs84_point.x())
                parts.append(self._format_lat_lon(lat, lon, self.copyLatLonFormat, self.copyLatLonStyle))

            clipboard_text = "\n".join(parts)
            QApplication.clipboard().setText(clipboard_text)

            extras = self.copyIncludeRKP or self.copyIncludeDCC or self.copyIncludeLatLon
            feedback = "KP info copied to clipboard" if extras else "KP copied to clipboard"
            QToolTip.showText(QCursor.pos(), feedback, self.canvas)
            self.iface.mainWindow().statusBar().showMessage(feedback, 2000)
            self.iface.messageBar().pushMessage("Info", feedback, level=Qgis.Info, duration=2)
        except Exception as e:
            self.iface.messageBar().pushMessage("Error", f"Copy failed: {e}", level=Qgis.Critical, duration=4)

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
            if lyr.name() == layer_name and lyr.type() == lyr.VectorLayer and lyr.geometryType() == QgsWkbTypes.PointGeometry:
                provider = lyr.dataProvider()
                existing = {f.name().lower() for f in lyr.fields()}
                new_fields = []
                if 'ref_line' not in existing:
                    new_fields.append(QgsField("ref_line", QVariant.String))
                if 'comment' not in existing:
                    new_fields.append(QgsField("comment", QVariant.String))
                if new_fields:
                    provider.addAttributes(new_fields)
                    lyr.updateFields()
                return lyr
        crs = self.canvas.mapSettings().destinationCrs()
        layer = QgsVectorLayer(f"Point?crs={crs.authid()}", layer_name, "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("name", QVariant.String),
            QgsField("kp", QVariant.Double),
            QgsField("rkp", QVariant.Double),
            QgsField("dcc", QVariant.Double),
            QgsField("lat", QVariant.Double),
            QgsField("lon", QVariant.Double),
            QgsField("ref_line", QVariant.String),
            QgsField("comment", QVariant.String),
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

            source_crs = self.canvas.mapSettings().destinationCrs()
            dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            if source_crs != dest_crs:
                transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())
                ll_point = transform.transform(point_xy)
            else:
                ll_point = point_xy

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
            if dlg.exec_() != QDialog.Accepted:
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
            msg = "Point placed at nearest KP" if snapped_to_kp else "Point placed"
            self.iface.mainWindow().statusBar().showMessage(msg, 3000)
            self.iface.messageBar().pushMessage("Success", msg, level=Qgis.Success, duration=2)
        except Exception as e:
            self.iface.messageBar().pushMessage("Error", f"Failed to place point: {e}", level=Qgis.Critical, duration=4)

    def _ensure_lines_layer(self):
        """Ensure there is a memory line layer to receive placed lines.

        Fields: name (string), range (double), bearing (double), range_unit (string), 
                origin_lat (double), origin_lon (double), target_lat (double), target_lon (double), 
                ref_line (string), comment (string)
        """
        project = QgsProject.instance()
        layer_name = "KP Range Lines"
        for lyr in project.mapLayers().values():
            if lyr.name() == layer_name and lyr.type() == lyr.VectorLayer and lyr.geometryType() == QgsWkbTypes.LineGeometry:
                provider = lyr.dataProvider()
                existing = {f.name().lower() for f in lyr.fields()}
                new_fields = []
                if 'ref_line' not in existing:
                    new_fields.append(QgsField("ref_line", QVariant.String))
                if 'comment' not in existing:
                    new_fields.append(QgsField("comment", QVariant.String))
                if new_fields:
                    provider.addAttributes(new_fields)
                    lyr.updateFields()
                return lyr
        crs = self.canvas.mapSettings().destinationCrs()
        layer = QgsVectorLayer(f"LineString?crs={crs.authid()}", layer_name, "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("name", QVariant.String),
            QgsField("range", QVariant.Double),
            QgsField("bearing", QVariant.Double),
            QgsField("range_unit", QVariant.String),
            QgsField("origin_lat", QVariant.Double),
            QgsField("origin_lon", QVariant.Double),
            QgsField("target_lat", QVariant.Double),
            QgsField("target_lon", QVariant.Double),
            QgsField("ref_line", QVariant.String),
            QgsField("comment", QVariant.String),
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
            if lyr.name() == layer_name and lyr.type() == lyr.VectorLayer and lyr.geometryType() == QgsWkbTypes.PolygonGeometry:
                provider = lyr.dataProvider()
                existing = {f.name().lower() for f in lyr.fields()}
                new_fields = []
                if 'ref_line' not in existing:
                    new_fields.append(QgsField("ref_line", QVariant.String))
                if 'comment' not in existing:
                    new_fields.append(QgsField("comment", QVariant.String))
                if new_fields:
                    provider.addAttributes(new_fields)
                    lyr.updateFields()
                return lyr
        crs = self.canvas.mapSettings().destinationCrs()
        layer = QgsVectorLayer(f"Polygon?crs={crs.authid()}", layer_name, "memory")
        pr = layer.dataProvider()
        pr.addAttributes([
            QgsField("name", QVariant.String),
            QgsField("radius", QVariant.Double),
            QgsField("radius_unit", QVariant.String),
            QgsField("center_lat", QVariant.Double),
            QgsField("center_lon", QVariant.Double),
            QgsField("ref_line", QVariant.String),
            QgsField("comment", QVariant.String),
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
            source_crs = self.canvas.mapSettings().destinationCrs()
            dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())

            origin_ll = transform.transform(self.range_bearing_origin)
            target_ll = transform.transform(self.last_mouse_point)

            # Create line geometry
            line_geom = QgsGeometry.fromPolylineXY([self.range_bearing_origin, self.last_mouse_point])

            # Create circle geometry (same logic as rubber band)
            segments = 120
            project_crs = self.canvas.mapSettings().destinationCrs()
            if range_distance_m <= 0:
                return

            if project_crs.isGeographic():
                lat_rad = math.radians(self.range_bearing_origin.y())
                deg_per_m_lat = 1.0 / 111320.0
                deg_per_m_lon = 1.0 / (111320.0 * max(math.cos(lat_rad), 1e-6))
                radius_x = range_distance_m * deg_per_m_lon
                radius_y = range_distance_m * deg_per_m_lat
                pts = []
                for i in range(segments + 1):
                    ang = 2 * math.pi * i / segments
                    x = self.range_bearing_origin.x() + radius_x * math.sin(ang)
                    y = self.range_bearing_origin.y() + radius_y * math.cos(ang)
                    pts.append(QgsPointXY(x, y))
            else:
                radius = range_distance_m
                pts = []
                for i in range(segments + 1):
                    ang = 2 * math.pi * i / segments
                    x = self.range_bearing_origin.x() + radius * math.sin(ang)
                    y = self.range_bearing_origin.y() + radius * math.cos(ang)
                    pts.append(QgsPointXY(x, y))

            circle_geom = QgsGeometry.fromPolygonXY([pts])

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
            if dlg.exec_() != QDialog.Accepted:
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

            msg = f"Range ring placed: {display_range:.2f} {self.measurementUnit} at {bearing_text}"
            self.iface.mainWindow().statusBar().showMessage(msg, 3000)
            self.iface.messageBar().pushMessage("Success", msg, level=Qgis.Success, duration=3)

        except Exception as e:
            self.iface.messageBar().pushMessage("Error", f"Failed to place range ring: {e}", level=Qgis.Critical, duration=4)

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

    def _compute_range_bearing(self, origin: QgsPointXY, target: QgsPointXY):
        try:
            project_crs = self.canvas.mapSettings().destinationCrs()
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            if project_crs != wgs84:
                transform = QgsCoordinateTransform(project_crs, wgs84, QgsProject.instance())
                o_ll = transform.transform(origin)
                t_ll = transform.transform(target)
            else:
                o_ll = origin
                t_ll = target
            lat1 = math.radians(o_ll.y())
            lat2 = math.radians(t_ll.y())
            dlon = math.radians(t_ll.x() - o_ll.x())
            x = math.sin(dlon) * math.cos(lat2)
            y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
            bearing = math.degrees(math.atan2(x, y))
            bearing = (bearing + 360.0) % 360.0
            distance_m = self.distanceArea.measureLine(origin, target)
            return distance_m, bearing
        except Exception:
            dx = target.x() - origin.x()
            dy = target.y() - origin.y()
            distance_m = math.hypot(dx, dy)
            bearing = (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0
            return distance_m, bearing

    def _bearing_to_compass(self, bearing_deg: float) -> str:
        dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        idx = int((bearing_deg + 11.25) // 22.5) % 16
        return dirs[idx]

    def _update_range_bearing_graphics(self, mousePoint: QgsPointXY, range_meters: float):
        try:
            self.rangeBearingLine.reset(QgsWkbTypes.LineGeometry)
            self.rangeBearingLine.addPoint(self.range_bearing_origin)
            self.rangeBearingLine.addPoint(mousePoint)
        except Exception:
            pass
        try:
            self.rangeBearingCircle.reset(QgsWkbTypes.PolygonGeometry)
            if range_meters <= 0:
                return
            segments = 120
            project_crs = self.canvas.mapSettings().destinationCrs()
            if project_crs.isGeographic():
                lat_rad = math.radians(self.range_bearing_origin.y())
                deg_per_m_lat = 1.0 / 111320.0
                deg_per_m_lon = 1.0 / (111320.0 * max(math.cos(lat_rad), 1e-6))
                radius_x = range_meters * deg_per_m_lon
                radius_y = range_meters * deg_per_m_lat
                pts = []
                for i in range(segments + 1):
                    ang = 2 * math.pi * i / segments
                    x = self.range_bearing_origin.x() + radius_x * math.sin(ang)
                    y = self.range_bearing_origin.y() + radius_y * math.cos(ang)
                    pts.append(QgsPointXY(x, y))
            else:
                radius = range_meters
                pts = []
                for i in range(segments + 1):
                    ang = 2 * math.pi * i / segments
                    x = self.range_bearing_origin.x() + radius * math.sin(ang)
                    y = self.range_bearing_origin.y() + radius * math.cos(ang)
                    pts.append(QgsPointXY(x, y))
            for p in pts:
                self.rangeBearingCircle.addPoint(p)
        except Exception:
            pass

    def _clear_range_bearing_graphics(self):
        try:
            if self.rangeBearingLine:
                self.rangeBearingLine.reset(QgsWkbTypes.LineGeometry)
            if self.rangeBearingCircle:
                self.rangeBearingCircle.reset(QgsWkbTypes.PolygonGeometry)
        except Exception:
            pass

    def _sample_depth_at_point(self, point: QgsPointXY):
        """Sample depth at the given point from the configured depth layer.
        
        Returns depth value as float, or None if not available or outside extent.
        Supports both raster (MBES) and vector (contour) layers.
        """
        if not self.showDepth or not self.depthLayer:
            return None
        
        try:
            # Transform point to depth layer CRS if necessary
            project_crs = self.canvas.mapSettings().destinationCrs()
            depth_crs = self.depthLayer.crs()
            transformed_point = point
            if project_crs != depth_crs:
                transform = QgsCoordinateTransform(project_crs, depth_crs, QgsProject.instance())
                transformed_point = transform.transform(point)
            
            # Check if transformed point is within layer extent
            extent = self.depthLayer.extent()
            if not extent.contains(transformed_point):
                return None
            
            if self.depthLayer.type() == self.depthLayer.RasterLayer:
                # For raster layers, use data provider's identify method
                provider = self.depthLayer.dataProvider()
                if provider is None:
                    print(f"DEBUG: No data provider for raster layer")
                    return None
                
                # identify() returns a dictionary {band_number: value}
                identify_result = provider.identify(transformed_point, QgsRaster.IdentifyFormatValue)
                
                # Handle both dictionary and QgsRasterIdentifyResult formats
                if hasattr(identify_result, 'results'):
                    # Newer QGIS versions return QgsRasterIdentifyResult object
                    values = identify_result.results()
                else:
                    # Older versions return dictionary directly
                    values = identify_result
                
                if values:
                    # Get band 1 (first band) value
                    for band_num in sorted(values.keys()):
                        val = values[band_num]
                        if val is not None and not math.isnan(val):
                            return float(val)
            elif self.depthLayer.type() == self.depthLayer.VectorLayer and self.depthLayer.geometryType() == QgsWkbTypes.LineGeometry:
                # For contour layers, find nearest feature by iterating through all features
                # (since this is on-demand sampling, performance isn't critical)
                if not self.depthField:
                    return None
                
                # Find field index
                field_idx = self.depthLayer.fields().lookupField(self.depthField)
                if field_idx == -1:
                    return None
                
                # Find nearest feature by distance
                min_distance = float('inf')
                nearest_depth = None
                
                for feature in self.depthLayer.getFeatures():
                    geom = feature.geometry()
                    if geom and not geom.isEmpty():
                        # Calculate distance from point to geometry
                        distance = geom.distance(QgsGeometry.fromPointXY(transformed_point))
                        if distance < min_distance:
                            min_distance = distance
                            attr_value = feature.attribute(field_idx)
                            if attr_value is not None:
                                try:
                                    nearest_depth = float(attr_value)
                                except (ValueError, TypeError):
                                    pass
                
                if nearest_depth is not None:
                    return nearest_depth
        except Exception as e:
            # Reset spatial index on error to allow rebuild
            pass
        return None

    def _sample_and_display_depth(self, point: QgsPointXY):
        """Sample depth at given point and display result in a message."""
        if not self.showDepth or self.depthLayer is None:
            msg = "Depth sampling is not configured. Please configure a depth layer in the KP Mouse Tool settings."
            self.iface.mainWindow().statusBar().showMessage(msg, 4000)
            self.iface.messageBar().pushMessage("Info", msg, level=Qgis.Warning, duration=5)
            return
            
        try:
            depth = self._sample_depth_at_point(point)
            if depth is not None:
                msg = f"Depth at point: {depth:.2f} m"
                self.iface.mainWindow().statusBar().showMessage(msg, 4000)
                self.iface.messageBar().pushMessage("Depth", msg, level=Qgis.Info, duration=5)
                QToolTip.showText(QCursor.pos(), msg, self.canvas)
            else:
                msg = "No depth value available at this location (outside layer extent?)"
                self.iface.mainWindow().statusBar().showMessage(msg, 4000)
                self.iface.messageBar().pushMessage("Info", msg, level=Qgis.Warning, duration=5)
        except Exception as e:
            msg = f"Error sampling depth: {e}"
            self.iface.messageBar().pushMessage("Error", msg, level=Qgis.Critical, duration=4)

    def deactivate(self):
        self.mouse_stop_timer.stop()
        self.persistent_tooltip_timer.stop()
        QToolTip.hideText()
        if self.rubberBand:
            self.rubberBand.reset(QgsWkbTypes.LineGeometry)
        if self.closestPointMarker:
            self.closestPointMarker.hide()
        self._clear_range_bearing_graphics()
        if hasattr(self, 'rangeBearingOriginMarker') and self.rangeBearingOriginMarker:
            self.rangeBearingOriginMarker.hide()
        if self.iface and self.iface.mainWindow():
            self.iface.mainWindow().statusBar().clearMessage()
        QgsMapTool.deactivate(self)

    def cleanup_resources(self):
        for timer_attr in ('mouse_stop_timer', 'persistent_tooltip_timer'):
            timer = getattr(self, timer_attr, None)
            if timer:
                try:
                    timer.stop()
                except Exception:
                    pass
                try:
                    timer.timeout.disconnect()
                except Exception:
                    pass
                try:
                    timer.deleteLater()
                except Exception:
                    pass
                setattr(self, timer_attr, None)
        QToolTip.hideText()
        if hasattr(self, 'rubberBand') and self.rubberBand:
            try:
                if _sip_isdeleted(self.rubberBand):
                    pass
                else:
                    try:
                        self.rubberBand.hide()
                    except Exception:
                        pass
                    try:
                        self.rubberBand.reset(QgsWkbTypes.LineGeometry)
                    except Exception:
                        pass
                    try:
                        self.rubberBand.deleteLater()
                    except Exception:
                        pass
            finally:
                self.rubberBand = None
        if hasattr(self, 'closestPointMarker') and self.closestPointMarker:
            try:
                if _sip_isdeleted(self.closestPointMarker):
                    pass
                else:
                    try:
                        self.closestPointMarker.hide()
                    except Exception:
                        pass
                    try:
                        self.closestPointMarker.deleteLater()
                    except Exception:
                        pass
            finally:
                self.closestPointMarker = None
        self.features_geoms = []
        self.segment_lengths = []
        self.total_length_meters = 0
        try:
            if hasattr(self, 'rangeBearingLine') and self.rangeBearingLine:
                self.rangeBearingLine.hide()
                self.rangeBearingLine.reset(QgsWkbTypes.LineGeometry)
                self.rangeBearingLine.deleteLater()
        except Exception:
            pass
        self.rangeBearingLine = None
        try:
            if hasattr(self, 'rangeBearingCircle') and self.rangeBearingCircle:
                self.rangeBearingCircle.hide()
                self.rangeBearingCircle.reset(QgsWkbTypes.PolygonGeometry)
                self.rangeBearingCircle.deleteLater()
        except Exception:
            pass
        self.rangeBearingCircle = None
        try:
            if hasattr(self, 'rangeBearingOriginMarker') and self.rangeBearingOriginMarker:
                self.rangeBearingOriginMarker.hide()
                self.rangeBearingOriginMarker.deleteLater()
        except Exception:
            pass
        self.rangeBearingOriginMarker = None
        self.range_bearing_origin = None

    def keyPressEvent(self, event):  # type: ignore
        try:
            if event.key() == Qt.Key_Escape and self.range_bearing_origin is not None:
                self.range_bearing_origin = None
                self.rangeBearingOriginMarker.hide()
                self._clear_range_bearing_graphics()
                self.iface.mainWindow().statusBar().showMessage("Range/Bearing cleared (ESC).", 2000)
        except Exception:
            pass

    def __del__(self):
        pass

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

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
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

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
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
        depth_layer=None,
        depth_field="",
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
        self.cartesian_checkbox = QCheckBox("Use Cartesian distances (planar, in project CRS units)")
        self.cartesian_checkbox.setChecked(current_use_cartesian)
        layout.addWidget(self.cartesian_checkbox)

        # Depth options
        self.depth_checkbox = QCheckBox("Show depth sample option on right click")
        self.depth_checkbox.setChecked(show_depth)
        layout.addWidget(self.depth_checkbox)

        self.depth_layer_label = QLabel("Select Depth Layer (Raster or Contour):")
        self.depth_layer_combo = QComboBox()
        layout.addWidget(self.depth_layer_label)
        layout.addWidget(self.depth_layer_combo)

        self.depth_field_label = QLabel("Depth Field Name (for Contour layers):")
        self.depth_field_combo = QComboBox()
        layout.addWidget(self.depth_field_label)
        layout.addWidget(self.depth_field_combo)

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
            "Note: Ellipsoidal uses the project's ellipsoid (fallback WGS84). "
            "Cartesian uses planar distances in the project CRS (requires a projected CRS)."
        )
        self.note_label.setWordWrap(True)
        layout.addWidget(self.note_label)

        # Buttons
        self.button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.line_layers = [
            l for l in QgsProject.instance().mapLayers().values()
            if l.type() == l.VectorLayer and l.geometryType() == QgsWkbTypes.LineGeometry
        ]
        
        for layer in self.line_layers:
            self.layer_combo.addItem(layer.name(), layer.id())

        # Depth layers: raster and vector line layers
        self.depth_layers = [
            l for l in QgsProject.instance().mapLayers().values()
            if (l.type() == l.RasterLayer) or (l.type() == l.VectorLayer and l.geometryType() == QgsWkbTypes.LineGeometry)
        ]
        
        self.depth_layer_combo.addItem("None", None)
        for layer in self.depth_layers:
            self.depth_layer_combo.addItem(layer.name(), layer.id())

        if current_layer:
            idx = self.layer_combo.findData(current_layer.id())
            if idx != -1:
                self.layer_combo.setCurrentIndex(idx)

        if current_unit:
            idx = self.unit_combo.findText(current_unit)
            if idx != -1:
                self.unit_combo.setCurrentIndex(idx)

        if depth_layer:
            idx = self.depth_layer_combo.findData(depth_layer.id())
            if idx != -1:
                self.depth_layer_combo.setCurrentIndex(idx)

        self.layer_combo.currentIndexChanged.connect(self.update_metrics)
        self.depth_checkbox.toggled.connect(self.update_depth_ui)
        self.depth_layer_combo.currentIndexChanged.connect(self.update_depth_ui)
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

        if depth_field:
            idx = self.depth_field_combo.findText(depth_field)
            if idx != -1:
                self.depth_field_combo.setCurrentIndex(idx)

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
            try:
                transform = QgsCoordinateTransform(layer_crs, project_crs, QgsProject.instance())
            except Exception:
                transform = None

        total_length_ell_m = 0.0
        total_length_planar_m = 0.0
        total_vertices = 0

        d_ell = QgsDistanceArea()
        d_ell.setSourceCrs(project_crs, transform_context)
        d_ell.setEllipsoid(QgsProject.instance().ellipsoid() or "WGS84")
        if hasattr(d_ell, "setEllipsoidalMode"):
            d_ell.setEllipsoidalMode(True)

        d_planar = QgsDistanceArea()
        d_planar.setSourceCrs(project_crs, transform_context)
        if hasattr(d_planar, "setEllipsoidalMode"):
            d_planar.setEllipsoidalMode(False)

        planar_ok = not project_crs.isGeographic()

        for feature in layer.getFeatures():
            geom = QgsGeometry(feature.geometry())
            if geom and not geom.isEmpty():
                if transform is not None:
                    try:
                        geom.transform(transform)
                    except Exception:
                        continue

                try:
                    total_length_ell_m += float(d_ell.measureLength(geom))
                except Exception:
                    pass
                if planar_ok:
                    try:
                        total_length_planar_m += float(d_planar.measureLength(geom))
                    except Exception:
                        pass

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

        self.metrics_text.setText(
            f"Length (ellipsoidal): {length_ell_km:.3f} km\n{planar_line}\nAC Count: {total_vertices}"
        )

        # Enable Cartesian checkbox only if the project CRS is projected
        is_projected = not project_crs.isGeographic()
        self.cartesian_checkbox.setEnabled(is_projected)
        if not is_projected:
            self.cartesian_checkbox.setChecked(False)

    def update_depth_ui(self):
        """Update the depth UI elements based on checkbox and layer selection."""
        enabled = self.depth_checkbox.isChecked()
        self.depth_layer_label.setEnabled(enabled)
        self.depth_layer_combo.setEnabled(enabled)
        self.depth_field_label.setEnabled(enabled)
        self.depth_field_combo.setEnabled(enabled)
        
        if enabled:
            layer_id = self.depth_layer_combo.currentData()
            if layer_id:
                layer = QgsProject.instance().mapLayer(layer_id)
                if layer and layer.type() == layer.VectorLayer:
                    # Populate field combo with numeric fields
                    self.depth_field_combo.clear()
                    fields = layer.fields()
                    for field in fields:
                        if field.type() in [QVariant.Int, QVariant.Double, QVariant.LongLong]:
                            self.depth_field_combo.addItem(field.name())
                    self.depth_field_label.setEnabled(True)
                    self.depth_field_combo.setEnabled(True)
                else:
                    self.depth_field_combo.clear()
                    self.depth_field_label.setEnabled(False)
                    self.depth_field_combo.setEnabled(False)
            else:
                self.depth_field_combo.clear()
                self.depth_field_label.setEnabled(False)
                self.depth_field_combo.setEnabled(False)

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
        depth_layer_id = self.depth_layer_combo.currentData()
        depth_layer = QgsProject.instance().mapLayer(depth_layer_id) if depth_layer_id else None
        depth_field = self.depth_field_combo.currentText().strip()
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
            depth_layer,
            depth_field,
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
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        layout = QVBoxLayout(self)

        self.kp_label = QLabel("KP (km):")
        self.kp_input = QLineEdit(self)
        self.kp_input.setPlaceholderText("e.g. 12.345")
        if initial_kp_km is not None:
            try:
                self.kp_input.setText(f"{float(initial_kp_km):.3f}")
            except Exception:
                pass

        kp_row = QHBoxLayout()
        kp_row.addWidget(self.kp_label)
        kp_row.addWidget(self.kp_input)
        layout.addLayout(kp_row)

        self.range_label = QLabel(
            f"Available KP range: {self._min_kp_km:.3f} to {self._max_kp_km:.3f} km"
        )
        layout.addWidget(self.range_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.go_button = QPushButton("Go to")
        self.buttons.addButton(self.go_button, QDialogButtonBox.AcceptRole)
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
        except Exception:
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
    """
    def __init__(self, iface):
        self.iface = iface
        self.mapTool = None
        self.referenceLayer = None
        self.measurementUnit = "km"
        self.showReverseKP = False
        self.useCartesian = False
        self.showDepth = False
        self.depthLayer = None
        self.depthField = ""
        # Right-click copy settings (KP is always included)
        self.copyIncludeRKP = False
        self.copyIncludeDCC = False
        self.copyIncludeLatLon = False
        self.copyLatLonFormat = "DD"
        self.copyLatLonStyle = "LABELLED"
        self.toolButton = None
        self.toolButtonAction = None
        self.actionConfig = None
        self.actionGoToKP = None
        self.load_settings()

    def _safe_layer_id(self, layer) -> Optional[str]:
        """Return a layer id if the layer wrapper is still valid, else None."""
        if layer is None:
            return None
        try:
            if _sip_isdeleted(layer):
                return None
        except Exception:
            # If sip is missing or the wrapper check fails, fall back to try/except below.
            pass
        try:
            return layer.id()
        except Exception:
            return None

    def _get_reference_layer(self) -> Optional[QgsVectorLayer]:
        """Return the current reference layer if it's still alive and in the project."""
        layer = self.referenceLayer
        layer_id = self._safe_layer_id(layer)
        if not layer_id:
            self.referenceLayer = None
            return None

        try:
            project_layer = QgsProject.instance().mapLayer(layer_id)
        except Exception:
            project_layer = None

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
        import os
        self.toolButton = QToolButton(self.iface.mainWindow())
        # Prefer dedicated kp_mouse_tool_icon.png if present; fallback to main plugin icon resource
        try:
            plugin_dir = os.path.dirname(os.path.dirname(__file__))  # maptools/ -> plugin root
            custom_icon_path = os.path.join(plugin_dir, 'kp_mouse_tool_icon.png')
            if os.path.exists(custom_icon_path):
                self.toolButton.setIcon(QIcon(custom_icon_path))
            else:
                self.toolButton.setIcon(QIcon(":/plugins/subsea_cable_tools/icon.png"))
        except Exception:
            # Last-resort fallback
            self.toolButton.setIcon(QIcon(":/plugins/subsea_cable_tools/icon.png"))
        self.toolButton.setCheckable(True)
        self.toolButton.toggled.connect(self.toggle_tool)
        self.toolButton.setToolTip("Enable/Disable KP Mouse Tool")
        self.toolButton.setPopupMode(QToolButton.MenuButtonPopup)

        menu = QMenu(self.toolButton)
        self.actionConfig = QAction("Configure...", self.iface.mainWindow())
        self.actionConfig.triggered.connect(self.show_config_dialog)
        menu.addAction(self.actionConfig)

        self.actionGoToKP = QAction("Go to KP...", self.iface.mainWindow())
        self.actionGoToKP.triggered.connect(self.show_go_to_kp_dialog)
        menu.addAction(self.actionGoToKP)

        self.toolButton.setMenu(menu)

        self.toolButtonAction = self.iface.addToolBarWidget(self.toolButton)
        self._update_go_to_kp_enabled()

        try:
            QgsProject.instance().layersRemoved.connect(self._on_layers_removed)
        except Exception:
            pass

        self.iface.addPluginToMenu("&Subsea Cable Tools", self.actionConfig)

    def unload(self):
        """Remove UI elements, disconnect signals, and clean up resources when the plugin is unloaded."""
        # Deactivate the map tool if it's active
        if hasattr(self, 'mapTool') and self.mapTool:
            try:
                if self.iface.mapCanvas().mapTool() == self.mapTool:
                    self.iface.mapCanvas().unsetMapTool(self.mapTool)
            except Exception:
                pass
            try:
                self.mapTool.cleanup_resources()
            except Exception:
                pass
            self.mapTool = None

        # Remove UI elements
        if hasattr(self, 'toolButtonAction') and self.toolButtonAction:
            try:
                self.iface.removeToolBarIcon(self.toolButtonAction)
            except Exception:
                pass
            self.toolButtonAction = None

        if hasattr(self, 'actionConfig') and self.actionConfig:
            try:
                self.iface.removePluginMenu("&Subsea Cable Tools", self.actionConfig)
            except Exception:
                pass
            self.actionConfig = None

        # Go to KP is a menu action under the tool button.
        self.actionGoToKP = None

        # Disconnect signals
        if hasattr(self, 'toolButton') and self.toolButton:
            try:
                self.toolButton.toggled.disconnect()
            except Exception:
                pass
            self.toolButton = None

        try:
            QgsProject.instance().layersRemoved.disconnect(self._on_layers_removed)
        except Exception:
            pass

        # Clean up references
        self.referenceLayer = None
        self.measurementUnit = None
        self.showReverseKP = None
        self.iface = None

    def cleanup_resources(self):
        """Clean up all resources to prevent memory leaks and disconnect signals."""
        # Stop timers and delete them
        if hasattr(self, 'mouse_stop_timer') and self.mouse_stop_timer:
            try:
                self.mouse_stop_timer.stop()
                self.mouse_stop_timer.timeout.disconnect()
                self.mouse_stop_timer.deleteLater()
            except Exception:
                pass
            self.mouse_stop_timer = None
        if hasattr(self, 'persistent_tooltip_timer') and self.persistent_tooltip_timer:
            try:
                self.persistent_tooltip_timer.stop()
                self.persistent_tooltip_timer.timeout.disconnect()
                self.persistent_tooltip_timer.deleteLater()
            except Exception:
                pass
            self.persistent_tooltip_timer = None
        # Clean up rubber band
        if hasattr(self, 'rubberBand') and self.rubberBand:
            try:
                self.rubberBand.reset(QgsWkbTypes.LineGeometry)
                scene = self.canvas.scene()
                if scene and self.rubberBand in scene.items():
                    scene.removeItem(self.rubberBand)
            except Exception:
                pass
            self.rubberBand = None
        # Clean up vertex marker
        if hasattr(self, 'closestPointMarker') and self.closestPointMarker:
            try:
                self.closestPointMarker.hide()
                scene = self.canvas.scene()
                if scene and self.closestPointMarker in scene.items():
                    scene.removeItem(self.closestPointMarker)
            except Exception:
                pass
            self.closestPointMarker = None
        # Clear cached data
        self.features_geoms = []
        self.segment_lengths = []
        self.total_length_meters = 0
        # Hide any remaining tooltips
        QToolTip.hideText()

    def toggle_tool(self, checked):
        """Handle the toggling of the map tool."""
        if checked:
            layer = self._get_reference_layer()
            if not layer:
                self.iface.messageBar().pushMessage(
                    "Info", "KP Mouse Tool: Please configure a reference layer.", level=Qgis.Info
                )
                self.show_config_dialog()
                layer = self._get_reference_layer()
                if not layer:
                    self.toolButton.setChecked(False)
                    return

            # Verify features exist (guard against deleted wrappers/provider errors)
            try:
                features = list(layer.getFeatures())
            except Exception:
                features = []
            if not features:
                QMessageBox.information(
                    self.iface.mainWindow(), "KP Mouse Tool", "No features found in the reference layer!"
                )
                self.toolButton.setChecked(False)
                return

            self.mapTool = KPMouseMapTool(
                self.iface.mapCanvas(),
                layer,
                self.iface,
                self.measurementUnit,
                self.showReverseKP,
                self.useCartesian,
                self.showDepth,
                self.depthLayer,
                self.depthField,
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

    def show_config_dialog(self):
        """Show the configuration dialog."""
        dialog = KPConfigDialog(
            self.iface.mainWindow(),
            self._get_reference_layer(),
            self.measurementUnit,
            self.showReverseKP,
            self.useCartesian,
            self.showDepth,
            self.depthLayer,
            self.depthField,
            self.copyIncludeRKP,
            self.copyIncludeDCC,
            self.copyIncludeLatLon,
            self.copyLatLonFormat,
            self.copyLatLonStyle,
        )
        if dialog.exec_():
            (
                layer,
                unit,
                show_reverse_kp,
                use_cartesian,
                show_depth,
                depth_layer,
                depth_field,
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
                self.showDepth = show_depth
                self.depthLayer = depth_layer
                self.depthField = depth_field
                self.copyIncludeRKP = bool(copy_include_rkp)
                self.copyIncludeDCC = bool(copy_include_dcc)
                self.copyIncludeLatLon = bool(copy_include_latlon)
                self.copyLatLonFormat = str(copy_latlon_format or "DD").upper()
                self.copyLatLonStyle = str(copy_latlon_style or "LABELLED").upper()
                self.save_settings()
                self.iface.messageBar().pushMessage(
                    "Success", f"KP Mouse Tool configured with layer '{layer.name()}'", level=Qgis.Success
                )
                if self.toolButton.isChecked():
                    self.toggle_tool(True)  # Re-enable with new settings
                self._update_go_to_kp_enabled()
            else:
                self.iface.messageBar().pushMessage(
                    "Warning", "No valid reference layer selected.", level=Qgis.Warning
                )
                self._update_go_to_kp_enabled()

    def _on_layers_removed(self, layer_ids):
        if not layer_ids:
            return
        layer = self.referenceLayer
        try:
            layer_id = self._safe_layer_id(layer)
            if layer_id and layer_id in set(layer_ids):
                self.referenceLayer = None
        except Exception:
            # If the wrapper has already been deleted, clear our cached reference.
            self.referenceLayer = None
        self._update_go_to_kp_enabled()

    def _reference_layer_ready(self) -> bool:
        layer = self._get_reference_layer()
        if layer is None:
            return False
        try:
            if not layer.isValid():
                return False
        except RuntimeError:
            # wrapped C/C++ object deleted
            self.referenceLayer = None
            return False
        except Exception:
            return False

        try:
            if layer.geometryType() != QgsWkbTypes.LineGeometry:
                return False
        except Exception:
            return False
        try:
            if layer.featureCount() <= 0:
                return False
        except Exception:
            # If featureCount isn't available for some providers, fall back to iterator check
            try:
                next(layer.getFeatures())
            except StopIteration:
                return False
            except Exception:
                return False
        return True

    def _update_go_to_kp_enabled(self):
        if self.actionGoToKP is None:
            return
        try:
            self.actionGoToKP.setEnabled(self._reference_layer_ready())
        except Exception:
            # If something unexpected happens during layer checks, fail closed.
            self.actionGoToKP.setEnabled(False)

    def _make_distance_area(self) -> QgsDistanceArea:
        distance = QgsDistanceArea()
        project_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        distance.setSourceCrs(project_crs, QgsProject.instance().transformContext())
        if self.useCartesian:
            if hasattr(distance, "setEllipsoidalMode"):
                distance.setEllipsoidalMode(False)
        else:
            ellipsoid = QgsProject.instance().ellipsoid() or "WGS84"
            distance.setEllipsoid(ellipsoid)
            if hasattr(distance, "setEllipsoidalMode"):
                distance.setEllipsoidalMode(True)
        return distance

    def _iter_reference_geometries_project_crs(self):
        """Yield reference layer geometries transformed to project CRS."""
        layer = self._get_reference_layer()
        if layer is None:
            return

        project_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        layer_crs = layer.crs()
        transform = None
        if layer_crs != project_crs:
            transform = QgsCoordinateTransform(layer_crs, project_crs, QgsProject.instance())

        for feature in layer.getFeatures():
            geom = QgsGeometry(feature.geometry())
            if geom is None or geom.isEmpty():
                continue
            if transform is not None:
                try:
                    geom.transform(transform)
                except Exception:
                    continue
            yield geom

    def _reference_kp_range(self) -> Optional[Tuple[float, float]]:
        if not self._reference_layer_ready():
            return None

        distance = self._make_distance_area()
        total_m = 0.0
        for geom in self._iter_reference_geometries_project_crs():
            try:
                total_m += float(distance.measureLength(geom))
            except Exception:
                continue

        total_km = max(0.0, total_m / 1000.0)
        return (0.0, total_km)

    def _point_at_kp_km(self, kp_km: float) -> Optional[QgsPointXY]:
        """Return the point on the reference line at the provided KP (km)."""
        if not self._reference_layer_ready():
            return None

        distance = self._make_distance_area()
        target_m = float(kp_km) * 1000.0
        if target_m < 0:
            return None

        cumulative = 0.0
        last_point = None

        for geom in self._iter_reference_geometries_project_crs():
            parts = []
            try:
                if geom.isMultipart():
                    parts = list(geom.asMultiPolyline())
                else:
                    parts = [geom.asPolyline()]
            except Exception:
                continue

            for part in parts:
                if not part or len(part) < 2:
                    continue

                for i in range(len(part) - 1):
                    p1 = part[i]
                    p2 = part[i + 1]
                    try:
                        seg_len = float(distance.measureLine(p1, p2))
                    except Exception:
                        continue
                    if seg_len <= 0:
                        continue

                    next_cum = cumulative + seg_len
                    last_point = QgsPointXY(p2)

                    if next_cum >= target_m:
                        ratio = (target_m - cumulative) / seg_len
                        x = float(p1.x()) + ratio * (float(p2.x()) - float(p1.x()))
                        y = float(p1.y()) + ratio * (float(p2.y()) - float(p1.y()))
                        return QgsPointXY(x, y)

                    cumulative = next_cum

        return last_point

    def show_go_to_kp_dialog(self):
        if not self._reference_layer_ready():
            self.iface.messageBar().pushMessage(
                "Info",
                "Go to KP is available after configuring a reference line layer.",
                level=Qgis.Info,
                duration=3,
            )
            self._update_go_to_kp_enabled()
            return

        kp_range = self._reference_kp_range()
        if kp_range is None:
            self.iface.messageBar().pushMessage(
                "Warning",
                "Reference layer is not ready. Please reconfigure.",
                level=Qgis.Warning,
                duration=3,
            )
            self._update_go_to_kp_enabled()
            return

        min_kp, max_kp = kp_range
        initial = None
        if self.mapTool is not None and getattr(self.mapTool, "last_chainage", None) is not None:
            try:
                initial = float(self.mapTool.last_chainage)
            except Exception:
                initial = None

        dialog = GoToKPDialog(self.iface.mainWindow(), min_kp, max_kp, initial_kp_km=initial)
        if dialog.exec_():
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
        depth_id = self._safe_layer_id(self.depthLayer)
        if depth_id:
            settings.setValue("depthLayerId", depth_id)
        else:
            settings.remove("depthLayerId")
        settings.setValue("depthField", self.depthField)

    def load_settings(self):
        """Load settings from QSettings."""
        settings = QSettings("SubseaCableTools", "KPMouseTool")
        layer_id = settings.value("referenceLayerId")
        if layer_id:
            self.referenceLayer = QgsProject.instance().mapLayer(layer_id)
        self.measurementUnit = settings.value("measurementUnit", "km")
        self.showReverseKP = settings.value("showReverseKP", False, type=bool)
        self.useCartesian = settings.value("useCartesian", False, type=bool)
        self.showDepth = settings.value("showDepth", False, type=bool)
        # New clipboard settings (default: only KP)
        self.copyIncludeRKP = settings.value("copyIncludeRKP", False, type=bool)
        self.copyIncludeDCC = settings.value("copyIncludeDCC", False, type=bool)
        self.copyIncludeLatLon = settings.value("copyIncludeLatLon", False, type=bool)
        self.copyLatLonFormat = str(settings.value("copyLatLonFormat", "DD") or "DD").upper()
        self.copyLatLonStyle = str(settings.value("copyLatLonStyle", "LABELLED") or "LABELLED").upper()
        depth_layer_id = settings.value("depthLayerId")
        if depth_layer_id:
            self.depthLayer = QgsProject.instance().mapLayer(depth_layer_id)
        self.depthField = settings.value("depthField", "")
