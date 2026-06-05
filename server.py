"""
InstagramOps — Serveur Flask
Lance avec : python server.py
Puis ouvre : http://localhost:5004
"""
import threading as _threading
_thread_local = _threading.local()
from flask import Flask, jsonify, request, send_from_directory, Response, send_file
from flask_cors import CORS
import signal
import subprocess
import threading
import queue
import json
import os
import sys
import time
import re
import uuid
import hashlib
import requests as req
from datetime import datetime, timezone
import io

# ─────────────────────────────────────────────────────────────────────────────
# Support binaire compilé (Nuitka / PyInstaller) + portage Windows
# ─────────────────────────────────────────────────────────────────────────────
import tempfile
# Dossier temporaire portable (Windows: %TEMP%, macOS: /var/folders/..., Linux: /tmp).
# DOIT être identique à celui de code.py pour le partage des photos de profil.
_TMP_DIR = tempfile.gettempdir()

IS_FROZEN = bool(getattr(sys, "frozen", False) or globals().get("__compiled__"))

def _bundle_dir():
    """Dossier des ressources embarquées (panel.html, code.py, worker.py…)."""
    if hasattr(sys, "_MEIPASS"):                        # PyInstaller
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))   # Nuitka / dev

def _app_dir():
    """Dossier persistant (écriture des .json) : à côté de l'exécutable RÉEL.

    ⚠️ Avec Nuitka onefile, l'exe s'extrait dans un dossier temporaire qui change
    à chaque lancement (…/Temp/onefile_xxx/). Il NE FAUT PAS écrire là (perdu au
    redémarrage). On vise le dossier du binaire d'origine lancé par l'utilisateur.
    """
    if IS_FROZEN:
        candidates = [
            os.environ.get("NUITKA_ONEFILE_BINARY"),  # fourni par Nuitka onefile (autoritatif)
            sys.argv[0] if sys.argv else None,
            sys.executable,
        ]
        for c in candidates:
            if not c:
                continue
            d = os.path.dirname(os.path.abspath(c))
            dl = d.replace("/", "\\").lower()
            # Écarter le dossier temporaire d'extraction Nuitka onefile
            if d and "onefile_" not in os.path.basename(d).lower() and "\\temp\\" not in dl:
                return d
        # Dernier recours
        return os.path.dirname(os.path.abspath(sys.argv[0] or sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

# Partagé avec code.py / worker.py pour qu'ils écrivent au même endroit.
os.environ.setdefault("DATA_DIR", _app_dir())

# Mode worker : le binaire compilé se relance lui-même pour chaque compte
# (en .exe, `python worker.py` n'existe pas → on dispatche via `exe --worker`).
if len(sys.argv) > 2 and sys.argv[1] == "--worker":
    sys.argv = [sys.argv[0], sys.argv[2]]   # worker.py lit la config dans argv[1]
    import worker                            # exécute worker.py (qui appelle sys.exit)
    sys.exit(0)

# Libère le port 5004 si déjà occupé (best-effort, Unix uniquement).
if sys.platform != "win32":
    os.system("lsof -ti:5004 | xargs kill -9 2>/dev/null")

sys.path.insert(0, _bundle_dir())

instagram_code = None
INSTAGRAM_CODE_LOADED = False

def _lazy_load_instagram_code():
    global instagram_code, INSTAGRAM_CODE_LOADED
    try:
        import insta_core as _tc
        instagram_code = _tc
        INSTAGRAM_CODE_LOADED = True
        apply_config_to_code()
        _load_panel_settings()  # applique FIRST_NAMES, MENTION_TAGS, etc. au module fraîchement chargé
        if hasattr(instagram_code, 'set_pool_log_callback'):
            instagram_code.set_pool_log_callback(pool_log_to_sse)
        try:
            instagram_code.set_debug_queue(debug_input_queue)
        except Exception:
            pass
        sys.__stdout__.write("[INFO] code.py chargé ✅\n")
        sys.__stdout__.flush()
    except Exception as e:
        sys.__stdout__.write(f"[WARN] Impossible de charger code.py : {e}\n")
        sys.__stdout__.flush()



app = Flask(__name__, static_folder=".")
CORS(app)

# ── État global ───────────────────────────────────────────────────────────────
session_state = {
    "running": False,
    "done": 0, "total": 0, "success": 0, "errors": 0,
    "current_account": None, "current_step": None,
    "current_device": None, "current_city": None,
}
# Multi-sessions support
active_sessions = []   # liste de dicts {id, thread, state, stop_flag}
_sessions_lock = _threading.Lock()
_session_id_counter = 0

log_queue  = queue.Queue()
all_logs        = []
all_logs_swipe  = []
log_queue_swipe = queue.Queue()
all_logs_pool   = []
log_queue_pool  = queue.Queue()
import threading as _threading
_logs_lock = _threading.Lock()

# ── GeeLark active URLs (phone_id → {url, label, sess_id}) ───────────────────
_active_geelark_urls = {}
_geelark_urls_lock   = _threading.Lock()

# Per-user log stores — keyed by username, each is a list of log entries
# Only logs with a known owner go here; system/unowned logs stay in the global lists only
_user_logs       = {}   # username → [entry, ...]
_user_logs_swipe = {}   # username → [entry, ...]
_user_logs_pool  = {}   # username → [entry, ...]
_user_logs_lock  = _threading.Lock()
_MAX_USER_LOGS   = 2000
# ── Répertoire de données persistant ──────────────────────────────────────────
_DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(_DATA_DIR, exist_ok=True)

accounts_file      = os.path.join(_DATA_DIR, "accounts.txt")
proxies_file       = os.path.join(_DATA_DIR, "proxies.json")
SWIPE_PROXIES_FILE = os.path.join(_DATA_DIR, "swipe_proxies.json")
stop_requested = False

# ── Config globale persistante ─────────────────────────────────────────────────
CONFIG_FILE = os.path.join(_DATA_DIR, "config.json")
DEFAULT_CONFIG = {
    "geelark": {
        "app_id": "",
        "api_key": "",
        "bearer": ""
    },
    "sms": {
        "active_provider": "herosms",
        "country": "78",
        "service": "oi",
        "providers": {
            "herosms":        {"enabled": True,  "api_key": "", "service": "oi", "country": 78, "dial_code": "+33", "max_price": 1.15},
            "smspin":         {"enabled": False, "api_key": "", "country_name": "France", "app": "Tinder 5", "dial_code": "+33"},
            "smsbower":       {"enabled": False, "api_key": "", "service": "oi", "country": 78, "dial_code": "+33", "max_price": 0.33},
            "sim_aggregator": {"enabled": False, "api_key": "", "service": "oi", "country": 78, "dial_code": "+33"},
            "pvapins":        {"enabled": False, "customer_key": "", "service": "oi", "country": 78, "dial_code": "+33"}
        }
    },
    "telegram": {"token": "", "chat_id": ""},
    "system": {
        "adb_path": "",
        "photos_dir": "",
        "creation_proxy": {"host": "", "port": "", "user": "", "pass": ""}
    }
}

def _derive_target_country(sms_cfg: dict) -> str:
    """Retourne le pays cible (pour GPS) du premier provider SMS activé qui en a un."""
    for prov in sms_cfg.get("providers", {}).values():
        if prov.get("enabled") and prov.get("target_country"):
            return prov["target_country"]
    return "france"

def _deep_merge(base, override):
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result

# ── I/O atomique — défini ICI avant tout usage ────────────────────────────────
_file_lock     = _threading.Lock()   # protège les I/O fichiers individuelles
_auth_lock     = _threading.Lock()   # protège _auth_sessions en mémoire
_users_rw_lock = _threading.Lock()  # protège les cycles read-modify-write sur users.json

def _read_json_safe(path):
    """Lecture JSON avec fallback sur .tmp en cas de crash."""
    for fname in [path, path + ".tmp"]:
        if os.path.exists(fname):
            try:
                with open(fname, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                continue
    return None

def _write_json_safe(path, data):
    """Écriture JSON atomique — jamais de fichier à moitié écrit."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def load_app_config():
    data = _read_json_safe(CONFIG_FILE)
    if data:
        return _deep_merge(DEFAULT_CONFIG, data)
    return _deep_merge(DEFAULT_CONFIG, {})

def save_app_config(cfg):
    try:
        with _file_lock:
            _write_json_safe(CONFIG_FILE, cfg)
    except Exception as e:
        sys.__stdout__.write(f"[WARN] save_app_config: {e}\n")

app_config = load_app_config()

import shutil as _shutil
_adb_found = _shutil.which("adb")
if not _adb_found:
    _adb_candidates = [
        "/usr/bin/adb", "/usr/local/bin/adb", "/opt/homebrew/bin/adb",
        r"C:\platform-tools\adb.exe",
        r"C:\Users\%s\AppData\Local\Android\Sdk\platform-tools\adb.exe" % os.environ.get("USERNAME", ""),
        r"C:\Android\platform-tools\adb.exe",
        r"C:\Program Files\Android\platform-tools\adb.exe",
    ]
    for _p in _adb_candidates:
        if os.path.exists(_p):
            _adb_found = _p
            break
if _adb_found:
    if not app_config.get("system", {}).get("adb_path"):
        app_config.setdefault("system", {})["adb_path"] = _adb_found
    sys.__stdout__.write(f"[INFO] ADB auto-détecté : {_adb_found}\n")
    if INSTAGRAM_CODE_LOADED:
        instagram_code.ADB_PATH = _adb_found

def apply_config_to_code():
    if not INSTAGRAM_CODE_LOADED:
        return
    try:
        c = app_config
        gl = c.get("geelark", {})
        if gl.get("app_id"):    instagram_code.GEELARK_APP_ID  = gl["app_id"]
        if gl.get("api_key"):   instagram_code.GEELARK_API_KEY = gl["api_key"]
        if gl.get("bearer"):    instagram_code.GEELARK_BEARER  = gl["bearer"]

        tg = c.get("telegram", {})
        if tg.get("token"):    instagram_code.TELEGRAM_TOKEN   = tg["token"]
        if tg.get("chat_id"):  instagram_code.TELEGRAM_CHAT_ID = tg["chat_id"]

        sys_ = c.get("system", {})
        if sys_.get("adb_path"):   instagram_code.ADB_PATH        = sys_["adb_path"]
        if sys_.get("photos_dir"): instagram_code.PHOTOS_BASE_DIR = sys_["photos_dir"]

        sms = c.get("sms", {})
        if sms.get("country"):  instagram_code.HERO_COUNTRY = sms["country"]
        if sms.get("service"):  instagram_code.HERO_SERVICE = sms["service"]
        prov = sms.get("providers", {})
        hero = prov.get("herosms", {})
        instagram_code.HEROSMS_ENABLED = hero.get("enabled", False)
        if hero.get("api_key"):   instagram_code.HERO_API_KEY   = hero["api_key"]
        if hero.get("service"):   instagram_code.HERO_SERVICE   = hero["service"]
        if hero.get("country"):   instagram_code.HERO_COUNTRY   = str(hero["country"])
        if hero.get("dial_code"): instagram_code.HERO_DIAL_CODE = hero["dial_code"]
        if hero.get("max_price") is not None: instagram_code.HERO_MAX_PRICE = float(hero["max_price"])
        pin = prov.get("smspin", {})
        instagram_code.SMSPIN_ENABLED = pin.get("enabled", False)
        if pin.get("api_key"):   instagram_code.SMSPIN_API_KEY = pin["api_key"]
        if pin.get("dial_code"): instagram_code.SMSPIN_DIAL_CODE = pin["dial_code"]
        bower = prov.get("smsbower", {})
        instagram_code.SMSBOWER_ENABLED = bower.get("enabled", False)
        if bower.get("api_key"):   instagram_code.SMSBOWER_API_KEY   = bower["api_key"]
        if bower.get("service"):   instagram_code.SMSBOWER_SERVICE   = bower["service"]
        if bower.get("country"):   instagram_code.SMSBOWER_COUNTRY   = str(bower["country"])
        if bower.get("dial_code"): instagram_code.SMSBOWER_DIAL_CODE = bower["dial_code"]
        if bower.get("max_price") is not None: instagram_code.SMSBOWER_MAX_PRICE = float(bower["max_price"])
        sagg = prov.get("sim_aggregator", {})
        instagram_code.SIM_AGGREGATOR_ENABLED = sagg.get("enabled", False)
        if sagg.get("api_key"):   instagram_code.SIM_AGGREGATOR_API_KEY   = sagg["api_key"]
        if sagg.get("service"):   instagram_code.SIM_AGGREGATOR_SERVICE   = sagg["service"]
        if sagg.get("country"):   instagram_code.SIM_AGGREGATOR_COUNTRY   = str(sagg["country"])
        if sagg.get("dial_code"): instagram_code.SIM_AGGREGATOR_DIAL_CODE = sagg["dial_code"]
        pvap = prov.get("pvapins", {})
        instagram_code.PVAPINS_ENABLED = pvap.get("enabled", False)
        if pvap.get("customer_key"): instagram_code.PVAPINS_CUSTOMER  = pvap["customer_key"]
        if pvap.get("service"):      instagram_code.PVAPINS_SERVICE   = pvap["service"]
        if pvap.get("country"):      instagram_code.PVAPINS_COUNTRY   = str(pvap["country"])
        if pvap.get("dial_code"):    instagram_code.PVAPINS_DIAL_CODE = pvap["dial_code"]
    except Exception as e:
        sys.__stdout__.write(f"[WARN] apply_config_to_code: {e}\n")

apply_config_to_code()

# ── Gestion photos persistantes ───────────────────────────────────────────────
_PHOTOS_BASE = os.path.join(_DATA_DIR, "photos")
_ZIP_FILENAME = ".last_upload.zip"

def _find_batch_root(path, depth=0):
    """Descend dans un dossier si 1 seul sous-dossier, sinon retourne path."""
    if depth > 3 or not os.path.isdir(path):
        return path
    entries = [e for e in os.listdir(path) if not e.startswith('.') and not e.startswith('__')]
    dirs  = [e for e in entries if os.path.isdir(os.path.join(path, e))]
    files = [e for e in entries if os.path.isfile(os.path.join(path, e))]
    if len(dirs) == 1 and not files:
        return _find_batch_root(os.path.join(path, dirs[0]), depth + 1)
    return path

def _restore_user_photos(username):
    """Re-extrait le ZIP sauvegardé d'un utilisateur. Retourne le batch_root ou None."""
    import shutil as _sh, zipfile as _zf
    user_dir  = os.path.join(_PHOTOS_BASE, username)
    zip_path  = os.path.join(user_dir, _ZIP_FILENAME)
    if not os.path.exists(zip_path):
        return None
    # Supprimer l'ancien contenu extrait (garder le ZIP)
    for entry in os.listdir(user_dir):
        if entry == _ZIP_FILENAME:
            continue
        ep = os.path.join(user_dir, entry)
        try:
            _sh.rmtree(ep) if os.path.isdir(ep) else os.remove(ep)
        except Exception:
            pass
    # Ré-extraire
    with _zf.ZipFile(zip_path, "r") as z:
        z.extractall(user_dir)
    root = _find_batch_root(user_dir)
    sys.__stdout__.write(f"[PHOTOS] {username} → {root}\n")
    return root

def _get_user_batch_dir(username):
    """Retourne le batch_root pour un user (depuis ZIP si dispo, sinon None)."""
    user_dir  = os.path.join(_PHOTOS_BASE, username)
    zip_path  = os.path.join(user_dir, _ZIP_FILENAME)
    if not os.path.exists(zip_path):
        return None
    root = _find_batch_root(user_dir)
    # Vérifier qu'il y a des dossiers extraits
    try:
        has_content = any(
            os.path.isdir(os.path.join(root, d)) and not d.startswith('.')
            for d in os.listdir(root)
        )
    except Exception:
        has_content = False
    if not has_content:
        root = _restore_user_photos(username)
    return root

def _bootstrap_photos():
    """Charge les dossiers photos existants. Re-extrait le ZIP seulement si aucun batch n'existe."""
    if not os.path.isdir(_PHOTOS_BASE):
        return
    for uname in os.listdir(_PHOTOS_BASE):
        udir = os.path.join(_PHOTOS_BASE, uname)
        if not os.path.isdir(udir):
            continue
        zip_path = os.path.join(udir, _ZIP_FILENAME)
        if not os.path.exists(zip_path):
            continue
        try:
            # Compter les dossiers batch existants (hors ZIP et fichiers cachés)
            existing_batches = [
                e for e in os.listdir(udir)
                if not e.startswith('.') and not e.startswith('__')
                and os.path.isdir(os.path.join(udir, e))
            ]
            if existing_batches:
                # Des dossiers non-utilisés existent — on les garde tels quels
                root = _find_batch_root(udir)
                sys.__stdout__.write(f"[PHOTOS] {uname} → {len(existing_batches)} batch(es) existant(s), pas de re-extraction\n")
            else:
                # Plus aucun batch disponible — re-extraire le ZIP
                sys.__stdout__.write(f"[PHOTOS] {uname} → aucun batch, re-extraction ZIP...\n")
                root = _restore_user_photos(uname)
            if root and INSTAGRAM_CODE_LOADED:
                instagram_code.PHOTOS_BASE_DIR = root
                instagram_code.load_photo_folders()
        except Exception as _e:
            sys.__stdout__.write(f"[WARN] bootstrap_photos {uname}: {_e}\n")

_bootstrap_photos()

# ── Media library : posts / reels / quick files persistés sur disque ──────────
import mimetypes as _mimetypes
from werkzeug.utils import secure_filename as _wsecure

_MEDIA_DIR = os.path.join(_DATA_DIR, "media")

def _mlib_folder_base(username, mtype):
    d = os.path.join(_MEDIA_DIR, mtype, username)
    os.makedirs(d, exist_ok=True)
    return d

def _mlib_quick_base(username, mtype):
    d = os.path.join(_MEDIA_DIR, "quick", mtype, username)
    os.makedirs(d, exist_ok=True)
    return d

def _mlib_profile_base(username):
    d = os.path.join(_MEDIA_DIR, "profile", username)
    os.makedirs(d, exist_ok=True)
    return d

def _mlib_read_meta(base_dir):
    p = os.path.join(base_dir, "_meta.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _mlib_write_meta(base_dir, data):
    p = os.path.join(base_dir, "_meta.json")
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, p)
    except Exception as e:
        sys.__stdout__.write(f"[WARN] mlib_write_meta: {e}\n")

def _mlib_delete_folder(base_dir, folder_id):
    import shutil as _sh
    fdir = os.path.join(base_dir, folder_id)
    if os.path.abspath(fdir).startswith(os.path.abspath(base_dir)) and os.path.isdir(fdir):
        _sh.rmtree(fdir, ignore_errors=True)
    metas = _mlib_read_meta(base_dir)
    _mlib_write_meta(base_dir, [m for m in metas if m.get("id") != folder_id])

# ── User / Auth system ────────────────────────────────────────────────────────
# ── User / Auth system ────────────────────────────────────────────────────────
USERS_FILE    = os.path.join(_DATA_DIR, "users.json")
_SESSIONS_FILE = os.path.join(_DATA_DIR, "auth_sessions.json")
_auth_sessions = {}

def _load_users():
    """Lecture thread-safe de users.json avec fallback sur backup."""
    with _file_lock:
        data = _read_json_safe(USERS_FILE)
        if not data:
            # Essayer le backup si le fichier principal est absent/corrompu
            bak = USERS_FILE + ".bak"
            data = _read_json_safe(bak)
            if data:
                sys.__stdout__.write("[WARN] users.json absent/corrompu — restauré depuis users.json.bak\n")
    if data:
        return data
    # Fichier absent → créer le compte admin par défaut
    default_users = {"users": {"ink06": {
        "password_hash": hashlib.sha256(b"ink06").hexdigest(),
        "role": "admin", "credits": 9999, "config": {},
        "created_at": datetime.now().isoformat()
    }}}
    _save_users(default_users)
    return default_users

def _save_users(data):
    """Écriture thread-safe de users.json avec backup automatique."""
    try:
        with _file_lock:
            # Conserver le fichier précédent comme backup avant d'écraser
            if os.path.exists(USERS_FILE):
                _shutil.copy2(USERS_FILE, USERS_FILE + ".bak")
            _write_json_safe(USERS_FILE, data)
    except Exception as e:
        sys.__stdout__.write(f"[WARN] save_users: {e}\n")

def _load_auth_sessions():
    """Recharge les sessions depuis le disque. N'écrase PAS si le fichier est illisible."""
    global _auth_sessions
    with _file_lock:
        data = _read_json_safe(_SESSIONS_FILE)
    if data is not None:
        now = time.time()
        with _auth_lock:
            _auth_sessions = {k: v for k, v in data.items() if v.get("expires", 0) > now}

def _save_auth_sessions():
    try:
        with _auth_lock:
            sessions_copy = dict(_auth_sessions)
        with _file_lock:
            _write_json_safe(_SESSIONS_FILE, sessions_copy)
    except Exception as e:
        sys.__stdout__.write(f"[WARN] save_auth_sessions: {e}\n")

# Charger les sessions au démarrage
_load_auth_sessions()

def _get_user_from_request():
    token = (request.headers.get("X-Auth-Token")
             or request.args.get("_token")
             or request.cookies.get("auth_token"))
    if not token:
        return None, None
    # Vérifier d'abord en mémoire (rapide, pas de race)
    with _auth_lock:
        sess = _auth_sessions.get(token)
    if not sess:
        # Recharger depuis disque uniquement si absent en mémoire (gère redémarrage serveur)
        _load_auth_sessions()
        with _auth_lock:
            sess = _auth_sessions.get(token)
    if not sess or time.time() > sess.get("expires", 0):
        return None, None
    users_data = _load_users()
    user = users_data["users"].get(sess["username"])
    if not user:
        return None, None
    return user, sess["username"]

def _apply_user_config(username):
    users_data = _load_users()
    user = users_data["users"].get(username, {})
    merged = _deep_merge(app_config, user.get("config", {}))
    if not INSTAGRAM_CODE_LOADED:
        return
    try:
        gl = merged.get("geelark", {})
        if gl.get("app_id"):    instagram_code.GEELARK_APP_ID  = gl["app_id"]
        if gl.get("api_key"):   instagram_code.GEELARK_API_KEY = gl["api_key"]
        if gl.get("bearer"):    instagram_code.GEELARK_BEARER  = gl["bearer"]
        tg = merged.get("telegram", {})
        if tg.get("token"):    instagram_code.TELEGRAM_TOKEN   = tg["token"]
        if tg.get("chat_id"):  instagram_code.TELEGRAM_CHAT_ID = tg["chat_id"]
        sys_ = merged.get("system", {})
        if sys_.get("adb_path"):   instagram_code.ADB_PATH = sys_["adb_path"]
        # Priorité : ZIP sauvegardé → sinon photos_dir stocké
        _batch = _get_user_batch_dir(username)
        if _batch:
            instagram_code.PHOTOS_BASE_DIR = _batch
        elif sys_.get("photos_dir"):
            instagram_code.PHOTOS_BASE_DIR = sys_["photos_dir"]
        sms = merged.get("sms", {})
        if sms.get("country"):  instagram_code.HERO_COUNTRY = sms["country"]
        if sms.get("service"):  instagram_code.HERO_SERVICE = sms["service"]
        prov = sms.get("providers", {})
        hero = prov.get("herosms", {})
        instagram_code.HEROSMS_ENABLED = hero.get("enabled", False)
        if hero.get("api_key"):   instagram_code.HERO_API_KEY   = hero["api_key"]
        if hero.get("service"):   instagram_code.HERO_SERVICE   = hero["service"]
        if hero.get("country"):   instagram_code.HERO_COUNTRY   = str(hero["country"])
        if hero.get("dial_code"): instagram_code.HERO_DIAL_CODE = hero["dial_code"]
        if hero.get("max_price") is not None: instagram_code.HERO_MAX_PRICE = float(hero["max_price"])
        pin = prov.get("smspin", {})
        instagram_code.SMSPIN_ENABLED = pin.get("enabled", False)
        if pin.get("api_key"):   instagram_code.SMSPIN_API_KEY  = pin["api_key"]
        if pin.get("dial_code"): instagram_code.SMSPIN_DIAL_CODE = pin["dial_code"]
        bower = prov.get("smsbower", {})
        instagram_code.SMSBOWER_ENABLED = bower.get("enabled", False)
        if bower.get("api_key"):   instagram_code.SMSBOWER_API_KEY   = bower["api_key"]
        if bower.get("service"):   instagram_code.SMSBOWER_SERVICE   = bower["service"]
        if bower.get("country"):   instagram_code.SMSBOWER_COUNTRY   = str(bower["country"])
        if bower.get("dial_code"): instagram_code.SMSBOWER_DIAL_CODE = bower["dial_code"]
        if bower.get("max_price") is not None: instagram_code.SMSBOWER_MAX_PRICE = float(bower["max_price"])
        sagg = prov.get("sim_aggregator", {})
        instagram_code.SIM_AGGREGATOR_ENABLED = sagg.get("enabled", False)
        if sagg.get("api_key"):   instagram_code.SIM_AGGREGATOR_API_KEY   = sagg["api_key"]
        if sagg.get("service"):   instagram_code.SIM_AGGREGATOR_SERVICE   = sagg["service"]
        if sagg.get("country"):   instagram_code.SIM_AGGREGATOR_COUNTRY   = str(sagg["country"])
        if sagg.get("dial_code"): instagram_code.SIM_AGGREGATOR_DIAL_CODE = sagg["dial_code"]
        pvap = prov.get("pvapins", {})
        instagram_code.PVAPINS_ENABLED = pvap.get("enabled", False)
        if pvap.get("customer_key"): instagram_code.PVAPINS_CUSTOMER  = pvap["customer_key"]
        if pvap.get("service"):      instagram_code.PVAPINS_SERVICE   = pvap["service"]
        if pvap.get("country"):      instagram_code.PVAPINS_COUNTRY   = str(pvap["country"])
        if pvap.get("dial_code"):    instagram_code.PVAPINS_DIAL_CODE = pvap["dial_code"]
    except Exception as e:
        sys.__stdout__.write(f"[WARN] _apply_user_config: {e}\n")

@app.route("/api/auth/register", methods=["POST"])
def auth_register():
    data = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username or len(username) < 3:
        return jsonify({"error": "Nom trop court (min 3 caractères)"}), 400
    if not password or len(password) < 6:
        return jsonify({"error": "Mot de passe trop court (min 6 caractères)"}), 400
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    with _users_rw_lock:
        users_data = _load_users()
        if username in users_data["users"]:
            return jsonify({"error": "Nom d'utilisateur déjà pris"}), 400
        users_data["users"][username] = {
            "password_hash": pw_hash, "role": "user", "credits": 0,
            "config": {
                "geelark": {"app_id": "", "api_key": "", "bearer": ""},
                "sms": {
                    "providers": {
                        "herosms":        {"api_key": ""},
                        "smspin":         {"api_key": ""},
                        "smsbower":       {"api_key": ""},
                        "sim_aggregator": {"api_key": ""},
                        "pvapins":        {"customer_key": ""}
                    }
                },
                "telegram": {"token": "", "chat_id": ""},
                "system": {"photos_dir": ""}
            },
            "created_at": datetime.now().isoformat()
        }
        _save_users(users_data)
        
        # Notification Telegram — nouvelle inscription
    try:
        msg = (
            f"🆕 <b>Nouvelle inscription</b>\n"
            f"👤 Username : <code>{username}</code>\n"
            f"🕐 Date : {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        )
        req.post(
            f"https://api.telegram.org/bot8512389683:AAGjagjTheVhiaYrj6by6ZLnoPUNcFRSINU/sendMessage",
            json={"chat_id": "5899192308", "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception:
        pass
    
    token = str(uuid.uuid4())
    with _auth_lock:
        _auth_sessions[token] = {"username": username, "expires": time.time() + 86400 * 90}
    _save_auth_sessions()

    return jsonify({"success": True, "token": token,
                    "user": {"username": username, "role": "user", "credits": 0}})

@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    users_data = _load_users()
    user = users_data["users"].get(username)
    if not user:
        return jsonify({"error": "Utilisateur introuvable"}), 401
    pw_hash = hashlib.sha256(password.encode()).hexdigest()
    if user.get("password_hash") != pw_hash:
        return jsonify({"error": "Mot de passe incorrect"}), 401
    token = str(uuid.uuid4())
    with _auth_lock:
        _auth_sessions[token] = {"username": username, "expires": time.time() + 86400 * 90}
    _save_auth_sessions()

    _apply_user_config(username)
    return jsonify({"success": True, "token": token, "user": {
        "username": username, "role": user.get("role", "user"),
        "credits": user.get("credits", 0)
    }})

@app.route("/api/auth/me", methods=["GET"])
def auth_me():
    user, username = _get_user_from_request()
    if not user:
        return jsonify({"error": "Non authentifié"}), 401

    token = (request.headers.get("X-Auth-Token")
             or request.args.get("_token")
             or request.cookies.get("auth_token"))
    # Auto-renouvellement
    with _auth_lock:
        if token and token in _auth_sessions:
            _auth_sessions[token]["expires"] = time.time() + 86400 * 90
    _save_auth_sessions()

    return jsonify({"username": username, "role": user.get("role", "user"),
                    "credits": user.get("credits", 0)})

@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    token = request.headers.get("X-Auth-Token")
    with _auth_lock:
        _auth_sessions.pop(token, None)
    _save_auth_sessions()
    return jsonify({"success": True})

@app.route("/api/admin/users", methods=["GET"])
def admin_list_users():
    user, username = _get_user_from_request()
    if not user:
        token = (request.headers.get("X-Auth-Token") or request.args.get("_token") or "")
        with _auth_lock:
            token_in_mem = token[:8] + "..." if token else "(aucun)"
            sess_count = len(_auth_sessions)
        return jsonify({"error": "Accès refusé", "raison": "token_invalide", "token_recu": token_in_mem, "sessions_en_memoire": sess_count}), 403
    if user.get("role") != "admin":
        return jsonify({"error": "Accès refusé", "raison": "role_insuffisant", "role_actuel": user.get("role"), "username": username}), 403
    users_data = _load_users()
    return jsonify({"users": [
        {"username": u, "role": d.get("role", "user"),
         "credits": d.get("credits", 0), "created_at": d.get("created_at", "")}
        for u, d in users_data["users"].items()
    ]})

@app.route("/api/admin/credits", methods=["POST"])
def admin_set_credits():
    user, _ = _get_user_from_request()
    if not user or user.get("role") != "admin":
        return jsonify({"error": "Accès refusé"}), 403
    data = request.json or {}
    target = data.get("username")
    amount = int(data.get("amount", 0))
    action = data.get("action", "add")
    with _users_rw_lock:
        users_data = _load_users()
        if target not in users_data["users"]:
            return jsonify({"error": "Utilisateur introuvable"}), 404
        cur = users_data["users"][target].get("credits", 0)
        users_data["users"][target]["credits"] = amount if action == "set" else max(0, cur - amount) if action == "remove" else cur + amount
        _save_users(users_data)
    return jsonify({"success": True, "credits": users_data["users"][target]["credits"]})

@app.route("/api/admin/users/delete", methods=["POST"])
def admin_delete_user():
    user, _ = _get_user_from_request()
    if not user or user.get("role") != "admin":
        return jsonify({"error": "Accès refusé"}), 403
    data = request.json or {}
    target = data.get("username")
    if target == "admin":
        return jsonify({"error": "Impossible de supprimer l'admin"}), 400
    with _users_rw_lock:
        users_data = _load_users()
        if target not in users_data["users"]:
            return jsonify({"error": "Utilisateur introuvable"}), 404
        del users_data["users"][target]
        _save_users(users_data)
    return jsonify({"success": True})

# ── Posting limits (modifiables via /api/panel_settings) ─────────────────────
POSTING_COOLDOWN_HOURS  = 10
MAX_POSTS_PER_DAY       = 2
MAX_REELS_PER_DAY       = 2
MAX_STORIES_PER_DAY     = 2
WARMUP_USERNAMES        = ""
PHONE_STAGGER_SEC       = 8
MIN_ACCOUNT_AGE_HOURS   = 24    # délai min après création avant de poster
REQUIRE_WARMUP          = True  # bloquer le post si pas de warmup effectué

_phone_start_lock    = _threading.Lock()
_last_phone_start_ts = [0.0]

def _acquire_phone_start_slot():
    with _phone_start_lock:
        now = time.time()
        elapsed = now - _last_phone_start_ts[0]
        if _last_phone_start_ts[0] > 0 and elapsed < PHONE_STAGGER_SEC:
            time.sleep(PHONE_STAGGER_SEC - elapsed)
        _last_phone_start_ts[0] = time.time()

POSTING_HISTORY_FILE = os.path.join(_DATA_DIR, "posting_history.json")
PANEL_SETTINGS_FILE  = os.path.join(_DATA_DIR, "panel_settings.json")

def _load_panel_settings():
    global POSTING_COOLDOWN_HOURS, MAX_POSTS_PER_DAY, MAX_REELS_PER_DAY, MAX_STORIES_PER_DAY
    global WARMUP_USERNAMES, PHONE_STAGGER_SEC, MIN_ACCOUNT_AGE_HOURS, REQUIRE_WARMUP
    if not os.path.exists(PANEL_SETTINGS_FILE):
        return
    try:
        with open(PANEL_SETTINGS_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        POSTING_COOLDOWN_HOURS = s.get("POSTING_COOLDOWN_HOURS", POSTING_COOLDOWN_HOURS)
        MAX_POSTS_PER_DAY      = s.get("MAX_POSTS_PER_DAY",   MAX_POSTS_PER_DAY)
        MAX_REELS_PER_DAY      = s.get("MAX_REELS_PER_DAY",   MAX_REELS_PER_DAY)
        MAX_STORIES_PER_DAY    = s.get("MAX_STORIES_PER_DAY", MAX_STORIES_PER_DAY)
        WARMUP_USERNAMES       = s.get("WARMUP_USERNAMES",    WARMUP_USERNAMES)
        PHONE_STAGGER_SEC      = int(s.get("PHONE_STAGGER_SEC", PHONE_STAGGER_SEC))
        MIN_ACCOUNT_AGE_HOURS  = int(s.get("MIN_ACCOUNT_AGE_HOURS", MIN_ACCOUNT_AGE_HOURS))
        REQUIRE_WARMUP         = bool(s.get("REQUIRE_WARMUP", REQUIRE_WARMUP))
        if INSTAGRAM_CODE_LOADED:
            if "MENTION_TAGS" in s:
                _tags = [t.strip() for t in s["MENTION_TAGS"].split("\n") if t.strip()]
                if _tags:
                    instagram_code.MENTION_TAGS = _tags
                    instagram_code.MENTION_TAG  = _tags[0]
            elif "MENTION_TAG" in s:
                instagram_code.MENTION_TAG  = s["MENTION_TAG"]
                instagram_code.MENTION_TAGS = [s["MENTION_TAG"]]
            if "FIRST_NAMES" in s:
                _names = [n.strip() for n in s["FIRST_NAMES"].split("\n") if n.strip()]
                if _names:
                    instagram_code.FIRST_NAMES = _names
                    instagram_code.FIRST_NAME  = _names[0]
            # Propagate MIN_ACCOUNT_AGE_HOURS to code.py
            instagram_code.MIN_ACCOUNT_AGE_HOURS = MIN_ACCOUNT_AGE_HOURS
            # Propagate CREATION_MODE to code.py
            if "CREATION_MODE" in s:
                instagram_code.CREATION_MODE = s["CREATION_MODE"]
            # Propagate ANDROID_VERSION to code.py
            if "ANDROID_VERSION" in s:
                instagram_code.ANDROID_VERSION = s["ANDROID_VERSION"]
    except Exception as e:
        sys.__stdout__.write(f"[WARN] panel_settings load error: {e}\n")

def _save_panel_settings():
    try:
        _mention_tag    = instagram_code.MENTION_TAG     if INSTAGRAM_CODE_LOADED else "@miaivvyy"
        _mention_tags   = instagram_code.MENTION_TAGS    if INSTAGRAM_CODE_LOADED else ["@miaivvyy"]
        _first_names    = instagram_code.FIRST_NAMES     if INSTAGRAM_CODE_LOADED else ["Miahyvina"]
        _creation_mode   = instagram_code.CREATION_MODE    if INSTAGRAM_CODE_LOADED else "phone"
        _android_version = instagram_code.ANDROID_VERSION  if INSTAGRAM_CODE_LOADED else "Android 14"
        s = {
            "POSTING_COOLDOWN_HOURS": POSTING_COOLDOWN_HOURS,
            "MAX_POSTS_PER_DAY":      MAX_POSTS_PER_DAY,
            "MAX_REELS_PER_DAY":      MAX_REELS_PER_DAY,
            "MAX_STORIES_PER_DAY":    MAX_STORIES_PER_DAY,
            "WARMUP_USERNAMES":       WARMUP_USERNAMES,
            "PHONE_STAGGER_SEC":      PHONE_STAGGER_SEC,
            "MIN_ACCOUNT_AGE_HOURS":  MIN_ACCOUNT_AGE_HOURS,
            "REQUIRE_WARMUP":         REQUIRE_WARMUP,
            "MENTION_TAG":     _mention_tag,
            "MENTION_TAGS":    "\n".join(_mention_tags),
            "FIRST_NAMES":     "\n".join(_first_names),
            "CREATION_MODE":   _creation_mode,
            "ANDROID_VERSION": _android_version,
        }
        with open(PANEL_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception as e:
        sys.__stdout__.write(f"[WARN] panel_settings save error: {e}\n")

_posting_history_lock = _threading.Lock()

def _load_posting_history():
    if not os.path.exists(POSTING_HISTORY_FILE):
        return {}
    try:
        with open(POSTING_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_posting_history(history):
    try:
        with open(POSTING_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        sys.__stdout__.write(f"[WARN] posting_history save error: {e}\n")

def record_post_action(phone_id, action_type):
    with _posting_history_lock:
        history = _load_posting_history()
        pid = str(phone_id)
        if pid not in history:
            history[pid] = []
        history[pid].append({
            "action": action_type,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        _save_posting_history(history)

def record_warmup(phone_id):
    record_post_action(phone_id, "warmup")

def has_warmup(phone_id):
    with _posting_history_lock:
        history = _load_posting_history()
    entries = history.get(str(phone_id), [])
    return any(e["action"] == "warmup" for e in entries)

def check_posting_limit(phone_id, action_type):
    limit_map = {"post": MAX_POSTS_PER_DAY, "reel": MAX_REELS_PER_DAY, "story": MAX_STORIES_PER_DAY}
    max_allowed = limit_map.get(action_type, MAX_POSTS_PER_DAY)
    with _posting_history_lock:
        history = _load_posting_history()
    pid = str(phone_id)
    entries = history.get(pid, [])
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff  = now_ts - POSTING_COOLDOWN_HOURS * 3600
    recent_same = [e for e in entries if e["action"] == action_type and _ts(e["timestamp"]) >= cutoff]
    if recent_same:
        last_ts = max(_ts(e["timestamp"]) for e in recent_same)
        elapsed_h = (now_ts - last_ts) / 3600
        if elapsed_h < POSTING_COOLDOWN_HOURS:
            remaining_h = POSTING_COOLDOWN_HOURS - elapsed_h
            return False, (f"⏱️ [{phone_id}] Intervalle {action_type} non respecté : "
                           f"dernier il y a {elapsed_h:.1f}h — encore {remaining_h:.1f}h à attendre")
    if len(recent_same) >= max_allowed:
        return False, (f"⏱️ [{phone_id}] Limite {action_type} atteinte : {len(recent_same)}/{max_allowed} "
                       f"dans les {POSTING_COOLDOWN_HOURS}h")
    return True, None

def _ts(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0

debug_input_queue = queue.Queue()

_load_panel_settings()

# ── Swipe state ───────────────────────────────────────────────────────────────
swipe_state = {
    "running": False,
    "done": 0, "total": 0, "liked": 0, "noped": 0,
    "current_phone": None, "current_step": None,
}
swipe_stop_flag = [False]
# ── Téléphones actuellement en cours de création ──────────────────────────────
_phones_in_creation = set()
_phones_in_creation_lock = _threading.Lock()
_geelark_create_lock = _threading.Lock()

def _get_phones_in_creation():
    with _phones_in_creation_lock:
        return set(_phones_in_creation)
    

def run_session_multi(config, sess_state, session_stop_flag):
    """Wrapper de run_session pour le mode multi-sessions."""
    global active_sessions, session_state

    _thread_local.log_target = "creation"
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = LogCapture("info", target="creation")
    sys.stderr = LogCapture("error", target="creation")

    sid = sess_state["id"]
    _thread_local.sess_id = sid
    push_log("info", f"▶ Session #{sid} démarrée")

    try:
        # Réutilise run_session en lui passant le stop_flag de cette session
        # On patche temporairement stop_requested pour cette session
        run_session_with_flag(config, sess_state, session_stop_flag)
    finally:
        sess_state["running"] = False
        with _sessions_lock:
            try:
                active_sessions.remove(sess_state)
            except ValueError:
                pass
        # Mettre à jour le state global
        session_state["running"] = any(s["running"] for s in active_sessions)
        # Nettoyer les URLs GeeLark de cette session
        with _geelark_urls_lock:
            to_del = [pid for pid, v in _active_geelark_urls.items() if v.get("sess_id") == sid]
            for pid in to_del:
                del _active_geelark_urls[pid]
        push_log("info", f"🔒 Session #{sid} terminée (restantes: {len(active_sessions)})")
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        _thread_local.log_target = None



def run_session_with_flag(config, sess_state, stop_flag):
    """Version de run_session utilisant un stop_flag local."""
    if not INSTAGRAM_CODE_LOADED:
        push_log("error", "❌ code.py non disponible")
        return

    # Appliquer la config GeeLark/SMS depuis le payload de la session
    geelark_cfg = config.get("geelark_config", {})
    if geelark_cfg.get("app_id"):  instagram_code.GEELARK_APP_ID  = geelark_cfg["app_id"]
    if geelark_cfg.get("api_key"): instagram_code.GEELARK_API_KEY = geelark_cfg["api_key"]
    if geelark_cfg.get("bearer"):  instagram_code.GEELARK_BEARER  = geelark_cfg["bearer"]
    
    sms_cfg = config.get("sms_config", {})
    if sms_cfg.get("country"): instagram_code.HERO_COUNTRY = sms_cfg["country"]
    if sms_cfg.get("service"): instagram_code.HERO_SERVICE = sms_cfg["service"]
    prov_cfg = sms_cfg.get("providers", {})
    hero_cfg = prov_cfg.get("herosms", {})
    if hero_cfg.get("enabled") is not None: instagram_code.HEROSMS_ENABLED = hero_cfg["enabled"]
    if hero_cfg.get("api_key"):  instagram_code.HERO_API_KEY  = hero_cfg["api_key"]
    if hero_cfg.get("service"):  instagram_code.HERO_SERVICE  = hero_cfg["service"]
    if hero_cfg.get("country"):  instagram_code.HERO_COUNTRY  = str(hero_cfg["country"])
    bower_cfg = prov_cfg.get("smsbower", {})
    if bower_cfg.get("enabled") is not None: instagram_code.SMSBOWER_ENABLED = bower_cfg["enabled"]
    if bower_cfg.get("api_key"):  instagram_code.SMSBOWER_API_KEY  = bower_cfg["api_key"]
    if bower_cfg.get("service"):  instagram_code.SMSBOWER_SERVICE  = bower_cfg["service"]
    if bower_cfg.get("country"):  instagram_code.SMSBOWER_COUNTRY  = str(bower_cfg["country"])
    smspin_cfg = prov_cfg.get("smspin", {})
    if smspin_cfg.get("enabled") is not None: instagram_code.SMSPIN_ENABLED = smspin_cfg["enabled"]
    if smspin_cfg.get("api_key"): instagram_code.SMSPIN_API_KEY = smspin_cfg["api_key"]
    sagg_cfg = prov_cfg.get("sim_aggregator", {})
    if sagg_cfg.get("enabled") is not None: instagram_code.SIM_AGGREGATOR_ENABLED = sagg_cfg["enabled"]
    if sagg_cfg.get("api_key"):   instagram_code.SIM_AGGREGATOR_API_KEY  = sagg_cfg["api_key"]
    if sagg_cfg.get("service"):   instagram_code.SIM_AGGREGATOR_SERVICE  = sagg_cfg["service"]
    if sagg_cfg.get("country"):   instagram_code.SIM_AGGREGATOR_COUNTRY  = str(sagg_cfg["country"])
    pvap_cfg = prov_cfg.get("pvapins", {})
    if pvap_cfg.get("enabled") is not None: instagram_code.PVAPINS_ENABLED = pvap_cfg["enabled"]
    if pvap_cfg.get("customer_key"): instagram_code.PVAPINS_CUSTOMER = pvap_cfg["customer_key"]
    if pvap_cfg.get("service"):      instagram_code.PVAPINS_SERVICE  = pvap_cfg["service"]
    if pvap_cfg.get("country"):      instagram_code.PVAPINS_COUNTRY  = str(pvap_cfg["country"])

    instagram_code.EMAIL        = config["email"] or instagram_code.EMAIL
    instagram_code.FIRST_NAME   = config["firstname"]
    instagram_code.BIRTH_YEAR   = config["birth_year"]
    instagram_code.HERO_COUNTRY = config.get("hero_country") or instagram_code.HERO_COUNTRY
    instagram_code.DEBUG_MODE   = (config["mode"] == "debug")
    instagram_code.set_debug_queue(debug_input_queue)

    # NE PAS toucher sys.stdout ici — run_session_multi s'en charge déjà

    try:
        instagram_code.load_photo_folders()
    except:
        pass

    # ── Démarrer le pool scraper si pas encore actif ──────────────────
    try:
        if not instagram_code._number_pool_running:
            instagram_code.start_pool_scraper(target_size=5)
            push_log("info", "🚀 Pool scraper démarré (target=5 numéros)")
        else:
            push_log("info", "ℹ️ Pool scraper déjà actif")
    except Exception as _e:
        push_log("warn", f"⚠️ Erreur démarrage pool scraper : {_e}")

    count           = config["count"]
    delay           = config["delay"]
    rotating_host   = config.get("rotating_proxy_host", "").strip()
    rotating_url    = config.get("rotating_url", "").strip()
    rotate_wait     = config.get("rotate_wait", True)
    rotate_wait_sec = config.get("rotate_wait_sec", 3)
    bio             = config.get("bio", "")
    _session_config_store["ban_on_existing_email"] = config.get("ban_on_existing_email", False)
    simultane_cfg   = config.get("simultane")

    def stopped():
        return stop_flag[0]

    sid = sess_state["id"]

    try:
        if simultane_cfg and simultane_cfg.get("enabled"):
            slots      = simultane_cfg.get("slots", [])
            nb_threads = simultane_cfg.get("count", 1)
            boucles    = simultane_cfg.get("boucles", 1)
            total      = nb_threads * boucles

            sess_state["total"] = total
            push_log("info", f"🔀 [#{sid}] Simultané : {nb_threads} threads × {boucles} boucles = {total} comptes")

            done_lock = threading.Lock()

            def run_one(slot, account_index, _retry_count=0):
                if stopped(): return

                _thread_local.log_target = "creation"
                proxy_host    = slot.get("host", "")
                proxy_port    = slot.get("port", "")
                proxy_user    = slot.get("user", "")
                proxy_pass    = slot.get("pass", "")
                rotate_url_sl = slot.get("rotateUrl", "") if slot.get("type") == "rotatif" else ""

                sess_state["current_account"] = f"[#{sid}] Compte {account_index+1}/{total}"
                push_log("info", f"{'─'*40}")
                push_log("info", f"📋 [#{sid}][{account_index+1}/{total}] Thread démarré — proxy {proxy_host}:{proxy_port}")

                phone_id = None
                try:
                    _cm = getattr(instagram_code, 'CREATION_MODE', 'phone')
                    pre_number_result = None
                    pre_email_result  = None  # (email, mailId)

                    if _cm == 'email':
                        push_log("info", f"📧 [#{sid}][{account_index+1}] Récupération Gmail (SMSBower)...")
                        _em_attempt = 0
                        while not stopped():
                            _em_attempt += 1
                            _mail, _mail_id = instagram_code.get_smsbower_email()
                            if _mail:
                                pre_email_result = (_mail, _mail_id)
                                push_log("success", f"✅ [{account_index+1}] Gmail obtenu : {_mail}")
                                break
                            push_log("info", f"⏳ Pas d'email ({_em_attempt}) — retry 10s...")
                            time.sleep(10)
                        if not pre_email_result:
                            with done_lock:
                                sess_state["errors"] += 1
                                sess_state["done"] += 1
                            return
                    else:
                        push_log("info", f"📱 [#{sid}][{account_index+1}] Récupération numéro...")
                        attempt = 0
                        while not stopped():
                            attempt += 1
                            _pool_result = instagram_code.pool_get_number()
                            if _pool_result:
                                _aid, _num, _prov = _pool_result
                                pre_number_result = (_aid, _num, _prov)
                                push_log("success", f"✅ [{account_index+1}] Numéro depuis le POOL : {_num} ({_prov})")
                                break
                            _r = instagram_code.get_hero_number()
                            if _r:
                                _aid, _num, _prov = _r
                                _fmt = instagram_code.format_number(_num)
                                if _fmt:
                                    pre_number_result = (_aid, _fmt, _prov)
                                    push_log("success", f"✅ [{account_index+1}] Numéro API : {_fmt} ({_prov})")
                                    break
                                else:
                                    push_log("warn", f"⚠️ Invalide ({_num}) — relance ({attempt})")
                            else:
                                push_log("info", f"⏳ Aucun numéro ({attempt}) — retry 5s...")
                                time.sleep(5)
                        if not pre_number_result:
                            with done_lock:
                                sess_state["errors"] += 1
                                sess_state["done"] += 1
                            return

                    with _geelark_create_lock:
                        push_log("info", f"📱 [{account_index+1}] Création profil GeeLark (verrou actif)...")
                        for _create_attempt in range(5):
                            phone_id = instagram_code.create_phone_profile(
                                "74z4xquqcn.cn.fxdx.in", "14864", "9haz01", "v51b8i"
                            )
                            if phone_id:
                                push_log("success", f"✅ [{account_index+1}] Profil créé : {phone_id}")
                                time.sleep(5)
                                break
                            push_log("warn", f"⚠️ [{account_index+1}] Tentative {_create_attempt+1}/5 échouée — attente 15s...")
                            time.sleep(15)
                    if not phone_id:
                        push_log("error", f"❌ [{account_index+1}] Échec création profil après 5 tentatives")
                        with done_lock:
                            session_state["errors"] += 1
                            session_state["done"] += 1
                        return

                    _register_phone_creation(phone_id)
                    time.sleep(2)

                    if not instagram_code.start_phone(phone_id):
                        push_log("error", f"❌ [#{sid}][{account_index+1}] Impossible de démarrer")
                        with done_lock:
                            sess_state["errors"] += 1
                            sess_state["done"] += 1
                        return

                    time.sleep(15)

                    if rotate_url_sl:
                        push_log("info", f"🔁 [#{sid}][{account_index+1}] Rotation IP...")
                        try:
                            r = req.get(rotate_url_sl, timeout=15)
                            push_log("success", f"✅ Rotation OK : {r.text.strip()[:60]}")
                        except Exception as e:
                            push_log("warn", f"⚠️ Rotation échouée : {e}")
                        time.sleep(int(config.get("rotate_wait_sec", 3)))

                    if stopped(): return

                    instagram_code.enable_adb(phone_id)
                    time.sleep(3)
                    device, pwd = instagram_code.wait_for_adb(phone_id, max_wait=90)
                    if not device:
                        push_log("error", f"❌ [#{sid}][{account_index+1}] ADB timeout")
                        instagram_code.stop_phone(phone_id)
                        with done_lock:
                            sess_state["errors"] += 1
                            sess_state["done"] += 1
                        return

                    if stopped(): return

                    connected = False
                    for attempt in range(10):
                        if stopped(): break
                        subprocess.run(f'"{instagram_code.ADB_PATH}" connect {device}', shell=True, capture_output=True)
                        time.sleep(3)
                        result = subprocess.run(
                            f'"{instagram_code.ADB_PATH}" -s {device} shell glogin {pwd}',
                            shell=True, capture_output=True, text=True
                        )
                        push_log("info", f"  glogin [{attempt+1}] → {result.stdout.strip()}")
                        if "success" in result.stdout.lower():
                            connected = True
                            break

                    if not connected:
                        push_log("error", f"❌ [#{sid}][{account_index+1}] glogin échoué")
                        instagram_code.stop_phone(phone_id)
                        with done_lock:
                            sess_state["errors"] += 1
                            sess_state["done"] += 1
                        return

                    if stopped(): return

                    instagram_code.enable_data_saver(device)
                    city, lat, lon = instagram_code.apply_random_city_gps(phone_id)
                    sess_state["current_city"] = city
                    time.sleep(2)

                    photo_folder = instagram_code.get_next_photo_folder()
                    if photo_folder:
                        instagram_code.push_photos_to_device(device, photo_folder, base_photos_dir=instagram_code.PHOTOS_BASE_DIR)

                    push_log("info", f"🔄 [#{sid}][{account_index+1}] Application proxy {proxy_host}:{proxy_port}...")
                    proxy_ok = instagram_code.change_phone_proxy(
                        phone_id, proxy_host, proxy_port, proxy_user, proxy_pass, "socks5"
                    )
                    if proxy_ok:
                        push_log("success", f"✅ [#{sid}][{account_index+1}] Proxy appliqué")
                    else:
                        push_log("warn", f"⚠️ [#{sid}][{account_index+1}] Proxy non appliqué — on continue")
                    time.sleep(5)

                    if _cm == 'email' and pre_email_result:
                        instagram_code._pre_fetched_email   = pre_email_result[0]
                        instagram_code._pre_fetched_mail_id = pre_email_result[1]
                        push_log("info", f"📧 [#{sid}][{account_index+1}] Gmail injecté : {pre_email_result[0]}")
                    else:
                        instagram_code._pre_fetched_number = pre_number_result
                        push_log("info", f"📱 [#{sid}][{account_index+1}] Numéro injecté : {pre_number_result[1] if pre_number_result else '—'}")

                    sess_state["current_step"] = f"[#{sid}] Création Tinder [{account_index+1}]"
                    push_log("info", f"📲 [#{sid}][{account_index+1}] Lancement Tinder...")
                    result_instagram = instagram_code.open_instagram(
                        device, None, city, lat, lon,
                        phone_id=phone_id, pwd=pwd,
                        bio=bio,
                        ban_on_existing_email=_session_config_store.get("ban_on_existing_email", False)
                    )

                    if result_instagram == "no_number_timeout":
                        push_log("warn", f"⏰ [#{sid}][{account_index+1}] Timeout numéro SMS — profil supprimé")
                        phone_id = None
                        with done_lock:
                            sess_state["errors"] += 1
                            sess_state["done"] += 1
                        return

                    if result_instagram == "email_banned":
                        push_log("warn", f"🚫 [#{sid}][{account_index+1}] Email existant — suppression et relance...")
                        try:
                            _unregister_phone_creation(phone_id)
                            instagram_code.delete_phone_geelark(phone_id)
                            push_log("success", f"✅ Profil supprimé")
                            phone_id = None
                        except Exception as e:
                            push_log("warn", f"⚠️ Erreur suppression : {e}")
                            phone_id = None
                        if not stopped() and _retry_count < 10:
                            push_log("warn", f"🔄 [#{sid}][{account_index+1}] Relance création (tentative {_retry_count + 1}/10)...")
                            run_one(slot, account_index, _retry_count + 1)
                        else:
                            push_log("error", f"❌ [#{sid}][{account_index+1}] Trop de tentatives email_banned — abandon")
                            with done_lock:
                                sess_state["errors"] += 1
                                sess_state["done"] += 1
                        return

                    push_log("info", f"🔍 [#{sid}][{account_index+1}] Vérification du compte...")
                    try:
                        statut = instagram_code.check_instagram_account(device)
                        try:
                            screenshot = instagram_code.take_screenshot(device)
                            phone_label = str(phone_id) if phone_id else device
                            if statut == "banned":
                                caption = (
                                    f"🚫 <b>Compte BANNI à la création</b>\n"
                                    f"📱 Téléphone : {phone_label}\n"
                                    f"🌆 Ville : {city or 'inconnue'}"
                                )
                            else:
                                caption = (
                                    f"✅ <b>Compte VIVANT créé</b>\n"
                                    f"📱 Téléphone : {phone_label}\n"
                                    f"🌆 Ville : {city or 'inconnue'}"
                                )
                            if screenshot:
                                instagram_code.telegram_send_photo(screenshot, caption)
                            else:
                                instagram_code.telegram_send_message(caption)
                        except Exception as _te:
                            push_log("warn", f"⚠️ Telegram erreur : {_te}")

                        if statut == "banned":
                            push_log("warn", f"🚫 [#{sid}][{account_index+1}] Banni — suppression...")
                            instagram_code.delete_phone_geelark(phone_id)
                            phone_id = None
                            with done_lock:
                                sess_state["errors"] += 1
                                sess_state["done"] += 1
                            return
                        else:
                            push_log("success", f"✅ [#{sid}][{account_index+1}] Compte vivant confirmé !")
                    except Exception as e:
                        push_log("warn", f"⚠️ [#{sid}][{account_index+1}] Erreur vérif : {e}")

                    push_log("success", f"✅ [#{sid}][{account_index+1}] Compte créé !")
                    with done_lock:
                        sess_state["success"] += 1
                        sess_state["done"] += 1

                except Exception as e:
                    import traceback
                    push_log("error", f"❌ [#{sid}][{account_index+1}] Exception : {e}")
                    push_log("error", traceback.format_exc().strip())
                    with done_lock:
                        sess_state["errors"] += 1
                        sess_state["done"] += 1
                finally:
                    if phone_id:
                        _unregister_phone_creation(phone_id)
                        try:
                            instagram_code.stop_phone(phone_id)
                        except:
                            pass

            account_idx = 0
            for boucle in range(boucles):
                if stopped(): break
                push_log("info", f"🔄 [#{sid}] Boucle {boucle+1}/{boucles}")
                threads = []
                for slot_idx in range(nb_threads):
                    if stopped(): break
                    slot = slots[slot_idx % len(slots)]
                    t = threading.Thread(target=run_one, args=(slot, account_idx), daemon=True)
                    threads.append(t)
                    account_idx += 1
                for t in threads: t.start()
                for t in threads: t.join()
                if boucle < boucles - 1 and not stopped():
                    push_log("info", f"⏳ [#{sid}] Pause ({delay}s)...")
                    time.sleep(delay)

            push_log("success", f"🎉 [#{sid}] Terminé — {sess_state['success']}/{sess_state['done']} succès")

    except Exception as e:
        import traceback
        push_log("error", f"❌ Erreur fatale session #{sid} : {e}")
        push_log("error", traceback.format_exc().strip())
    finally:
        sess_state["running"] = False
        sess_state["current_step"] = "Terminé"


import subprocess as _sp
import json as _json

_POOL_LOG_KEYWORDS_LOWER = [
    "[pool]",  # toutes les lignes préfixées [POOL] → console numéros/emails
    "pool scraper", "pré-récupération numéro", "hero sms", "hero erreur",
    "no_numbers", "maxprice", "aucun numéro disponible", "aucun numéro dispo",
    "⏳ aucun numéro", "smspin", "smsbower", "sim aggregator", "pvapins",
    "numéro disponible", "récupération numéro sms", "free channels",
    "no free channels", "numéro depuis le pool", "numéro api",
    "numéro saisi via keycodes", "numéro pioché dans le pool",
    "numéro de remplacement depuis le pool", "numéro session terminé",
]

def _is_pool_line(line):
    low = line.lower()
    return any(kw in low for kw in _POOL_LOG_KEYWORDS_LOWER)

import re as _re
import collections as _collections
# (regex, group_index_number, group_index_provider)
_NUMERO_ADD_PATTERNS = [
    # ✅ Numéro API : 664754062 (smspin)
    (_re.compile(r"Numéro API\s*:\s*(\d+)\s*\(([^)]+)\)"), 1, 2),
    # ✅ [SMSPIN] Numéro obtenu : 33752933224
    (_re.compile(r"\[([A-Z]+)\]\s*Numéro obtenu\s*:\s*(\d+)"), 2, 1),
]
_NUMERO_REMOVE_PATTERNS = [
    _re.compile(r"Numéro pioché dans le POOL\s*:\s*(\d+)"),
    _re.compile(r"Numéro saisi via keycodes\s*:\s*(\d+)"),
    _re.compile(r"Numéro session terminé\s*:\s*(\d+)"),
]

def _remove_from_pool(number):
    """Retire un numéro du pool serveur — supporte match exact ou suffixe (avec/sans indicatif)."""
    if not INSTAGRAM_CODE_LOADED:
        return
    try:
        with instagram_code._number_pool_lock:
            instagram_code._number_pool = type(instagram_code._number_pool)(
                e for e in instagram_code._number_pool
                if e["number"] != number and not e["number"].endswith(number)
            )
    except Exception:
        pass

# ── Inventaire email côté serveur (parsé depuis les logs workers) ─────────────
_email_pool_server      = _collections.deque()
_email_pool_server_lock = _threading.Lock()
_EMAIL_ADD_RE  = _re.compile(r"Gmail obtenu\s*:\s*([\w.+\-]+@[\w.\-]+)\s*\(mailId=(\d+)")
_EMAIL_USED_RE = _re.compile(r"Gmail (?:pré-récupéré|injecté|depuis le pool)\s*:\s*([\w.+\-]+@[\w.\-]+)")

def _try_add_worker_email_to_pool(line, owner=None):
    """Détecte un email obtenu/consommé dans un log worker et met à jour l'inventaire serveur."""
    rm = _EMAIL_USED_RE.search(line)
    if rm:
        addr = rm.group(1)
        with _email_pool_server_lock:
            _email_pool_server.__class__
            for e in list(_email_pool_server):
                if e["mail"] == addr:
                    try: _email_pool_server.remove(e)
                    except Exception: pass
                    break
        return
    m = _EMAIL_ADD_RE.search(line)
    if m:
        addr, mail_id = m.group(1), m.group(2)
        with _email_pool_server_lock:
            existing = {e["mail"] for e in _email_pool_server}
            if addr not in existing:
                _email_pool_server.append({
                    "mail": addr, "mail_id": mail_id, "owner": owner,
                    "expires_at": time.time() + 1200,  # 20 min
                })

def _try_add_worker_number_to_pool(line, owner=None):
    """Détecte un numéro obtenu/consommé dans un log worker et met à jour le pool serveur."""
    if not INSTAGRAM_CODE_LOADED:
        return
    # Numéro consommé → retirer de l'inventaire
    for rm_re in _NUMERO_REMOVE_PATTERNS:
        rm = rm_re.search(line)
        if rm:
            _remove_from_pool(rm.group(1))
            return
    # Numéro obtenu → ajouter à l'inventaire
    for pattern, num_grp, prov_grp in _NUMERO_ADD_PATTERNS:
        m = pattern.search(line)
        if m:
            number = m.group(num_grp)
            provider = m.group(prov_grp).strip().lower()
            try:
                import time as _t
                entry = {"number": number, "provider": provider, "expires_at": _t.time() + 600, "owner": owner}
                with instagram_code._number_pool_lock:
                    existing = {e["number"] for e in instagram_code._number_pool}
                    if number not in existing:
                        instagram_code._number_pool.append(entry)
            except Exception:
                pass
            break

def run_worker_manager(config, sess_state, stop_flag):
    """
    Lance chaque compte dans un subprocess worker isolé.
    Le worker gère lui-même : numéro → création profil → démarrage → Tinder
    """
    _thread_local.log_target = "creation"
    _thread_local.username = sess_state.get("username")
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = LogCapture("info", target="creation")
    sys.stderr = LogCapture("error", target="creation")

    sid        = sess_state["id"]
    sim        = config.get("simultane")
    proxy_list = _build_proxy_list(config, sim)
    total      = len(proxy_list)

    sess_state["total"]   = total
    sess_state["running"] = True

    push_log("info", f"🚀 Session #{sid} — {total} compte(s) en workers isolés")

    batch_size = sim.get("count", 1) if sim and sim.get("enabled") else 1

    done_count = 0
    for batch_start in range(0, total, batch_size):
        if stop_flag[0]:
            push_log("warn", "⛔ Arrêt demandé")
            break

        batch = proxy_list[batch_start:batch_start + batch_size]
        push_log("info", f"── Batch {batch_start//batch_size + 1} — {len(batch)} worker(s) ──")

        # ── Lancer 1 worker subprocess par proxy directement ──────────────
        # Plus de création GeeLark ici — c'est le worker qui s'en charge
        procs = []
        for worker_idx, proxy in enumerate(batch):
            if stop_flag[0]:
                break

            global_idx  = batch_start + worker_idx
            log_file    = os.path.join(_TMP_DIR, f"instagramops_worker_{sid}_{global_idx}.log")

            worker_config = _json.dumps({
                "proxy":                  proxy,
                "bio":                    config.get("bio", ""),
                "firstname":              config.get("firstname", ""),
                "first_names":            getattr(instagram_code, 'FIRST_NAMES', ['Miahyvina']),
                "birth_year":             config.get("birth_year", "2004"),
                "email":                  config.get("email", ""),
                "worker_id":              global_idx,
                "log_file":               log_file,
                "sms_config":             config.get("sms_config", {}),
                "geelark_config":         config.get("geelark_config", {}),
                "telegram_config":        config.get("telegram_config", {}),
                "photos_dir":             config.get("photos_dir", ""),
                "profile_stock_dir":      _mlib_profile_base(sess_state.get("username") or "default"),
                "ban_on_existing_email":  config.get("ban_on_existing_email", False),
                "target_country":         proxy.get("gps_country") or config.get("target_country", "france"),
                "creation_mode":          getattr(instagram_code, 'CREATION_MODE', 'phone'),
                "android_version":        getattr(instagram_code, 'ANDROID_VERSION', 'Android 14'),
            })

            push_log("info",
                f"🔧 Worker {global_idx+1} — proxy {proxy['host']}:{proxy['port']}"
            )

            if IS_FROZEN:
                # Binaire compilé : on se relance soi-même en mode worker.
                _worker_cmd = [sys.executable, "--worker", worker_config]
            else:
                _worker_path = os.path.join(_bundle_dir(), "worker.py")
                _worker_cmd = [sys.executable, "-u", _worker_path, worker_config]
            proc = _sp.Popen(
                _worker_cmd,
                stdout=_sp.PIPE,
                stderr=_sp.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=_app_dir(),
            )
            procs.append((proc, global_idx, log_file))

            # Petit délai symbolique pour ne pas tout lancer à la même milliseconde
            time.sleep(2)

        # ── Lire les logs de chaque worker via threads ────────────────────
        from queue import Queue as _Queue, Empty as _Empty

        line_q = _Queue()

        def _reader(proc, widx):
            try:
                for raw in proc.stdout:
                    line = raw.rstrip('\n')
                    if line:
                        line_q.put(('log', widx, line, proc))
            except Exception:
                pass
            line_q.put(('done', widx, None, proc))

        for proc, widx, log_file in procs:
            threading.Thread(target=_reader, args=(proc, widx), daemon=True).start()

        pending = {widx for _, widx, _ in procs}
        while pending and not stop_flag[0]:
            try:
                kind, widx, line, proc = line_q.get(timeout=0.5)
            except _Empty:
                continue
            if kind == 'log':
                # Intercepter __GEELARK_URL__ ici pour associer le bon sess_id
                if "__GEELARK_URL__:" in line:
                    try:
                        _gp = line.split("__GEELARK_URL__:", 1)[1].split(":", 1)
                        if len(_gp) == 2:
                            with _geelark_urls_lock:
                                _active_geelark_urls[_gp[0].strip()] = {
                                    "url": _gp[1].strip(), "sess_id": sid,
                                    "owner": sess_state.get("username"), "added_at": time.time()
                                }
                    except Exception:
                        pass
                    continue
                lvl = (
                    "success" if any(x in line for x in ["✅", "OK", "vivant"])
                    else "error" if any(x in line for x in ["❌", "Erreur", "BANNI"])
                    else "warn"  if any(x in line for x in ["⚠️", "🚫", "⏰"])
                    else "info"
                )
                if _is_pool_line(line):
                    push_log_pool(lvl, f"[W{widx+1}] {line}")
                    _try_add_worker_number_to_pool(line, owner=sess_state.get("username"))
                    _try_add_worker_email_to_pool(line, owner=sess_state.get("username"))
                else:
                    push_log(lvl, f"[W{widx+1}] {line}")
            else:
                code = proc.wait()
                if code == 0:
                    push_log("success", f"✅ Worker {widx+1} terminé avec succès")
                    sess_state["success"] += 1
                    # Déduire 1 crédit par compte créé avec succès
                    _creator_username = sess_state.get("username")
                    if _creator_username:
                        with _users_rw_lock:
                            _ud = _load_users()
                            _u  = _ud["users"].get(_creator_username, {})
                            if _u.get("role") != "admin":
                                _cur = _u.get("credits", 0)
                                _ud["users"][_creator_username]["credits"] = max(0, _cur - 1)
                                _save_users(_ud)
                                push_log("info", f"💳 1 crédit déduit ({_creator_username}) — reste : {max(0, _cur - 1)}")
                elif code == 2:
                    push_log("warn", f"🚫 Worker {widx+1} — compte banni")
                    sess_state["errors"] += 1
                else:
                    push_log("error", f"❌ Worker {widx+1} — échec (code {code})")
                    sess_state["errors"] += 1
                done_count += 1
                sess_state["done"] = done_count
                pending.discard(widx)

        # Si stop demandé, killer les workers en cours
        if stop_flag[0]:
            for proc, widx, log_file in procs:
                try: proc.terminate()
                except Exception: pass
                push_log("warn", f"⛔ Worker {widx+1} tué")
            break

        # Pause entre batches
        if batch_start + batch_size < total and not stop_flag[0]:
            pause = config.get("delay", 10)
            push_log("info", f"⏳ Pause {pause}s avant prochain batch...")
            time.sleep(pause)

    push_log("success",
        f"🎉 Session #{sid} terminée — "
        f"{sess_state['success']}/{sess_state['done']} succès"
    )

    sess_state["running"] = False
    with _sessions_lock:
        try: active_sessions.remove(sess_state)
        except: pass

    # Nettoyer les URLs GeeLark de cette session
    with _geelark_urls_lock:
        to_del = [pid for pid, v in _active_geelark_urls.items() if v.get("sess_id") == sid]
        for pid in to_del:
            del _active_geelark_urls[pid]

    sys.stdout = old_stdout
    sys.stderr = old_stderr


def _build_proxy_list(config, sim):
    """Construit la liste des proxies selon la config."""
    if sim and sim.get("enabled"):
        slots   = sim.get("slots", [])
        count   = sim.get("count", 1)
        boucles = sim.get("boucles", 1)
        result  = []
        for b in range(boucles):
            for i in range(count):
                slot = slots[i % len(slots)]
                result.append({
                    "host":        slot.get("host", ""),
                    "port":        slot.get("port", ""),
                    "user":        slot.get("user", ""),
                    "pass":        slot.get("pass", ""),
                    "gps_country": slot.get("gps_country", ""),
                })
        return result
    else:
        # Mode normal — proxy unique répété N fois
        parts = config.get("rotating_proxy_host", "").split(":")
        if len(parts) >= 4:
            proxy = {"host": parts[0], "port": parts[1],
                     "user": parts[2], "pass": parts[3]}
        else:
            proxy = {"host": "", "port": "", "user": "", "pass": ""}
        return [proxy] * config.get("count", 1)


def _register_phone_creation(phone_id):
    with _phones_in_creation_lock:
        _phones_in_creation.add(str(phone_id))

def _unregister_phone_creation(phone_id):
    with _phones_in_creation_lock:
        _phones_in_creation.discard(str(phone_id))

# ── Log capture ───────────────────────────────────────────────────────────────
class LogCapture(io.StringIO):
    """Capture les print() et les redirige vers push_log ou push_log_swipe selon le thread."""
    def __init__(self, level="info", target="creation"):
        super().__init__()
        self.level = level
        self.target = target  # "creation" ou "swipe"

    def write(self, text):
        if text and text.strip():
            t = text.strip()
            level = self.level
            if any(x in t for x in ["✅", "OK", "succès", "Succès", "créé", "connecté"]):
                level = "success"
            elif any(x in t for x in ["❌", "Erreur", "erreur", "échoué", "Échec", "KO"]):
                level = "error"
            elif any(x in t for x in ["⚠️", "Timeout", "retry", "Retry"]):
                level = "warn"
            elif any(x in t for x in ["⏳", "Attente", "Chargement"]):
                level = "info"
            # Chaque thread envoie dans sa propre console
            target = getattr(_thread_local, 'log_target', self.target)
            if target == "swipe":
                push_log_swipe(level, t)
            else:
                push_log(level, t)

    def flush(self):
        pass

def push_log(level, msg):
    owner = getattr(_thread_local, 'username', None)
    # Intercepter __GEELARK_URL__:phone_id:url
    if "__GEELARK_URL__:" in msg:
        try:
            parts = msg.split("__GEELARK_URL__:", 1)[1].split(":", 1)
            if len(parts) == 2:
                _gurl_phone_id, _gurl_url = parts[0].strip(), parts[1].strip()
                sess_id = getattr(_thread_local, 'sess_id', None)
                with _geelark_urls_lock:
                    _active_geelark_urls[_gurl_phone_id] = {
                        "url": _gurl_url, "sess_id": sess_id, "owner": owner,
                        "added_at": time.time()
                    }
        except Exception:
            pass
        return  # ne pas afficher cette ligne brute dans les logs
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "type": level, "msg": msg, "owner": owner}
    log_queue.put(entry)
    all_logs.append(entry)
    if len(all_logs) > 3000:
        all_logs.pop(0)
    if owner:
        with _user_logs_lock:
            lst = _user_logs.setdefault(owner, [])
            lst.append(entry)
            if len(lst) > _MAX_USER_LOGS:
                del lst[0]
    sys.__stdout__.write(f"[{level.upper()}] {msg}\n")
    sys.__stdout__.flush()



def push_log_pool(level, msg):
    owner = getattr(_thread_local, 'username', None)
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "type": level, "msg": msg, "owner": owner}
    log_queue_pool.put(entry)
    all_logs_pool.append(entry)
    if len(all_logs_pool) > 1000:
        all_logs_pool.pop(0)
    if owner:
        with _user_logs_lock:
            lst = _user_logs_pool.setdefault(owner, [])
            lst.append(entry)
            if len(lst) > _MAX_USER_LOGS:
                del lst[0]
    sys.__stdout__.write(f"[POOL/{level.upper()}] {msg}\n")
    sys.__stdout__.flush()

def pool_log_to_sse(msg: str):
    """Appelé depuis code.py pool_log() pour pousser vers le SSE Flask."""
    level = "success" if any(x in msg for x in ["✅", "➕"]) else \
            "error"   if any(x in msg for x in ["❌", "⛔"]) else \
            "warn"    if any(x in msg for x in ["⚠️", "⏰", "⏳"]) else "info"
    push_log_pool(level, msg)

def push_log_swipe(level, msg):
    owner = getattr(_thread_local, 'username', None)
    if "__GEELARK_URL__:" in msg:
        try:
            parts = msg.split("__GEELARK_URL__:", 1)[1].split(":", 1)
            if len(parts) == 2:
                _gurl_phone_id, _gurl_url = parts[0].strip(), parts[1].strip()
                sess_id = getattr(_thread_local, 'sess_id', None)
                with _geelark_urls_lock:
                    _active_geelark_urls[_gurl_phone_id] = {
                        "url": _gurl_url, "sess_id": sess_id, "owner": owner,
                        "added_at": time.time()
                    }
        except Exception:
            pass
        return
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "type": level, "msg": msg, "owner": owner}
    log_queue_swipe.put(entry)
    all_logs_swipe.append(entry)
    if len(all_logs_swipe) > 3000:
        all_logs_swipe.pop(0)
    if owner:
        with _user_logs_lock:
            lst = _user_logs_swipe.setdefault(owner, [])
            lst.append(entry)
            if len(lst) > _MAX_USER_LOGS:
                del lst[0]
    sys.__stdout__.write(f"[SWIPE/{level.upper()}] {msg}\n")
    sys.__stdout__.flush()


# ── Persistance ───────────────────────────────────────────────────────────────
def load_proxies_from_file():
    return _read_json_safe(proxies_file) or []

def save_proxies_to_file(proxies):
    try:
        with _file_lock:
            _write_json_safe(proxies_file, proxies)
    except Exception as e:
        sys.__stdout__.write(f"[WARN] save_proxies : {e}\n")

def load_swipe_proxies():
    return _read_json_safe(SWIPE_PROXIES_FILE) or []

def save_swipe_proxies(proxies):
    try:
        with _file_lock:
            _write_json_safe(SWIPE_PROXIES_FILE, proxies)
    except Exception as e:
        sys.__stdout__.write(f"[WARN] save_swipe_proxies : {e}\n")

# ── Routes statiques ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    with open(os.path.join(_bundle_dir(), "panel.html"), "r", encoding="utf-8") as f:
        html = f.read()
    from flask import Response
    resp = Response(html, mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp

# ── Logs ──────────────────────────────────────────────────────────────────────
@app.route("/api/logs")
def get_logs():
    logs = []
    try:
        while True:
            logs.append(log_queue.get_nowait())
    except queue.Empty:
        pass
    return jsonify(logs)

@app.route("/api/logs/all")
def get_all_logs():
    _, req_username = _get_user_from_request()
    if req_username:
        with _user_logs_lock:
            logs = list(_user_logs.get(req_username, []))
    else:
        logs = all_logs[-500:]
    return jsonify(logs[-200:])

@app.route("/api/logs/swipe")
def get_swipe_logs():
    logs = []
    try:
        while True:
            logs.append(log_queue_swipe.get_nowait())
    except queue.Empty:
        pass
    return jsonify(logs)

@app.route("/api/logs/swipe/all")
def get_all_swipe_logs():
    _, req_username = _get_user_from_request()
    if req_username:
        with _user_logs_lock:
            logs = list(_user_logs_swipe.get(req_username, []))
    else:
        logs = all_logs_swipe[-500:]
    return jsonify(logs[-200:])

@app.route("/api/logs/swipe/clear", methods=["POST"])
def clear_swipe_logs():
    _, req_username = _get_user_from_request()
    if req_username:
        with _user_logs_lock:
            _user_logs_swipe[req_username] = []
    else:
        all_logs_swipe.clear()
        while not log_queue_swipe.empty():
            try:
                log_queue_swipe.get_nowait()
            except:
                break
    return jsonify({"success": True})

@app.route("/api/logs/swipe/stream")
def stream_swipe_logs():
    _, req_username = _get_user_from_request()
    def generate():
        last_idx = 0
        while True:
            if req_username:
                with _user_logs_lock:
                    user_list = _user_logs_swipe.get(req_username, [])
                    snapshot = user_list[last_idx:]
                    last_idx += len(snapshot)
            else:
                snapshot = all_logs_swipe[last_idx:]
                last_idx += len(snapshot)

            for log in snapshot:
                yield f"data: {json.dumps(log)}\n\n"
            yield ": heartbeat\n\n"
            time.sleep(0.3)
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        }
    )


@app.route("/api/logs/pool/stream")
def pool_logs_stream():
    _, req_username = _get_user_from_request()
    def generate():
        last_idx = 0
        while True:
            if req_username:
                with _user_logs_lock:
                    user_list = _user_logs_pool.get(req_username, [])
                    snapshot = user_list[last_idx:]
                    last_idx += len(snapshot)
            else:
                snapshot = all_logs_pool[last_idx:]
                last_idx += len(snapshot)
            for log in snapshot:
                yield f"data: {json.dumps(log)}\n\n"
            yield ": heartbeat\n\n"
            time.sleep(0.3)
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    )

@app.route("/api/logs/pool/clear", methods=["POST"])
def pool_logs_clear():
    _, req_username = _get_user_from_request()
    if req_username:
        with _user_logs_lock:
            _user_logs_pool[req_username] = []
    else:
        all_logs_pool.clear()
        while not log_queue_pool.empty():
            try: log_queue_pool.get_nowait()
            except: break
    return jsonify({"success": True})

@app.route("/api/pool/inventory")
def pool_inventory():
    _, req_username = _get_user_from_request()
    import time as _t
    now = _t.time()
    entries = []
    # Numéros SMS
    if INSTAGRAM_CODE_LOADED:
        try:
            with instagram_code._number_pool_lock:
                for e in instagram_code._number_pool:
                    if e["expires_at"] > now and e.get("owner") == req_username:
                        entries.append({
                            "type": "phone",
                            "number":   e["number"],
                            "provider": e["provider"],
                            "remaining": max(0, e["expires_at"] - now),
                        })
        except Exception:
            pass
    # Emails
    try:
        with _email_pool_server_lock:
            for e in _email_pool_server:
                exp = e.get("expires_at", 0)
                if exp > now and e.get("owner") == req_username:
                    entries.append({
                        "type": "email",
                        "number":   e["mail"],
                        "provider": "gmail",
                        "remaining": max(0, exp - now),
                    })
    except Exception:
        pass
    return jsonify({"entries": entries, "count": len(entries)})

@app.route("/api/logs/pool/all")
def get_all_pool_logs():
    _, req_username = _get_user_from_request()
    if req_username:
        with _user_logs_lock:
            logs = list(_user_logs_pool.get(req_username, []))
    else:
        logs = all_logs_pool[-500:]
    return jsonify(logs[-200:])

@app.route("/api/logs/clear", methods=["POST"])
def clear_logs_api():
    _, req_username = _get_user_from_request()
    if req_username:
        with _user_logs_lock:
            _user_logs[req_username] = []
    else:
        all_logs.clear()
        while not log_queue.empty():
            try:
                log_queue.get_nowait()
            except:
                break
    return jsonify({"success": True})

@app.route("/api/logs/stream")
def stream_logs():
    _, req_username = _get_user_from_request()
    def generate():
        last_idx = 0
        while True:
            if req_username:
                with _user_logs_lock:
                    user_list = _user_logs.get(req_username, [])
                    snapshot = user_list[last_idx:]
                    last_idx += len(snapshot)
            else:
                with _logs_lock:
                    snapshot = all_logs[last_idx:]
                    last_idx += len(snapshot)

            sent = 0
            for log in snapshot:
                yield f"data: {json.dumps(log)}\n\n"
                sent += 1
                if sent >= 50:
                    break

            yield ": heartbeat\n\n"
            time.sleep(0.3)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        }
    )
@app.route("/api/logs/since/<int:idx>")
def logs_since(idx):
    _, req_username = _get_user_from_request()
    if req_username:
        with _user_logs_lock:
            user_list = _user_logs.get(req_username, [])
            slice_ = user_list[idx:]
            total  = len(user_list)
        return jsonify({"logs": slice_, "next_idx": total})
    with _logs_lock:
        slice_ = all_logs[idx:]
        total  = len(all_logs)
    return jsonify({"logs": slice_, "next_idx": total})

# ── Session status ────────────────────────────────────────────────────────────
@app.route("/api/session/status")
def session_status():
    _, req_username = _get_user_from_request()
    with _sessions_lock:
        running_sessions = [
            s for s in active_sessions
            if s.get("running") and s.get("username") == req_username
        ]
    if not running_sessions:
        session_state["running"] = False
        return jsonify(session_state)

    # Agréger toutes les sessions actives
    total_done    = sum(s.get("done", 0) for s in running_sessions)
    total_total   = sum(s.get("total", 0) for s in running_sessions)
    total_success = sum(s.get("success", 0) for s in running_sessions)
    total_errors  = sum(s.get("errors", 0) for s in running_sessions)

    # Prendre les infos de la dernière session active pour l'affichage
    last = running_sessions[-1]

    return jsonify({
        "running": True,
        "done": total_done,
        "total": total_total,
        "success": total_success,
        "errors": total_errors,
        "current_account": last.get("current_account"),
        "current_step": last.get("current_step"),
        "current_device": last.get("current_device"),
        "current_city": last.get("current_city"),
        "active_sessions_count": len(running_sessions),
        "sessions": [{"id": s["id"], "done": s["done"], "total": s["total"], "success": s["success"]} for s in running_sessions],
    })

@app.route("/api/session/active")
def get_active_sessions():
    """Retourne la liste de toutes les sessions actives."""
    with _sessions_lock:
        return jsonify({
            "sessions": [
                {
                    "id": s["id"],
                    "running": s["running"],
                    "done": s["done"],
                    "total": s["total"],
                    "success": s["success"],
                    "errors": s["errors"],
                    "current_step": s.get("current_step"),
                }
                for s in active_sessions
            ],
            "count": len(active_sessions),
        })

# ── Téléphones ────────────────────────────────────────────────────────────────
@app.route("/api/phones")
def get_phones():
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé", "phones": []})

    # Enforce per-user GeeLark isolation:
    # Only show phones if the user has their own credentials configured.
    user, username = _get_user_from_request()
    if user and username:
        user_cfg = user.get("config", {})
        gl_user = user_cfg.get("geelark", {})
        has_creds = any([gl_user.get("app_id"), gl_user.get("api_key"), gl_user.get("bearer")])
        if not has_creds:
            return jsonify({"phones": [], "excluded_count": 0,
                            "info": "Configurez vos clés GeeLark dans Paramètres"})
        # Apply this user's credentials before querying
        saved = (
            instagram_code.GEELARK_APP_ID,
            instagram_code.GEELARK_API_KEY,
            instagram_code.GEELARK_BEARER,
        )
        try:
            if gl_user.get("app_id"):  instagram_code.GEELARK_APP_ID  = gl_user["app_id"]
            if gl_user.get("api_key"): instagram_code.GEELARK_API_KEY = gl_user["api_key"]
            if gl_user.get("bearer"):  instagram_code.GEELARK_BEARER  = gl_user["bearer"]
            phones = instagram_code.get_all_phones()
            instagram_code.all_phones = phones
        except Exception as e:
            if instagram_code.all_phones:
                phones = instagram_code.all_phones
            else:
                return jsonify({"error": str(e), "phones": []})
        finally:
            instagram_code.GEELARK_APP_ID, instagram_code.GEELARK_API_KEY, instagram_code.GEELARK_BEARER = saved
    else:
        try:
            phones = instagram_code.get_all_phones()
            instagram_code.all_phones = phones
        except Exception as e:
            if instagram_code.all_phones:
                phones = instagram_code.all_phones
            else:
                return jsonify({"error": str(e), "phones": []})

    in_creation = _get_phones_in_creation()
    if in_creation:
        phones = [p for p in phones if str(p.get("id")) not in in_creation]

    group_filter = request.args.get("group", "").strip().lower()
    if group_filter:
        phones = [p for p in phones if p.get("group", "").lower() == group_filter]

    # Enrich each phone with warmup status
    with _posting_history_lock:
        history = _load_posting_history()
    for p in phones:
        pid = str(p.get("id", ""))
        entries = history.get(pid, [])
        p["warmup"] = any(e["action"] == "warmup" for e in entries)

    return jsonify({"phones": phones, "excluded_count": len(in_creation)})


@app.route("/api/phones/raw-debug")
def phones_raw_debug():
    """Retourne les données brutes de l'API GeeLark pour voir les champs disponibles."""
    _, username = _get_user_from_request()
    if username:
        user = _load_users().get("users", {}).get(username, {})
        gl = user.get("config", {}).get("geelark", {})
        saved = (instagram_code.GEELARK_APP_ID, instagram_code.GEELARK_API_KEY, instagram_code.GEELARK_BEARER)
        if gl.get("app_id"):  instagram_code.GEELARK_APP_ID  = gl["app_id"]
        if gl.get("api_key"): instagram_code.GEELARK_API_KEY = gl["api_key"]
        if gl.get("bearer"):  instagram_code.GEELARK_BEARER  = gl["bearer"]
    try:
        result = instagram_code.geelark_request("POST", "/open/v1/phone/list", {"page": 1, "pageSize": 5})
        items = result.get("data", {}).get("items", [])
        return jsonify({"first_phone_raw": items[0] if items else None, "total": len(items)})
    finally:
        if username:
            instagram_code.GEELARK_APP_ID, instagram_code.GEELARK_API_KEY, instagram_code.GEELARK_BEARER = saved


@app.route("/api/phones/start", methods=["POST"])
def start_phone_route():
    data = request.json
    phone_id = data.get("phone_id")
    if not phone_id:
        return jsonify({"error": "phone_id manquant"}), 400
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé"}), 500
    try:
        geelark_url = None
        
        # Capturer stdout pour intercepter __GEELARK_URL__
        import io
        old_stdout = sys.stdout
        captured = io.StringIO()
        sys.stdout = captured
        
        ok = instagram_code.start_phone(phone_id)
        
        sys.stdout = old_stdout
        output = captured.getvalue()
        
        # Pousser dans les logs
        for line in output.splitlines():
            if line.strip():
                push_log("info", line.strip())
                if "__GEELARK_URL__:" in line:
                    geelark_url = line.split("__GEELARK_URL__:")[1].strip()
        
        return jsonify({"success": ok, "url": geelark_url})
    except Exception as e:
        sys.stdout = old_stdout if 'old_stdout' in dir() else sys.stdout
        return jsonify({"error": str(e)}), 500

@app.route("/api/phones/stop", methods=["POST"])
def stop_phone_route():
    data = request.json
    phone_id = data.get("phone_id")
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé"}), 500
    try:
        instagram_code.stop_phone(phone_id)
        # Retirer l'URL active si elle existe
        with _geelark_urls_lock:
            _active_geelark_urls.pop(str(phone_id), None)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/geelark/active_urls", methods=["GET"])
def get_active_geelark_urls():
    _url_ttl = 3600  # expiration 1h si le thread a crashé sans cleanup
    _now = time.time()
    with _geelark_urls_lock:
        expired = [pid for pid, v in _active_geelark_urls.items()
                   if _now - v.get("added_at", _now) > _url_ttl]
        for pid in expired:
            del _active_geelark_urls[pid]
        return jsonify({"urls": dict(_active_geelark_urls)})

@app.route("/api/geelark/active_urls/<phone_id>", methods=["DELETE"])
def delete_geelark_url(phone_id):
    with _geelark_urls_lock:
        _active_geelark_urls.pop(str(phone_id), None)
    return jsonify({"success": True})

@app.route("/api/phones/create", methods=["POST"])
def create_phone():
    data = request.json
    proxy_str = data.get("proxy", "")
    parts = proxy_str.split(":")
    if len(parts) < 4:
        return jsonify({"error": "Format proxy invalide (host:port:user:pass)"}), 400
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé"}), 500
    try:
        phone_id = instagram_code.create_phone_profile(parts[0], parts[1], parts[2], parts[3])
        return jsonify({"success": phone_id is not None, "phone_id": phone_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/phones/adb", methods=["POST"])
def get_adb():
    data = request.json
    phone_id = data.get("phone_id")
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé"}), 500
    try:
        device, pwd = instagram_code.get_adb_info(phone_id)
        return jsonify({"device": device, "pwd": pwd})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/phones/gps", methods=["POST"])
def set_gps():
    data = request.json
    phone_id = data.get("phone_id")
    lat = data.get("lat")
    lon = data.get("lon")
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé"}), 500
    try:
        ok = instagram_code.set_gps(phone_id, lat, lon)
        return jsonify({"success": ok})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Comptes ───────────────────────────────────────────────────────────────────
@app.route("/api/accounts")
def get_accounts():
    accounts = []
    if os.path.exists(accounts_file):
        with open(accounts_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                acc = parse_account_line(line)
                if acc:
                    accounts.append(acc)
    return jsonify({"accounts": accounts})

def parse_account_line(line):
    try:
        parts = {}
        for p in line.split("|"):
            p = p.strip()
            if "=" in p:
                k, v = p.split("=", 1)
                parts[k.strip()] = v.strip()
        token = parts.get("token", "")
        if not token:
            return None
        return {
            "token": token,
            "city": parts.get("city", "—"),
            "lat": float(parts.get("lat", 0)) if parts.get("lat") else 0,
            "lon": float(parts.get("lon", 0)) if parts.get("lon") else 0,
            "number": parts.get("number", "—"),
            "date": parts.get("date", datetime.now().strftime("%d/%m/%Y %H:%M")),
            "raw": line
        }
    except:
        return None

@app.route("/api/accounts/delete", methods=["POST"])
def delete_account():
    data = request.json
    token = data.get("token")
    if not token or not os.path.exists(accounts_file):
        return jsonify({"success": False})
    with open(accounts_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    lines = [l for l in lines if token not in l]
    with open(accounts_file, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return jsonify({"success": True})

@app.route("/api/accounts/clear", methods=["POST"])
def clear_accounts():
    open(accounts_file, "w").close()
    return jsonify({"success": True})

# ── Préférences profil Tinder ────────────────────────────────────────────────
@app.route("/api/user/profile_defaults", methods=["GET"])
def get_profile_defaults():
    _, username = _get_user_from_request()
    if not username:
        return jsonify({}), 401
    users_data = _load_users()
    defaults = users_data["users"].get(username, {}).get("profile_defaults", {})
    return jsonify(defaults)

@app.route("/api/user/profile_defaults", methods=["POST"])
def save_profile_defaults():
    _, username = _get_user_from_request()
    if not username:
        return jsonify({"error": "Non authentifié"}), 401
    data = request.json or {}
    allowed = {"firstname", "birth_year", "email", "bio"}
    defaults = {k: v for k, v in data.items() if k in allowed}
    with _users_rw_lock:
        users_data = _load_users()
        users_data["users"].setdefault(username, {})["profile_defaults"] = defaults
        _save_users(users_data)
    return jsonify({"success": True})

# ── Photos ────────────────────────────────────────────────────────────────────
@app.route("/api/photos")
def get_photos():
    base = request.args.get("dir", "")

    if not base:
        user, username = _get_user_from_request()
        if username:
            # Priorité 1 : ZIP sauvegardé (source de vérité)
            base = _get_user_batch_dir(username) or ""
        if not base and user:
            # Priorité 2 : photos_dir stocké dans config
            base = user.get("config", {}).get("system", {}).get("photos_dir", "")

    if not base and INSTAGRAM_CODE_LOADED:
        base = instagram_code.PHOTOS_BASE_DIR

    if not base or not os.path.exists(base):
        return jsonify({"folders": [], "count": 0, "base": base or "—"})

    folders = [d for d in os.listdir(base)
               if os.path.isdir(os.path.join(base, d)) and not d.startswith('.')]
    return jsonify({"folders": folders, "count": len(folders), "base": base})

# ── Proxies création ──────────────────────────────────────────────────────────
@app.route("/api/proxies", methods=["GET"])
def get_proxies():
    user, username = _get_user_from_request()
    if not user:
        return jsonify({"error": "Non authentifié", "folders": []}), 401
    users_data = _load_users()
    folders = users_data["users"].get(username, {}).get("proxies", [])
    return jsonify({"folders": folders})

@app.route("/api/proxies", methods=["POST"])
def save_proxies():
    user, username = _get_user_from_request()
    if not user:
        return jsonify({"error": "Non authentifié"}), 401
    data = request.json
    folders = data.get("folders", data.get("proxies", []))
    with _users_rw_lock:
        users_data = _load_users()
        users_data["users"][username]["proxies"] = folders
        _save_users(users_data)
    return jsonify({"success": True, "count": len(folders)})

@app.route("/api/proxies/add", methods=["POST"])
def add_proxy_server():
    user, username = _get_user_from_request()
    if not user:
        return jsonify({"error": "Non authentifié"}), 401
    data = request.json
    with _users_rw_lock:
        users_data = _load_users()
        proxies = users_data["users"][username].get("proxies", [])
        proxies.append(data)
        users_data["users"][username]["proxies"] = proxies
        _save_users(users_data)
    return jsonify({"success": True, "proxies": proxies})

# ── Proxies swipe ─────────────────────────────────────────────────────────────
@app.route("/api/swipe/proxies", methods=["GET"])
def get_swipe_proxies():
    return jsonify({"proxies": load_swipe_proxies()})

@app.route("/api/swipe/proxies", methods=["POST"])
def save_swipe_proxies_route():
    data = request.json
    proxies = data.get("proxies", [])
    save_swipe_proxies(proxies)
    return jsonify({"success": True, "count": len(proxies)})

@app.route("/api/swipe/proxies/add", methods=["POST"])
def add_swipe_proxy():
    data = request.json
    proxies = load_swipe_proxies()
    proxies.append(data)
    save_swipe_proxies(proxies)
    return jsonify({"success": True, "proxies": proxies})

@app.route("/api/swipe/proxies/delete", methods=["POST"])
def delete_swipe_proxy():
    idx = request.json.get("idx", -1)
    proxies = load_swipe_proxies()
    if 0 <= idx < len(proxies):
        proxies.pop(idx)
        save_swipe_proxies(proxies)
    return jsonify({"success": True, "proxies": proxies})

@app.route("/api/swipe/proxies/import", methods=["POST"])
def import_swipe_proxies():
    raw = request.json.get("raw", "")
    proxies = load_swipe_proxies()
    added = 0
    for line in raw.strip().splitlines():
        parts = line.strip().split(":")
        if len(parts) >= 4:
            proxies.append({
                "host": parts[0], "port": parts[1],
                "user": parts[2], "pass": parts[3],
                "type": "socks5", "name": "",
            })
            added += 1
    save_swipe_proxies(proxies)
    return jsonify({"success": True, "added": added, "proxies": proxies})

# ── Tests ─────────────────────────────────────────────────────────────────────
@app.route("/api/test/geelark")
def test_geelark():
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"success": False, "error": "code.py non chargé"})
    try:
        result = instagram_code.geelark_request("POST", "/open/v1/phone/list", {"page": 1, "pageSize": 1})
        ok = result.get("code") == 0
        return jsonify({"success": ok, "response": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/test/hero")
def test_hero():
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"success": False, "error": "code.py non chargé"})
    try:
        r = req.get("https://hero-sms.com/stubs/handler_api.php", params={
            "api_key": instagram_code.HERO_API_KEY, "action": "getBalance"
        }, timeout=5)
        return jsonify({"success": True, "response": r.text})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# ── Debug input ───────────────────────────────────────────────────────────────
@app.route("/api/debug/input", methods=["POST"])
def debug_input():
    data = request.json
    cmd = data.get("cmd", "continue")
    while not debug_input_queue.empty():
        try:
            debug_input_queue.get_nowait()
        except:
            break
    debug_input_queue.put(cmd)
    push_log("info", f"🎮 Commande debug reçue : {cmd}")
    return jsonify({"success": True, "cmd": cmd})

# ── Email code ────────────────────────────────────────────────────────────────
_email_code_store = {}  # { phone_id: {"code": "", "waiting": False, "device": ""} }
_session_config_store = {"ban_on_existing_email": True}

@app.route("/api/session/waiting_code", methods=["POST"])
def set_waiting_code():
    data = request.json
    phone_id = str(data.get("phone_id", "default"))
    _email_code_store[phone_id] = {
        "waiting": data.get("waiting", False),
        "device": data.get("device", ""),
        "code": ""
    }
    push_log("warn", f"⚠️ [{phone_id}] EN ATTENTE CODE EMAIL — saisissez le code dans le panel !")
    return jsonify({"success": True})

@app.route("/api/session/email_code", methods=["GET"])
def get_email_code():
    phone_id = request.args.get("phone_id", "default")
    store = _email_code_store.get(str(phone_id), {})
    return jsonify({
        "code": store.get("code", ""),
        "waiting": store.get("waiting", False)
    })

@app.route("/api/session/email_code", methods=["POST"])
def set_email_code():
    data = request.json
    phone_id = str(data.get("phone_id", "default"))
    code = data.get("code", "")
    if phone_id not in _email_code_store:
        _email_code_store[phone_id] = {}
    _email_code_store[phone_id]["code"] = code
    if code:
        _email_code_store[phone_id]["waiting"] = False
        push_log("success", f"✅ [{phone_id}] Code email reçu : {code}")
    return jsonify({"success": True})

@app.route("/api/session/code_status", methods=["GET"])
def get_code_status():
    # Retourne tous les comptes en attente
    waiting = {
        pid: store for pid, store in _email_code_store.items()
        if store.get("waiting")
    }
    return jsonify({"waiting_all": waiting, "count": len(waiting)})

@app.route("/api/session/config", methods=["GET"])
def get_session_config():
    return jsonify(_session_config_store)




@app.route("/api/session/launch", methods=["POST"])
def launch_session():
    global stop_requested, _session_id_counter

    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé"}), 500

    stop_requested = False

    _, username = _get_user_from_request()
    if username:
        _apply_user_config(username)
        users_data = _load_users()
        user_data  = users_data["users"].get(username, {})
        merged_cfg = _deep_merge(app_config, user_data.get("config", {}))
    else:
        merged_cfg = app_config

    # ── Validations pré-lancement ────────────────────────────────────────────
    errors = []

    # 1. GeeLark configuré
    if INSTAGRAM_CODE_LOADED:
        if not instagram_code.GEELARK_APP_ID or not instagram_code.GEELARK_API_KEY:
            errors.append("GeeLark non configuré — ajoutez App ID et API Key dans Paramètres")
        _creation_mode = getattr(instagram_code, 'CREATION_MODE', 'phone')
        if _creation_mode == 'email':
            # Mode email : vérifier que la clé SMSBower est présente (utilisée pour l'API mail)
            if not getattr(instagram_code, 'SMSBOWER_API_KEY', ''):
                errors.append("Mode email : clé SMSBower requise — configurez-la dans Paramètres > SMS Providers > SMSBower")
        else:
            sms_ok = any([
                getattr(instagram_code, 'HEROSMS_ENABLED', False) and getattr(instagram_code, 'HERO_API_KEY', ''),
                getattr(instagram_code, 'SMSPIN_ENABLED', False) and getattr(instagram_code, 'SMSPIN_API_KEY', ''),
                getattr(instagram_code, 'SMSBOWER_ENABLED', False) and getattr(instagram_code, 'SMSBOWER_API_KEY', ''),
                getattr(instagram_code, 'SIM_AGGREGATOR_ENABLED', False) and getattr(instagram_code, 'SIM_AGGREGATOR_API_KEY', ''),
                getattr(instagram_code, 'PVAPINS_ENABLED', False) and getattr(instagram_code, 'PVAPINS_CUSTOMER', ''),
            ])
            if not sms_ok:
                errors.append("Aucun provider SMS actif avec clé API — activez et configurez au moins un provider dans Paramètres")
    # 3. Proxy configuré (mode simultané ou rotatif)
    req_data = request.json or {}
    sim = req_data.get("simultane")
    has_proxy = False
    if sim and sim.get("enabled"):
        slots = sim.get("slots", [])
        has_proxy = any(s.get("host") for s in slots)
    else:
        has_proxy = bool(req_data.get("rotating_proxy_host", "").strip())
    if not has_proxy:
        errors.append("Aucun proxy configuré — ajoutez un proxy dans la section Lancer session")

    if errors:
        return jsonify({"error": " | ".join(errors), "errors": errors}), 400

    data = request.json
    config = {
        "count":               int(data.get("count", 1)),
        "mode":                data.get("mode", "auto"),
        "delay":               int(data.get("delay", 10)),
        "firstname":           data.get("firstname", "Lilou"),
        "email":               data.get("email", ""),
        "birth_year":          data.get("birth_year", "2004"),
        "bio":                 data.get("bio", ""),
        "ban_on_existing_email": data.get("ban_on_existing_email", False),
        "phone_ids":           data.get("phone_ids", []),
        "hero_country":        data.get("hero_country", "187"),
        "use_rotating_proxy":  data.get("use_rotating_proxy", False),
        "rotating_proxy_host": data.get("rotating_proxy_host", ""),
        "rotating_url":        data.get("rotating_url", ""),
        "rotate_wait":         data.get("rotate_wait", True),
        "rotate_wait_sec":     int(data.get("rotate_wait_sec", 3)),
        "simultane":           data.get("simultane", None),
        "sms_config":          merged_cfg.get("sms", {}),
        "geelark_config":      merged_cfg.get("geelark", {}),
        "telegram_config":     merged_cfg.get("telegram", {}),
        "photos_dir":          merged_cfg.get("system", {}).get("photos_dir", ""),
        "target_country":      _derive_target_country(merged_cfg.get("sms", {})),
    }

    # Créer un stop_flag dédié à cette session
    session_stop_flag = [False]
    
    with _sessions_lock:
        _session_id_counter += 1
        sid = _session_id_counter
        sess_state = {
            "id": sid,
            "username": username,
            "running": True,
            "done": 0, "total": config["count"],
            "success": 0, "errors": 0,
            "current_account": None, "current_step": None,
            "current_device": None, "current_city": None,
            "stop_flag": session_stop_flag,
        }
        active_sessions.append(sess_state)

    # Mettre à jour le state global pour compatibilité UI
    session_state["running"] = True
    session_state["total"] = config["count"]

# ── Déduction crédits ────────────────────────────────────────────────────
    if username:
        sim = req_data.get("simultane")
        if sim and sim.get("enabled"):
            nb_threads = int(sim.get("count", 1))
            boucles    = int(sim.get("boucles", 1))
            credits_needed = nb_threads * boucles
        else:
            credits_needed = config["count"]

        with _users_rw_lock:
            users_data = _load_users()
            user_data  = users_data["users"].get(username, {})
            current_credits = user_data.get("credits", 0)

            if user_data.get("role") != "admin":
                if current_credits < 1:
                    return jsonify({
                        "error": f"Crédits insuffisants — vous n'avez plus de crédit(s).",
                        "errors": ["Crédits insuffisants (0)"]
                    }), 400
                # Pas de déduction ici — 1 crédit sera déduit par compte créé avec succès

    thread = threading.Thread(
    target=run_worker_manager,
    args=(config, sess_state, session_stop_flag),
    daemon=True
    )
    thread.start()

    push_log("info", f"🚀 Session #{sid} lancée (total actives: {len(active_sessions)})")
    return jsonify({"success": True, "message": f"Session #{sid} lancée", "session_id": sid})


def _run_simultane(config, sim_cfg, old_stdout, old_stderr):
    global stop_requested, session_state

    slots  = sim_cfg.get("slots", [])
    count  = sim_cfg.get("count", 1)
    boucles = sim_cfg.get("boucles", 1)
    total  = count * boucles

    session_state["total"] = total
    push_log("info", f"🔀 Simultané : {count} threads × {boucles} boucles = {total} comptes")

    done_lock = threading.Lock()

    def run_one(slot, account_index):
        global stop_requested
        if stop_requested:
            return

        proxy_host = slot.get("host", "")
        proxy_port = slot.get("port", "")
        proxy_user = slot.get("user", "")
        proxy_pass = slot.get("pass", "")
        rotate_url = slot.get("rotateUrl", "") if slot.get("type") == "rotatif" else ""

        push_log("info", f"{'─'*40}")
        push_log("info", f"📋 [{account_index+1}/{total}] Thread démarré — proxy {proxy_host}:{proxy_port}")
        session_state["current_account"] = f"Compte {account_index+1}/{total}"

        phone_id = None
        try:
            # Création profil
            phone_id = instagram_code.create_phone_profile(
                "74z4xquqcn.cn.fxdx.in", "14864", "9haz01", "v51b8i"
            )
            if not phone_id:
                push_log("error", f"❌ [{account_index+1}] Échec création profil")
                with done_lock:
                    session_state["errors"] += 1
                    session_state["done"] += 1
                return

            push_log("success", f"✅ [{account_index+1}] Profil créé : {phone_id}")
            instagram_code.enable_root(phone_id)
            time.sleep(2)

            # Démarrage
            if not instagram_code.start_phone(phone_id):
                push_log("error", f"❌ [{account_index+1}] Impossible de démarrer")
                with done_lock:
                    session_state["errors"] += 1
                    session_state["done"] += 1
                return

            time.sleep(15)

            # Rotation IP
            if rotate_url:
                push_log("info", f"🔁 [{account_index+1}] Rotation IP...")
                try:
                    r = req.get(rotate_url, timeout=15)
                    push_log("success", f"✅ Rotation OK : {r.text.strip()[:60]}")
                except Exception as e:
                    push_log("warn", f"⚠️ Rotation échouée : {e}")
                time.sleep(int(config.get("rotate_wait_sec", 3)))

            # ADB
            instagram_code.enable_adb(phone_id)
            time.sleep(3)
            device, pwd = instagram_code.wait_for_adb(phone_id, max_wait=90)
            if not device:
                push_log("error", f"❌ [{account_index+1}] ADB timeout")
                instagram_code.stop_phone(phone_id)
                with done_lock:
                    session_state["errors"] += 1
                    session_state["done"] += 1
                return

            # glogin
            connected = False
            for attempt in range(10):
                if stop_requested:
                    break
                subprocess.run(f'"{instagram_code.ADB_PATH}" connect {device}', shell=True, capture_output=True)
                time.sleep(3)
                result = subprocess.run(
                    f'"{instagram_code.ADB_PATH}" -s {device} shell glogin {pwd}',
                    shell=True, capture_output=True, text=True
                )
                if "success" in result.stdout.lower():
                    connected = True
                    break

            if not connected:
                push_log("error", f"❌ [{account_index+1}] glogin échoué")
                instagram_code.stop_phone(phone_id)
                with done_lock:
                    session_state["errors"] += 1
                    session_state["done"] += 1
                return

            # Proxy
            instagram_code.change_phone_proxy(phone_id, proxy_host, proxy_port, proxy_user, proxy_pass, "socks5")
            time.sleep(3)

            instagram_code.enable_data_saver(device)
            # GPS
            city, lat, lon = instagram_code.apply_random_city_gps(phone_id)
            time.sleep(2)

            # Photos
            photo_folder = instagram_code.get_next_photo_folder()
            if photo_folder:
                instagram_code.push_photos_to_device(device, photo_folder, base_photos_dir=instagram_code.PHOTOS_BASE_DIR)

            # Tinder
            instagram_code.open_instagram(device, None, city, lat, lon,
                                     phone_id=phone_id, pwd=pwd,
                                     bio=config.get("bio", ""))

            push_log("success", f"✅ [{account_index+1}] Compte créé !")
            with done_lock:
                session_state["success"] += 1
                session_state["done"] += 1

        except Exception as e:
            import traceback
            push_log("error", f"❌ [{account_index+1}] Exception : {e}")
            push_log("error", traceback.format_exc().strip())
            with done_lock:
                session_state["errors"] += 1
                session_state["done"] += 1
        finally:
            if phone_id:
                try:
                    instagram_code.stop_phone(phone_id)
                except:
                    pass

    account_idx = 0
    for boucle in range(boucles):
        if stop_requested:
            break
        push_log("info", f"🔄 Boucle {boucle+1}/{boucles}")
        threads = []
        for slot_idx in range(count):
            if stop_requested:
                break
            slot = slots[slot_idx % len(slots)]
            t = threading.Thread(target=run_one, args=(slot, account_idx), daemon=True)
            threads.append(t)
            account_idx += 1

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if boucle < boucles - 1 and not stop_requested:
            push_log("info", f"⏳ Pause entre boucles ({config.get('delay', 10)}s)...")
            time.sleep(config.get("delay", 10))

    push_log("success", f"🎉 Simultané terminé — {session_state['success']}/{session_state['done']} succès")

    
def run_session(config):
    global session_state, stop_requested

    if stop_requested:
        session_state["running"] = False
        return

    if not INSTAGRAM_CODE_LOADED:
        push_log("error", "❌ code.py non disponible")
        session_state["running"] = False
        return

    while not debug_input_queue.empty():
        try:
            debug_input_queue.get_nowait()
        except:
            pass

    session_state.update({
        "running": True, "done": 0, "total": config["count"],
        "success": 0, "errors": 0,
    })

    # Appliquer la config GeeLark/SMS depuis le payload de la session
    geelark_cfg = config.get("geelark_config", {})
    if geelark_cfg.get("app_id"):  instagram_code.GEELARK_APP_ID  = geelark_cfg["app_id"]
    if geelark_cfg.get("api_key"): instagram_code.GEELARK_API_KEY = geelark_cfg["api_key"]
    if geelark_cfg.get("bearer"):  instagram_code.GEELARK_BEARER  = geelark_cfg["bearer"]
    
    sms_cfg = config.get("sms_config", {})
    if sms_cfg.get("country"): instagram_code.HERO_COUNTRY = sms_cfg["country"]
    if sms_cfg.get("service"): instagram_code.HERO_SERVICE = sms_cfg["service"]
    prov_cfg = sms_cfg.get("providers", {})
    hero_cfg = prov_cfg.get("herosms", {})
    if hero_cfg.get("enabled") is not None: instagram_code.HEROSMS_ENABLED = hero_cfg["enabled"]
    if hero_cfg.get("api_key"):  instagram_code.HERO_API_KEY  = hero_cfg["api_key"]
    if hero_cfg.get("service"):  instagram_code.HERO_SERVICE  = hero_cfg["service"]
    if hero_cfg.get("country"):  instagram_code.HERO_COUNTRY  = str(hero_cfg["country"])
    bower_cfg = prov_cfg.get("smsbower", {})
    if bower_cfg.get("enabled") is not None: instagram_code.SMSBOWER_ENABLED = bower_cfg["enabled"]
    if bower_cfg.get("api_key"):  instagram_code.SMSBOWER_API_KEY  = bower_cfg["api_key"]
    if bower_cfg.get("service"):  instagram_code.SMSBOWER_SERVICE  = bower_cfg["service"]
    if bower_cfg.get("country"):  instagram_code.SMSBOWER_COUNTRY  = str(bower_cfg["country"])
    smspin_cfg = prov_cfg.get("smspin", {})
    if smspin_cfg.get("enabled") is not None: instagram_code.SMSPIN_ENABLED = smspin_cfg["enabled"]
    if smspin_cfg.get("api_key"): instagram_code.SMSPIN_API_KEY = smspin_cfg["api_key"]
    sagg_cfg = prov_cfg.get("sim_aggregator", {})
    if sagg_cfg.get("enabled") is not None: instagram_code.SIM_AGGREGATOR_ENABLED = sagg_cfg["enabled"]
    if sagg_cfg.get("api_key"):   instagram_code.SIM_AGGREGATOR_API_KEY  = sagg_cfg["api_key"]
    if sagg_cfg.get("service"):   instagram_code.SIM_AGGREGATOR_SERVICE  = sagg_cfg["service"]
    if sagg_cfg.get("country"):   instagram_code.SIM_AGGREGATOR_COUNTRY  = str(sagg_cfg["country"])
    pvap_cfg = prov_cfg.get("pvapins", {})
    if pvap_cfg.get("enabled") is not None: instagram_code.PVAPINS_ENABLED = pvap_cfg["enabled"]
    if pvap_cfg.get("customer_key"): instagram_code.PVAPINS_CUSTOMER = pvap_cfg["customer_key"]
    if pvap_cfg.get("service"):      instagram_code.PVAPINS_SERVICE  = pvap_cfg["service"]
    if pvap_cfg.get("country"):      instagram_code.PVAPINS_COUNTRY  = str(pvap_cfg["country"])

    instagram_code.EMAIL        = config["email"] or instagram_code.EMAIL
    instagram_code.FIRST_NAME   = config["firstname"]
    instagram_code.BIRTH_YEAR   = config["birth_year"]
    instagram_code.HERO_COUNTRY = config.get("hero_country") or instagram_code.HERO_COUNTRY
    instagram_code.DEBUG_MODE   = (config["mode"] == "debug")
    instagram_code.set_debug_queue(debug_input_queue)

    # Marquer ce thread comme "creation" — ne touche PAS au sys.stdout global
    _thread_local.log_target = "creation"
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = LogCapture("info", target="creation")
    sys.stderr = LogCapture("error", target="creation")

    try:
        instagram_code.load_photo_folders()
    except:
        pass

    # ── Démarrer le pool scraper si pas encore actif ──────────────────
    try:
        if not instagram_code._number_pool_running:
            instagram_code.start_pool_scraper(target_size=5)
            push_log("info", "🚀 Pool scraper démarré depuis session (target=5)")
        else:
            push_log("info", "ℹ️ Pool scraper déjà actif")
    except Exception as _e:
        push_log("warn", f"⚠️ Erreur démarrage pool scraper : {_e}")

    count           = config["count"]
    delay           = config["delay"]
    rotating_host   = config.get("rotating_proxy_host", "").strip()
    rotating_url    = config.get("rotating_url", "").strip()
    rotate_wait     = config.get("rotate_wait", True)
    rotate_wait_sec = config.get("rotate_wait_sec", 3)
    bio             = config.get("bio", "")
    _session_config_store["ban_on_existing_email"] = config.get("ban_on_existing_email", False)

    simultane_cfg = config.get("simultane")

    push_log("info", f"🚀 Session démarrée — {count} compte(s), mode {config['mode']}")

    try:
        # ── MODE SIMULTANÉ ──────────────────────────────────────────────
        if simultane_cfg and simultane_cfg.get("enabled"):
            slots   = simultane_cfg.get("slots", [])
            nb_threads = simultane_cfg.get("count", 1)
            boucles = simultane_cfg.get("boucles", 1)
            total   = nb_threads * boucles

            session_state["total"] = total
            push_log("info", f"🔀 Simultané : {nb_threads} threads × {boucles} boucles = {total} comptes")

            done_lock = threading.Lock()

            def run_one(slot, account_index, _retry_count=0):
                global stop_requested
                if stop_requested:
                    return

                # Ce sous-thread hérite du contexte → forcer "creation"
                _thread_local.log_target = "creation"

                proxy_host = slot.get("host", "")
                proxy_port = slot.get("port", "")
                proxy_user = slot.get("user", "")
                proxy_pass = slot.get("pass", "")
                rotate_url_slot = slot.get("rotateUrl", "") if slot.get("type") == "rotatif" else ""

                session_state["current_account"] = f"Compte {account_index+1}/{total}"
                push_log("info", f"{'─'*40}")
                push_log("info", f"📋 [{account_index+1}/{total}] Thread démarré — proxy {proxy_host}:{proxy_port}")

                phone_id = None
                try:
                    _cm = getattr(instagram_code, 'CREATION_MODE', 'phone')
                    pre_number_result = None
                    pre_email_result  = None

                    if _cm == 'email':
                        push_log("info", f"📧 [{account_index+1}] Récupération Gmail (SMSBower)...")
                        _em_attempt = 0
                        while not stop_requested:
                            _em_attempt += 1
                            _mail, _mail_id = instagram_code.get_smsbower_email()
                            if _mail:
                                pre_email_result = (_mail, _mail_id)
                                push_log("success", f"✅ [{account_index+1}] Gmail obtenu : {_mail}")
                                break
                            push_log("info", f"⏳ Pas d'email ({_em_attempt}) — retry 10s...")
                            time.sleep(10)
                        if not pre_email_result:
                            push_log("warn", f"⛔ [{account_index+1}] Arrêt demandé pendant attente email")
                            with done_lock:
                                session_state["errors"] += 1
                                session_state["done"] += 1
                            return
                    else:
                        push_log("info", f"📱 [{account_index+1}] Récupération numéro avant démarrage...")
                        attempt = 0
                        while not stop_requested:
                            attempt += 1
                            _r = instagram_code.get_hero_number()
                            if _r:
                                _aid, _num, _prov = _r
                                _fmt = instagram_code.format_number(_num)
                                if _fmt:
                                    pre_number_result = (_aid, _fmt, _prov)
                                    push_log("success", f"✅ [{account_index+1}] Numéro : {_fmt} ({_prov})")
                                    break
                                else:
                                    push_log("warn", f"⚠️ Invalide ({_num}) — relance ({attempt})")
                            else:
                                push_log("info", f"⏳ Aucun numéro ({attempt}) — retry 5s...")
                                time.sleep(5)
                        if not pre_number_result:
                            push_log("warn", f"⛔ [{account_index+1}] Arrêt demandé pendant attente numéro")
                            with done_lock:
                                session_state["errors"] += 1
                                session_state["done"] += 1
                            return

                    with _geelark_create_lock:
                        push_log("info", f"📱 [{account_index+1}] Création profil GeeLark (verrou actif)...")
                        for _create_attempt in range(5):
                            phone_id = instagram_code.create_phone_profile(
                                "74z4xquqcn.cn.fxdx.in", "14864", "9haz01", "v51b8i"
                            )
                            if phone_id:
                                push_log("success", f"✅ [{account_index+1}] Profil créé : {phone_id}")
                                time.sleep(5)
                                break
                            push_log("warn", f"⚠️ [{account_index+1}] Tentative {_create_attempt+1}/5 échouée — attente 15s...")
                            time.sleep(15)
                    if not phone_id:
                        push_log("error", f"❌ [{account_index+1}] Échec création profil après 5 tentatives")
                        with done_lock:
                            session_state["errors"] += 1
                            session_state["done"] += 1
                        return

                    time.sleep(2)

                    # Démarrage téléphone
                    if not instagram_code.start_phone(phone_id):
                        push_log("error", f"❌ [{account_index+1}] Impossible de démarrer")
                        with done_lock:
                            session_state["errors"] += 1
                            session_state["done"] += 1
                        return

                    time.sleep(15)

                    # Rotation IP
                    if rotate_url_slot:
                        push_log("info", f"🔁 [{account_index+1}] Rotation IP...")
                        try:
                            r = req.get(rotate_url_slot, timeout=15)
                            push_log("success", f"✅ Rotation OK : {r.text.strip()[:60]}")
                        except Exception as e:
                            push_log("warn", f"⚠️ Rotation échouée : {e}")
                        time.sleep(int(config.get("rotate_wait_sec", 3)))

                    if stop_requested:
                        return

                    # ADB
                    instagram_code.enable_adb(phone_id)
                    time.sleep(3)
                    device, pwd = instagram_code.wait_for_adb(phone_id, max_wait=90)
                    if not device:
                        push_log("error", f"❌ [{account_index+1}] ADB timeout")
                        instagram_code.stop_phone(phone_id)
                        with done_lock:
                            session_state["errors"] += 1
                            session_state["done"] += 1
                        return

                    if stop_requested:
                        return

                    # glogin
                    connected = False
                    for attempt in range(10):
                        if stop_requested:
                            break
                        subprocess.run(
                            f'"{instagram_code.ADB_PATH}" connect {device}',
                            shell=True, capture_output=True
                        )
                        time.sleep(3)
                        result = subprocess.run(
                            f'"{instagram_code.ADB_PATH}" -s {device} shell glogin {pwd}',
                            shell=True, capture_output=True, text=True
                        )
                        push_log("info", f"  glogin [{attempt+1}] → {result.stdout.strip()}")
                        if "success" in result.stdout.lower():
                            connected = True
                            break

                    if not connected:
                        push_log("error", f"❌ [{account_index+1}] glogin échoué")
                        instagram_code.stop_phone(phone_id)
                        with done_lock:
                            session_state["errors"] += 1
                            session_state["done"] += 1
                        return

                    if stop_requested:
                        return

                    instagram_code.enable_data_saver(device)
                    # GPS
                    city, lat, lon = instagram_code.apply_random_city_gps(phone_id)
                    session_state["current_city"] = city
                    time.sleep(2)

                    # Photos
                    photo_folder = instagram_code.get_next_photo_folder()
                    if photo_folder:
                        instagram_code.push_photos_to_device(device, photo_folder, base_photos_dir=instagram_code.PHOTOS_BASE_DIR)

                    # ── Proxy juste avant Tinder ──────────────────────────
                    push_log("info", f"🔄 [{account_index+1}] Application proxy {proxy_host}:{proxy_port}...")
                    proxy_ok = instagram_code.change_phone_proxy(
                        phone_id, proxy_host, proxy_port, proxy_user, proxy_pass, "socks5"
                    )
                    if proxy_ok:
                        push_log("success", f"✅ [{account_index+1}] Proxy appliqué")
                    else:
                        push_log("warn", f"⚠️ [{account_index+1}] Proxy non appliqué — on continue")
                    time.sleep(5)

                    if _cm == 'email' and pre_email_result:
                        instagram_code._pre_fetched_email   = pre_email_result[0]
                        instagram_code._pre_fetched_mail_id = pre_email_result[1]
                        push_log("info", f"📧 [{account_index+1}] Gmail injecté : {pre_email_result[0]}")
                    else:
                        instagram_code._pre_fetched_number = pre_number_result
                        push_log("info", f"📱 [{account_index+1}] Numéro injecté : {pre_number_result[1] if pre_number_result else '—'}")

                    # Tinder
                    session_state["current_step"] = f"Création Tinder [{account_index+1}]"
                    push_log("info", f"📲 [{account_index+1}] Lancement Tinder...")
                    result_instagram = instagram_code.open_instagram(
                        device, None, city, lat, lon,
                        phone_id=phone_id, pwd=pwd,
                        bio=bio,
                        ban_on_existing_email=_session_config_store.get("ban_on_existing_email", False)
                    )

                    if result_instagram == "no_number_timeout":
                        push_log("warn", f"⏰ [{account_index+1}] Timeout numéro SMS — profil GeeLark déjà supprimé")
                        phone_id = None
                        with done_lock:
                            session_state["errors"] += 1
                            session_state["done"] += 1
                        return

                    if result_instagram == "email_banned":
                        push_log("warn", f"🚫 [{account_index+1}] Email existant — suppression et relance...")
                        try:
                            _unregister_phone_creation(phone_id)
                            instagram_code.delete_phone_geelark(phone_id)
                            push_log("success", f"✅ Profil supprimé")
                            phone_id = None
                        except Exception as e:
                            push_log("warn", f"⚠️ Erreur suppression : {e}")
                            phone_id = None
                        if not stop_requested and _retry_count < 10:
                            push_log("warn", f"🔄 [{account_index+1}] Relance création (tentative {_retry_count + 1}/10)...")
                            run_one(slot, account_index, _retry_count + 1)
                        else:
                            push_log("error", f"❌ [{account_index+1}] Trop de tentatives email_banned — abandon")
                            with done_lock:
                                session_state["errors"] += 1
                                session_state["done"] += 1
                        return

                    # ── Vérification que le compte est vivant avant de fermer ──
                    push_log("info", f"🔍 [{account_index+1}] Vérification du compte avant fermeture...")
                    try:
                        statut = instagram_code.check_instagram_account(device)
                        # ── Screenshot + Telegram ──────────────────────────
                        try:
                            screenshot = instagram_code.take_screenshot(device)
                            phone_label = str(phone_id) if phone_id else device
                            if statut == "banned":
                                caption = (
                                    f"🚫 <b>Compte BANNI à la création</b>\n"
                                    f"📱 Téléphone : {phone_label}\n"
                                    f"🌆 Ville : {city or 'inconnue'}"
                                )
                            else:
                                caption = (
                                    f"✅ <b>Compte VIVANT créé</b>\n"
                                    f"📱 Téléphone : {phone_label}\n"
                                    f"🌆 Ville : {city or 'inconnue'}"
                                )
                            if screenshot:
                                instagram_code.telegram_send_photo(screenshot, caption)
                            else:
                                instagram_code.telegram_send_message(caption)
                        except Exception as _te:
                            push_log("warn", f"⚠️ Telegram screenshot erreur : {_te}")
                        # ──────────────────────────────────────────────────

                        if statut == "banned":
                            push_log("warn", f"🚫 [{account_index+1}] Compte banni détecté — suppression du profil GeeLark...")
                            instagram_code.delete_phone_geelark(phone_id)
                            phone_id = None
                            with done_lock:
                                session_state["errors"] += 1
                                session_state["done"] += 1
                            return
                        else:
                            push_log("success", f"✅ [{account_index+1}] Compte vivant confirmé !")
                    except Exception as e:
                        push_log("warn", f"⚠️ [{account_index+1}] Erreur vérification : {e} — on continue quand même")

                    push_log("success", f"✅ [{account_index+1}] Compte créé !")
                    with done_lock:
                        session_state["success"] += 1
                        session_state["done"] += 1

                except InterruptedError:
                    push_log("warn", f"⛔ [{account_index+1}] Interrompu")
                    stop_requested = True
                except Exception as e:
                    import traceback
                    push_log("error", f"❌ [{account_index+1}] Exception : {e}")
                    push_log("error", traceback.format_exc().strip())
                    with done_lock:
                        session_state["errors"] += 1
                        session_state["done"] += 1
                finally:
                    if phone_id:
                        # ── Libérer le téléphone de la liste "en création" ──
                        _unregister_phone_creation(phone_id)
                        try:
                            instagram_code.stop_phone(phone_id)
                        except:
                            pass

            # ── Boucles ─────────────────────────────────────────────────
            account_idx = 0
            for boucle in range(boucles):
                if stop_requested:
                    break
                push_log("info", f"🔄 Boucle {boucle+1}/{boucles}")
                threads = []
                for slot_idx in range(nb_threads):
                    if stop_requested:
                        break
                    slot = slots[slot_idx % len(slots)]
                    t = threading.Thread(
                        target=run_one,
                        args=(slot, account_idx),
                        daemon=True
                    )
                    threads.append(t)
                    account_idx += 1

                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                if boucle < boucles - 1 and not stop_requested:
                    push_log("info", f"⏳ Pause entre boucles ({delay}s)...")
                    time.sleep(delay)

        # ── MODE NORMAL ─────────────────────────────────────────────────
        else:
            if not rotating_host:
                push_log("error", "❌ Aucun proxy configuré")
                session_state["running"] = False
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                return

            _i = 0
            while _i < count and not stop_requested:
                if stop_requested:
                    push_log("warn", "⛔ Arrêt demandé")
                    break

                i = _i
                session_state["current_account"] = f"Compte {i+1}/{count}"
                push_log("info", f"{'─'*40}")
                push_log("info", f"📋 [{i+1}/{count}] Début création compte...")

                _email_ban = False
                phone_id = None

                try:
                    parts = rotating_host.split(":")
                    if len(parts) < 4:
                        push_log("error", "❌ Format proxy invalide — host:port:user:pass")
                        session_state["errors"] += 1
                        continue
                    proxy_host, proxy_port, proxy_user, proxy_pass = parts[0], parts[1], parts[2], parts[3]

                    # ── PRÉ-RÉCUPÉRATION EMAIL OU NUMÉRO ─────────────────────
                    _cm = getattr(instagram_code, 'CREATION_MODE', 'phone')
                    pre_number_result = None
                    pre_email_result  = None

                    if _cm == 'email':
                        session_state["current_step"] = "Pré-récupération Gmail"
                        push_log("info", f"📧 [{i+1}/{count}] Récupération Gmail (SMSBower)...")
                        _em_attempt = 0
                        while not stop_requested:
                            _em_attempt += 1
                            _mail, _mail_id = instagram_code.get_smsbower_email()
                            if _mail:
                                pre_email_result = (_mail, _mail_id)
                                push_log("success", f"✅ [{i+1}] Gmail obtenu : {_mail}")
                                break
                            push_log("info", f"⏳ Pas d'email ({_em_attempt}) — retry 10s...")
                            time.sleep(10)
                        if not pre_email_result:
                            push_log("warn", f"⛔ [{i+1}] Arrêt demandé pendant attente email")
                            break
                    else:
                        session_state["current_step"] = "Pré-récupération numéro SMS"
                        push_log("info", f"📱 [{i+1}/{count}] Récupération numéro SMS avant démarrage...")
                        attempt = 0
                        while not stop_requested:
                            attempt += 1
                            _r = instagram_code.get_hero_number()
                            if _r:
                                _aid, _num, _prov = _r
                                _fmt = instagram_code.format_number(_num)
                                if _fmt:
                                    pre_number_result = (_aid, _fmt, _prov)
                                    push_log("success", f"✅ [{i+1}] Numéro pré-récupéré : {_fmt} ({_prov})")
                                    break
                                else:
                                    push_log("warn", f"⚠️ Numéro invalide ({_num}) — relance ({attempt})")
                            else:
                                push_log("info", f"⏳ Aucun numéro ({attempt}) — retry 5s...")
                                time.sleep(5)
                        if not pre_number_result:
                            push_log("warn", f"⛔ [{i+1}] Arrêt demandé pendant attente numéro")
                            break
                    # ─────────────────────────────────────────────────────────

                    # Création profil GeeLark — seulement maintenant
                    session_state["current_step"] = "Création profil GeeLark"
                    push_log("info", f"📱 Création profil GeeLark...")
                    phone_id = None
                    for _create_attempt in range(5):
                        phone_id = instagram_code.create_phone_profile(
                            "74z4xquqcn.cn.fxdx.in", "14864", "9haz01", "v51b8i"
                        )
                        if phone_id:
                            push_log("success", f"✅ Profil créé : {phone_id}")
                            time.sleep(5)
                            break
                        push_log("warn", f"⚠️ Tentative {_create_attempt+1}/5 échouée — attente 15s...")
                        time.sleep(15)
                    if not phone_id:
                        push_log("error", "❌ Échec création profil après 5 tentatives")
                        session_state["errors"] += 1
                        continue
                    session_state["current_device"] = str(phone_id)
                    # ── Marquer ce téléphone comme "en cours de création" ──
                    _register_phone_creation(phone_id)
                    time.sleep(2)

                    # Root
                    session_state["current_step"] = "Activation Root"
                    instagram_code.enable_root(phone_id)
                    time.sleep(2)

                    # Démarrage
                    session_state["current_step"] = "Démarrage téléphone"
                    started = instagram_code.start_phone(phone_id)
                    if not started:
                        push_log("error", "❌ Impossible de démarrer")
                        session_state["errors"] += 1
                        continue

                    push_log("info", "⏳ Attente boot (15s)...")
                    time.sleep(15)

                    # Rotation IP
                    if rotating_url:
                        push_log("info", "🔁 Rotation IP...")
                        session_state["current_step"] = "Rotation IP"
                        try:
                            r = req.get(rotating_url, timeout=15)
                            push_log("success", f"✅ Rotation OK : {r.text.strip()[:60]}")
                        except Exception as e:
                            push_log("warn", f"⚠️ Rotation échouée : {e}")
                        if rotate_wait and rotate_wait_sec > 0:
                            push_log("info", f"⏳ Attente {rotate_wait_sec}s...")
                            time.sleep(3)

                    if stop_requested:
                        break

                    # ADB
                    session_state["current_step"] = "Activation ADB"
                    instagram_code.enable_adb(phone_id)
                    time.sleep(3)

                    session_state["current_step"] = "Attente ADB"
                    device, pwd = instagram_code.wait_for_adb(phone_id, max_wait=90)
                    if not device:
                        push_log("error", "❌ ADB non disponible")
                        instagram_code.stop_phone(phone_id)
                        session_state["errors"] += 1
                        continue
                    session_state["current_device"] = device

                    if stop_requested:
                        break

                    # glogin
                    session_state["current_step"] = "Connexion ADB"
                    connected = False
                    for attempt in range(10):
                        if stop_requested:
                            break
                        subprocess.run(
                            f'"{instagram_code.ADB_PATH}" connect {device}',
                            shell=True, capture_output=True
                        )
                        time.sleep(3)
                        result = subprocess.run(
                            f'"{instagram_code.ADB_PATH}" -s {device} shell glogin {pwd}',
                            shell=True, capture_output=True, text=True
                        )
                        push_log("info", f"  glogin [{attempt+1}] → {result.stdout.strip()}")
                        if "success" in result.stdout.lower():
                            connected = True
                            break

                    if not connected:
                        push_log("error", "❌ glogin échoué")
                        instagram_code.stop_phone(phone_id)
                        session_state["errors"] += 1
                        continue

                    if stop_requested:
                        break

                    instagram_code.enable_data_saver(device)
                    # GPS
                    session_state["current_step"] = "GPS"
                    city, lat, lon = instagram_code.apply_random_city_gps(phone_id)
                    session_state["current_city"] = city
                    time.sleep(2)

                    # Photos
                    session_state["current_step"] = "Photos"
                    photo_folder = instagram_code.get_next_photo_folder()
                    if photo_folder:
                        instagram_code.push_photos_to_device(device, photo_folder, base_photos_dir=instagram_code.PHOTOS_BASE_DIR)

                    # Proxy
                    session_state["current_step"] = "Application proxy"
                    push_log("info", "⏳ Attente 3s avant changement proxy...")
                    time.sleep(3)
                    push_log("info", f"🔄 Application proxy {proxy_host}:{proxy_port}...")
                    proxy_ok = instagram_code.change_phone_proxy(
                        phone_id, proxy_host, proxy_port, proxy_user, proxy_pass, "socks5"
                    )
                    if proxy_ok:
                        push_log("success", "✅ Proxy appliqué")
                    else:
                        push_log("warn", "⚠️ Proxy non appliqué — on continue quand même")
                    time.sleep(3)

                    if _cm == 'email' and pre_email_result:
                        instagram_code._pre_fetched_email   = pre_email_result[0]
                        instagram_code._pre_fetched_mail_id = pre_email_result[1]
                        push_log("info", f"📧 Gmail injecté pour Tinder : {pre_email_result[0]}")
                    else:
                        instagram_code._pre_fetched_number = pre_number_result
                        push_log("info", f"📱 Numéro injecté pour Tinder : {pre_number_result[1] if pre_number_result else '—'}")

                    # Tinder
                    session_state["current_step"] = "Création compte Tinder"
                    push_log("info", "📲 Lancement Tinder...")
                    result_instagram = instagram_code.open_instagram(
                        device, None, city, lat, lon,
                        phone_id=phone_id, pwd=pwd,
                        bio=bio,
                        ban_on_existing_email=_session_config_store.get("ban_on_existing_email", False)
                    )

                    if result_instagram == "no_number_timeout":
                        push_log("warn", f"⏰ [{i+1}] Timeout numéro SMS — profil GeeLark déjà supprimé")
                        phone_id = None
                        session_state["errors"] += 1
                        continue

                    if result_instagram == "adb_timeout":
                        push_log("error", f"❌ [{i+1}] ADB indisponible après reboot numéro")
                        session_state["errors"] += 1
                        continue

                    if result_instagram == "glogin_failed":
                        push_log("error", f"❌ [{i+1}] glogin échoué après reboot numéro")
                        session_state["errors"] += 1
                        continue

                    if result_instagram == "email_banned":
                        push_log("warn", f"🚫 [{i+1}] Email existant — suppression et relance...")
                        try:
                            _unregister_phone_creation(phone_id)
                            instagram_code.delete_phone_geelark(phone_id)
                            push_log("success", f"✅ Profil supprimé — relance création...")
                            phone_id = None
                        except Exception as e:
                            push_log("warn", f"⚠️ Erreur suppression : {e}")
                            phone_id = None
                        _email_ban = True
                        continue

                    # ── Vérification que le compte est vivant avant de fermer ──
                    push_log("info", f"🔍 [{i+1}] Vérification du compte avant fermeture...")
                    try:
                        statut = instagram_code.check_instagram_account(device)

                        # ── Screenshot + Telegram ──────────────────────────
                        try:
                            screenshot = instagram_code.take_screenshot(device)
                            phone_label = str(phone_id) if phone_id else device
                            if statut == "banned":
                                caption = (
                                    f"🚫 <b>Compte BANNI à la création</b>\n"
                                    f"📱 Téléphone : {phone_label}\n"
                                    f"🌆 Ville : {city or 'inconnue'}"
                                )
                            else:
                                caption = (
                                    f"✅ <b>Compte VIVANT créé</b>\n"
                                    f"📱 Téléphone : {phone_label}\n"
                                    f"🌆 Ville : {city or 'inconnue'}"
                                )
                            if screenshot:
                                instagram_code.telegram_send_photo(screenshot, caption)
                            else:
                                instagram_code.telegram_send_message(caption)
                        except Exception as _te:
                            push_log("warn", f"⚠️ Telegram screenshot erreur : {_te}")
                        # ──────────────────────────────────────────────────

                        if statut == "banned":
                            push_log("warn", f"🚫 [{i+1}] Compte banni détecté — suppression du profil GeeLark...")
                            instagram_code.delete_phone_geelark(phone_id)
                            phone_id = None
                            session_state["errors"] += 1
                            continue
                        else:
                            push_log("success", f"✅ [{i+1}] Compte vivant confirmé !")
                    except Exception as e:
                        push_log("warn", f"⚠️ [{i+1}] Erreur vérification : {e} — on continue quand même")

                    push_log("success", f"✅ Compte {i+1} créé !")
                    session_state["success"] += 1

                except InterruptedError:
                    push_log("warn", f"⛔ Interrompu (compte {i+1})")
                    stop_requested = True
                    break
                except Exception as e:
                    import traceback
                    push_log("error", f"❌ Exception compte {i+1} : {e}")
                    push_log("error", traceback.format_exc().strip())
                    session_state["errors"] += 1
                finally:
                    if not _email_ban:
                        _i += 1
                    session_state["done"] = _i
                    if phone_id:
                        _unregister_phone_creation(phone_id)
                    if phone_id and config["mode"] != "debug" and not stop_requested:
                        try:
                            instagram_code.stop_phone(phone_id)
                        except:
                            pass

                if not _email_ban and _i < count and delay > 0 and not stop_requested:
                    push_log("info", f"⏳ Pause {delay}s...")
                    time.sleep(delay)

        push_log("success",
                 f"🎉 Session terminée — {session_state['success']}/{session_state['done']} succès")

    except Exception as e:
        import traceback
        push_log("error", f"❌ Erreur fatale run_session : {e}")
        push_log("error", traceback.format_exc().strip())

    finally:
        session_state["running"] = False
        session_state["current_step"] = "Terminé"
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        _thread_local.log_target = None
        push_log("info", "🔒 Session libérée — prête pour relance")



# ── Stop session création ─────────────────────────────────────────────────────
@app.route("/api/session/stop", methods=["POST"])
def stop_session():
    global stop_requested, session_state, swipe_state, swipe_stop_flag

    _, req_username = _get_user_from_request()
    _thread_local.username = req_username

    # Stopper uniquement les sessions de l'utilisateur demandeur
    with _sessions_lock:
        for s in active_sessions:
            if s.get("username") == req_username:
                if s.get("stop_flag"):
                    s["stop_flag"][0] = True
                s["running"] = False

    # Mettre à jour le state global seulement si plus aucune session active pour cet user
    with _sessions_lock:
        user_still_running = any(
            s for s in active_sessions
            if s.get("running") and s.get("username") == req_username
        )
    if not user_still_running:
        session_state["running"] = False
        session_state["current_step"] = "Arrêté"
        session_state["current_account"] = None
        session_state["current_device"] = None

    debug_input_queue.put("stop")

    push_log("warn", f"⚠️ Arrêt demandé par {req_username} — en cours...")

    if INSTAGRAM_CODE_LOADED:
        try:
            result = instagram_code.geelark_request("POST", "/open/v1/phone/list", {
                "page": 1, "pageSize": 100
            })
            if not isinstance(result, dict):
                raise ValueError(f"Réponse GeeLark invalide : {result}")
            phones = result.get("data", {}).get("items", [])
            running_ids = [str(p["id"]) for p in phones if p.get("status") in (0, 1)]
            if running_ids:
                push_log("info", f"⏹ Arrêt de {len(running_ids)} téléphone(s)...")
                for pid in running_ids:
                    try:
                        instagram_code.stop_phone(pid)
                        push_log("info", f"  ✅ {pid} arrêté")
                    except Exception as e:
                        push_log("warn", f"  ⚠️ Erreur {pid} : {e}")
        except Exception as e:
            push_log("error", f"❌ Erreur arrêt téléphones : {e}")

    push_log("success", "✅ Session arrêtée")

    return jsonify({"success": True})

# ── Swipe session ─────────────────────────────────────────────────────────────
@app.route("/api/swipe/status")
def swipe_status():
    return jsonify(swipe_state)

@app.route("/api/swipe/launch", methods=["POST"])
def launch_swipe():
    global swipe_stop_flag
    if swipe_state["running"]:
        return jsonify({"error": "Session swipe déjà en cours"}), 400
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé"}), 500

    _, req_username = _get_user_from_request()

    data        = request.json
    phone_ids   = data.get("phone_ids", [])
    proxy_idx   = data.get("proxy_idx", 0)
    swipe_count = int(data.get("swipe_count", 50))
    like_ratio  = float(data.get("like_ratio", 0.85))
    delay_min   = float(data.get("delay_min", 0.9))
    delay_max   = float(data.get("delay_max", 2.8))
    force_match = bool(data.get("force_match", False))
    fm_loop = bool(data.get("fm_loop", False))
    fm_proxies = data.get("fm_proxies", None)
    fm_proxy_mode = data.get("fm_proxy_mode", "none")

    if not phone_ids:
        return jsonify({"error": "Aucun téléphone sélectionné"}), 400

    swipe_proxies = load_swipe_proxies()
    if swipe_proxies:
        proxy = swipe_proxies[proxy_idx % len(swipe_proxies)]
    elif fm_proxies and fm_proxy_mode != "none":
        # Giga/rotatif mode — use first assigned proxy as fallback
        first_fp = next(iter(fm_proxies.values()))
        proxy = {
            "host": first_fp.get("host", ""),
            "port": first_fp.get("port", ""),
            "user": first_fp.get("user", ""),
            "pass": first_fp.get("pass", ""),
            "type": "socks5",
        }
    else:
        return jsonify({"error": "Aucun proxy swipe configuré"}), 400

    rotate_url      = data.get("rotate_url", "").strip()
    rotate_wait_sec = int(data.get("rotate_wait_sec", 15))
    swipe_stop_flag = [False]
    swipe_state.update({
        "running": True, "done": 0,
        "total": len(phone_ids) * swipe_count,
        "liked": 0, "noped": 0,
        "current_phone": None, "current_step": None,
        "mode": "Force Match 💘" if force_match else "Swipe Normal",
    })

    t = threading.Thread(
        target=_run_swipe_session_thread,
        args=(phone_ids, proxy, swipe_count, like_ratio, delay_min, delay_max,
              rotate_url, rotate_wait_sec, force_match, fm_loop, fm_proxies, fm_proxy_mode,
              req_username),
        daemon=True,
    )
    t.start()
    return jsonify({"success": True, "phones": len(phone_ids), "proxy": proxy.get("host"), "mode": "Force Match 💘" if force_match else "Swipe Normal"})

def _run_swipe_session_thread(phone_ids, proxy, swipe_count,
                               like_ratio, delay_min, delay_max,
                               rotate_url="", rotate_wait_sec=3,
                               force_match=False, fm_loop=False,
                               fm_proxies=None, fm_proxy_mode="none",
                               username=None):

    _thread_local.log_target = "swipe"
    _thread_local.username = username

    # Appliquer les credentials GeeLark de l'utilisateur
    _saved_geelark = None
    if username and INSTAGRAM_CODE_LOADED:
        _saved_geelark = (
            instagram_code.GEELARK_APP_ID,
            instagram_code.GEELARK_API_KEY,
            instagram_code.GEELARK_BEARER,
        )
        _apply_user_config(username)

    old_stdout = sys.stdout
    old_stderr = sys.stderr
    sys.stdout = LogCapture("info", target="swipe")
    sys.stderr = LogCapture("error", target="swipe")

    try:
        total_liked = 0
        total_noped = 0

        banned_ids = set()
        last_swipped_id = None
        boucle_num = 1

        # APRÈS
        def get_ordered_ids():
            selected_ids = {str(p) for p in phone_ids}
            try:
                data = instagram_code.geelark_request(
                    "POST", "/open/v1/phone/list",
                    {"page": 1, "pageSize": 100}
                )
                live_phones = data.get("data", {}).get("items", [])
            except Exception as e:
                push_log_swipe("warn", f"⚠️ Impossible de rafraîchir la liste GeeLark : {e}")
                live_phones = []

            live_map = {str(p["id"]): p for p in live_phones}
            all_known = list(live_map.keys())
            all_known.reverse()

            in_creation = _get_phones_in_creation()

            # En boucle infinie : on prend TOUS les tels GeeLark du groupe tinder
            # qui ne sont pas bannis/en création, pas seulement ceux sélectionnés au départ
            if fm_loop:
                result = []
                for pid in all_known:
                    if pid in banned_ids:
                        continue
                    if pid in in_creation:
                        continue
                    # Filtrer uniquement le groupe tinder
                    phone_info = live_map.get(pid, {})
                    group = str(phone_info.get("profileGroup") or phone_info.get("groupName") or "").lower()
                    if group != "instagram":
                        continue
                    result.append(pid)
            else:
                result = []
                for pid in all_known:
                    if pid not in selected_ids:
                        continue
                    if pid in banned_ids:
                        continue
                    if pid in in_creation:
                        continue
                    result.append(pid)

                if not result:
                    result = [p for p in phone_ids
                            if str(p) not in banned_ids
                            and str(p) not in in_creation]

            return result

        while True:
            if swipe_stop_flag[0]:
                push_log_swipe("warn", "⛔ Swipe — arrêt demandé")
                break

            ordered_ids = get_ordered_ids()

            if not ordered_ids:
                push_log_swipe("warn", "⚠️ Plus aucun téléphone disponible — arrêt")
                break

            if last_swipped_id is not None and last_swipped_id in ordered_ids:
                last_pos = ordered_ids.index(last_swipped_id)
                start_idx = (last_pos + 1) % len(ordered_ids)
            else:
                start_idx = 0

            run_order = ordered_ids[start_idx:] + ordered_ids[:start_idx]

            mode_label = "Force Match 💘" if force_match else "Swipe Normal"
            loop_label = f" — Boucle {boucle_num}" if fm_loop else ""
            push_log_swipe("info", f"{'═'*45}")
            push_log_swipe("info", f"🔄 {mode_label}{loop_label} — {len(run_order)} téléphone(s)")
            push_log_swipe("info", f"📋 Ordre : {' → '.join(str(p) for p in run_order)}")
            if last_swipped_id:
                push_log_swipe("info", f"📍 Départ depuis le suivant de {last_swipped_id} → {run_order[0]}")
            push_log_swipe("info", f"{'═'*45}")

            # ── Swipe simultané si force_match avec plusieurs téléphones ──────────────
            use_parallel = force_match and len(run_order) > 1 and fm_proxy_mode in ("giga", "rotatif")

            if use_parallel:
                import threading as _th
                stats_lock = _th.Lock()
                
                def run_one_phone(pos, phone_id):
                    nonlocal total_liked, total_noped
                    if swipe_stop_flag[0]:
                        return
                    if str(phone_id) in _get_phones_in_creation():
                        push_log_swipe("warn", f"⏭ [{pos+1}/{len(run_order)}] {phone_id} en cours de création — skip")
                        return

                    swipe_state["current_phone"] = str(phone_id)
                    push_log_swipe("info", f"\n📱 [{pos+1}/{len(run_order)}] Téléphone {phone_id} (thread parallèle)")

                    # ── Déterminer le proxy pour ce téléphone ─────────────────────
                    swipe_proxy_for_phone = proxy
                    rotate_url_for_phone = rotate_url

                    if force_match and fm_proxies and str(phone_id) in fm_proxies:
                        fp = fm_proxies[str(phone_id)]
                        swipe_proxy_for_phone = {
                            "host": fp.get("host", ""),
                            "port": fp.get("port", ""),
                            "user": fp.get("user", ""),
                            "pass": fp.get("pass", ""),
                            "type": "socks5",
                        }
                        push_log_swipe("info", f"🔌 [{phone_id}] Proxy {fm_proxy_mode} : {fp.get('host')}:{fp.get('port')}")
                        if fm_proxy_mode == "rotatif" and fp.get("rotateUrl"):
                            rotate_url_for_phone = fp["rotateUrl"]
                        else:
                            rotate_url_for_phone = rotate_url

                    res = instagram_code.run_swipe_session(
                        phone_id=str(phone_id),
                        swipe_proxy=swipe_proxy_for_phone,
                        swipe_count=swipe_count,
                        like_ratio=like_ratio,
                        delay_min=delay_min,
                        delay_max=delay_max,
                        stop_flag=swipe_stop_flag,
                        rotate_url=rotate_url_for_phone,
                        rotate_wait_sec=rotate_wait_sec,
                        force_match=force_match,
                    )

                    with stats_lock:
                        swipe_state["done"] += swipe_count
                        if res.get("success"):
                            total_liked += res.get("liked", 0)
                            total_noped += res.get("noped", 0)
                            swipe_state["liked"] = total_liked
                            swipe_state["noped"] = total_noped
                            push_log_swipe("success", f"✅ {phone_id} — ❤️ {res['liked']} likes | 👎 {res['noped']} nopes")
                        elif res.get("reason") == "banned":
                            total_liked += res.get("liked", 0)
                            total_noped += res.get("noped", 0)
                            swipe_state["liked"] = total_liked
                            swipe_state["noped"] = total_noped
                            push_log_swipe("warn", f"🚫 {phone_id} — BANNI & supprimé")
                            banned_ids.add(str(phone_id))
                        else:
                            reason = res.get('reason', 'inconnu')
                            emoji = "📡" if reason in ("adb_timeout", "glogin_failed") else "❌"
                            push_log_swipe("error", f"{emoji} {phone_id} — échec ({reason}) — le téléphone a tenté une relance automatique")

                threads = []
                for pos, phone_id in enumerate(run_order):
                    if swipe_stop_flag[0]:
                        break
                    if str(phone_id) in _get_phones_in_creation():
                        continue
                    t = _th.Thread(target=run_one_phone, args=(pos, phone_id), daemon=True)
                    threads.append(t)

                push_log_swipe("info", f"🚀 Lancement de {len(threads)} thread(s) en parallèle...")
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

            else:
                # ── Mode séquentiel (non force_match ou 1 seul téléphone) ─────────
                for pos, phone_id in enumerate(run_order):
                    if swipe_stop_flag[0]:
                        break

                    if str(phone_id) in _get_phones_in_creation():
                        push_log_swipe("warn", f"⏭ [{pos+1}/{len(run_order)}] {phone_id} en cours de création — skip")
                        continue

                    swipe_state["current_phone"] = str(phone_id)
                    swipe_state["current_step"] = (
                        f"{'Boucle ' + str(boucle_num) + ' — ' if fm_loop else ''}"
                        f"Téléphone {pos+1}/{len(run_order)}"
                    )

                    push_log_swipe("info", f"\n📱 [{pos+1}/{len(run_order)}] Téléphone {phone_id}")

                    # ── Vérification périodique (Force Match, toutes les 5 boucles) ─
                    if force_match and boucle_num > 1 and boucle_num % 5 == 1:
                        push_log_swipe("info", f"🔍 Boucle {boucle_num} — vérification statut {phone_id}...")
                        try:
                            instagram_code.start_phone(phone_id)
                            time.sleep(8)
                            _device, _pwd = instagram_code.wait_for_adb(phone_id, max_wait=40)
                            if _device and _pwd:
                                import subprocess as _sp
                                for _attempt in range(5):
                                    _sp.run(f'"{instagram_code.ADB_PATH}" connect {_device}', shell=True, capture_output=True)
                                    time.sleep(2)
                                    _r = _sp.run(f'"{instagram_code.ADB_PATH}" -s {_device} shell glogin {_pwd}', shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
                                    if "success" in _r.stdout.lower():
                                        break
                                _statut = instagram_code.check_instagram_account(_device)
                                if _statut == "banned":
                                    push_log_swipe("warn", f"🚫 {phone_id} BANNI à la boucle {boucle_num} — suppression...")
                                    instagram_code.delete_phone_geelark(phone_id)
                                    banned_ids.add(str(phone_id))
                                    continue
                                else:
                                    push_log_swipe("success", f"✅ {phone_id} vivant à la boucle {boucle_num}")
                                    instagram_code.stop_phone(phone_id)
                                    time.sleep(3)
                            else:
                                push_log_swipe("warn", f"⚠️ ADB indisponible pour vérif {phone_id} — on continue")
                                instagram_code.stop_phone(phone_id)
                        except Exception as _e:
                            push_log_swipe("warn", f"⚠️ Erreur vérif boucle {boucle_num} : {_e} — on continue")

                    # ── Déterminer le proxy pour ce téléphone ─────────────────────
                    swipe_proxy_for_phone = proxy
                    rotate_url_for_phone = rotate_url

                    if force_match and fm_proxies and str(phone_id) in fm_proxies:
                        fp = fm_proxies[str(phone_id)]
                        swipe_proxy_for_phone = {
                            "host": fp.get("host", ""),
                            "port": fp.get("port", ""),
                            "user": fp.get("user", ""),
                            "pass": fp.get("pass", ""),
                            "type": "socks5",
                        }
                        push_log_swipe("info", f"🔌 [{phone_id}] Proxy {fm_proxy_mode} : {fp.get('host')}:{fp.get('port')}")
                        if fm_proxy_mode == "rotatif" and fp.get("rotateUrl"):
                            rotate_url_for_phone = fp["rotateUrl"]
                        else:
                            rotate_url_for_phone = rotate_url

                    # ── Swipe ─────────────────────────────────────────────────────
                    res = instagram_code.run_swipe_session(
                        phone_id=str(phone_id),
                        swipe_proxy=swipe_proxy_for_phone,
                        swipe_count=swipe_count,
                        like_ratio=like_ratio,
                        delay_min=delay_min,
                        delay_max=delay_max,
                        stop_flag=swipe_stop_flag,
                        rotate_url=rotate_url_for_phone,
                        rotate_wait_sec=rotate_wait_sec,
                        force_match=force_match,
                    )

                    swipe_state["done"] += swipe_count
                    swipe_state["liked"] = total_liked + res.get("liked", 0)
                    swipe_state["noped"] = total_noped + res.get("noped", 0)

                    if res.get("success"):
                        total_liked += res.get("liked", 0)
                        total_noped += res.get("noped", 0)
                        push_log_swipe("success", f"✅ {phone_id} — ❤️ {res['liked']} likes | 👎 {res['noped']} nopes")
                        last_swipped_id = str(phone_id)
                    elif res.get("reason") == "banned":
                        total_liked += res.get("liked", 0)
                        total_noped += res.get("noped", 0)
                        push_log_swipe("warn", f"🚫 {phone_id} — BANNI & supprimé")
                        banned_ids.add(str(phone_id))
                    else:
                        push_log_swipe("error", f"❌ {phone_id} — échec : {res.get('reason')}")
                        last_swipped_id = str(phone_id)

                    if not swipe_stop_flag[0] and pos < len(run_order) - 1:
                        time.sleep(5)

            push_log_swipe("success",
                f"✅ {'Boucle ' + str(boucle_num) + ' terminée' if fm_loop else 'Session terminée'} — "
                f"❤️ {total_liked} likes | 👎 {total_noped} nopes")
            if last_swipped_id:
                push_log_swipe("info",
                    f"📍 Curseur mémorisé sur {last_swipped_id} → "
                    f"la prochaine boucle commencera par le suivant")

            if not fm_loop or swipe_stop_flag[0]:
                break

            # ── En boucle infinie, renouveler les proxies Giga ────────────────
            if fm_loop and fm_proxy_mode == "giga" and fm_proxies:
                push_log_swipe("info",
                    f"🔄 Boucle {boucle_num} — renouvellement proxies Giga depuis la bibliothèque...")
                push_log_swipe("warn",
                    f"⚠️ Les proxies Giga doivent être renouvelés manuellement depuis le panel — "
                    f"les proxies actuels sont réutilisés pour cette boucle")

            boucle_num += 1
            push_log_swipe("info", f"⏳ Pause 5s avant la boucle {boucle_num}...")
            time.sleep(5)

        push_log_swipe("success",
            f"🎉 Terminé — ❤️ {total_liked} likes total | 👎 {total_noped} nopes total")

    except Exception as e:
        import traceback
        push_log_swipe("error", f"❌ Exception swipe : {e}")
        push_log_swipe("error", traceback.format_exc().strip())
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        _thread_local.log_target = None
        if _saved_geelark is not None and INSTAGRAM_CODE_LOADED:
            instagram_code.GEELARK_APP_ID, instagram_code.GEELARK_API_KEY, instagram_code.GEELARK_BEARER = _saved_geelark
        swipe_state["running"] = False
        swipe_state["current_step"] = "Terminé"
        swipe_state.pop("loop_start_idx", None)


@app.route("/api/swipe/stop", methods=["POST"])
def stop_swipe():
    global swipe_stop_flag
    swipe_stop_flag[0] = True
    swipe_state["running"] = False
    swipe_state["current_step"] = "Arrêté"
    push_log_swipe("warn", "⛔ Arrêt session swipe demandé")
    return jsonify({"success": True})

# ── Proxy rotation test ───────────────────────────────────────────────────────
@app.route("/api/proxy/rotate", methods=["POST"])
def proxy_rotate():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"success": False, "error": "URL manquante"}), 400
    try:
        r = req.get(url, timeout=15)
        return jsonify({"success": True, "response": r.text.strip()[:200], "status": r.status_code})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

# ── Instagram actions ─────────────────────────────────────────────────────────
@app.route("/api/instagram/warmup", methods=["POST"])
def warmup_account():
    phone_id         = request.form.get("phone_id")
    duration_minutes = int(request.form.get("duration_minutes", 15))
    usernames        = request.form.getlist("usernames")
    if not phone_id or not usernames:
        return jsonify({"error": "phone_id et usernames requis"}), 400
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé"}), 500
    def run():
        _thread_local.log_target = "swipe"
        _acquire_phone_start_slot()
        old_out = sys.stdout; old_err = sys.stderr
        sys.stdout = LogCapture("info", target="swipe")
        sys.stderr = LogCapture("error", target="swipe")
        try:
            push_log_swipe("info", f"⚡ [{phone_id}] Warmup démarré — {duration_minutes} min")
            ok = instagram_code.warmup_account_on_device(phone_id, duration_minutes, usernames)
            if ok:
                record_warmup(phone_id)
                push_log_swipe("success", f"✅ [{phone_id}] Warmup terminé")
            else:
                push_log_swipe("error", f"❌ [{phone_id}] Warmup échoué")
        except Exception as e:
            import traceback
            push_log_swipe("error", f"❌ [{phone_id}] Exception warmup : {e}")
            push_log_swipe("error", traceback.format_exc().strip())
        finally:
            sys.stdout = old_out; sys.stderr = old_err
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True})

@app.route("/api/instagram/highlight_registry", methods=["GET"])
def get_highlight_registry():
    highlight_name = request.args.get("highlight_name", "tuto 1")
    phone_ids_raw  = request.args.get("phone_ids", "")
    phone_ids      = [p.strip() for p in phone_ids_raw.split(",") if p.strip()]
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"statuses": []})
    try:
        from insta_core import _has_highlight
    except Exception:
        try:
            _has_highlight = instagram_code._has_highlight
        except Exception:
            return jsonify({"statuses": [], "error": "registry non disponible"})
    statuses = [{"phone_id": pid, "highlight_name": highlight_name, "created": _has_highlight(pid, highlight_name)} for pid in phone_ids]
    return jsonify({"statuses": statuses, "highlight_name": highlight_name})

@app.route("/api/instagram/post_story", methods=["POST"])
def post_story():
    import tempfile as _tempfile
    file             = request.files.get("media")
    phone_ids        = request.form.getlist("phone_ids")
    add_to_highlight = request.form.get("add_to_highlight", "false").lower() == "true"
    highlight_name   = request.form.get("highlight_name", "tuto 1").strip()
    if not file or not phone_ids:
        return jsonify({"error": "Média ou téléphones manquants"}), 400
    suffix = os.path.splitext(file.filename)[1]
    tmp = _tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    file.save(tmp.name); tmp.close(); tmp_path = tmp.name
    def run():
        _thread_local.log_target = "swipe"
        old_out = sys.stdout; old_err = sys.stderr
        sys.stdout = LogCapture("info", target="swipe")
        sys.stderr = LogCapture("error", target="swipe")
        try:
            hl_label = f" + à la une '{highlight_name}'" if add_to_highlight else ""
            def run_phone_story(pid):
                _thread_local.log_target = "swipe"
                _acquire_phone_start_slot()
                allowed, reason = check_posting_limit(pid, "story")
                if not allowed:
                    push_log_swipe("warn", reason)
                    return
                push_log_swipe("info", f"📱 [Story{hl_label}] Lancement téléphone {pid}...")
                try:
                    ok = instagram_code.post_story_on_device(pid, tmp_path, add_to_highlight=add_to_highlight, highlight_name=highlight_name)
                    if ok:
                        record_post_action(pid, "story")
                        push_log_swipe("success", f"✅ Story postée sur {pid}{hl_label}")
                    else:
                        push_log_swipe("error", f"❌ Échec story sur {pid}")
                except Exception as e:
                    import traceback
                    push_log_swipe("error", f"❌ Exception {pid} : {e}")
                    push_log_swipe("error", traceback.format_exc().strip())
                finally:
                    with _geelark_urls_lock:
                        _active_geelark_urls.pop(str(pid), None)
            phone_threads = [threading.Thread(target=run_phone_story, args=(pid,), daemon=True) for pid in phone_ids]
            for t in phone_threads: t.start()
            for t in phone_threads: t.join()
        finally:
            try: os.unlink(tmp_path)
            except Exception: pass
            sys.stdout = old_out; sys.stderr = old_err
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True, "phones": len(phone_ids), "highlight": highlight_name if add_to_highlight else None})

@app.route("/api/instagram/post_reel", methods=["POST"])
def post_reel():
    import tempfile as _tempfile, shutil as _sh
    files      = request.files.getlist("medias")
    phone_ids  = request.form.getlist("phone_ids")
    folder_id  = request.form.get("folder_id", "").strip()
    quick_file = request.form.get("quick_file", "").strip()
    if not phone_ids:
        return jsonify({"error": "Téléphones manquants"}), 400
    username = _mlib_username()
    # phone_tmp_map : {pid: [tmp_path, ...]} — chaque téléphone a ses propres copies
    phone_tmp_map   = {}
    originals_used  = []   # fichiers originaux à supprimer après les pushes
    _cleanup_folder = None
    _cleanup_quick  = None

    if folder_id:
        base = _mlib_folder_base(username, "reels")
        fdir = os.path.join(base, folder_id)
        if not os.path.abspath(fdir).startswith(os.path.abspath(base)) or not os.path.isdir(fdir):
            return jsonify({"error": "Dossier introuvable"}), 404
        folder_files = [
            os.path.join(fdir, fn) for fn in os.listdir(fdir)
            if os.path.isfile(os.path.join(fdir, fn)) and not fn.startswith(".")
        ]
        if not folder_files:
            return jsonify({"error": "Dossier vide"}), 400
        import random as _random
        _random.shuffle(folder_files)  # ordre totalement aléatoire
        for i, pid in enumerate(phone_ids):
            fp = folder_files[i % len(folder_files)]
            sfx = os.path.splitext(fp)[1]
            tmp = _tempfile.NamedTemporaryFile(delete=False, suffix=sfx)
            _sh.copy2(fp, tmp.name); tmp.close()
            phone_tmp_map[pid] = [tmp.name]
        # Marquer les originaux uniques pour suppression après push
        originals_used = folder_files[:len(phone_ids)]
        _cleanup_folder = (base, folder_id)
    elif quick_file:
        qdir = _mlib_quick_base(username, "reel")
        # Supporte plusieurs quick_file (un par téléphone)
        quick_files_list = request.form.getlist('quick_file')
        if not quick_files_list:
            quick_files_list = [quick_file]
        _cleanup_quick_list = []
        for i, pid in enumerate(phone_ids):
            qf_name = quick_files_list[i % len(quick_files_list)]
            qpath = os.path.join(qdir, _wsecure(qf_name))
            if not os.path.abspath(qpath).startswith(os.path.abspath(qdir)) or not os.path.isfile(qpath):
                return jsonify({"error": f"Fichier rapide introuvable : {qf_name}"}), 404
            sfx = os.path.splitext(qpath)[1]
            tmp = _tempfile.NamedTemporaryFile(delete=False, suffix=sfx)
            _sh.copy2(qpath, tmp.name); tmp.close()
            phone_tmp_map[pid] = [tmp.name]
            if qpath not in _cleanup_quick_list:
                _cleanup_quick_list.append(qpath)
        _cleanup_quick = _cleanup_quick_list
    elif files:
        # Fichiers uploadés : chaque téléphone reçoit ses propres copies
        uploaded = []
        for f in files[:3]:
            suffix = os.path.splitext(f.filename)[1]
            tmp = _tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            f.save(tmp.name); tmp.close(); uploaded.append(tmp.name)
        for pid in phone_ids:
            copies = []
            for src in uploaded:
                sfx = os.path.splitext(src)[1]
                c = _tempfile.NamedTemporaryFile(delete=False, suffix=sfx)
                _sh.copy2(src, c.name); c.close(); copies.append(c.name)
            phone_tmp_map[pid] = copies
        originals_used = uploaded  # supprimer les uploads sources après
    else:
        return jsonify({"error": "Médias ou téléphones manquants"}), 400

    def run():
        _thread_local.log_target = "swipe"
        old_out = sys.stdout; old_err = sys.stderr
        sys.stdout = LogCapture("info", target="swipe")
        sys.stderr = LogCapture("error", target="swipe")
        try:
            def run_phone_reel(pid):
                _thread_local.log_target = "swipe"
                _acquire_phone_start_slot()
                if REQUIRE_WARMUP and not has_warmup(pid):
                    push_log_swipe("warn", f"🚫 [{pid}] Aucun warmup effectué — post reel bloqué")
                    return
                allowed, reason = check_posting_limit(pid, "reel")
                if not allowed:
                    push_log_swipe("warn", reason)
                    return
                push_log_swipe("info", f"📱 [Reel] Lancement téléphone {pid}...")
                my_paths = phone_tmp_map.get(pid, [])
                try:
                    ok = instagram_code.post_reel_on_device(pid, my_paths)
                    if ok:
                        record_post_action(pid, "reel")
                        push_log_swipe("success", f"✅ Reel posté sur {pid}")
                    else:
                        push_log_swipe("error", f"❌ Échec reel sur {pid}")
                except Exception as e:
                    import traceback
                    push_log_swipe("error", f"❌ Exception {pid} : {e}")
                    push_log_swipe("error", traceback.format_exc().strip())
                finally:
                    with _geelark_urls_lock:
                        _active_geelark_urls.pop(str(pid), None)
                    # Supprimer la copie tmp de ce téléphone dès qu'il a fini
                    for p in my_paths:
                        try: os.unlink(p)
                        except Exception: pass
            phone_threads = [threading.Thread(target=run_phone_reel, args=(pid,), daemon=True) for pid in phone_ids]
            for t in phone_threads: t.start()
            for t in phone_threads: t.join()
        finally:
            # Supprimer les originaux utilisés (fichiers dossier / quick file / uploads sources)
            for orig in originals_used:
                try: os.unlink(orig)
                except Exception: pass
            if _cleanup_folder:
                # Supprimer le dossier seulement s'il est vide après les suppressions
                try:
                    fdir_check = os.path.join(_cleanup_folder[0], _cleanup_folder[1])
                    remaining = [f for f in os.listdir(fdir_check) if not f.startswith('.')]
                    if not remaining:
                        _mlib_delete_folder(_cleanup_folder[0], _cleanup_folder[1])
                except Exception: pass
            if _cleanup_quick:
                for _qp in (_cleanup_quick if isinstance(_cleanup_quick, list) else [_cleanup_quick]):
                    try: os.unlink(_qp)
                    except Exception: pass
            sys.stdout = old_out; sys.stderr = old_err
    threading.Thread(target=run, daemon=True).start()
    total_media = len(next(iter(phone_tmp_map.values()), []))
    return jsonify({"success": True, "phones": len(phone_ids), "medias": total_media})

@app.route("/api/instagram/post_feed", methods=["POST"])
def post_feed():
    import tempfile as _tempfile, shutil as _sh
    files      = request.files.getlist("medias")
    phone_ids  = request.form.getlist("phone_ids")
    folder_id  = request.form.get("folder_id", "").strip()
    quick_file = request.form.get("quick_file", "").strip()
    if not phone_ids:
        return jsonify({"error": "Téléphones manquants"}), 400
    username = _mlib_username()
    tmp_paths = []
    _cleanup_folder = None
    _cleanup_quick  = None
    if folder_id:
        base = _mlib_folder_base(username, "posts")
        fdir = os.path.join(base, folder_id)
        if not os.path.abspath(fdir).startswith(os.path.abspath(base)) or not os.path.isdir(fdir):
            return jsonify({"error": "Dossier introuvable"}), 404
        for fn in sorted(os.listdir(fdir)):
            fp = os.path.join(fdir, fn)
            if os.path.isfile(fp) and not fn.startswith("."):
                sfx = os.path.splitext(fn)[1]
                tmp = _tempfile.NamedTemporaryFile(delete=False, suffix=sfx)
                _sh.copy2(fp, tmp.name); tmp.close(); tmp_paths.append(tmp.name)
        if not tmp_paths:
            return jsonify({"error": "Dossier vide"}), 400
        _cleanup_folder = (base, folder_id)
    elif quick_file:
        qdir = _mlib_quick_base(username, "post")
        qpath = os.path.join(qdir, _wsecure(quick_file))
        if not os.path.abspath(qpath).startswith(os.path.abspath(qdir)) or not os.path.isfile(qpath):
            return jsonify({"error": "Fichier rapide introuvable"}), 404
        sfx = os.path.splitext(qpath)[1]
        tmp = _tempfile.NamedTemporaryFile(delete=False, suffix=sfx)
        _sh.copy2(qpath, tmp.name); tmp.close(); tmp_paths = [tmp.name]
        _cleanup_quick = qpath
    elif files:
        for f in files[:3]:
            suffix = os.path.splitext(f.filename)[1]
            tmp = _tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            f.save(tmp.name); tmp.close(); tmp_paths.append(tmp.name)
    else:
        return jsonify({"error": "Médias ou téléphones manquants"}), 400
    def run():
        _thread_local.log_target = "swipe"
        old_out = sys.stdout; old_err = sys.stderr
        sys.stdout = LogCapture("info", target="swipe")
        sys.stderr = LogCapture("error", target="swipe")
        try:
            def run_phone_feed(pid):
                _thread_local.log_target = "swipe"
                _acquire_phone_start_slot()
                if REQUIRE_WARMUP and not has_warmup(pid):
                    push_log_swipe("warn", f"🚫 [{pid}] Aucun warmup effectué — post feed bloqué")
                    return
                allowed, reason = check_posting_limit(pid, "post")
                if not allowed:
                    push_log_swipe("warn", reason)
                    return
                push_log_swipe("info", f"📱 [Post Feed] Lancement téléphone {pid}...")
                try:
                    ok = instagram_code.post_feed_on_device(pid, tmp_paths)
                    if ok:
                        record_post_action(pid, "post")
                        push_log_swipe("success", f"✅ Post Feed publié sur {pid}")
                    else:
                        push_log_swipe("error", f"❌ Échec Post Feed sur {pid}")
                except Exception as e:
                    import traceback
                    push_log_swipe("error", f"❌ Exception {pid} : {e}")
                    push_log_swipe("error", traceback.format_exc().strip())
                finally:
                    with _geelark_urls_lock:
                        _active_geelark_urls.pop(str(pid), None)
            phone_threads = [threading.Thread(target=run_phone_feed, args=(pid,), daemon=True) for pid in phone_ids]
            for t in phone_threads: t.start()
            for t in phone_threads: t.join()
        finally:
            for p in tmp_paths:
                try: os.unlink(p)
                except Exception: pass
            if _cleanup_folder:
                try: _mlib_delete_folder(_cleanup_folder[0], _cleanup_folder[1])
                except Exception: pass
            if _cleanup_quick:
                try: os.unlink(_cleanup_quick)
                except Exception: pass
            sys.stdout = old_out; sys.stderr = old_err
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True, "phones": len(phone_ids), "medias": len(tmp_paths)})

@app.route("/api/instagram/add_link", methods=["POST"])
def add_link():
    phone_id = request.form.get("phone_id")
    link_url = request.form.get("link_url", "").strip()
    if not phone_id or not link_url:
        return jsonify({"error": "phone_id et link_url requis"}), 400
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé"}), 500
    def run():
        _thread_local.log_target = "swipe"
        _acquire_phone_start_slot()
        old_out = sys.stdout; old_err = sys.stderr
        sys.stdout = LogCapture("info", target="swipe")
        sys.stderr = LogCapture("error", target="swipe")
        try:
            push_log_swipe("info", f"🔗 [{phone_id}] Ajout lien : {link_url[:50]}...")
            ok = instagram_code.add_link_on_device(phone_id, link_url)
            if ok: push_log_swipe("success", f"✅ [{phone_id}] Lien ajouté avec succès")
            else:  push_log_swipe("error",   f"❌ [{phone_id}] Échec ajout lien")
        except Exception as e:
            import traceback
            push_log_swipe("error", f"❌ [{phone_id}] Exception : {e}")
            push_log_swipe("error", traceback.format_exc().strip())
        finally:
            with _geelark_urls_lock:
                _active_geelark_urls.pop(str(phone_id), None)
            sys.stdout = old_out; sys.stderr = old_err
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True})

@app.route("/api/instagram/add_bio", methods=["POST"])
def add_bio():
    phone_id = request.form.get("phone_id")
    bio      = request.form.get("bio", "").strip()
    if not phone_id or not bio:
        return jsonify({"error": "phone_id et bio requis"}), 400
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé"}), 500
    def run():
        _thread_local.log_target = "swipe"
        _acquire_phone_start_slot()
        old_out = sys.stdout; old_err = sys.stderr
        sys.stdout = LogCapture("info", target="swipe")
        sys.stderr = LogCapture("error", target="swipe")
        try:
            push_log_swipe("info", f"📝 [{phone_id}] Ajout bio : {bio[:40]}...")
            ok = instagram_code.add_bio_on_device(phone_id, bio)
            if ok: push_log_swipe("success", f"✅ [{phone_id}] Bio ajoutée avec succès")
            else:  push_log_swipe("error",   f"❌ [{phone_id}] Échec ajout bio")
        except Exception as e:
            import traceback
            push_log_swipe("error", f"❌ [{phone_id}] Exception : {e}")
            push_log_swipe("error", traceback.format_exc().strip())
        finally:
            with _geelark_urls_lock:
                _active_geelark_urls.pop(str(phone_id), None)
            sys.stdout = old_out; sys.stderr = old_err
    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True})

# ── Photo profil ──────────────────────────────────────────────────────────────
@app.route('/api/session/profile_photo', methods=['POST'])
def upload_profile_photo():
    import glob, uuid
    # Identifiant unique par appel → évite les collisions de noms entre uploads concurrents
    _uid = uuid.uuid4().hex[:8]
    stored_fn = request.form.get('stored_filename')
    if stored_fn:
        username = _mlib_username()
        base = _mlib_profile_base(username)
        src = os.path.join(base, _wsecure(stored_fn))
        if os.path.isfile(src):
            for old in glob.glob(os.path.join(_TMP_DIR, "profile_photo_*")):
                try: os.remove(old)
                except Exception: pass
            ext = os.path.splitext(stored_fn)[1] or '.jpg'
            dst = os.path.join(_TMP_DIR, f"profile_photo_{int(time.time())}_{_uid}_0{ext}")
            try:
                # copyfile (pas copy2) → pas de copie de métadonnées, donc pas de
                # utime() qui plantait par race condition si le fichier disparaissait
                _shutil.copyfile(src, dst)
                # Mémoriser le chemin du stock → le worker supprimera la photo du
                # stock une fois qu'elle aura été poussée avec succès sur le profil.
                try:
                    with open(dst + ".src", "w", encoding="utf-8") as _sf:
                        _sf.write(os.path.abspath(src))
                except Exception:
                    pass
            except Exception as e:
                sys.__stdout__.write(f"[WARN] upload_profile_photo copy: {e}\n")
                return jsonify({"error": f"Copie échouée: {e}"}), 500
            return jsonify({"success": True, "paths": [dst], "count": 1})
        return jsonify({"error": "Photo introuvable"}), 404
    files = request.files.getlist('photo')
    if not files:
        return jsonify({"error": "no file"}), 400
    for old in glob.glob(os.path.join(_TMP_DIR, "profile_photo_*")):
        try: os.remove(old)
        except Exception: pass
    paths = []
    for i, f in enumerate(files):
        ext = os.path.splitext(f.filename)[1] or '.jpg'
        path = os.path.join(_TMP_DIR, f"profile_photo_{int(time.time())}_{_uid}_{i}{ext}")
        try:
            f.save(path)
            paths.append(path)
        except Exception as e:
            sys.__stdout__.write(f"[WARN] upload_profile_photo save: {e}\n")
    if not paths:
        return jsonify({"error": "Aucune photo sauvegardée"}), 500
    return jsonify({"success": True, "paths": paths, "count": len(paths)})


import zipfile
import tempfile
import shutil

@app.route("/api/photos/upload_zip", methods=["POST"])
def upload_zip():
    user, username = _get_user_from_request()
    if not user:
        return jsonify({"error": "Non authentifié"}), 401

    if "zip" not in request.files:
        return jsonify({"error": "Aucun fichier ZIP reçu"}), 400

    zip_file = request.files["zip"]
    if not zip_file.filename.endswith(".zip"):
        return jsonify({"error": "Fichier invalide — uniquement .zip"}), 400

    base_dir = os.path.join(_PHOTOS_BASE, username)
    os.makedirs(base_dir, exist_ok=True)
    zip_dest  = os.path.join(base_dir, _ZIP_FILENAME)

    try:
        # 1. Valider le ZIP avant tout
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
            zip_file.save(tmp.name)
            tmp_path = tmp.name

        with zipfile.ZipFile(tmp_path, "r") as zf:
            zf.testzip()  # lève BadZipFile si corrompu

        # 2. Sauvegarder le ZIP (source de vérité pour les redémarrages)
        shutil.copy2(tmp_path, zip_dest)
        os.unlink(tmp_path)

        # 3. Vider l'ancien contenu extrait (garder le ZIP)
        for _e in os.listdir(base_dir):
            if _e == _ZIP_FILENAME:
                continue
            _ep = os.path.join(base_dir, _e)
            try:
                shutil.rmtree(_ep) if os.path.isdir(_ep) else os.remove(_ep)
            except Exception:
                pass

        # 4. Extraire le nouveau ZIP
        with zipfile.ZipFile(zip_dest, "r") as zf:
            zf.extractall(base_dir)

        # 5. Trouver le batch root et compter
        real_root = _find_batch_root(base_dir)
        batch_dirs = [
            d for d in os.listdir(real_root)
            if os.path.isdir(os.path.join(real_root, d)) and not d.startswith('.')
        ]
        extracted = len(batch_dirs)

        # 6. Mettre à jour PHOTOS_BASE_DIR en mémoire (plus besoin de le stocker dans config)
        if INSTAGRAM_CODE_LOADED:
            instagram_code.PHOTOS_BASE_DIR = real_root
            try:
                instagram_code.load_photo_folders()
            except Exception:
                pass

        push_log("success", f"📸 ZIP sauvegardé et extrait — {extracted} batch(s)")
        return jsonify({"success": True, "folders": extracted, "path": real_root})

    except zipfile.BadZipFile:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return jsonify({"error": "Fichier ZIP corrompu"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    


@app.route("/api/photos/delete_folder", methods=["POST"])
def delete_photo_folder():
    user, username = _get_user_from_request()
    if not user:
        return jsonify({"error": "Non authentifié"}), 401
    data = request.json or {}
    folder_name = data.get("folder")
    base_dir = _get_user_batch_dir(username) or (user.get("config", {}).get("system", {}).get("photos_dir") or
               app_config.get("system", {}).get("photos_dir", ""))
    if not base_dir or not folder_name:
        return jsonify({"error": "Dossier introuvable"}), 400
    full_path = os.path.join(base_dir, folder_name)
    # Sécurité : vérifier que le chemin est bien dans base_dir
    if not os.path.abspath(full_path).startswith(os.path.abspath(base_dir)):
        return jsonify({"error": "Chemin invalide"}), 400
    if not os.path.isdir(full_path):
        return jsonify({"error": "Dossier introuvable"}), 404
    shutil.rmtree(full_path)
    if INSTAGRAM_CODE_LOADED:
        try:
            instagram_code.load_photo_folders()
        except:
            pass
    return jsonify({"success": True, "deleted": folder_name})

@app.route("/api/photos/clear_all", methods=["POST"])
def clear_all_photos():
    user, username = _get_user_from_request()
    if not user:
        return jsonify({"error": "Non authentifié"}), 401
    base_dir = _get_user_batch_dir(username) or (user.get("config", {}).get("system", {}).get("photos_dir") or
               app_config.get("system", {}).get("photos_dir", ""))
    if not base_dir or not os.path.isdir(base_dir):
        return jsonify({"error": "Dossier photos non configuré"}), 400
    count = 0
    # Supprimer aussi le ZIP sauvegardé pour repartir de zéro
    _zip = os.path.join(os.path.join(_PHOTOS_BASE, username), _ZIP_FILENAME)
    if os.path.exists(_zip):
        try:
            os.remove(_zip)
        except Exception:
            pass
    for entry in os.listdir(base_dir):
        full = os.path.join(base_dir, entry)
        if os.path.isdir(full) and not entry.startswith('.'):
            shutil.rmtree(full)
            count += 1
    if INSTAGRAM_CODE_LOADED:
        try:
            instagram_code.load_photo_folders()
        except:
            pass
    return jsonify({"success": True, "deleted": count})

# ── Médiathèque : dossiers posts/reels + quick files ─────────────────────────

def _mlib_username():
    """Retourne le username de la session ou 'default' si non authentifié."""
    _, username = _get_user_from_request()
    return username or "default"

@app.route("/api/media/folders/<mtype>", methods=["GET"])
def media_list_folders(mtype):
    if mtype not in ("posts", "reels"):
        return jsonify({"error": "Type invalide"}), 400
    username = _mlib_username()
    base = _mlib_folder_base(username, mtype)
    folders = _mlib_read_meta(base)
    valid = []
    for f in folders:
        fdir = os.path.join(base, f.get("id", ""))
        if os.path.isdir(fdir):
            files = [fn for fn in sorted(os.listdir(fdir)) if not fn.startswith(".") and os.path.isfile(os.path.join(fdir, fn))]
            f["files"] = [{"name": fn, "mime": _mimetypes.guess_type(fn)[0] or "application/octet-stream"} for fn in files]
            valid.append(f)
    if len(valid) != len(folders):
        _mlib_write_meta(base, valid)
    return jsonify({"folders": valid})

@app.route("/api/media/folders/<mtype>", methods=["POST"])
def media_create_folder(mtype):
    if mtype not in ("posts", "reels"):
        return jsonify({"error": "Type invalide"}), 400
    username = _mlib_username()
    name = request.form.get("name", "").strip() or "Dossier"
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Aucun fichier reçu"}), 400
    folder_id = str(int(time.time() * 1000))
    base = _mlib_folder_base(username, mtype)
    fdir = os.path.join(base, folder_id)
    os.makedirs(fdir, exist_ok=True)
    saved = []
    for f in files:
        fn = _wsecure(f.filename) or f"file_{len(saved)+1}"
        f.save(os.path.join(fdir, fn))
        saved.append({"name": fn, "mime": f.content_type or _mimetypes.guess_type(fn)[0] or "application/octet-stream"})
    metas = _mlib_read_meta(base)
    metas.append({"id": folder_id, "name": name, "files": saved})
    _mlib_write_meta(base, metas)
    return jsonify({"success": True, "id": folder_id, "name": name, "files": saved})

@app.route("/api/media/folders/<mtype>/<folder_id>", methods=["DELETE"])
def media_delete_folder(mtype, folder_id):
    if mtype not in ("posts", "reels"):
        return jsonify({"error": "Type invalide"}), 400
    username = _mlib_username()
    base = _mlib_folder_base(username, mtype)
    _mlib_delete_folder(base, folder_id)
    return jsonify({"success": True})

@app.route("/api/media/file/<mtype>/<folder_id>/<filename>")
def media_serve_file(mtype, folder_id, filename):
    if mtype not in ("posts", "reels"):
        return jsonify({"error": "Type invalide"}), 400
    username = _mlib_username()
    base = _mlib_folder_base(username, mtype)
    fn = _wsecure(filename)
    fpath = os.path.join(base, folder_id, fn)
    if not os.path.abspath(fpath).startswith(os.path.abspath(base)):
        return jsonify({"error": "Chemin invalide"}), 400
    if not os.path.isfile(fpath):
        return jsonify({"error": "Fichier introuvable"}), 404
    return send_file(fpath)

@app.route("/api/media/quick/<mtype>", methods=["GET"])
def media_list_quick(mtype):
    if mtype not in ("post", "reel"):
        return jsonify({"error": "Type invalide"}), 400
    username = _mlib_username()
    qdir = _mlib_quick_base(username, mtype)
    files = [{"name": fn, "mime": _mimetypes.guess_type(fn)[0] or "application/octet-stream"}
             for fn in sorted(os.listdir(qdir))
             if not fn.startswith(".") and os.path.isfile(os.path.join(qdir, fn))]
    return jsonify({"files": files})

@app.route("/api/media/quick/<mtype>", methods=["POST"])
def media_upload_quick(mtype):
    if mtype not in ("post", "reel"):
        return jsonify({"error": "Type invalide"}), 400
    username = _mlib_username()
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Aucun fichier reçu"}), 400
    qdir = _mlib_quick_base(username, mtype)
    saved = []
    for f in files:
        fn = _wsecure(f.filename) or f"file_{int(time.time()*1000)}"
        base_fn, ext = os.path.splitext(fn)
        dest = os.path.join(qdir, fn)
        counter = 0
        while os.path.exists(dest):
            counter += 1
            fn = f"{base_fn}_{counter}{ext}"
            dest = os.path.join(qdir, fn)
        f.save(dest)
        saved.append({"name": fn, "mime": f.content_type or _mimetypes.guess_type(fn)[0] or "application/octet-stream"})
    return jsonify({"success": True, "files": saved})

@app.route("/api/media/quick/<mtype>/<filename>", methods=["DELETE"])
def media_delete_quick(mtype, filename):
    if mtype not in ("post", "reel"):
        return jsonify({"error": "Type invalide"}), 400
    username = _mlib_username()
    qdir = _mlib_quick_base(username, mtype)
    fn = _wsecure(filename)
    fpath = os.path.join(qdir, fn)
    if os.path.abspath(fpath).startswith(os.path.abspath(qdir)) and os.path.isfile(fpath):
        os.remove(fpath)
    return jsonify({"success": True})

@app.route("/api/media/quick/file/<mtype>/<filename>")
def media_serve_quick(mtype, filename):
    if mtype not in ("post", "reel"):
        return jsonify({"error": "Type invalide"}), 400
    username = _mlib_username()
    qdir = _mlib_quick_base(username, mtype)
    fn = _wsecure(filename)
    fpath = os.path.join(qdir, fn)
    if not os.path.abspath(fpath).startswith(os.path.abspath(qdir)):
        return jsonify({"error": "Chemin invalide"}), 400
    if not os.path.isfile(fpath):
        return jsonify({"error": "Fichier introuvable"}), 404
    return send_file(fpath)

# ── Stock photos de profil (persistant serveur) ────────────────────────────────
@app.route("/api/media/profile_photos", methods=["GET"])
def media_list_profile_photos():
    username = _mlib_username()
    qdir = _mlib_profile_base(username)
    files = [{"name": fn, "mime": _mimetypes.guess_type(fn)[0] or "image/jpeg"}
             for fn in sorted(os.listdir(qdir))
             if not fn.startswith(".") and ".claiming_" not in fn
             and os.path.isfile(os.path.join(qdir, fn))]
    return jsonify({"files": files})

@app.route("/api/media/profile_photos", methods=["POST"])
def media_upload_profile_photos():
    username = _mlib_username()
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "Aucun fichier reçu"}), 400
    qdir = _mlib_profile_base(username)
    saved = []
    for f in files:
        fn = _wsecure(f.filename) or f"photo_{int(time.time()*1000)}"
        base_fn, ext = os.path.splitext(fn)
        dest = os.path.join(qdir, fn)
        counter = 0
        while os.path.exists(dest):
            counter += 1
            fn = f"{base_fn}_{counter}{ext}"
            dest = os.path.join(qdir, fn)
        f.save(dest)
        saved.append({"name": fn, "mime": f.content_type or _mimetypes.guess_type(fn)[0] or "image/jpeg"})
    return jsonify({"success": True, "files": saved})

@app.route("/api/media/profile_photos/<filename>", methods=["DELETE"])
def media_delete_profile_photo(filename):
    username = _mlib_username()
    qdir = _mlib_profile_base(username)
    fn = _wsecure(filename)
    fpath = os.path.join(qdir, fn)
    if os.path.abspath(fpath).startswith(os.path.abspath(qdir)) and os.path.isfile(fpath):
        os.remove(fpath)
    return jsonify({"success": True})

@app.route("/api/media/profile/file/<filename>")
def media_serve_profile_photo(filename):
    username = _mlib_username()
    qdir = _mlib_profile_base(username)
    fn = _wsecure(filename)
    fpath = os.path.join(qdir, fn)
    if not os.path.abspath(fpath).startswith(os.path.abspath(qdir)):
        return jsonify({"error": "Chemin invalide"}), 400
    if not os.path.isfile(fpath):
        return jsonify({"error": "Fichier introuvable"}), 404
    return send_file(fpath)

# ── Config (per-user) ────────────────────────────────────────────────────────
@app.route("/api/config", methods=["GET"])
def get_config():
    user, _ = _get_user_from_request()
    if user:
        if user.get("role") == "admin":
            return jsonify(_deep_merge(app_config, user.get("config", {})))
        # Regular users get neutral defaults as base — never inherit another user's credentials
        return jsonify(_deep_merge(DEFAULT_CONFIG, user.get("config", {})))
    return jsonify(app_config)

@app.route("/api/config", methods=["POST"])
def update_config():
    global app_config
    data = request.json
    if not data:
        return jsonify({"error": "Données manquantes"}), 400
    user, username = _get_user_from_request()
    if user and username:
        with _users_rw_lock:
            users_data = _load_users()
            users_data["users"][username]["config"] = _deep_merge(
                users_data["users"][username].get("config", {}), data)
            _save_users(users_data)
        _apply_user_config(username)
    else:
        app_config = _deep_merge(app_config, data)
        save_app_config(app_config)
        apply_config_to_code()
    push_log("info", "⚙️ Configuration sauvegardée")
    return jsonify({"success": True})


# ── Vérification comptes ──────────────────────────────────────────────────────

check_state = {
    "running": False,
    "done": 0, "total": 0, "vivants": 0, "bannis": 0,
    "current_phone": None, "current_step": None,
}
check_stop_flag = [False]


@app.route("/api/check/status")
def check_status():
    return jsonify(check_state)


@app.route("/api/check/launch", methods=["POST"])
def launch_check():
    global check_stop_flag
    if check_state["running"]:
        return jsonify({"error": "Vérification déjà en cours"}), 400
    if not INSTAGRAM_CODE_LOADED:
        return jsonify({"error": "code.py non chargé"}), 500

    data = request.json or {}
    config = {
        "delete_banned": data.get("delete_banned", True),
    }

    check_stop_flag = [False]
    check_state.update({
        "running": True, "done": 0, "total": 0,
        "vivants": 0, "bannis": 0,
        "current_phone": None, "current_step": "Démarrage",
    })

    def run():
        _thread_local.log_target = "creation"
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = LogCapture("info", target="creation")
        sys.stderr = LogCapture("error", target="creation")
        try:
            result = instagram_code.run_check_session(config, check_stop_flag)
            check_state.update({
                "total":   result["total"],
                "vivants": result["vivants"],
                "bannis":  result["bannis"],
                "done":    result["total"],
            })
        except Exception as e:
            import traceback
            push_log("error", f"❌ Erreur check : {e}")
            push_log("error", traceback.format_exc().strip())
        finally:
            check_state["running"] = False
            check_state["current_step"] = "Terminé"
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            _thread_local.log_target = None

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/check/stop", methods=["POST"])
def stop_check():
    global check_stop_flag
    check_stop_flag[0] = True
    check_state["running"] = False
    check_state["current_step"] = "Arrêté"
    push_log("warn", "⛔ Vérification arrêtée")
    return jsonify({"success": True})

# ── Stats ─────────────────────────────────────────────────────────────────────
@app.route("/api/stats")
def get_stats():
    accounts = []
    if os.path.exists(accounts_file):
        with open(accounts_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                acc = parse_account_line(line)
                if acc:
                    accounts.append(acc)

    cities_used = {}
    for acc in accounts:
        c = acc.get("city", "?")
        cities_used[c] = cities_used.get(c, 0) + 1

    from datetime import timedelta
    daily = {}
    today = datetime.now().date()
    for i in range(14):
        d = today - timedelta(days=i)
        daily[d.strftime("%d/%m")] = 0

    for acc in accounts:
        try:
            d = datetime.strptime(acc["date"], "%d/%m/%Y %H:%M").date()
            key = d.strftime("%d/%m")
            if key in daily:
                daily[key] += 1
        except:
            pass

    daily_list = [{"label": k, "count": v} for k, v in reversed(list(daily.items()))]

    return jsonify({
        "total": len(accounts),
        "cities_used": cities_used,
        "daily": daily_list,
        "success_rate": round(session_state["success"] / max(session_state["done"], 1) * 100)
                        if session_state["done"] > 0 else None,
        "session_success": session_state["success"],
        "session_errors": session_state["errors"],
        "session_done": session_state["done"],
    })

# ── Panel settings API ───────────────────────────────────────────────────────
@app.route("/api/panel_settings", methods=["GET"])
def get_panel_settings():
    _mention_tags    = (instagram_code.MENTION_TAGS    if INSTAGRAM_CODE_LOADED else ["@miaivvyy"])
    _first_names     = (instagram_code.FIRST_NAMES     if INSTAGRAM_CODE_LOADED else ["Miahyvina"])
    _creation_mode   = (instagram_code.CREATION_MODE   if INSTAGRAM_CODE_LOADED else "phone")
    _android_version = (instagram_code.ANDROID_VERSION if INSTAGRAM_CODE_LOADED else "Android 14")
    return jsonify({
        "POSTING_COOLDOWN_HOURS": POSTING_COOLDOWN_HOURS,
        "MAX_POSTS_PER_DAY":      MAX_POSTS_PER_DAY,
        "MAX_REELS_PER_DAY":      MAX_REELS_PER_DAY,
        "MAX_STORIES_PER_DAY":    MAX_STORIES_PER_DAY,
        "WARMUP_USERNAMES":       WARMUP_USERNAMES,
        "PHONE_STAGGER_SEC":      PHONE_STAGGER_SEC,
        "MIN_ACCOUNT_AGE_HOURS":  MIN_ACCOUNT_AGE_HOURS,
        "REQUIRE_WARMUP":         REQUIRE_WARMUP,
        "MENTION_TAG":     (instagram_code.MENTION_TAG  if INSTAGRAM_CODE_LOADED else "@miaivvyy"),
        "MENTION_TAGS":    "\n".join(_mention_tags),
        "FIRST_NAMES":     "\n".join(_first_names),
        "CREATION_MODE":   _creation_mode,
        "ANDROID_VERSION": _android_version,
    })

@app.route("/api/panel_settings", methods=["POST"])
def update_panel_settings():
    global POSTING_COOLDOWN_HOURS, MAX_POSTS_PER_DAY, MAX_REELS_PER_DAY, MAX_STORIES_PER_DAY
    global WARMUP_USERNAMES, PHONE_STAGGER_SEC, MIN_ACCOUNT_AGE_HOURS, REQUIRE_WARMUP
    data = request.json or {}
    if "POSTING_COOLDOWN_HOURS" in data: POSTING_COOLDOWN_HOURS = int(data["POSTING_COOLDOWN_HOURS"])
    if "MAX_POSTS_PER_DAY"      in data: MAX_POSTS_PER_DAY      = int(data["MAX_POSTS_PER_DAY"])
    if "MAX_REELS_PER_DAY"      in data: MAX_REELS_PER_DAY      = int(data["MAX_REELS_PER_DAY"])
    if "MAX_STORIES_PER_DAY"    in data: MAX_STORIES_PER_DAY    = int(data["MAX_STORIES_PER_DAY"])
    if "WARMUP_USERNAMES"       in data: WARMUP_USERNAMES        = str(data["WARMUP_USERNAMES"])
    if "PHONE_STAGGER_SEC"      in data: PHONE_STAGGER_SEC       = max(0, int(data["PHONE_STAGGER_SEC"]))
    if "MIN_ACCOUNT_AGE_HOURS"  in data: MIN_ACCOUNT_AGE_HOURS   = max(0, int(data["MIN_ACCOUNT_AGE_HOURS"]))
    if "REQUIRE_WARMUP"         in data: REQUIRE_WARMUP           = bool(data["REQUIRE_WARMUP"])
    if INSTAGRAM_CODE_LOADED:
        if "MENTION_TAGS" in data:
            _tags = [t.strip() for t in data["MENTION_TAGS"].split("\n") if t.strip()]
            if _tags:
                instagram_code.MENTION_TAGS = _tags
                instagram_code.MENTION_TAG  = _tags[0]
        elif "MENTION_TAG" in data:
            _t = data["MENTION_TAG"].strip()
            instagram_code.MENTION_TAG  = _t
            instagram_code.MENTION_TAGS = [_t]
        if "FIRST_NAMES" in data:
            _names = [n.strip() for n in data["FIRST_NAMES"].split("\n") if n.strip()]
            if _names:
                instagram_code.FIRST_NAMES = _names
                instagram_code.FIRST_NAME  = _names[0]
        instagram_code.MIN_ACCOUNT_AGE_HOURS = MIN_ACCOUNT_AGE_HOURS
        if "CREATION_MODE" in data and data["CREATION_MODE"] in ("phone", "email"):
            instagram_code.CREATION_MODE = data["CREATION_MODE"]
        if "ANDROID_VERSION" in data and data["ANDROID_VERSION"] in ("Android 13", "Android 14"):
            instagram_code.ANDROID_VERSION = data["ANDROID_VERSION"]
    _save_panel_settings()
    _saved_tags = (instagram_code.MENTION_TAGS if INSTAGRAM_CODE_LOADED else [])
    push_log("info", f"⚙️ Limites posting — fenêtre: {POSTING_COOLDOWN_HOURS}h | posts: {MAX_POSTS_PER_DAY} | reels: {MAX_REELS_PER_DAY} | stories: {MAX_STORIES_PER_DAY} | âge min: {MIN_ACCOUNT_AGE_HOURS}h | warmup requis: {REQUIRE_WARMUP} | tags: {_saved_tags}")
    push_log("info", f"💾 Réglages enregistrés dans : {PANEL_SETTINGS_FILE}")
    return jsonify({"success": True, "saved_to": PANEL_SETTINGS_FILE})

@app.route("/api/posting_history", methods=["GET"])
def get_posting_history():
    with _posting_history_lock:
        history = _load_posting_history()
    return jsonify(history)

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=_lazy_load_instagram_code, daemon=True).start()
    sys.__stdout__.write("\n" + "="*55 + "\n")
    sys.__stdout__.write("  InstagramOps Server\n")
    sys.__stdout__.write("="*55 + "\n")
    sys.__stdout__.write(f"  Panel   : http://localhost:5004\n")
    sys.__stdout__.write(f"  Données : {_DATA_DIR}\n")
    sys.__stdout__.write("="*55 + "\n\n")
    sys.__stdout__.flush()
    app.run(host="0.0.0.0", port=5004, debug=False, threaded=True)