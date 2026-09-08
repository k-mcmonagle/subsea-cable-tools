"""Analytical regression cases for supported slopes and measurements."""
import math
import tempfile
import csv
from pathlib import Path
from ..slope_utils import (supported_slopes, windowed_slope_series, clean_crossings,
                           interpolate_covered, cross_profile_metrics)
from ..maptools.kp_profile_math import profile_slope_series
from ..maptools.profile_measurements import measurement, write_profile_csv
from ..burial.profile_data import PlanProfile, long_slope_series
from ..workbench.rules_engine import intervals_from_profile


def test_supported_planes():
    xs = [float(i) for i in range(101)]
    ys = [100+.1*x for x in xs]
    expected = -math.degrees(math.atan(.1))
    slopes,widths = supported_slopes(xs,ys,[10]*101,['a']*101)
    assert all(abs(v-expected)<1e-9 for v in slopes)
    assert set(widths)=={20}
    assert supported_slopes(xs,ys,[10]*101,['a']*101,window_m=5)[0]==[None]*101
    short={'rasters':[{'x':[0,.05,.1],'y':[100,100,101],'pixel_size_m':10}]}
    assert profile_slope_series(short)[1] == [None]*3
    print('[PASS] analytic plane, fixed edge baselines, unsupported footprint/short line')
    return True


def test_gaps_seams_and_invalids():
    xs=list(range(101)); ys=[100+.1*x for x in xs]
    ys[50]=None
    slopes,widths=supported_slopes(xs,ys,[5]*101,['a']*101)
    assert slopes[50] is None and all(v is None or abs(v+math.degrees(math.atan(.1)))<1e-8 for v in slopes)
    ys=[100+.1*x+(10 if x>50 else 0) for x in xs]
    slopes,_=supported_slopes(xs,ys,[5]*101,['a' if x<=50 else 'b' for x in xs])
    assert slopes[51] is None and max(abs(v) for v in slopes if v is not None)<6
    assert windowed_slope_series([0,10,20],[100,None,120],10)==[None]*3
    assert windowed_slope_series([0,10,20],[100,float('inf'),120],10)==[None]*3
    assert not intervals_from_profile([(0,10),(1,None),(2,10)],'>',5)
    print('[PASS] missing/nonfinite data and source seams cannot fabricate slopes/threshold intervals')
    return True


def test_contours_and_depression():
    pairs=clean_crossings([(0,100),(0,100),(1,101),(1,105),(2,102)])
    assert pairs==[(0,100),(1,None),(2,102)]
    assert interpolate_covered([x for x,y in pairs],[y for x,y in pairs],.5) is None
    xs=[-20,-10,0,10,20]; ys=[100,100,110,100,100]
    tilt,peak,port,stbd=cross_profile_metrics(xs,ys,20,True)
    assert tilt==0 and peak==45 and port==stbd==100
    assert cross_profile_metrics([0,10,20],[100,101,102],20,True)[0] is None
    assert cross_profile_metrics(xs,[100,100,None,100,100],20,True)[0] is None
    print('[PASS] conflicting contours, symmetric depression, one-sided and missing cross coverage')
    return True


def test_measurement_and_export():
    assert abs(measurement((0,0),(3,4))['angle_deg']-math.degrees(math.atan2(4,3)))<1e-10
    assert measurement((3,4),(0,0))['angle_deg']==measurement((0,0),(3,4))['angle_deg']
    assert measurement((0,0),(0,4))['angle_deg']==90
    assert measurement((0,0),(4,0))['angle_deg']==0
    assert measurement((0,0),(0,0))['angle_deg'] is None
    values=measurement((0,100),(10,110),[0,5,10],[100,115,110])
    assert values['width_m']==10 and values['height_m']==10
    assert abs(values['endpoint_distance_m']-math.sqrt(200))<1e-10
    assert values['seabed_distance_m']>values['endpoint_distance_m']
    assert measurement((0,100),(10,110),[0,5,10],[100,None,110])['seabed_distance_m'] is None
    with tempfile.TemporaryDirectory() as temp:
        path=Path(temp)/'profile.csv'
        write_profile_csv(path,{'slope_baseline_m':[None,10]},
            [{'name':'survey','x':[0,10],'y':[100,None]}],[0,10],[None,None],
            [{'source':'survey','a':(0,100),'b':(10,110),'metrics':values}])
        rows=list(csv.DictReader(path.open(encoding='utf-8-sig')))
        assert rows[1]['valid']=='0' and rows[1]['depth_positive_down_m']==''
        assert rows[-1]['width_m']=='10' and rows[-1]['record']=='measurement'
    print('[PASS] metric measurements, missing seabed distance, CSV units/validity/annotations')
    return True


def test_persistent_provenance():
    p=PlanProfile(kps=[0,.01,.02],depths=[100,101,102],source_ids=['a','b','b'],cell_sizes_m=[10,10,10],cross_max_deg=[4,5,6])
    q=PlanProfile.from_row(p.to_row('plan'))
    assert q.source_ids==p.source_ids and q.cell_sizes_m==p.cell_sizes_m and q.cross_max_deg==p.cross_max_deg
    assert all(v is None for x,v in q.slope_series(0,1)[2])
    assert q.depth_at(.005) is None
    assert all(v is None for x,v in long_slope_series(q.kps,q.depths,.01,q.source_ids,q.cell_sizes_m))
    print('[PASS] persisted source/resolution/peak metadata and safe interpolation')
    return True


def test_terraced_raster_baseline():
    from ..slope_utils import terrace_baseline_m
    xs = list(range(501)); ys = [100 + 20*(x//50) for x in xs]
    assert terrace_baseline_m(xs,ys,1) == 100
    slopes,widths = supported_slopes(xs,ys,[1]*len(xs))
    assert set(widths) == {100}
    assert max(abs(v) for v in slopes) < 22
    # An isolated real cliff and a continuous steep plane are not suppressed.
    cliff = [100 if x < 250 else 200 for x in xs]
    assert terrace_baseline_m(xs,cliff,1) == 0
    assert max(abs(v) for v in supported_slopes(xs,cliff,[1]*len(xs))[0]) > 80
    assert terrace_baseline_m(xs,[100+10*x for x in xs],1) == 0
    # Explicit engineering baselines remain deliberate; contours are unchanged.
    assert set(supported_slopes(xs,ys,[1]*len(xs),window_m=10)[1]) == {10}
    assert max(abs(v) for v in supported_slopes(xs,ys)[0] if v is not None) > 80
    short = {'rasters':[{'x':xs,'y':ys,'pixel_size_m':1}]}
    profile_slope_series(short)
    assert short['terrace_baseline_m'] == 100
    assert ys[49] == 100 and ys[50] == 120
    print('[PASS] repeated terrace averaging, explicit windows, isolated cliffs and contours')
    return True


def run_all():
    return [test_terraced_raster_baseline(),test_supported_planes(),test_gaps_seams_and_invalids(),test_contours_and_depression(),
            test_measurement_and_export(),test_persistent_provenance()]
