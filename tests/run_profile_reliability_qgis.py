"""Headless QGIS regression checks for bathymetry and frozen profiles."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
from pathlib import Path
from run_qgis_smoke_tests import _init_qgis, _register_plugin_package
_init_qgis(); _register_plugin_package()
import numpy as np
from osgeo import gdal, osr
gdal.UseExceptions()
from qgis.core import QgsRasterLayer, QgsPointXY, QgsCoordinateReferenceSystem, QgsProject, QgsGeometry, QgsCoordinateTransform
from subsea_cable_tools.bathymetry_sampling import RasterSampler, PREFIX
from subsea_cable_tools.maptools.kp_depth_utils import DepthSampler
from subsea_cable_tools.maptools.kp_depth_profile_window import KPDepthProfileWindow
from subsea_cable_tools.kp_range_utils import make_distance_area
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import Qgis
print('Runtime:', Qgis.QGIS_VERSION)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp:
    from qgis.PyQt.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat,QSettings.Scope.UserScope,temp)
    path = str(Path(temp)/'plane.tif')
    ds=gdal.GetDriverByName('GTiff').Create(path,30,30,1,gdal.GDT_Float32)
    ds.SetGeoTransform((500000,10,0,6000000,0,-10))
    crs=osr.SpatialReference(); crs.ImportFromEPSG(32630); ds.SetProjection(crs.ExportToWkt())
    array=np.array([[100 + c + r*.5 for c in range(30)] for r in range(30)],dtype=np.float32)
    ds.GetRasterBand(1).WriteArray(array); ds.GetRasterBand(1).SetNoDataValue(-9999); ds=None
    layer=QgsRasterLayer(path,'plane'); assert layer.isValid()
    layer.setCustomProperty(PREFIX+'vertical','depth')
    sampler=RasterSampler(layer)
    assert abs(sampler.sample(QgsPointXY(500100,5999900))-114.25)<1e-6
    assert sampler.sample(QgsPointXY(500000,6000000)) is None
    depth=DepthSampler(layer.crs(),[(layer,'')])
    da=make_distance_area(layer.crs(),QgsProject.instance().transformContext())
    profile=depth.profile(QgsPointXY(500050,5999900),QgsPointXY(500240,5999900),da)
    assert profile['rasters'] and profile['rasters'][0]['raw_y']
    # A legacy nearest-neighbour upsample carries 1 m pixels but 10 m steps.
    legacy_path = str(Path(temp)/'legacy_upsampled.tif')
    legacy_ds = gdal.Translate(legacy_path,path,width=300,height=300,resampleAlg='nearest'); legacy_ds=None
    legacy_layer = QgsRasterLayer(legacy_path,'legacy')
    legacy_depth = DepthSampler(layer.crs(),[(legacy_layer,'')])
    legacy_profile = legacy_depth.profile(QgsPointXY(500050,5999900),QgsPointXY(500240,5999900),da)
    from subsea_cable_tools.maptools.kp_profile_math import profile_slope_series
    _,legacy_slopes,_ = profile_slope_series(legacy_profile,positive_down=True)
    assert legacy_profile['terrace_baseline_m'] > 15
    assert max(abs(v) for v in legacy_slopes if v is not None) < 8
    print('[PASS] actual GDAL nearest-upsampled raster uses terrace-scale averaging')
    from subsea_cable_tools.burial.analysis_task import DepthSnapshot, ProfileSamplingTask
    from subsea_cable_tools.workbench.depth_service import DepthSourceConfig
    from subsea_cable_tools.kp_geo_utils import RouteFrame
    from subsea_cable_tools.processing.kp_range_depth_slope_summary_algorithm import KPRangeDepthSlopeSummaryAlgorithm
    from subsea_cable_tools.depth_profile_dockwidget import DepthProfileDockWidget
    from qgis.core import QgsProcessingContext
    project = QgsProject.instance(); project.addMapLayer(layer)
    wgs = QgsCoordinateReferenceSystem('EPSG:4326')
    transform = QgsCoordinateTransform(layer.crs(), wgs, project)
    p0, p1 = transform.transform(QgsPointXY(500050,5999900)), transform.transform(QgsPointXY(500240,5999900))
    wgs_da = make_distance_area(wgs,project.transformContext())
    route = RouteFrame.from_source([QgsGeometry.fromPolylineXY([p0,p1])], wgs_da)
    snapshot=DepthSnapshot(DepthSourceConfig({'mode':1,'raster_layer_ids':[layer.id()]}),project)
    task=ProfileSamplingTask(route,snapshot,0,route.total_length_km,10,lambda t: None,distance=wgs_da,cross_offset_m=20)
    assert task.run(), task.error
    assert task.source_ids and task.cell_sizes_m and task.cross_max_deg
    assert all(v is None or abs(v-10)<.01 for v in task.cell_sizes_m)
    assert any(v is not None for v in task.port_depths)
    context=QgsProcessingContext(); context.setProject(project)
    sources=KPRangeDepthSlopeSummaryAlgorithm._prepare_raster_sources(layer.crs(),[layer],context)
    assert abs(KPRangeDepthSlopeSummaryAlgorithm._sample_rasters_at_point(QgsPointXY(500100,5999900),sources)-114.25)<1e-6
    from types import SimpleNamespace
    from qgis.PyQt.QtWidgets import QMainWindow
    from qgis.gui import QgsMapCanvas, QgsMessageBar
    root=QMainWindow(); canvas=QgsMapCanvas(); bar=QgsMessageBar()
    iface=SimpleNamespace(mainWindow=lambda:root,mapCanvas=lambda:canvas,messageBar=lambda:bar)
    dock=DepthProfileDockWidget(iface)
    dock.current_line_crs=layer.crs(); dock.distance_area=da
    dock.line_parts=[[QgsPointXY(500050,5999900),QgsPointXY(500240,5999900)]]
    dock.line_length=da.measureLine(*dock.line_parts[0])
    dock.kp_values=[i/1000 for i in range(0,181,10)]
    dock.depth_values=[109.25+i*.1 for i in range(0,181,10)]
    dock.depth_source_ids=['a']*10+['b']*9; dock.depth_cell_m=[10]*19
    dock._build_route_stationing_cache()
    dock.slope_window_spin.setValue(0)
    dock._compute_slopes()
    assert len(dock.slope_deg)==len(dock.kp_values) and dock.slope_deg[10] is None
    dock._compute_seabed_length(False)
    assert abs(dock.seabed_covered_m-170)<1e-6
    dock.depth_source_ids=['a']*19
    dock._get_selected_raster_layers=lambda:[layer]
    dock.side_slope_search_spin.setValue(20)
    dock._compute_side_slopes_with_progress()
    assert any(v is not None for v in dock.side_slope_deg)
    assert any(v is not None for v in dock.side_local_max_deg)
    dock._compute_slopes()
    from qgis.PyQt.QtWidgets import QFileDialog
    saved_dialog=QFileDialog.getSaveFileName
    try:
        QFileDialog.getSaveFileName=lambda *a,**k:(str(Path(temp)/'depth.csv'),'')
        dock.export_csv()
    finally:
        QFileDialog.getSaveFileName=saved_dialog
    assert 'SlopeBaseline' in (Path(temp)/'depth.csv').read_text()
    dock.close(); dock.deleteLater(); root.close(); canvas.close()
    print('[PASS] Depth Profile station alignment, covered seabed length, transverse metrics and CSV')
    print('[PASS] Burial sampling/provenance/transverse profiles and Processing use the same native sampler')
    # Contours with genuine transverse brackets recover a known plane.
    from qgis.core import QgsVectorLayer, QgsFeature
    contours=QgsVectorLayer('LineString?crs=EPSG:32630&field=depth:double','contours','memory')
    features=[]
    for off in range(-60,61,10):
        feature=QgsFeature(contours.fields())
        feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(499900,5999900+off),QgsPointXY(500400,5999900+off)]))
        feature.setAttributes([100-off*.1]); features.append(feature)
    contours.dataProvider().addFeatures(features); contours.updateExtents()
    contours.setCustomProperty(PREFIX+'vertical','depth'); project.addMapLayer(contours)
    cs=DepthSnapshot(DepthSourceConfig({'mode':2,'contour_layers':[{'layer_id':contours.id(),'depth_field':'depth'}]}),project)
    cs.prepare()
    port,starboard=cs.offset_profile_samples(route,[.05,.1,.15],20,wgs_da)
    assert all(p is not None and q is not None and q>p for p,q in zip(port,starboard))
    assert all(v is not None and 5.5<v<6 for v in cs.cross_max_deg)
    project.removeMapLayer(contours.id())
    print('[PASS] Exact transverse contour brackets recover the analytical cross slope')
    # A new mosaic's companion resolves the original file/native resolution.
    import json
    from subsea_cable_tools.bathymetry_sampling import expand_rasters
    mosaic_path=str(Path(temp)/'mosaic.tif')
    copied=gdal.Translate(mosaic_path,path); copied=None
    Path(mosaic_path+'.sources.json').write_text(json.dumps({'sources':[{'path':path,'name':'native','options':{'vertical':'depth'}}]}))
    mosaic=QgsRasterLayer(mosaic_path,'mosaic')
    native=expand_rasters([mosaic])
    assert len(native)==1 and native[0].source()==path and RasterSampler(native[0]).cell_m==10
    mosaic.setCustomProperty(PREFIX+'native_cell_m',25)
    assert RasterSampler(expand_rasters([mosaic])[0]).cell_m==25
    print('[PASS] Mosaic provenance resolves native sources and explicit resolution override')
    frame = RouteFrame.from_source([QgsGeometry.fromPolylineXY([QgsPointXY(500000,5999900),QgsPointXY(500300,5999900)])],da)
    window=KPDepthProfileWindow(); window.configure(depth,da,frame); window.show(); QApplication.processEvents()
    window.schedule(QgsPointXY(500050,5999900),QgsPointXY(500240,5999900)); window.set_frozen(True)
    assert window._profile is not None and window.frozen
    before=window._profile
    window.schedule(QgsPointXY(500050,5999900),QgsPointXY(500150,5999900))
    assert window._pending is None and window._profile is before
    assert any(v is not None for v in window._slopes)
    from qgis.PyQt.QtCore import Qt
    class Click:
        def __init__(self,x,z): self.p=window.depth_item.vb.mapViewToScene(__import__('qgis.PyQt.QtCore',fromlist=['QPointF']).QPointF(x,z))
        def scenePos(self): return self.p
        def button(self): return Qt.MouseButton.LeftButton
        def accept(self): pass
    assert window.measure_action in window.depth_item.vb.getMenu(None).actions()
    window.measure_action.trigger()
    assert window.measure_action.isChecked()
    assert all(window.depth_item.vb.state['mouseEnabled'])
    window._plot_click(Click(50,115))
    window._plot_move(Click(100,120).scenePos())
    assert window._preview is not None and window._preview[0].isVisible()
    assert len(window._preview) == 5
    assert all(abs(v-e)<1e-6 for v,e in zip(window._preview[1].getData()[0],[50,100,100]))
    leg_y = window._preview[1].getData()[1]
    assert abs(leg_y[0]-leg_y[1])<1e-6
    assert len(window._measurements) == 0
    assert abs(window._preview[0].getData()[0][1]-100)<1e-6
    window._plot_click(Click(100,120))
    assert window._preview is None and window.table.columnCount() == 4
    window.kp_check.setChecked(True)
    kp = window._kp_at_distance(50)
    assert kp is not None and .099 < kp < .101
    labels = window.slope_item.getAxis('bottom').tickStrings([50],1,10)
    assert labels == ['%.4f' % kp]
    assert window._kp_at_distance(-1) is None
    assert len(window._measurements)==1
    assert abs(window._measurements[0]['metrics']['width_m']-50)<1e-6
    assert window.table.item(0,3).text().endswith('°')
    assert window.scale_check.isChecked()
    assert abs(window.depth_item.vb.getAspectRatio()-1)<.001
    window.x_units.setCurrentText('ft')
    QApplication.processEvents()
    assert abs(window.depth_item.vb.getAspectRatio()-.3048)<.001
    window.z_units.setCurrentText('ft')
    QApplication.processEvents()
    assert abs(window.depth_item.vb.getAspectRatio()-1)<.001
    triangle,handles = window._measurement_graphics[0]
    handles[1].setPos(120/.3048,0)
    moved = window._measurements[0]
    assert abs(moved['b'][0]-120)<1e-6 and moved['b'][1]>100
    assert abs(moved['metrics']['width_m']-70)<1e-6
    assert abs(triangle[0].getData()[0][1]-120/.3048)<1e-6
    old = moved['b']
    handles[1].setPos(-10000,0)
    assert moved['b']==old
    window.scale_check.setChecked(False)
    assert window.depth_item.vb.state['aspectLocked'] is False
    window.scale_check.setChecked(True)
    assert abs(window.depth_item.vb.getAspectRatio()-1)<.001
    from qgis.PyQt.QtTest import QTest
    from qgis.PyQt.QtCore import QPoint, QPointF, QEvent
    from qgis.PyQt.QtGui import QMouseEvent
    QApplication.processEvents()
    handle = window._measurement_graphics[0][1][1]
    viewport = window.depth_widget.viewport()
    start = window.depth_widget.mapFromScene(handle.mapToScene(QPointF(0,0)))
    end = start+QPoint(20,5)
    original = window._measurements[0]['b']
    original_a = window._measurements[0]['a']
    QTest.mousePress(viewport,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,start)
    move = QMouseEvent(QEvent.Type.MouseMove,QPointF(end),QPointF(viewport.mapToGlobal(end)),
                       Qt.MouseButton.NoButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport,move)
    QTest.mouseRelease(viewport,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,end)
    QApplication.processEvents()
    assert window._measurements[0]['b'] != original
    assert window._measurements[0]['a'] == original_a
    assert window._first_point is None and len(window._measurements)==1
    print('[PASS] actual endpoint drag, snapped editing, gap rejection and physical 1:1 across mixed units')
    assert window.plot_container.grab().save(str(Path(temp)/'profile.png'))
    assert len(window._measurements)==1 and 'ft' in window.table.item(0,1).text()
    QApplication.processEvents()
    if os.environ.get('PROFILE_TEST_PNG'):
        window.plot_container.grab().save(os.environ['PROFILE_TEST_PNG'])
    window._delete_measurement(); assert not window._measurements
    window._plot_click(Click(50/.3048,115/.3048))
    window._plot_move(Click(100/.3048,120/.3048).scenePos())
    assert window._preview is not None
    window.snap_check.setChecked(False)
    assert window._first_point is None and window._preview is None
    # Real viewport gestures while measuring: a drag pans without placing a point.
    from qgis.PyQt.QtTest import QTest
    from qgis.PyQt.QtCore import QPoint, QPointF, QEvent
    from qgis.PyQt.QtGui import QWheelEvent, QMouseEvent
    viewport = window.depth_widget.viewport()
    center = window.depth_widget.mapFromScene(window.depth_item.vb.sceneBoundingRect().center())
    before_range = window.depth_item.vb.viewRange()[0][:]
    QTest.mousePress(viewport,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,center)
    # QTest.mouseMove does not preserve pressed buttons on the offscreen platform.
    moved = center+QPoint(45,15)
    move = QMouseEvent(QEvent.Type.MouseMove,QPointF(moved),QPointF(viewport.mapToGlobal(moved)),
                       Qt.MouseButton.NoButton,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(viewport,move)
    QApplication.processEvents()
    QTest.mouseRelease(viewport,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier,center+QPoint(45,15))
    QApplication.processEvents()
    assert window._first_point is None and not window._measurements
    assert window.depth_item.vb.viewRange()[0] != before_range
    before_width = window.depth_item.vb.viewRange()[0][1]-window.depth_item.vb.viewRange()[0][0]
    wheel = QWheelEvent(QPointF(center),QPointF(viewport.mapToGlobal(center)),QPoint(),QPoint(0,120),
                        Qt.MouseButton.NoButton,Qt.KeyboardModifier.NoModifier,Qt.ScrollPhase.NoScrollPhase,False)
    QApplication.sendEvent(viewport,wheel); QApplication.processEvents()
    after_range = window.depth_item.vb.viewRange()[0]
    assert after_range[1]-after_range[0] < before_width
    assert window._first_point is None and not window._measurements
    window.measure_action.trigger()
    assert not window.measure_action.isChecked() and all(window.depth_item.vb.state['mouseEnabled'])
    print('[PASS] context-menu measurement keeps viewport drag pan and wheel zoom without placing endpoints')
    window.close(); QApplication.processEvents(); assert window.user_closed and not window._timer.isActive()
    window.cleanup(); QApplication.processEvents()
    project.removeMapLayer(layer.id())
    del window, depth, sampler, layer, sources, task, snapshot
    import gc
    gc.collect()
    print('[PASS] QGIS bilinear plane, no extrapolation, quick profile freeze/units/PNG/close')
print('Profile reliability QGIS checks passed')

# Existing integration suites exercise changes to rule acquisition and MBES IO.
import importlib, sys
for name in sys.argv[1:]:
    module=importlib.import_module('subsea_cable_tools.tests.'+name)
    assert all(module.run_all()), name
