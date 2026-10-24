from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import uuid, re, struct, base64, zlib
# Clone helpers extracted from MARKCPM1TOOLS
from cpm1_core import *
import threading, time, json, html, random, string, uuid, re, struct, base64, zlib
try:
    import brotli
except ImportError:
    brotli = None
from typing import Any, Dict, List, Optional
import math
import time

# Parallel cloning
CLONE_WORKERS = 20          # bulk account workers
CAR_CLONE_WORKERS = 20      # maximum parallel car saves per account
BULK_CLONE_LIMIT_USER = 300
BULK_CLONE_LIMIT_ADMIN = 500

SOURCE_ACCOUNT = ('vssesg1790117265x5ja@gmail.com', 'AB31cx18wW96tA')

CPM_CARS_FETCH_URL = "https://europe-west1-cp-multiplayer.cloudfunctions.net/GetAllCars2"
CPM_CARS_SAVE_URL = "https://europe-west1-cp-multiplayer.cloudfunctions.net/SaveCarsPartially8"

CPM_FIELD_NAMES = {
    1: 'CarID', 2: 'dataVersion', 3: 'vectors', 4: 'floats', 5: 'gears',
    6: 'typeToInstall', 7: 'BoughtParts', 8: 'texts', 9: 'flagID',
    10: 'fsoData', 11: 'installedPoliceLights', 12: 'Vynils', 13: 'WindowVinyls',
}

def cpm1_ios_headers(token):
    return {
        'accept': '*/*',
        'authorization': f'Bearer {token}',
        'content-type': 'application/json; charset=utf-8',
        'user-agent': 'CarParking/265 CFNetwork/3860.600.12 Darwin/25.5.0',
        'x-client-platform': 'IOS',
        'x-client-version': '4.9.10',
        'x-client-deviceid': str(uuid.uuid4()).upper(),
        'x-request-nonce': str(uuid.uuid4()),
        'x-unity-version': '2022.3.62f2',
        'accept-encoding': 'gzip',
        'connection': 'Keep-Alive',
    }

def cpm1_write_memorypack_string(s):
    if s is None or s == "": return struct.pack("<i", 0)
    sb = str(s).encode("utf-8")
    return struct.pack("<ii", -len(sb) - 1, len(sb)) + sb

def cpm1_looks_like_base64(val):
    if not isinstance(val, str): return False
    if len(val) < 4 or len(val) % 4 != 0: return False
    return bool(re.match(r'^[A-Za-z0-9+/]+={0,2}$', val))

def cpm1_deserialize_string_list(b64):
    if not b64 or not isinstance(b64, str): return []
    try: compressed = base64.b64decode(b64)
    except: return []
    try: decompressed = brotli.decompress(compressed)
    except:
        try: decompressed = zlib.decompress(compressed, zlib.MAX_WBITS | 16)
        except: decompressed = compressed
    if not decompressed or len(decompressed) < 4: return []
    out = []
    pos = 0
    count = struct.unpack_from("<i", decompressed, 0)[0]
    pos = 4
    if count <= 0 or count > 100000: return []
    for _ in range(count):
        if pos + 4 > len(decompressed): break
        marker = struct.unpack_from("<i", decompressed, pos)[0]
        pos += 4
        if marker == 0:
            out.append("")
            continue
        if marker == -1: break
        if marker < -1:
            byte_len = -marker - 1
            if pos + 4 > len(decompressed): break
            pos += 4
        else:
            byte_len = marker
        if pos + byte_len > len(decompressed): break
        s = decompressed[pos:pos+byte_len].decode("utf-8", errors="replace")
        pos += byte_len
        out.append(s.replace("\x00", "").strip())
    return out

def cpm1_decode_car_array(value, kind):
    """Decode CPM's base64+Brotli packed numeric arrays returned by GetAllCars2."""
    if isinstance(value, list):
        return value[:]
    if value is None:
        return []
    if not isinstance(value, str) or not cpm1_looks_like_base64(value):
        raise ValueError("unsupported encoded car field")
    packed = base64.b64decode(value, validate=True)
    try:
        raw = brotli.decompress(packed) if brotli else zlib.decompress(packed)
    except Exception:
        raw = zlib.decompress(packed)
    if len(raw) < 4:
        raise ValueError("packed car field is truncated")
    count = struct.unpack_from("<i", raw, 0)[0]
    if count < 0 or count > 10000:
        raise ValueError("packed car field has invalid item count")
    if kind == "vectors":
        stride = 12
        if len(raw) != 4 + count * stride:
            raise ValueError("packed vector field length does not match count")
        return [dict(zip(("x", "y", "z"), struct.unpack_from("<fff", raw, 4 + i * stride))) for i in range(count)]
    if kind == "floats":
        stride = 4
        if len(raw) != 4 + count * stride:
            raise ValueError("packed float field length does not match count")
        return [struct.unpack_from("<f", raw, 4 + i * stride)[0] for i in range(count)]
    if kind == "ints":
        stride = 4
        if len(raw) != 4 + count * stride:
            raise ValueError("packed int field length does not match count")
        return [struct.unpack_from("<i", raw, 4 + i * stride)[0] for i in range(count)]
    raise ValueError("unsupported car array kind")


def cpm1_decode_car_field(car, field):
    """Decode one packed CPM numeric field in-place on a copied car object."""
    kinds = {
        "vectors": "vectors", "floats": "floats", "gears": "floats",
        "typeToInstall": "ints", "BoughtParts": "ints", "fsoData": "ints",
        "installedPoliceLights": "ints",
    }
    if field not in kinds:
        raise ValueError("unsupported car field: %s" % field)
    car[field] = cpm1_decode_car_array(car.get(field), kinds[field])
    return car[field]


def cpm1_is_readonly_cpm_id(v):
    if not isinstance(v, str): return False
    x = re.sub(r"\[[0-9A-Fa-f]{6}\]", "", v.strip()).upper()
    return len(x) == 8 and bool(re.match(r'^[A-Z]{2}\d{6}$', x))

def cpm1_generate_random_cpm_id():
    L = string.ascii_uppercase
    D = string.digits
    return ''.join(random.choice(L) for _ in range(2)) + ''.join(random.choice(D) for _ in range(6))

def cpm1_resolve_cpm_id(record):
    val = (record or {}).get("localID")
    if not val or not cpm1_is_readonly_cpm_id(val): val = cpm1_generate_random_cpm_id()
    return re.sub(r"\[[0-9A-Fa-f]{6}\]", "", str(val).strip()).upper()

def cpm1_generated_id_from_car(car):
    for k in ("generatedID", "generatedId", "carGeneratedID", "carGeneratedId"):
        v = car.get(k)
        if v and isinstance(v, str): return v
    texts = car.get("texts")
    if isinstance(texts, str):
        try: texts = cpm1_deserialize_string_list(texts)
        except: texts = []
    if isinstance(texts, list):
        for v in texts:
            if v and isinstance(v, str) and '_' in v and re.search(r'\d', v): return v
    return None

def cpm1_merge_cars_by_id(*car_lists):
    merged = {}
    for lst in car_lists:
        if not isinstance(lst, list): continue
        for car in lst:
            if not isinstance(car, dict): continue
            try: cid = int(car.get("CarID"))
            except: continue
            if cid < 0: continue
            merged[cid] = car
    return [merged[k] for k in sorted(merged.keys())]

def cpm1_replace_uid_in_value(value, src_uid, tgt_uid):
    if not src_uid or src_uid == tgt_uid: return value
    if isinstance(value, str):
        if cpm1_looks_like_base64(value): return value
        return value.replace(str(src_uid), str(tgt_uid))
    if isinstance(value, list): return [cpm1_replace_uid_in_value(v, src_uid, tgt_uid) for v in value]
    if isinstance(value, dict): return {k: cpm1_replace_uid_in_value(v, src_uid, tgt_uid) for k, v in value.items()}
    return value


def cpm1_normalize_car_physics(car):
    """Stop flying / fling / chrome-tint after clone or unlock.
    - NaN/Inf -> 0
    - Clamp absurd floats (suspension / height)
    - Soft-clamp vectors (keep normal paint 0..1)
    - Force near-ground if Y offset absurd
    - Preserve vinyl colors as uint32
    - Do NOT force floats[0] (siren) here — separate path
    """
    if not isinstance(car, dict):
        return car

    floats = car.get("floats")
    if isinstance(floats, list):
        out = []
        for i, v in enumerate(floats):
            try:
                f = float(v)
            except Exception:
                f = 0.0
            if math.isnan(f) or math.isinf(f):
                f = 0.0
            if abs(f) > 1e4:
                f = max(-1e4, min(1e4, f))
            out.append(f)
        # ride height / suspension-ish indices — keep modest
        for idx in (5, 6, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 35, 36, 37, 38, 39, 43):
            if idx < len(out):
                if abs(out[idx]) > 80:
                    out[idx] = max(-20.0, min(20.0, out[idx]))
        # mileage-ish index 19 sometimes huge → clamp
        if len(out) > 19 and abs(out[19]) > 1e6:
            out[19] = 0.0
        car["floats"] = out

    gears = car.get("gears")
    if isinstance(gears, list):
        gout = []
        for v in gears:
            try:
                f = float(v)
            except Exception:
                f = -10.0
            if math.isnan(f) or math.isinf(f):
                f = -10.0
            if abs(f) > 50:
                f = max(-50.0, min(50.0, f))
            gout.append(f)
        car["gears"] = gout

    vectors = car.get("vectors")
    if isinstance(vectors, list):
        vout = []
        for idx, vec in enumerate(vectors):
            if not isinstance(vec, dict):
                vout.append({"x": 0.0, "y": 0.0, "z": 0.0})
                continue
            try:
                x = float(vec.get("x") if vec.get("x") is not None else 0)
            except Exception:
                x = 0.0
            try:
                y = float(vec.get("y") if vec.get("y") is not None else 0)
            except Exception:
                y = 0.0
            try:
                z = float(vec.get("z") if vec.get("z") is not None else 0)
            except Exception:
                z = 0.0
            if math.isnan(x) or math.isinf(x):
                x = 0.0
            if math.isnan(y) or math.isinf(y):
                y = 0.0
            if math.isnan(z) or math.isinf(z):
                z = 0.0

            def soft(v, paint_ok=True):
                # normal paint / small scale
                if abs(v) <= 5.0:
                    return v
                if abs(v) > 200:
                    return max(-30.0, min(30.0, v))
                if abs(v) > 50:
                    return max(-15.0, min(15.0, v))
                return v

            # first few vectors often position/scale — harder clamp on Y (flying)
            if idx <= 2 and abs(y) > 10:
                y = max(-2.0, min(2.0, y))
            x, y, z = soft(x), soft(y), soft(z)
            vout.append({"x": x, "y": y, "z": z})
        car["vectors"] = vout

    # --- vynils: keep colors / packedData exact ---
    vyn = car.get("Vynils")
    if isinstance(vyn, dict) and isinstance(vyn.get("allVynils"), list):
        fixed = []
        for vinyl in vyn["allVynils"]:
            if not isinstance(vinyl, dict):
                continue
            nv = dict(vinyl)
            for fn in ("position", "scaleRotation", "iconPosition"):
                vec = nv.get(fn)
                if not isinstance(vec, dict):
                    continue
                try:
                    x = float(vec.get("x") if vec.get("x") is not None else 0)
                    y = float(vec.get("y") if vec.get("y") is not None else 0)
                    z = float(vec.get("z") if vec.get("z") is not None else 0)
                except Exception:
                    x = y = z = 0.0
                for a in (x, y, z):
                    pass
                if any(math.isnan(a) or math.isinf(a) for a in (x, y, z)):
                    x = y = z = 0.0
                # absurd scale → chrome/stretch artifacts
                if abs(x) > 500:
                    x = max(-100.0, min(100.0, x))
                if abs(y) > 500:
                    y = max(-100.0, min(100.0, y))
                if abs(z) > 1e6:
                    z = max(-3600.0, min(3600.0, z))
                nv[fn] = {"x": x, "y": y, "z": z}
            # color as full uint32 (do NOT use `or 0` which is fine; avoid float loss)
            try:
                if "color" in nv and nv["color"] is not None:
                    nv["color"] = int(nv["color"]) & 0xFFFFFFFF
            except Exception:
                nv["color"] = 0
            try:
                if "packedData" in nv and nv["packedData"] is not None:
                    nv["packedData"] = int(nv["packedData"])
            except Exception:
                pass
            fixed.append(nv)
        vyn = dict(vyn)
        vyn["allVynils"] = fixed
        try:
            vyn["CarID"] = int(car.get("CarID") or vyn.get("CarID") or 0)
        except Exception:
            pass
        car["Vynils"] = vyn

    # drop non-save junk that can confuse encoder
    for junk in ("source_slot", "sourceSlot", "one_car"):
        car.pop(junk, None)
    return car


# Private legacy migration data; retained only to clean up exact old preset
# tints when importing a car. No Chrome operation or UI uses these constants.
LEGACY_TINT_PRESETS = {
    "silver": (0.85, 0.85, 0.88), "chrome": (0.92, 0.92, 0.95),
    "gold": (0.95, 0.75, 0.25), "black": (0.08, 0.08, 0.10),
    "red": (0.85, 0.12, 0.12), "blue": (0.15, 0.35, 0.95),
    "green": (0.12, 0.75, 0.35), "purple": (0.55, 0.20, 0.85),
    "pink": (0.95, 0.40, 0.70), "orange": (0.95, 0.45, 0.10),
    "white": (0.96, 0.96, 0.98), "copper": (0.80, 0.45, 0.25),
}
LEGACY_TINT_VECTOR_INDEXES = tuple(range(12))


def cpm1_restore_legacy_preset_tint(car):
    """Undo exact legacy preset tint vectors left by earlier tool versions."""
    if not isinstance(car, dict):
        return False
    value = car.get("vectors")
    if value is None:
        return False
    try:
        vectors = cpm1_decode_car_array(value, "vectors")
    except Exception:
        return False

    # Previous versions stored min(1, channel * 1.16 + 0.015) on every
    # vector in the selected part. Skip ambiguous clipped colors (e.g. white).
    legacy = {}
    ambiguous = set()
    for name, rgb in LEGACY_TINT_PRESETS.items():
        if name == "glow":
            continue
        boosted = tuple(min(1.0, channel * 1.16 + 0.015) for channel in rgb)
        if boosted in legacy and legacy[boosted] != rgb:
            ambiguous.add(boosted)
        else:
            legacy[boosted] = rgb
    groups = []
    for indexes in (LEGACY_TINT_VECTOR_INDEXES,):
        indexes = tuple(indexes)
        if indexes and indexes not in groups:
            groups.append(indexes)

    changed = False
    for indexes in groups:
        if max(indexes) >= len(vectors):
            continue
        for boosted, original in legacy.items():
            if boosted in ambiguous:
                continue
            matches = True
            for idx in indexes:
                vec = vectors[idx]
                try:
                    matches = isinstance(vec, dict) and all(
                        abs(float(vec.get(axis, 0.0)) - boosted[n]) <= 0.001
                        for n, axis in enumerate(("x", "y", "z"))
                    )
                except (TypeError, ValueError):
                    matches = False
                if not matches:
                    break
            if matches:
                for idx in indexes:
                    vectors[idx] = {"x": original[0], "y": original[1], "z": original[2]}
                changed = True
                break
    if changed:
        car["vectors"] = vectors
    return changed


def cpm1_prepare_car(stock_car, cpm_id):
    car_copy = json.loads(json.dumps(stock_car))
    car_id = int(car_copy.get("CarID") or 0)
    # strip harvest junk
    for junk in ("source_slot", "sourceSlot", "one_car", "car_id"):
        car_copy.pop(junk, None)
    if isinstance(car_copy.get("Vynils"), dict):
        car_copy["Vynils"]["CarID"] = car_id
    texts = car_copy.get("texts")
    if isinstance(texts, str):
        try: texts = cpm1_deserialize_string_list(texts)
        except: texts = []
    if not isinstance(texts, list): texts = []
    while len(texts) < 4: texts.append('')
    orig = texts[2]
    if isinstance(orig, str) and '_' in orig:
        parts = orig.split('_')
        texts[2] = f"{cpm_id}_" + "_".join(parts[1:])
    else:
        texts[2] = f"{cpm_id}_{car_id}XX000"
    car_copy["texts"] = texts
    cpm1_restore_legacy_preset_tint(car_copy)
    # physics + vinyl safety (anti flying / anti chrome)
    car_copy = cpm1_normalize_car_physics(car_copy)
    return car_copy

def cpm1_build_car_entries(cars, cpm_id):
    entries = []
    for car in cars:
        try: cid = int(car.get("CarID"))
        except: continue
        if cid < 0: continue
        gen = cpm1_generated_id_from_car(car) or f"{cpm_id}_{cid}XX000"
        entries.append([cid, gen])
    return entries

def cpm1_entries_to_sparse(car_entries):
    if not car_entries:
        return {"carGeneratedIDs": [], "carStatus": []}
    ids = []
    for e in car_entries:
        try: ids.append(int(e[0]))
        except: pass
    ids = [i for i in ids if i >= 0]
    if not ids:
        return {"carGeneratedIDs": [], "carStatus": []}
    max_id = max(ids)
    gen_ids = [''] * (max_id + 1)
    statuses = [0] * (max_id + 1)
    for e in car_entries:
        try: cid = int(e[0])
        except: continue
        if 0 <= cid <= max_id:
            gen_ids[cid] = str(e[1]) if e[1] is not None else ''
            statuses[cid] = 1
    return {"carGeneratedIDs": gen_ids, "carStatus": statuses}

def cpm1_sanitize_car_for_encoding(car):
    clean = {}
    for key, value in car.items():
        if isinstance(value, str) and cpm1_looks_like_base64(value):
            clean[key] = value
            continue
        if key == 'floats':
            arr = value if isinstance(value, list) else []
            out = []
            for v in arr:
                try: f = float(v)
                except: f = 0.0
                out.append(f)
            while len(out) < 48: out.append(0.0)
            clean[key] = out
        elif key == 'gears':
            arr = value if isinstance(value, list) else []
            out = []
            for v in arr:
                try: f = float(v)
                except: f = -10.0
                out.append(f)
            clean[key] = out
        elif key == 'vectors':
            arr = value if isinstance(value, list) else []
            out = []
            for vec in arr:
                if isinstance(vec, dict):
                    try: x = float(vec.get('x') or 0)
                    except: x = 0.0
                    try: y = float(vec.get('y') or 0)
                    except: y = 0.0
                    try: z = float(vec.get('z') or 0)
                    except: z = 0.0
                    out.append({'x': x, 'y': y, 'z': z})
                else:
                    out.append({'x': 0.0, 'y': 0.0, 'z': 0.0})
            clean[key] = out
        elif key in ('typeToInstall', 'BoughtParts', 'fsoData', 'installedPoliceLights'):
            arr = value if isinstance(value, list) else []
            out = []
            for v in arr:
                try: iv = int(v)
                except: iv = 0
                out.append(iv)
            clean[key] = out
        elif key == 'texts':
            arr = value if isinstance(value, list) else []
            clean[key] = [str(v) if v is not None else '' for v in arr]
        elif key == 'Vynils':
            if isinstance(value, dict) and isinstance(value.get('allVynils'), list):
                fc = int(value.get('_fieldCount', 2) or 2)
                if fc < 2: fc = 2
                nv = {'_fieldCount': fc, 'allVynils': [], 'CarID': int(car.get('CarID', 0) or 0)}
                if isinstance(value.get('_remainingHex'), str): nv['_remainingHex'] = value['_remainingHex']
                for vinyl in value['allVynils']:
                    if not isinstance(vinyl, dict): continue
                    cv = {'_fieldCount': int(vinyl.get('_fieldCount', 6) or 6)}
                    for fn in ('position', 'scaleRotation', 'iconPosition'):
                        vec = vinyl.get(fn) or {}
                        if not isinstance(vec, dict): vec = {}
                        try: x = float(vec.get('x') or 0)
                        except: x = 0.0
                        try: y = float(vec.get('y') or 0)
                        except: y = 0.0
                        try: z = float(vec.get('z') or 0)
                        except: z = 0.0
                        cv[fn] = {'x': x, 'y': y, 'z': z}
                    cv['text'] = str(vinyl.get('text', '') or '')
                    cv['color'] = int(vinyl['color']) & 0xFFFFFFFF if vinyl.get('color') is not None else 0
                    cv['packedData'] = int(vinyl['packedData']) if vinyl.get('packedData') is not None else 0
                    nv['allVynils'].append(cv)
                clean[key] = nv
            else:
                clean[key] = value
        elif key == 'WindowVinyls':
            if isinstance(value, list):
                out = []
                for vinyl in value:
                    if not isinstance(vinyl, dict): continue
                    cv = {'_fieldCount': int(vinyl.get('_fieldCount', 6) or 6)}
                    for fn in ('position', 'scaleRotation', 'iconPosition'):
                        vec = vinyl.get(fn) or {}
                        if not isinstance(vec, dict): vec = {}
                        try: x = float(vec.get('x') or 0)
                        except: x = 0.0
                        try: y = float(vec.get('y') or 0)
                        except: y = 0.0
                        try: z = float(vec.get('z') or 0)
                        except: z = 0.0
                        cv[fn] = {'x': x, 'y': y, 'z': z}
                    cv['text'] = str(vinyl.get('text', '') or '')
                    cv['color'] = int(vinyl['color']) & 0xFFFFFFFF if vinyl.get('color') is not None else 0
                    cv['packedData'] = int(vinyl['packedData']) if vinyl.get('packedData') is not None else 0
                    out.append(cv)
                clean[key] = out
            else:
                clean[key] = value
        elif key in ('CarID', 'dataVersion', 'flagID'):
            try: clean[key] = int(value)
            except: clean[key] = 0
        else:
            clean[key] = value
    return clean

def cpm1_encode_field_value_binary(name, value):
    if name == 'vectors':
        if not isinstance(value, list): value = []
        buf = struct.pack("<i", len(value))
        for vec in value:
            x = float(vec.get('x') or 0) if isinstance(vec, dict) else 0.0
            y = float(vec.get('y') or 0) if isinstance(vec, dict) else 0.0
            z = float(vec.get('z') or 0) if isinstance(vec, dict) else 0.0
            buf += struct.pack("<fff", x, y, z)
        return buf
    if name in ('floats', 'gears'):
        if not isinstance(value, list): value = []
        buf = struct.pack("<i", len(value))
        for v in value:
            try: f = float(v)
            except: f = 0.0
            buf += struct.pack("<f", f)
        return buf
    if name in ('typeToInstall', 'BoughtParts', 'fsoData', 'installedPoliceLights'):
        if not isinstance(value, list): value = []
        buf = struct.pack("<i", len(value))
        for v in value:
            try: iv = int(v)
            except: iv = 0
            buf += struct.pack("<i", iv)
        return buf
    if name == 'texts':
        if not isinstance(value, list): value = []
        buf = struct.pack("<i", len(value))
        for v in value:
            buf += cpm1_write_memorypack_string(str(v) if v is not None else '')
        return buf
    if name == 'Vynils':
        if not isinstance(value, dict) or not isinstance(value.get('allVynils'), list): return b""
        fc = int(value.get('_fieldCount', 2) or 2)
        parts = [bytes([fc & 0xFF])]
        if fc == 0: return b"".join(parts)
        parts.append(struct.pack("<i", len(value['allVynils'])))
        for vinyl in value['allVynils']:
            if not isinstance(vinyl, dict):
                parts.append(bytes([0]))
                continue
            vfc = int(vinyl.get('_fieldCount', 6) or 6)
            parts.append(bytes([vfc & 0xFF]))
            if vfc == 0: continue
            for fn in ('position', 'scaleRotation', 'iconPosition'):
                vec = vinyl.get(fn) if isinstance(vinyl.get(fn), dict) else {}
                x = float(vec.get('x') or 0)
                y = float(vec.get('y') or 0)
                z = float(vec.get('z') or 0)
                parts.append(struct.pack("<fff", x, y, z))
            parts.append(cpm1_write_memorypack_string(str(vinyl.get('text', '') or '')))
            parts.append(struct.pack("<I", int(vinyl.get('color', 0) or 0) & 0xFFFFFFFF))
            try: pd = int(vinyl.get('packedData', 0) or 0)
            except: pd = 0
            parts.append(struct.pack("<q", pd))
        if fc >= 2:
            carid = value.get('CarID')
            if carid is None and isinstance(value.get('_remainingHex'), str) and value['_remainingHex']:
                try:
                    hx = bytes.fromhex(value['_remainingHex'])
                    carid = struct.unpack_from("<i", hx, 0)[0]
                except: carid = 0
            try: carid = int(carid) if carid is not None else 0
            except: carid = 0
            parts.append(struct.pack("<i", carid))
        return b"".join(parts)
    if name == 'WindowVinyls':
        if not isinstance(value, list): value = []
        parts = [struct.pack("<i", len(value))]
        for vinyl in value:
            if not isinstance(vinyl, dict):
                parts.append(bytes([0]))
                continue
            vfc = int(vinyl.get('_fieldCount', 6) or 6)
            parts.append(bytes([vfc & 0xFF]))
            if vfc == 0: continue
            for fn in ('position', 'scaleRotation', 'iconPosition'):
                vec = vinyl.get(fn) if isinstance(vinyl.get(fn), dict) else {}
                x = float(vec.get('x') or 0)
                y = float(vec.get('y') or 0)
                z = float(vec.get('z') or 0)
                parts.append(struct.pack("<fff", x, y, z))
            parts.append(cpm1_write_memorypack_string(str(vinyl.get('text', '') or '')))
            parts.append(struct.pack("<I", int(vinyl.get('color', 0) or 0) & 0xFFFFFFFF))
            try: pd = int(vinyl.get('packedData', 0) or 0)
            except: pd = 0
            parts.append(struct.pack("<q", pd))
        return b"".join(parts)
    return b""

def cpm1_encode_car_to_memorypack(car):
    fields = []
    for fid, name in CPM_FIELD_NAMES.items():
        if name in car and car[name] is not None:
            fields.append((fid, name, car[name]))
    fields.sort(key=lambda x: x[0])
    parts = [struct.pack("<i", len(fields))]
    for fid, name, value in fields:
        parts.append(struct.pack("<h", fid))
        if name in ('CarID', 'dataVersion', 'flagID'):
            try: iv = int(value)
            except: iv = 0
            parts.append(cpm1_write_memorypack_string(str(iv)))
        elif isinstance(value, str) and cpm1_looks_like_base64(value):
            parts.append(cpm1_write_memorypack_string(value))
        else:
            binv = cpm1_encode_field_value_binary(name, value)
            try: compressed = brotli.compress(binv, quality=4)
            except: compressed = brotli.compress(binv)
            parts.append(cpm1_write_memorypack_string(base64.b64encode(compressed).decode('ascii')))
    return b"".join(parts)

def cpm1_encrypt_car_for_save(car, uid, normalize=True):
    if normalize:
        car = cpm1_normalize_car_physics(car)
    else:
        car = json.loads(json.dumps(car))
    clean = cpm1_sanitize_car_for_encoding(car)
    key = make_xor_key(uid)
    mp = cpm1_encode_car_to_memorypack(clean)
    compressed = brotli.compress(mp, quality=4)
    encrypted = xor_bytes(compressed, key)
    return "__ver1__" + base64.b64encode(encrypted).decode('ascii')

def cpm1_fetch_cars_v2(token):
    try:
        resp = http_session.post(CPM_CARS_FETCH_URL, json={"data": ""}, headers=cpm1_ios_headers(token), timeout=20)
        if resp.status_code < 200 or resp.status_code >= 300:
            return {"success": False, "error": f"HTTP {resp.status_code}", "status": resp.status_code, "cars": []}
        parsed = resp.json() if resp.text else {}
        if isinstance(parsed, str):
            try: parsed = json.loads(parsed)
            except: pass
        raw_result = None
        if isinstance(parsed, dict):
            if parsed.get("result") is not None:
                raw_result = parsed["result"]
            elif isinstance(parsed.get("data"), dict) and parsed["data"].get("result") is not None:
                raw_result = parsed["data"]["result"]
        if raw_result is None:
            return {"success": True, "cars": []}
        cars = []
        try: cars = json.loads(raw_result)
        except:
            m = re.search(r'\[\s*\{[\s\S]*\}\s*\]', str(raw_result))
            if m:
                try: cars = json.loads(m.group(0))
                except: pass
        return {"success": True, "cars": cars if isinstance(cars, list) else []}
    except Exception as e:
        return {"success": False, "error": str(e), "cars": []}

def cpm1_save_car_encrypted_v2(token, car, uid, retries=2, normalize=True):
    try: payload = cpm1_encrypt_car_for_save(car, uid, normalize=normalize)
    except Exception as e: return {"success": False, "error": f"encode:{str(e)[:80]}"}
    headers = cpm1_ios_headers(token)
    last = {"success": False, "error": "max_retries"}
    for attempt in range(1, retries + 1):
        try:
            resp = http_session.post(CPM_CARS_SAVE_URL, json={"data": payload}, headers=headers, timeout=30)
            if resp.status_code < 300:
                body = (resp.text or "").strip()
                if not body:
                    return {"success": True, "status": resp.status_code, "data": body}
                try:
                    result_body = resp.json()
                except Exception:
                    result_body = body
                explicit_result = False
                accepted = True
                if isinstance(result_body, dict):
                    for key in ("result", "ok", "success"):
                        if key in result_body:
                            explicit_result = True
                            accepted = bool(nuker._ok(result_body))
                            break
                    if not explicit_result and result_body.get("error"):
                        explicit_result = True
                        accepted = False
                elif str(result_body).strip().lower() in ("0", "false", "failed", "error"):
                    explicit_result, accepted = True, False
                elif str(result_body).strip().lower() in ("1", "true", "ok", "success"):
                    explicit_result, accepted = True, True
                if accepted:
                    return {"success": True, "status": resp.status_code, "data": body}
                last = {"success": False, "status": resp.status_code,
                        "error": "API rejected save: %s" % body[:160], "data": body}
            elif resp.status_code in (401, 403):
                return {"success": False, "auth": True, "status": resp.status_code, "error": f"HTTP {resp.status_code}"}
            else:
                last = {"success": False, "status": resp.status_code, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            last = {"success": False, "error": str(e)[:120]}
        if attempt < retries:
            time.sleep(min(1 * attempt, 3))
    return last

def cpm1_save_car_status(auth, fuid, car_entries):
    sparse = cpm1_entries_to_sparse(car_entries)
    gen_ids = ['' if v is None else str(v) for v in sparse["carGeneratedIDs"]]
    statuses = [0 if v is None else int(v) for v in sparse["carStatus"]]
    record = {"carIDnStatus": {"carGeneratedIDs": gen_ids, "carStatus": statuses}}
    try:
        ok, msg = nuker._send(auth, record, fuid, original=None, force_fields={"carIDnStatus"})
        return {"success": bool(ok), "message": msg}
    except Exception as e:
        return {"success": False, "message": str(e)[:120]}

def cpm1_verify_car_updates(token, expected_cars, matcher, attempts=3):
    """Re-fetch the garage and verify requested in-place edits actually persisted."""
    last_error = None
    last_verified = []
    last_missing = []
    for attempt in range(max(1, int(attempts))):
        fetched = cpm1_fetch_cars_v2(token)
        if fetched.get("success"):
            by_id = _cars_by_id(fetched.get("cars") or [])
            verified, missing = [], []
            for expected in expected_cars:
                try:
                    cid = int(expected.get("CarID"))
                except Exception:
                    continue
                current = by_id.get(cid)
                if current is not None and matcher(current, expected):
                    verified.append(cid)
                else:
                    missing.append(cid)
            if len(verified) == len(expected_cars) or attempt + 1 >= max(1, int(attempts)):
                return {"success": len(verified) == len(expected_cars), "verified": verified, "missing": missing}
            last_verified, last_missing = verified, missing
        else:
            last_error = fetched.get("error") or "garage verification fetch failed"
        if attempt + 1 < max(1, int(attempts)):
            time.sleep(0.8)
    return {"success": False, "verified": last_verified, "missing": last_missing, "error": last_error}


def cpm1_verify_clone_v2(token, expected_car_ids, attempts=3):
    """Read back the garage, retrying briefly for eventual consistency."""
    expected = []
    for cid in expected_car_ids:
        try:
            expected.append(int(cid))
        except Exception:
            continue
    last_error = None
    last_present = []
    last_missing = list(expected)
    for attempt in range(max(1, int(attempts))):
        r = cpm1_fetch_cars_v2(token)
        if r.get("success"):
            present = set()
            for c in r.get("cars", []):
                try:
                    present.add(int(c.get("CarID")))
                except Exception:
                    pass
            missing = [cid for cid in expected if cid not in present]
            last_present, last_missing = sorted(present), missing
            if not missing or attempt + 1 >= max(1, int(attempts)):
                return {"success": not missing, "present": last_present, "missing": missing}
        else:
            last_error = r.get("error") or "garage fetch failed"
        if attempt + 1 < max(1, int(attempts)):
            time.sleep(min(0.8 * (attempt + 1), 2.0))
    out = {"success": False, "present": last_present, "missing": last_missing}
    if last_error:
        out["error"] = last_error
    return out

def cpm1_clone_cars_core(tgt_email, tgt_password, src_cars_to_clone, src_uid=None, tgt_token=None, tgt_uid=None, existing_tgt_cars=None, progress_cb=None, verify=True):
    result = {"success": False, "ok": 0, "fail": 0, "total": len(src_cars_to_clone), "stage": "INIT", "failed": [], "verified": None, "tgt_token": tgt_token, "tgt_uid": tgt_uid}

    if not tgt_token or not tgt_uid:
        lr = nuker.login(tgt_email, tgt_password)
        if not lr.get("ok"):
            result["stage"] = "LOGIN"
            result["error"] = lr.get("message", "login_fail")
            return result
        tgt_token, tgt_uid = lr["auth"], lr["firebase_uid"]

    if existing_tgt_cars is None:
        r = cpm1_fetch_cars_v2(tgt_token)
        if not r.get("success") and r.get("status") in (401, 403):
            lr = nuker.login(tgt_email, tgt_password)
            if lr.get("ok"):
                tgt_token, tgt_uid = lr["auth"], lr["firebase_uid"]
                r = cpm1_fetch_cars_v2(tgt_token)
        if not r.get("success"):
            result["stage"] = "FETCH_TGT"
            result["error"] = r.get("error", "fetch_fail")
            return result
        existing_tgt_cars = r["cars"]

    result["tgt_token"] = tgt_token
    result["tgt_uid"] = tgt_uid

    rec = nuker.get_record(tgt_uid, tgt_email) or {}
    cpm_id = cpm1_resolve_cpm_id(rec)
    result["cpm_id"] = cpm_id

    prepared = []
    for car in src_cars_to_clone:
        try:
            c2 = json.loads(json.dumps(car))
            if src_uid:
                c2 = cpm1_replace_uid_in_value(c2, src_uid, tgt_uid)
            prepared.append(cpm1_prepare_car(c2, cpm_id))
        except Exception as e:
            result["failed"].append({"CarID": car.get("CarID"), "stage": "PREPARE", "error": str(e)[:120]})

    if not prepared:
        result["stage"] = "PREPARE"
        result["error"] = "no_prepared"
        return result

    merged = cpm1_merge_cars_by_id(existing_tgt_cars, prepared)
    entries = cpm1_build_car_entries(merged, cpm_id)

    sr = {"success": False, "message": "status save not attempted"}
    for status_attempt in range(3):
        if status_attempt:
            time.sleep(0.75 * status_attempt)
            lr = nuker.login(tgt_email, tgt_password)
            if lr.get("ok"):
                tgt_token, tgt_uid = lr["auth"], lr["firebase_uid"]
                result["tgt_token"] = tgt_token
                result["tgt_uid"] = tgt_uid
        sr = cpm1_save_car_status(tgt_token, tgt_uid, entries)
        if sr.get("success"):
            break
    if not sr.get("success"):
        result["stage"] = "STATUS_SAVE_WARN"
        result["status_error"] = sr.get("message", "status_fail")
        # continue — car body saves can still succeed without perfect status write

    ok = 0
    total = len(prepared)
    token_lock = Lock()
    state = {"token": tgt_token, "uid": tgt_uid, "done": 0}

    def _save_one(car):
        sr = {"success": False, "error": "save not attempted"}
        for recovery in range(3):
            with token_lock:
                tok, uid_ = state["token"], state["uid"]
            sr = cpm1_save_car_encrypted_v2(tok, car, uid_, retries=3)
            if sr.get("success"):
                break
            if recovery >= 2:
                break
            if sr.get("auth"):
                lr = nuker.login(tgt_email, tgt_password)
                if lr.get("ok"):
                    with token_lock:
                        state["token"] = lr["auth"]
                        state["uid"] = lr["firebase_uid"]
                        result["tgt_token"] = state["token"]
                        result["tgt_uid"] = state["uid"]
            else:
                time.sleep(0.75 * (recovery + 1))
        with token_lock:
            state["done"] += 1
            done_n = state["done"]
        if progress_cb:
            try:
                progress_cb(done_n, total)
            except Exception:
                pass
        return car.get("CarID"), sr

    workers = max(1, min(int(CAR_CLONE_WORKERS), total, 30))
    if total <= 2:
        workers = 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_save_one, car) for car in prepared]
        for fut in as_completed(futures):
            try:
                cid, sr = fut.result()
            except Exception as e:
                result["failed"].append({"CarID": None, "stage": "CAR_SAVE", "error": str(e)[:120]})
                continue
            if sr.get("success"):
                ok += 1
            else:
                result["failed"].append({
                    "CarID": cid,
                    "stage": "CAR_SAVE",
                    "error": sr.get("error", "unknown"),
                })

    tgt_token, tgt_uid = state["token"], state["uid"]
    result["tgt_token"] = tgt_token
    result["tgt_uid"] = tgt_uid
    result["stage"] = "DONE"
    result["workers"] = workers

    if verify:
        try:
            expected_ids = []
            by_id = {}
            for car in prepared:
                try:
                    cid = int(car.get("CarID"))
                    expected_ids.append(cid)
                    by_id[cid] = car
                except Exception:
                    continue
            v = cpm1_verify_clone_v2(tgt_token, expected_ids, attempts=3)
            for recovery in range(2):
                missing = set(v.get("missing") or [])
                if not missing:
                    break
                time.sleep(1.0 * (recovery + 1))
                lr = nuker.login(tgt_email, tgt_password)
                if lr.get("ok"):
                    tgt_token, tgt_uid = lr["auth"], lr["firebase_uid"]
                    result["tgt_token"], result["tgt_uid"] = tgt_token, tgt_uid
                for cid in sorted(missing):
                    car = by_id.get(cid)
                    if car is not None:
                        cpm1_save_car_encrypted_v2(tgt_token, car, tgt_uid, retries=4)
                v = cpm1_verify_clone_v2(tgt_token, expected_ids, attempts=3)
            result["verified"] = v
            prep_failed = [f for f in result["failed"] if f.get("stage") == "PREPARE"]
            remaining = set(v.get("missing") or [])
            result["failed"] = prep_failed + [
                {"CarID": cid, "stage": "VERIFY", "error": "car not present after recovery retries"}
                for cid in sorted(remaining)
            ]
            result["ok"] = len(expected_ids) - len(remaining)
        except Exception as e:
            result["verified"] = {"success": False, "error": str(e)[:80]}
            result["ok"] = ok
            result["failed"].append({"CarID": None, "stage": "VERIFY", "error": str(e)[:120]})
    else:
        result["ok"] = ok
    result["fail"] = len(result["failed"])
    result["success"] = int(result.get("ok") or 0) > 0 or (int(result.get("total") or 0) == 0)

    return result

# ================================================================
#  BACKGROUND WORKERS (WITH TOKEN DEDUCTION)
# ================================================================
def _rand_gmail():
    """Random Gmail e.g. IajOriknk200@gmail.com"""
    n = random.randint(8, 12)
    name = "".join(random.choice(string.ascii_letters) for _ in range(n))
    # mix case like example
    name = "".join(c.upper() if random.random() < 0.35 else c.lower() for c in name)
    digits = str(random.randint(10, 9999))
    return name + digits + "@gmail.com"


def _rand_password():
    """Random password letters+digits, length 10–14."""
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(random.randint(10, 14)))


def clone_task(src_record, src_cars, i, res_list, source_token, source_uid, progress_cb=None):
    reg_ok = False
    t_email = t_pass = ""
    for attempt in range(5):
        t_email = _rand_gmail()
        t_pass = _rand_password()
        try:
            r = http_session.post(f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FK}", json={"email": t_email, "password": t_pass, "returnSecureToken": True}, timeout=15)
            if "idToken" in r.text:
                reg_ok = True
                break
        except Exception:
            pass
        # A timed-out signup may have created the account despite losing its response.
        try:
            check = nuker.login(t_email, t_pass)
            if check.get("ok"):
                reg_ok = True
                break
        except Exception:
            pass
        time.sleep(min(0.75 * (attempt + 1), 3.0))

    if progress_cb: progress_cb(i, "registering")

    if not reg_ok:
        if res_list is not None:
            res_list.append({"success": False, "stage": "REGISTER", "error": "registration failed after retries"})
        if progress_cb: progress_cb(i, "done")
        return "", ""

    try:
        lr = nuker.login(t_email, t_pass)
        if lr.get("ok"):
            if hasattr(nuker, "seed_blank_profile"):
                nuker.seed_blank_profile(lr["auth"], lr.get("firebase_uid", ""), t_email)
            else:
                blank = {
                    "Name": "Player",
                    "money": 25000,
                    "coin": 0,
                    "localID": str(random.randint(1000000, 9999999)).zfill(8),
                    "allData": '{"cars":[]}',
                    "floats": list(src_record.get("floats") or []) if isinstance(src_record, dict) else [0.0] * 50,
                    "integers": list(src_record.get("integers") or []) if isinstance(src_record, dict) else [0] * 120,
                }
                nuker._send(lr["auth"], blank, lr["firebase_uid"])
            time.sleep(0.6)
    except Exception:
        pass

    cres = {"success": True, "ok": 0, "total": 0, "stage": "NO_CARS"}
    if src_cars:
        cres = cpm1_clone_cars_core(t_email, t_pass, src_cars, src_uid=source_uid, verify=False)
        try:
            if int(cres.get("ok") or 0) > 0:
                cres["success"] = True
        except Exception:
            pass
    if res_list is not None:
        res_list.append(cres)
    if progress_cb:
        progress_cb(i, "ok" if cres.get("success") or int(cres.get("ok") or 0) > 0 else "fail")
    return t_email, t_pass




# Full profile clone: everything except friends + player ID (localID)
IDENTITY_EXCLUDE = (
    "localID", "LocalID", "id", "ID", "playerId", "playerID",
    "email", "Email", "firebase_uid",
)
FRIEND_EXCLUDE = (
    "friends", "Friends", "friendList", "FriendList",
    "friendsData", "FriendsData", "saveFriends", "SaveFriends",
    "FriendsID", "friendsID",
)

PROFILE_CLONE_FIELDS = (
    "money", "coin",
    "floats", "integers",
    "boughtFsos", "boughtPoliceLights", "boughtPoliceSirens",
    "wheels", "animations", "emojiPacks",
    "favouriteWheels", "favouriteVinyls", "favouriteEmojis",
    "LevelsDoneTime",
    "personEquipmentsMale", "personEquipmentsFemale",
    "platesData",
    "carIDnStatus", "flags", "fcar",
    "allData",
    # Name optional — not an ID; still skip by default (include_name=True to copy)
)


def load_source_profile(src_email, src_pass):
    """Login source, load full player record + cars."""
    s = nuker.login(src_email, src_pass)
    if not s.get("ok"):
        return None, "Source login failed"
    # temporary uid for loading source into nuker cache
    src_uid_hash = abs(hash(src_email)) % (10**9)
    nuker.save_token(
        src_uid_hash, s["auth"], src_email, src_pass,
        s.get("refresh_token", ""), s.get("firebase_uid", ""),
    )
    nuker.load(src_uid_hash, force=True)
    rec = nuker.get_record(src_uid_hash, src_email) or {}
    fr = cpm1_fetch_cars_v2(s["auth"])
    cars = fr.get("cars", []) if fr.get("success") else []
    return {
        "login": s,
        "record": rec,
        "cars": cars,
        "uid_hash": src_uid_hash,
    }, "ok"


def apply_profile_to_target(tgt_email, tgt_pass, src_record, include_name=False):
    """Copy full data (money coin unlocks plates cars status rank…) except friends + localID."""
    t = nuker.login(tgt_email, tgt_pass)
    if not t.get("ok"):
        return {"ok": False, "message": "Target login failed"}
    tgt_hash = abs(hash(tgt_email + ":tgt")) % (10**9)
    nuker.save_token(
        tgt_hash, t["auth"], tgt_email, tgt_pass,
        t.get("refresh_token", ""), t.get("firebase_uid", ""),
    )
    nuker.load(tgt_hash, force=True)
    mods = {}
    force = set()
    skip = set(IDENTITY_EXCLUDE) | set(FRIEND_EXCLUDE)
    for field in PROFILE_CLONE_FIELDS:
        if field in skip:
            continue
        if field in src_record and src_record[field] is not None:
            mods[field] = src_record[field]
            force.add(field)
    # never write localID / FriendsID even if present in allData
    if "allData" in mods:
        ad = mods["allData"]
        if isinstance(ad, str):
            try:
                import json as _json
                ad_obj = _json.loads(ad)
            except Exception:
                ad_obj = None
            if isinstance(ad_obj, dict):
                for k in list(ad_obj.keys()):
                    kl = str(k).lower()
                    if "friend" in kl or kl in ("localid", "id", "playerid"):
                        ad_obj.pop(k, None)
                mods["allData"] = _json.dumps(ad_obj)
        elif isinstance(ad, dict):
            ad = dict(ad)
            for k in list(ad.keys()):
                kl = str(k).lower()
                if "friend" in kl or kl in ("localid", "id", "playerid"):
                    ad.pop(k, None)
            mods["allData"] = ad
    if include_name and src_record.get("Name"):
        mods["Name"] = src_record["Name"]
        force.add("Name")
    if "money" in mods:
        try:
            mods["money"] = min(int(mods["money"]), MAX_MONEY)
        except Exception:
            pass
    if "coin" in mods:
        try:
            mods["coin"] = min(int(mods["coin"]), MAX_COIN)
        except Exception:
            pass
    notes = []
    if mods:
        res = nuker._modify(tgt_hash, mods, force_fields=force)
        notes.append("profile:" + ("OK" if res.get("ok") else str(res.get("message", "fail"))))
    else:
        res = {"ok": False, "message": "No profile fields"}
        notes.append("profile:empty")
    # Explicit money/coin if present
    try:
        if "money" in mods:
            notes.append("money:%s" % mods.get("money"))
        if "coin" in mods:
            notes.append("coin:%s" % mods.get("coin"))
        if mods.get("personEquipmentsMale") or mods.get("personEquipmentsFemale"):
            notes.append("clothes:OK")
    except Exception:
        pass
    # King rank always
    try:
        rr = nuker.set_rank(tgt_hash)
        notes.append("rank:" + ("OK" if rr.get("ok") else "fail"))
    except Exception as e:
        notes.append("rank:" + str(e)[:40])
    # Full unlock pack (W16 fuel houses etc.)
    try:
        ur = nuker.unlock_all_features(tgt_hash)
        notes.append("unlocks:" + ("OK" if ur.get("ok") else "partial"))
    except Exception as e:
        notes.append("unlocks:" + str(e)[:40])
    rank_ok = any(n.startswith("rank:OK") for n in notes)
    unlock_ok = any(n.startswith("unlocks:OK") or n.startswith("unlocks:partial") for n in notes)
    ok = bool(res.get("ok")) or rank_ok or unlock_ok
    return {
        "ok": ok,
        "message": " · ".join(notes),
    }


def web_clone_account(src_email, src_pass, tgt_email, tgt_pass, progress_cb=None):
    """Full clone: all data except friends + localID (keeps target ID).
    progress_cb(pct:int 0-100, message:str)
    """
    def _p(pct, msg):
        if progress_cb:
            try:
                progress_cb(int(max(0, min(100, pct))), str(msg))
            except Exception:
                pass

    _p(2, "Loading source account…")
    src, msg = load_source_profile(src_email, src_pass)
    if not src:
        _p(100, "Failed: " + str(msg))
        return {"ok": False, "message": msg}
    cars = src["cars"] or []
    rec = src["record"]
    notes = []
    _p(12, "Copying profile data…")
    # 1) profile data
    pr = apply_profile_to_target(tgt_email, tgt_pass, rec, include_name=False)
    notes.append(pr.get("message", "profile"))
    # 2) cars
    if cars:
        total_cars = len(cars)
        _p(18, "Cloning cars 0/%d…" % total_cars)

        def car_progress(done, total):
            total = max(1, int(total or 1))
            done = int(done or 0)
            # cars occupy 20% → 92%
            pct = 20 + int(72 * done / total)
            _p(pct, "Cloning cars %d/%d…" % (done, total))

        res = cpm1_clone_cars_core(
            tgt_email, tgt_pass, cars,
            src_uid=src["login"].get("firebase_uid"),
            verify=True,
            progress_cb=car_progress,
        )
        notes.append(f"cars:{res.get('ok', 0)}/{res.get('total', 0)}")
        cars_ok = bool(res.get("success"))
    else:
        notes.append("cars:0")
        cars_ok = True
        res = {}
        _p(90, "No cars to clone")
    # plates count
    plates = 0
    pd = rec.get("platesData")
    if isinstance(pd, dict):
        plates = len(pd.get("allPlates") or [])
    elif isinstance(pd, list):
        plates = len(pd)
    ok = bool(pr.get("ok") and cars_ok)
    _p(100, "Clone finished" if ok else "Clone finished with errors")
    return {
        "ok": bool(ok),
        "message": f"Clone done · plates {plates} · " + " · ".join(notes),
        "detail": {"profile": pr, "cars": res},
    }


def web_bulk_clone(src_email, src_pass, count, progress_cb=None, workers=None, max_limit=None):
    """Create N accounts with parallel workers — full data except friends + player ID.
    progress_cb(done, total, message, percent) — message includes ETA.
    """
    lim = int(max_limit or BULK_CLONE_LIMIT_USER)
    lim = max(1, min(int(BULK_CLONE_LIMIT_ADMIN), lim))
    count = max(1, min(lim, int(count)))
    workers = int(workers or CLONE_WORKERS)
    workers = max(1, min(workers, 20, count))

    src, msg = load_source_profile(src_email, src_pass)
    if not src:
        return {"ok": False, "message": msg, "accounts": []}
    cars = list(src["cars"] or [])
    rec = src["record"]
    src_token = src["login"]["auth"]
    src_uid = src["login"].get("firebase_uid")

    accounts = [None] * count
    statuses = [None] * count
    done = {"n": 0, "failed": 0}
    done_lock = Lock()
    t0 = time.time()

    def _fmt_eta(seconds):
        seconds = max(0, int(seconds))
        if seconds < 60:
            return "%ds" % seconds
        m, s = divmod(seconds, 60)
        if m < 60:
            return "%dm %ds" % (m, s)
        h, m = divmod(m, 60)
        return "%dh %dm" % (h, m)

    def _one(i):
        try:
            if progress_cb:
                with done_lock:
                    n0 = done["n"]
                pct0 = int(100 * n0 / max(1, count))
                progress_cb(n0, count, "Working account %d/%d…\nWorkers: %d\nETA finish: calculating…" % (i + 1, count, workers), max(1, pct0))
        except Exception:
            pass
        task_result = []
        email, pw = clone_task(rec, cars, i, task_result, src_token, src_uid)
        clone_result = task_result[0] if task_result else {"success": False, "stage": "REGISTER"}
        profile_result = {"ok": False, "message": "target account was not created"}
        cars_ok = int(clone_result.get("ok") or 0)
        cars_total = int(clone_result.get("total") or len(cars) or 0)
        if email and pw:
            for profile_attempt in range(3):
                try:
                    profile_result = apply_profile_to_target(email, pw, rec, include_name=False)
                except Exception as exc:
                    profile_result = {"ok": False, "message": str(exc)[:120]}
                if profile_result.get("ok"):
                    break
                if profile_attempt < 2:
                    time.sleep(0.5 * (profile_attempt + 1))
            if not profile_result.get("ok"):
                try:
                    lr = nuker.login(email, pw)
                    if lr.get("ok") and hasattr(nuker, "seed_blank_profile"):
                        nuker.seed_blank_profile(lr["auth"], lr.get("firebase_uid", ""), email)
                        time.sleep(0.5)
                        profile_result = apply_profile_to_target(email, pw, rec, include_name=False)
                except Exception as exc:
                    profile_result = {"ok": False, "message": str(exc)[:120]}
        profile_ok = bool(profile_result.get("ok"))
        cars_ok_enough = (cars_total == 0) or (cars_ok > 0) or bool(clone_result.get("success"))
        complete = bool(email and pw and profile_ok and cars_ok_enough)
        stage = "DONE" if complete else ("PROFILE" if not profile_ok else ("CARS" if not cars_ok_enough else "PARTIAL"))
        status = {
            "success": complete,
            "stage": stage,
            "cars_ok": cars_ok,
            "cars_total": cars_total,
            "profile_ok": profile_ok,
            "profile_msg": str(profile_result.get("message") or "")[:80],
        }
        with done_lock:
            done["n"] += 1
            if not complete:
                done["failed"] += 1
            n = done["n"]
            failed_n = done["failed"]
        if progress_cb:
            elapsed = max(0.001, time.time() - t0)
            rate = n / elapsed
            left = count - n
            eta = left / rate if rate > 0 else 0
            pct = int(100 * n / max(1, count))
            import datetime as _dt
            finish_at = _dt.datetime.now() + _dt.timedelta(seconds=eta) if n < count else _dt.datetime.now()
            finish_str = finish_at.strftime("%H:%M:%S")
            msg = (
                "CLONING PROGRESS  %d/%d (%d%%)\n"
                "OK data: money coin clothes cars rank unlocks\n"
                "Workers: %d\n"
                "Needs attention: %d\n"
                "ETA finish: %s\n"
                "Finish around: %s"
            ) % (
                n, count, pct, workers, failed_n,
                _fmt_eta(eta) if n < count else "0s",
                finish_str,
            )
            try:
                progress_cb(n, count, msg, pct)
            except TypeError:
                try:
                    progress_cb(n, count, msg)
                except Exception:
                    pass
            except Exception:
                pass
        return i, email, pw, status

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, i): i for i in range(count)}
        for fut in as_completed(futs):
            try:
                i, email, pw, status = fut.result()
                if email and pw:
                    accounts[i] = "%s:%s" % (email, pw)
                statuses[i] = status
            except Exception as exc:
                statuses[futs[fut]] = {"success": False, "stage": "WORKER", "error": str(exc)[:120]}

    account_details = [
        {
            "credentials": account,
            "complete": bool(statuses[index] and statuses[index].get("success")),
            "stage": (statuses[index] or {}).get("stage", "UNKNOWN"),
        }
        for index, account in enumerate(accounts) if account
    ]
    accounts = [item["credentials"] for item in account_details]
    complete_n = sum(1 for status in statuses if status and status.get("success"))
    failed_n = count - complete_n
    total_time = _fmt_eta(time.time() - t0)
    if progress_cb:
        try:
            progress_cb(count, count, "DONE  %d accounts · money/coin/clothes/cars/rank/unlocks · %d/%d complete · %d need attention · %d workers · %s" % (len(accounts), complete_n, count, failed_n, workers, total_time), 100)
        except Exception:
            pass
    return {
        "ok": len(accounts) > 0,
        "message": "Created %d/%d accounts · full clone %d/%d complete · %d need attention (%d workers · %s)" % (len(accounts), count, complete_n, count, failed_n, workers, total_time),
        "accounts": accounts,
        "account_details": account_details,
        "complete_n": complete_n,
        "failed_n": failed_n,
        "statuses": statuses,
        "workers": workers,
        "elapsed": total_time,
    }


def set_cars_siren(cars):
    import copy
    out = []
    for car in cars:
        c = copy.deepcopy(car)
        floats = list(c.get("floats") or [])
        while len(floats) < 1:
            floats.append(0.0)
        floats[0] = 1.0
        c["floats"] = floats
        c = cpm1_normalize_car_physics(c)
        out.append(c)
    return out


# ================================================================
# UNLOCK CARS (from SOURCE_ACCOUNT → target logged-in account)
# ================================================================
def unlock_cars_to_account(tgt_email, tgt_pass, car_ids=None, progress_cb=None):
    """
    Inject cars from SOURCE_ACCOUNT into target.
    car_ids: None = all cars from source; or list/int of CarID(s).
    Applies physics normalize (no forced siren).
    """
    src_email, src_pass = SOURCE_ACCOUNT
    src, msg = load_source_profile(src_email, src_pass)
    if not src:
        return {"ok": False, "message": "Source login failed: %s" % msg, "ok_n": 0, "total": 0}
    cars = list(src.get("cars") or [])
    if not cars:
        return {"ok": False, "message": "Source has no cars", "ok_n": 0, "total": 0}

    if car_ids is not None:
        if isinstance(car_ids, (int, float, str)):
            want = {int(car_ids)}
        else:
            want = {int(x) for x in car_ids}
        filtered = []
        for c in cars:
            try:
                cid = int(c.get("CarID"))
            except Exception:
                continue
            if cid in want:
                filtered.append(c)
        cars = filtered
        if not cars:
            return {"ok": False, "message": "Car ID(s) not on source: %s" % sorted(want), "ok_n": 0, "total": 0}

    # never force siren here
    def _p(done, total):
        if progress_cb:
            try:
                progress_cb(done, total)
            except Exception:
                pass

    res = cpm1_clone_cars_core(
        tgt_email, tgt_pass, cars,
        src_uid=src["login"].get("firebase_uid"),
        verify=True,
        progress_cb=_p,
    )
    return {
        "ok": bool(res.get("ok", 0) > 0),
        "message": "Cars unlocked %s/%s" % (res.get("ok", 0), res.get("total", 0)),
        "ok_n": res.get("ok", 0),
        "total": res.get("total", 0),
        "failed": res.get("failed") or [],
        "detail": res,
    }


def unlock_all_cars(tgt_email, tgt_pass, progress_cb=None):
    return unlock_cars_to_account(tgt_email, tgt_pass, car_ids=None, progress_cb=progress_cb)


def unlock_one_car(tgt_email, tgt_pass, car_id, progress_cb=None):
    return unlock_cars_to_account(tgt_email, tgt_pass, car_ids=car_id, progress_cb=progress_cb)


# ================================================================
# COPY VINYL / CLONE CARS / COPY PLATES
# ================================================================

def _cars_by_id(cars):
    out = {}
    for c in cars or []:
        if not isinstance(c, dict):
            continue
        try:
            raw_cid = c.get("CarID")
            if raw_cid is None:
                continue
            cid = int(raw_cid)
        except Exception:
            continue
        if cid >= 0:
            out[cid] = c
    return out


def _copy_vinyl_fields(src_car, tgt_car):
    """Copy body + window vinyl (+ related design fields) onto target car shell."""
    import copy
    out = copy.deepcopy(tgt_car) if isinstance(tgt_car, dict) else {}
    if not isinstance(src_car, dict):
        return out
    for key in (
        "Vynils", "WindowVinyls", "vynils", "windowVinyls",
        "vectors", "Vectors",
    ):
        if key in src_car and src_car[key] is not None:
            out[key] = copy.deepcopy(src_car[key])
    if "Vynils" not in out and "vynils" in out:
        out["Vynils"] = out.pop("vynils")
    if "WindowVinyls" not in out and "windowVinyls" in out:
        out["WindowVinyls"] = out.pop("windowVinyls")
    # keep target CarID / identity
    for k in ("CarID", "carGeneratedID", "UniqueID", "uniqueId"):
        if k in (tgt_car or {}):
            out[k] = tgt_car[k]
    return cpm1_normalize_car_physics(out)


def copy_car_vinyl_single(src_email, src_pass, tgt_email, tgt_pass, car_id, progress_cb=None):
    """Copy vinyl design of one CarID from source garage → target same CarID."""
    try:
        car_id = int(car_id)
    except Exception:
        return {"ok": False, "message": "Invalid car ID"}
    src, msg = load_source_profile(src_email, src_pass)
    if not src:
        return {"ok": False, "message": msg}
    tgt_lr = nuker.login(tgt_email, tgt_pass)
    if not tgt_lr.get("ok"):
        return {"ok": False, "message": "Target login failed"}
    fr = cpm1_fetch_cars_v2(tgt_lr["auth"])
    if not fr.get("success"):
        return {"ok": False, "message": "Target cars fetch failed"}
    src_map = _cars_by_id(src["cars"])
    tgt_map = _cars_by_id(fr.get("cars") or [])
    if car_id not in src_map:
        return {"ok": False, "message": "Source does not have car ID %s" % car_id}
    if car_id not in tgt_map:
        return {"ok": False, "message": "Target does not have car ID %s — unlock/clone car first" % car_id}
    merged = _copy_vinyl_fields(src_map[car_id], tgt_map[car_id])
    # rebind uid
    su = src["login"].get("firebase_uid")
    tu = tgt_lr.get("firebase_uid")
    if su and tu:
        merged = cpm1_replace_uid_in_value(merged, su, tu)
    rec = nuker.get_record(abs(hash(tgt_email)) % (10**9), tgt_email) or {}
    # load target into nuker for cpm id
    th = abs(hash(tgt_email + ":vinyl")) % (10**9)
    nuker.save_token(th, tgt_lr["auth"], tgt_email, tgt_pass, tgt_lr.get("refresh_token", ""), tu or "")
    nuker.load(th, force=True)
    rec = nuker.get_record(th, tgt_email) or {}
    cpm_id = cpm1_resolve_cpm_id(rec)
    car = cpm1_prepare_car(merged, cpm_id)
    if progress_cb:
        try:
            progress_cb(1, 1)
        except Exception:
            pass
    # ensure car status lists include this car
    try:
        fr2 = cpm1_fetch_cars_v2(tgt_lr["auth"])
        existing = fr2.get("cars") or [] if fr2.get("success") else list(tgt_map.values())
        merged_list = cpm1_merge_cars_by_id(existing, [car])
        entries = cpm1_build_car_entries(merged_list, cpm_id)
        cpm1_save_car_status(tgt_lr["auth"], tu, entries)
    except Exception:
        pass
    sr = cpm1_save_car_encrypted_v2(tgt_lr["auth"], car, tu, retries=3)
    if not sr.get("success"):
        tgt_lr = nuker.login(tgt_email, tgt_pass)
        if tgt_lr.get("ok"):
            sr = cpm1_save_car_encrypted_v2(tgt_lr["auth"], car, tgt_lr.get("firebase_uid"), retries=2)
    if sr.get("success"):
        return {"ok": True, "message": "Vinyl copied for car %s — reopen game" % car_id}
    return {"ok": False, "message": "Vinyl save failed: %s" % (sr.get("error") or sr.get("message") or "error")}


def copy_car_vinyl_all(src_email, src_pass, tgt_email, tgt_pass, progress_cb=None):
    """Copy vinyl for every CarID that exists on both source and target."""
    src, msg = load_source_profile(src_email, src_pass)
    if not src:
        return {"ok": False, "message": msg}
    tgt_lr = nuker.login(tgt_email, tgt_pass)
    if not tgt_lr.get("ok"):
        return {"ok": False, "message": "Target login failed"}
    fr = cpm1_fetch_cars_v2(tgt_lr["auth"])
    if not fr.get("success"):
        return {"ok": False, "message": "Target cars fetch failed"}
    src_map = _cars_by_id(src["cars"])
    tgt_map = _cars_by_id(fr.get("cars") or [])
    common = sorted(set(src_map) & set(tgt_map))
    if not common:
        return {"ok": False, "message": "No matching car IDs on both accounts"}
    su = src["login"].get("firebase_uid")
    tu = tgt_lr.get("firebase_uid")
    th = abs(hash(tgt_email + ":vinylall")) % (10**9)
    nuker.save_token(th, tgt_lr["auth"], tgt_email, tgt_pass, tgt_lr.get("refresh_token", ""), tu or "")
    nuker.load(th, force=True)
    rec = nuker.get_record(th, tgt_email) or {}
    cpm_id = cpm1_resolve_cpm_id(rec)
    ok = fail = 0
    total = len(common)
    token = tgt_lr["auth"]
    for i, cid in enumerate(common):
        merged = _copy_vinyl_fields(src_map[cid], tgt_map[cid])
        if su and tu:
            merged = cpm1_replace_uid_in_value(merged, su, tu)
        car = cpm1_prepare_car(merged, cpm_id)
        sr = cpm1_save_car_encrypted_v2(token, car, tu, retries=2)
        if not sr.get("success") and sr.get("auth"):
            lr = nuker.login(tgt_email, tgt_pass)
            if lr.get("ok"):
                token, tu = lr["auth"], lr.get("firebase_uid")
                sr = cpm1_save_car_encrypted_v2(token, car, tu, retries=2)
        if sr.get("success"):
            ok += 1
        else:
            fail += 1
        if progress_cb:
            try:
                progress_cb(i + 1, total)
            except Exception:
                pass
    return {
        "ok": ok > 0,
        "message": "Vinyl copied: %d ok, %d fail (matched %d cars)" % (ok, fail, total),
        "ok_n": ok,
        "fail_n": fail,
    }


def clone_single_car(src_email, src_pass, tgt_email, tgt_pass, car_id, progress_cb=None):
    """Inject one full car (body + vinyl + status) from source → target."""
    try:
        car_id = int(car_id)
    except Exception:
        return {"ok": False, "message": "Invalid car ID"}
    src, msg = load_source_profile(src_email, src_pass)
    if not src:
        return {"ok": False, "message": msg}
    src_map = _cars_by_id(src["cars"])
    if car_id not in src_map:
        return {"ok": False, "message": "Source does not have car ID %s" % car_id}
    cars = [src_map[car_id]]
    res = cpm1_clone_cars_core(
        tgt_email, tgt_pass, cars,
        src_uid=src["login"].get("firebase_uid"),
        progress_cb=progress_cb,
        verify=False,
    )
    if res.get("ok", 0) > 0 or res.get("success"):
        return {"ok": True, "message": "Car %s cloned (%s ok)" % (car_id, res.get("ok", 0))}
    return {
        "ok": False,
        "message": "Clone car failed: %s" % (res.get("error") or res.get("stage") or "error"),
    }


def clone_all_cars(src_email, src_pass, tgt_email, tgt_pass, progress_cb=None):
    """Inject all cars from source garage → target."""
    src, msg = load_source_profile(src_email, src_pass)
    if not src:
        return {"ok": False, "message": msg}
    cars = list(src["cars"] or [])
    if not cars:
        return {"ok": False, "message": "Source has 0 cars"}
    res = cpm1_clone_cars_core(
        tgt_email, tgt_pass, cars,
        src_uid=src["login"].get("firebase_uid"),
        progress_cb=progress_cb,
        verify=False,
    )
    ok_n = int(res.get("ok") or 0)
    fail_n = int(res.get("fail") or 0)
    if ok_n > 0 or res.get("success"):
        return {
            "ok": True,
            "message": "Cars cloned: %d ok, %d fail (total %d)" % (ok_n, fail_n, len(cars)),
            "ok_n": ok_n,
            "fail_n": fail_n,
        }
    return {
        "ok": False,
        "message": "Clone cars failed: %s" % (res.get("error") or res.get("stage") or "error"),
    }


def copy_plates_from_account(src_email, src_pass, tgt_email, tgt_pass):
    """Copy platesData from source account → target account."""
    src, msg = load_source_profile(src_email, src_pass)
    if not src:
        return {"ok": False, "message": msg}
    plates = src["record"].get("platesData")
    if not plates or not (isinstance(plates, dict) and plates.get("allPlates")):
        return {"ok": False, "message": "Source has no plates"}
    t = nuker.login(tgt_email, tgt_pass)
    if not t.get("ok"):
        return {"ok": False, "message": "Target login failed"}
    th = abs(hash(tgt_email + ":platescopy")) % (10**9)
    nuker.save_token(
        th, t["auth"], tgt_email, tgt_pass,
        t.get("refresh_token", ""), t.get("firebase_uid", ""),
    )
    if not nuker.load(th, force=True):
        return {"ok": False, "message": "Target load failed"}
    data = nuker.get_record(th, tgt_email)
    if not data:
        return {"ok": False, "message": "Target profile empty"}
    import copy
    data = copy.deepcopy(data)
    data["platesData"] = copy.deepcopy(plates)
    res = nuker._save(th, data, force_fields={"platesData"})
    if res.get("ok"):
        n = len(plates.get("allPlates") or [])
        return {"ok": True, "message": "Copied %d plates to target" % n}
    return {"ok": False, "message": res.get("message") or "Plate save failed"}


# ================================================================
# BUMPER REMOVER (FRONT / REAR)
# In the inspected garage data, slots 1 and 2 are already -1. The
# active front/rear bumper selections are slots 3 and 4 (0 = stock).
# Set these selections to -1 so the game actually removes the bumpers.
# ================================================================
BUMPER_FRONT_SLOT = 3
BUMPER_REAR_SLOT = 4


def apply_bumper_remove(car, front=True, rear=True):
    """Set front/rear bumper slots to -1 (removed). Keeps other parts."""
    import copy
    if not isinstance(car, dict):
        return car
    c = copy.deepcopy(car)
    if c.get("typeToInstall") is None:
        tti = []
    else:
        tti = cpm1_decode_car_field(c, "typeToInstall")
    need = max(BUMPER_FRONT_SLOT, BUMPER_REAR_SLOT) + 1
    while len(tti) < need:
        tti.append(-2)
    if front:
        tti[BUMPER_FRONT_SLOT] = -1
    if rear:
        tti[BUMPER_REAR_SLOT] = -1
    c["typeToInstall"] = tti
    # BoughtParts is ownership inventory; leave it untouched.
    return c


def _save_modified_car_to_account(tgt_email, tgt_pass, car, progress_cb=None):
    lr = nuker.login(tgt_email, tgt_pass)
    if not lr.get("ok"):
        return {"ok": False, "message": "Login failed"}
    th = abs(hash(tgt_email + ":bumper")) % (10**9)
    nuker.save_token(
        th, lr["auth"], tgt_email, tgt_pass,
        lr.get("refresh_token", ""), lr.get("firebase_uid", ""),
    )
    nuker.load(th, force=True)
    rec = nuker.get_record(th, tgt_email) or {}
    cpm_id = cpm1_resolve_cpm_id(rec)
    prepared = car
    # update status entry for this car among garage
    fr = cpm1_fetch_cars_v2(lr["auth"])
    existing = fr.get("cars") or [] if fr.get("success") else []
    merged = cpm1_merge_cars_by_id(existing, [prepared])
    entries = cpm1_build_car_entries(merged, cpm_id)
    cpm1_save_car_status(lr["auth"], lr.get("firebase_uid"), entries)
    sr = cpm1_save_car_encrypted_v2(lr["auth"], prepared, lr.get("firebase_uid"), retries=3, normalize=False)
    if not sr.get("success"):
        lr2 = nuker.login(tgt_email, tgt_pass)
        if lr2.get("ok"):
            sr = cpm1_save_car_encrypted_v2(lr2["auth"], prepared, lr2.get("firebase_uid"), retries=2, normalize=False)
    if progress_cb:
        try:
            progress_cb(1, 1)
        except Exception:
            pass
    if sr.get("success"):
        return {"ok": True, "message": "Bumper updated on car %s" % prepared.get("CarID")}
    return {"ok": False, "message": "Save failed: %s" % (sr.get("error") or "error")}


def remove_bumper_one(tgt_email, tgt_pass, car_id, front=True, rear=True, progress_cb=None):
    try:
        car_id = int(car_id)
    except Exception:
        return {"ok": False, "message": "Invalid car ID"}
    lr = nuker.login(tgt_email, tgt_pass)
    if not lr.get("ok"):
        return {"ok": False, "message": "Login failed"}
    fr = cpm1_fetch_cars_v2(lr["auth"])
    if not fr.get("success"):
        return {"ok": False, "message": "Fetch cars failed"}
    cars = _cars_by_id(fr.get("cars") or [])
    if car_id not in cars:
        return {"ok": False, "message": "Account has no car ID %s" % car_id}
    which = []
    if front:
        which.append("FRONT")
    if rear:
        which.append("REAR")
    mod = apply_bumper_remove(cars[car_id], front=front, rear=rear)
    res = _save_modified_car_to_account(tgt_email, tgt_pass, mod, progress_cb=progress_cb)
    if res.get("ok"):
        res["message"] = "Removed %s bumper on car %s" % ("+".join(which) or "none", car_id)
    return res


def remove_bumper_all(tgt_email, tgt_pass, front=True, rear=True, progress_cb=None):
    lr = nuker.login(tgt_email, tgt_pass)
    if not lr.get("ok"):
        return {"ok": False, "message": "Login failed"}
    token, uid = lr.get("auth"), lr.get("firebase_uid")
    if not token or not uid:
        return {"ok": False, "message": "Login did not return the car-save credentials"}
    fr = cpm1_fetch_cars_v2(token)
    if not fr.get("success"):
        return {"ok": False, "message": "Fetch cars failed: %s" % (fr.get("error") or "unknown error")}
    cars = cpm1_merge_cars_by_id(fr.get("cars") or [])
    if not cars:
        return {"ok": False, "message": "No cars on account"}
    th = abs(hash(tgt_email + ":bumperall")) % (10**9)
    nuker.save_token(th, token, tgt_email, tgt_pass, lr.get("refresh_token", ""), uid)
    nuker.load(th, force=True)
    rec = nuker.get_record(th, tgt_email) or {}
    cpm_id = cpm1_resolve_cpm_id(rec)
    # Keep original generated IDs/texts: these are already owned cars, not clone imports.
    try:
        modified = [apply_bumper_remove(c, front=front, rear=rear) for c in cars]
    except Exception as exc:
        return {"ok": False, "message": "Could not decode bumper data; no cars were changed: %s" % str(exc)[:120]}
    entries = cpm1_build_car_entries(cars, cpm_id)
    status_result = cpm1_save_car_status(token, uid, entries)
    saved = failed = 0
    total = len(modified)
    for i, car in enumerate(modified):
        sr = cpm1_save_car_encrypted_v2(token, car, uid, retries=3, normalize=False)
        if not sr.get("success") and (sr.get("auth") or sr.get("status") in (401, 403)):
            lr2 = nuker.login(tgt_email, tgt_pass)
            if lr2.get("ok") and lr2.get("auth") and lr2.get("firebase_uid"):
                token, uid = lr2["auth"], lr2["firebase_uid"]
                sr = cpm1_save_car_encrypted_v2(token, car, uid, retries=3, normalize=False)
        if sr.get("success"):
            saved += 1
        else:
            failed += 1
        if progress_cb:
            try:
                progress_cb(i + 1, total)
            except Exception:
                pass
        if i + 1 < total:
            time.sleep(0.25)
    def _bumper_matches(current, _expected):
        try:
            actual = cpm1_decode_car_field(dict(current), "typeToInstall")
        except Exception:
            return current.get("typeToInstall") is None
        if not isinstance(actual, list):
            return False
        if _expected.get("typeToInstall") is None:
            return False
        return ((not front or (len(actual) > BUMPER_FRONT_SLOT and actual[BUMPER_FRONT_SLOT] == -1)) and
                (not rear or (len(actual) > BUMPER_REAR_SLOT and actual[BUMPER_REAR_SLOT] == -1)))
    verification = cpm1_verify_car_updates(token, modified, _bumper_matches)
    verified_n = len(verification.get("verified") or [])
    which = "+".join(name for flag, name in ((front, "FRONT"), (rear, "REAR")) if flag) or "NONE"
    details = "Bumper %s removed: saved %d/%d, verified %d/%d, failed %d" % (which, saved, total, verified_n, total, failed)
    if verification.get("error"):
        details += " (verification unavailable: %s)" % verification["error"]
    if not status_result.get("success"):
        details += "; garage status update: %s" % (status_result.get("message") or "failed")
    return {"ok": verified_n > 0, "message": details, "ok_n": verified_n,
            "saved_n": saved, "verified_n": verified_n, "fail_n": max(failed, total - verified_n)}


# ================================================================
