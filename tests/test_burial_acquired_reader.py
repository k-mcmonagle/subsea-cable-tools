# -*- coding: utf-8 -*-
"""Acquired burial data: roles, import profiles and the table reader (numpy, no QGIS).

* Roles: header aliases, units from headers / a units line, tension names,
  sign conventions.
* Profiles: plain-language problems (KP and DOL needed, one column per
  role, position pairs, CRS, time combinations, UTC offsets); Suggest on
  the sample plough log and the ROV re-survey's other house style.
* Reader: sniffing (delimiter, header and units lines, decimal comma), the
  day file's bad cells (missing codes kept as NaN, '#VALUE!' and a line cut
  short rejected with reasons and source lines), units and sign to stored
  units, date + time-of-day columns with a UTC offset, degrees-minutes with
  decimal commas, time formats (ISO with offsets, patterns with day/month
  ambiguity, day of year, epoch), ragged rows, extra channels, XLSX.
* Samples: positions follow the plugin's KP convention.
"""

from __future__ import annotations

import io
import math
from datetime import datetime, timezone

import numpy as np

from ..burial.acquired import profile as P
from ..burial.acquired import reader
from ..burial.acquired import roles as R
from ..burial.acquired import samples as S

REQUIRES_QGIS = False


def _result(name: str, ok: bool, detail: str = "") -> bool:
    ok = bool(ok)    # the runner wants plain bools (numpy comparisons give numpy.bool_)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def _utc(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def _check(name: str, checks: dict) -> bool:
    failed = [k for k, v in checks.items() if not v]
    return _result(name, not failed, ", ".join(failed))


_RUN = None


def _run():
    global _RUN
    if _RUN is None:
        _RUN = S.plough_run(rate_hz=0.1)
    return _RUN


def _plough_profile(content: bytes, name: str) -> dict:
    table = reader.sniff(content, name)
    raw = reader.read_table(content, name, table, max_rows=5)
    prof = P.new_profile("Plough")
    prof["table"] = table
    prof["columns"] = P.suggest(raw.headers, raw.units)
    prof["crs"] = f"EPSG:{S.UTM_EPSG}"
    return prof


def test_roles() -> bool:
    return _check("roles: aliases, units from text, tension names, signs", {
        "kp": R.match_role("KP (km)") == R.KP and R.match_role("Chainage_m") == R.KP,
        "dol": R.match_role("DOL [m]") == R.DOL and R.match_role("Burial Depth") == R.DOL,
        "time": R.match_role("Date Time") == R.TIME and R.match_role("Date") == R.DATE,
        "tension": R.match_role("Tow Tension (kN)") == R.TENSION and R.match_role("Cable Tension") == R.TENSION,
        "unknown": R.match_role("Skid height") is None,
        "kp unit m": R.unit_from_text(R.KP, "KP [m]") == "m" and R.unit_from_text(R.KP, "km") == "km",
        "kn speed": R.unit_from_text(R.SPEED, "kn") == "kn" and R.unit_from_text(R.TENSION, "kN") == "kN",
        "tonnes": R.unit_from_text(R.TENSION, "Tension_t") == "t",
        "degrees": R.unit_from_text(R.PITCH, "°") == "deg",
        "dol cm": R.unit_from_text(R.DOL, "DOL [cm]") == "cm",
        "tension name": R.tension_name("TowTension (kN)") == "Tow" and R.tension_name("Tension") == "Tension",
        "channel keys": R.channel_key(R.TENSION, "Tow") == "tension:tow" and R.channel_key(R.KP) == "kp",
        "signs": R.sign_factor(R.DOL, "up") == -1.0 and R.sign_factor(R.DCC, "port") == -1.0
        and R.sign_factor(R.DCC, "unsigned") == 1.0 and R.sign_factor(R.PITCH, "up") == 1.0,
    })


def test_profile_problems() -> bool:
    empty = P.problems(P.new_profile())
    base = P.new_profile()
    base["columns"] = [P.column("K", R.KP), P.column("D", R.DOL)]
    dup = P.normalise(base)
    dup["columns"].append(P.column("K2", R.KP))
    lat_only = P.normalise(base)
    lat_only["columns"].append(P.column("La", R.LAT))
    en = P.normalise(base)
    en["columns"] += [P.column("E", R.EASTING), P.column("N", R.NORTHING)]
    clock = P.normalise(base)
    clock["columns"].append(P.column("T", R.CLOCK))
    both = P.normalise(base)
    both["columns"] += [P.column("TS", R.TIME), P.column("Da", R.DATE), P.column("T", R.CLOCK)]
    day = P.normalise(base)
    day["columns"] += [P.column("Dn", R.DAY), P.column("T", R.CLOCK)]
    names = P.normalise(base)
    names["columns"] += [P.column("A", R.TENSION, name="Tow"), P.column("B", R.TENSION, name="tow")]
    zone = P.normalise(base)
    zone["timezone"] = "Europe/London"
    return _check("profiles: plain-language problems", {
        "needs kp and dol": empty == ["Map a KP column.", "Map a DOL column."],
        "ok": P.problems(base) == [] and P.status(base) == {"kp": True, "dol": True, "time": False,
                                                            "position": False},
        "one column per role": any("more than one column" in p for p in P.problems(dup)),
        "pair": any("Latitude needs Longitude" in p for p in P.problems(lat_only)),
        "crs": any("CRS" in p for p in P.problems(en)),
        "clock alone": any("Time of day needs" in p for p in P.problems(clock)),
        "timestamp or parts": any("either Timestamp" in p for p in P.problems(both)),
        "day start": any("start date" in p for p in P.problems(day)),
        "names collide": any("different names" in p for p in P.problems(names)),
        "named zone refused": any("UTC offset" in p for p in P.problems(zone)),
        "header missing": any("not in this file" in p for p in P.problems(base, ["K"])),
        "offsets": P.parse_utc_offset("UTC+01:00") == 3600 and P.parse_utc_offset("-0330") == -12600
        and P.parse_utc_offset("Z") == 0 and P.parse_utc_offset("+15:00") is None,
        "json round trip": P.from_json(P.to_json(base)) == P.normalise(base),
    })


def test_suggest() -> bool:
    run = _run()
    name, content = S.daily_files(run)[0]
    prof = _plough_profile(content, name)
    plough_headers = reader.read_table(content, name, prof["table"], max_rows=1).headers
    roles = {c["header"]: (c["role"], c.get("unit"), c.get("name")) for c in prof["columns"]}
    _n, resurvey = S.rov_resurvey()
    table = reader.sniff(resurvey, "x.txt")
    raw = reader.read_table(resurvey, "x.txt", table, max_rows=3)
    other = {c["header"]: (c["role"], c.get("unit")) for c in P.suggest(raw.headers, raw.units)}
    return _check("suggest: plough log and another house style", {
        "time": roles["Date Time"][0] == R.TIME,
        "kp km": roles["KP"][:2] == (R.KP, "km"),
        "speed from units line": roles["Speed"][:2] == (R.SPEED, "m/s"),
        "tensions named": roles["Tow Tension"] == (R.TENSION, "kN", "Tow")
        and roles["Cable Tension"] == (R.TENSION, "kN", "Cable"),
        "event": roles["Event"][0] == R.EVENT,
        "everything mapped": len(roles) == len(S.PLOUGH_HEADERS),
        "plough reads": P.matches(prof, plough_headers) and not P.matches(prof, raw.headers),
        "date + time of day": other["Date"][0] == R.DATE and other["Time"][0] == R.CLOCK,
        "kp m, dol cm": other["KP [m]"] == (R.KP, "m") and other["DOL [cm]"] == (R.DOL, "cm"),
        "wd": other["WD [m]"][0] == R.WATER_DEPTH and other["Remarks"][0] == R.EVENT,
    })


def test_sniff() -> bool:
    run = _run()
    name, content = S.daily_files(run)[0]
    plough = reader.sniff(content, name)
    _n, foreign = S.rov_resurvey()
    other = reader.sniff(foreign, "x.txt")
    _n, survey = S.survey_listing(step_m=5.0)
    listing = reader.sniff(survey, "s.csv")
    titled = reader.sniff(b"Burial listing for cable S1\r\n\r\nKP,DOL\r\nkm,m\r\n1.000,1.2\r\n1.001,1.3\r\n", "t.csv")
    spaced = reader.sniff(b"KP   DOL   WD\n0.001  1.20  40.1\n0.002  1.25  40.2\n", "s.txt")
    return _check("sniff: delimiter, header, units line, decimal comma", {
        "plough": (plough["delimiter"], plough["header_line"], plough["units_line"], plough["decimal_comma"])
        == (",", 1, 2, False),
        "foreign": (other["delimiter"], other["header_line"], other["units_line"], other["decimal_comma"])
        == (";", 1, 0, True),
        "listing": (listing["delimiter"], listing["header_line"], listing["units_line"]) == (",", 1, 0),
        "title above header": (titled["header_line"], titled["units_line"]) == (3, 4),
        "whitespace": spaced["delimiter"] == "whitespace",
    })


def test_parse_plough_day() -> bool:
    run = _run()
    files = S.daily_files(run)
    name, content = files[1]                       # day 2: the damaged day
    prof = _plough_profile(content, name)
    raw, data = reader.read(content, name, prof)
    reasons = [r[1] for r in data.rejected]
    start = math.floor(run.time[0] / 86400.0) * 86400.0 + 86400.0
    rows = np.flatnonzero((run.time >= start) & (run.time < start + 86400.0 + 600.0))
    first_line_ok = data.line[0] == 3                # header line 1, units line 2
    tow = data.columns.get("tension:tow")
    return _check("parse: plough day with bad cells", {
        "rows read": data.rows_read == rows.size,
        "three rejected": len(data.rejected) == 3,
        "value reasons": sum("'#VALUE!' is not a number" in r for r in reasons) == 2,
        "cut line": sum(r.startswith("row ends early: 9 of 15") for r in reasons) == 1,
        "missing kept as NaN": int(np.isnan(data.columns[R.DOL]).sum()) == 3,
        "kept": data.n == rows.size - 3,
        "first line": first_line_ok,
        "time UTC": abs(data.columns[R.TIME][0] - run.time[rows[0]]) < 0.5 and data.time_format == "iso",
        "kp": np.allclose(data.columns[R.KP][:5], np.round(run.kp[rows[:5]], 4)),
        "tensions": tow is not None and "tension:cable" in data.columns,
        "event text": len(data.texts[R.EVENT]) == data.n,
        "channel units": data.channel(R.SPEED).unit == "m/s" and data.channel("tension:tow").label == "Tow tension",
        "no warnings": not any("negative" in w for w in data.warnings),
        "rejected keeps text": all(r[2] for r in data.rejected) and all(r[0] > 2 for r in data.rejected),
    })


def test_parse_other_house_style() -> bool:
    route = S.SyntheticRoute()
    name, content = S.rov_resurvey(route)
    table = reader.sniff(content, name)
    raw = reader.read_table(content, name, table)
    prof = P.new_profile()
    prof["table"] = table
    prof["columns"] = P.suggest(raw.headers, raw.units)
    prof["timezone"] = "+01:00"
    data = reader.parse(raw, prof)
    kp = np.arange(3000.0, 3800.0 + 1e-6, 0.5) / 1000.0
    lon, lat = route.position(kp, 0.0)
    expected_t0 = _utc(S.RESURVEY_START_UTC) - 3600.0
    lat_err = np.abs(data.columns[R.LAT] - lat).max() * 111000.0
    return _check("parse: semicolons, decimal commas, KP m, DOL cm, date + time, UTC offset", {
        "all rows": data.n == kp.size and not data.rejected,
        "kp to km": np.allclose(data.columns[R.KP], kp),
        "dol cm to m": 0.5 < np.nanmedian(data.columns[R.DOL]) < 3.0,
        "degrees-minutes": lat_err < 1.0 and np.abs(data.columns[R.LON] - lon).max() * 64000.0 < 1.5,
        "date + time - offset": abs(data.columns[R.TIME][0] - expected_t0) < 1e-6,
        "two seconds apart": np.allclose(np.diff(data.columns[R.TIME]), 2.0),
        "remarks padded": len(data.texts[R.EVENT]) == data.n,
    })


def test_time_formats() -> bool:
    iso = ["2026-10-20T00:00:00Z", "2026-10-20 01:00:00+01:00", "2026-10-20T00:00:30.5"]
    v_iso, bad_iso, f_iso, _a = reader.parse_timestamps(iso, "auto", 0)
    dm = ["03/04/2026 10:00:00", "04/04/2026 10:00:00"]
    v_dm, _b, f_dm, amb = reader.parse_timestamps(dm, "auto", 0)
    md = ["04/13/2026 10:00:00"]
    v_md, _b, f_md, amb_md = reader.parse_timestamps(md, "auto", 0)
    jd = ["2026:293:12:00:00"]
    v_jd, _b, f_jd, _a = reader.parse_timestamps(jd, "auto", 0)
    ep = ["1792800000", "1792800001.5"]
    v_ep, _b, f_ep, _a = reader.parse_timestamps(ep, "auto", 0)
    ms = ["1792800000000"]
    v_ms, _b, f_ms, _a = reader.parse_timestamps(ms, "auto", 0)
    frac = ["20/10/2026 12:00:00.250"]
    v_fr, bad_fr, _f, _a = reader.parse_timestamps(frac, "%d/%m/%Y %H:%M:%S", 3600)
    clock, bad_clock = reader.parse_clock(["23:59:59.5", "000130", "25:00", "n/a"])
    return _check("time formats", {
        "iso offsets": f_iso == "iso" and not bad_iso.any() and v_iso[0] == v_iso[1] == _utc("2026-10-20T00:00:00")
        and v_iso[2] == _utc("2026-10-20T00:00:30.5"),
        "day first, ambiguous": f_dm.startswith("%d/%m/%Y") and amb and v_dm[0] == _utc("2026-04-03T10:00:00"),
        "month first when it must be": f_md.startswith("%m/%d/%Y") and not amb_md
        and v_md[0] == _utc("2026-04-13T10:00:00"),
        "day of year": f_jd == "%Y:%j:%H:%M:%S" and v_jd[0] == _utc("2026-10-20T12:00:00"),
        "epoch": f_ep == "epoch" and v_ep[1] == 1792800001.5,
        "epoch ms": f_ms == "epoch_ms" and v_ms[0] == 1792800000.0,
        "fraction + offset": not bad_fr.any() and v_fr[0] == _utc("2026-10-20T11:00:00.25"),
        "clock": clock[0] == 86399.5 and clock[1] == 90.0 and bad_clock[2] and not bad_clock[3]
        and math.isnan(clock[3]),
    })


def test_numbers_and_coordinates() -> bool:
    values, bad = reader.parse_numbers(["1.5", " 2 ", "", "n/a", "-9999", "x", "inf", "1e3"])
    comma, bad_c = reader.parse_numbers(["1,5", "-0,25", "3"], decimal_comma=True)
    coords = [reader.parse_coordinate(t) for t in ("55.5", "55°30.0'N", "N55 30 00", "003-07.20W", "3 07 12 W",
                                                   "55°60.0'N", "12.3 N S")]
    lat, bad_lat = reader.parse_coordinates(["91.0", "54.5"], False, P.DEFAULT_MISSING, 90.0)
    return _check("numbers, missing codes, coordinates", {
        "numbers": values[0] == 1.5 and values[1] == 2.0 and values[7] == 1000.0,
        "missing": all(math.isnan(values[i]) for i in (2, 3, 4)) and not bad[2:5].any(),
        "bad": bad[5] and bad[6],
        "decimal comma": comma.tolist() == [1.5, -0.25, 3.0] and not bad_c.any(),
        "coordinates": coords[:3] == [55.5, 55.5, 55.5] and abs(coords[3] + 3.12) < 1e-12
        and abs(coords[4] + 3.12) < 1e-12,
        "rejects": math.isnan(coords[5]) and math.isnan(coords[6]),
        "range": bad_lat[0] and not bad_lat[1] and math.isnan(lat[0]),
    })


def test_ragged_rows_and_extras() -> bool:
    rows = ["1.000,1.2,0.10,ok,5,a",
            "1.001,1.3,0.11",                    # ends early, only text/extra columns missing
            "1.002",                             # ends early inside mapped numbers
            "1.003,1.4,0.12,ok,6,b,7",           # one value too many
            "1.004,1.5,0.13,ok,abc,c"]           # a stray text cell in a numeric column
    rows += [f"1.{5 + i:03d},1.6,0.14,ok,{i},d" for i in range(10)]
    text = ("KP,DOL,Skid height,Note,Mixed,Code\r\n" + "\r\n".join(rows) + "\r\n").encode()
    prof = P.new_profile()
    prof["table"] = reader.sniff(text, "r.csv")
    prof["columns"] = [P.column("KP", R.KP), P.column("DOL", R.DOL), P.column("Note", R.EVENT)]
    raw, data = reader.read(text, "r.csv", prof)
    reasons = {r[0]: r[1] for r in data.rejected}
    return _check("ragged rows and extra channels", {
        "kept": data.line.tolist()[:3] == [2, 3, 6] and data.n == 13,
        "early": reasons.get(4, "").startswith("row ends early"),
        "too many": reasons.get(5, "").startswith("7 values where the header has 6"),
        "extra kept": "extra:skid_height" in data.columns and data.channel("extra:skid_height").auto,
        "stray text is NaN": "extra:mixed" in data.columns and math.isnan(data.columns["extra:mixed"][2]),
        "text column not kept": "extra:code" not in data.columns,
        "padded event": data.texts[R.EVENT][:3] == ["ok", "", "ok"],
    })


def test_xlsx() -> bool:
    try:
        import openpyxl
    except ImportError:
        return _result("xlsx (openpyxl not available: skipped)", True)
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Burial"
    sheet.append(["Burial data"])
    sheet.append([])
    sheet.append(["Time", "KP", "DOL"])
    sheet.append([datetime(2026, 10, 20, 12, 0, 0), 1.0, 1.25])
    sheet.append([datetime(2026, 10, 20, 12, 0, 1), 1.0005, None])
    buffer = io.BytesIO()
    book.save(buffer)
    content = buffer.getvalue()
    table = reader.sniff(content, "b.xlsx")
    raw = reader.read_table(content, "b.xlsx", table)
    prof = P.new_profile()
    prof["table"] = table
    prof["columns"] = P.suggest(raw.headers)
    data = reader.parse(raw, prof)
    return _check("xlsx through the bundled reader", {
        "header row": table["header_line"] == 3 and raw.sheet == "Burial",
        "rows": data.n == 2 and data.line.tolist() == [4, 5],
        "datetime cells": data.columns[R.TIME][1] - data.columns[R.TIME][0] == 1.0
        and data.columns[R.TIME][0] == _utc("2026-10-20T12:00:00"),
        "empty cell is missing": math.isnan(data.columns[R.DOL][1]),
    })


def test_sample_kp_convention() -> bool:
    route = S.SyntheticRoute()
    kp = np.array([0.0, 1.234, 10.0, route.length_km - 0.001])
    lon, lat = route.position(kp)
    errors = []
    for k, x, y in zip(kp, lon, lat):
        # The plugin's KP path: the geodesic between vertices, KP the distance along it.
        seg = int(np.clip(np.searchsorted(route.start_m, k * 1000.0, side="right") - 1, 0, len(route.segment_m) - 1))
        v0 = route.vertices[seg]
        along = route.start_m[seg] + S.geodesic_m(v0[0], v0[1], x, y)
        errors.append(abs(along - k * 1000.0))
    end_lon, end_lat, _az = S.geodesic_direct(2.8, 54.9, 33.0, 12345.678)
    back, back_azimuth = S.geodesic_inverse(2.8, 54.9, float(end_lon), float(end_lat))
    offset_lon, offset_lat = route.position(np.array([5.0]), 10.0)
    base_lon, base_lat = route.position(np.array([5.0]))
    across = S.geodesic_m(base_lon[0], base_lat[0], offset_lon[0], offset_lat[0])
    e, n = S.utm(np.array([3.0, 2.0, 4.0]), np.array([55.0, 55.0, 55.0]))
    dol = S.dol_profile(np.array([3.4, 14.36, 10.0]))
    return _check("samples: KP convention, DCC offset, UTM symmetry, hard ground", {
        "kp along the geodesic legs": max(errors) < 0.001,
        "direct and inverse agree": abs(back - 12345.678) < 1e-4 and abs(back_azimuth - 33.0) < 1e-8,
        "dcc offset 10 m": abs(across - 10.0) < 0.05,
        "starboard is right of travel": offset_lon[0] > base_lon[0],
        "central meridian": abs(e[0] - 500000.0) < 1e-6 and abs((e[1] + e[2]) / 2 - 500000.0) < 1e-6,
        "hard ground shallow": dol[0] < S.TARGET_DOL_M and dol[1] < S.TARGET_DOL_M and dol[2] > S.TARGET_DOL_M,
        "route about 33 km": 30.0 < route.length_km < 36.0,
    })


def run_all() -> list:
    return [
        test_roles(),
        test_profile_problems(),
        test_suggest(),
        test_sniff(),
        test_parse_plough_day(),
        test_parse_other_house_style(),
        test_time_formats(),
        test_numbers_and_coordinates(),
        test_ragged_rows_and_extras(),
        test_xlsx(),
        test_sample_kp_convention(),
    ]


if __name__ == "__main__":
    raise SystemExit(0 if all(run_all()) else 1)
