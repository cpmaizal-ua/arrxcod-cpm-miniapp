
"""Inject plates.json into CPM1 account (sync, uses cpm1_core.nuker)."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent
DEFAULT_PLATES = ROOT / "plates.json"


def normalize_plate(p: dict) -> dict:
    if not isinstance(p, dict):
        return {"plateId": 0, "frontCarId": 0, "rearCarId": 0, "vinyls": []}
    vinyls = []
    for v in p.get("vinyls") or []:
        if not isinstance(v, dict):
            continue
        vinyls.append({
            "vectors": v.get("vectors") or [],
            "v": v.get("v") or [],
            "floats": v.get("floats") or [],
            "text": str(v.get("text") or ""),
        })
    return {
        "plateId": int(p.get("plateId") or 0),
        "frontCarId": int(p.get("frontCarId") or 0),
        "rearCarId": int(p.get("rearCarId") or 0),
        "vinyls": vinyls,
    }


def load_plates(path: Path = None) -> Dict[str, Any]:
    path = path or DEFAULT_PLATES
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {"allPlates": [normalize_plate(p) for p in data]}
    if isinstance(data, dict) and "allPlates" in data:
        return {"allPlates": [normalize_plate(p) for p in data["allPlates"]]}
    return {"allPlates": []}


def inject_plates(email: str, password: str, merge: bool = True, plates_path: Path = None) -> Dict[str, Any]:
    from cpm1_core import nuker
    plates_data = load_plates(plates_path)
    incoming = plates_data.get("allPlates") or []
    if not incoming:
        return {"ok": False, "message": "No plates in plates.json"}
    lr = nuker.login(email, password)
    if not lr.get("ok"):
        return {"ok": False, "message": lr.get("message", "login failed")}
    uid = abs(hash(email + ":plates")) % (10**9)
    nuker.save_token(uid, lr["auth"], email, password, lr.get("refresh_token", ""), lr.get("firebase_uid", ""))
    if not nuker.load(uid, force=True):
        return {"ok": False, "message": "Could not load account"}
    record = nuker.get_record(uid, email) or {}
    current = record.get("platesData") if isinstance(record.get("platesData"), dict) else {"allPlates": []}
    if merge:
        by_id = {}
        for p in current.get("allPlates") or []:
            if isinstance(p, dict):
                by_id[int(p.get("plateId") or 0)] = normalize_plate(p)
        for p in incoming:
            by_id[int(p.get("plateId") or 0)] = p
        final = {"allPlates": list(by_id.values())}
    else:
        final = {"allPlates": incoming}
    res = nuker._modify(uid, {"platesData": final}, force_fields={"platesData"})
    if res.get("ok"):
        res["message"] = "OK — %d plate(s) injected" % len(incoming)
        res["injected"] = len(incoming)
        res["total_on_account"] = len(final["allPlates"])
    return res
