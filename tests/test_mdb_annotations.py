"""Regression cases for annotation text and field preservation (no MDB needed)."""

import json
import struct

from ..processing import geomedia_blob, mdb_odbc_worker as worker


def text_blob(x, y, text):
    payload = text.encode('cp1252')
    return (bytes([geomedia_blob.GEOMEDIA_TEXT]) + bytes.fromhex('ffd20fbc8ccf11abde08003601b769')
            + struct.pack('<ddd', x, y, 0) + struct.pack('<d', 0)
            + struct.pack('<ddd', 0, 0, 1) + bytes([0, 0, 1, 9])
            + struct.pack('<i', len(payload)) + payload)


def _export(tmp_path, columns, rows, geometry_field, geometry_type):
    return worker._write_rows_to_geojson(
        'synthetic.mdb', 'Synthetic_Table', columns, rows,
        geometry_field, geometry_type, str(tmp_path / 'annotations'), split=True)


def _features(result, kind):
    with open(result['outputs'][kind], encoding='utf-8') as handle:
        return json.load(handle)['features']


def test_rtf_surrogate_pair_exports_as_utf8(tmp_path):
    rtf = r'{\rtf1 marker \u-10179?\u-8704?}'
    result = _export(tmp_path, ['Geometry'], [(text_blob(1, 2, rtf),)], 'Geometry', 33)
    props = _features(result, 'Point')[0]['properties']
    assert props['label_text'] == 'marker 😀'
    assert props['label_rtf'] == rtf


def test_rtf_lone_surrogate_and_escaped_nul_are_safe():
    assert geomedia_blob.rtf_to_text(r'{\rtf1 a\u-10179?b}') == 'a�b'
    assert geomedia_blob.rtf_to_text(r'{\rtf1 a\u0?b}') == 'ab'


def test_rtf_escaped_fallback_is_not_duplicated():
    assert geomedia_blob.rtf_to_text(r'{\rtf1 a\u123\{b}') == 'a{b'
    assert geomedia_blob.rtf_to_text(r'{\rtf1 a\u92\\b}') == 'a\\b'


def test_label_field_names_are_case_insensitive_and_never_overwrite(tmp_path):
    columns = ['LABEL_TEXT', 'gm_label_text', 'gm_label_text_2', 'Geometry']
    rows = [('original', 'also original', 'third', text_blob(1, 2, 'decoded')),
            (None, None, None, text_blob(3, 4, 'second'))]
    result = _export(tmp_path, columns, rows, 'Geometry', 33)
    first, second = [f['properties'] for f in _features(result, 'Point')]
    assert first['LABEL_TEXT'] == 'original'
    assert first['gm_label_text'] == 'also original'
    assert first['gm_label_text_2'] == 'third'
    assert first['gm_label_text_3'] == 'decoded'
    assert second['gm_label_text_3'] == 'second'
    assert second['gm_label_text'] is None


def test_schema_discovery_keeps_spatial_annotation_tables(monkeypatch):
    inventory = [
        {'table': 'Contacts_Text', 'columns': ['Id', 'TextGeometry'], 'row_count': 2},
        {'table': 'Contacts_Name', 'columns': ['Id', 'GraphicText'], 'row_count': 1},
        {'table': 'Contacts_Label', 'columns': ['Id', 'TextGeometry_sk'], 'row_count': 1},
    ]
    monkeypatch.setattr(worker, 'inventory_tables', lambda *a: ({}, {}, inventory))
    result = worker.discover_tables('unused.mdb', include_schema_discovery=True)
    assert set(result['tables']) == {'Contacts_Text', 'Contacts_Name'}
    assert [t['table'] for t in result['non_spatial']] == ['Contacts_Label']
