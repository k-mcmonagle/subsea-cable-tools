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
    QgsProcessingException,
    QgsProcessingOutputMultipleLayers,
    QgsProcessingParameterFileDestination,
    QgsVectorLayer,
)

from . import cable_lay_parsers as clp


class CreateCableLayGeoPackageAlgorithm(QgsProcessingAlgorithm):
    """Sets up a GeoPackage with the standard, empty cable-lay layers ready to fill."""

    GEOPACKAGE = "GEOPACKAGE"
    OUTPUT_LAYERS = "OUTPUT_LAYERS"

    # Display order for the created layers in the layer tree, matching the order
    # of the matching import tools in the Processing Toolbox. Any schema not
    # listed here is appended after these (keeps the algorithm future-proof).
    DISPLAY_ORDER = (
        "model_solutions",
        "as_laid",
        "body_logs",
        "cable_lay",
        "event_logs",
        "plough_data",
        "slack_logs",
        "qc_findings",
        "qc_config",
    )

    def __init__(self):
        super().__init__()
        self._group_name = ""
        self._ordered_layers = []

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
<p>Two QC layers are also created for use by the <b>Run Cable Lay QC</b> tool and
the <b>Cable Lay Data Explorer</b>:</p>
<ul>
  <li><code>ProjectX_qc_findings</code> (points - one per QC result)</li>
  <li><code>ProjectX_qc_config</code> (table - saved check settings)</li>
</ul>
<p>Run this once to set up a project GeoPackage, then point each of the Cable Lay
Data Import tools at it to fill the matching layer. This keeps every layer named
consistently and in one file.</p>
<p>When run from QGIS, the layers are added to the project inside a layer group
named after the GeoPackage (e.g. <code>ProjectX</code>) and ordered to match the
matching import tools in the Processing Toolbox.</p>
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

        schemas = {**clp.CANONICAL_SCHEMAS, **clp.QC_SCHEMAS, **clp.MANAGEMENT_SCHEMAS}
        # Build the ordered list of layer types: the toolbox-matching display
        # order first, then any remaining schemas (future-proofing).
        ordered_types = [t for t in self.DISPLAY_ORDER if t in schemas]
        ordered_types += [t for t in schemas if t not in ordered_types]

        created = []
        ordered_layers = []  # (layer_name, uri) in display order
        for layer_type in ordered_types:
            wkb_type, specs = schemas[layer_type]
            layer_name = clp.prefixed_layer_name(gpkg_path, layer_type)
            uri = clp.gpkg_layer_uri(gpkg_path, layer_name)
            ordered_layers.append((layer_name, uri))
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

        # Stash for postProcessAlgorithm, which runs on the main thread and can
        # safely add the layers to a group in the project layer tree.
        self._group_name = clp.gpkg_stem(gpkg_path)
        self._ordered_layers = ordered_layers

        if created:
            feedback.pushInfo(self.tr("Created layer(s): {names}.").format(names=", ".join(created)))
        else:
            feedback.pushInfo(self.tr("All cable-lay layers were already present."))

        return {
            self.GEOPACKAGE: gpkg_path,
            self.OUTPUT_LAYERS: [uri for _, uri in ordered_layers],
        }

    def postProcessAlgorithm(self, context, feedback):
        """Add the layers to the project inside a named group, in display order.

        Runs on the main thread, so it is safe to manipulate the layer tree. The
        layers are appended to the group in the order they were created, giving
        the toolbox-matching layer order requested. Re-running moves any existing
        layers into the group rather than duplicating them.
        """
        project = context.project()
        if project is None or not self._ordered_layers:
            return {}

        root = project.layerTreeRoot()
        group = root.findGroup(self._group_name) if self._group_name else None
        if group is None:
            group = root.insertGroup(0, self._group_name or "Cable Lay")

        for layer_name, uri in self._ordered_layers:
            layer = self._existing_project_layer(project, uri, layer_name)
            if layer is None:
                layer = QgsVectorLayer(uri, layer_name, "ogr")
                if not layer.isValid():
                    feedback.pushWarning(
                        self.tr("Could not load layer '{layer}'.").format(layer=layer_name)
                    )
                    continue
                project.addMapLayer(layer, False)

            # Append the layer's tree node to the group in iteration order. If it
            # already has a node somewhere in the tree, move it; otherwise add it.
            node = root.findLayer(layer.id())
            if node is not None:
                group.addChildNode(node.clone())
                parent = node.parent()
                if parent is not None:
                    parent.removeChildNode(node)
            else:
                group.addLayer(layer)

        return {}

    @staticmethod
    def _existing_project_layer(project, uri, layer_name):
        """Return a layer already in the project pointing at ``uri``, or None."""
        suffix = "layername=" + layer_name
        for layer in project.mapLayers().values():
            source = layer.source()
            if suffix in source:
                return layer
        return None
