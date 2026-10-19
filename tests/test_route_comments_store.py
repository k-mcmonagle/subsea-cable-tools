# -*- coding: utf-8 -*-
"""Route Comments register and data rules (pure Python + sqlite3, no QGIS).

* The register is a valid GeoPackage: system tables, a feature table with
  EPSG:4326 Point/LineString blobs that round-trip to WKT.
* Sets, custom fields, comments (UUIDs, per-set numbers, timestamps),
  replies in order, image attachments copied beside the register and
  removed with their comment or set.
* Saves are transactional: a bad row in a batch adds nothing; a save from
  an editor opened on an older version is refused (ConflictError); a
  second connection (another project / QGIS) sees committed changes and
  ``data_version`` tells the first one.
* Statuses/priorities are free choices with defaults; custom values are
  coerced by type; search needs every word; filters combine.
* Import validation: column guessing, KP/position rules, KP off the route,
  duplicate IDs (in the register and within the file), UUIDs kept.
* Export rows hold every documented column, including custom fields.
"""

from __future__ import annotations

import os
import sqlite3
import struct
import tempfile

from ..route_comments import model
from ..route_comments import store as store_module
from ..route_comments.store import (CommentStore, CommentStoreError, ConflictError, blob_to_wkt,
                                    gpkg_blob, is_register)

REQUIRES_QGIS = False


def _result(name: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _folder() -> str:
    return tempfile.mkdtemp(prefix="rc_store_")


def _point(lon=3.0, lat=55.0, **extra):
    values = {"text": "Boulder field near the route", "ref_kind": model.REF_ROUTE_POINT, "kp_km": 12.5,
              "wkt": f"POINT({lon} {lat})", "status": "Open", "priority": "Normal", "author": "A. Tester"}
    values.update(extra)
    return values


def test_gpkg_structure_and_geometry() -> bool:
    folder = _folder()
    path = os.path.join(folder, "register.gpkg")
    store = CommentStore.create(path)
    store.close()
    conn = sqlite3.connect(path)
    try:
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        geom_cols = conn.execute("SELECT table_name, geometry_type_name, srs_id FROM gpkg_geometry_columns").fetchall()
        contents = {r[0]: r[1] for r in conn.execute("SELECT table_name, data_type FROM gpkg_contents")}
        srs = {r[0] for r in conn.execute("SELECT srs_id FROM gpkg_spatial_ref_sys")}
    finally:
        conn.close()
    ok = (app_id == 0x47504B47 and geom_cols == [("rc_comment", "GEOMETRY", 4326)]
          and contents.get("rc_comment") == "features" and contents.get("rc_set") == "attributes"
          and {-1, 0, 4326} <= srs and is_register(path) and not is_register(os.path.join(folder, "x.gpkg")))
    line = "LINESTRING(3.0 55.0, 3.1 55.05, 3.2 55.1)"
    blob = gpkg_blob(line)
    env = struct.unpack_from("<dddd", blob, 8)
    ok = ok and blob[:2] == b"GP" and env == (3.0, 3.2, 55.0, 55.1)
    ok = ok and blob_to_wkt(blob) == "LINESTRING(3.0 55.0, 3.1 55.05, 3.2 55.1)"
    ok = ok and blob_to_wkt(gpkg_blob("POINT Z (1.5 -2.25 7)")) == "POINT(1.5 -2.25)"
    ok = ok and gpkg_blob("POLYGON((0 0, 1 0, 1 1, 0 0))") is None and gpkg_blob("POINT(nan 1)") is None
    return _result("register is a GeoPackage; geometry blobs round-trip", ok)


def test_sets_comments_numbering() -> bool:
    path = os.path.join(_folder(), "reg.gpkg")
    store = CommentStore.create(path)
    try:
        sets = store.list_sets()
        first = sets[0]
        second = store.create_set("Client review", priorities=["P1", "P2", "P3"])
        try:
            store.create_set("client REVIEW")
            dup_refused = False
        except CommentStoreError:
            dup_refused = True
        a = store.add_comment(first["set_id"], _point())
        b = store.add_comment(first["set_id"], _point(text="Second"))
        c = store.add_comment(second["set_id"], _point(text="Other set"))
        ok = (len(sets) == 1 and first["name"] == "Comments" and dup_refused
              and [a["ref_no"], b["ref_no"], c["ref_no"]] == [1, 2, 1]
              and len(a["comment_id"]) == 36 and a["version"] == 1 and a["created_utc"] and a["updated_utc"]
              and a["wkt"] == "POINT(3.0 55.0)" and a["reply_count"] == 0
              and store.priorities_of(store.get_set(second["set_id"])) == ["P1", "P2", "P3"]
              and [s["name"] for s in store.list_sets()] == ["Client review", "Comments"])
        store.update_set(second["set_id"], name="Client review B",
                         statuses=[{"name": "New"}, {"name": "Done", "closed": True}, {"name": "new"}])
        renamed = store.get_set(second["set_id"])
        ok = ok and renamed["name"] == "Client review B" and \
            store.statuses_of(renamed) == [{"name": "New", "closed": False}, {"name": "Done", "closed": True}]
        store.delete_set(second["set_id"])
        ok = ok and store.get_comment(c["comment_id"]) is None and len(store.list_comments()) == 2
    finally:
        store.close()
    return _result("sets, per-set numbering, UUIDs and timestamps", ok)


def test_batch_is_atomic_and_validated() -> bool:
    path = os.path.join(_folder(), "reg.gpkg")
    store = CommentStore.create(path)
    try:
        set_id = store.list_sets()[0]["set_id"]
        problems = []
        for bad in ({"text": ""}, {"wkt": "POLYGON((0 0,1 0,1 1,0 0))"}, {"ref_kind": "somewhere"}):
            try:
                store.add_comments(set_id, [_point(), _point(**bad)])
                problems.append(f"accepted {bad}")
            except CommentStoreError:
                pass
        fixed_id = "0190f000-0000-7000-8000-000000000001"
        store.add_comment(set_id, _point(comment_id=fixed_id))
        try:
            store.add_comment(set_id, _point(comment_id=fixed_id))
            problems.append("duplicate UUID accepted")
        except CommentStoreError:
            pass
        ok = not problems and len(store.list_comments(set_id)) == 1
    finally:
        store.close()
    return _result("a bad row adds nothing (one transaction); duplicate UUIDs refused", ok, "; ".join(problems))


def test_versioned_save_and_second_connection() -> bool:
    path = os.path.join(_folder(), "reg.gpkg")
    store = CommentStore.create(path)
    try:
        set_id = store.list_sets()[0]["set_id"]
        comment = store.add_comment(set_id, _point())
        # Another project / QGIS: an independent connection to the same file.
        other = sqlite3.connect(path, timeout=5)
        before = store.data_version()
        with other:
            other.execute("UPDATE rc_comment SET status = 'Closed', version = version + 1 WHERE comment_id = ?",
                          (comment["comment_id"],))
        changed_seen = store.data_version() != before
        other.close()
        try:
            store.update_comment(comment["comment_id"], {"text": "Edited here"}, expected_version=1)
            conflict = False
        except ConflictError:
            conflict = True
        saved = store.update_comment(comment["comment_id"], {"text": "Edited here", "wkt": "POINT(4 56)"},
                                     expected_version=2)
        unchanged_id = saved["comment_id"] == comment["comment_id"] and saved["ref_no"] == 1
        store.delete_comments([comment["comment_id"]])
        try:
            store.update_comment(comment["comment_id"], {"text": "x"}, expected_version=3)
            deleted_conflict = False
        except ConflictError:
            deleted_conflict = True
        ok = (changed_seen and conflict and saved["version"] == 3 and saved["status"] == "Closed"
              and saved["text"] == "Edited here" and saved["wkt"] == "POINT(4.0 56.0)" and unchanged_id
              and deleted_conflict)
    finally:
        store.close()
    return _result("versioned saves refuse stale edits; other connections are seen", ok)


def test_replies_and_attachments() -> bool:
    folder = _folder()
    path = os.path.join(folder, "reg.gpkg")
    image = os.path.join(folder, "photo.PNG")
    with open(image, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\nfake")
    store = CommentStore.create(path)
    try:
        set_id = store.list_sets()[0]["set_id"]
        comment = store.add_comment(set_id, _point())
        first = store.add_reply(comment["comment_id"], "B", "Agreed")
        second = store.add_reply(comment["comment_id"], "C", "Rerouted in rev C")
        replies = store.list_replies(comment["comment_id"])
        rows = store.add_attachments(comment["comment_id"], [image, image])
        files = [store.attachment_path(r) for r in rows]
        in_folder = all(os.path.isfile(p) and os.path.dirname(p) == store.attachments_dir for p in files)
        try:
            store.add_attachments(comment["comment_id"], [os.path.join(folder, "notes.txt")])
            refused_type = False
        except CommentStoreError:
            refused_type = True
        reread = store.get_comment(comment["comment_id"])
        store.delete_attachment(rows[0]["attachment_id"])
        removed_one = not os.path.exists(files[0]) and os.path.exists(files[1])
        store.delete_comments([comment["comment_id"]])
        ok = ([r["reply_id"] for r in replies] == [first["reply_id"], second["reply_id"]]
              and reread["reply_count"] == 2 and reread["attachment_count"] == 2 and in_folder
              and refused_type and removed_one and not os.path.exists(files[1])
              and store.list_replies(comment["comment_id"]) == []
              and store.attachments_dir.endswith("reg_attachments")
              and rows[0]["original_name"] == "photo.PNG" and rows[0]["file_name"].endswith(".png"))
    finally:
        store.close()
    return _result("replies in order; images copied beside the register and cleaned up", ok)


def test_custom_fields() -> bool:
    path = os.path.join(_folder(), "reg.gpkg")
    store = CommentStore.create(path)
    try:
        set_id = store.list_sets()[0]["set_id"]
        fields = store.save_fields(set_id, [
            model.CustomField("", "Water depth", model.FIELD_NUMBER),
            model.CustomField("", "Due", model.FIELD_DATE),
            model.CustomField("", "Survey needed", model.FIELD_BOOLEAN),
            model.CustomField("", "Discipline", model.FIELD_CHOICE, ["Route", "Geotech", "Permits"]),
            model.CustomField("", "Note"),
        ])
        try:
            store.save_fields(set_id, [model.CustomField("", "A"), model.CustomField("", "a")])
            dup = False
        except CommentStoreError:
            dup = True
        reread = store.list_fields(set_id)
        depth, due, survey, disc, _note = reread
        checks = [
            model.coerce_custom(depth, "1 234,5") == (1234.5, ""),
            model.coerce_custom(depth, "deep") == (None, "Water depth: 'deep' is not a number"),
            model.coerce_custom(depth, "-45,5") == (-45.5, ""),
            model.coerce_custom(due, "03/04/2026") == ("2026-04-03", ""),
            model.coerce_custom(survey, "Yes") == (True, ""),
            model.coerce_custom(survey, "maybe")[0] is None,
            model.coerce_custom(disc, "geotech") == ("Geotech", ""),
            model.coerce_custom(disc, "Cables")[0] == "Cables",
            model.coerce_custom(depth, "  ") == (None, ""),
            model.custom_text(survey, False) == "No",
        ]
        ok = (dup and [f.name for f in reread] == [f.name for f in fields] and all(checks)
              and disc.choices == ["Route", "Geotech", "Permits"] and reread[0].field_id == fields[0].field_id)
    finally:
        store.close()
    return _result("custom fields: saved per set, values coerced by type", ok, str(checks))


def test_reopen_adds_missing_columns() -> bool:
    path = os.path.join(_folder(), "reg.gpkg")
    store = CommentStore.create(path)
    store.add_comment(store.list_sets()[0]["set_id"], _point())
    store.close()
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("ALTER TABLE rc_comment DROP COLUMN tags")
    conn.close()
    reopened = CommentStore(path).open()
    try:
        comment = reopened.list_comments()[0]
        ok = "tags" in comment and comment["tags"] is None and reopened.meta().get("schema_version") == "1"
    finally:
        reopened.close()
    try:
        CommentStore(os.path.join(_folder(), "nothing.gpkg")).open()
        clear = False
    except CommentStoreError as exc:
        clear = "not a comment register" in str(exc)
    return _result("reopening adds new columns; opening a non-register says so", ok and clear)


def test_busy_register_message() -> bool:
    path = os.path.join(_folder(), "reg.gpkg")
    store = CommentStore.create(path)
    set_id = store.list_sets()[0]["set_id"]
    blocker = sqlite3.connect(path, timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")
    conn = store_module.connect(path)
    conn.execute("PRAGMA busy_timeout = 100")
    try:
        store.add_comment(set_id, _point())
        message = ""
    except CommentStoreError as exc:
        message = str(exc)
    finally:
        blocker.rollback()
        blocker.close()
        conn.execute("PRAGMA busy_timeout = 10000")
        store.close()
    return _result("a locked register gives a plain 'busy, try again' error", "is busy" in message, message)


def test_register_is_not_wal() -> bool:
    """QGIS reads the register read-only: it must not need WAL side files."""
    path = os.path.join(_folder(), "reg.gpkg")
    store = CommentStore.create(path)
    set_id = store.list_sets()[0]["set_id"]
    store.add_comment(set_id, _point())
    mode = store._conn().execute("PRAGMA journal_mode").fetchone()[0]
    store.close()
    # A register made WAL by an earlier build is switched back when opened.
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    reopened = CommentStore(path).open()
    again = reopened._conn().execute("PRAGMA journal_mode").fetchone()[0]
    reopened.close()
    readonly = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        count = readonly.execute("SELECT COUNT(*) FROM rc_comment").fetchone()[0]
    finally:
        readonly.close()
    no_sidecars = not os.path.exists(path + "-wal") and not os.path.exists(path + "-shm")
    return _result("rollback journal: a read-only reader opens the register without side files",
                   mode == "delete" and again == "delete" and count == 1 and no_sidecars, f"{mode}/{again}")


def test_choices_search_filters() -> bool:
    statuses = model.normalise_statuses([{"name": "Open"}, "  ", {"name": "Closed", "closed": True}, "open"])
    checks = {
        "statuses": statuses == [{"name": "Open", "closed": False}, {"name": "Closed", "closed": True}],
        "defaults": model.status_names(None)[0] == "Open" and model.default_priority(None) == "Normal",
        "default priority otherwise": model.default_priority(["P1", "P2", "P3"]) == "P2",
        "closed": model.closed_statuses(statuses) == {"closed"},
    }
    fields = [model.CustomField("f1", "Discipline", model.FIELD_CHOICE, ["Geotech"])]
    comment = {"ref_no": 7, "text": "Steep slope at the shelf break", "author": "Jo", "status": "Open",
               "priority": "High", "ref_kind": model.REF_KP_RANGE, "kp_km": 10.0, "kp_end_km": 10.5,
               "rpl_name": "Seg 1", "rpl_rev": "C", "custom_json": model.dump_custom({"f1": "Geotech"})}
    blob = model.search_blob(comment, fields, [{"author": "Sam", "text": "boulders seen on ROV"}])
    f = model.Filter
    checks.update({
        "words": model.matches(comment, f(text="shelf SLOPE"), blob),
        "all words": not model.matches(comment, f(text="shelf trench"), blob),
        "number": model.matches(comment, f(text="#7"), blob),
        "custom value": model.matches(comment, f(text="geotech"), blob),
        "reply text": model.matches(comment, f(text="rov"), blob),
        "route": model.matches(comment, f(text="seg 1 rev c"), blob),
        "status filter": not model.matches(comment, f(statuses={"closed"}), blob),
        "priority filter": model.matches(comment, f(priorities={"high"}), blob),
        "kind filter": not model.matches(comment, f(kinds={model.REF_LOCATION}), blob),
        "condition filter": model.matches(comment, f(conditions={model.COND_CHANGED}), blob,
                                          condition=model.COND_CHANGED),
        "hide closed": not model.matches(dict(comment, status="Closed"), f(hide_closed=True), blob,
                                         closed={"closed"}),
        "kp text": model.kp_text(comment) == "10.000 – 10.500",
        "location text": model.location_text(comment) == "KP 10.000 – 10.500 (500 m) on Seg 1 rev C",
        "summary": model.summary("  \nFirst line here\nsecond", 10) == "First lin…",
    })
    failed = [k for k, v in checks.items() if not v]
    return _result("choices, search (every word) and filters", not failed, ", ".join(failed))


def test_parsers() -> bool:
    checks = {
        "dd": model.parse_coordinate("-12.5", True) == -12.5,
        "comma": model.parse_coordinate("3,25", False) == 3.25,
        "ddm": abs(model.parse_coordinate("55 30.0 N", True) - 55.5) < 1e-12,
        "dms south": abs(model.parse_coordinate("12°30'36\"S", True) + 12.51) < 1e-12,
        "west": model.parse_coordinate("W 3 15", False) == -3.25,
        "lat range": model.parse_coordinate("95", True) is None,
        "hemisphere on wrong axis": model.parse_coordinate("10 E", True) is None,
        "junk": model.parse_coordinate("abc", True) is None,
        "kp plus": model.parse_kp("12+345") == 12.345,
        "kp prefix": model.parse_kp("KP 3.5") == 3.5,
        "kp comma": model.parse_kp("3,5") == 3.5,
        "date iso": model.parse_date("2026-10-19") == "2026-10-19",
        "date cell": model.parse_date("2026-10-19 00:00:00") == "2026-10-19",
        "date dayfirst": model.parse_date("03/04/2026") == "2026-04-03",
        "date junk": model.parse_date("soon") == "",
    }
    failed = [k for k, v in checks.items() if not v]
    return _result("coordinate, KP and date parsing", not failed, ", ".join(failed))


def test_import_validation() -> bool:
    headers = ["Comment No", "Remarks", "Status", "Priority", "Raised by", "Date", "KP From", "KP To",
               "Latitude", "Longitude", "Discipline"]
    fields = [model.CustomField("fd", "Discipline", model.FIELD_CHOICE, ["Route", "Geotech"])]
    mapping = model.guess_mapping(headers, fields)
    want = {"ext_id": 0, "text": 1, "status": 2, "priority": 3, "author": 4, "comment_date": 5, "kp_km": 6,
            "kp_end_km": 7, "lat": 8, "lon": 9, "cf:fd": 10}
    uuid = "0190f000-0000-7000-8000-00000000abcd"
    rows = [
        ["C-1", "Point comment", "open", "High", "Jo", "19/10/2026", "12.5", "", "", "", "geotech"],
        ["C-2", "Range comment", "Pending", "", "", "", "12.0", "13.0", "", "", ""],
        ["C-3", "Position comment", "", "", "", "", "", "", "55 30 N", "3.25", ""],
        ["C-1", "Repeated ID", "", "", "", "", "12.6", "", "", "", ""],
        ["OLD-9", "Already in register", "", "", "", "", "12.6", "", "", "", ""],
        ["C-6", "Off the route", "", "", "", "", "99", "", "", "", ""],
        ["C-7", "", "", "", "", "", "12.6", "", "", "", ""],
        ["C-8", "No location", "", "", "", "", "", "", "", "", ""],
        [uuid, "Exported earlier", "", "", "", "", "12.7", "", "", "", ""],
        ["", "", "", "", "", "", "", "", "", "", ""],
        ["C-10", "Half a position", "", "", "", "", "", "", "55", "", ""],
    ]
    out = model.validate_import(rows, mapping, fields, statuses=None, priorities=None,
                                existing_ids={"old-9"}, kp_range=(10.0, 20.0), default_author="Me",
                                default_date="2026-10-01")
    by_line = {r.line: r for r in out}
    checks = {
        "mapping": mapping == want,
        "blank row skipped": len(out) == 10,
        "point": by_line[2].ok and by_line[2].kind == model.REF_ROUTE_POINT and by_line[2].values["status"] == "Open"
        and by_line[2].values["comment_date"] == "2026-10-19" and by_line[2].values["ext_id"] == "C-1"
        and model.load_custom(by_line[2].values) == {"fd": "Geotech"},
        "range + unknown status kept": by_line[3].ok and by_line[3].kind == model.REF_KP_RANGE
        and by_line[3].values["status"] == "Pending" and by_line[3].warnings
        and by_line[3].values["author"] == "Me" and by_line[3].values["priority"] == "Normal",
        "position": by_line[4].ok and by_line[4].kind == model.REF_LOCATION and by_line[4].values["lat"] == 55.5,
        "duplicate in file": not by_line[5].ok and "row 2" in by_line[5].errors[0],
        "duplicate in register": not by_line[6].ok and "already in the register" in by_line[6].errors[0],
        "off route": not by_line[7].ok and "off the route" in by_line[7].errors[0],
        "no text": not by_line[8].ok,
        "no location": not by_line[9].ok,
        "uuid kept": by_line[10].ok and by_line[10].values.get("comment_id") == uuid,
        "half position": not by_line[12].ok,
        "summary": model.import_summary(out).startswith("4 of 10 rows will be imported; 6 with errors"),
    }
    no_route = model.validate_import([["x", "KP only", "", "", "", "", "12", "", "", "", ""]], mapping, fields)
    checks["kp needs route"] = not no_route[0].ok and "need a route" in no_route[0].errors[0]
    failed = [k for k, v in checks.items() if not v]
    return _result("import: mapping guess, location rules, duplicate IDs", not failed, ", ".join(failed))


def test_export_rows() -> bool:
    fields = {"s1": [model.CustomField("f1", "Discipline")], "s2": [model.CustomField("f9", "discipline"),
                                                                    model.CustomField("f8", "Depth")]}
    comments = [
        {"comment_id": "a", "set_id": "s1", "ref_no": 1, "text": "One", "ref_kind": model.REF_ROUTE_POINT,
         "kp_km": 1.0, "kp_end_km": None, "custom_json": model.dump_custom({"f1": "Route"}), "wkt": "POINT(1 2)",
         "attachment_count": 2},
        {"comment_id": "b", "set_id": "s2", "ref_no": 1, "text": "Two", "ref_kind": model.REF_KP_RANGE,
         "kp_km": 1.0, "kp_end_km": 2.0, "custom_json": model.dump_custom({"f9": "Geo", "f8": 40}),
         "rpl_name": "R", "rpl_rev": "B"},
    ]
    replies = {"a": [{"created_utc": "2026-10-02T00:00:00Z", "author": "Z", "text": "later"},
                     {"created_utc": "2026-10-01T00:00:00Z", "author": "Y", "text": "first"}]}
    headers, rows = model.export_rows(comments, {"s1": "Set one", "s2": "Set two"}, fields,
                                      {"a": model.COND_CHANGED}, replies)
    col = {h: i for i, h in enumerate(headers)}
    checks = {
        "custom columns merged by name": headers[-2:] == ["Discipline", "Depth"],
        "custom values": rows[0][col["Discipline"]] == "Route" and rows[1][col["Discipline"]] == "Geo"
        and rows[1][col["Depth"]] == "40",
        "set names": rows[1][col["Comment set"]] == "Set two",
        "range end only on ranges": rows[0][col["KP to"]] is None and rows[1][col["KP to"]] == 2.0,
        "condition": rows[0][col["Reference condition"]] == "Route changed",
        "replies chronological": rows[0][col["Reply text (export only)"]].startswith("2026-10-01 Y: first"),
        "counts": rows[0][col["Replies"]] == 2 and rows[0][col["Images"]] == 2,
        "route": rows[1][col["Route"]] == "R rev B",
    }
    failed = [k for k, v in checks.items() if not v]
    return _result("export rows: every column, custom fields of every set", not failed, ", ".join(failed))


def run_all() -> list:
    return [
        test_gpkg_structure_and_geometry(),
        test_sets_comments_numbering(),
        test_batch_is_atomic_and_validated(),
        test_versioned_save_and_second_connection(),
        test_replies_and_attachments(),
        test_custom_fields(),
        test_reopen_adds_missing_columns(),
        test_busy_register_message(),
        test_register_is_not_wal(),
        test_choices_search_filters(),
        test_parsers(),
        test_import_validation(),
        test_export_rows(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
