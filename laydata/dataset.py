# -*- coding: utf-8 -*-
"""Columnar in-memory representation of a cable-lay data layer.

``LayDataset`` turns a set of records (each a name->value mapping plus an
optional lat/lon) into numpy column arrays with cached numeric / time / source
views. It is deliberately free of any QGIS *UI* dependency; the only QGIS import
is inside :meth:`LayDataset.from_qgis_layer`, which is skipped entirely by the
unit tests (they build datasets directly from column dicts).

Design notes
------------
* Records are loaded exactly once and kept as columns so bulk operations run in
  vectorised numpy rather than per-feature Python loops - important because raw
  lay data can run to hundreds of thousands of rows.
* Time is stored as float "seconds since 1970-01-01" (naive, DST-free) so that
  gap deltas are exact; the absolute base is irrelevant for QC.
* The per-record *source reference* (``source_file`` / ``event_file`` /
  ``slack_file`` / ``body_file``) is auto-detected so gap/duplicate checks can
  run independently per logging source.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np

# Candidate per-record source-reference field names, in priority order. Mirrors
# the fields written by the cable-lay importers (see cable_lay_parsers).
SOURCE_FIELD_CANDIDATES: Tuple[str, ...] = (
    "source_file",
    "event_file",
    "slack_file",
    "body_file",
)

# Candidate time field names (ISO-8601 string columns).
TIME_FIELD_CANDIDATES: Tuple[str, ...] = ("ISO_Time",)

_EPOCH = datetime(1970, 1, 1)

# Curation column written by the Data Explorer's Manage tab (kept in sync with
# ``processing.cable_lay_manage_ops.STATUS_FIELD``; not imported to keep this
# module free of QGIS dependencies).
RECORD_STATUS_FIELD = "record_status"


def parse_iso_epoch(value) -> float:
    """Parse an ISO-8601 timestamp to seconds since 1970-01-01 (naive).

    Returns ``nan`` for empty / unparseable values. Differences between the
    returned values are exact seconds (no timezone / DST distortion).
    """
    if value is None:
        return np.nan
    text = str(value).strip()
    if not text:
        return np.nan
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        # Tolerate a trailing "Z" and space-separated variants.
        try:
            dt = datetime.fromisoformat(text.replace("Z", "").replace(" ", "T", 1))
        except ValueError:
            return np.nan
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return (dt - _EPOCH).total_seconds()


def epoch_array(values: Sequence) -> np.ndarray:
    """Vectorise :func:`parse_iso_epoch` over an iterable of ISO strings."""
    out = np.full(len(values), np.nan, dtype=float)
    for i, value in enumerate(values):
        out[i] = parse_iso_epoch(value)
    return out


def to_float(value) -> float:
    """Best-effort float conversion; ``nan`` when not numeric."""
    if value is None:
        return np.nan
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return np.nan
    # Tolerate locale decimal commas and thousands separators / stray spaces.
    text = text.replace("\u00a0", "").replace(" ", "")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return np.nan


def haversine_m(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    """Great-circle distance in metres between paired coordinate arrays."""
    radius = 6371000.0
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2.0) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2.0) ** 2
    return 2.0 * radius * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


class LayDataset:
    """Column-oriented view over one cable-lay layer's records."""

    def __init__(
        self,
        columns: Dict[str, Sequence],
        lat: Optional[Sequence] = None,
        lon: Optional[Sequence] = None,
        fids: Optional[Sequence] = None,
        layer_name: str = "",
        source_field: Optional[str] = None,
        time_field: Optional[str] = None,
    ):
        self.layer_name = layer_name
        self.columns: Dict[str, np.ndarray] = {
            name: np.asarray(list(values), dtype=object) for name, values in columns.items()
        }
        self.row_count = len(next(iter(self.columns.values()))) if self.columns else 0

        self._numeric_cache: Dict[str, np.ndarray] = {}
        self._is_numeric_cache: Dict[str, bool] = {}
        self._sources_cache: Optional[List[str]] = None
        self._time_epoch: Optional[np.ndarray] = None
        self._status_cache: Optional[np.ndarray] = None
        self._active_cache: Optional[np.ndarray] = None
        self._source_masks_cache: Optional[Dict[str, np.ndarray]] = None

        self.fids = (
            np.asarray(list(fids), dtype=np.int64)
            if fids is not None
            else np.arange(self.row_count, dtype=np.int64)
        )
        self._lat = np.asarray([to_float(v) for v in lat], dtype=float) if lat is not None else None
        self._lon = np.asarray([to_float(v) for v in lon], dtype=float) if lon is not None else None

        # Fall back to Lat_dd / Lon_dd columns for geometry when not supplied.
        if self._lat is None and self.has_field("Lat_dd"):
            self._lat = self.numeric("Lat_dd")
        if self._lon is None and self.has_field("Lon_dd"):
            self._lon = self.numeric("Lon_dd")

        self.source_field = source_field or self._detect(SOURCE_FIELD_CANDIDATES)
        self.time_field = time_field or self._detect(TIME_FIELD_CANDIDATES)

    # -- field access ------------------------------------------------------
    def _detect(self, candidates: Iterable[str]) -> Optional[str]:
        for name in candidates:
            if name in self.columns:
                return name
        return None

    @property
    def field_names(self) -> List[str]:
        return list(self.columns.keys())

    def has_field(self, name: str) -> bool:
        return name in self.columns

    def raw(self, name: str) -> np.ndarray:
        return self.columns[name]

    def numeric(self, name: str) -> np.ndarray:
        """Return a cached float array for ``name`` (nan where non-numeric)."""
        cached = self._numeric_cache.get(name)
        if cached is None:
            values = self.columns[name]
            cached = np.array([to_float(v) for v in values], dtype=float)
            self._numeric_cache[name] = cached
        return cached

    def is_numeric_field(self, name: str, threshold: float = 0.8) -> bool:
        """True when at least ``threshold`` of non-empty values parse as float.

        The result is cached because callers (plot, QC, inspection and
        processing panels) probe every field repeatedly; recomputing the
        O(row_count) scan each time is what makes large datasets hang.
        """
        if name not in self.columns:
            return False
        cached = self._is_numeric_cache.get(name)
        if cached is not None:
            return cached
        non_empty = 0
        for v in self.columns[name]:
            if v is None:
                continue
            if isinstance(v, (int, float)):
                non_empty += 1
            elif str(v).strip() != "":
                non_empty += 1
        if non_empty == 0:
            self._is_numeric_cache[name] = False
            return False
        good = int(np.count_nonzero(~np.isnan(self.numeric(name))))
        result = good >= threshold * non_empty
        self._is_numeric_cache[name] = result
        return result

    # -- derived views -----------------------------------------------------
    @property
    def lat(self) -> Optional[np.ndarray]:
        return self._lat

    @property
    def lon(self) -> Optional[np.ndarray]:
        return self._lon

    @property
    def has_geometry(self) -> bool:
        return self._lat is not None and self._lon is not None

    @property
    def time_epoch(self) -> Optional[np.ndarray]:
        if self.time_field is None:
            return None
        if self._time_epoch is None:
            self._time_epoch = epoch_array(self.columns[self.time_field])
        return self._time_epoch

    @property
    def has_time(self) -> bool:
        epoch = self.time_epoch
        return epoch is not None and bool(np.any(~np.isnan(epoch)))

    def iso_time_at(self, index: int) -> Optional[str]:
        if self.time_field is None:
            return None
        value = self.columns[self.time_field][index]
        return None if value is None else str(value)

    @property
    def source_array(self) -> np.ndarray:
        if self.source_field is None:
            return np.array(["<all>"] * self.row_count, dtype=object)
        return self.columns[self.source_field]

    def sources(self) -> List[str]:
        if self._sources_cache is None:
            seen: List[str] = []
            known = set()
            for value in self.source_array:
                text = "" if value is None else str(value)
                if text not in known:
                    known.add(text)
                    seen.append(text)
            self._sources_cache = seen
        return list(self._sources_cache)

    def source_at(self, index: int) -> Optional[str]:
        if self.source_field is None:
            return None
        value = self.columns[self.source_field][index]
        return None if value is None else str(value)

    def iter_source_groups(self, order_by_time: bool = True) -> Iterator[Tuple[str, np.ndarray]]:
        """Yield ``(source_value, row_indices)`` for each distinct source.

        When ``order_by_time`` and a usable time field is present, the row
        indices within each group are ordered by ascending timestamp (records
        with an unparseable time are dropped from that ordering).
        """
        sources = self.source_array
        epoch = self.time_epoch if order_by_time else None
        for value in self.sources():
            target = "" if value == "" else value
            mask = np.array(
                [("" if s is None else str(s)) == target for s in sources],
                dtype=bool,
            )
            indices = np.nonzero(mask)[0]
            if epoch is not None:
                group_epoch = epoch[indices]
                valid = ~np.isnan(group_epoch)
                indices = indices[valid]
                order = np.argsort(group_epoch[valid], kind="stable")
                indices = indices[order]
            yield value, indices

    def source_masks(self) -> Dict[str, np.ndarray]:
        """``{source_value: boolean row mask}`` for every non-empty source.

        Computed once per dataset (one pass over the source column) and cached,
        so panels that need per-file views do not each rescan millions of
        rows. Empty when the dataset has no source field.
        """
        if self._source_masks_cache is None:
            masks: Dict[str, np.ndarray] = {}
            if self.source_field is not None:
                values = np.array(
                    [("" if v is None else str(v)) for v in self.columns[self.source_field]],
                    dtype=object,
                )
                for name in sorted(set(values.tolist())):
                    if name:
                        masks[name] = values == name
            self._source_masks_cache = masks
        return dict(self._source_masks_cache)

    # -- record status (non-destructive curation) --------------------------
    def status_array(self) -> Optional[np.ndarray]:
        """Stripped ``record_status`` text per row, or ``None`` without the column.

        NULL attributes (``None`` or a null QVariant, whose text is ``NULL``)
        become ``""`` so callers only need to compare against the plain
        status words.
        """
        if RECORD_STATUS_FIELD not in self.columns:
            return None
        if self._status_cache is None:
            out = []
            for value in self.columns[RECORD_STATUS_FIELD]:
                text = "" if value is None else str(value).strip()
                out.append("" if text == "NULL" else text)
            self._status_cache = np.array(out, dtype=object)
        return self._status_cache

    def active_mask(self) -> np.ndarray:
        """Rows that count as active: status ``active``, empty, or no column."""
        if self._active_cache is None:
            statuses = self.status_array()
            if statuses is None:
                self._active_cache = np.ones(self.row_count, dtype=bool)
            else:
                self._active_cache = (statuses == "") | (statuses == "active")
        return self._active_cache

    @property
    def has_status_field(self) -> bool:
        return RECORD_STATUS_FIELD in self.columns

    def subset(self, mask: np.ndarray) -> "LayDataset":
        """A new dataset holding only the rows where ``mask`` is True.

        Row indices in the subset are renumbered, but ``fids`` still point at
        the original layer features, so map sync and QC findings keep working.
        Field / source / time detection is carried over unchanged.
        """
        mask = np.asarray(mask, dtype=bool)
        view = LayDataset.__new__(LayDataset)
        view.layer_name = self.layer_name
        view.columns = {name: values[mask] for name, values in self.columns.items()}
        view.row_count = int(mask.sum())
        view._numeric_cache = {
            name: values[mask] for name, values in self._numeric_cache.items()
        }
        view._is_numeric_cache = dict(self._is_numeric_cache)
        view._sources_cache = None
        view._time_epoch = None if self._time_epoch is None else self._time_epoch[mask]
        view._status_cache = None if self._status_cache is None else self._status_cache[mask]
        view._active_cache = None
        view._source_masks_cache = None
        view.fids = self.fids[mask]
        view._lat = None if self._lat is None else self._lat[mask]
        view._lon = None if self._lon is None else self._lon[mask]
        view.source_field = self.source_field
        view.time_field = self.time_field
        return view

    def active_view(self) -> "LayDataset":
        """``self`` when every row is active, else a subset of the active rows."""
        mask = self.active_mask()
        if bool(mask.all()):
            return self
        return self.subset(mask)

    # -- QGIS loading ------------------------------------------------------
    @classmethod
    def from_qgis_layer(cls, layer, feedback=None) -> "LayDataset":
        """Load every feature of a QGIS vector layer into a dataset.

        Geometry is reduced to a representative lon/lat (point centroid) in the
        layer CRS reprojected to WGS84 when required. Imported cable-lay layers
        are already WGS84, so the transform is usually a no-op.
        """
        from qgis.core import QgsProject, QgsVectorLayerFeatureSource

        # A feature *source* is a thread-safe snapshot of the provider, so the
        # same reader can be driven either on the main thread (here) or from a
        # background QgsTask (see explorer/layer_loader.py).
        source = QgsVectorLayerFeatureSource(layer)
        return cls.from_feature_source(
            source,
            field_names=[field.name() for field in layer.fields()],
            layer_crs=layer.crs(),
            is_spatial=layer.isSpatial(),
            transform_context=QgsProject.instance().transformContext(),
            layer_name=layer.name(),
            feature_count=max(int(layer.featureCount()), 0),
        )

    @classmethod
    def from_feature_source(
        cls,
        source,
        field_names,
        layer_crs,
        is_spatial: bool,
        transform_context,
        layer_name: str = "",
        feature_count: int = 0,
        progress=None,
        is_canceled=None,
    ) -> Optional["LayDataset"]:
        """Build a dataset from a QGIS feature source.

        Safe to call from a background thread when ``source`` is a
        ``QgsVectorLayerFeatureSource`` created on the main thread. ``progress``
        (called with the running feature index) and ``is_canceled`` (returning
        ``True`` to abort) are optional hooks used by the background loader;
        returns ``None`` when cancelled.
        """
        from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

        columns: Dict[str, List] = {name: [] for name in field_names}
        # Append via a positional list of the column buffers so the hot loop
        # avoids a field-name lookup per attribute (feature[name]) on every row.
        col_lists = [columns[name] for name in field_names]
        n_fields = len(field_names)
        lats: List[float] = []
        lons: List[float] = []
        fids: List[int] = []

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = None
        if layer_crs.isValid() and layer_crs != wgs84:
            transform = QgsCoordinateTransform(layer_crs, wgs84, transform_context)

        for idx, feature in enumerate(source.getFeatures()):
            if is_canceled is not None and idx % 2000 == 0 and is_canceled():
                return None
            attrs = feature.attributes()
            for i in range(n_fields):
                col_lists[i].append(attrs[i])
            fids.append(int(feature.id()))
            if not is_spatial:
                lats.append(np.nan)
                lons.append(np.nan)
            else:
                geom = feature.geometry()
                if geom is None or geom.isEmpty():
                    lats.append(np.nan)
                    lons.append(np.nan)
                else:
                    point = geom.centroid().asPoint()
                    if transform is not None:
                        point = transform.transform(point)
                    lons.append(float(point.x()))
                    lats.append(float(point.y()))
            if progress is not None and idx % 2000 == 0:
                progress(idx)

        return cls(
            columns=columns,
            lat=lats,
            lon=lons,
            fids=fids,
            layer_name=layer_name,
        )
