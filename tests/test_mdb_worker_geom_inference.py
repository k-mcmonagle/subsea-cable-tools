from processing.mdb_odbc_worker import _infer_geom_type


def test_single_vertex_stays_point_even_with_line_metadata():
    vertices = [(100.0, 200.0, -12.0)]
    assert _infer_geom_type(vertices, geometry_type_code=1) == "Point"


def test_multi_vertex_point_metadata_is_reclassified_as_line():
    vertices = [(0.0, 0.0, -1.0), (1.0, 1.0, -1.5), (2.0, 1.5, -2.0)]
    assert _infer_geom_type(vertices, geometry_type_code=3) == "LineString"


def test_polygon_metadata_is_preserved_for_multi_vertex_shapes():
    vertices = [(0.0, 0.0, -1.0), (1.0, 0.0, -1.0), (1.0, 1.0, -1.0), (0.0, 0.0, -1.0)]
    assert _infer_geom_type(vertices, geometry_type_code=2) == "Polygon"


def test_ambiguous_code_defaults_multi_vertex_to_line():
    vertices = [(0.0, 0.0, -1.0), (1.0, 0.0, -1.0), (1.0, 1.0, -1.0), (0.0, 0.0, -1.0)]
    assert _infer_geom_type(vertices, geometry_type_code=10) == "LineString"
