"""Burial Planner results computed before the KP path change read out of date where it matters.

* Exclusion analyses, the bathymetry profile and the plough/tool path carry a
  KP path marker only on routes whose KP path (the geodesic) is more than
  1 m from the line as stored somewhere; results without it are then out of
  date. Routes with short legs keep their stored fingerprints unchanged.
* Risk checks carry the KP method version: scans recorded before read out of
  date on every route (hazard KPs beside the route changed everywhere).
"""
from types import SimpleNamespace

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransformContext, QgsGeometry, QgsPointXY

from ..burial import analysis_state, path_data
from ..burial.plan_model import PlanModel
from ..burial.profile_data import PlanProfile
from ..kp_geo_utils import RouteFrame
from ..kp_range_utils import make_distance_area

REQUIRES_QGIS = True

_PARAMS = SimpleNamespace(scope=SimpleNamespace(start_km=0.0, end_km=10.0), direction=1, method="plough",
                          coarse_step_m=10.0, sliver_tol_km=0.001, refine_tol_m=0.5)


def _route(points):
    distance = make_distance_area(QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateTransformContext())
    return RouteFrame.from_source([QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in points])], distance), distance


def test_route_departure_and_plan_marker():
    # One 20 km east-west leg at 57.5 N: the geodesic is ~14 m from the line straight in degrees.
    long_route, distance = _route([(0.0, 57.5), (0.3335, 57.5)])
    short_route, _ = _route([(0.0, 57.5 + 0.005 * i) if i % 2 else (0.01, 57.5 + 0.005 * i) for i in range(30)])
    assert 10.0 < long_route.max_path_departure_m() < 20.0
    assert short_route.max_path_departure_m() < 0.1
    long_model = SimpleNamespace(route=long_route, distance=distance, _kp_path_fp=None,
                                 KP_PATH_STALE_M=PlanModel.KP_PATH_STALE_M)
    short_model = SimpleNamespace(route=short_route, distance=distance, _kp_path_fp=None,
                                  KP_PATH_STALE_M=PlanModel.KP_PATH_STALE_M)
    assert PlanModel.kp_path_component(long_model) == "kp-path:geodesic"
    assert PlanModel.kp_path_component(short_model) == ""


def test_global_fingerprint_and_compare():
    plain = analysis_state.global_fingerprint(_PARAMS, "geom:abc")
    marked = analysis_state.global_fingerprint(_PARAMS, "geom:abc", "kp-path:geodesic")
    assert "kp_path" not in plain                       # short legs: digest as before
    assert marked["kp_path"] == "kp-path:geodesic"
    # A run stored before the change, on a route where the KP path departs: out of date.
    assert analysis_state.compare_global(plain, marked) == [analysis_state.KP_PATH_LABEL]
    # Stored after the change, or a route with short legs: current.
    assert analysis_state.compare_global(marked, marked) == []
    assert analysis_state.compare_global(plain, plain) == []


def test_profile_kp_path_reason_and_round_trip():
    profile = PlanProfile(kps=[0.0, 1.0], depths=[10.0, 11.0], scope_start_kp=0.0, scope_end_kp=1.0,
                          route_geom_fingerprint="geom:a", depth_fingerprint="d")
    kwargs = dict(route_geom_fingerprint="geom:a", depth_fingerprint="d", scope_start_kp=0.0, scope_end_kp=1.0)
    stale = profile.stale_reasons(kp_path="kp-path:geodesic", **kwargs)
    assert any("KP path" in reason for reason in stale)
    assert not any("KP path" in reason for reason in profile.stale_reasons(**kwargs))
    profile.kp_path = "kp-path:geodesic"
    assert not any("KP path" in r for r in profile.stale_reasons(kp_path="kp-path:geodesic", **kwargs))
    again = PlanProfile.from_row(profile.to_row("plan-1"))
    assert again.kp_path == "kp-path:geodesic"


def test_path_fingerprint_changes_only_with_kp_path():
    plan = {"scope_start_kp": 0.0, "scope_end_kp": 10.0, "direction": 1}
    before = path_data.build_fingerprints(plan, "rpl", None, None, {})
    assert path_data.build_fingerprints(plan, "rpl", None, None, {}, kp_path="") == before
    assert path_data.build_fingerprints(plan, "rpl", None, None, {}, kp_path="kp-path:geodesic")["tool"] \
        != before["tool"]


def test_risk_check_fingerprint_carries_kp_method():
    check = {"config_json": "{}", "enabled": 1}
    old_payload = {"config_json": {}, "enabled": 1, "inputs": {}}
    assert analysis_state.check_fingerprint(check, {}) != analysis_state.digest(old_payload)
    assert analysis_state.check_fingerprint(check, {}) == analysis_state.digest(
        dict(old_payload, kp_method=analysis_state.RISK_KP_METHOD))
