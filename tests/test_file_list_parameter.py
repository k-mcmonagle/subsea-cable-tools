"""Processing history must not open file inputs through a native provider."""

import ast
import json
from pathlib import Path
import tempfile

from qgis.core import QgsProcessingContext, QgsProperty, QgsVectorLayer

from ..processing.file_list_parameter import FileListParameter
from ..processing.import_mdb_algorithm import ImportMdbAlgorithm
from ..processing.subsea_cable_processing_provider import SubseaCableProcessingProvider
from ..qgis_compat import PROCESSING_SOURCE_FILE


def test_mdb_dialog_serialization_never_opens_inputs():
    # A harmless provider-readable file with an MDB suffix exposes the native
    # serializer's unwanted provider loading without needing an Access driver.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "café survey's.mdb"
        path.write_text(json.dumps({'type': 'FeatureCollection', 'features': [{
            'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [0, 0]},
            'properties': {'label': 'harmless fixture'},
        }]}), encoding='utf-8')
        fixture = QgsVectorLayer(str(path), 'fixture', 'ogr')
        assert fixture.isValid()  # otherwise an empty store would prove nothing
        fixture = None
        algorithm = ImportMdbAlgorithm()
        algorithm.initAlgorithm()
        context = QgsProcessingContext()
        opened = []
        context.temporaryLayerStore().layersAdded.connect(lambda layers: opened.extend(layers))
        parameter = algorithm.parameterDefinition(algorithm.INPUT_MDB)
        values = [str(path)]
        params = {algorithm.INPUT_MDB: values}
        try:
            assert algorithm.checkParameterValues(params, context)[0]
            assert algorithm.validateInputCrs(params, context)
            assert parameter.valueAsJsonObject(values, context) == values
            assert parameter.valueAsString(values[0], context) == (values[0], True)
            assert parameter.valueAsStringList(values, context) == (values, True)
            assert ast.literal_eval(parameter.valueAsPythonString(values, context)) == values
            assert algorithm.asMap(params, context)['inputs'][algorithm.INPUT_MDB] == values
            assert algorithm.asPythonCommand(params, context)
            assert algorithm.asQgisProcessCommand(params, context)[1]
            assert not opened, 'Parameter serialization opened a raw input as a map layer'
            assert not context.temporaryLayerStore().mapLayers()
        finally:
            opened.clear()
            context.temporaryLayerStore().removeAllMapLayers()


def test_cloned_file_parameter_preserves_behavior_and_options():
    parameter = FileListParameter('FILES', 'Files', defaultValue=['a.mdb'], optional=True)
    parameter.setMinimumNumberInputs(2)
    parameter.setMetadata({'widget_wrapper': {'file_filter': 'Access (*.mdb)'}})
    clone = parameter.clone()
    assert isinstance(clone, FileListParameter)
    assert clone.toVariantMap() == parameter.toVariantMap()
    context = QgsProcessingContext()
    assert clone.valueAsJsonObject(QgsProperty.fromValue(['a.mdb']), context) == ['a.mdb']
    assert clone.valueAsJsonObject(None, context) is None
    assert clone.valueAsStringList(['a.mdb', 'b.mdb'], context) == (['a.mdb', 'b.mdb'], True)


def test_all_plugin_multi_file_inputs_use_safe_serialization():
    provider = SubseaCableProcessingProvider()
    provider.loadAlgorithms()
    found = []
    for algorithm in provider.algorithms():
        for parameter in algorithm.parameterDefinitions():
            if (parameter.type() == 'multilayer'
                    and parameter.layerType() == PROCESSING_SOURCE_FILE):
                found.append(algorithm.name())
                assert isinstance(parameter, FileListParameter), algorithm.name()
    assert len(found) >= 4
