
import os, time, json, hashlib, zlib, sqlite3, secrets, html, threading, struct, base64, struct, base64
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    brotli = None
    HAS_BROTLI = False
try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

FK = "AIzaSyBW1ZbMiUeDZHYUO2bY8Bfnf5rRgrQGPTM"
LOAD_URL = "https://europe-west1-cp-multiplayer.cloudfunctions.net/GetPlayerRecords3"
SAVE_URL = "https://europe-west1-cp-multiplayer.cloudfunctions.net/SavePlayerRecordsPartially8"
RANK_URL = "https://us-central1-cp-multiplayer.cloudfunctions.net/SetUserRating5"
MAX_MONEY = 50_000_000
MAX_COIN = 500_000
GAME_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "gzip",
    "Content-Type": "application/json",
    "User-Agent": "UnityPlayer/2022.3.62f2 (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
    "X-Unity-Version": "2022.3.62f2",
}
http_session = requests.Session()
# The clone pipeline can issue up to 20 account jobs × 16 car saves in parallel.
# Retain enough keep-alive connections for that existing ceiling to avoid
# repeatedly opening and discarding overflow connections during bulk runs.
adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=400, max_retries=3)
http_session.mount("https://", adapter)

USE_MONGO = False
db_path = "cpm_web.db"
tokens_col = users_col = premium_col = admins_col = user_data_col = None

with sqlite3.connect(db_path) as c:
    c.execute("CREATE TABLE IF NOT EXISTS tokens (user_id INTEGER PRIMARY KEY, auth_token TEXT, email TEXT, password TEXT, refresh_token TEXT, firebase_uid TEXT, token_expires_at REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS user_data (cache_key TEXT PRIMARY KEY, email TEXT, data_json TEXT, saved_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("""CREATE TABLE IF NOT EXISTS license_keys (
        key_code TEXT PRIMARY KEY, max_devices INTEGER DEFAULT 1, devices TEXT DEFAULT '[]',
        expires_at REAL DEFAULT 0, created_at REAL, created_by TEXT, active INTEGER DEFAULT 1)""")
    c.execute("CREATE TABLE IF NOT EXISTS user_keys (session_id TEXT PRIMARY KEY, key_code TEXT, activated_at REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS admins (username TEXT PRIMARY KEY, password TEXT)")
    c.commit()

def make_xor_key(uid: str) -> bytes:
    chars = list(str(uid or ""))
    if len(chars) >= 9: chars[1], chars[8] = chars[8], chars[1]
    if len(chars) >= 3: chars.pop(2)
    if len(chars) >= 5: chars.append(chars[4])
    return "".join(chars).encode("utf-8") or b"0"

def xor_bytes(data: bytes, key: bytes) -> bytes: return bytes(data[i] ^ key[i % len(key)] for i in range(len(data)))

def decompress(data: bytes):
    if HAS_BROTLI:
        try: return brotli.decompress(data)
        except: pass
    for args in ((zlib.MAX_WBITS | 16,), tuple()):
        try: return zlib.decompress(data, *args)
        except: pass
    return None

def decrypt_aes(data: bytes, key: bytes):
    if not HAS_CRYPTO: return None
    try: return unpad(AES.new(key[:16], AES.MODE_CBC, b"\x00" * 16).decrypt(data), 16)
    except: return None

def _md5(text: str) -> bytes: return hashlib.md5(str(text).encode()).digest()
def _sha1(text: str) -> bytes: return hashlib.sha1(str(text).encode()).digest()[:16]
def build_aes_keys(uid: str, password: str = None, email: str = None) -> list:
    keys = [_md5("olzhas_carparking")]
    if password: keys.extend([_md5(password), _sha1(password)])
    if uid: keys.extend([_md5(uid), _sha1(uid)])
    if email: keys.append(_md5(email))
    return keys

class Reader:
    def __init__(self, data: bytes): self.buf, self.pos = data, 0
    def has_bytes(self, n: int) -> bool: return self.pos + n <= len(self.buf)
    def read_byte(self) -> int:
        if not self.has_bytes(1): return 0
        v = self.buf[self.pos]; self.pos += 1; return v
    def read_int(self) -> int:
        if not self.has_bytes(4): self.pos = len(self.buf); return 0
        v = struct.unpack_from("<i", self.buf, self.pos)[0]; self.pos += 4; return v
    def read_float(self) -> float:
        if not self.has_bytes(4): self.pos = len(self.buf); return 0.0
        v = struct.unpack_from("<f", self.buf, self.pos)[0]; self.pos += 4; return v
    def read_string(self) -> str:
        marker = self.read_int()
        if marker in (0, -1): return ""
        length = (-marker) - 1 if marker < -1 else marker
        if marker < -1: self.read_int()
        length = max(0, min(length, 1000000))
        if not self.has_bytes(length): return ""
        text = self.buf[self.pos:self.pos + length].decode("utf-8", errors="replace")
        self.pos += length
        return text.replace("\x00", "").strip()
    def read_list(self, item_fn):
        count = self.read_int()
        if count <= 0 or count > 1000000: return []
        res = []
        for _ in range(count):
            if self.pos >= len(self.buf): break
            val = item_fn()
            if val is not None: res.append(val)
        return res
    def read_dict(self) -> dict:
        count = self.read_int()
        if count <= 0 or count > 1000000: return {}
        return {self.read_int(): self.read_int() for _ in range(count) if self.pos < len(self.buf)}
    def read_equipment(self):
        if self.read_byte() == 0: return None
        return {k: self.read_list(self.read_int) for k in ["hair", "face", "beard", "cap", "mask", "top", "gloves", "bag", "pants", "shoes", "glasses", "SelectedEquipments"]} | {"Gender": self.read_int()}

def parse_player(buf: bytes) -> dict:
    r = Reader(buf)
    if r.read_byte() == 0: return None
    player = {"Name": r.read_string(), "money": r.read_int(), "coin": r.read_int(), "localID": r.read_string(), "boughtFsos": r.read_list(r.read_int)}
    player["FriendsID"] = r.read_list(lambda: (r.read_byte(), {"id": r.read_string(), "Name": r.read_string(), "accountID": r.read_string()})[1])
    player.update({"LevelsDoneTime": r.read_list(r.read_float), "floats": r.read_list(r.read_float), "integers": r.read_list(r.read_int), "fcar": r.read_list(r.read_int), "favouriteWheels": r.read_list(r.read_int), "favouriteVinyls": r.read_list(r.read_int), "favouriteEmojis": r.read_list(r.read_int), "personEquipmentsMale": r.read_equipment(), "personEquipmentsFemale": r.read_equipment()})
    if r.read_byte() == 0: player["platesData"] = None
    else:
        def read_vinyl(): r.read_byte(); return {"vectors": r.read_list(lambda: {"x": r.read_float(), "y": r.read_float(), "z": r.read_float()}), "v": r.read_list(r.read_string), "floats": r.read_list(r.read_float), "text": r.read_string()}
        def read_plate(): r.read_byte(); return {"plateId": r.read_int(), "frontCarId": r.read_int(), "rearCarId": r.read_int(), "vinyls": r.read_list(read_vinyl)}
        player["platesData"] = {"allPlates": r.read_list(read_plate)}

    if r.read_byte() == 0: player["carIDnStatus"] = None
    else: player["carIDnStatus"] = {"carGeneratedIDs": r.read_list(r.read_string), "carStatus": r.read_list(r.read_int)}
    
    player["allData"] = r.read_string()
    player["flags"] = r.read_dict()
    player["animations"] = r.read_list(r.read_int)
    player["emojiPacks"] = r.read_list(r.read_int)
    player["wheels"] = r.read_list(r.read_int)
    player["boughtPoliceLights"] = r.read_list(r.read_int)
    player["boughtPoliceSirens"] = r.read_list(r.read_int)
    return player

def try_parse(buf: bytes) -> dict:
    candidates = [buf, decompress(buf)]
    if candidates[1]: candidates.append(decompress(candidates[1]))
    for candidate in filter(None, candidates):
        if candidate[0] in (17, 23, 24):
            try:
                p = parse_player(candidate)
                if p and p.get("Name") is not None: return p
            except: pass
        try:
            clean = candidate[3:] if len(candidate) >= 3 and candidate[:2] == b"\xef\xbb" else candidate
            if clean and clean[0] == 123: return json.loads(clean.decode("utf-8"))
        except: pass
    return None

def decrypt_player_record(base64_text: str, uid: str, password: str = None, email: str = None) -> dict:
    try: buf = base64.b64decode(base64_text)
    except: return {"success": False, "message": "Bad base64"}
    if len(buf) < 10: return {"success": False, "message": "Too small"}
    direct = try_parse(buf)
    if direct: return {"success": True, "record": direct}
    if uid:
        try:
            decoded = decompress(xor_bytes(buf, make_xor_key(uid)))
            if decoded:
                parsed = try_parse(decoded)
                if parsed: return {"success": True, "record": parsed}
        except: pass
    for key in build_aes_keys(uid or "", password, email):
        plain = decrypt_aes(buf, key)
        if not plain: continue
        parsed = try_parse(plain)
        if parsed: return {"success": True, "record": parsed}
    return {"success": False, "message": "Could not decrypt"}

class Writer:
    def __init__(self): self._p: List[bytes] = []
    def write_byte(self, v): self._p.append(bytes([int(v or 0) & 0xFF]))
    def write_int(self, v):
        if v is None: v = 0
        self._p.append(struct.pack("<i", int(v)))
    def write_float(self, v): self._p.append(struct.pack("<f", float(v or 0.0)))
    def write_string(self, s):
        if s is None: self._p.append(struct.pack("<i", -1)); return
        s = str(s)
        if s == "": self._p.append(struct.pack("<i", 0)); return
        enc = s.encode("utf-8")
        self._p.append(struct.pack("<ii", -(len(enc)) - 1, len(s)) + enc)
    def write_list(self, lst, fn):
        if lst is None: self._p.append(struct.pack("<i", -1)); return
        self._p.append(struct.pack("<i", len(lst)))
        for item in lst: fn(item)
    def write_equipment(self, data):
        if not data:
            self.write_byte(0)
            return
        self.write_byte(1)
        for key in ["hair", "face", "beard", "cap", "mask", "top", "gloves", "bag", "pants", "shoes", "glasses", "SelectedEquipments"]:
            vals = data.get(key, []) if isinstance(data, dict) else []
            if not isinstance(vals, list):
                vals = []
            clean = []
            for v in vals:
                try:
                    clean.append(int(v))
                except Exception:
                    continue
            if key == "SelectedEquipments":
                while len(clean) < 11:
                    clean.append(-1)
                clean = clean[:11]
            self.write_list(clean, self.write_int)
        try:
            self.write_int(int((data or {}).get("Gender", 0)))
        except Exception:
            self.write_int(0)
    def write_plates(self, data):
        if not data: self.write_byte(0); return
        self.write_byte(1)
        plates = data.get("allPlates", [])
        self._p.append(struct.pack("<i", len(plates)))
        for plate in plates:
            self.write_byte(4); self.write_int(plate.get("plateId", 0)); self.write_int(plate.get("frontCarId", 0)); self.write_int(plate.get("rearCarId", 0))
            vinyls = plate.get("vinyls", [])
            self._p.append(struct.pack("<i", len(vinyls)))
            for vinyl in vinyls:
                self.write_byte(4)
                vecs = vinyl.get("vectors", [])
                self._p.append(struct.pack("<i", len(vecs)))
                for vec in vecs: self._p.append(struct.pack("<fff", vec.get("x", 0), vec.get("y", 0), vec.get("z", 0)))
                self.write_list(vinyl.get("v", []), self.write_string)
                self.write_list(vinyl.get("floats", []), self.write_float)
                self.write_string(vinyl.get("text", ""))
    def write_car_id_status(self, data):
        if not data: self.write_byte(0); return
        self.write_byte(2)
        self.write_list(data.get("carGeneratedIDs", []), self.write_string)
        self.write_list(data.get("carStatus", []), self.write_int)
    def to_bytes(self): return b"".join(self._p)

FIELD_MAPPING = [(1, "localID"), (2, "money"), (3, "Name"), (4, "coin"), (5, "allData"), (6, "boughtFsos"), (7, "boughtPoliceLights"), (8, "boughtPoliceSirens"), (9, "FriendsID"), (10, "LevelsDoneTime"), (11, "floats"), (12, "integers"), (13, "fcar"), (14, "favouriteWheels"), (15, "favouriteVinyls"), (16, "favouriteEmojis"), (18, "emojiPacks"), (41, "personEquipmentsMale"), (42, "personEquipmentsFemale"), (43, "platesData"), (44, "carIDnStatus"), (45, "flags"), (46, "animations"), (48, "wheels")]
INT_LIST_FIELDS = {6, 7, 8, 12, 13, 14, 15, 16, 18, 46, 48}
FLOAT_LIST_FIELDS = {10, 11}

def _field_modified(new_value, old_value) -> bool:
    if new_value is None and old_value is None: return False
    if new_value is None or old_value is None: return True
    if type(new_value) != type(old_value): return True
    if isinstance(new_value, (dict, list)): return json.dumps(new_value, sort_keys=True) != json.dumps(old_value, sort_keys=True)
    return new_value != old_value

def serialize_field(fid: int, value: Any) -> Optional[bytes]:
    w = Writer()
    if fid in (1, 3, 5): w.write_string(value); return w.to_bytes()
    if fid in (2, 4): w.write_int(value or 0); return w.to_bytes()
    if fid == 9:
        friends = value or []
        w._p.append(struct.pack("<i", len(friends)))
        for friend in friends: w.write_byte(3); w.write_string(friend.get("id", "")); w.write_string(friend.get("Name", "")); w.write_string(friend.get("accountID", ""))
        return w.to_bytes()
    if fid in INT_LIST_FIELDS: w.write_list(value or [], w.write_int); return w.to_bytes()
    if fid in FLOAT_LIST_FIELDS: w.write_list(value or [], w.write_float); return w.to_bytes()
    if fid in (41, 42): w.write_equipment(value); return w.to_bytes()
    if fid == 43: w.write_plates(value); return w.to_bytes()
    if fid == 44: w.write_car_id_status(value); return w.to_bytes()
    if fid == 45:
        w._p.append(struct.pack("<i", len(value or {})))
        for key, val in (value or {}).items(): w.write_int(int(key)); w.write_int(int(val))
        return w.to_bytes()
    return None

def build_payload(record: Dict[str, Any], uid: str, original: Optional[Dict[str, Any]] = None, force_fields: Optional[set] = None) -> str:
    force_fields = set(force_fields or [])
    fields = []
    for fid, key in FIELD_MAPPING:
        value = record.get(key)
        if value is None: continue
        if key == "allData": should_send = isinstance(value, str) and len(value) > 0
        elif key in force_fields: should_send = True
        elif original is not None: should_send = _field_modified(value, original.get(key))
        else: should_send = True
        if not should_send: continue
        raw = serialize_field(fid, value)
        if raw is not None: fields.append((fid, raw))
    parts = [struct.pack("<i", len(fields))]
    for fid, raw in fields: parts.extend([struct.pack("<hi", fid, len(raw)), raw])
    combined = b"".join(parts)
    compressed = brotli.compress(combined) if HAS_BROTLI else zlib.compress(combined)
    encrypted = xor_bytes(compressed, make_xor_key(uid))
    return base64.b64encode(encrypted).decode("ascii")

class SyncCPMNuker:
    def __init__(self):
        self.cache = {}

    def _ck(self, uid: int, email: Optional[str] = None) -> str:
        td = self.get_token_data(uid)
        return f"{uid}_{email or (td.get('email') if td else '')}"

    def save_token(self, uid: int, auth: str, email: str, pw: Optional[str] = None, rt: Optional[str] = None, fuid: Optional[str] = None):
        if USE_MONGO:
            try:
                tokens_col.update_one({"user_id": uid}, {"$set": {"auth_token": auth, "email": email, "password": pw, "refresh_token": rt, "firebase_uid": fuid, "token_expires_at": time.time() + 3600}}, upsert=True)
            except: pass
        else:
            with sqlite3.connect(db_path) as c:
                c.execute("INSERT OR REPLACE INTO tokens (user_id, auth_token, email, password, refresh_token, firebase_uid, token_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (uid, auth, email, pw, rt, fuid, time.time() + 3600))
                c.commit()

    def get_token_data(self, uid: int) -> Optional[Dict[str, Any]]:
        if USE_MONGO:
            try:
                row = tokens_col.find_one({"user_id": uid})
                if not row: return None
                return row
            except: return None
        else:
            with sqlite3.connect(db_path) as c:
                row = c.execute("SELECT auth_token, email, password, refresh_token, firebase_uid, token_expires_at FROM tokens WHERE user_id=?", (uid,)).fetchone()
            if not row: return None
            return {"auth_token": row[0], "email": row[1], "password": row[2], "refresh_token": row[3], "firebase_uid": row[4], "token_expires_at": row[5]}

    def update_token(self, uid: int, auth: str, rt: Optional[str] = None):
        if USE_MONGO:
            try:
                update_data = {"auth_token": auth, "token_expires_at": time.time() + 3600}
                if rt: update_data["refresh_token"] = rt
                tokens_col.update_one({"user_id": uid}, {"$set": update_data})
            except: pass
        else:
            with sqlite3.connect(db_path) as c:
                if rt: c.execute("UPDATE tokens SET auth_token=?, refresh_token=?, token_expires_at=? WHERE user_id=?", (auth, rt, time.time() + 3600, uid))
                else: c.execute("UPDATE tokens SET auth_token=?, token_expires_at=? WHERE user_id=?", (auth, time.time() + 3600, uid))
                c.commit()

    def delete_token(self, uid: int):
        if USE_MONGO:
            try: tokens_col.delete_one({"user_id": uid})
            except: pass
        else:
            with sqlite3.connect(db_path) as c:
                c.execute("DELETE FROM tokens WHERE user_id=?", (uid,))
                c.commit()
        for key in list(self.cache.keys()):
            if key.startswith(str(uid)): del self.cache[key]

    def is_expired(self, uid: int) -> bool:
        td = self.get_token_data(uid)
        return not td or not td.get("token_expires_at") or td.get("token_expires_at") < time.time()

    def get_record(self, uid: int, email: Optional[str] = None) -> Dict[str, Any]:
        ck = self._ck(uid, email)
        if ck not in self.cache:
            if USE_MONGO:
                try:
                    doc = user_data_col.find_one({"cache_key": ck})
                    if doc and "data_json" in doc: self.cache[ck] = json.loads(doc["data_json"])
                except: pass
            else:
                with sqlite3.connect(db_path) as c:
                    row = c.execute("SELECT data_json FROM user_data WHERE cache_key=?", (ck,)).fetchone()
                if row:
                    try: self.cache[ck] = json.loads(row[0])
                    except: pass
        return self.cache.get(ck, {})

    def set_record(self, uid: int, data: Dict[str, Any], email: Optional[str] = None):
        ck = self._ck(uid, email)
        self.cache[ck] = data
        if USE_MONGO:
            try:
                user_data_col.update_one({"cache_key": ck}, {"$set": {"email": email, "data_json": json.dumps(data), "saved_at": datetime.now()}}, upsert=True)
            except: pass
        else:
            with sqlite3.connect(db_path) as c:
                c.execute("INSERT OR REPLACE INTO user_data (cache_key, email, data_json) VALUES (?, ?, ?)", (ck, email, json.dumps(data)))
                c.commit()

    def _post(self, url: str, payload: Dict[str, Any], headers: Dict[str, str]) -> Dict[str, Any]:
        try:
            clean_headers = {k: v for k, v in headers.items() if k.lower() != "host"}
            resp = http_session.post(url, json=payload, headers=clean_headers, timeout=15)
            try: return resp.json()
            except: return {"raw": resp.text, "status": resp.status_code, "ok": False}
        except Exception as e:
            return {"ok": False, "message": f"CONNECTION FAILED."}

    def login(self, email: str, password: str) -> Dict[str, Any]:
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FK}"
        payload = {"email": email, "password": password, "returnSecureToken": True, "clientType": "CLIENT_TYPE_ANDROID"}
        result = self._post(url, payload, GAME_HEADERS)
        if result.get("ok") is False: return result
        if "idToken" in result: return {"ok": True, "message": "OK", "auth": result["idToken"], "refresh_token": result.get("refreshToken", ""), "firebase_uid": result.get("localId", "")}
        err = "INVALID_CREDENTIALS"
        try:
            if isinstance(result.get("error"), dict): err = str(result["error"].get("message", "INVALID_CREDENTIALS"))
            elif isinstance(result.get("error"), str): err = result["error"]
        except: pass
        return {"ok": False, "message": err.upper()[:80]}

    def register(self, email: str, password: str) -> Dict[str, Any]:
        email = (email or "").strip()
        password = password or ""
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            return {"ok": False, "message": "INVALID_EMAIL"}
        if len(password) < 6:
            return {"ok": False, "message": "WEAK_PASSWORD (min 6 chars)"}
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FK}"
        payload = {
            "email": email,
            "password": password,
            "returnSecureToken": True,
            "clientType": "CLIENT_TYPE_ANDROID",
        }
        result = self._post(url, payload, GAME_HEADERS)
        if result.get("ok") is False:
            return {"ok": False, "message": result.get("message", "CONNECTION FAILED")}
        if "idToken" in result:
            return {
                "ok": True,
                "message": "OK",
                "auth": result["idToken"],
                "refresh_token": result.get("refreshToken", ""),
                "firebase_uid": result.get("localId", ""),
            }
        err = "REGISTRATION_FAILED"
        try:
            if isinstance(result.get("error"), dict):
                err = str(result["error"].get("message", "FAILED"))
            elif isinstance(result.get("error"), str):
                err = result["error"]
        except Exception:
            pass
        el = err.upper()
        if "EMAIL_EXISTS" in el:
            err = "EMAIL_EXISTS — use Login"
        elif "INVALID_EMAIL" in el:
            err = "INVALID_EMAIL"
        elif "WEAK_PASSWORD" in el:
            err = "WEAK_PASSWORD — min 6 characters"
        elif "OPERATION_NOT_ALLOWED" in el:
            err = "SIGNUP_DISABLED on Firebase"
        elif "TOO_MANY_ATTEMPTS" in el:
            err = "TOO_MANY_ATTEMPTS — try later"
        return {"ok": False, "message": err[:100]}


    def _refresh(self, uid: int) -> Tuple[bool, str]:
        td = self.get_token_data(uid)
        if not td: return False, "NO_TOKEN"
        rt, em, pw = td.get("refresh_token"), td.get("email"), td.get("password")
        if rt:
            res = self._post(f"https://securetoken.googleapis.com/v1/token?key={FK}", {"grant_type": "refresh_token", "refresh_token": rt}, {"Content-Type": "application/json"})
            if res.get("id_token"):
                self.update_token(uid, res["id_token"], res.get("refresh_token", rt))
                return True, "OK"
        if em and pw:
            res = self.login(em, pw)
            if res.get("ok"):
                self.save_token(uid, res["auth"], em, pw, res.get("refresh_token", ""), res.get("firebase_uid", ""))
                return True, "OK"
        return False, "REFRESH_FAILED"

    def get_auth(self, uid: int) -> Tuple[bool, str, str]:
        if self.is_expired(uid):
            ok, msg = self._refresh(uid)
            if not ok: return False, msg, ""
        td = self.get_token_data(uid)
        if td and td.get("auth_token"): return True, "OK", td.get("auth_token")
        return False, "NO_TOKEN", ""

    def load(self, uid: int, force: bool = False) -> bool:
        td = self.get_token_data(uid)
        if not td: return False
        if not force and self._ck(uid) in self.cache: return True
        ok, msg, auth = self.get_auth(uid)
        if not ok: return False
        res = self._post(LOAD_URL, {"data": None}, {**GAME_HEADERS, "Authorization": f"Bearer {auth}"})
        if res.get("ok") is False or not res.get("result"): return False
        dec = decrypt_player_record(res["result"], td.get("firebase_uid", ""), td.get("password", ""), td.get("email", ""))
        if dec.get("success") and dec.get("record"):
            self.set_record(uid, dec["record"], td.get("email", ""))
            return True
        return False

    def _ok(self, value: Any) -> bool:
        if value in (1, True, "1"): return True
        if value in (0, False, None, "0"): return False
        if isinstance(value, str):
            try: return self._ok(json.loads(value.strip()))
            except: return False
        if isinstance(value, dict):
            for k in ("result", "ok", "success"):
                if k in value: return self._ok(value[k])
        return False

    def _send(self, auth: str, record: Dict[str, Any], fuid: str, original: Optional[Dict[str, Any]] = None, force_fields: Optional[set] = None) -> Tuple[bool, str]:
        if not fuid: return False, "NO_FIREBASE_UID"
        try:
            payload = build_payload(record, fuid, original, force_fields=force_fields)
            res = self._post(SAVE_URL, {"data": {"data": payload, "deviceId": fuid[:8]}}, {**GAME_HEADERS, "Authorization": f"Bearer {auth}", "Connection": "Keep-Alive", "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 12; Pixel 6 Build/SD1A.210817.036)"})
            if res.get("ok") is False: return False, res.get("message", "API TIMEOUT")
            if res and self._ok(res): return True, "OK"
            return False, f"SAVE-FAILED"
        except Exception as e: return False, str(e)

    def _save(self, uid: int, data: Dict[str, Any], force_fields: Optional[set] = None) -> Dict[str, Any]:
        ok, msg, auth = self.get_auth(uid)
        if not ok: return {"ok": False, "message": msg}
        td = self.get_token_data(uid)
        fuid = td.get("firebase_uid", "") if td else ""
        email = td.get("email", "") if td else ""
        original = self.get_record(uid, email) or None
        ok2, msg2 = self._send(auth, data, fuid, original, force_fields=force_fields)
        if ok2:
            self.set_record(uid, data, email)
            return {"ok": True, "message": "OK"}
        return {"ok": False, "message": msg2}

    def _modify(self, uid: int, mods: Dict[str, Any], force_fields: Optional[set] = None) -> Dict[str, Any]:
        if not self.load(uid): return {"ok": False, "message": "ACCOUNT LOAD FAILED. CHECK CREDENTIALS."}
        td = self.get_token_data(uid)
        data = deepcopy(self.get_record(uid, td.get("email") if td else None))
        if not data or data.get("Name") is None: return {"ok": False, "message": "PROFILE DATA CORRUPTED."}
        for k, v in mods.items():
            if k == "money": v = min(int(v), MAX_MONEY)
            if k == "coin": v = min(int(v), MAX_COIN)
            data[k] = v
        return self._save(uid, data, force_fields=set(force_fields or mods.keys()))

    def _set_floats(self, uid: int, indices_values: List[Tuple[int, float]]) -> Dict[str, Any]:
        if not self.load(uid): return {"ok": False, "message": "ACCOUNT LOAD FAILED."}
        td = self.get_token_data(uid)
        data = deepcopy(self.get_record(uid, td.get("email") if td else None))
        if not data or data.get("Name") is None: return {"ok": False, "message": "PROFILE DATA CORRUPTED."}
        floats = data.get("floats", [])
        max_idx = max(idx for idx, _ in indices_values)
        while len(floats) <= max_idx: floats.append(0.0)
        for idx, val in indices_values: floats[idx] = float(val)
        data["floats"] = floats
        return self._save(uid, data, force_fields={"floats"})

    def _set_integers(self, uid: int, indices_values: List[Tuple[int, int]]) -> Dict[str, Any]:
        if not self.load(uid): return {"ok": False, "message": "ACCOUNT LOAD FAILED."}
        td = self.get_token_data(uid)
        data = deepcopy(self.get_record(uid, td.get("email") if td else None))
        if not data or data.get("Name") is None: return {"ok": False, "message": "PROFILE DATA CORRUPTED."}
        integers = data.get("integers", [])
        max_idx = max(idx for idx, _ in indices_values)
        while len(integers) <= max_idx: integers.append(0)
        for idx, val in indices_values: integers[idx] = int(val)
        data["integers"] = integers
        return self._save(uid, data, force_fields={"integers"})

    def set_money(self, uid: int, amount: int) -> Dict[str, Any]: return self._modify(uid, {"money": min(int(amount), MAX_MONEY)}, force_fields={"money"})
    def set_coin(self, uid: int, amount: int) -> Dict[str, Any]: return self._modify(uid, {"coin": min(int(amount), MAX_COIN)}, force_fields={"coin"})
    
    def change_player_id(self, uid: int, new_id: str) -> Dict[str, Any]:
        if not self.load(uid, force=True): return {"ok": False, "message": "ACCOUNT LOAD FAILED."}
        td = self.get_token_data(uid)
        data = deepcopy(self.get_record(uid, td.get("email") if td else None))
        if not data or data.get("Name") is None: return {"ok": False, "message": "PROFILE DATA CORRUPTED."}
        new_id_upper = str(new_id).strip().upper()
        data["localID"] = new_id_upper
        result = self._save(uid, data, force_fields={"localID"})
        if result.get("ok"): return {"ok": True, "message": f"TAG MASKED TO {new_id_upper}", "new_id": new_id_upper}
        return {"ok": False, "message": result.get("message", "SAVE FAILED")}
        
    def change_player_name(self, uid: int, new_name: str) -> Dict[str, Any]:
        if not self.load(uid, force=True): return {"ok": False, "message": "ACCOUNT LOAD FAILED."}
        td = self.get_token_data(uid)
        data = deepcopy(self.get_record(uid, td.get("email") if td else None))
        data["Name"] = new_name
        return self._save(uid, data, force_fields={"Name"})

    def change_email(self, uid: int, new_email: str) -> Dict[str, Any]:
        td = self.get_token_data(uid)
        if not td: return {"ok": False, "message": "Not logged in"}
        return {"ok": True, "message": "OK"}

    def unlock_w16(self, uid: int) -> Dict[str, Any]: return self._set_floats(uid, [(32, 1.0)])
    def unlock_horns(self, uid: int) -> Dict[str, Any]: return self._set_floats(uid, [(27, 1.0), (28, 1.0), (29, 1.0), (30, 1.0), (31, 1.0)])
    def disable_damage(self, uid: int) -> Dict[str, Any]: return self._set_floats(uid, [(34, 1.0)])
    def unlimited_fuel(self, uid: int) -> Dict[str, Any]: return self._set_floats(uid, [(3, 1.0)])
    def unlock_smoke(self, uid: int) -> Dict[str, Any]: return self._set_floats(uid, [(33, 1.0)])
    
    def unlock_animations(self, uid: int) -> Dict[str, Any]:
        if not self.load(uid): return {"ok": False, "message": "ACCOUNT LOAD FAILED."}
        td = self.get_token_data(uid)
        data = deepcopy(self.get_record(uid, td.get("email") if td else None))
        data["animations"] = sorted(set(data.get("animations", []) + list(range(301))))
        return self._save(uid, data, force_fields={"animations"})

    def unlock_wheels(self, uid: int) -> Dict[str, Any]:
        if not self.load(uid): return {"ok": False, "message": "ACCOUNT LOAD FAILED."}
        td = self.get_token_data(uid)
        data = deepcopy(self.get_record(uid, td.get("email") if td else None))
        data["wheels"] = sorted(set(data.get("wheels", []) + list(range(73, 221))))
        integers = data.get("integers", [])
        while len(integers) < 113: integers.append(0)
        for idx in [0, 1, 2, 3, 4, 5, 110, 111, 112]: integers[idx] = 1
        data["integers"] = integers
        return self._save(uid, data, force_fields={"wheels", "integers"})

    def unlock_houses(self, uid: int) -> Dict[str, Any]: return self._set_integers(uid, [(8, 1), (110, 1), (111, 1), (112, 1)])
    def complete_all_levels(self, uid: int) -> Dict[str, Any]: return self._modify(uid, {"LevelsDoneTime": [0] + [120 if i == 43 else 1 for i in range(1, 110)]}, force_fields={"LevelsDoneTime"})

    def set_rank(self, uid: int) -> Dict[str, Any]:
        self.load(uid)
        ok, msg, auth = self.get_auth(uid)
        if not ok: return {"ok": True, "message": "OK"}
        rating_data = {"RatingData": {"time": 1e22, "cars": 1e16, "car_fix": 1e13, "car_collided": 1e12, "car_exchange": 1e13, "car_trade": 1e13, "car_wash": 1e13, "slicer_cut": 1e13, "drift_max": 1e14, "drift": 1e14, "cargo": 1e5, "delivery": 1e5, "race_win": 3e20, "taxi": 1e10, "levels": 10000990000, "gifts": 1e9, "fuel": 1e10, "offroad": 1e10, "speed_banner": 1e9, "reactions": 1e17, "run": 1e9, "real_estate": 1e9, "t_distance": 1e10, "treasure": 1e10, "block_post": 1e10, "push_ups": 1e12, "burnt_tire": 1e10, "passanger_distance": 1e8}}
        try: self._post(RANK_URL, {"data": json.dumps(rating_data)}, {**GAME_HEADERS, "Authorization": f"Bearer {auth}"})
        except: pass
        return {"ok": True, "message": "OK"}

    def _normalize_equipment(self, equipment: Dict[str, Any], gender: int) -> Dict[str, Any]:
        list_fields = ["hair", "face", "beard", "cap", "mask", "top", "gloves", "bag", "pants", "shoes", "glasses", "SelectedEquipments"]
        normalized = {key: [int(v) for v in (equipment.get(key, []) if isinstance(equipment, dict) else [])] for key in list_fields}
        normalized["Gender"] = int(gender)
        return normalized

    def unlock_all_clothes(self, uid: int) -> Dict[str, Any]:
        """Unlock male + female wardrobe from clothes.json catalog."""
        if not self.load(uid, force=True):
            return {"ok": False, "message": "ACCOUNT LOAD FAILED. Login again."}
        td = self.get_token_data(uid)
        if not td:
            return {"ok": False, "message": "No session token. Login again."}
        email = td.get("email")
        data = deepcopy(self.get_record(uid, email))
        if not data or data.get("Name") is None:
            return {"ok": False, "message": "PROFILE DATA CORRUPTED. Re-login."}

        slots = ["hair", "face", "beard", "cap", "mask", "top", "gloves", "bag", "pants", "shoes", "glasses"]
        catalog_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clothes.json")
        if not os.path.isfile(catalog_path):
            return {"ok": False, "message": "clothes.json missing next to cpm1_core.py"}
        try:
            with open(catalog_path, "r", encoding="utf-8") as fh:
                catalog = json.load(fh)
            male_base = catalog.get("male") if isinstance(catalog, dict) else None
            female_base = catalog.get("female") if isinstance(catalog, dict) else None
            if not isinstance(male_base, dict) or not isinstance(female_base, dict):
                raise ValueError("male/female missing")
        except Exception as e:
            return {"ok": False, "message": "CLOTHES CATALOG invalid: %s" % str(e)[:80]}

        def _ids(existing, new_ids):
            out = set()
            for src in (existing, new_ids):
                if not isinstance(src, list):
                    continue
                for v in src:
                    try:
                        iv = int(v)
                    except Exception:
                        continue
                    if iv < 0:
                        continue
                    out.add(iv)
            return sorted(out)

        def _selected(existing):
            selected = []
            raw = existing.get("SelectedEquipments") if isinstance(existing, dict) else None
            if isinstance(raw, list):
                for item in raw[:11]:
                    try:
                        selected.append(int(item))
                    except Exception:
                        selected.append(-1)
            while len(selected) < 11:
                selected.append(-1)
            return selected[:11]

        cur_m = data.get("personEquipmentsMale") if isinstance(data.get("personEquipmentsMale"), dict) else {}
        cur_f = data.get("personEquipmentsFemale") if isinstance(data.get("personEquipmentsFemale"), dict) else {}

        eq_male = {"Gender": 0, "SelectedEquipments": _selected(cur_m)}
        eq_female = {"Gender": 1, "SelectedEquipments": _selected(cur_f)}
        for slot in slots:
            eq_male[slot] = _ids(cur_m.get(slot), male_base.get(slot) if isinstance(male_base.get(slot), list) else [])
            eq_female[slot] = _ids(cur_f.get(slot), female_base.get(slot) if isinstance(female_base.get(slot), list) else [])

        data["personEquipmentsMale"] = self._normalize_equipment(eq_male, 0)
        data["personEquipmentsFemale"] = self._normalize_equipment(eq_female, 1)

        force = {"personEquipmentsMale", "personEquipmentsFemale"}
        res = self._save(uid, data, force_fields=force)
        if res.get("ok"):
            m_count = sum(len(data["personEquipmentsMale"].get(s, []) or []) for s in slots)
            f_count = sum(len(data["personEquipmentsFemale"].get(s, []) or []) for s in slots)
            res["message"] = "Clothes unlocked (male ~%s items, female ~%s items). Reopen game." % (m_count, f_count)
            return res

        # Retry: refresh auth + reload then save again
        ok_auth, msg_auth, _ = self.get_auth(uid)
        if not self.load(uid, force=True):
            return {"ok": False, "message": res.get("message") or "CLOTHES SAVE FAILED (reload)"}
        data2 = deepcopy(self.get_record(uid, email) or {})
        if not data2:
            return {"ok": False, "message": res.get("message") or "CLOTHES SAVE FAILED"}
        data2["personEquipmentsMale"] = data["personEquipmentsMale"]
        data2["personEquipmentsFemale"] = data["personEquipmentsFemale"]
        res2 = self._save(uid, data2, force_fields=force)
        if res2.get("ok"):
            res2["message"] = "Clothes unlocked (retry OK). Reopen game."
            return res2

        # Last resort: save genders one at a time
        notes = []
        for label, key, payload in (
            ("male", "personEquipmentsMale", data["personEquipmentsMale"]),
            ("female", "personEquipmentsFemale", data["personEquipmentsFemale"]),
        ):
            if not self.load(uid, force=True):
                notes.append("%s:reload-fail" % label)
                continue
            piece = deepcopy(self.get_record(uid, email) or {})
            piece[key] = payload
            r = self._save(uid, piece, force_fields={key})
            notes.append("%s:%s" % (label, "OK" if r.get("ok") else (r.get("message") or "fail")))
        ok_any = any(x.endswith(":OK") for x in notes)
        return {
            "ok": ok_any,
            "message": ("Clothes partial: " if ok_any else "Clothes failed: ") + " · ".join(notes),
        }



    def unlock_all_features(self, uid: int) -> Dict[str, Any]:
        feature_calls = [("W16 Engine", self.unlock_w16), ("Horns", self.unlock_horns), ("No Damage", self.disable_damage), ("Unlimited Fuel", self.unlimited_fuel), ("Smoke", self.unlock_smoke), ("Animations", self.unlock_animations), ("Wheels", self.unlock_wheels), ("Houses", self.unlock_houses), ("All Levels", self.complete_all_levels), ("All Clothes", self.unlock_all_clothes), ("Max Rank", self.set_rank)]
        if not self.load(uid, force=True): return {"ok": False, "message": "ACCOUNT LOAD FAILED."}
        results, failed = [], []
        for name, fn in feature_calls:
            res = fn(uid)
            if res.get("ok"): results.append(name)
            else: failed.append(f"{name}: {res.get('message', 'Failed')}")
        return {"ok": not failed, "message": f"Unlocked {len(results)}/{len(feature_calls)} features"}
        
    def fix_account(self, uid: int) -> Dict[str, Any]:
        if not self.load(uid, force=True): return {"ok": False, "message": "ACCOUNT LOAD FAILED."}
        td = self.get_token_data(uid)
        data = deepcopy(self.get_record(uid, td.get("email") if td else None))
        if data.get("money", 0) > MAX_MONEY: data["money"] = MAX_MONEY
        if data.get("coin", 0) > MAX_COIN: data["coin"] = MAX_COIN
        flags = data.get("flags", {})
        if isinstance(flags, dict):
            for bad_flag in [0, 1, 2, "0", "1", "2"]:
                flags.pop(bad_flag, None)
            data["flags"] = flags
        return self._save(uid, data, force_fields={"money", "coin", "flags"})



    def seed_blank_profile(self, auth: str, fuid: str, email: str = "") -> Dict[str, Any]:
        """Create initial CPM player record for a brand-new Firebase account."""
        if not auth or not fuid:
            return {"ok": False, "message": "NO_AUTH_OR_UID"}
        import random as _rnd
        blank = {
            "Name": "Player",
            "money": 25000,
            "coin": 0,
            "localID": str(_rnd.randint(1000000, 9999999)).zfill(8),
            "allData": '{"cars":[]}',
            "floats": [0.0] * 50,
            "integers": [0] * 120,
            "LevelsDoneTime": [0.0] * 110,
            "wheels": [],
            "animations": [],
            "boughtFsos": [],
            "boughtPoliceLights": [],
            "boughtPoliceSirens": [],
            "favouriteWheels": [],
            "favouriteVinyls": [],
            "favouriteEmojis": [],
            "emojiPacks": [],
            "fcar": [],
            "flags": {},
            "personEquipmentsMale": {"Gender": 0, "SelectedEquipments": [-1] * 11},
            "personEquipmentsFemale": {"Gender": 1, "SelectedEquipments": [-1] * 11},
        }
        force = {
            "Name", "money", "coin", "localID", "allData",
            "floats", "integers", "LevelsDoneTime",
            "personEquipmentsMale", "personEquipmentsFemale",
        }
        ok, msg = self._send(auth, blank, fuid, original=None, force_fields=force)
        if ok:
            return {"ok": True, "message": "PROFILE_SEEDED", "localID": blank["localID"]}
        # retry once
        ok2, msg2 = self._send(auth, blank, fuid, original=None, force_fields=force)
        if ok2:
            return {"ok": True, "message": "PROFILE_SEEDED_RETRY", "localID": blank["localID"]}
        return {"ok": False, "message": msg2 or msg or "SEED_FAILED"}

    def get_account_info(self, uid: int, force_refresh: bool = False) -> Dict[str, Any]:
        if not self.load(uid, force=force_refresh): return {"ok": False}
        td = self.get_token_data(uid)
        if not td: return {"ok": False}
        data = self.get_record(uid, td.get("email"))
        if not data or data.get("Name") is None: return {"ok": False}
        
        cars_count = 0
        try:
            c_status = data.get('carIDnStatus')
            if isinstance(c_status, dict):
                c_list = c_status.get('carStatus', [])
                if isinstance(c_list, list): cars_count = len(c_list)
        except: pass

        if cars_count == 0:
            try:
                ad = data.get('allData', '{}')
                if isinstance(ad, str):
                    ad_json = json.loads(ad)
                    if isinstance(ad_json, dict):
                        cars_count = len(ad_json.get('cars', []))
            except: pass
        
        return {"ok": True, "name": data.get("Name", "Unknown"), "money": data.get("money", 0), "coin": data.get("coin", 0), "localID": data.get("localID", "Unknown"), "email": td.get("email"), "cars": cars_count}

nuker = SyncCPMNuker()
