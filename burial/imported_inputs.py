# -*- coding: utf-8 -*-
"""Input-register summaries derived from persisted imports, with no extra writes.

Horizons belong to a plan; CPT campaigns belong to the project. Keeping these
rows derived means removal, rollback, copied plans and older imports agree with
the register, and embedded readings remain available without the original files.
"""

import json
from collections import Counter


def horizon_inputs(rows, notice=None, annotations=None):
    groups = {}
    for row in rows + (annotations or []):
        source = row.get("source") or {}
        key = source.get("import_id") or (source.get("file", ""), source.get("imported_utc", ""))
        group = groups.setdefault(key, {"source": source, "names": [], "vertices": 0,
                                        "horizons": 0, "annotations": 0, "notices": set()})
        group["names"].append(row["name"])
        if "labels" in row:
            group["annotations"] += len(row["labels"])
        else:
            group["horizons"] += 1
            group["vertices"] += sum(len(s) for s in row["segments"])
        message = notice(row) if notice else ""
        if message:
            group["notices"].add(message)
    out = []
    for group in groups.values():
        source = group["source"]
        detail = (f"Saved in this plan: {group['horizons']} horizon(s), {group['vertices']:,} working vertices, "
                  f"{group['annotations']} annotation(s).\n"
                  f"Imported: {source.get('imported_utc') or 'not recorded'}\n"
                  "Manage in Ground Model → Horizons. Original DXF is not needed to display saved picks.")
        if source.get("sha256"):
            detail += "\nSource SHA-256: " + source["sha256"]
        if group["notices"]:
            detail += "\n" + "\n".join(sorted(group["notices"]))
        out.append({"role": "DXF horizons" if group["horizons"] else "DXF annotations", "layer_name": source.get("file") or "Imported horizons",
                    "layer_source": "\n".join(group["names"]),
                    "register_state": "check" if group["notices"] else "stored",
                    "register_detail": detail, "register_users": ["Ground Model horizon overlays (this plan)"]})
    return out


def cpt_inputs(campaigns, cpts):
    counts = Counter(c.get("campaign_id") for c in cpts)
    out = []
    for campaign in campaigns:
        try:
            config = json.loads(campaign.get("config_json") or "{}")
            files = [str(f.get("path") or "") for f in config.get("files", [])]
        except (ValueError, TypeError, AttributeError):
            files = []
        superseded = campaign.get("status") == "superseded"
        detail = (f"{counts[campaign.get('campaign_id')]} CPT(s) stored in the project, shared by all plans.\n"
                  f"Imported: {campaign.get('imported_utc') or 'not recorded'}\n"
                  "Manage in Ground Model → CPTs. Original files are needed only for re-import."
                  + ("\nSuperseded: excluded from current CPT displays and datasets." if superseded else ""))
        out.append(dict(campaign, role="CPT campaign", layer_name=campaign.get("name") or "CPT import",
                        layer_source="\n".join(files), register_state="superseded" if superseded else "stored",
                        register_detail=detail, register_users=[] if superseded else ["Ground Model CPT register (project)"]))
    return out
