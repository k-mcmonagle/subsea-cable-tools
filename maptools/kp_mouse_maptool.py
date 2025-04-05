# kp_mouse_maptool.py
# -*- coding: utf-8 -*-
"""
KPMouseMapTool
Integrated into the Subsea Cable Tools plugin.
This tool displays the closest point on a selected line to the mouse pointer,
draws a dashed line connecting them, and shows distance and KP (chainage) data.
Right-clicking copies the distance, KP (DCC) and lat/long of the mouse position to the clipboard.
"""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.PyQt.QtWidgets import (QAction, QMessageBox, QInputDialog, QToolTip,
                                 QToolButton, QMenu, QApplication)
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
    def __init__(self, canvas, feature, layer, iface, measurementUnit="m"):
        super().__init__(canvas)
        self.canvas = canvas
        self.iface = iface
        self.layer = layer
        self.measurementUnit = measurementUnit

        # Transform the feature geometry to the project CRS if necessary.
        project_crs = self.canvas.mapSettings().destinationCrs()
        layer_crs = self.layer.crs()
        # Instead of using clone(), create a new geometry instance
        self.feature_geom = QgsGeometry(feature.geometry())
        if layer_crs != project_crs:
            transform = QgsCoordinateTransform(layer_crs, project_crs, QgsProject.instance())
            self.feature_geom.transform(transform)

        # Set up ellipsoidal distance measurements.
        self.distanceArea = QgsDistanceArea()
        self.distanceArea.setSourceCrs(project_crs, QgsProject.instance().transformContext())
        ellipsoid = QgsProject.instance().ellipsoid()
        if ellipsoid:
            self.distanceArea.setEllipsoid(ellipsoid)
            if hasattr(self.distanceArea, "setEllipsoidalMode"):
                self.distanceArea.setEllipsoidalMode(True)
        else:
            if hasattr(self.distanceArea, "setEllipsoidalMode"):
                self.distanceArea.setEllipsoidalMode(False)

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

    def canvasMoveEvent(self, event):
        # Convert the mouse event position to map coordinates.
        mousePoint = self.toMapCoordinates(event.pos())
        if self.feature_geom.isEmpty():
            return

        # Handle multipart geometries by using the first part.
        if self.feature_geom.isMultipart():
            parts = self.feature_geom.asMultiPolyline()
            if not parts or len(parts[0]) == 0:
                return
            polyline = parts[0]
            line_geom = QgsGeometry.fromPolylineXY(polyline)
        else:
            polyline = self.feature_geom.asPolyline()
            line_geom = QgsGeometry.fromPolylineXY(polyline)

        # Get the closest point along the line.
        mouse_geom = QgsGeometry.fromPointXY(mousePoint)
        distance_along_line = line_geom.lineLocatePoint(mouse_geom)
        closest_point_geom = line_geom.interpolate(distance_along_line)
        if not closest_point_geom or closest_point_geom.isEmpty():
            return
        closest_point = closest_point_geom.asPoint()

        # Update the rubber band and marker.
        self.rubberBand.reset(QgsWkbTypes.LineGeometry)
        self.rubberBand.addPoint(mousePoint)
        self.rubberBand.addPoint(closest_point)
        self.closestPointMarker.setCenter(closest_point)

        # Calculate distance between the mouse and the closest point.
        distance_meters = self.distanceArea.measureLine(mousePoint, closest_point)
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
        tolerance = 1e-6
        total_length = 0.0
        chainage_meters = 0.0
        found = False
        closest_point_xy = QgsPointXY(closest_point)
        for i in range(len(polyline) - 1):
            p1 = polyline[i]
            p2 = polyline[i + 1]
            seg_length = self.distanceArea.measureLine(p1, p2)
            d_from_p1 = self.distanceArea.measureLine(p1, closest_point_xy)
            if d_from_p1 <= seg_length + tolerance:
                chainage_meters = total_length + d_from_p1
                found = True
                break
            total_length += seg_length
        if not found:
            chainage_meters = total_length
        chainage_km = chainage_meters / 1000.0

        # Save these values for use in the right-click copy functionality.
        self.last_mouse_point = mousePoint
        self.last_distance = converted_distance
        self.last_chainage = chainage_km

        # Create a message and display it in the status bar and as a tooltip.
        message = (f"Distance: {converted_distance:.2f} {self.measurementUnit}\n"
                   f"KP {chainage_km:.3f}")
        self.iface.mainWindow().statusBar().showMessage(message)
        QToolTip.showText(event.globalPos(), message, self.canvas)

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
            clipboard_text = (f"Distance: {self.last_distance:.2f} {self.measurementUnit}\n"
                              f"KP: {self.last_chainage:.3f}\n"
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
        QgsMapTool.deactivate(self)
        self.rubberBand.reset(QgsWkbTypes.LineGeometry)
        self.closestPointMarker.hide()
        self.iface.mainWindow().statusBar().clearMessage()


class KPMouseTool:
    """
    This class wraps the map tool functionality with a toolbar button and dropdown menu.
    It allows the user to toggle tracking on/off, select a reference line layer,
    and set the measurement units.
    """
    def __init__(self, iface):
        self.iface = iface
        self.mapTool = None
        self.referenceLayer = None
        self.measurementUnit = "km"  # Default measurement unit
        self.toolButton = None
        self.toolButtonAction = None  # Will hold the toolbar widget
        self.actionToggleTracking = None
        self.actionSelectLayer = None
        self.actionSetMeasurementUnit = None

    def initGui(self):
        """Initialize the UI elements for the KP Mouse Tool."""
        self.toolButton = QToolButton(self.iface.mainWindow())
        self.toolButton.setIcon(QIcon(":/plugins/subsea_cable_tools/icon.png"))
        self.toolButton.setText("KP Mouse Tool")
        self.toolButton.setToolTip("KP Mouse Tool Options")
        self.toolButton.setPopupMode(QToolButton.MenuButtonPopup)

        # Create the dropdown menu for extra options.
        menu = QMenu(self.toolButton)
        self.actionToggleTracking = QAction("Enable Tracking", self.iface.mainWindow())
        self.actionToggleTracking.setCheckable(True)
        self.actionToggleTracking.toggled.connect(self.toggleTracking)
        menu.addAction(self.actionToggleTracking)

        self.actionSelectLayer = QAction("Select Reference Layer", self.iface.mainWindow())
        self.actionSelectLayer.triggered.connect(self.selectReferenceLayer)
        menu.addAction(self.actionSelectLayer)

        self.actionSetMeasurementUnit = QAction("Set Measurement Unit", self.iface.mainWindow())
        self.actionSetMeasurementUnit.triggered.connect(self.setMeasurementUnit)
        menu.addAction(self.actionSetMeasurementUnit)

        self.toolButton.setMenu(menu)
        self.toolButtonAction = self.iface.addToolBarWidget(self.toolButton)

    def unload(self):
        """Remove UI elements when the plugin is unloaded."""
        if self.toolButtonAction:
            self.iface.removeToolBarIcon(self.toolButtonAction)
            self.toolButtonAction = None
        if self.toolButton:
            try:
                self.actionToggleTracking.toggled.disconnect(self.toggleTracking)
            except Exception:
                pass
            try:
                self.actionSelectLayer.triggered.disconnect(self.selectReferenceLayer)
            except Exception:
                pass
            try:
                self.actionSetMeasurementUnit.triggered.disconnect(self.setMeasurementUnit)
            except Exception:
                pass
            self.toolButton.hide()
            self.toolButton.setParent(None)
            self.toolButton.deleteLater()
            self.toolButton = None

    def toggleTracking(self, enabled):
        if enabled:
            self.enableTracking()
        else:
            self.disableTracking()

    def enableTracking(self):
        if self.referenceLayer is None:
            QMessageBox.information(self.iface.mainWindow(), "KP Mouse Tool",
                                    "Please select a reference layer first.")
            self.actionToggleTracking.setChecked(False)
            return
        if self.referenceLayer.geometryType() != QgsWkbTypes.LineGeometry:
            QMessageBox.information(self.iface.mainWindow(), "KP Mouse Tool",
                                    "The selected reference layer is not a line layer!")
            self.actionToggleTracking.setChecked(False)
            return
        features = list(self.referenceLayer.getFeatures())
        if not features:
            QMessageBox.information(self.iface.mainWindow(), "KP Mouse Tool",
                                    "No features found in the reference layer!")
            self.actionToggleTracking.setChecked(False)
            return
        feature = features[0]
        self.mapTool = KPMouseMapTool(self.iface.mapCanvas(), feature,
                                      self.referenceLayer, self.iface, self.measurementUnit)
        self.iface.mapCanvas().setMapTool(self.mapTool)

    def disableTracking(self):
        if self.mapTool:
            self.iface.mapCanvas().unsetMapTool(self.mapTool)
            self.mapTool = None
            self.iface.mainWindow().statusBar().clearMessage()

    def selectReferenceLayer(self):
        layer = self.promptForLineLayer()
        if layer:
            self.referenceLayer = layer
            QMessageBox.information(self.iface.mainWindow(), "KP Mouse Tool",
                                    f"Reference layer set to: {layer.name()}")
            if self.actionToggleTracking.isChecked():
                self.enableTracking()

    def promptForLineLayer(self):
        line_layers = []
        for lyr in QgsProject.instance().mapLayers().values():
            # Check if the layer is a vector layer with line geometry.
            if lyr.type() == lyr.VectorLayer and lyr.geometryType() == QgsWkbTypes.LineGeometry:
                line_layers.append(lyr)
        if not line_layers:
            QMessageBox.information(self.iface.mainWindow(), "KP Mouse Tool",
                                    "No line layers available!")
            return None
        names = [lyr.name() for lyr in line_layers]
        layer_choice, ok = QInputDialog.getItem(self.iface.mainWindow(),
                                                "Select Line Layer", "Line Layer:",
                                                names, 0, False)
        if not ok:
            return None
        for lyr in line_layers:
            if lyr.name() == layer_choice:
                return lyr
        return None

    def setMeasurementUnit(self):
        units = ["m", "km", "nautical miles", "miles"]
        try:
            current_index = units.index(self.measurementUnit)
        except ValueError:
            current_index = 0
        unit_choice, ok = QInputDialog.getItem(self.iface.mainWindow(),
                                               "Select Measurement Unit",
                                               "Measurement Unit:",
                                               units, current_index, False)
        if ok and unit_choice:
            self.measurementUnit = unit_choice
            if self.mapTool:
                self.mapTool.measurementUnit = unit_choice
            QMessageBox.information(self.iface.mainWindow(), "KP Mouse Tool",
                                    f"Measurement unit set to: {unit_choice}")
