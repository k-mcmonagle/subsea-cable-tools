# kp_mouse_maptool.py
# -*- coding: utf-8 -*-
"""
KPMouseMapTool
Integrated into the Subsea Cable Tools plugin.
This tool displays the closest point on a selected line to the mouse pointer,
draws a dashed line connecting them, and shows distance and KP (chainage) data.
Right-clicking copies the distance, KP (DCC) and lat/long of the mouse position to the clipboard.
"""

from qgis.PyQt.QtCore import Qt, QSettings, QTimer
from qgis.PyQt.QtGui import QIcon, QColor, QCursor
from qgis.PyQt.QtWidgets import (QAction, QMessageBox, QToolTip,
                                 QApplication, QDialog, QVBoxLayout,
                                 QComboBox, QLabel, QDialogButtonBox,
                                 QToolButton, QMenu, QCheckBox)
from qgis.core import (QgsWkbTypes, QgsGeometry, QgsProject, QgsDistanceArea,
                       QgsPointXY, QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                       Qgis)
from qgis.gui import QgsMapTool, QgsRubberBand, QgsVertexMarker


class KPMouseMapTool(QgsMapTool):
    """
    Map tool that tracks the mouse pointer and shows:
      - The closest point on a selected line,
      - A dashed line between the mouse pointer and that point,
      - The distance (in the selected unit) and chainage (KP).
    Also allows copying the current data via a right-click.
    """
    def __init__(self, canvas, layer, iface, measurementUnit="m", showReverseKP=False):
        super().__init__(canvas)
        self.canvas = canvas
        self.iface = iface
        self.layer = layer
        self.measurementUnit = measurementUnit
        self.showReverseKP = showReverseKP

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

        # Set up a rubber band for drawing the dashed line.
        self.rubberBand = QgsRubberBand(self.canvas, QgsWkbTypes.LineGeometry)
        self.rubberBand.setColor(QColor(255, 0, 0))
        self.rubberBand.setWidth(2)
        self.rubberBand.setLineStyle(Qt.DashLine)

        # Set up a vertex marker to highlight the closest point.
        self.closestPointMarker = QgsVertexMarker(self.canvas)
        self.closestPointMarker.setColor(QColor(0, 255, 0))
        self.closestPointMarker.setIconType(QgsVertexMarker.ICON_CROSS)
        self.closestPointMarker.setIconSize(10)
        self.closestPointMarker.setPenWidth(3)

        # Variables to store the latest values for copy-to-clipboard functionality.
        self.last_mouse_point = None
        self.last_distance = None
        self.last_chainage = None
        self.last_reverse_chainage = None
        self.last_message = ""
        self.last_global_pos = None

        # Timer to detect when the mouse stops moving.
        self.mouse_stop_timer = QTimer(self.canvas)
        self.mouse_stop_timer.setSingleShot(True)
        self.mouse_stop_timer.timeout.connect(self.start_persistent_tooltip)

        # Timer to repeatedly show the tooltip to make it persistent.
        self.persistent_tooltip_timer = QTimer(self.canvas)
        self.persistent_tooltip_timer.timeout.connect(self.show_persistent_tooltip)

        # Set the initial cursor to crosshair.
        self.canvas.setCursor(Qt.CrossCursor)

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
        project_crs = QgsProject.instance().transformContext().destinationCrs()
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

        # Show the standard, transient tooltip immediately.
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
        # Check for right-click events.
        if event.button() == Qt.RightButton:
            # Ensure we have valid data from the last move event.
            if self.last_mouse_point is None or self.last_distance is None or self.last_chainage is None:
                return

            # Convert the current mouse point to WGS84 (lat/long).
            source_crs = self.canvas.mapSettings().destinationCrs()
            dest_crs = QgsCoordinateReferenceSystem("EPSG:4326")
            transform = QgsCoordinateTransform(source_crs, dest_crs, QgsProject.instance())
            wgs84_point = transform.transform(self.last_mouse_point)
            lat = wgs84_point.y()
            lon = wgs84_point.x()

            # Prepare the text to copy.
            clipboard_text = f"KP: {self.last_chainage:.3f}\n"

            if self.last_reverse_chainage is not None:
                clipboard_text += f"rKP: {self.last_reverse_chainage:.3f}\n"
            
            clipboard_text += (f"DCC: {self.last_distance:.2f} {self.measurementUnit}\n"
                               f"Lat: {lat:.6f}, Lon: {lon:.6f}")

            # Copy the text to the clipboard.
            clipboard = QApplication.clipboard()
            clipboard.setText(clipboard_text)

            # Provide user feedback with the updated message.
            feedback = "KP, DCC and Lat/Long copied to clipboard"
            QToolTip.showText(event.globalPos(), feedback, self.canvas)
            self.iface.mainWindow().statusBar().showMessage(feedback, 2000)
            self.iface.messageBar().pushMessage("Info", feedback, level=Qgis.Info, duration=2)

    def deactivate(self):
        # Stop all timers and hide any tooltips.
        self.mouse_stop_timer.stop()
        self.persistent_tooltip_timer.stop()
        QToolTip.hideText()

        QgsMapTool.deactivate(self)
        self.rubberBand.reset(QgsWkbTypes.LineGeometry)
        self.closestPointMarker.hide()
        self.iface.mainWindow().statusBar().clearMessage()


class KPConfigDialog(QDialog):
    """A dialog for configuring the KP Mouse Tool settings."""
    def __init__(self, parent=None, current_layer=None, current_unit="km", show_reverse_kp=False):
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

    def get_settings(self):
        layer_id = self.layer_combo.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        unit = self.unit_combo.currentText()
        show_reverse_kp = self.reverse_kp_checkbox.isChecked()
        return layer, unit, show_reverse_kp


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
        self.toolButton = None
        self.toolButtonAction = None
        self.actionConfig = None
        self.load_settings()

    def initGui(self):
        """Initialize the UI elements for the KP Mouse Tool."""
        self.toolButton = QToolButton(self.iface.mainWindow())
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
        """Remove UI elements when the plugin is unloaded."""
        if self.toolButtonAction:
            self.iface.removeToolBarIcon(self.toolButtonAction)
        if self.actionConfig:
            self.iface.removePluginMenu("&Subsea Cable Tools", self.actionConfig)
        self.toolButton = None
        self.toolButtonAction = None
        self.actionConfig = None

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
                self.iface.mapCanvas(), self.referenceLayer, self.iface, self.measurementUnit, self.showReverseKP
            )
            self.iface.mapCanvas().setMapTool(self.mapTool)
        else:
            if self.mapTool:
                self.iface.mapCanvas().unsetMapTool(self.mapTool)
                self.mapTool = None

    def show_config_dialog(self):
        """Show the configuration dialog."""
        dialog = KPConfigDialog(self.iface.mainWindow(), self.referenceLayer, self.measurementUnit, self.showReverseKP)
        if dialog.exec_():
            layer, unit, show_reverse_kp = dialog.get_settings()
            if layer:
                self.referenceLayer = layer
                self.measurementUnit = unit
                self.showReverseKP = show_reverse_kp
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
