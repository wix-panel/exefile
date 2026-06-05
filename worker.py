# worker.py
# Lancé en subprocess pour chaque compte — complètement isolé
import sys
import os
import json
import time
import subprocess
import tempfile

# Lire la config depuis argv[1] (JSON)
config = json.loads(sys.argv[1])

proxy           = config["proxy"]
bio             = config.get("bio", "")
_first_names_cfg = config.get("first_names") or []
firstname        = (config.get("firstname") or (_first_names_cfg[0] if _first_names_cfg else "Miahyvina"))
birth_year      = config.get("birth_year", "2004")
email           = config.get("email", "")
worker_id       = config.get("worker_id", 0)
log_file        = config.get("log_file", os.path.join(tempfile.gettempdir(), f"worker_{worker_id}.log"))
sms_config      = config.get("sms_config", {})
creation_mode   = config.get("creation_mode", "phone")
android_version = config.get("android_version", "Android 14")

import insta_core as instagram_code

class FileLogger:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8", buffering=1)
    def write(self, text):
        if text and text.strip():
            line = f"[{time.strftime('%H:%M:%S')}] {text.strip()}\n"
            self.f.write(line)
            self.f.flush()
            sys.__stdout__.write(line)
            sys.__stdout__.flush()
    def flush(self):
        self.f.flush()

logger = FileLogger(log_file)
sys.stdout = logger
sys.stderr = logger

# pool_log → stdout dans le worker (pas de queue à consommer ici)
instagram_code._pool_log_callback = lambda msg: print(f"[POOL] {msg}")

print(f"🚀 Worker {worker_id} démarré")

# ── Config de base ───────────────────────────────────────────────
instagram_code.FIRST_NAME     = firstname
instagram_code.FIRST_NAMES    = _first_names_cfg if _first_names_cfg else [firstname]
instagram_code.BIRTH_YEAR     = birth_year
instagram_code.EMAIL          = email
instagram_code.DEBUG_MODE     = False
instagram_code.CREATION_MODE   = creation_mode
instagram_code.ANDROID_VERSION = android_version
# Dossier stock des photos de profil → chaque worker y réserve SA propre photo
instagram_code.PROFILE_STOCK_DIR = config.get("profile_stock_dir", "")

# ── Appliquer la config SMS depuis le panel ──────────────────────
prov_cfg = sms_config.get("providers", {})

bower_cfg = prov_cfg.get("smsbower", {})
if bower_cfg.get("enabled") is not None: instagram_code.SMSBOWER_ENABLED = bower_cfg["enabled"]
if bower_cfg.get("api_key"):             instagram_code.SMSBOWER_API_KEY  = bower_cfg["api_key"]
if bower_cfg.get("service"):             instagram_code.SMSBOWER_SERVICE  = bower_cfg["service"]
if bower_cfg.get("country"):             instagram_code.SMSBOWER_COUNTRY  = str(bower_cfg["country"])
if bower_cfg.get("dial_code"):           instagram_code.SMSBOWER_DIAL_CODE = bower_cfg["dial_code"]
if bower_cfg.get("max_price"):           instagram_code.SMSBOWER_MAX_PRICE = float(bower_cfg["max_price"])

smspin_cfg = prov_cfg.get("smspin", {})
if smspin_cfg.get("enabled") is not None: instagram_code.SMSPIN_ENABLED  = smspin_cfg["enabled"]
if smspin_cfg.get("api_key"):             instagram_code.SMSPIN_API_KEY   = smspin_cfg["api_key"]
if smspin_cfg.get("country_name"):        instagram_code.SMSPIN_COUNTRY   = smspin_cfg["country_name"]
if smspin_cfg.get("app"):                 instagram_code.SMSPIN_APP       = smspin_cfg["app"]
if smspin_cfg.get("dial_code"):           instagram_code.SMSPIN_DIAL_CODE = smspin_cfg["dial_code"]

hero_cfg = prov_cfg.get("herosms", {})
if hero_cfg.get("enabled") is not None: instagram_code.HEROSMS_ENABLED = hero_cfg["enabled"]
if hero_cfg.get("api_key"):             instagram_code.HERO_API_KEY    = hero_cfg["api_key"]
if hero_cfg.get("service"):             instagram_code.HERO_SERVICE    = hero_cfg["service"]
if hero_cfg.get("country"):             instagram_code.HERO_COUNTRY    = str(hero_cfg["country"])
if hero_cfg.get("dial_code"):           instagram_code.HERO_DIAL_CODE  = hero_cfg["dial_code"]

sagg_cfg = prov_cfg.get("sim_aggregator", {})
if sagg_cfg.get("enabled") is not None: instagram_code.SIM_AGGREGATOR_ENABLED  = sagg_cfg["enabled"]
if sagg_cfg.get("api_key"):             instagram_code.SIM_AGGREGATOR_API_KEY  = sagg_cfg["api_key"]
if sagg_cfg.get("service"):             instagram_code.SIM_AGGREGATOR_SERVICE  = sagg_cfg["service"]
if sagg_cfg.get("country"):             instagram_code.SIM_AGGREGATOR_COUNTRY  = str(sagg_cfg["country"])
if sagg_cfg.get("dial_code"):           instagram_code.SIM_AGGREGATOR_DIAL_CODE = sagg_cfg["dial_code"]

pvap_cfg = prov_cfg.get("pvapins", {})
if pvap_cfg.get("enabled") is not None: instagram_code.PVAPINS_ENABLED  = pvap_cfg["enabled"]
if pvap_cfg.get("customer_key"):        instagram_code.PVAPINS_CUSTOMER  = pvap_cfg["customer_key"]
if pvap_cfg.get("service"):             instagram_code.PVAPINS_SERVICE   = pvap_cfg["service"]
if pvap_cfg.get("country"):             instagram_code.PVAPINS_COUNTRY   = str(pvap_cfg["country"])
if pvap_cfg.get("dial_code"):           instagram_code.PVAPINS_DIAL_CODE = pvap_cfg["dial_code"]

# ── Charger les photos ───────────────────────────────────────────
instagram_code.load_photo_folders()

# ══════════════════════════════════════════════════════════════
# ÉTAPE 1 : RÉCUPÉRER EMAIL OU NUMÉRO AVANT CRÉATION GEELARK
# ══════════════════════════════════════════════════════════════
if creation_mode == "email":
    # Démarrer le pool scraper TOUT DE SUITE → 5 appels API en parallèle
    instagram_code.start_email_pool_scraper(target_size=5, parallel=5)

    # Essayer d'abord le pool (si un autre worker / le scraper en a mis)
    pre_email, pre_mail_id = instagram_code.pool_get_email()
    if pre_email:
        print(f"[POOL] ✅ Gmail depuis le pool : {pre_email}")
    else:
        print(f"[POOL] 📧 Récupération Gmail (SMSBower) avant démarrage...")
        while pre_email is None:
            # Re-check pool à chaque tour (le scraper de fond peut l'avoir rempli)
            pre_email, pre_mail_id = instagram_code.pool_get_email()
            if pre_email:
                print(f"[POOL] ✅ Gmail depuis le pool : {pre_email}")
                break
            pre_email, pre_mail_id = instagram_code.get_smsbower_email()
            if pre_email:
                print(f"[POOL] ✅ Gmail prêt : {pre_email}")
            else:
                print(f"[POOL] ⏳ Pas d'email dispo — retry immédiat...")

    # Injecter pour open_instagram
    instagram_code._pre_fetched_email   = pre_email
    instagram_code._pre_fetched_mail_id = pre_mail_id
else:
    print(f"[POOL] 📱 Attente d'un numéro disponible avant démarrage...")
    pre_number_result = None
    while pre_number_result is None:
        result = instagram_code.get_hero_number()
        if result:
            aid, num, prov = result
            fmt = instagram_code.format_number(num)
            if fmt:
                pre_number_result = (aid, fmt, prov)
                print(f"[POOL] ✅ Numéro prêt : {fmt} ({prov})")
            else:
                instagram_code.cancel_bower_number(aid)
                print(f"[POOL] ⚠️ Numéro invalide ({num}) — retry...")
        else:
            print(f"[POOL] ⏳ Pas de numéro dispo — retry 5s...")
            time.sleep(5)
    # Injecter pour open_instagram
    instagram_code._pre_fetched_number = pre_number_result

# ══════════════════════════════════════════════════════════════
# ÉTAPE 2 : Créer le profil GeeLark
# maintenant qu'on a un identifiant garanti
# ══════════════════════════════════════════════════════════════
print(f"📱 Création profil GeeLark...")
phone_id = None
for create_attempt in range(5):
    phone_id = instagram_code.create_phone_profile(
        proxy["host"], proxy["port"], proxy["user"], proxy["pass"]
    )
    if phone_id:
        print(f"✅ Profil créé : {phone_id}")
        time.sleep(8)
        break
    print(f"⚠️ Tentative {create_attempt+1}/5 échouée — retry 15s...")
    time.sleep(15)

if not phone_id:
    print(f"❌ Échec création profil après 5 tentatives")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════
# ÉTAPE 3 : Démarrer le téléphone
# ══════════════════════════════════════════════════════════════
print(f"📱 Démarrage téléphone {phone_id}...")
START_MAX_RETRIES = 5
started = False
for start_attempt in range(START_MAX_RETRIES):
    if instagram_code.start_phone(phone_id):
        started = True
        break
    print(f"⚠️ Échec démarrage (tentative {start_attempt+1}/{START_MAX_RETRIES}) — retry 20s...")
    time.sleep(20)

if not started:
    print(f"❌ Impossible de démarrer le téléphone {phone_id} après {START_MAX_RETRIES} tentatives")
    instagram_code.stop_phone(phone_id)
    sys.exit(1)

print(f"⏳ Attente boot (15s)...")
time.sleep(15)

# ── Activation ADB ──────────────────────────────────────────────
instagram_code.enable_adb(phone_id)
time.sleep(5)

# ── Attendre ADB disponible avec retry ─────────────────────────
ADB_MAX_RETRIES = 3
device, pwd = None, None

for adb_attempt in range(ADB_MAX_RETRIES):
    device, pwd = instagram_code.wait_for_adb(phone_id, max_wait=150)
    if device:
        break
    print(f"⚠️ ADB timeout (tentative {adb_attempt+1}/{ADB_MAX_RETRIES}) — relance téléphone...")
    instagram_code.stop_phone(phone_id)
    time.sleep(8)
    if adb_attempt < ADB_MAX_RETRIES - 1:
        if not instagram_code.start_phone(phone_id):
            print(f"❌ Impossible de relancer le téléphone")
            break
        print(f"⏳ Attente boot relance (15s)...")
        time.sleep(15)
        instagram_code.enable_adb(phone_id)
        time.sleep(5)

if not device:
    print(f"❌ ADB définitivement indisponible après {ADB_MAX_RETRIES} tentatives")
    instagram_code.stop_phone(phone_id)
    sys.exit(1)

print(f"✅ ADB disponible : {device}")

# ── glogin ──────────────────────────────────────────────────────
connected = False
for attempt in range(20):
    subprocess.run(
        f'"{instagram_code.ADB_PATH}" disconnect {device}',
        shell=True, capture_output=True
    )
    time.sleep(1)
    subprocess.run(
        f'"{instagram_code.ADB_PATH}" connect {device}',
        shell=True, capture_output=True
    )
    time.sleep(4)
    result = subprocess.run(
        f'"{instagram_code.ADB_PATH}" -s {device} shell glogin {pwd}',
        shell=True, capture_output=True, text=True
    )
    print(f"glogin [{attempt+1}] → {result.stdout.strip()}")
    if "success" in result.stdout.lower():
        connected = True
        break
    time.sleep(3)

if not connected:
    print(f"❌ glogin échoué sur {phone_id} — abandon")
    instagram_code.stop_phone(phone_id)
    sys.exit(1)

print(f"✅ ADB connecté")

# ── Data Saver ──────────────────────────────────────────────────
instagram_code.enable_data_saver(device)

# ── GPS ─────────────────────────────────────────────────────────
city, lat, lon = instagram_code.apply_random_city_gps(phone_id)
time.sleep(2)

# ── Proxy ───────────────────────────────────────────────────────
print(f"🔄 Application proxy {proxy['host']}:{proxy['port']}...")
proxy_ok = instagram_code.change_phone_proxy(
    phone_id,
    proxy["host"], proxy["port"],
    proxy["user"], proxy["pass"],
    "socks5"
)
if proxy_ok:
    print(f"✅ Proxy appliqué")
else:
    print(f"⚠️ Proxy non appliqué — on continue quand même")
time.sleep(5)

# ══════════════════════════════════════════════════════════════
# ÉTAPE 4 : Lancer Instagram
# ══════════════════════════════════════════════════════════════
print(f"📲 Lancement instagram (mode={creation_mode})...")
result_instagram = instagram_code.open_instagram(
    device, None, city, lat, lon,
    phone_id=phone_id, pwd=pwd,
    bio=bio,
    ban_on_existing_email=False,
)

# ── Gestion des retours ──────────────────────────────────────────
# open_instagram ne retourne None QUE en cas de succès complet
# (clic 'Got it' + compte sauvegardé). Tout autre retour = création
# NON aboutie (pas de 'Got it') → suppression du profil GeeLark.
if result_instagram is not None:
    print(f"⚠️ Création non aboutie (code: {result_instagram}) — suppression du profil...")
    try:
        instagram_code.delete_phone_geelark(phone_id)
        print(f"🗑️ Profil supprimé : {phone_id}")
    except Exception as e:
        print(f"⚠️ Erreur suppression (peut-être déjà supprimé) : {e}")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════
# ÉTAPE 5 : Succès complet ('Got it' cliqué + compte sauvegardé)
# ══════════════════════════════════════════════════════════════
instagram_code.stop_phone(phone_id)
print(f"✅ Worker {worker_id} terminé avec succès")
sys.exit(0)
