# kp_mouse_maptool.py
# -*- coding: utf-8 -*-
"""
KPMouseMapTool
Integrated into the Subsea Cable Tools plugin.
This tool displays the closest point on a selected line to the mouse pointer,
draws a dashed line connecting them, and shows distance and KP (chainage) data.
Right-clicking copies the distance, KP (DCC) and lat/long of the mouse position to the clipboard.
"""

from qgis.PyQt.QtCore import Qt, QSettings, QTimer, QVariant
from qgis.PyQt.QtGui import QIcon, QColor, QCursor
from qgis.PyQt.QtWidgets import (QAction, QMessageBox, QToolTip,
                                 QApplication, QDialog, QVBoxLayout,
                                 QComboBox, QLabel, QDialogButtonBox,
                                 QToolButton, QMenu, QCheckBox, QLineEdit, QHBoxLayout)
from qgis.core import (QgsWkbTypes, QgsGeometry, QgsProject, QgsDistanceArea,
                       QgsPointXY, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       Qgis, QgsVectorLayer, QgsField, QgsFeature)
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
    def __init__(self, canvas, layer, iface, measurementUnit="m", showReverseKP=False, useCartesian=False):
        super().__init__(canvas)
        self.canvas = canvas
        self.iface = iface
        self.layer = layer
        self.measurementUnit = measurementUnit
        self.showReverseKP = showReverseKP
        self.useCartesian = useCartesian

        # Distance / chainage preparation
        self.distanceArea = QgsDistanceArea()
        project_crs = self.canvas.mapSettings().destinationCrs()
        self.distanceArea.setSourceCrs(project_crs, QgsProject.instance().transformContext())
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

        # Set up ellipsoidal distance measurements.
        self.distanceArea = QgsDistanceArea()
        project_crs = self.canvas.mapSettings().destinationCrs()
        self.distanceArea.setSourceCrs(project_crs, QgsProject.instance().transformContext())
        ellipsoid = QgsProject.instance().ellipsoid()
        if ellipsoid:
            self.distanceArea.setEllipsoid(ellipsoid)
            if hasattr(self.distanceArea, "setEllipsoidalMode"):
                self.distanceArea.setEllipsoidalMode(True)
        else:
            if hasattr(self.distanceArea, "setEllipsoidalMode"):
                self.distanceArea.setEllipsoidalMode(False)

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
            # Optional: keep original copy behaviour
            if self.last_mouse_point is not None and self.last_chainage is not None:
                act_copy = menu.addAction("Copy KP Info to Clipboard")
            else:
                act_copy = None

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
            elif act_copy and chosen == act_copy:
                self._copy_kp_to_clipboard()

    def _copy_kp_to_clipboard(self):
        """Preserve original copy-to-clipboard behaviour as a callable."""
        if self.last_mouse_point is None or self.last_distance is None or self.last_chainage is None:
            return
        try:
            source_crs = self.canvas.mapSettings().destinationCrs()
            dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())
            wgs84_point = transform.transform(self.last_mouse_point)
            lat = wgs84_point.y()
            lon = wgs84_point.x()
            clipboard_text = f"KP: {self.last_chainage:.3f}\n"
            if self.last_reverse_chainage is not None:
                clipboard_text += f"rKP: {self.last_reverse_chainage:.3f}\n"
            clipboard_text += (f"DCC: {self.last_distance:.2f} {self.measurementUnit}\n"
                               f"Lat: {lat:.6f}, Lon: {lon:.6f}")
            QApplication.clipboard().setText(clipboard_text)
            feedback = "KP, DCC and Lat/Long copied to clipboard"
            QToolTip.showText(QCursor.pos(), feedback, self.canvas)
            self.iface.mainWindow().statusBar().showMessage(feedback, 2000)
            self.iface.messageBar().pushMessage("Info", feedback, level=Qgis.Info, duration=2)
        except Exception as e:
            self.iface.messageBar().pushMessage("Error", f"Copy failed: {e}", level=Qgis.Critical, duration=4)

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
    def __init__(self, parent=None, current_layer=None, current_unit="km", show_reverse_kp=False, current_use_cartesian=False):
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
        self.cartesian_checkbox = QCheckBox("Use Cartesian distances (planar, in CRS units)")
        self.cartesian_checkbox.setChecked(current_use_cartesian)
        layout.addWidget(self.cartesian_checkbox)

        # Note about calculations
        self.note_label = QLabel("Note: KP calculations are based on geodetic distances using the WGS84 ellipsoid.")
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

        if current_layer:
            idx = self.layer_combo.findData(current_layer.id())
            if idx != -1:
                self.layer_combo.setCurrentIndex(idx)

        if current_unit:
            idx = self.unit_combo.findText(current_unit)
            if idx != -1:
                self.unit_combo.setCurrentIndex(idx)

        self.layer_combo.currentIndexChanged.connect(self.update_metrics)
        self.update_metrics()

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
        
        total_length = 0
        total_vertices = 0
        d = QgsDistanceArea()
        d.setEllipsoid(QgsProject.instance().ellipsoid())

        for feature in layer.getFeatures():
            geom = feature.geometry()
            if geom:
                total_length += d.measureLength(geom)
                if geom.isMultipart():
                    for part in geom.asMultiPolyline():
                        total_vertices += len(part)
                else:
                    total_vertices += len(geom.asPolyline())

        length_km = total_length / 1000
        
        self.metrics_text.setText(f"Length: {length_km:.2f} km\nAC Count: {total_vertices}")

        # Enable Cartesian checkbox only if layer CRS is projected
        is_projected = not layer.crs().isGeographic()
        self.cartesian_checkbox.setEnabled(is_projected)
        if not is_projected:
            self.cartesian_checkbox.setChecked(False)

    def get_settings(self):
        layer_id = self.layer_combo.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        unit = self.unit_combo.currentText()
        show_reverse_kp = self.reverse_kp_checkbox.isChecked()
        use_cartesian = self.cartesian_checkbox.isChecked()
        return layer, unit, show_reverse_kp, use_cartesian


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
        self.toolButton = None
        self.toolButtonAction = None
        self.actionConfig = None
        self.load_settings()

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

        self.toolButton.setMenu(menu)

        self.toolButtonAction = self.iface.addToolBarWidget(self.toolButton)
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

        # Disconnect signals
        if hasattr(self, 'toolButton') and self.toolButton:
            try:
                self.toolButton.toggled.disconnect()
            except Exception:
                pass
            self.toolButton = None

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
            if not self.referenceLayer:
                self.iface.messageBar().pushMessage(
                    "Info", "KP Mouse Tool: Please configure a reference layer.", level=Qgis.Info
                )
                self.show_config_dialog()
                if not self.referenceLayer:
                    self.toolButton.setChecked(False)
                    return

            # Verify the layer is still in the project
            if self.referenceLayer.id() not in [l.id() for l in QgsProject.instance().mapLayers().values()]:
                self.iface.messageBar().pushMessage(
                    "Warning", "KP Mouse Tool: Reference layer not found. Please reconfigure.", level=Qgis.Warning
                )
                self.referenceLayer = None
                self.toolButton.setChecked(False)
                self.show_config_dialog()
                return

            features = list(self.referenceLayer.getFeatures())
            if not features:
                QMessageBox.information(
                    self.iface.mainWindow(), "KP Mouse Tool", "No features found in the reference layer!"
                )
                self.toolButton.setChecked(False)
                return

            self.mapTool = KPMouseMapTool(
                self.iface.mapCanvas(), self.referenceLayer, self.iface, self.measurementUnit, self.showReverseKP, self.useCartesian
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

    def show_config_dialog(self):
        """Show the configuration dialog."""
        dialog = KPConfigDialog(self.iface.mainWindow(), self.referenceLayer, self.measurementUnit, self.showReverseKP, self.useCartesian)
        if dialog.exec_():
            layer, unit, show_reverse_kp, use_cartesian = dialog.get_settings()
            if layer:
                self.referenceLayer = layer
                self.measurementUnit = unit
                self.showReverseKP = show_reverse_kp
                self.useCartesian = use_cartesian
                self.save_settings()
                self.iface.messageBar().pushMessage(
                    "Success", f"KP Mouse Tool configured with layer '{layer.name()}'", level=Qgis.Success
                )
                if self.toolButton.isChecked():
                    self.toggle_tool(True)  # Re-enable with new settings
            else:
                self.iface.messageBar().pushMessage(
                    "Warning", "No valid reference layer selected.", level=Qgis.Warning
                )

    def save_settings(self):
        """Save settings to QSettings."""
        settings = QSettings("SubseaCableTools", "KPMouseTool")
        if self.referenceLayer:
            settings.setValue("referenceLayerId", self.referenceLayer.id())
        else:
            settings.remove("referenceLayerId")
        settings.setValue("measurementUnit", self.measurementUnit)
        settings.setValue("showReverseKP", self.showReverseKP)

    def load_settings(self):
        """Load settings from QSettings."""
        settings = QSettings("SubseaCableTools", "KPMouseTool")
        layer_id = settings.value("referenceLayerId")
        if layer_id:
            self.referenceLayer = QgsProject.instance().mapLayer(layer_id)
        self.measurementUnit = settings.value("measurementUnit", "km")
        self.showReverseKP = settings.value("showReverseKP", False, type=bool)
