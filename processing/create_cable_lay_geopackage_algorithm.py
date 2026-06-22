# create_cable_lay_geopackage_algorithm.py
# -*- coding: utf-8 -*-
"""
CreateCableLayGeoPackageAlgorithm
Create a GeoPackage pre-populated with the empty canonical cable-lay layers.
"""

from __future__ import annotations

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingOutputMultipleLayers,
    QgsProcessingParameterFileDestination,
)

from . import cable_lay_parsers as clp


class CreateCableLayGeoPackageAlgorithm(QgsProcessingAlgorithm):
    """Sets up a GeoPackage with the standard, empty cable-lay layers ready to fill."""

    GEOPACKAGE = "GEOPACKAGE"
    OUTPUT_LAYERS = "OUTPUT_LAYERS"

    def tr(self, string):
        return QCoreApplication.translate("CreateCableLayGeoPackageAlgorithm", string)

    def createInstance(self):
        return CreateCableLayGeoPackageAlgorithm()

    def name(self):
        return "create_cable_lay_geopackage"

    def displayName(self):
        return self.tr("Create Cable Lay GeoPackage")

    def group(self):
        return self.tr("Cable Lay Data Import")

    def groupId(self):
        return "cable_lay_data_import"

    def shortHelpString(self):
        return self.tr(
            """
<h3>Create Cable Lay GeoPackage</h3>
<p>Creates a GeoPackage pre-populated with the standard, <b>empty</b> cable-lay
layers, each with the correct geometry type and CRS (WGS 84 / EPSG:4326). Layer
names are prefixed with the GeoPackage file name so they group and identify
cleanly in the layer tree - e.g. for <code>ProjectX.gpkg</code>:</p>
<ul>
  <li><code>ProjectX_cable_lay</code> (points)</li>
  <li><code>ProjectX_event_logs</code> (points)</li>
  <li><code>ProjectX_slack_logs</code> (lines)</li>
  <li><code>ProjectX_body_logs</code> (points)</li>
  <li><code>ProjectX_model_solutions</code> (points)</li>
  <li><code>ProjectX_as_laid</code> (points)</li>
  <li><code>ProjectX_plough_data</code> (points)</li>
</ul>
<p>Run this once to set up a project GeoPackage, then point each of the Cable Lay
Data Import tools at it to fill the matching layer. This keeps every layer named
consistently and in one file.</p>
<p>Running this on an existing GeoPackage is safe: layers that already exist are
left untouched (their data is preserved) and only missing layers are added.</p>
"""
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFileDestination(
                self.GEOPACKAGE,
                self.tr("GeoPackage to create"),
                fileFilter="GeoPackage (*.gpkg)",
            )
        )
        self.addOutput(
            QgsProcessingOutputMultipleLayers(
                self.OUTPUT_LAYERS,
                self.tr("Cable lay layers"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        gpkg_path = self.parameterAsFileOutput(parameters, self.GEOPACKAGE, context)

        created = []
        loaded_uris = []
        for layer_type, (wkb_type, specs) in clp.CANONICAL_SCHEMAS.items():
            layer_name = clp.prefixed_layer_name(gpkg_path, layer_type)
            uri = clp.gpkg_layer_uri(gpkg_path, layer_name)
            loaded_uris.append(uri)
            if clp.open_gpkg_layer(gpkg_path, layer_name) is not None:
                feedback.pushInfo(
                    self.tr("Layer '{layer}' already exists - left unchanged.").format(
                        layer=layer_name
                    )
                )
                continue
            fields = clp.fields_from_specs(specs)
            try:
                clp.write_layer_to_gpkg(
                    gpkg_path, layer_name, fields, wkb_type, [], context.transformContext()
                )
            except RuntimeError as exc:
                raise QgsProcessingException(str(exc))
            created.append(layer_name)
            if context.project() is not None:
                details = QgsProcessingContext.LayerDetails(layer_name, context.project(), layer_name)
                context.addLayerToLoadOnCompletion(uri, details)

        if created:
            feedback.pushInfo(self.tr("Created layer(s): {names}.").format(names=", ".join(created)))
        else:
            feedback.pushInfo(self.tr("All cable-lay layers were already present."))

        return {self.GEOPACKAGE: gpkg_path, self.OUTPUT_LAYERS: loaded_uris}
