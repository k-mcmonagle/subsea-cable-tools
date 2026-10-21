# -*- coding: utf-8 -*-
"""Importing deliveries into a dataset: review everything first, then write it in one go.

1. :func:`review_files` reads each file through the import profile and says
   what would happen, writing nothing: rows kept and rejected (with
   reasons), the same bytes already in the dataset (skipped), a current file
   of the same name (replaced by default), and overlaps in time with the
   dataset's current files and the files before it in this import (newer
   wins by default). :func:`plan_overlaps` works the overlaps out again when
   the user changes what is included or replaced, keeping their rule choices.
2. :func:`import_batch` runs in a background task on its own connection:
   the dataset (if new), the profile and every included file in one
   transaction. A cancel, or any failure, leaves the data file as it was.
   Each file is read again from disk and must still match the checksum the
   review saw.

No Qt; numpy and sqlite3 (through :mod:`.store`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import overlap as ov
from . import reader as rd
from .store import AcquiredStore, AcquiredStoreError, prepare_file, sha256

OLDER_FILE, OLDER_BATCH = "file", "batch"
REJECTED_SAMPLE = 200


@dataclass
class OverlapChoice:
    """An overlap of a file in this import with an older one (in the dataset, or earlier in the import)."""
    older: Tuple[str, str]           # (OLDER_FILE, file_id) or (OLDER_BATCH, index)
    older_name: str
    t0: float
    t1: float
    older_count: int
    newer_count: int
    rule: str = ov.RULE_NEWER
    split_utc: Optional[float] = None

    def key(self) -> Tuple[str, str]:
        return self.older


@dataclass
class BatchFile:
    path: str
    name: str
    sha256: str = ""
    size_bytes: int = 0
    rows_read: int = 0
    rows_kept: int = 0
    rows_rejected: int = 0
    rejected: List[Tuple[int, str, str]] = field(default_factory=list)   # the first REJECTED_SAMPLE
    warnings: List[str] = field(default_factory=list)
    time_range: Tuple[Optional[float], Optional[float]] = (None, None)
    kp_range: Tuple[Optional[float], Optional[float]] = (None, None)
    times: np.ndarray = field(default_factory=lambda: np.zeros(0))
    time_format: str = ""
    error: str = ""                  # could not be read: not imported
    duplicate: Optional[Dict] = None  # the file already in the dataset with these bytes
    replaces: Optional[str] = None   # file_id of the current file it replaces
    replaces_name: str = ""
    same_name: Optional[Dict] = None  # the current file with this name (offered for replacement)
    overlaps: List[OverlapChoice] = field(default_factory=list)
    include: bool = True

    @property
    def importable(self) -> bool:
        return self.include and not self.error and self.duplicate is None and self.rows_kept > 0

    @property
    def has_time(self) -> bool:
        return self.time_range[0] is not None


@dataclass
class DatasetTarget:
    """The dataset to import into: an existing one, or the details of a new one."""
    dataset_id: Optional[str] = None
    name: str = ""
    kind: str = "trencher"
    originator: str = ""
    rpl_id: str = ""
    rpl_label: str = ""
    rpl_start_kp: Optional[float] = None


@dataclass
class ImportOptions:
    profile: Dict
    profile_name: str
    received_utc: str = ""
    acquired_utc: str = ""           # for files without a time column
    originator: str = ""
    reason: str = ""


@dataclass
class ImportResult:
    dataset_id: str
    imported: List[Tuple[str, str]]  # (file_id, name)
    skipped: List[Tuple[str, str]]   # (name, why)
    rows: int


def _read_bytes(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise AcquiredStoreError(f"Could not read {os.path.basename(path)}: {exc.strerror or exc}") from exc


def review_files(reader, dataset_id: Optional[str], paths: Sequence[str], profile: Dict,
                 cancelled: Optional[Callable[[], bool]] = None,
                 progress: Optional[Callable[[float, str], None]] = None) -> List[BatchFile]:
    """What importing ``paths`` (in this order) into the dataset would do; nothing is written.

    ``reader`` is a :class:`.store.StoreReader` / :class:`.store.AcquiredStore`
    on the data file, or None when there is no data file or dataset yet.
    """
    existing_rows = reader.list_files(dataset_id) if (reader is not None and dataset_id) else []
    current = [r for r in existing_rows if r["status"] == "current"]
    batch: List[BatchFile] = []
    for index, path in enumerate(paths):
        if cancelled and cancelled():
            raise InterruptedError("Review cancelled")
        name = os.path.basename(path)
        if progress:
            progress(index / max(1, len(paths)), f"Reading {name}")
        item = BatchFile(path=path, name=name)
        try:
            content = _read_bytes(path)
        except AcquiredStoreError as exc:
            item.error = str(exc)
            batch.append(item)
            continue
        item.sha256, item.size_bytes = sha256(content), len(content)
        try:
            _raw, parsed = rd.read(content, name, profile, cancelled)
        except rd.ReadError as exc:
            item.error = str(exc)
            batch.append(item)
            continue
        item.rows_read, item.rows_kept, item.rows_rejected = parsed.rows_read, parsed.n, len(parsed.rejected)
        item.rejected = list(parsed.rejected[:REJECTED_SAMPLE])
        item.warnings = list(parsed.warnings)
        item.time_range, item.kp_range = parsed.time_range, parsed.kp_range
        times = parsed.columns.get("time")
        item.times = np.zeros(0) if times is None else times.copy()
        item.time_format = parsed.time_format
        same_bytes = next((r for r in existing_rows if r["sha256"] == item.sha256), None)
        if same_bytes is None:
            earlier = next((b for b in batch if b.sha256 == item.sha256), None)
            if earlier is not None:
                same_bytes = {"name": earlier.name, "imported_utc": "", "author": "this import",
                              "file_id": ""}
        item.duplicate = same_bytes
        same_name = next((r for r in current if r["name"] == name), None)
        if same_name is not None and same_bytes is None:
            item.same_name = same_name
            item.replaces, item.replaces_name = same_name["file_id"], same_name["name"]
        batch.append(item)
    existing_times = reader.file_times(dataset_id) if (reader is not None and dataset_id) else []
    plan_overlaps(batch, existing_times)
    if progress:
        progress(1.0, "Review ready")
    return batch


def plan_overlaps(batch: Sequence[BatchFile], existing: Sequence[ov.FileTimes]) -> None:
    """Fill each file's overlaps from what is included and replaced, keeping chosen rules."""
    replaced = {b.replaces for b in batch if b.importable and b.replaces}
    older: List[Tuple[Tuple[str, str], str, ov.FileTimes]] = [
        ((OLDER_FILE, f.file_id), f.name, f) for f in existing if f.file_id not in replaced]
    for index, item in enumerate(batch):
        previous = {o.key(): o for o in item.overlaps}
        item.overlaps = []
        if not item.importable:
            continue
        if item.has_time:
            mine = ov.FileTimes(f"batch:{index}", 0, item.times, item.name)
            for key, name, times in older:
                for found in ov.find_overlaps([times], mine):
                    choice = OverlapChoice(key, name, found.t0, found.t1, found.older_count, found.newer_count)
                    kept = previous.get(key)
                    if kept is not None:
                        choice.rule = kept.rule
                        if kept.split_utc is not None and choice.t0 <= kept.split_utc <= choice.t1:
                            choice.split_utc = kept.split_utc
                        elif choice.rule == ov.RULE_SPLIT:
                            choice.rule = ov.RULE_NEWER
                    item.overlaps.append(choice)
        older.append(((OLDER_BATCH, str(index)), item.name,
                      ov.FileTimes(f"batch:{index}", 0, item.times, item.name)))


def summary(batch: Sequence[BatchFile]) -> Dict[str, int]:
    """Counts for the review line."""
    importable = [b for b in batch if b.importable]
    return {
        "files": len(importable),
        "rows": sum(b.rows_kept for b in importable),
        "rejected": sum(b.rows_rejected for b in importable),
        "duplicates": sum(1 for b in batch if b.duplicate is not None),
        "errors": sum(1 for b in batch if b.error),
        "replaced": sum(1 for b in importable if b.replaces),
        "overlaps": sum(len(b.overlaps) for b in importable),
        "no_time": sum(1 for b in importable if not b.has_time),
    }


def import_batch(path: str, target: DatasetTarget, batch: Sequence[BatchFile], options: ImportOptions,
                 cancelled: Optional[Callable[[], bool]] = None,
                 progress: Optional[Callable[[float, str], None]] = None) -> ImportResult:
    """Write the reviewed files in one transaction (run in a background task)."""
    todo = [(i, b) for i, b in enumerate(batch) if b.importable]
    if not todo:
        raise AcquiredStoreError("There is nothing to import.")
    if any(not b.has_time for _i, b in todo) and not options.acquired_utc:
        raise AcquiredStoreError("Some files have no time column: give the date they were acquired.")
    store = AcquiredStore.for_task(path)
    imported: List[Tuple[str, str]] = []
    rows = 0
    try:
        with store.batch("import the files"):
            dataset_id = target.dataset_id
            if not dataset_id:
                dataset_id = store.create_dataset(target.name, target.kind, target.rpl_id, target.rpl_label,
                                                  target.rpl_start_kp, target.originator)
            profile_id = store.save_profile(options.profile_name, options.profile)
            if (store.get_dataset(dataset_id) or {}).get("profile_id") != profile_id:
                store.update_dataset(dataset_id, {"profile_id": profile_id}, reason="import profile")
            ids: Dict[int, str] = {}
            for step, (index, item) in enumerate(todo):
                if cancelled and cancelled():
                    raise InterruptedError("Import cancelled: nothing was written.")
                if progress:
                    progress(step / len(todo), f"Importing {item.name}")
                content = _read_bytes(item.path)
                if sha256(content) != item.sha256:
                    raise AcquiredStoreError(f"{item.name} changed on disk since the review: review it again.")
                _raw, parsed = rd.read(content, item.name, options.profile, cancelled)
                prepared = prepare_file(item.name, content, parsed, cancelled)
                overlaps = []
                for choice in item.overlaps:
                    kind, ref = choice.older
                    older_id = ref if kind == OLDER_FILE else ids.get(int(ref))
                    if older_id is None:
                        continue
                    overlaps.append(ov.Overlap(older_id, "", choice.t0, choice.t1, choice.older_count,
                                               choice.newer_count, choice.rule, choice.split_utc))
                file_id = store.add_file(dataset_id, prepared, profile_id, options.received_utc,
                                         options.originator, options.acquired_utc if not item.has_time else "",
                                         replaces_file_id=item.replaces, overlaps=overlaps,
                                         reason=options.reason)
                ids[index] = file_id
                imported.append((file_id, item.name))
                rows += prepared.rows_kept
            if cancelled and cancelled():
                raise InterruptedError("Import cancelled: nothing was written.")
    finally:
        store.close()
    if progress:
        progress(1.0, "Imported")
    skipped = [(b.name, b.error or ("same bytes as " + b.duplicate["name"] if b.duplicate else
                                    "not included" if not b.include else "no rows"))
               for b in batch if not b.importable]
    return ImportResult(dataset_id, imported, skipped, rows)
