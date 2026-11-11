"""Exercise the dialog setup, background task, conversion and layer handoff."""

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile

from qgis.PyQt.QtCore import QEventLoop, QSize, QThread, QTimer
from qgis.core import (
    QgsApplication, QgsMapRendererSequentialJob, QgsMapSettings,
    QgsProcessingAlgRunnerTask, QgsProcessingContext, QgsProcessingFeedback,
    QgsProject,
)

from ..processing.import_mdb_algorithm import ImportMdbAlgorithm


class _Feedback(QgsProcessingFeedback):
    def __init__(self):
        super().__init__()
        self.errors = []

    def reportError(self, message, fatalError=False):
        self.errors.append(message)
        super().reportError(message, fatalError)


class _SyntheticImport(ImportMdbAlgorithm):
    """Replace only the MDB reader; keep real native conversion and loading."""

    def __init__(self, feature_count=12, label_size=64):
        super().__init__()
        self.feature_count = feature_count
        self.label_size = label_size

    def createInstance(self):
        return _SyntheticImport(self.feature_count, self.label_size)

    def _run_worker(self, args, feedback, **kwargs):
        if args[:2] == ['--mode', 'list']:
            return {'tables': {'Labels': {'geom_field_name': 'Geometry', 'geometry_type_code': 33}}}
        if args[:2] == ['--mode', 'export']:
            path = args[args.index('--out') + 1] + '.geojson'
            with open(path, 'w', encoding='utf-8') as handle:
                handle.write('{"type":"FeatureCollection","features":[')
                for i in range(self.feature_count):
                    if i:
                        handle.write(',')
                    json.dump({'type': 'Feature',
                               'geometry': {'type': 'Point', 'coordinates': [500000 + i, 5600000, -12]},
                               'properties': {'label_text': 'Café 12°\nmarker 😀 ' + 'x' * self.label_size,
                                              'label_rotation': 35.0, 'label_alignment': 9}},
                              handle, ensure_ascii=False)
                handle.write(']}')
            print(f'Synthetic GeoJSON: {os.path.getsize(path) / 1024**2:.1f} MiB', flush=True)
            return {'outputs': {'Point': path}, 'feature_counts': {'Point': self.feature_count}}
        assert QThread.currentThread() != QgsApplication.instance().thread()
        return super()._run_worker(args, feedback, **kwargs)


def _exercise_import(feature_count=12, label_size=64):
    # Reproduce a desktop session: QGIS is available in this process's sys.path
    # but the shell has not supplied PYTHONPATH to child interpreters.
    previous = os.environ.pop('PYTHONPATH', None)
    project = QgsProject()
    context = QgsProcessingContext()
    context.setProject(project)
    feedback = _Feedback()
    errors = feedback.errors
    original_path = list(sys.path)
    # The standalone runner starts in the plugin repository, whose processing/
    # directory can shadow QGIS's own Processing plugin. Load the latter just
    # as the desktop does, and restore the runner's module namespace afterwards.
    saved_processing = {key: sys.modules.pop(key) for key in list(sys.modules)
                        if key == 'processing' or key.startswith('processing.')}
    try:
        with tempfile.TemporaryDirectory() as folder:
            mdb = Path(folder) / 'synthetic.mdb'
            mdb.touch()
            algorithm = _SyntheticImport(feature_count, label_size)
            algorithm.initAlgorithm()
            params = {algorithm.INPUT_MDB: [str(mdb)], algorithm.SOURCE_CRS: 'EPSG:32631'}
            assert algorithm.asMap(params, context)['inputs'][algorithm.INPUT_MDB] == [str(mdb)]
            assert algorithm.asQgisProcessCommand(params, context)[1]
            assert not context.temporaryLayerStore().mapLayers()

            loop = QEventLoop()
            outcome = []
            task = QgsProcessingAlgRunnerTask(algorithm, params, context, feedback)
            assert not task.isCanceled(), errors

            def complete(ok, results):
                outcome.append((ok, results))
                loop.quit()

            task.executed.connect(complete)
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(feedback.cancel)
            timer.start(90000)
            QgsApplication.taskManager().addTask(task)
            loop.exec()
            timer.stop()
            assert outcome and outcome[0][0], errors
            assert len(outcome[0][1][algorithm.OUTPUT_LAYERS]) == 1
            assert context.thread() == QgsApplication.instance().thread()

            plugin_path = os.path.join(QgsApplication.prefixPath(), 'python', 'plugins')
            sys.path.insert(0, plugin_path)
            postprocessing = importlib.import_module('processing.gui.Postprocessing')
            assert postprocessing.handleAlgorithmResults(algorithm, context, feedback, params), errors
            assert len(project.mapLayers()) == 1
            layer = next(iter(project.mapLayers().values()))
            assert layer.thread() == QgsApplication.instance().thread()
            assert layer.featureCount() == feature_count
            feature = next(layer.getFeatures())
            assert feature['label_text'] == 'Café 12°\nmarker 😀 ' + 'x' * label_size
            assert feature['label_rotation'] == 35.0
            assert feature['label_alignment'] == 9

            settings = QgsMapSettings()
            settings.setLayers([layer])
            settings.setDestinationCrs(layer.crs())
            extent = layer.extent()
            extent.grow(10)
            settings.setExtent(extent)
            settings.setOutputSize(QSize(320, 240))
            render = QgsMapRendererSequentialJob(settings)
            render.start()
            render.waitForFinished()
            assert not render.errors()
            assert not render.renderedImage().isNull()
    finally:
        project.removeAllMapLayers()
        context.temporaryLayerStore().removeAllMapLayers()
        sys.path[:] = original_path
        for key in list(sys.modules):
            if key == 'processing' or key.startswith('processing.'):
                del sys.modules[key]
        sys.modules.update(saved_processing)
        if previous is not None:
            os.environ['PYTHONPATH'] = previous


def test_background_import_and_render_without_pythonpath():
    _exercise_import()
