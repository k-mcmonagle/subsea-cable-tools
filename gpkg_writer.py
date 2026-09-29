# -*- coding: utf-8 -*-
"""Write vector layers into GeoPackage files.

Shared by the MDB import (one GeoPackage per database) and the
Save Layers to GeoPackage action.
"""

import os
import re
import sqlite3
from contextlib import closing

from qgis.core import Qgis, QgsField, QgsFields, QgsVectorFileWriter

from .plugin_log import log_exception


class RenamingFieldConverter(QgsVectorFileWriter.FieldValueConverter):
    """Renames fields on write; values pass through unchanged."""

    def __init__(self, renamed_fields):
        super().__init__()
        self._renamed_fields = renamed_fields

    def fieldDefinition(self, field):
        output_field = QgsField(field)
        output_field.setName(self._renamed_fields.get(field.name(), field.name()))
        return output_field

    def convert(self, field_index, value):
        return value


def resolve_gpkg_field_names(source_fields):
    """Return ``(renamed, reserved)`` for GeoPackage-safe field names.

    GeoPackage column names are case-insensitive, so a source ``Depth`` column
    and the derived ``depth`` attribute collide and the whole table fails to be
    created. First occurrences keep their name — source attributes are listed
    before derived ones — and later collisions are suffixed. ``fid`` is always
    renamed because it is the GeoPackage primary key.
    """
    reserved = set()
    deferred = []
    for field in source_fields:
        name = field.name()
        key = name.casefold()
        if key == "fid" or key in reserved:
            deferred.append(name)
            continue
        reserved.add(key)

    renamed = {}
    for name in deferred:
        base = "source_fid" if name.casefold() == "fid" else name
        candidate = base
        suffix = 2
        while candidate.casefold() in reserved:
            candidate = f"{base}_{suffix}"
            suffix += 1
        reserved.add(candidate.casefold())
        renamed[name] = candidate
    return renamed, reserved


def gpkg_table_name(name, taken=()):
    """A tidy table name for ``name``, unique (case-insensitively) among ``taken``."""
    cleaned = re.sub(r"[^\w-]+", "_", str(name)).strip("_-") or "layer"
    taken_keys = {str(t).casefold() for t in taken}
    candidate = cleaned
    suffix = 2
    while candidate.casefold() in taken_keys:
        candidate = f"{cleaned}_{suffix}"
        suffix += 1
    return candidate


def gpkg_table_names(gpkg_path):
    """Table names already registered in ``gpkg_path`` ([] if missing or unreadable)."""
    if not gpkg_path or not os.path.isfile(gpkg_path):
        return []
    try:
        with closing(sqlite3.connect(f"file:{gpkg_path}?mode=ro", uri=True)) as conn:
            return [row[0] for row in conn.execute("SELECT table_name FROM gpkg_contents")]
    except sqlite3.Error:
        # Callers use this to warn before replacing tables; say why they can't.
        log_exception(f"Could not list the tables in {gpkg_path}; existing "
                      "tables of the same name may be replaced without a prompt")
        return []


def gpkg_layer_uri(gpkg_path, table_name):
    return f"{gpkg_path}|layername={table_name}"


def _virtual_field_origins():
    """Field origins that live on the layer, not in its data (joins, expressions)."""
    origins = []
    scoped = getattr(Qgis, "FieldOrigin", None)
    for owner, names in ((scoped, ("Join", "Expression")), (QgsFields, ("OriginJoin", "OriginExpression"))):
        if owner is None:
            continue
        origins.extend(getattr(owner, n) for n in names if hasattr(owner, n))
    return origins


def stored_field_indexes(layer):
    """Indexes of ``layer``'s fields that belong in a saved copy of its data."""
    fields = layer.fields()
    virtual = _virtual_field_origins()
    return [i for i in range(fields.count()) if fields.fieldOrigin(i) not in virtual]


def write_layer_to_gpkg(source_layer, gpkg_path, table_name, transform_context,
                        feedback=None, fid_name="fid", attributes=None):
    """Write ``source_layer`` as table ``table_name`` in ``gpkg_path``.

    Creates the file if it does not exist, otherwise adds the table (replacing a
    table of the same name) and leaves every other table in place. Field names
    that would clash in a GeoPackage are renamed. ``attributes`` limits the
    written fields to those indexes (``None`` writes all of them).

    Returns ``(error_message, renamed_fields)``; the message is ``""`` on success.
    """
    options = QgsVectorFileWriter.SaveVectorOptions()
    options.driverName = "GPKG"
    options.layerName = table_name
    options.actionOnExistingFile = (
        QgsVectorFileWriter.CreateOrOverwriteLayer if os.path.exists(gpkg_path)
        else QgsVectorFileWriter.CreateOrOverwriteFile
    )

    fields = source_layer.fields()
    indexes = list(range(fields.count())) if attributes is None else list(attributes)
    if attributes is not None:
        options.attributes = indexes
    renamed_fields, reserved_names = resolve_gpkg_field_names([fields.at(i) for i in indexes])

    # The key must not match an *original* field name either: QGIS 3.x then
    # maps that source field onto the key and shifts the remaining values.
    taken = reserved_names | {fields.at(i).name().casefold() for i in indexes}
    base_fid = fid_name
    suffix = 2
    while fid_name.casefold() in taken:
        fid_name = f"{base_fid}_{suffix}"
        suffix += 1
    converter = RenamingFieldConverter(renamed_fields)
    options.fieldValueConverter = converter
    options.layerOptions = ["SPATIAL_INDEX=YES", f"FID={fid_name}"]
    if feedback is not None and hasattr(options, "feedback"):
        options.feedback = feedback

    result = QgsVectorFileWriter.writeAsVectorFormatV3(
        source_layer, gpkg_path, transform_context, options)
    writer_error = result[0] if isinstance(result, tuple) else result
    error_message = result[1] if isinstance(result, tuple) and len(result) > 1 else ""
    error_scope = getattr(QgsVectorFileWriter, "WriterError", QgsVectorFileWriter)
    if writer_error != getattr(error_scope, "NoError"):
        return (error_message or "unknown write error"), renamed_fields
    return "", renamed_fields
