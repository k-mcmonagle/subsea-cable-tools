"""File pickers which never ask a map provider to open their inputs."""

from qgis.core import QgsProcessingParameterDefinition, QgsProcessingParameterMultipleLayers

from ..qgis_compat import PROCESSING_SOURCE_FILE


class FileListParameter(QgsProcessingParameterMultipleLayers):
    """Keep the native multi-file widget without its layer-aware serializers.

    QGIS 3/4's MultipleLayers parameter honours TypeFile when validating and
    generating Python commands, but its JSON/string serializers still try
    parameterAsLayer(). The Processing dialog calls these for history and
    qgis_process commands BEFORE starting the algorithm. For MDB files this
    bypasses our worker isolation and can crash the host in an Access driver.
    """

    def __init__(self, name, description='', defaultValue=None, optional=False):
        super().__init__(name, description, PROCESSING_SOURCE_FILE, defaultValue, optional)

    def clone(self):
        parameter = FileListParameter(self.name())
        parameter.fromVariantMap(self.toVariantMap())
        return parameter

    def valueAsJsonObject(self, value, context):
        return QgsProcessingParameterDefinition.valueAsJsonObject(self, value, context)

    def valueAsString(self, value, context):
        return QgsProcessingParameterDefinition.valueAsString(self, value, context)

    def valueAsStringList(self, value, context):
        # The base implementation dispatches each element to valueAsString()
        # above, preserving QGIS's normal handling of properties/empty values.
        return QgsProcessingParameterDefinition.valueAsStringList(self, value, context)
