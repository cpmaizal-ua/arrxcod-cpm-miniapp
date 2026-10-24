#!/usr/bin/env python3
"""CPM1 Police Siren Unlocker — ONLY login + unlock siren. Nothing else."""
from __future__ import annotations

import os, re, time, json, uuid, struct, base64, zlib, sqlite3, secrets, threading
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    import brotli
except ImportError:
    brotli = None
try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
except ImportError:
    AES = pad = unpad = None


FK = "AIzaSyBW1ZbMiUeDZHYUO2bY8Bfnf5rRgrQGPTM"
CPM_CARS_FETCH_URL = "https://europe-west1-cp-multiplayer.cloudfunctions.net/GetAllCars2"
CPM_CARS_SAVE_URL = "https://europe-west1-cp-multiplayer.cloudfunctions.net/SaveCarsPartially8"
DB = "siren_tokens.db"

http_session = requests.Session()
http_session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20, max_retries=2))

with sqlite3.connect(DB) as c:
    c.execute("""CREATE TABLE IF NOT EXISTS tokens (
        user_id INTEGER PRIMARY KEY, auth_token TEXT, email TEXT, password TEXT,
        refresh_token TEXT, firebase_uid TEXT, token_expires_at REAL)""")
    c.commit()


def login(email, password):
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FK}"
    try:
        r = http_session.post(url, json={"email": email, "password": password, "returnSecureToken": True}, timeout=20)
        data = r.json() if r.text else {}
        if "idToken" in data:
            return {
                "ok": True,
                "auth": data["idToken"],
                "firebase_uid": data.get("localId", ""),
                "refresh_token": data.get("refreshToken", ""),
            }
        err = data.get("error", {})
        return {"ok": False, "message": err.get("message", "login failed") if isinstance(err, dict) else str(err)}
    except Exception as e:
        return {"ok": False, "message": str(e)[:120]}


def save_token(uid, auth, email, pw, rt, fuid):
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR REPLACE INTO tokens VALUES (?,?,?,?,?,?,?)",
                  (uid, auth, email, pw, rt, fuid, time.time() + 3500))
        c.commit()


def get_token(uid):
    with sqlite3.connect(DB) as c:
        row = c.execute("SELECT auth_token,email,password,refresh_token,firebase_uid FROM tokens WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return None
    return {"auth": row[0], "email": row[1], "password": row[2], "refresh_token": row[3], "firebase_uid": row[4]}


def delete_token(uid):
    with sqlite3.connect(DB) as c:
        c.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
        c.commit()


def make_xor_key(uid) -> bytes:
    chars = list(str(uid or ""))
    if len(chars) >= 9:
        chars[1], chars[8] = chars[8], chars[1]
    if len(chars) >= 3:
        chars.pop(2)
    if len(chars) >= 5:
        chars.append(chars[4])
    return "".join(chars).encode("utf-8") or b"0"


def xor_bytes(data: bytes, key: bytes) -> bytes:
    return bytes(data[i] ^ key[i % len(key)] for i in range(len(data)))



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

def cpm1_prepare_car(stock_car, cpm_id):
    car_copy = json.loads(json.dumps(stock_car))
    car_id = int(car_copy.get("CarID") or 0)
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
                    cv['color'] = int(vinyl.get('color', 0) or 0)
                    cv['packedData'] = int(vinyl.get('packedData', 0) or 0)
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
                    cv['color'] = int(vinyl.get('color', 0) or 0)
                    cv['packedData'] = int(vinyl.get('packedData', 0) or 0)
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

def cpm1_encrypt_car_for_save(car, uid):
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

def cpm1_save_car_encrypted_v2(token, car, uid, retries=2):
    try: payload = cpm1_encrypt_car_for_save(car, uid)
    except Exception as e: return {"success": False, "error": f"encode:{str(e)[:80]}"}
    headers = cpm1_ios_headers(token)
    last = {"success": False, "error": "max_retries"}
    for attempt in range(1, retries + 1):
        try:
            resp = http_session.post(CPM_CARS_SAVE_URL, json={"data": payload}, headers=headers, timeout=30)
            if resp.status_code < 300:
                return {"success": True, "status": resp.status_code, "data": resp.text}
            if resp.status_code in (401, 403):
                return {"success": False, "auth": True, "status": resp.status_code, "error": f"HTTP {resp.status_code}"}
            last = {"success": False, "status": resp.status_code, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            last = {"success": False, "error": str(e)[:120]}
        if attempt < retries:
            time.sleep(min(1 * attempt, 3))
    return last



# ---------- siren only ----------
def set_siren(car):
    c = deepcopy(car)
    floats = list(c.get("floats") or [])
    while len(floats) < 1:
        floats.append(0.0)
    floats[0] = 1.0
    c["floats"] = floats
    return c


def unlock_siren(email, password, car_id=None, progress_cb=None):
    lr = login(email, password)
    if not lr.get("ok"):
        return {"ok": False, "message": lr.get("message", "login failed"), "updated": 0, "total": 0}
    token, uid = lr["auth"], lr["firebase_uid"]
    fr = cpm1_fetch_cars_v2(token)
    if not fr.get("success"):
        return {"ok": False, "message": fr.get("error", "fetch failed"), "updated": 0, "total": 0}
    cars_list = fr.get("cars") or []
    if not cars_list:
        return {"ok": False, "message": "No cars", "updated": 0, "total": 0}
    targets = []
    for c in cars_list:
        try:
            cid = int(c.get("CarID") or c.get("id") or -1)
        except Exception:
            cid = -1
        if car_id is None or cid == int(car_id):
            targets.append(c)
    if not targets:
        return {"ok": False, "message": "Car not found", "updated": 0, "total": 0}
    updated, total = 0, len(targets)
    for i, car in enumerate(targets):
        if progress_cb:
            try:
                progress_cb(i, total)
            except Exception:
                pass
        fixed = set_siren(car)
        # soft clamp floats to reduce flying side-effect
        fl = list(fixed.get("floats") or [])
        for i,v in enumerate(fl):
            try:
                f=float(v)
            except Exception:
                f=0.0
            if abs(f)>1e4: f=max(-1e4,min(1e4,f))
            fl[i]=f
        fixed["floats"]=fl
        sr = cpm1_save_car_encrypted_v2(token, fixed, uid, retries=2)
        if sr.get("success"):
            updated += 1
        time.sleep(0.12)
    if progress_cb:
        try:
            progress_cb(total, total)
        except Exception:
            pass
    return {"ok": updated > 0, "message": "Siren %d/%d" % (updated, total), "updated": updated, "total": total}

