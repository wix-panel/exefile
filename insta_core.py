import subprocess
import time
import re
import requests
import random
import os
import sys
import json
import uuid
import hashlib
import threading
import tempfile
import shutil as _shutil
import queue as _queue
import json as _json_hl
import os as _os_hl
from datetime import datetime, timedelta

# Dossier temporaire portable (Windows: %TEMP%, macOS/Linux: /tmp)
_TMP_DIR = tempfile.gettempdir()

# Dossier de données persistant : variable DATA_DIR (posée par server.py),
# sinon à côté de l'exécutable compilé, sinon à côté du script.
def _resolve_data_dir():
    env = os.environ.get("DATA_DIR")
    if env:
        return env
    if getattr(sys, "frozen", False) or globals().get("__compiled__"):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))

_DATA_DIR = _resolve_data_dir()
os.makedirs(_DATA_DIR, exist_ok=True)

HIGHLIGHT_REGISTRY_FILE = os.path.join(_TMP_DIR, "highlight_registry.json")
ACCOUNTS_FILE = os.path.join(_DATA_DIR, "accounts_created.json")

# username pré-généré à la création du profil GeeLark, réutilisé pendant le flow Instagram
_phone_usernames: dict = {}


def rename_phone_profile(phone_id, username):
    """Renomme le profil GeeLark avec le username Instagram + date (ex: mia1234ab 07/05)."""
    if not phone_id or not username:
        return
    new_name = f"{username} {datetime.now().strftime('%d/%m')}"
    try:
        result = geelark_request("POST", "/open/v1/phone/detail/update", {
            "id": str(phone_id),
            "serialName": new_name,
        })
        if result.get("code") == 0:
            print(f"  ✅ Profil GeeLark renommé : {new_name}")
        else:
            print(f"  ⚠️ Renommage profil échoué : code={result.get('code')} msg={result.get('msg')}")
    except Exception as e:
        print(f"  ⚠️ Erreur renommage profil : {e}")


def save_created_account(username, phone_id, password="Alexis06"):
    """Enregistre un compte créé avec l'heure de création dans accounts_created.json."""
    if not username:
        return
    try:
        accounts = []
        if os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accounts = json.load(f)
        accounts.append({
            "username": username,
            "password": password,
            "phone_id": str(phone_id),
            "created_at": datetime.now().isoformat(),
        })
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, indent=2, ensure_ascii=False)
        print(f"  💾 Compte sauvegardé : @{username} (créé le {datetime.now().strftime('%d/%m/%Y à %H:%M')})")
        rename_phone_profile(phone_id, username)
    except Exception as e:
        print(f"  ⚠️ Erreur sauvegarde compte : {e}")


def _detect_logged_out_and_cleanup(device, phone_id, xml):
    """
    Appelé dans les boucles d'attente feed des fonctions d'action.
    Si Instagram affiche l'écran d'accueil (compte déconnecté) ou 'Confirm you're human',
    supprime l'entrée JSON + profil GeeLark et retourne True (l'appelant doit s'arrêter).
    """
    _login_kw = [
        "Get started", "Get Started", "Create new account", "Create New Account",
        "Log in with", "Log In with",
    ]
    _human_kw = [
        "Confirm you're human", "Confirm you're human",
        "community standards on account integrity",
    ]
    problem = None
    if any(kw in xml for kw in _login_kw):
        problem = "logged_out"
    elif any(kw in xml for kw in _human_kw):
        problem = "human_check"

    if not problem:
        return False

    if problem == "logged_out":
        print(f"  🚫 Écran d'accueil Instagram — compte déconnecté, suppression...")
    else:
        print(f"  🚫 'Confirm you're human' — compte inutilisable, suppression...")

    try:
        if os.path.exists(ACCOUNTS_FILE):
            with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                accounts = json.load(f)
            accounts = [a for a in accounts if str(a.get("phone_id")) != str(phone_id)]
            with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                json.dump(accounts, f, indent=2, ensure_ascii=False)
            print(f"  🗑️ Compte supprimé de accounts_created.json")
    except Exception as e:
        print(f"  ⚠️ Erreur suppression JSON : {e}")

    try:
        delete_phone_geelark(phone_id)
    except Exception:
        pass

    return True


def check_account_age_warning(phone_id, action_name="cette action"):
    """
    Vérifie si le dernier compte créé sur ce téléphone a moins de 24h.
    Affiche un avertissement et demande confirmation si c'est le cas.
    Retourne True pour continuer, False pour annuler.
    """
    try:
        if not os.path.exists(ACCOUNTS_FILE):
            return True
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accounts = json.load(f)
        phone_accounts = [a for a in accounts if str(a.get("phone_id")) == str(phone_id)]
        if not phone_accounts:
            return True
        last = sorted(phone_accounts, key=lambda a: a["created_at"])[-1]
        created_at = datetime.fromisoformat(last["created_at"])
        age = datetime.now() - created_at
        _min_age_h = MIN_ACCOUNT_AGE_HOURS if MIN_ACCOUNT_AGE_HOURS > 0 else 0
        if _min_age_h > 0 and age < timedelta(hours=_min_age_h):
            heures = int(age.total_seconds() // 3600)
            minutes = int((age.total_seconds() % 3600) // 60)
            print(f"\n  ⚠️  Compte récent (@{last.get('username', '?')}) — {heures}h {minutes}min — délai min: {_min_age_h}h — {action_name} ignorée.")
            return False
    except Exception as e:
        print(f"  ⚠️ Erreur vérification âge compte : {e}")
    return True

def _load_highlight_registry():
    if _os_hl.path.exists(HIGHLIGHT_REGISTRY_FILE):
        try:
            with open(HIGHLIGHT_REGISTRY_FILE, "r") as f:
                return _json_hl.load(f)
        except:
            pass
    return {}

def _save_highlight_registry(registry):
    try:
        with open(HIGHLIGHT_REGISTRY_FILE, "w") as f:
            _json_hl.dump(registry, f, indent=2)
    except Exception as e:
        print(f"  ⚠️ Erreur sauvegarde highlight registry : {e}")

def _has_highlight(phone_id, highlight_name):
    registry = _load_highlight_registry()
    pid = str(phone_id)
    return registry.get(pid, {}).get(highlight_name, False)


def add_story_to_highlight(device: str, phone_id: str, highlight_name: str = "tuto 1") -> bool:
    print(f"\n  ⭐ add_story_to_highlight — compte {phone_id} | '{highlight_name}'")

    already_created = _has_highlight(phone_id, highlight_name)
    print(f"  📋 Registry : '{highlight_name}' déjà créé = {already_created}")

    # ── Relancer Instagram ──────────────────────────────────────────────
    print(f"  🔄 Redémarrage Instagram...")
    adb(device, "shell am force-stop com.instagram.android")
    time.sleep(2)
    subprocess.run(
        f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
        f'-c android.intent.category.LAUNCHER 1',
        shell=True, capture_output=True, text=True
    )
    time.sleep(6)
    _click_allow_if_present(device)
    time.sleep(1)

    # ── Attendre le feed ────────────────────────────────────────────────
    print(f"  🔍 Attente feed Instagram...")
    for tick in range(20):
        adb(device, "shell uiautomator dump /sdcard/ui_hl_feed.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_hl_feed.xml").stdout
        _click_allow_if_present(device)
        if any(kw in xml for kw in ["Your story", "your story", "com.instagram.android"]):
            print(f"  ✅ Feed détecté ({tick+1}s)")
            handle_notifications_popup(device, safe_ui_dump(device, "/sdcard/ui_notif_popup.xml"))
            break
        print(f"  ⏳ Attente feed ({tick+1}/20)...")
        time.sleep(1)

    # ── Cliquer sur notre story (Your story) ───────────────────────────
    print(f"  👆 Tap sur 'Your story'...")
    story_tapped = False
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_hl_story.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_hl_story.xml").stdout

        for text in ["Your story", "your story"]:
            for pattern in [
                rf'content-desc="[^"]*{re.escape(text)}[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="[^"]*{re.escape(text)}[^"]*"',
                rf'text="[^"]*{re.escape(text)}[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="[^"]*{re.escape(text)}[^"]*"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    print(f"  ✅ 'Your story' trouvé ({cx},{cy}) — tap")
                    adb(device, f"shell input tap {cx} {cy}")
                    story_tapped = True
                    break
            if story_tapped:
                break

        if not story_tapped:
            res = adb(device, "shell wm size")
            m = re.search(r'(\d+)x(\d+)', res.stdout)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                fx, fy = int(w * 0.121), int(h * 0.117)
                print(f"  🎯 Fallback Your story ({fx},{fy})")
                adb(device, f"shell input tap {fx} {fy}")
                story_tapped = True
        if story_tapped:
            break
        print(f"  ⏳ Your story pas encore là ({tick+1}/10)...")
        time.sleep(1)

    _click_allow_if_present(device)

    # ── Attendre ouverture story ────────────────────────────────────────
    # ── Attendre ouverture story ────────────────────────────────────────
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_hl_story_open.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_hl_story_open.xml").stdout

        # Si on est passé aux stories des autres (plus de bouton Activity) → arrêt propre
        has_activity = any(kw in xml for kw in [
            "Activity", "activity",
            "com.instagram.android:id/activity_button",
        ])
        # Détecter si on est encore sur NOS stories (barre de progression en haut)
        has_our_story_ui = any(kw in xml for kw in [
            "Highlight", "highlight", "Added to", "Send to", "More", "more"
        ])

        if has_our_story_ui:
            print(f"  ✅ Story ouverte ({tick+1}s)")
            break

        if not has_activity and tick >= 2:
            print(f"  ℹ️ Plus de bouton Activity ({tick+1}s) → stories des autres → arrêt")
            adb(device, "shell input keyevent KEYCODE_BACK")
            return True

        print(f"  ⏳ Story pas encore ouverte ({tick+1}/10)...")

    # ── Vérifier "Added to" ─────────────────────────────────────────────
    # ── Vérifier "Added to" sur la story actuelle ───────────────────────
    adb(device, "shell uiautomator dump /sdcard/ui_hl_check_added.xml")
    time.sleep(0.4)
    xml_added = adb(device, "shell cat /sdcard/ui_hl_check_added.xml").stdout
    if "Added to" in xml_added or "added to" in xml_added.lower():
        print(f"  ℹ️ Story 1 déjà 'Added to' — attente passage story suivante (5s)...")

        # ── Vérifier si on est passé à la story suivante ─────────────────
        adb(device, "shell uiautomator dump /sdcard/ui_hl_next_story.xml")
        time.sleep(0.4)
        xml_next = adb(device, "shell cat /sdcard/ui_hl_next_story.xml").stdout

        # Si le bouton Activity n'est plus là → on est sur les stories des autres → fermer
        has_activity = any(kw in xml_next for kw in [
            "Activity", "activity",
            "com.instagram.android:id/activity_button",
            "com.instagram.android:id/story_activity",
        ])
        if not has_activity:
            print(f"  ✅ Plus de bouton Activity → toutes les stories traitées → fermeture")
            adb(device, "shell input keyevent KEYCODE_BACK")
            return True

        # On est sur la story suivante → vérifier si elle aussi est "Added to"
        if "Added to" in xml_next or "added to" in xml_next.lower():
            print(f"  ℹ️ Story 2 aussi déjà 'Added to' — toutes traitées")
            adb(device, "shell input keyevent KEYCODE_BACK")
            return True

        # Story suivante pas encore ajoutée → on continue le flow normalement
        print(f"  ▶️ Story suivante détectée, pas encore 'Added to' → on continue")

    # ── Chercher le bouton Highlight ────────────────────────────────────
    print(f"  🔍 Recherche bouton 'Highlight'...")
    highlight_clicked = False
    for tick in range(20):  # plus de tentatives
        adb(device, "shell uiautomator dump /sdcard/ui_hl_btn.xml")
        time.sleep(0.2)  # réduit de 0.5 à 0.2 — critique pour attraper Highlight avant que la story passe
        xml = adb(device, "shell cat /sdcard/ui_hl_btn.xml").stdout

        for text in ["Highlight", "highlight", "HIGHLIGHT"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
                rf'content-desc="[^"]*{re.escape(text)}[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="[^"]*{re.escape(text)}[^"]*"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    print(f"  ✅ 'Highlight' trouvé ({cx},{cy}) — tap")
                    adb(device, f"shell input tap {cx} {cy}")
                    highlight_clicked = True
                    break
            if highlight_clicked:
                break

        if not highlight_clicked:
            # Essayer via "..." en haut à droite
            for more_text in ["More", "more"]:
                for pattern in [
                    rf'content-desc="[^"]*{re.escape(more_text)}[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="[^"]*{re.escape(more_text)}[^"]*"',
                ]:
                    matches = re.findall(pattern, xml)
                    if matches:
                        x1, y1, x2, y2 = map(int, matches[0])
                        cx, cy = (x1+x2)//2, (y1+y2)//2
                        res2 = adb(device, "shell wm size")
                        m2 = re.search(r'(\d+)x(\d+)', res2.stdout)
                        if m2:
                            w2, h2 = int(m2.group(1)), int(m2.group(2))
                            if cx > w2 * 0.7 and cy < h2 * 0.15:
                                adb(device, f"shell input tap {cx} {cy}")
                                print(f"  🎯 '...' cliqué ({cx},{cy})")
                                time.sleep(1.5)
                                adb(device, "shell uiautomator dump /sdcard/ui_hl_menu.xml")
                                time.sleep(0.4)
                                xml_menu = adb(device, "shell cat /sdcard/ui_hl_menu.xml").stdout
                                for hl_text in ["Highlight", "highlight"]:
                                    for hl_pat in [
                                        rf'text="{re.escape(hl_text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                                        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(hl_text)}"',
                                    ]:
                                        hl_matches = re.findall(hl_pat, xml_menu)
                                        if hl_matches:
                                            _x1,_y1,_x2,_y2 = map(int, hl_matches[0])
                                            adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                                            print(f"  ✅ 'Highlight' dans menu")
                                            highlight_clicked = True
                                            break
                                    if highlight_clicked:
                                        break

        if highlight_clicked:
            break
        print(f"  ⏳ 'Highlight' pas encore là ({tick+1}/10)...")
        time.sleep(1)

    if not highlight_clicked:
        print(f"  ❌ Bouton 'Highlight' introuvable")
        adb(device, "shell input keyevent KEYCODE_BACK")
        return False

    time.sleep(2)
    _click_allow_if_present(device)

    # ── Attendre que l'interface highlight soit bien chargée ─────────────
    print(f"  🔍 Attente interface highlight (max 8s)...")
    xml_hl = ""
    for _iface_tick in range(16):
        adb(device, "shell uiautomator dump /sdcard/ui_hl_interface.xml")
        time.sleep(0.3)
        xml_hl = adb(device, "shell cat /sdcard/ui_hl_interface.xml").stdout
        all_texts_dbg = re.findall(r'text="([^"]+)"', xml_hl)
        print(f"  📋 Interface [{_iface_tick+1}/16] textes : {[t for t in all_texts_dbg if t.strip()][:20]}")
        # On attend que l'interface soit chargée = contient Add to highlights OU Name OU EditText
        _has_iface = any(kw in xml_hl for kw in [
            "Add to highlights", "Highlights", "New",
            "Name", "name", "Add a title",
            'class="android.widget.EditText"',
        ])
        if _has_iface:
            print(f"  ✅ Interface highlight chargée ({_iface_tick+1}s)")
            break
        time.sleep(0.2)

    all_texts = re.findall(r'text="([^"]+)"', xml_hl)
    print(f"  📋 Interface textes final : {[t for t in all_texts if t.strip()][:20]}")

    highlight_name_lower = highlight_name.lower()
    highlight_exists_in_ui = highlight_name_lower in xml_hl.lower()
    has_name_field = any(kw in xml_hl for kw in ["Name", "name", "Add a title"])
    has_existing_list = any(kw in xml_hl for kw in ["Add to highlights", "Highlights", "New"])

    print(f"  🔍 name_field={has_name_field} | existing_list={has_existing_list} | '{highlight_name}' in UI={highlight_exists_in_ui} | registry={already_created}")

    # ═══════════════════════════════════════════════════════
    # CAS 1 : Créer la story à la une
    # ═══════════════════════════════════════════════════════
    if (not already_created and not highlight_exists_in_ui) or has_name_field:
        print(f"  📝 CAS 1 : Création '{highlight_name}'")

        # Si EditText déjà visible → on est directement sur l'écran de création, pas besoin du "+"
        _edittext_already_visible = 'class="android.widget.EditText"' in xml_hl
        
        # Si on est sur la liste (avec ou sans has_existing_list détecté) → clic sur "+"
        # On tente le "+" si pas d'EditText visible ET pas de champ Name
        if not _edittext_already_visible and not has_name_field:
            print(f"  🔍 Pas d'EditText visible — tentative clic '+'...")
            print(f"  🔍 Liste détectée, '{highlight_name}' absent → clic '+'...")
            plus_clicked = False
            
            # Re-dump pour avoir le XML frais de l'interface "Add to highlights"
            adb(device, "shell uiautomator dump /sdcard/ui_hl_plus.xml")
            time.sleep(0.5)
            xml_hl_fresh = adb(device, "shell cat /sdcard/ui_hl_plus.xml").stdout
            
            # Debug : afficher tous les textes visibles
            all_texts_hl = re.findall(r'text="([^"]+)"', xml_hl_fresh)
            all_descs_hl = re.findall(r'content-desc="([^"]+)"', xml_hl_fresh)
            print(f"  📋 Textes interface: {[t for t in all_texts_hl if t.strip()][:20]}")
            print(f"  📋 Descs interface: {[d for d in all_descs_hl if d.strip()][:20]}")
            
            for pattern in [
                r'text="\+"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="\+"',
                r'content-desc="[^"]*New[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="[^"]*New[^"]*"',
                # Chercher le + via ImageButton cliquable en haut à droite
                r'class="android\.widget\.ImageButton"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            ]:
                matches = re.findall(pattern, xml_hl_fresh)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    # Le "+" est en haut à droite - vérifier position
                    res_chk = adb(device, "shell wm size")
                    m_chk = re.search(r'(\d+)x(\d+)', res_chk.stdout)
                    if m_chk:
                        w_chk, h_chk = int(m_chk.group(1)), int(m_chk.group(2))
                        # Accepter seulement si dans la moitié droite et moitié haute
                        if cx > w_chk * 0.5 and cy < h_chk * 0.5:
                            adb(device, f"shell input tap {cx} {cy}")
                            print(f"  ✅ '+' cliqué ({cx},{cy})")
                            plus_clicked = True
                            time.sleep(2)
                            break
            
            if not plus_clicked:
                # Fallback : chercher tous les éléments cliquables dans la zone haut-droite
                res3 = adb(device, "shell wm size")
                m3 = re.search(r'(\d+)x(\d+)', res3.stdout)
                if m3:
                    w3, h3 = int(m3.group(1)), int(m3.group(2))
                    # D'après screenshot image 2 : le "+" est à ~557/607 x ~945/1280
                    # Soit environ 0.79*w, 0.74*h pour l'interface "Add to highlights"
                    # Chercher tous les cliquables en haut droite
                    clickables_hl = re.findall(
                        r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                        xml_hl_fresh)
                    if not clickables_hl:
                        clickables_hl = re.findall(
                            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"',
                            xml_hl_fresh)
                    print(f"  📋 Cliquables disponibles ({len(clickables_hl)}):")
                    for coords in clickables_hl:
                        x1, y1, x2, y2 = map(int, coords)
                        cx, cy = (x1+x2)//2, (y1+y2)//2
                        bw, bh = x2-x1, y2-y1
                        print(f"    ({cx},{cy}) {bw}x{bh}")
                        # Le "+" = petit bouton carré en haut à droite
                        if (cx > w3 * 0.6 and cy < h3 * 0.55 
                                and 40 < bw < 180 and 40 < bh < 180):
                            adb(device, f"shell input tap {cx} {cy}")
                            print(f"  ✅ '+' cliqué via fallback cliquable ({cx},{cy})")
                            plus_clicked = True
                            time.sleep(2)
                            break
                    
                    if not plus_clicked:
                        # Dernier fallback : coordonnées proportionnelles basées sur screenshot
                        # Image 2 : "+" visible à droite de "Add to highlights"
                        fx_plus = int(w3 * 0.915)  # très à droite
                        fy_plus = int(h3 * 0.738)  # milieu bas de la bottom sheet
                        adb(device, f"shell input tap {fx_plus} {fy_plus}")
                        print(f"  🎯 Fallback '+' coordonnées proportionnelles ({fx_plus},{fy_plus})")
                        plus_clicked = True
                        time.sleep(2)
           # Attendre que l'interface "Add to highlight" (image 3) apparaisse
            print(f"  🔍 Attente interface 'Add to highlight' avec champ nom (max 5s)...")
            for _wait_new in range(10):
                adb(device, "shell uiautomator dump /sdcard/ui_hl_new.xml")
                time.sleep(0.5)
                xml_hl = adb(device, "shell cat /sdcard/ui_hl_new.xml").stdout
                # Image 3 : on voit "Add to highlight" + photo centrale + bouton "Add" bleu
                # Le champ nom (Highlights) est un EditText
                has_edittext = 'class="android.widget.EditText"' in xml_hl
                has_add_btn = any(kw in xml_hl for kw in ["Add", "ADD"])
                has_highlight_screen = "Add to highlight" in xml_hl or has_edittext
                print(f"  📋 Attente interface [{_wait_new+1}/10]: edittext={has_edittext}, add={has_add_btn}, screen={has_highlight_screen}")
                if has_edittext or has_highlight_screen:
                    print(f"  ✅ Interface 'Add to highlight' détectée ({_wait_new+1}s)")
                    break
                time.sleep(0.3)

        # Trouver le champ Name
        field_found = False
        for hint in ["Name", "name", "Add a title", "Story name"]:
            for pat in [
                rf'hint="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(hint)}"',
                rf'text="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(hint)}"',
            ]:
                matches = re.findall(pat, xml_hl)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  ✅ Champ '{hint}' cliqué")
                    field_found = True
                    time.sleep(0.8)
                    break
            if field_found:
                break
        if not field_found:
            edits = re.findall(
                r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_hl)
            if edits:
                x1, y1, x2, y2 = map(int, edits[0])
                adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                print(f"  🎯 Fallback EditText champ nom")
                field_found = True
                time.sleep(0.8)

        if field_found:
            adb(device, "shell input keyevent KEYCODE_CTRL_A")
            time.sleep(0.2)
            adb(device, "shell input keyevent KEYCODE_DEL")
            time.sleep(0.2)
            name_clean = highlight_name.replace(' ', '%s').replace("'", "")
            adb(device, f"shell input text '{name_clean}'")
            print(f"  ✅ Nom '{highlight_name}' saisi")
            time.sleep(0.5)
            adb(device, "shell input keyevent KEYCODE_BACK")
            time.sleep(0.5)

        # Cliquer Add
        adb(device, "shell uiautomator dump /sdcard/ui_hl_add.xml")
        time.sleep(0.5)
        xml_add = adb(device, "shell cat /sdcard/ui_hl_add.xml").stdout
        add_clicked = False
        for text in ["Add", "ADD", "Ajouter", "Done", "DONE"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
            ]:
                matches = re.findall(pattern, xml_add)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  ✅ 'Add' cliqué")
                    add_clicked = True
                    time.sleep(2)
                    break
            if add_clicked:
                break
        if not add_clicked:
            res4 = adb(device, "shell wm size")
            m4 = re.search(r'(\d+)x(\d+)', res4.stdout)
            if m4:
                w4, h4 = int(m4.group(1)), int(m4.group(2))
                adb(device, f"shell input tap {w4//2} {int(h4*0.85)}")
                print(f"  🎯 Fallback Add bas")
                time.sleep(2)

        _mark_highlight_created(phone_id, highlight_name)
        print(f"  ✅ CAS 1 terminé — '{highlight_name}' créée !")

    # ═══════════════════════════════════════════════════════
    # CAS 2 : Ajouter à une story à la une existante
    # ═══════════════════════════════════════════════════════
    else:
        print(f"  📝 CAS 2 : Ajout à '{highlight_name}' existante")

        adb(device, "shell uiautomator dump /sdcard/ui_hl_list.xml")
        time.sleep(0.5)
        xml_list = adb(device, "shell cat /sdcard/ui_hl_list.xml").stdout

        highlight_item_clicked = False
        for pattern in [
            rf'text="{re.escape(highlight_name)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(highlight_name)}"',
        ]:
            matches = re.findall(pattern, xml_list)
            if matches:
                x1, y1, x2, y2 = map(int, matches[0])
                adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                print(f"  ✅ '{highlight_name}' trouvé et cliqué")
                highlight_item_clicked = True
                time.sleep(1)
                break

        if not highlight_item_clicked:
            print(f"  ⚠️ '{highlight_name}' absent dans liste — clic premier item...")
            clickables = re.findall(
                r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_list)
            if not clickables:
                clickables = re.findall(
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"', xml_list)
            res5 = adb(device, "shell wm size")
            m5 = re.search(r'(\d+)x(\d+)', res5.stdout)
            w5, h5 = (int(m5.group(1)), int(m5.group(2))) if m5 else (1080, 2400)
            for coords in clickables:
                x1, y1, x2, y2 = map(int, coords)
                cx, cy = (x1+x2)//2, (y1+y2)//2
                bw, bh = x2-x1, y2-y1
                if 40 < bw < 200 and 40 < bh < 200 and h5*0.15 < cy < h5*0.75:
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  🎯 Premier item highlight ({cx},{cy})")
                    highlight_item_clicked = True
                    time.sleep(1)
                    break

        time.sleep(1)

        adb(device, "shell uiautomator dump /sdcard/ui_hl_add2.xml")
        time.sleep(0.5)
        xml_add2 = adb(device, "shell cat /sdcard/ui_hl_add2.xml").stdout
        add_clicked2 = False
        for text in ["Add", "ADD", "Ajouter", "Done", "DONE"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
            ]:
                matches = re.findall(pattern, xml_add2)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  ✅ 'Add' cliqué")
                    add_clicked2 = True
                    time.sleep(2)
                    break
            if add_clicked2:
                break
        if not add_clicked2:
            res6 = adb(device, "shell wm size")
            m6 = re.search(r'(\d+)x(\d+)', res6.stdout)
            if m6:
                w6, h6 = int(m6.group(1)), int(m6.group(2))
                adb(device, f"shell input tap {w6//2} {int(h6*0.85)}")
                print(f"  🎯 Fallback Add bas")
                time.sleep(2)

        print(f"  ✅ CAS 2 terminé — story ajoutée à '{highlight_name}' !")

    time.sleep(2)

    # ── Vérifier s'il reste des stories à traiter ───────────────────────
    print(f"  🔍 Vérification s'il reste des stories à traiter...")
    adb(device, "shell uiautomator dump /sdcard/ui_hl_after_add.xml")
    time.sleep(0.5)
    xml_after = adb(device, "shell cat /sdcard/ui_hl_after_add.xml").stdout

    has_activity_after = any(kw in xml_after for kw in [
        "Activity", "activity",
        "com.instagram.android:id/activity_button",
    ])

    if not has_activity_after:
        print(f"  ✅ Plus de bouton Activity → toutes les stories traitées → fermeture propre")
        adb(device, "shell input keyevent KEYCODE_BACK")
        print(f"  ✅ add_story_to_highlight terminé !")
        return True

    # Il reste potentiellement une autre story → attendre 5s et re-vérifier
    print(f"  ⏳ Bouton Activity encore présent → attente 5s pour voir si story suivante...")
    time.sleep(5)

    adb(device, "shell uiautomator dump /sdcard/ui_hl_after_wait.xml")
    time.sleep(0.5)
    xml_wait = adb(device, "shell cat /sdcard/ui_hl_after_wait.xml").stdout

    has_activity_wait = any(kw in xml_wait for kw in [
        "Activity", "activity",
        "com.instagram.android:id/activity_button",
    ])

    if not has_activity_wait:
        print(f"  ✅ Activity disparu après attente → stories des autres → fermeture propre")
        adb(device, "shell input keyevent KEYCODE_BACK")
        print(f"  ✅ add_story_to_highlight terminé !")
        return True

    # Toujours sur nos stories → vérifier Added to sur la story suivante
    if "Added to" in xml_wait or "added to" in xml_wait.lower():
        print(f"  ℹ️ Story suivante aussi 'Added to' → toutes traitées")
        adb(device, "shell input keyevent KEYCODE_BACK")
        print(f"  ✅ add_story_to_highlight terminé !")
        return True

    # Story suivante pas encore à la une → relancer le flow pour cette story
    print(f"  ▶️ Story suivante détectée, non traitée → relance du flow highlight...")
    # Re-appel récursif pour traiter la 2ème story
    # On ne relance pas Instagram (device déjà actif), on cherche directement Highlight
    highlight_clicked_2 = False
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_hl_btn2.xml")
        time.sleep(0.5)
        xml2 = adb(device, "shell cat /sdcard/ui_hl_btn2.xml").stdout
        for text in ["Highlight", "highlight"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
                rf'content-desc="[^"]*{re.escape(text)}[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="[^"]*{re.escape(text)}[^"]*"',
            ]:
                matches = re.findall(pattern, xml2)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  ✅ 'Highlight' story 2 cliqué")
                    highlight_clicked_2 = True
                    break
            if highlight_clicked_2:
                break
        if highlight_clicked_2:
            break
        print(f"  ⏳ Highlight story 2 pas encore là ({tick+1}/10)...")
        time.sleep(1)

    if highlight_clicked_2:
        time.sleep(2)
        _click_allow_if_present(device)

        # Même logique : détecter cas 1 ou 2 et ajouter
        adb(device, "shell uiautomator dump /sdcard/ui_hl_interface2.xml")
        time.sleep(0.5)
        xml_hl2 = adb(device, "shell cat /sdcard/ui_hl_interface2.xml").stdout
        already_created_2 = _has_highlight(phone_id, highlight_name)
        highlight_exists_in_ui_2 = highlight_name.lower() in xml_hl2.lower()
        has_name_field_2 = any(kw in xml_hl2 for kw in ["Name", "name", "Add a title"])
        has_existing_list_2 = any(kw in xml_hl2 for kw in ["Add to highlights", "Highlights", "New"])

        if (not already_created_2 and not highlight_exists_in_ui_2) or has_name_field_2:
            # CAS 1 pour story 2
            print(f"  📝 CAS 1 story 2 : Création '{highlight_name}'")
            if has_existing_list_2 and not has_name_field_2:
                # Re-dump frais
                adb(device, "shell uiautomator dump /sdcard/ui_hl_plus2.xml")
                time.sleep(0.5)
                xml_hl2_fresh = adb(device, "shell cat /sdcard/ui_hl_plus2.xml").stdout
                all_texts_hl2 = re.findall(r'text="([^"]+)"', xml_hl2_fresh)
                print(f"  📋 Textes story2: {[t for t in all_texts_hl2 if t.strip()][:20]}")
                
                plus2_clicked = False
                res_s2 = adb(device, "shell wm size")
                m_s2 = re.search(r'(\d+)x(\d+)', res_s2.stdout)
                w_s2, h_s2 = (int(m_s2.group(1)), int(m_s2.group(2))) if m_s2 else (1080, 2400)
                
                for pattern in [
                    r'text="\+"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="\+"',
                    r'content-desc="[^"]*New[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    r'class="android\.widget\.ImageButton"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                ]:
                    matches = re.findall(pattern, xml_hl2_fresh)
                    if matches:
                        x1, y1, x2, y2 = map(int, matches[0])
                        cx, cy = (x1+x2)//2, (y1+y2)//2
                        if cx > w_s2 * 0.5 and cy < h_s2 * 0.5:
                            adb(device, f"shell input tap {cx} {cy}")
                            print(f"  ✅ '+' story2 cliqué ({cx},{cy})")
                            plus2_clicked = True
                            time.sleep(2)
                            break
                
                if not plus2_clicked:
                    # Chercher cliquables haut-droite
                    clickables_s2 = re.findall(
                        r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                        xml_hl2_fresh)
                    if not clickables_s2:
                        clickables_s2 = re.findall(
                            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"',
                            xml_hl2_fresh)
                    for coords in clickables_s2:
                        x1, y1, x2, y2 = map(int, coords)
                        cx, cy = (x1+x2)//2, (y1+y2)//2
                        bw, bh = x2-x1, y2-y1
                        if (cx > w_s2 * 0.6 and cy < h_s2 * 0.55
                                and 40 < bw < 180 and 40 < bh < 180):
                            adb(device, f"shell input tap {cx} {cy}")
                            print(f"  ✅ '+' story2 via cliquable ({cx},{cy})")
                            plus2_clicked = True
                            time.sleep(2)
                            break
                    if not plus2_clicked:
                        fx2 = int(w_s2 * 0.915)
                        fy2 = int(h_s2 * 0.738)
                        adb(device, f"shell input tap {fx2} {fy2}")
                        print(f"  🎯 Fallback '+' story2 ({fx2},{fy2})")
                        time.sleep(2)
                
                # Attendre interface champ nom
                for _wait_new2 in range(10):
                    adb(device, "shell uiautomator dump /sdcard/ui_hl_new2.xml")
                    time.sleep(0.5)
                    xml_hl2 = adb(device, "shell cat /sdcard/ui_hl_new2.xml").stdout
                    has_edittext2 = 'class="android.widget.EditText"' in xml_hl2
                    has_hl_screen2 = "Add to highlight" in xml_hl2 or has_edittext2
                    print(f"  📋 Attente story2 [{_wait_new2+1}/10]: edittext={has_edittext2}")
                    if has_edittext2 or has_hl_screen2:
                        print(f"  ✅ Interface story2 détectée ({_wait_new2+1}s)")
                        break
                    time.sleep(0.3)

            edits2 = re.findall(
                r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_hl2)
            if edits2:
                x1, y1, x2, y2 = map(int, edits2[0])
                adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                time.sleep(0.8)
                adb(device, "shell input keyevent KEYCODE_CTRL_A")
                time.sleep(0.2)
                adb(device, "shell input keyevent KEYCODE_DEL")
                time.sleep(0.2)
                name_clean2 = highlight_name.replace(' ', '%s').replace("'", "")
                adb(device, f"shell input text '{name_clean2}'")
                time.sleep(0.5)
                adb(device, "shell input keyevent KEYCODE_BACK")
                time.sleep(0.5)
        else:
            # CAS 2 pour story 2
            print(f"  📝 CAS 2 story 2 : Ajout à '{highlight_name}'")
            for pattern in [
                rf'text="{re.escape(highlight_name)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(highlight_name)}"',
            ]:
                matches = re.findall(pattern, xml_hl2)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    time.sleep(1)
                    break

        # Cliquer Add pour story 2
        adb(device, "shell uiautomator dump /sdcard/ui_hl_add_s2.xml")
        time.sleep(0.5)
        xml_add_s2 = adb(device, "shell cat /sdcard/ui_hl_add_s2.xml").stdout
        for text in ["Add", "ADD", "Done", "DONE"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
            ]:
                matches = re.findall(pattern, xml_add_s2)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  ✅ 'Add' story 2 cliqué")
                    time.sleep(2)
                    break

        _mark_highlight_created(phone_id, highlight_name)
        print(f"  ✅ Story 2 ajoutée à la une '{highlight_name}' !")

    # Vérification finale : Activity encore présent ?
    time.sleep(2)
    adb(device, "shell uiautomator dump /sdcard/ui_hl_final.xml")
    time.sleep(0.4)
    xml_final = adb(device, "shell cat /sdcard/ui_hl_final.xml").stdout
    has_activity_final = any(kw in xml_final for kw in ["Activity", "activity"])
    if not has_activity_final:
        print(f"  ✅ Plus d'Activity → toutes les stories traitées → fermeture propre")
    else:
        print(f"  ℹ️ Activity encore visible → on ferme quand même (stories des autres probables)")

    adb(device, "shell input keyevent KEYCODE_BACK")
    print(f"  ✅ add_story_to_highlight terminé !")
    return True



def _click_allow_if_present(device: str) -> bool:
    """Détecte et clique sur ALLOW s'il est présent. Retourne True si cliqué."""
    try:
        adb(device, "shell uiautomator dump /sdcard/ui_allow_check.xml")
        time.sleep(0.4)
        xml = adb(device, "shell cat /sdcard/ui_allow_check.xml").stdout
        for text in ["ALLOW", "Allow", "Allow all", "ALLOW ALL",
                     "While using the app", "WHILE USING THE APP"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ ALLOW cliqué ({cx},{cy})")
                    time.sleep(1.0)
                    return True
    except Exception as e:
        print(f"  ⚠️ _click_allow_if_present erreur : {e}")
    return False

def _dismiss_sticker_popup(device: str) -> bool:
    """Ferme tout popup Instagram ayant un bouton 'Not now'. Retourne True si un popup a été fermé."""
    try:
        adb(device, "shell uiautomator dump /sdcard/ui_popup_check.xml")
        time.sleep(0.3)
        xml = adb(device, "shell cat /sdcard/ui_popup_check.xml").stdout
        for text in ["Not now", "NOT NOW", "Not Now"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  🚫 Popup fermé — 'Not now' ({(x1+x2)//2},{(y1+y2)//2})")
                    time.sleep(0.8)
                    return True
    except Exception as e:
        print(f"  ⚠️ _dismiss_sticker_popup erreur : {e}")
    return False

def _tap_next_or_continue(device: str, dump_file: str = "ui_next_cont.xml", max_ticks: int = 10) -> bool:
    """Cherche Next/Continue à chaque tick. Retourne True si cliqué."""
    # text= variantes + content-desc pour "Next →" (écran Edit video)
    _btn_texts    = ["Next", "NEXT", "Continue", "CONTINUE", "Continuer"]
    _btn_descs    = ["Next", "Continue"]
    _exclude_text = ["Edit video", "Edit Video"]
    for tick in range(max_ticks):
        _dismiss_sticker_popup(device)
        adb(device, f"shell uiautomator dump /sdcard/{dump_file}")
        time.sleep(0.5)
        xml = adb(device, f"shell cat /sdcard/{dump_file}").stdout
        found = False

        # Chercher par text= (exact ou préfixe "Next ")
        for text in _btn_texts:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
                # "Next →" ou "Next >" variantes
                rf'text="{re.escape(text)} [^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)} [^"]*"',
            ]:
                for coords in re.findall(pattern, xml):
                    x1, y1, x2, y2 = map(int, coords)
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    # Exclure le bouton "Edit video" (bas-gauche)
                    node_ctx = xml[max(0, xml.find(f'[{x1},{y1}]')-200):xml.find(f'[{x1},{y1}]')+50]
                    if any(ex in node_ctx for ex in _exclude_text):
                        continue
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ '{text}' cliqué ({cx},{cy})")
                    found = True
                    break
                if found:
                    break
            if found:
                return True

        # Chercher par content-desc= (Next → a souvent content-desc="Next")
        if not found:
            for desc in _btn_descs:
                for pattern in [
                    rf'content-desc="{re.escape(desc)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(desc)}"',
                ]:
                    for coords in re.findall(pattern, xml):
                        x1, y1, x2, y2 = map(int, coords)
                        cx, cy = (x1+x2)//2, (y1+y2)//2
                        node_ctx = xml[max(0, xml.find(f'[{x1},{y1}]')-200):xml.find(f'[{x1},{y1}]')+50]
                        if any(ex in node_ctx for ex in _exclude_text):
                            continue
                        adb(device, f"shell input tap {cx} {cy}")
                        print(f"  ✅ '{desc}' (content-desc) cliqué ({cx},{cy})")
                        found = True
                        break
                    if found:
                        break
                if found:
                    return True

        print(f"  ⏳ Next/Continue pas encore ({tick+1}/{max_ticks})...")
        time.sleep(1)
    print(f"  ⚠️ Next/Continue non trouvé après {max_ticks} ticks")
    return False

def _mark_highlight_created(phone_id, highlight_name):
    registry = _load_highlight_registry()
    pid = str(phone_id)
    if pid not in registry:
        registry[pid] = {}
    registry[pid][highlight_name] = True
    _save_highlight_registry(registry)
    print(f"  ✅ Highlight '{highlight_name}' marqué créé pour {phone_id}")
_pre_fetched_number = None  # (activation_id, number_formaté, provider) ou None
_pre_fetched_email  = None  # (email, mailId) pré-récupéré avant création GeeLark (mode email)
_pre_fetched_mail_id = None

# ─────────────────────────────────────────
#  POOL DE NUMÉROS PRÉ-RÉCUPÉRÉS
# ─────────────────────────────────────────
import threading as _threading
import collections as _collections

_number_pool_lock = _threading.Lock()
# Chaque entrée : {"activation_id": str, "number": str, "provider": str, "expires_at": float}
_number_pool = _collections.deque()
_number_pool_target_size = 3   # combien de numéros on veut en stock
_number_pool_running = False
_number_pool_thread = None
_pool_log_queue = _queue.Queue()   # logs du scraper → console dédiée
_pool_inventory_event = _threading.Event()  # force un refresh de l'affichage inventaire

NUMBER_VALIDITY_SEC = 14 * 60   # 14 min (marge de 1 min avant expiration réelle)

_pool_log_callback = None

def pool_log(msg: str):
    ts = time.strftime("%H:%M:%S")
    _pool_log_queue.put(f"[{ts}] {msg}")
    if _pool_log_callback:
        try:
            _pool_log_callback(msg)
        except Exception:
            pass

def pool_get_number():
    """
    Tire un numéro valide du pool.
    Retourne (activation_id, number, provider) ou None si pool vide.
    Supprime les numéros expirés au passage.
    """
    now = time.time()
    with _number_pool_lock:
        # Purger les expirés
        while _number_pool and _number_pool[0]["expires_at"] <= now:
            expired = _number_pool.popleft()
            pool_log(f"⏰ Numéro expiré supprimé : {expired['number']} ({expired['provider']})")
            _pool_inventory_event.set()

        if not _number_pool:
            return None

        entry = _number_pool.popleft()
        pool_log(f"✅ Numéro pioché dans le pool : {entry['number']} ({entry['provider']}) — {len(_number_pool)} restant(s)")
        _pool_inventory_event.set()
        return entry["activation_id"], entry["number"], entry["provider"]

def pool_add_number(activation_id: str, number: str, provider: str):
    """Ajoute un numéro au pool avec timestamp d'expiration."""
    entry = {
        "activation_id": activation_id,
        "number":        number,
        "provider":      provider,
        "expires_at":    time.time() + NUMBER_VALIDITY_SEC,
    }
    with _number_pool_lock:
        _number_pool.append(entry)
    pool_log(f"➕ Numéro ajouté au pool : {number} ({provider}) — expire dans {NUMBER_VALIDITY_SEC//60} min")
    _pool_inventory_event.set()

def pool_size() -> int:
    """Nombre de numéros valides dans le pool."""
    now = time.time()
    with _number_pool_lock:
        return sum(1 for e in _number_pool if e["expires_at"] > now)

def _pool_scraper_loop(target_size: int, stop_flag: list):
    """
    Thread daemon : maintient le pool à target_size numéros.
    Tourne en continu, même quand personne ne crée de compte.
    """
    pool_log(f"🚀 Pool scraper démarré (target={target_size})")
    while not stop_flag[0]:
        try:
            current = pool_size()
            if current < target_size:
                pool_log(f"🔍 Pool à {current}/{target_size} — recherche numéro...")
                import sys as _sys
                _saved_stdout = _sys.stdout
                _sys.stdout = _sys.__stdout__
                try:
                    result = get_hero_number()
                finally:
                    _sys.stdout = _saved_stdout
                if result:
                    activation_id, number, provider = result
                    formatted = format_number(number)
                    if formatted and not is_blacklisted(number):
                        pool_add_number(activation_id, formatted, provider)
                    else:
                        pool_log(f"⛔ Numéro invalide/blacklisté ({number}) — ignoré")
                else:
                    pool_log(f"⚠️ Aucun numéro disponible — retry dans 5s")
                    time.sleep(5)
                    continue
            else:
                # Pool plein — purge des expirés toutes les 30s
                time.sleep(30)
                now = time.time()
                with _number_pool_lock:
                    before = len(_number_pool)
                    while _number_pool and _number_pool[0]["expires_at"] <= now:
                        expired = _number_pool.popleft()
                        pool_log(f"⏰ Purge expiration : {expired['number']}")
                    after = len(_number_pool)
                if before != after:
                    _pool_inventory_event.set()
                continue

            # Petite pause entre chaque fetch pour ne pas spammer les APIs
            time.sleep(2)

        except Exception as e:
            pool_log(f"❌ Erreur pool scraper : {e}")
            time.sleep(10)

    pool_log("⛔ Pool scraper arrêté")

def start_pool_scraper(target_size: int = 3):
    """Démarre le thread de scraping de numéros en arrière-plan."""
    global _number_pool_running, _number_pool_thread, _number_pool_target_size
    if _number_pool_running:
        pool_log("ℹ️ Pool scraper déjà en cours")
        return
    _number_pool_target_size = target_size
    _number_pool_running = True
    _stop_flag = [False]
    _number_pool_thread = _threading.Thread(
        target=_pool_scraper_loop,
        args=(target_size, _stop_flag),
        daemon=True,
        name="PoolScraper"
    )
    _number_pool_thread.start()
    pool_log(f"✅ Pool scraper lancé (target={target_size})")
    return _stop_flag

# ── Pool d'emails (mode création email) ──────────────────────────────────────
_email_pool_lock    = _threading.Lock()
_email_pool         = _collections.deque()   # {"mail": str, "mail_id": str}
_email_pool_running = False
_email_pool_thread  = None

def pool_get_email():
    """Tire un email du pool. Retourne (email, mail_id) ou (None, None)."""
    with _email_pool_lock:
        if not _email_pool:
            return None, None
        entry = _email_pool.popleft()
        pool_log(f"✅ Email pioché dans le pool : {entry['mail']} — {len(_email_pool)} restant(s)")
        _pool_inventory_event.set()
        return entry["mail"], entry["mail_id"]

def pool_add_email(mail: str, mail_id: str):
    """Ajoute un email au pool."""
    with _email_pool_lock:
        _email_pool.append({"mail": mail, "mail_id": mail_id})
    pool_log(f"➕ Email ajouté au pool : {mail} — {len(_email_pool)} en stock")
    _pool_inventory_event.set()

def email_pool_size() -> int:
    with _email_pool_lock:
        return len(_email_pool)

_EMAIL_PARALLEL_CALLS = 5  # nombre de requêtes API simultanées par vague

def _email_pool_scraper_loop(target_size: int, stop_flag: list, parallel: int = _EMAIL_PARALLEL_CALLS):
    """Thread daemon : maintient le pool d'emails à target_size via appels parallèles."""
    from concurrent.futures import ThreadPoolExecutor
    pool_log(f"🚀 Email pool scraper démarré (target={target_size}, {parallel} appels //)")
    while not stop_flag[0]:
        try:
            # Pool déjà plein → ne pas gaspiller de crédits API, attendre
            _need = target_size - email_pool_size()
            if _need <= 0:
                time.sleep(0.5)
                continue
            # Lancer une vague d'appels parallèles (max `parallel`, mais pas plus que nécessaire)
            _wave = min(parallel, _need)
            pool_log(f"🔍 Email pool ({email_pool_size()}/{target_size}) — {_wave} appels Gmail //...")
            with ThreadPoolExecutor(max_workers=_wave) as _ex:
                _futures = [_ex.submit(get_smsbower_email) for _ in range(_wave)]
                for _f in _futures:
                    try:
                        mail, mail_id = _f.result()
                        if mail:
                            pool_add_email(mail, mail_id)
                    except Exception:
                        pass
            # Pas d'email dispo → retry immédiat (pas de délai)
        except Exception as e:
            pool_log(f"❌ Erreur email pool scraper : {e}")
            time.sleep(1)
    pool_log("⛔ Email pool scraper arrêté")

def start_email_pool_scraper(target_size: int = 3, parallel: int = _EMAIL_PARALLEL_CALLS):
    """Démarre le thread de scraping d'emails en arrière-plan."""
    global _email_pool_running, _email_pool_thread
    if _email_pool_running:
        pool_log("ℹ️ Email pool scraper déjà en cours")
        return
    _email_pool_running = True
    _stop_flag = [False]
    _email_pool_thread = _threading.Thread(
        target=_email_pool_scraper_loop,
        args=(target_size, _stop_flag, parallel),
        daemon=True,
        name="EmailPoolScraper"
    )
    _email_pool_thread.start()
    pool_log(f"✅ Email pool scraper lancé (target={target_size}, {parallel} appels //)")
    return _stop_flag

def _default_adb_path():
    # Windows : on cherche adb.exe dans le PATH (ou adb.exe à côté de l'exe).
    if sys.platform == "win32":
        found = _shutil.which("adb")
        if found:
            return found
        local = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "adb.exe")
        return local if os.path.exists(local) else "adb.exe"
    # macOS / Linux
    for cand in ("/opt/homebrew/bin/adb", "/usr/local/bin/adb", "/usr/bin/adb"):
        if os.path.exists(cand):
            return cand
    return _shutil.which("adb") or "adb"

ADB_PATH = _default_adb_path()
# Vide par défaut : les photos sont poussées via l'onglet Photo du panel.
# Peut être redéfini par la config (system.photos_dir) si besoin.
PHOTOS_BASE_DIR = ""
# Dossier "stock" des photos de profil (défini par le worker). Chaque worker y
# réserve atomiquement SA photo pour qu'aucun compte n'utilise la même.
PROFILE_STOCK_DIR = ""


def claim_profile_photo_from_stock():
    """
    Réserve atomiquement UNE photo du stock pour ce worker.
    Le rename dans le même dossier est atomique : si deux workers visent la même
    photo, un seul réussit, l'autre tombe en FileNotFoundError et prend la suivante.
    Retourne (chemin_réservé, nom_original) ou (None, None) si stock vide/absent.
    """
    import uuid as _uuid
    d = PROFILE_STOCK_DIR
    if not d or not os.path.isdir(d):
        return None, None
    try:
        _files = sorted(os.listdir(d))
    except Exception:
        return None, None
    for fn in _files:
        if fn.startswith(".") or ".claiming_" in fn:
            continue
        src = os.path.join(d, fn)
        if not os.path.isfile(src):
            continue
        claimed = src + f".claiming_{os.getpid()}_{_uuid.uuid4().hex[:6]}"
        try:
            os.rename(src, claimed)  # atomique
            return claimed, fn
        except (FileNotFoundError, OSError):
            continue  # déjà pris par un autre worker → suivant
    return None, None
NO_NUMBER_TIMEOUT_SEC = 60  # 5 minutes

SMSPIN_API_KEY  = ""
SMSPIN_COUNTRY  = ""
SMSPIN_APP      = ""
SMSPIN_ENABLED  = False
SMSPIN_DIAL_CODE = ""

SMSPIN_URL     = "https://api.smspinverify.com/user/get_number.php"
SMSPIN_SMS_URL = "https://api.smspinverify.com/user/get_sms.php"

HERO_API_KEY   = ""
HERO_SERVICE   = ""
HERO_COUNTRY   = ""
HEROSMS_ENABLED = False
HERO_MAX_PRICE  = 0
HERO_DIAL_CODE  = ""

PVAPINS_CUSTOMER  = ""
PVAPINS_ENABLED   = False
PVAPINS_SERVICE   = ""
PVAPINS_COUNTRY   = ""
PVAPINS_DIAL_CODE = ""
PVAPINS_URL_GET  = "https://api.pvapins.com/user/api/get_number.php"
PVAPINS_URL_SMS  = "https://api.pvapins.com/user/api/get_sms.php"
PVAPINS_APPS     = ["instagram", "instagram1", "instagram2", "instagram3", "instagram4",
                    "instagram13", "instagram44", "instagram45"]

EMAIL = "inkjbz@gmail.com"
FIRST_NAME = "Miahyvina"       # valeur de secours — remplacé par FIRST_NAMES si défini
FIRST_NAMES = ["Miahyvina"]    # liste de prénoms — un sera pris au hasard à la création
BIRTH_YEAR = "2004"
MIN_ACCOUNT_AGE_HOURS = 24     # délai min après création avant de poster
CREATION_MODE = "phone"        # "phone" ou "email" — mode de création de compte Instagram
ANDROID_VERSION = "Android 14" # "Android 13" ou "Android 14"

GEELARK_APP_ID  = "O8288LLB6STEX00ZMI4TWQQCSG"
GEELARK_API_KEY = "9GB0G2QPXIDVOBXA1S4VKAQ25Y7YSX"
GEELARK_BEARER  = "4X67XO9THLCP1AW6AO4XIFSW6XSRFJSG"
MENTION_TAG     = "@miaivvyy"
MENTION_TAGS    = ["@miaivvyy"]  # liste de tags — un sera pris au hasard au posting

DEBUG_MODE = False

all_phones     = []
started_phones = []

_debug_queue = None

def set_debug_queue(q):
    global _debug_queue
    _debug_queue = q

LOCATIONS_FRANCE = [
    ("new-york",        40.7128, -74.0060),
    ("los-angeles",     34.0522, -118.2437),
    ("chicago",         41.8781, -87.6298),
    ("houston",         29.7604, -95.3698),
    ("phoenix",         33.4484, -112.0740),
    ("philadelphia",    39.9526, -75.1652),
    ("san-antonio",     29.4241, -98.4936),
    ("san-diego",       32.7157, -117.1611),
    ("dallas",          32.7767, -96.7970),
    ("miami",           25.7617, -80.1918),
    ("atlanta",         33.7490, -84.3880),
    ("boston",          42.3601, -71.0589),
    ("seattle",         47.6062, -122.3321),
    ("denver",          39.7392, -104.9903),
    ("las-vegas",       36.1699, -115.1398),
]

used_cities = set()

def get_next_city():
    available = [c for c in LOCATIONS_FRANCE if c[0] not in used_cities]
    if not available:
        print("  Toutes les villes utilisées, réinitialisation...")
        used_cities.clear()
        available = LOCATIONS_FRANCE[:]
    city = random.choice(available)
    used_cities.add(city[0])
    return city

photo_folders      = []
photo_folder_index = 0

def adb(device, command, timeout=30):
    full_cmd = f'"{ADB_PATH}" -s {device} {command}'
    try:
        # encoding/errors explicites : sous Windows, text=True décode en cp1252 par
        # défaut et plante (UnicodeDecodeError) sur les emojis/caractères du XML UI.
        return subprocess.run(full_cmd, shell=True, capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        print(f"  ⚠️ ADB timeout ({timeout}s) : {command[:60]}")
        return subprocess.CompletedProcess(full_cmd, returncode=1, stdout="", stderr="timeout")


def safe_ui_dump(device, dump_file="/sdcard/ui_dump.xml", retries=4, settle=0.6):
    """
    Dump l'UI de façon FIABLE, avec retries — robuste sous forte charge
    (plusieurs instances en parallèle, où 'uiautomator dump' échoue souvent
    avec 'ERROR: could not get idle state!' ou renvoie un XML incomplet).
    Retourne le XML (str) valide, ou "" si échec total.
    """
    for attempt in range(retries):
        res = adb(device, f"shell uiautomator dump {dump_file}")
        out = ((res.stdout or "") + (res.stderr or "")).lower()
        if "error" in out or "null root" in out:
            # UI pas stabilisée → attendre plus longtemps et réessayer
            time.sleep(settle + 0.5 * attempt)
            continue
        time.sleep(settle)
        xml = adb(device, f"shell cat {dump_file}").stdout
        if xml and "bounds=" in xml and "<hierarchy" in xml:
            return xml
        time.sleep(settle + 0.4 * attempt)
    # Dernier recours : renvoyer ce qu'on peut lire, même imparfait
    return adb(device, f"shell cat {dump_file}").stdout or ""


def handle_refresh_page(device, xml):
    """
    Détecte la page d'erreur "Page isn't available right now" et clique sur
    'Refresh' si présent. Retourne True si un Refresh a été cliqué.
    À appeler dans toutes les boucles d'attente d'écran (elle peut surgir
    à tout moment pendant le chargement d'une interface).
    """
    if not any(kw in xml for kw in [
        "Page isn't available", "Page isn", "isn't available right now",
        "technical error", "Try reloading",
    ]):
        return False
    for _rf in ["Refresh", "REFRESH", "Réessayer", "Retry", "Try again", "Reload"]:
        for _rfp in [
            rf'text="{re.escape(_rf)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_rf)}"',
            rf'content-desc="{re.escape(_rf)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        ]:
            _rfm = re.findall(_rfp, xml)
            if _rfm:
                _x1, _y1, _x2, _y2 = map(int, _rfm[0])
                adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                print(f"  🔄 Page d'erreur — '{_rf}' cliqué, attente du rechargement...")
                time.sleep(2.5)
                return True
    # Page d'erreur détectée mais bouton introuvable → tap au centre-bas (zone bouton)
    _res = adb(device, "shell wm size")
    _m = re.search(r'(\d+)x(\d+)', _res.stdout)
    _w, _h = (int(_m.group(1)), int(_m.group(2))) if _m else (1080, 2340)
    adb(device, f"shell input tap {_w//2} {int(_h*0.87)}")
    print(f"  🔄 Page d'erreur (bouton non localisé) — tap zone Refresh ({_w//2},{int(_h*0.87)})")
    time.sleep(2.5)
    return True


def handle_notifications_popup(device, xml):
    """
    Détecte le popup "Your notifications are off" (Turn on / Not now) qui surgit
    à l'ouverture d'Instagram, et clique 'Not now' pour le fermer.
    Retourne True si le popup a été détecté et fermé.
    À appeler dans les flows (warmup, post, reel...) une fois le feed atteint.
    """
    if not any(kw in xml for kw in [
        "notifications are off", "Your notifications", "Turn on notifications",
        "Don't miss new likes", "Don’t miss new likes",
    ]):
        return False
    for _nn in ["Not now", "Not Now", "NOT NOW", "Pas maintenant", "Plus tard"]:
        for _nnp in [
            rf'text="{re.escape(_nn)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nn)}"',
            rf'content-desc="{re.escape(_nn)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        ]:
            _nnm = re.findall(_nnp, xml)
            if _nnm:
                _x1, _y1, _x2, _y2 = map(int, _nnm[0])
                adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                print(f"  🔔 Popup notifications — '{_nn}' cliqué")
                time.sleep(1.2)
                return True
    # Popup détecté mais bouton introuvable → tap zone basse du popup (Not now)
    _res = adb(device, "shell wm size")
    _m = re.search(r'(\d+)x(\d+)', _res.stdout)
    _w, _h = (int(_m.group(1)), int(_m.group(2))) if _m else (1080, 2340)
    adb(device, f"shell input tap {_w//2} {int(_h*0.66)}")
    print(f"  🔔 Popup notifications (bouton non localisé) — tap zone 'Not now'")
    time.sleep(1.2)
    return True


def still_on_name_screen(device):
    """
    True si on est ENCORE sur l'écran de saisie du nom (Full name / What's your name).
    Sert à détecter un compte bugué qui ne passe pas l'étape nom après Next.
    """
    xml = safe_ui_dump(device, "/sdcard/ui_name_stuck.xml")
    return any(kw in xml for kw in [
        "What's your name", "what's your name",
        "Full name", "full name",
        "Edit how you'll appear", "Edit how you",
    ])

def load_photo_folders():
    global photo_folders
    if not PHOTOS_BASE_DIR or not os.path.exists(PHOTOS_BASE_DIR):
        # Normal si les photos sont poussées via l'onglet Photo du panel
        photo_folders = []
        return
    photo_folders = [
        os.path.join(PHOTOS_BASE_DIR, d)
        for d in os.listdir(PHOTOS_BASE_DIR)
        if os.path.isdir(os.path.join(PHOTOS_BASE_DIR, d))
    ]
    random.shuffle(photo_folders)
    print(f"  {len(photo_folders)} dossiers de photos charges")

def get_next_photo_folder():
    global photo_folder_index, photo_folders

    # Nettoyer les dossiers supprimés
    photo_folders = [f for f in photo_folders if os.path.exists(f)]

    if not photo_folders:
        print("  ⚠️ Plus aucun dossier photo disponible — rechargement...")
        load_photo_folders()
        if not photo_folders:
            return None

    folder = photo_folders[photo_folder_index % len(photo_folders)]
    photo_folder_index += 1
    print(f"  📁 Dossier utilisé : {os.path.basename(folder)}")
    return folder


def get_number_from_pvapins():
    """Tente d'obtenir un numéro via PVAPins. Retourne (activation_id, number, 'pvapins') ou None."""
    for app in PVAPINS_APPS:
        try:
            response = requests.get(PVAPINS_URL_GET, params={
                "customer": PVAPINS_CUSTOMER,
                "country":  "France",
                "app":      app,
            }, timeout=15)
            print(f"  PVAPins [{app}] → {response.text.strip()[:80]}")
            data = response.json()
            number = data.get("number") or data.get("phone") or data.get("num")
            activation_id = str(data.get("id") or data.get("activation_id") or number)
            if number:
                if is_blacklisted(str(number)):
                    print(f"  ⛔ Numéro PVAPins blacklisté ({number}), app suivante...")
                    continue
                print(f"  ✅ [PVAPINS/{app}] Numéro obtenu : {number} (ID: {activation_id})")
                return activation_id, str(number), "pvapins"
        except Exception as e:
            print(f"  ⚠️ PVAPins [{app}] erreur : {e}")
    print(f"  ⚠️ PVAPins — aucun numéro disponible sur tous les apps instagram")
    return None




# ─────────────────────────────────────────
#  TELEGRAM + SCREENSHOT
# ─────────────────────────────────────────
def take_screenshot(device):
    try:
        ts     = int(time.time())
        remote = f"/sdcard/screenshot_{ts}.png"
        local  = os.path.join(_TMP_DIR, f"screenshot_{ts}.png")
        adb(device, f"shell screencap -p {remote}")
        time.sleep(1)
        subprocess.run(
            f'"{ADB_PATH}" -s {device} pull {remote} "{local}"',
            shell=True, capture_output=True
        )
        adb(device, f"shell rm {remote}")
        return local if os.path.exists(local) else None
    except Exception as e:
        print(f"  ⚠️ Erreur screenshot : {e}")
        return None

def telegram_send_photo(photo_path, caption=""):
    try:
        with open(photo_path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": f}, timeout=15
            )
        os.remove(photo_path)
    except Exception as e:
        print(f"  ⚠️ Erreur Telegram photo : {e}")

def telegram_send_message(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"  ⚠️ Erreur Telegram message : {e}")



def get_sms_from_pvapins(activation_id, number):
    """Attend le SMS via PVAPins. Retourne le code ou None."""
    print(f"  ⏳ Attente du SMS [PVAPINS]...")
    for i in range(5):
        try:
            response = requests.get(PVAPINS_URL_SMS, params={
                "customer": PVAPINS_CUSTOMER,
                "number":   number,
                "country":  "France",
                "app":      "instagram",
            }, timeout=10)
            print(f"  Status [PVAPINS] : {response.text.strip()[:80]}")
            data = response.json()
            code = data.get("sms") or data.get("code") or data.get("message")
            if code:
                # Extraire uniquement les chiffres si c'est un message complet
                import re as _re
                digits = _re.findall(r'\b(\d{4,8})\b', str(code))
                if digits:
                    print(f"  ✅ Code reçu [PVAPINS] : {digits[0]}")
                    return digits[0]
                print(f"  ✅ Code brut [PVAPINS] : {code}")
                return str(code)
        except Exception as e:
            print(f"  ⚠️ Erreur SMS PVAPins : {e}")
        time.sleep(6)
    print(f"  ❌ Timeout [PVAPINS], pas de SMS reçu")
    return None

def get_number_from_smspin():
    try:
        response = requests.get(SMSPIN_URL, params={
            "customer": SMSPIN_API_KEY,
            "app":      "1731",
            "country":  "USA",
            "number":   "",
            "duration": "15 minutes",
        }, timeout=15)
        text = response.text.strip()
        print(f"  SMSPin → {text[:80]}")

        if "No free" in text or "check after" in text or "Error" in text:
            print(f"  ⚠️ SMSPin — aucun numéro dispo")
            return None

        # Extraire le numéro — format SMSPin : "12025551234|token" ou juste le numéro
        number = text.split("|")[0].strip() if "|" in text else text.strip()
        if not number.lstrip("+").isdigit() or len(number) < 6:
            print(f"  ⚠️ SMSPin réponse invalide : {text[:60]}")
            return None

        print(f"  ✅ [SMSPIN] Numéro obtenu : {number}")
        return number, number, "smspin"

    except Exception as e:
        print(f"  ⚠️ SMSPin erreur : {e}")
        return None

def _detect_and_close_captcha(device):
    """Détecte le popup CAPTCHA instagram et appuie sur retour pour le fermer."""
    adb(device, "shell uiautomator dump /sdcard/ui.xml")
    time.sleep(0.5)
    result = adb(device, "shell cat /sdcard/ui.xml")
    xml = result.stdout

    captcha_keywords = [
        "drag the element",
        "most similar",
        "Please drag",
        "Skip",
        "arkose",
        "funcaptcha",
    ]
    
    if any(kw.lower() in xml.lower() for kw in captcha_keywords):
        print(f"  ⚠️ CAPTCHA détecté ! Retour arrière...")
        adb(device, "shell input keyevent KEYCODE_BACK")
        time.sleep(2)
        return True
    return False



def get_sms_from_smspin(number):
    print(f"  ⏳ Attente du SMS [SMSPIN USA]...")
    for i in range(5):
        try:
            response = requests.get(SMSPIN_SMS_URL, params={
                "customer": SMSPIN_API_KEY,
                "app":      "1731",
                "country":  "USA",
                "number":   number,
            }, timeout=10)
            text = response.text.strip()
            print(f"  Status [SMSPIN] : {text[:80]}")

            if text and not any(x in text for x in [
                "not received", "expired", "Error",
                "Waiting", "No free", "check after"
            ]):
                digits = re.findall(r'\b(\d{4,8})\b', text)
                if digits:
                    print(f"  ✅ Code reçu [SMSPIN] : {digits[0]}")
                    return digits[0]
                return text

        except Exception as e:
            print(f"  ⚠️ Erreur SMS SMSPin : {e}")
        time.sleep(4)
    print(f"  ❌ Timeout [SMSPIN]")
    return None


def push_photos_to_device(device, folder_path, base_photos_dir=None):
    """
    Pousse des photos vers l'appareil.
    Si le dossier est vide ou introuvable, le supprime et cherche dans d'autres dossiers.
    
    :param device: ID de l'appareil ADB
    :param folder_path: Chemin du dossier de photos initial
    :param base_photos_dir: Dossier parent contenant tous les sous-dossiers de photos
    """
    import shutil

    def try_push_from_folder(path):
        """Tente de pousser des photos depuis un dossier donné. Retourne True si succès."""
        if not path or not os.path.exists(path):
            print(f"  Dossier photos introuvable : {path}")
            return False

        photos = [
            f for f in os.listdir(path)
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        ][:4]

        if not photos:
            print(f"  Aucune photo dans {path}, suppression du dossier...")
            try:
                shutil.rmtree(path)
                print(f"  🗑️ Dossier supprimé : {os.path.basename(path)}")
            except Exception as e:
                print(f"  ⚠️ Erreur suppression dossier : {e}")
            return False

        print(f"  Photos trouvées : {photos}")
        adb(device, "shell rm -rf /sdcard/DCIM/instagram_photos")
        adb(device, "shell mkdir -p /sdcard/DCIM/instagram_photos")

        for photo in photos:
            local_path  = os.path.join(path, photo)
            remote_path = f"/sdcard/DCIM/instagram_photos/{photo}"
            result = subprocess.run(
                [ADB_PATH, "-s", device, "push", local_path, remote_path],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                print(f"  Photo envoyée : {photo}")
            else:
                print(f"  Erreur push {photo}: {result.stderr.strip()[:80]}")

        adb(device, "shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/DCIM/instagram_photos/")
        time.sleep(1)
        print(f"  {len(photos)} photos prêtes sur le téléphone")

        try:
            shutil.rmtree(path)
            print(f"  🗑️ Dossier supprimé : {os.path.basename(path)}")
        except Exception as e:
            print(f"  ⚠️ Erreur suppression dossier : {e}")

        return True

    # --- Tentative avec le dossier initial ---
    if try_push_from_folder(folder_path):
        return True

    # --- Si échec, on pioche dans les autres sous-dossiers du dossier parent ---
    if not base_photos_dir or not os.path.exists(base_photos_dir):
        print("  ❌ Aucun dossier parent fourni, impossible de chercher d'autres photos.")
        return False

    print(f"  🔍 Recherche d'autres dossiers dans : {base_photos_dir}")

    # Liste tous les sous-dossiers disponibles (sauf le dossier initial déjà tenté)
    subfolders = sorted([
        os.path.join(base_photos_dir, d)
        for d in os.listdir(base_photos_dir)
        if os.path.isdir(os.path.join(base_photos_dir, d))
        and os.path.join(base_photos_dir, d) != folder_path
    ])

    for subfolder in subfolders:
        print(f"  ➡️ Tentative avec : {os.path.basename(subfolder)}")
        if try_push_from_folder(subfolder):
            return True

    print("  ❌ Aucun dossier avec des photos trouvé.")
    return False

def geelark_request(method: str, api_path: str, body: dict = None) -> dict:
    url       = f"https://openapi.geelark.com{api_path}"
    timestamp = str(int(time.time() * 1000))
    trace_id  = uuid.uuid4().hex.upper()
    nonce     = trace_id[:6]
    sign_str  = GEELARK_APP_ID + trace_id + timestamp + nonce + GEELARK_API_KEY
    signature = hashlib.sha256(sign_str.encode("utf-8")).hexdigest().zfill(64).upper()
    headers = {
        "Content-Type": "application/json",
        "appId":        GEELARK_APP_ID,
        "traceId":      trace_id,
        "ts":           timestamp,
        "nonce":        nonce,
        "sign":         signature,
    }
    res = requests.request(method, url, headers=headers, json=body)
    res.raise_for_status()
    return res.json()


KEYCODE_MAP = {
    '0': '7', '1': '8', '2': '9', '3': '10',
    '4': '11', '5': '12', '6': '13', '7': '14',
    '8': '15', '9': '16'
}

def type_number_keycode(device, number):
    """Saisit un numéro via keycodes — zéro risque d'artefact."""
    # Vider le champ
    for _ in range(15):
        adb(device, "shell input keyevent KEYCODE_DEL")
        time.sleep(0.04)
    # Saisir chiffre par chiffre via keycode
    for digit in number:
        keycode = KEYCODE_MAP.get(digit)
        if keycode:
            adb(device, f"shell input keyevent {keycode}")
            time.sleep(0.07)
    print(f"  ✅ Numéro saisi via keycodes : {number}")

def get_raw_phones_debug():
    """Retourne les 3 premiers téléphones bruts de GeeLark pour diagnostiquer les champs disponibles."""
    result = geelark_request("POST", "/open/v1/phone/list", {"page": 1, "pageSize": 3})
    if result.get("code") != 0:
        return {"error": result}
    items = result.get("data", {}).get("items", [])
    return {"items": items, "keys": list(items[0].keys()) if items else []}

def get_all_phones():
    result = geelark_request("POST", "/open/v1/phone/list", {"page": 1, "pageSize": 100})
    if result.get("code") != 0:
        print(f"  Erreur API : {result}")
        return []
    phones = result.get("data", {}).get("items", [])
    phone_list = []
    for i, phone in enumerate(phones):
        phone_id     = phone.get("id")
        name         = phone.get("serialName", "?")
        serial       = phone.get("serialNo", "?")
        status       = phone.get("status", "?")
        status_label = {0: "ON", 1: "STARTING", 2: "OFF"}.get(status, "?")
        label        = f"{name} / {serial}"
        group_raw    = phone.get("profileGroup") or phone.get("groupName") or phone.get("group") or ""
        group        = (group_raw.get("name") if isinstance(group_raw, dict) else str(group_raw)).lower()
        if i == 0:
            print(f"  [DEBUG groupe] clés dispo: {list(phone.keys())} | group_raw={group_raw!r} | group={group!r}")
        phone_list.append({"id": phone_id, "name": name, "serial": serial, "label": label, "status": status_label, "group": group})
    # Mettre à jour le cache global
    global all_phones
    all_phones = phone_list
    return phone_list

def start_phone(phone_id):
    print(f"  Démarrage du téléphone {phone_id}...")
    result = geelark_request("POST", "/open/v1/phone/start", {"ids": [str(phone_id)]})
    if result.get("code") == 0:
        success = result.get("data", {}).get("successAmount", 0)
        if success > 0:
            details = result.get("data", {}).get("successDetails", [])
            url = details[0].get("url", "") if details else ""
            if url:
                print(f"  Téléphone démarré ! URL : {url}")
                print(f"__GEELARK_URL__:{phone_id}:{url}")
            else:
                print(f"  Téléphone démarré !")
            return True
        else:
            print(f"  Echec démarrage : {result.get('data', {}).get('failDetails', [])}")
            return False
    else:
        print(f"  Erreur API start : {result}")
        return False

def start_phone_with_retry(phone_id, max_attempts=10, delay_sec=30):
    """Tente de démarrer le téléphone jusqu'à max_attempts fois (phone exhaustion = attente)."""
    for attempt in range(1, max_attempts + 1):
        ok = start_phone(phone_id)
        if ok:
            return True
        print(f"  ⚠️ Démarrage échoué (tentative {attempt}/{max_attempts}) — retry dans {delay_sec}s...")
        time.sleep(delay_sec)
    print(f"  ❌ Impossible de démarrer {phone_id} après {max_attempts} tentatives")
    return False

def stop_phone(phone_id):
    print(f"  Arrêt du téléphone {phone_id}...")
    result = geelark_request("POST", "/open/v1/phone/stop", {"ids": [str(phone_id)]})
    if result.get("code") == 0:
        print(f"  Téléphone arrêté !")
    else:
        print(f"  Erreur arrêt : {result}")

def get_adb_info(phone_id):
    result = geelark_request("POST", "/open/v1/adb/getData", {"ids": [str(phone_id)]})
    if result.get("code") == 0:
        items = result.get("data", {}).get("items", [])
        for item in items:
            if item.get("code") == 0 and item.get("ip"):
                device = f"{item['ip']}:{item['port']}"
                pwd    = item["pwd"]
                print(f"  ADB info : {device} (pwd: {pwd})")
                return device, pwd
    return None, None


def wait_for_adb(phone_id, max_wait=150):
    print(f"  Attente ADB (max {max_wait}s)...")
    for i in range(max_wait // 5):
        time.sleep(5)
        device, pwd = get_adb_info(phone_id)
        if device:
            return device, pwd
        print(f"  Pas encore prêt... ({(i+1)*5}s)")
    print(f"  Timeout ADB — fermeture du téléphone {phone_id}...")
    try:
        stop_phone(phone_id)
        print(f"  ✅ Téléphone fermé : {phone_id}")
    except Exception as e:
        print(f"  ⚠️ Erreur suppression : {e}")
    return None, None


def set_gps(phone_id, lat, lon):
    result = geelark_request("POST", "/open/v1/phone/gps/set", {
        "list": [{"id": str(phone_id), "latitude": lat, "longitude": lon}]
    })
    return result.get("code") == 0

def apply_random_city_gps(phone_id):
    city_name, lat, lon = get_next_city()
    lat += random.uniform(-0.003, 0.003)
    lon += random.uniform(-0.003, 0.003)
    lat  = round(lat, 6)
    lon  = round(lon, 6)
    print(f"  GPS ville : {city_name} ({lat}, {lon})")
    ok = set_gps(phone_id, lat, lon)
    if ok:
        print(f"  GPS appliqué : {city_name}")
    else:
        print(f"  GPS échoué")
    return city_name, lat, lon

# APRÈS
def create_phone_profile(proxy_host, proxy_port, proxy_user, proxy_pass, proxy_type="socks5"):
    import string as _str
    _suffix = ''.join(random.choices(_str.ascii_lowercase, k=4)) + ''.join(random.choices('0123456789', k=2))
    _pre_username = f"mia{_suffix}"
    profile_name = f"{_pre_username} {datetime.now().strftime('%d/%m')}"
    proxy_str = f"{proxy_type}://{proxy_user}:{proxy_pass}@{proxy_host}:{proxy_port}"
    print(f"  Création du profil '{profile_name}' avec proxy {proxy_host}:{proxy_port}...")
    _model = "Galaxy S23" if ANDROID_VERSION == "Android 13" else "Galaxy S24"
    result = geelark_request("POST", "/open/v1/phone/addNew", {
        "mobileType": ANDROID_VERSION,
        "chargeMode": 0,
        "data": [{
            "profileName": profile_name,
            "proxyInformation": proxy_str,
            "mobileLanguage": "default",
            "profileGroup": "instagram",
            "surfaceBrandName": "Samsung",
            "surfaceModelName": _model,
        }]
    })
    if result.get("code") == 0:
        details = result.get("data", {}).get("details", [])
        if details and details[0].get("code") == 0:
            phone_id = details[0].get("id")
            _phone_usernames[str(phone_id)] = _pre_username
            print(f"  ✅ Profil créé ! ID : {phone_id} | Nom : {profile_name}")
            return phone_id
        else:
            print(f"  ❌ Erreur création détail : {details}")
            return None
    else:
        print(f"  ❌ Erreur création profil : {result}")
        return None

def enable_root(phone_id):
    print(f"  Activation du root...")
    result = geelark_request("POST", "/open/v1/root/setStatus", {
        "ids": [str(phone_id)],
        "open": True
    })
    if result.get("code") == 0:
        items = result.get("data", {}).get("items", [])
        for item in items:
            if item.get("code") == 0:
                print(f"  ✅ Root activé !")
                return True
            else:
                print(f"  ❌ Erreur root : {item.get('msg')}")
    else:
        print(f"  ❌ Erreur root API : {result}")
    return False

def enable_adb(phone_id):
    print(f"  Activation de l'ADB...")
    result = geelark_request("POST", "/open/v1/adb/setStatus", {
        "ids": [str(phone_id)],
        "open": True
    })
    if result.get("code") == 0:
        print(f"  ✅ ADB activé !")
        return True
    else:
        print(f"  ❌ Erreur ADB : {result}")
    return False


def enable_data_saver(device: str):
    """Active le Data Saver Android pour réduire la consommation proxy."""
    try:
        adb(device, "shell settings put global data_saver_enabled 1")
        adb(device, "shell settings put global low_power_data_usage 1")
        print(f"  ✅ Data Saver activé — consommation proxy réduite")
    except Exception as e:
        print(f"  ⚠️ Data Saver erreur : {e}")


SIM_AGGREGATOR_API_KEY  = ""
SIM_AGGREGATOR_URL      = "https://api.sim-aggregator.com/stubs/handler_api.php"
SIM_AGGREGATOR_COUNTRY  = ""
SIM_AGGREGATOR_SERVICE  = ""
SIM_AGGREGATOR_ENABLED  = False
SIM_AGGREGATOR_DIAL_CODE = ""

def get_number_from_sim_aggregator():
    try:
        response = requests.get(SIM_AGGREGATOR_URL, params={
            "action":  "getNumber",
            "key":     SIM_AGGREGATOR_API_KEY,
            "country": SIM_AGGREGATOR_COUNTRY,
            "service": SIM_AGGREGATOR_SERVICE,
        }, timeout=15)
        text = response.text.strip()
        print(f"  SimAggregator → {text}")

        if not text.startswith("ACCESS_NUMBER"):
            print(f"  ⚠️ SimAggregator pas de numéro : {text}")
            return None

        parts = text.split(":")
        activation_id = parts[1]
        number        = parts[2]

        # Vérification longueur max 9 caractères (numéro formaté sans préfixe)
        # Supprimer le préfixe 33 pour obtenir le numéro local à 9 chiffres
        clean = number.lstrip("+")
        if clean.startswith("33"):
            clean = clean[2:]
        elif clean.startswith("0"):
            clean = clean[1:]
        if len(clean) != 9:
            print(f"  ⚠️ Numéro invalide ({len(clean)} chiffres après nettoyage : {number}), annulation...")
            cancel_sim_aggregator_number(activation_id)
            return None

        if is_blacklisted(number):
            print(f"  ⛔ Numéro SimAggregator blacklisté ({number}), annulation...")
            cancel_sim_aggregator_number(activation_id)
            return None

        print(f"  ✅ [SIM_AGGREGATOR] Numéro obtenu : {number} (ID: {activation_id})")
        return activation_id, number, "sim_aggregator"

    except Exception as e:
        print(f"  ⚠️ SimAggregator erreur : {e}")
        return None


def cancel_sim_aggregator_number(activation_id):
    try:
        requests.get(SIM_AGGREGATOR_URL, params={
            "action":       "setStatus",
            "key":          SIM_AGGREGATOR_API_KEY,
            "activationId": activation_id,
            "status":       "8",
        }, timeout=10)
        print(f"  SimAggregator numéro {activation_id} annulé.")
    except:
        pass


def get_sms_from_sim_aggregator(activation_id):
    print(f"  ⏳ Attente du SMS [SIM_AGGREGATOR]...")
    for i in range(13):
        try:
            response = requests.get(SIM_AGGREGATOR_URL, params={
                "action":       "getStatus",
                "key":          SIM_AGGREGATOR_API_KEY,
                "activationId": activation_id,
            }, timeout=10)
            text = response.text.strip()
            print(f"  Status [SIM_AGGREGATOR] : {text}")

            if text.startswith("STATUS_OK"):
                code = text.split(":")[1]
                print(f"  ✅ Code reçu [SIM_AGGREGATOR] : {code}")
                return code

        except Exception as e:
            print(f"  ⚠️ Erreur SMS SimAggregator : {e}")
        time.sleep(10)

    print(f"  ❌ Timeout [SIM_AGGREGATOR]")
    return None



def parse_nstance(raw):
    parts = raw.strip().split(":")
    device = f"{parts[0]}:{parts[1]}"
    code = parts[2]
    return device, code

def wait_next(step_name):
    if not DEBUG_MODE:
        print(f"  ▶️  {step_name}")
        return True
    print(f"\n⏸  [{step_name}] En attente de commande debug...")
    if _debug_queue is None:
        val = input(f"⏎  [{step_name}] Entrée pour exécuter, autre lettre pour passer : ").strip()
        return val == ""
    while True:
        try:
            cmd = _debug_queue.get(timeout=600)
            if cmd == "continue":
                print(f"  ▶️  [{step_name}] → continué")
                return True
            elif cmd == "skip":
                print(f"  ⏭️  [{step_name}] → passé")
                return False
            elif cmd == "back":
                print(f"  ↩️  [{step_name}] → (back non supporté ici, on continue)")
                return True
            elif cmd == "stop":
                print(f"  ⏹  [{step_name}] → stop demandé")
                raise InterruptedError("Stop demandé depuis le terminal debug")
        except _queue.Empty:
            print(f"  ⏱️  Timeout debug sur [{step_name}] — exécution automatique")
            return True


def handle_verify_email_popup(device):
    """
    Détecte et gère la popup 'Verify Your Email'.
    Retourne True si popup détectée et traitée, False sinon.
    """
    adb(device, "shell uiautomator dump /sdcard/ui_email_verify.xml")
    time.sleep(0.5)
    result = adb(device, "shell cat /sdcard/ui_email_verify.xml")
    xml = result.stdout

    verify_keywords = [
        "verify your email",
        "enter email address",
        "send email",
        "verify instantly",
    ]
    if not any(kw in xml.lower() for kw in verify_keywords):
        return False

    print(f"  📧 Popup 'Verify Your Email' détectée — saisie email...")

    # Cliquer sur le champ email
    found = False
    for text in ["Enter Email Address", "Enter email address", "enter email address"]:
        matches = re.findall(
            rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        if not matches:
            matches = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"', xml)
        if not matches:
            matches = re.findall(
                rf'hint="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        if not matches:
            matches = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(text)}"', xml)
        if matches:
            x1, y1, x2, y2 = map(int, matches[0])
            cx, cy = (x1+x2)//2, (y1+y2)//2
            adb(device, f"shell input tap {cx} {cy}")
            print(f"  ✅ Champ email cliqué ({cx},{cy})")
            found = True
            time.sleep(1)
            break

    if not found:
        # Fallback coordonnées fixes basées sur la screenshot
        print(f"  ⚠️ Champ email non trouvé via XML — fallback coordonnées")
        adb(device, "shell input tap 300 545")
        time.sleep(1)

    # Vider le champ et saisir l'email
    adb(device, "shell input keyevent KEYCODE_CTRL_A")
    time.sleep(0.3)
    adb(device, "shell input keyevent KEYCODE_DEL")
    time.sleep(0.3)
    adb(device, f"shell input text 'inkjbz@gmail.com'")
    time.sleep(0.5)
    print(f"  ✅ Email saisi : inkjbz@gmail.com")

    # Cliquer sur SEND EMAIL
    found_send = False
    adb(device, "shell uiautomator dump /sdcard/ui_email_verify2.xml")
    time.sleep(0.4)
    result2 = adb(device, "shell cat /sdcard/ui_email_verify2.xml")
    xml2 = result2.stdout

    for text in ["SEND EMAIL", "Send Email", "Send email"]:
        matches = re.findall(
            rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml2)
        if not matches:
            matches = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"', xml2)
        if matches:
            x1, y1, x2, y2 = map(int, matches[0])
            cx, cy = (x1+x2)//2, (y1+y2)//2
            adb(device, f"shell input tap {cx} {cy}")
            print(f"  ✅ SEND EMAIL cliqué ({cx},{cy})")
            found_send = True
            time.sleep(1)
            break

    if not found_send:
        print(f"  ⚠️ SEND EMAIL non trouvé — fallback coordonnées")
        adb(device, "shell input tap 300 678")
        time.sleep(1)

    print(f"  ⏳ Attente 5s après envoi email...")
    time.sleep(5)
    print(f"  ✅ Popup 'Verify Your Email' traitée")
    return True



def click_button(device, texts):
    subprocess.run(
        f'"{ADB_PATH}" -s {device} shell uiautomator dump /sdcard/ui.xml',
        shell=True, capture_output=True
    )
    result = subprocess.run(
        f'"{ADB_PATH}" -s {device} shell cat /sdcard/ui.xml',
        shell=True, capture_output=True, text=True
    )
    for text in texts:
        matches = re.findall(rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', result.stdout)
        if not matches:
            matches = re.findall(rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"', result.stdout)
        if not matches:
            matches = re.findall(rf'text="[^"]*{re.escape(text)}[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', result.stdout)
        if not matches:
            matches = re.findall(rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="[^"]*{re.escape(text)}[^"]*"', result.stdout)
        if not matches:
            matches = re.findall(rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', result.stdout)
        if not matches:
            matches = re.findall(rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"', result.stdout)
        if not matches:
            matches = re.findall(rf'content-desc="[^"]*{re.escape(text)}[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', result.stdout)
        if not matches:
            matches = re.findall(rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="[^"]*{re.escape(text)}[^"]*"', result.stdout)
        if matches:
            x1, y1, x2, y2 = map(int, matches[0])
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            subprocess.run(f'"{ADB_PATH}" -s {device} shell input tap {cx} {cy}', shell=True)
            print(f"  ✅ '{text}' cliqué à ({cx}, {cy})")
            return True
    print(f"  ℹ️ Aucun bouton trouvé parmi : {texts}")
    return False

def type_text(device, text):
    subprocess.run(f'"{ADB_PATH}" -s {device} shell input text \'{text}\'', shell=True)
    print(f"  ✅ Texte saisi : {text}")

def tap(device, x, y):
    subprocess.run(f'"{ADB_PATH}" -s {device} shell input tap {x} {y}', shell=True)

def format_number(number):
    clean = number.strip()
    if not clean.lstrip("+").isdigit():
        print(f"  ❌ format_number reçu une valeur invalide : {clean[:40]}")
        return None

    if clean.startswith("+"):
        clean = clean[1:]

    # Supprimer le préfixe pays (du plus long au plus court)
    country_prefixes = {
        "34": 9,   # Espagne → 9 chiffres
        "32": 9,   # Belgique

        "33": 9,   # France → 9 chiffres
        "46": 9,   # Suède → 9 chiffres (parfois 7-9)
        "1":  10,  # USA → 10 chiffres
    }
    for prefix, expected_len in sorted(country_prefixes.items(), key=lambda x: -len(x[0])):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
            break
    else:
        if clean.startswith("0"):
            clean = clean[1:]

    if len(clean) < 7 or len(clean) > 10:
        print(f"  ❌ Numéro invalide après formatage ({len(clean)} chiffres) : {clean}")
        return None

    return clean

def random_birthdate():
    month = str(random.randint(1, 12)).zfill(2)
    day = str(random.randint(1, 28)).zfill(2)
    return month, day, BIRTH_YEAR

BLACKLISTED_PREFIXES = ['773',"745", "7599" "754", '774', '624', '748', "772" "771", "775", "776", "777", "778", "779", "744"]

def is_blacklisted(number: str) -> bool:
    clean = number.strip().lstrip('+').replace(' ', '').replace('-', '')
    country_prefixes = ['33', '1', '44', '49', '46', '34', '32']  # ← ajout de '46'
    normalized = clean
    for cp in country_prefixes:
        if clean.startswith(cp):
            normalized = clean[len(cp):]
            break
    return any(normalized.startswith(p) for p in BLACKLISTED_PREFIXES)

SMSBOWER_API_KEY  = ""
SMSBOWER_URL      = "https://smsbower.page/stubs/handler_api.php"
SMSBOWER_ENABLED  = False
SMSBOWER_SERVICE  = ""
SMSBOWER_COUNTRY  = ""
SMSBOWER_MAX_PRICE = 0
SMSBOWER_DIAL_CODE = ""
HERO_URL          = "https://hero-sms.com/stubs/handler_api.php"


def cancel_bower_number(activation_id):
    try:
        requests.get(SMSBOWER_URL, params={
            "api_key": SMSBOWER_API_KEY,
            "action":  "setStatus",
            "id":      activation_id,
            "status":  "8"
        })
        print(f"  Bower numéro {activation_id} annulé.")
    except:
        pass


def get_number_from_hero():
    for max_price in [HERO_MAX_PRICE]:
        try:
            response = requests.get(HERO_URL, params={
                "api_key":  HERO_API_KEY,
                "action":   "getNumberV2",
                "service":  HERO_SERVICE,
                "country":  HERO_COUNTRY,
                "maxPrice": max_price,
            }, timeout=10)
            print(f"  Hero SMS (maxPrice={max_price}) → {response.text}")
            data = response.json()
            activation_id = str(data.get("activationId", ""))
            number = str(data.get("phoneNumber", ""))
            operator = str(data.get("activationOperator", "")).lower()

            if not activation_id or not number:
                print(f"  ⚠️ Hero pas de numéro (maxPrice={max_price}) : {response.text}")
                continue  # essaie le prochain prix

            if is_blacklisted(number):
                print(f"  ⛔ Numéro Hero blacklisté ({number}), annulation...")
                cancel_bower_number(activation_id)
                continue

            print(f"  ✅ [HERO] Numéro obtenu (maxPrice={max_price}) : {number} (ID: {activation_id})")
            return activation_id, number, "hero"

        except Exception as e:
            print(f"  ⚠️ Hero erreur (maxPrice={max_price}) : {e}")

    return None



def _check_network_error(device: str) -> bool:
    """Retourne True si l'écran affiche 'Network connection unavailable'."""
    try:
        adb(device, "shell uiautomator dump /sdcard/ui_net.xml")
        time.sleep(0.3)
        result = adb(device, "shell cat /sdcard/ui_net.xml")
        xml = result.stdout.lower()
        return any(kw in xml for kw in [
            "network connection unavailable",
            "check that you have a data connection",
            "connexion réseau indisponible",
            "no internet connection",
        ])
    except:
        return False
    

def get_number_from_bower_v2():
    try:
        response = requests.get(SMSBOWER_URL, params={
            "api_key":           SMSBOWER_API_KEY,
            "action":            "getNumberV2",
            "service":           SMSBOWER_SERVICE,
            "country":           SMSBOWER_COUNTRY,
            "maxPrice":          SMSBOWER_MAX_PRICE,
            "exceptProviderIds": "3270",
        }, timeout=20)
        print(f"  SMSBower V2 → {response.text}")

        try:
            data = response.json()
        except Exception:
            print(f"  ⚠️ Bower V2 réponse non-JSON : {response.text[:80]}")
            return None

        activation_id = str(data.get("activationId", ""))
        number        = str(data.get("phoneNumber", ""))
        operator      = str(data.get("activationOperator", "")).lower()

        if not activation_id or not number:
            print(f"  ⚠️ Bower V2 pas de numéro : {response.text[:80]}")
            return None

        if is_blacklisted(number):
            print(f"  ⛔ Numéro Bower V2 blacklisté ({number}), annulation...")
            cancel_bower_number(activation_id)
            return None

        print(f"  ✅ [BOWER V2/3109] Numéro obtenu : {number} (ID: {activation_id}, opérateur: {operator})")
        return activation_id, number, "bower"

    except Exception as e:
        print(f"  ⚠️ Bower V2 erreur : {e}")
        return None


def get_hero_number():
    """Tente chaque provider SMS activé dans l'ordre de priorité configuré."""
    if SMSBOWER_ENABLED:
        result = get_number_from_bower_v2()
        if result:
            return result
    if HEROSMS_ENABLED:
        result = get_number_from_hero()
        if result:
            return result
    if SMSPIN_ENABLED:
        result = get_number_from_smspin()
        if result:
            return result
    if SIM_AGGREGATOR_ENABLED:
        result = get_number_from_sim_aggregator()
        if result:
            return result
    if PVAPINS_ENABLED:
        result = get_number_from_pvapins()
        if result:
            return result
    return None



def _block_instagram_images(device: str) -> bool:
    print(f"  🚫 Blocage SNI images instagram...")

    domains = [
        "images-ssl.goinstagram.com",
        "media.goinstagram.com",
        "goinstagram.map.fastly.net",
        "fastly.net",  # large mais efficace
    ]

    for domain in domains:
        adb(device, f'shell iptables -I OUTPUT -p tcp --dport 443 -m string --string "{domain}" --algo bm --from 0 --to 500 -j DROP')
        print(f"  ✅ SNI bloqué : {domain}")

    # Bloquer aussi les IPs Fastly connues (réseau 151.101.0.0/16)
    adb(device, "shell iptables -I OUTPUT -d 151.101.0.0/16 -j DROP")
    print(f"  ✅ Réseau Fastly bloqué : 151.101.0.0/16")

    print(f"  ✅ Blocage SNI actif")
    return True


def _unblock_instagram_images(device: str):
    """Remet à zéro les règles iptables ajoutées."""
    try:
        adb(device, "shell iptables -F OUTPUT")
        adb(device, "shell iptables -F INPUT")
        print(f"  ✅ iptables remis à zéro")
    except:
        pass




def _send_ban_telegram(device, phone_id, liked, noped, city="", reason=""):
    try:
        screenshot = take_screenshot(device)
        phone_label = str(phone_id) if phone_id else device
        caption = (
            f"🚫 <b>Compte BANNI</b>\n"
            f"📱 Téléphone : {phone_label}\n"
            f"❤️ Likes : {liked} | 👎 Nopes : {noped}\n"
            f"💬 Raison : {reason or 'ban détecté'}"
        )
        if screenshot:
            telegram_send_photo(screenshot, caption)
        else:
            telegram_send_message(caption)
    except Exception as e:
        print(f"  ⚠️ Erreur Telegram ban : {e}")

def open_instagram_after_media(device, phone_id=None, wait_sec=5):
    """
    Étape 1 : Ouvre Instagram après que les photos ont été envoyées sur l'appareil.
    Vérifie que la galerie contient bien des photos avant de lancer l'app.
    Retourne True si Instagram s'est ouvert correctement, False sinon.
    """
    print(f"  📸 Vérification présence photos dans /sdcard/DCIM/instagram_photos...")

    # ── Vérifier que le dossier photos existe et contient des fichiers ──────
    result = adb(device, "shell ls /sdcard/DCIM/instagram_photos/")
    files = [f.strip() for f in result.stdout.split('\n') if f.strip() and f.strip().lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))]
    
    if not files:
        print(f"  ⚠️ Aucune photo détectée dans /sdcard/DCIM/instagram_photos — on tente quand même")
    else:
        print(f"  ✅ {len(files)} photo(s) détectée(s) : {files}")


    _insta_kw = [
        "Get started", "Get Started", "Create new account", "Create New Account",
        "Log in", "Log In", "Log into another account",
        "Continue with", "Se connecter", "S'inscrire", "com.instagram.android",
    ]
    _allow_kw_list = ["Allow", "ALLOW", "allow"]

    def _launch_insta():
        adb(device, "shell am force-stop com.instagram.android")
        time.sleep(1)
        subprocess.run(
            f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
            f'-c android.intent.category.LAUNCHER 1',
            shell=True, capture_output=True, text=True, timeout=10
        )

    def _tap_allow_if_present(xml):
        for _at in _allow_kw_list:
            for _pat in [
                rf'text="{re.escape(_at)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_at)}"',
            ]:
                m = re.findall(_pat, xml)
                if m:
                    x1, y1, x2, y2 = map(int, m[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  ✅ Bouton ALLOW cliqué")
                    time.sleep(0.5)
                    return True
        return False

    # Premier lancement immédiat
    print(f"  📱 Lancement Instagram...")
    _launch_insta()

    # Boucle : relance toutes les 8s si Instagram n'est pas détecté
    print(f"  🔍 Attente Instagram (relance automatique si nécessaire)...")
    MAX_ATTEMPTS = 8
    for _tick in range(MAX_ATTEMPTS):
        time.sleep(3)  # laisser Instagram charger avant de dumper
        adb(device, "shell uiautomator dump /sdcard/ui_insta_check.xml")
        time.sleep(0.5)
        _xml = adb(device, "shell cat /sdcard/ui_insta_check.xml").stdout

        # Popup ALLOW → taper et re-scanner
        if _tap_allow_if_present(_xml):
            time.sleep(1)
            continue

        if any(kw in _xml for kw in _insta_kw):
            print(f"  ✅ Instagram détecté ({_tick+1}/{MAX_ATTEMPTS})")
            return True

        if any(kw in _xml.lower() for kw in ["app not installed", "n'est pas installée"]):
            print(f"  ❌ Instagram non installé")
            return False

        # Debug : afficher ce qui est à l'écran pour diagnostiquer
        _texts = re.findall(r'text="([^"]{3,})"', _xml)
        _visible = [t for t in _texts if t.strip()][:8]
        if _visible:
            print(f"  🔎 Écran actuel : {_visible}")
        else:
            print(f"  🔎 XML vide ou aucun texte détecté (longueur XML: {len(_xml)})")

        print(f"  ⏳ Instagram pas encore là ({_tick+1}/{MAX_ATTEMPTS}) — relance...")
        _launch_insta()

    print(f"  ❌ Instagram jamais détecté après {MAX_ATTEMPTS} tentatives")
    return False



def insta_step_next(device, silent_if_absent=False):
    """
    Clique sur Next. Utilisable à plusieurs endroits du flow.
    Retourne True si cliqué, False sinon.
    silent_if_absent : si True, n'affiche pas de message d'erreur quand 'Next'
                       est absent (cas normal, ex : code email auto-validé).
    """
    print(f"  🔍 Recherche bouton 'Next'...")
    adb(device, "shell uiautomator dump /sdcard/ui_insta_next.xml")
    time.sleep(0.5)
    xml = adb(device, "shell cat /sdcard/ui_insta_next.xml").stdout

    for text in ["Next", "next", "NEXT"]:
        for pattern in [
            rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
            rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"',
        ]:
            matches = re.findall(pattern, xml)
            if matches:
                x1, y1, x2, y2 = map(int, matches[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                adb(device, f"shell input tap {cx} {cy}")
                print(f"  ✅ 'Next' cliqué ({cx},{cy})")
                time.sleep(2)
                return True

    if silent_if_absent:
        print(f"  ⏭️ Pas de bouton 'Next' (validation auto) — on continue")
    else:
        print(f"  ❌ Bouton 'Next' non trouvé")
    return False


def _tap_code_field(device, xml):
    """Trouve et tape le champ de saisie du code. Retourne (cx, cy) ou None."""
    for hint in ["Enter code", "Code", "Confirmation code"]:
        for pattern in [
            rf'hint="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(hint)}"',
            rf'text="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(hint)}"',
        ]:
            m = re.findall(pattern, xml)
            if m:
                x1, y1, x2, y2 = map(int, m[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                adb(device, f"shell input tap {cx} {cy}")
                time.sleep(0.5)
                return cx, cy
    edits = re.findall(
        r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if edits:
        x1, y1, x2, y2 = map(int, edits[0])
        cx, cy = (x1+x2)//2, (y1+y2)//2
        adb(device, f"shell input tap {cx} {cy}")
        time.sleep(0.5)
        return cx, cy
    return None


def insta_step_email_confirmation_code(device, mail_id, get_new_email_fn=None, max_retries=3):
    """
    Attend le code de confirmation email via SMSBower, le saisit, clique Next.
    Si le code n'est pas 6 chiffres :
      - Back Android → clique sur le champ email → efface → nouveau mail via get_new_email_fn
      - Réessaie max_retries fois.
    Retourne (True, mail_id_final) si succès, (False, None) sinon.
    """
    _code_screen_kw = [
        "confirmation code", "Enter the code", "Enter code",
        "check your email", "Check your email",
    ]

    for attempt in range(max_retries):
        # ── Attendre l'écran du code ─────────────────────────────────────────
        print(f"  🔍 Attente écran code email (tentative {attempt+1}/{max_retries})...")
        screen_ok = False
        for tick in range(20):
            adb(device, "shell uiautomator dump /sdcard/ui_insta_code.xml")
            time.sleep(0.5)
            xml = adb(device, "shell cat /sdcard/ui_insta_code.xml").stdout
            if any(kw.lower() in xml.lower() for kw in _code_screen_kw):
                print(f"  ✅ Écran confirmation code ({tick+1}s)")
                screen_ok = True
                break
            print(f"  ⏳ Écran code pas encore là ({tick+1}/20)...")
            time.sleep(0.8)

        if not screen_ok:
            print(f"  ❌ Écran confirmation code jamais apparu")
            return False, None

        # ── Récupérer le code (4 polls × 8s = ~32s max) ─────────────────────
        code = get_smsbower_email_code(mail_id, max_polls=4)

        # Code absent OU invalide → même retry : back + nouveau mail
        digits = re.sub(r'\D', '', code or "")
        need_new_email = (not code) or (len(digits) != 6)
        if need_new_email:
            if not code:
                print(f"  ⚠️ Code non reçu après 4 polls — changement d'email...")
            else:
                print(f"  ⚠️ Code '{code}' invalide ({len(digits)} chiffres ≠ 6) — retry avec nouveau mail...")
            cancel_smsbower_email(mail_id)

            if get_new_email_fn is None or attempt >= max_retries - 1:
                print(f"  ❌ Pas de callback pour nouveau mail ou tentatives épuisées")
                return False, None

            # Back Android → retour écran email
            print(f"  🔙 Back vers écran email...")
            for _ in range(3):
                adb(device, "shell input keyevent KEYCODE_BACK")
                time.sleep(1.5)
                adb(device, "shell uiautomator dump /sdcard/ui_back_email.xml")
                time.sleep(0.4)
                xml_back = adb(device, "shell cat /sdcard/ui_back_email.xml").stdout
                if any(kw.lower() in xml_back.lower() for kw in [
                    "What's your email", "your email", "Email"
                ]):
                    break

            # Cliquer sur le champ email et vider complètement
            adb(device, "shell uiautomator dump /sdcard/ui_email_retry.xml")
            time.sleep(0.4)
            xml_em = adb(device, "shell cat /sdcard/ui_email_retry.xml").stdout
            edits = re.findall(
                r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_em)
            if edits:
                x1, y1, x2, y2 = map(int, edits[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                adb(device, f"shell input tap {cx} {cy}")
                time.sleep(0.3)
            # ── Vider TOUT le champ de façon fiable ──────────────────────────
            # Le tap tombe au milieu du texte → on déplace d'abord le curseur à la
            # FIN du mail (MOVE_END), puis on supprime de droite à gauche.
            # Comme ça aucun résidu, peu importe où le tap a atterri.
            adb(device, "shell input keyevent KEYCODE_MOVE_END")
            time.sleep(0.15)
            # 80 backspaces = largement assez pour n'importe quel email
            adb(device, "shell input keyevent " + " ".join(["KEYCODE_DEL"] * 80))
            time.sleep(0.15)
            # Sécurité : forward-delete au cas où il resterait du texte à droite
            adb(device, "shell input keyevent KEYCODE_MOVE_HOME")
            time.sleep(0.15)
            adb(device, "shell input keyevent " + " ".join(["KEYCODE_FORWARD_DEL"] * 80))
            time.sleep(0.3)

            # ── Vérification : le champ est-il bien vide ? ───────────────────
            adb(device, "shell uiautomator dump /sdcard/ui_email_cleared.xml")
            time.sleep(0.3)
            xml_cleared = adb(device, "shell cat /sdcard/ui_email_cleared.xml").stdout
            _resid = re.search(
                r'class="android\.widget\.EditText"[^>]*text="([^"]+)"', xml_cleared)
            if _resid and _resid.group(1).strip() and "@" in _resid.group(1):
                print(f"  ⚠️ Résidu détecté dans le champ : '{_resid.group(1)}' — nouveau nettoyage...")
                adb(device, "shell input keyevent KEYCODE_MOVE_END")
                time.sleep(0.15)
                adb(device, "shell input keyevent " + " ".join(["KEYCODE_DEL"] * 80))
                time.sleep(0.3)

            # Nouveau mail
            new_email, new_mail_id = get_new_email_fn()
            if not new_email:
                print(f"  ❌ Impossible d'obtenir un nouvel email")
                return False, None
            print(f"  📧 Nouveau mail : {new_email}")
            new_email_escaped = new_email.replace("@", "\\@")
            adb(device, f"shell input text '{new_email_escaped}'")
            time.sleep(0.5)
            insta_step_next(device)
            time.sleep(2)
            mail_id = new_mail_id
            continue  # retente avec le nouveau mail_id

        code = digits  # 6 chiffres extraits

        # ── Trouver le champ et saisir ───────────────────────────────────────
        adb(device, "shell uiautomator dump /sdcard/ui_insta_code2.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_insta_code2.xml").stdout
        pos = _tap_code_field(device, xml)
        if pos is None:
            print(f"  ❌ Aucun champ de code trouvé")
            return False, None
        print(f"  ✅ Champ code cliqué {pos}")

        adb(device, "shell input keyevent KEYCODE_CTRL_A")
        time.sleep(0.2)
        adb(device, "shell input keyevent KEYCODE_DEL")
        time.sleep(0.2)
        adb(device, f"shell input text '{code}'")
        print(f"  ✅ Code saisi : {code}")
        time.sleep(0.5)

        # Après saisie du code, Instagram valide souvent automatiquement → 'Next' absent = normal
        insta_step_next(device, silent_if_absent=True)
        return True, mail_id

    return False, None



def get_hero_sms(activation_id, provider="hero", number=None):
    """
    Attend le SMS selon le provider utilisé.
    provider : 'pvapins' | 'hero' | 'bower'
    """


    """if provider == "pvapins":
        return get_sms_from_pvapins(activation_id, number or activation_id)"""
    

    
    if provider == "smspin":
        return get_sms_from_smspin(number)

    if provider == "bower":
        url     = SMSBOWER_URL
        api_key = SMSBOWER_API_KEY
    else:
        url     = HERO_URL
        api_key = HERO_API_KEY

    print(f"  ⏳ Attente du SMS [{provider.upper()}]...")
    for i in range(5):
        try:
            response = requests.get(url, params={
                "api_key": api_key,
                "action":  "getStatus",
                "id":      activation_id
            }, timeout=10)
            print(f"  Status [{provider.upper()}] : {response.text}")
            if response.text.startswith("STATUS_OK"):
                code = response.text.split(":")[1]
                print(f"  ✅ Code reçu [{provider.upper()}] : {code}")
                return code
        except Exception as e:
            print(f"  ⚠️ Erreur status {provider} : {e}")
        time.sleep(6)
    print(f"  ❌ Timeout [{provider.upper()}], pas de SMS reçu")
    return None




SMSBOWER_MAIL_API = "https://smsbower.page/api/mail"
# SMSBOWER_API_KEY est défini ligne ~2083 et chargé depuis la config — ne pas redéfinir ici


def get_smsbower_email(service="ig", domain="gmail.com"):
    """
    Obtient un Gmail temporaire via SMSBower (service ig = Instagram uniquement).
    Retourne (email, mailId) ou (None, None).
    """
    services_to_try = [service]  # ig uniquement — pas de fallback ot (Any Email)
    for svc in services_to_try:
        try:
            response = requests.get(
                f"{SMSBOWER_MAIL_API}/getActivation",
                params={
                    "api_key": SMSBOWER_API_KEY,
                    "service": svc,
                    "domain":  domain,
                },
                timeout=15
            )
            pool_log(f"SMSBower mail [{svc}] → {response.text.strip()[:80]}")
            data = response.json()

            if data.get("status") == 1:
                mail    = data.get("mail")
                mail_id = data.get("mailId")
                pool_log(f"✅ Gmail obtenu : {mail} (mailId={mail_id}, service={svc})")
                return mail, mail_id

            err = data.get("error", "")
            pool_log(f"⚠️ SMSBower mail [{svc}] erreur : {err}")
            if "No mails yet" not in err:
                break  # erreur fatale (balance, clé, etc.) — pas la peine de réessayer

        except Exception as e:
            pool_log(f"⚠️ SMSBower mail exception : {e}")
            break

    return None, None


def _get_email_pool_or_api(max_attempts=5, wait_between=4):
    """
    Essaie d'obtenir un email : pool d'abord, puis API SMSBower avec retries.
    max_attempts : nombre total de tentatives API si le pool reste vide.
    wait_between : secondes d'attente entre chaque tentative.
    """
    # 1. Essai immédiat du pool
    mail, mail_id = pool_get_email()
    if mail:
        print(f"  ✅ Email pioché dans le pool")
        return mail, mail_id

    # 2. Tentatives API avec fallback pool entre chaque essai
    for attempt in range(1, max_attempts + 1):
        print(f"  🔄 Tentative email {attempt}/{max_attempts} (API SMSBower)...")
        mail, mail_id = get_smsbower_email()
        if mail:
            return mail, mail_id
        # Entre deux tentatives : attendre et re-checker le pool
        if attempt < max_attempts:
            print(f"  ⏳ Pas d'email dispo — attente {wait_between}s puis re-check pool...")
            time.sleep(wait_between)
            mail, mail_id = pool_get_email()
            if mail:
                print(f"  ✅ Email pioché dans le pool (après attente)")
                return mail, mail_id

    print(f"  ❌ Impossible d'obtenir un email après {max_attempts} tentatives")
    return None, None


def get_smsbower_email_code(mail_id, max_polls=7):
    print(f"  ⏳ Attente code email SMSBower (mailId={mail_id}, max={max_polls} polls)...")
    for i in range(max_polls):
        try:
            response = requests.get(
                f"{SMSBOWER_MAIL_API}/getCode",
                params={
                    "api_key": SMSBOWER_API_KEY,
                    "mailId":  mail_id,
                },
                timeout=10
            )
            print(f"  Code poll [{i+1}/{max_polls}] → {response.text.strip()[:80]}")
            data = response.json()

            if data.get("status") == 1:
                code = str(data.get("code", ""))
                digits = re.findall(r'\b(\d{4,8})\b', code)
                if digits:
                    print(f"  ✅ Code extrait : {digits[0]}")
                    return digits[0]
                print(f"  ✅ Code brut : {code}")
                return code

            err = data.get("error", "")
            if "canceled" in err.lower():
                print(f"  ❌ Activation annulée")
                return None

            if i < max_polls - 1:
                print(f"  ⏳ Pas encore reçu ({err}) — retry {i+2}/{max_polls}...")
            else:
                print(f"  ⏳ Pas encore reçu ({err}) — dernier poll épuisé")

        except Exception as e:
            print(f"  ⚠️ Poll erreur : {e}")

        if i < max_polls - 1:
            time.sleep(8)

    print(f"  ❌ Code non reçu après {max_polls} polls — changement d'email nécessaire")
    return None


def confirm_smsbower_email(mail_id):
    """Confirme la réception du code (débite le solde)."""
    try:
        requests.get(f"{SMSBOWER_MAIL_API}/setStatus", params={
            "api_key": SMSBOWER_API_KEY, "id": mail_id, "status": 3,
        }, timeout=10)
    except Exception:
        pass


def cancel_smsbower_email(mail_id):
    """Annule l'activation email (ne débite pas)."""
    try:
        requests.get(f"{SMSBOWER_MAIL_API}/setStatus", params={
            "api_key": SMSBOWER_API_KEY, "id": mail_id, "status": 2,
        }, timeout=10)
    except Exception:
        pass


def insta_step_switch_to_email(device, max_wait=25):
    """
    S'assure d'être sur l'écran 'What's your email?'.
    - Tape 'Allow' si un popup bloque l'écran.
    - Si déjà sur l'écran email → retourne True.
    - Sinon cherche 'Sign up with email address' / 'Sign up with email' et le tape.
    """
    _email_screen_kw = [
        "What's your email", "what's your email",
        "your email", "Enter the email",
        "Sign up with mobile number",
    ]
    # Textes exacts du bouton — le plus long d'abord pour éviter les faux positifs
    _switch_kw = [
        "Sign up with email address",
        "Sign up with email",
        "Use email address",
        "Use Email Address",
        "use email address",
        "Use an email address",
    ]
    _allow_kw = ["Allow", "ALLOW", "Autoriser", "OK", "Continue"]

    print(f"  🔄 Vérification / basculement vers écran email...")
    for tick in range(max_wait):
        xml = safe_ui_dump(device, "/sdcard/ui_switch_email.xml")

        # Déjà sur l'écran email ?
        if any(kw.lower() in xml.lower() for kw in _email_screen_kw):
            print(f"  ✅ Écran 'What\'s your email?' détecté ({tick+1}s)")
            return True

        # Page d'erreur "Page isn't available" → cliquer Refresh et re-scanner
        if handle_refresh_page(device, xml):
            continue

        # Popup ALLOW bloquant ? → taper et re-scanner immédiatement
        _allow_tapped = False
        for kw in _allow_kw:
            if _allow_tapped:
                break
            for pattern in [
                rf'text="{re.escape(kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(kw)}"',
            ]:
                m = re.findall(pattern, xml)
                if m:
                    x1, y1, x2, y2 = map(int, m[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  ✅ Popup '{kw}' fermé — re-scan...")
                    time.sleep(1.5)
                    _allow_tapped = True
                    break
        if _allow_tapped:
            continue

        # Chercher le bouton "Sign up with email..."
        tapped = False
        for kw in _switch_kw:
            # text= exact, content-desc=, ou recherche partielle dans l'attribut text
            patterns = [
                rf'text="{re.escape(kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(kw)}"',
                rf'content-desc="{re.escape(kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(kw)}"',
            ]
            for pattern in patterns:
                matches = re.findall(pattern, xml, re.IGNORECASE)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ '{kw}' tapé ({cx},{cy}) — attente écran email...")
                    time.sleep(2)
                    tapped = True
                    break
            if tapped:
                break

        if not tapped:
            print(f"  ⏳ Bouton email pas encore visible ({tick+1}/{max_wait})...")
            time.sleep(0.8)

    print(f"  ❌ Impossible d'atteindre l'écran 'What\'s your email?' après {max_wait}s")
    return False


def insta_step_enter_email(device, email):
    print(f"  🔍 Attente écran 'What's your email' pendant 20s...")

    for tick in range(20):
        adb(device, "shell uiautomator dump /sdcard/ui_insta_email.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_insta_email.xml").stdout

        screen_keywords = [
            "What's your email",
            "your email",
            "Email address",
        ]
        if not any(kw.lower() in xml.lower() for kw in screen_keywords):
            print(f"  ⏳ Écran email pas encore là ({tick+1}/20)...")
            time.sleep(0.8)
            continue

        print(f"  ✅ Écran email détecté ({tick+1}s)")

        field_found = False
        for hint in ["Email", "Email address", "email"]:
            for pattern in [
                rf'text="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(hint)}"',
                rf'hint="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(hint)}"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ Champ email cliqué ({cx},{cy})")
                    field_found = True
                    time.sleep(0.8)
                    break
            if field_found:
                break

        if not field_found:
            print(f"  ⚠️ Champ email non trouvé via XML — fallback EditText...")
            edits = re.findall(
                r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if edits:
                x1, y1, x2, y2 = map(int, edits[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                adb(device, f"shell input tap {cx} {cy}")
                print(f"  ✅ EditText cliqué ({cx},{cy})")
                time.sleep(0.8)
            else:
                print(f"  ❌ Aucun champ trouvé")
                return False

        adb(device, "shell input keyevent KEYCODE_CTRL_A")
        time.sleep(0.2)
        adb(device, "shell input keyevent KEYCODE_DEL")
        time.sleep(0.2)

        email_escaped = email.replace("@", "\\@")
        adb(device, f"shell input text '{email_escaped}'")
        print(f"  ✅ Email saisi : {email}")
        time.sleep(0.5)
        return True

    print(f"  ❌ Écran email jamais apparu")
    return False



def restart_phone(phone_id, wait_boot=20):
    print(f"  🔄 Redémarrage du téléphone {phone_id}...")
    stop_phone(phone_id)
    print(f"  ⏳ Attente extinction (5s)...")
    time.sleep(5)
    started = start_phone(phone_id)
    if not started:
        print(f"  ❌ Impossible de redémarrer le téléphone")
        return False
    print(f"  ⏳ Attente boot ({wait_boot}s)...")
    time.sleep(wait_boot)
    print(f"  ✅ Téléphone redémarré !")
    return True

def cancel_bower_number(activation_id):
    try:
        requests.get("https://hero-sms.com/stubs/handler_api.php", params={
            "api_key": HERO_API_KEY,
            "action":  "setStatus",
            "id":      activation_id,
            "status":  "8"
        })
        print(f"  Numéro {activation_id} annulé.")
    except:
        pass



def add_link_on_device(phone_id: str, link_url: str) -> bool:
    print(f"  🔗 Ajout lien → téléphone {phone_id} : {link_url[:50]}")
    if not check_account_age_warning(phone_id, "ajout de lien"):
        return False

    ok = start_phone_with_retry(phone_id)
    if not ok:
        return False
    time.sleep(15)

    enable_adb(phone_id)
    time.sleep(5)
    device, pwd = wait_for_adb(phone_id, max_wait=150)
    if not device:
        print(f"  ❌ ADB timeout pour {phone_id}")
        stop_phone(phone_id)
        return False

    connected = False
    for attempt in range(30):
        subprocess.run(f'"{ADB_PATH}" connect {device}', shell=True, capture_output=True)
        time.sleep(3)
        result = subprocess.run(
            f'"{ADB_PATH}" -s {device} shell glogin {pwd}',
            shell=True, capture_output=True, text=True
        )
        print(f"  glogin [{attempt+1}] → {result.stdout.strip()}")
        if "success" in result.stdout.lower():
            connected = True
            break
    if not connected:
        print(f"  ❌ glogin échoué pour {phone_id}")
        stop_phone(phone_id)
        return False

    # ── Ouvrir Instagram ──────────────────────────────────────────────────
    print(f"  📱 Ouverture Instagram...")
    adb(device, "shell am force-stop com.instagram.android")
    time.sleep(1)
    subprocess.run(
        f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
        f'-c android.intent.category.LAUNCHER 1',
        shell=True, capture_output=True, text=True
    )
    time.sleep(6)
    _click_allow_if_present(device)
    time.sleep(1)
    _click_allow_if_present(device)

    res = adb(device, "shell wm size")
    m = re.search(r'(\d+)x(\d+)', res.stdout)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)

    # ── Attendre feed ─────────────────────────────────────────────────────
    for tick in range(15):
        adb(device, "shell uiautomator dump /sdcard/ui_link_feed.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_link_feed.xml").stdout
        if _detect_logged_out_and_cleanup(device, phone_id, xml):
            stop_phone(phone_id)
            return False
        if any(kw in xml for kw in ["com.instagram.android", "Your story", "For you"]):
            print(f"  ✅ Feed détecté ({tick+1}s)")
            handle_notifications_popup(device, safe_ui_dump(device, "/sdcard/ui_notif_popup.xml"))
            break
        print(f"  ⏳ Attente feed ({tick+1}/15)...")
        time.sleep(1)

    # ── Aller sur le profil ───────────────────────────────────────────────
    print(f"  👤 Navigation vers le profil...")
    profile_clicked = False
    for tick in range(15):  # plus de tentatives
        adb(device, "shell uiautomator dump /sdcard/ui_hl_btn.xml")
        time.sleep(0.3)  # réduit de 0.5 à 0.3
        xml = adb(device, "shell cat /sdcard/ui_hl_btn.xml").stdout
        y_min = int(h * 0.85)
        x_min = int(w * 0.70)
        for desc in ["Profile", "Profil"]:
            for pat in [
                rf'content-desc="{re.escape(desc)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(desc)}"',
            ]:
                found = re.findall(pat, xml)
                for coords in found:
                    x1, y1, x2, y2 = map(int, coords)
                    if (y1+y2)//2 >= y_min and (x1+x2)//2 >= x_min:
                        adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                        print(f"  ✅ Profil cliqué ({(x1+x2)//2},{(y1+y2)//2})")
                        profile_clicked = True
                        break
                if profile_clicked:
                    break
            if profile_clicked:
                break
        if not profile_clicked:
            adb(device, f"shell input tap {int(w*0.92)} {int(h*0.965)}")
            print(f"  🎯 Fallback profil bas-droite")
        break
    time.sleep(3)

    # ── Cliquer Edit profile ──────────────────────────────────────────────
    print(f"  ✏️ Recherche bouton Edit profile...")
    edit_clicked = False
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_link_prof.xml")
        time.sleep(0.5)
        xml_prof = adb(device, "shell cat /sdcard/ui_link_prof.xml").stdout
        for kw in ["Edit profile", "Modifier le profil", "Edit Profile"]:
            for pat in [
                rf'text="{re.escape(kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(kw)}"',
            ]:
                found = re.findall(pat, xml_prof)
                if found:
                    x1, y1, x2, y2 = map(int, found[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  ✅ Edit profile cliqué ({(x1+x2)//2},{(y1+y2)//2})")
                    edit_clicked = True
                    break
            if edit_clicked:
                break
        if edit_clicked:
            break
        print(f"  ⏳ Edit profile pas encore là ({tick+1}/10)...")
        time.sleep(1)

    if not edit_clicked:
        print(f"  ❌ Edit profile introuvable")
        stop_phone(phone_id)
        return False
    time.sleep(2)

    # ── Cliquer Add link (premier bouton) ─────────────────────────────────
    print(f"  🔗 Recherche bouton 'Add link'...")
    addlink_clicked = False
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_link_edit.xml")
        time.sleep(0.5)
        xml_edit = adb(device, "shell cat /sdcard/ui_link_edit.xml").stdout
        for kw in ["Add link", "Add Link", "Ajouter un lien"]:
            for pat in [
                rf'text="{re.escape(kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(kw)}"',
                rf'content-desc="{re.escape(kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(kw)}"',
            ]:
                found = re.findall(pat, xml_edit)
                if found:
                    x1, y1, x2, y2 = map(int, found[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  ✅ 'Add link' cliqué ({(x1+x2)//2},{(y1+y2)//2})")
                    addlink_clicked = True
                    break
            if addlink_clicked:
                break
        if addlink_clicked:
            break
        print(f"  ⏳ 'Add link' pas encore là ({tick+1}/10)...")
        time.sleep(1)

    if not addlink_clicked:
        print(f"  ❌ 'Add link' introuvable")
        stop_phone(phone_id)
        return False
    time.sleep(2)

    # ── Cliquer Add link (second bouton dans la page dédiée) ──────────────
    print(f"  🔗 Recherche second bouton 'Add link'...")
    for tick in range(8):
        adb(device, "shell uiautomator dump /sdcard/ui_link_add2.xml")
        time.sleep(0.5)
        xml_add2 = adb(device, "shell cat /sdcard/ui_link_add2.xml").stdout
        addlink2_clicked = False
        for kw in ["Add link", "Add Link", "Ajouter un lien"]:
            for pat in [
                rf'text="{re.escape(kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(kw)}"',
                rf'content-desc="{re.escape(kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(kw)}"',
            ]:
                found = re.findall(pat, xml_add2)
                if found:
                    x1, y1, x2, y2 = map(int, found[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  ✅ Second 'Add link' cliqué ({(x1+x2)//2},{(y1+y2)//2})")
                    addlink2_clicked = True
                    break
            if addlink2_clicked:
                break
        if addlink2_clicked:
            break
        print(f"  ⏳ Second 'Add link' pas encore là ({tick+1}/8)...")
        time.sleep(1)
    time.sleep(2)

    # ── Cliquer sur le champ URL ──────────────────────────────────────────
    print(f"  🖊️ Recherche champ URL...")
    adb(device, "shell uiautomator dump /sdcard/ui_link_url.xml")
    time.sleep(0.5)
    xml_url = adb(device, "shell cat /sdcard/ui_link_url.xml").stdout

    url_field_clicked = False
    for hint in ["URL", "url", "Link URL", "Add URL", "Enter URL"]:
        for pat in [
            rf'hint="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(hint)}"',
            rf'text="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(hint)}"',
        ]:
            found = re.findall(pat, xml_url)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                print(f"  ✅ Champ URL cliqué ({(x1+x2)//2},{(y1+y2)//2})")
                url_field_clicked = True
                time.sleep(0.8)
                break
        if url_field_clicked:
            break

    if not url_field_clicked:
        # Fallback : premier EditText de la page
        edits = re.findall(
            r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml_url)
        if edits:
            x1, y1, x2, y2 = map(int, edits[0])
            adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
            print(f"  🎯 Fallback EditText URL ({(x1+x2)//2},{(y1+y2)//2})")
            time.sleep(0.8)
        else:
            print(f"  ⚠️ Champ URL non trouvé — fallback coords")
            adb(device, f"shell input tap {w//2} {int(h*0.35)}")
            time.sleep(0.8)

    # ── Saisir l'URL ──────────────────────────────────────────────────────
    adb(device, "shell input keyevent KEYCODE_CTRL_A")
    time.sleep(0.2)
    adb(device, "shell input keyevent KEYCODE_DEL")
    time.sleep(0.2)
    url_escaped = link_url.replace("'", "").replace(" ", "%s")
    adb(device, f"shell input text '{url_escaped}'")
    print(f"  ✅ URL saisie : {link_url}")
    time.sleep(0.8)

    # ── Cliquer sur le champ Title ────────────────────────────────────────
    print(f"  🖊️ Recherche champ Title...")
    adb(device, "shell uiautomator dump /sdcard/ui_link_title.xml")
    time.sleep(0.5)
    xml_title = adb(device, "shell cat /sdcard/ui_link_title.xml").stdout

    title_field_clicked = False
    for hint in ["Title", "title", "Link title", "Add title"]:
        for pat in [
            rf'hint="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(hint)}"',
            rf'text="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(hint)}"',
        ]:
            found = re.findall(pat, xml_title)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                print(f"  ✅ Champ Title cliqué ({(x1+x2)//2},{(y1+y2)//2})")
                title_field_clicked = True
                time.sleep(0.8)
                break
        if title_field_clicked:
            break

    if not title_field_clicked:
        # Fallback : second EditText
        edits = re.findall(
            r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml_title)
        if len(edits) >= 2:
            x1, y1, x2, y2 = map(int, edits[1])
            adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
            print(f"  🎯 Fallback second EditText Title")
            time.sleep(0.8)
        else:
            print(f"  ⚠️ Champ Title non trouvé — fallback coords")
            adb(device, f"shell input tap {w//2} {int(h*0.50)}")
            time.sleep(0.8)

    # ── Saisir le titre ───────────────────────────────────────────────────
    adb(device, "shell input keyevent KEYCODE_CTRL_A")
    time.sleep(0.2)
    adb(device, "shell input keyevent KEYCODE_DEL")
    time.sleep(0.2)
    adb(device, "shell input text 'lien'")
    print(f"  ✅ Title saisi : lien")
    time.sleep(0.8)

    # ── Fermer le clavier ─────────────────────────────────────────────────
    adb(device, "shell input keyevent KEYCODE_BACK")
    time.sleep(0.5)

    # ── Valider avec le checkmark en haut à droite ────────────────────────
    print(f"  ✅ Recherche bouton validation...")
    adb(device, "shell uiautomator dump /sdcard/ui_link_validate.xml")
    time.sleep(0.5)
    xml_val = adb(device, "shell cat /sdcard/ui_link_validate.xml").stdout

    validated = False
    for val_kw in ["Done", "DONE", "Save", "SAVE", "✓", "Enregistrer"]:
        for val_pat in [
            rf'text="{re.escape(val_kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(val_kw)}"',
            rf'content-desc="{re.escape(val_kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(val_kw)}"',
        ]:
            found = re.findall(val_pat, xml_val)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                print(f"  ✅ Validation '{val_kw}' cliquée ({(x1+x2)//2},{(y1+y2)//2})")
                validated = True
                break
        if validated:
            break

    if not validated:
        # Fallback checkmark haut-droite
        adb(device, f"shell input tap {int(w*0.85)} {int(h*0.06)}")
        print(f"  🎯 Fallback validation haut-droite")
    time.sleep(2)

    print(f"  ⏹ Arrêt téléphone {phone_id}...")
    stop_phone(phone_id)
    return True


def add_bio_on_device(phone_id: str, bio: str) -> bool:
    """
    Ouvre Instagram, va sur le profil, clique Add your bio,
    saisit la bio et valide avec le bouton en haut à droite (checkmark).
    """
    print(f"  📝 Ajout bio → téléphone {phone_id}")
    if not check_account_age_warning(phone_id, "ajout de bio"):
        return False

    # ── 1. Démarrer le téléphone ──────────────────────────────────────
    ok = start_phone_with_retry(phone_id)
    if not ok:
        return False
    time.sleep(15)

    # ── 2. Activer ADB ────────────────────────────────────────────────
    enable_adb(phone_id)
    time.sleep(5)

    # ── 3. Attendre ADB ───────────────────────────────────────────────
    device, pwd = wait_for_adb(phone_id, max_wait=150)
    if not device:
        print(f"  ❌ ADB timeout pour {phone_id}")
        stop_phone(phone_id)
        return False

    # ── 4. Connexion glogin ───────────────────────────────────────────
    connected = False
    for attempt in range(30):
        subprocess.run(f'"{ADB_PATH}" connect {device}', shell=True, capture_output=True)
        time.sleep(3)
        result = subprocess.run(
            f'"{ADB_PATH}" -s {device} shell glogin {pwd}',
            shell=True, capture_output=True, text=True
        )
        print(f"  glogin [{attempt+1}] → {result.stdout.strip()}")
        if "success" in result.stdout.lower():
            connected = True
            break
    if not connected:
        print(f"  ❌ glogin échoué pour {phone_id}")
        stop_phone(phone_id)
        return False

    # ── 5. Ouvrir Instagram ───────────────────────────────────────────
    print(f"  📱 Ouverture Instagram...")
    adb(device, "shell am force-stop com.instagram.android")
    time.sleep(1)
    subprocess.run(
        f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
        f'-c android.intent.category.LAUNCHER 1',
        shell=True, capture_output=True, text=True
    )
    time.sleep(6)
    _click_allow_if_present(device)
    time.sleep(1)
    _click_allow_if_present(device)

    # ── 6. Aller sur le profil (onglet bas droite) ────────────────────
    print(f"  👤 Navigation vers le profil...")
    res = adb(device, "shell wm size")
    m = re.search(r'(\d+)x(\d+)', res.stdout)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)

    # Attendre que le feed soit chargé
    for tick in range(15):
        adb(device, "shell uiautomator dump /sdcard/ui_bio_feed.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_bio_feed.xml").stdout
        if _detect_logged_out_and_cleanup(device, phone_id, xml):
            stop_phone(phone_id)
            return False
        if any(kw in xml for kw in ["com.instagram.android", "Your story", "For you"]):
            print(f"  ✅ Feed détecté ({tick+1}s)")
            handle_notifications_popup(device, safe_ui_dump(device, "/sdcard/ui_notif_popup.xml"))
            break
        print(f"  ⏳ Attente feed ({tick+1}/15)...")
        time.sleep(1)

    # Cliquer sur l'onglet profil (bas droite)
    profile_clicked = False
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_bio_nav.xml")
        time.sleep(0.5)
        xml_nav = adb(device, "shell cat /sdcard/ui_bio_nav.xml").stdout

        # Chercher l'onglet profil
        y_min = int(h * 0.85)
        x_min = int(w * 0.70)
        for desc in ["Profile", "Profil"]:
            for pat in [
                rf'content-desc="{re.escape(desc)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(desc)}"',
            ]:
                found = re.findall(pat, xml_nav)
                for coords in found:
                    x1, y1, x2, y2 = map(int, coords)
                    cy = (y1+y2)//2
                    cx = (x1+x2)//2
                    if cy >= y_min and cx >= x_min:
                        adb(device, f"shell input tap {cx} {cy}")
                        print(f"  ✅ Onglet profil cliqué ({cx},{cy})")
                        profile_clicked = True
                        break
                if profile_clicked:
                    break
            if profile_clicked:
                break

        if not profile_clicked:
            # Fallback : coin bas droite
            adb(device, f"shell input tap {int(w*0.92)} {int(h*0.965)}")
            print(f"  🎯 Fallback profil bas-droite")
            profile_clicked = True
        break

    time.sleep(3)

    # ── 7. Chercher "Add your bio" ou "Add picture" ───────────────────
    print(f"  🔍 Recherche bouton 'Add your bio'...")
    bio_btn_clicked = False

    for tick in range(15):
        adb(device, "shell uiautomator dump /sdcard/ui_bio_profile.xml")
        time.sleep(0.5)
        xml_prof = adb(device, "shell cat /sdcard/ui_bio_profile.xml").stdout

        # Liste exhaustive des textes possibles pour le bouton bio
        bio_btn_keywords = [
            "Add your bio",
            "Add bio",
            "add your bio",
            "add bio",
            "ADD YOUR BIO",
            "Add a bio",
            "Ajouter une bio",
        ]

        for kw in bio_btn_keywords:
            for pat in [
                rf'text="{re.escape(kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(kw)}"',
                rf'content-desc="{re.escape(kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(kw)}"',
            ]:
                found = re.findall(pat, xml_prof)
                if found:
                    x1, y1, x2, y2 = map(int, found[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ '{kw}' cliqué ({cx},{cy})")
                    bio_btn_clicked = True
                    break
            if bio_btn_clicked:
                break

        if bio_btn_clicked:
            break

        # Si on voit "Edit profile", c'est qu'on est bien sur le profil
        # → Cliquer "Edit profile" puis chercher le champ Bio
        if any(kw in xml_prof for kw in ["Edit profile", "Modifier le profil"]):
            print(f"  ℹ️ 'Add your bio' absent — passage par 'Edit profile'...")
            for ep_kw in ["Edit profile", "Modifier le profil", "Edit Profile"]:
                for ep_pat in [
                    rf'text="{re.escape(ep_kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(ep_kw)}"',
                ]:
                    ep_found = re.findall(ep_pat, xml_prof)
                    if ep_found:
                        x1, y1, x2, y2 = map(int, ep_found[0])
                        adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                        print(f"  ✅ 'Edit profile' cliqué")
                        bio_btn_clicked = True
                        break
                if bio_btn_clicked:
                    break
            break

        print(f"  ⏳ Bouton bio pas encore là ({tick+1}/15)...")
        time.sleep(1)

    if not bio_btn_clicked:
        print(f"  ❌ Impossible de trouver le bouton bio")
        stop_phone(phone_id)
        return False

    time.sleep(2)

    # ── 8. Trouver et cliquer le champ Bio ───────────────────────────
    print(f"  🔍 Recherche du champ Bio dans Edit profile...")
    field_clicked = False

    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_bio_edit.xml")
        time.sleep(0.5)
        xml_edit = adb(device, "shell cat /sdcard/ui_bio_edit.xml").stdout

        all_texts = re.findall(r'text="([^"]*)"', xml_edit)
        all_hints = re.findall(r'hint="([^"]*)"', xml_edit)
        print(f"  📋 Textes : {[t for t in all_texts if t.strip()][:15]}")
        print(f"  📋 Hints  : {[h for h in all_hints if h.strip()][:10]}")

        # Cas 1 : on est directement sur la page Bio (après "Add your bio")
        bio_field_keywords = ["Bio", "bio", "Votre bio"]
        for bk in bio_field_keywords:
            for bp in [
                rf'hint="{re.escape(bk)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(bk)}"',
                rf'text="{re.escape(bk)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(bk)}"',
            ]:
                found = re.findall(bp, xml_edit)
                if found:
                    x1, y1, x2, y2 = map(int, found[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ Champ '{bk}' cliqué ({cx},{cy})")
                    field_clicked = True
                    time.sleep(1)
                    break
            if field_clicked:
                break

        # Cas 2 : page Edit profile complète → chercher le champ Bio par position
        if not field_clicked and any(kw in xml_edit for kw in ["Edit profile", "Name", "Username"]):
            edits = re.findall(
                r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                xml_edit)
            # Le champ Bio est généralement le 3e ou 4e EditText
            # On cherche celui dont le hint contient "bio"
            bio_edit = None
            for coords in edits:
                x1, y1, x2, y2 = map(int, coords)
                nearby = xml_edit[max(0, xml_edit.find(f'[{x1},{y1}][{x2},{y2}]')-300):xml_edit.find(f'[{x1},{y1}][{x2},{y2}]')+50]
                if 'bio' in nearby.lower():
                    bio_edit = coords
                    break
            if not bio_edit and len(edits) >= 3:
                bio_edit = edits[2]  # 3e champ = Bio
            if bio_edit:
                x1, y1, x2, y2 = map(int, bio_edit)
                cx, cy = (x1+x2)//2, (y1+y2)//2
                adb(device, f"shell input tap {cx} {cy}")
                print(f"  ✅ Champ Bio (EditText) cliqué ({cx},{cy})")
                field_clicked = True
                time.sleep(1)

        if field_clicked:
            break
        print(f"  ⏳ Champ bio pas encore là ({tick+1}/10)...")
        time.sleep(1)

    if not field_clicked:
        print(f"  ❌ Champ bio introuvable")
        stop_phone(phone_id)
        return False

    # ── 9. Vider le champ et saisir la bio ───────────────────────────
    print(f"  ✏️ Saisie de la bio : {bio[:40]}...")
    adb(device, "shell input keyevent KEYCODE_CTRL_A")
    time.sleep(0.2)
    adb(device, "shell input keyevent KEYCODE_DEL")
    time.sleep(0.3)

    # Saisir la bio caractère par caractère (évite les problèmes d'encodage)
    import unicodedata as _UD
    def strip_accents(s):
        return ''.join(c for c in _UD.normalize('NFD', s) if _UD.category(c) != 'Mn')

    bio_clean = (strip_accents(bio)
        .replace("'", "")
        .replace('"', '')
        .replace('`', '')
        .replace('&', 'and')
        .replace('<', '')
        .replace('>', '')
        .replace(' ', '%s'))

    adb(device, f"shell input text '{bio_clean}'")
    print(f"  ✅ Bio saisie")
    time.sleep(1)

    # ── 10. Valider avec le checkmark en haut à droite ────────────────
    print(f"  🔍 Recherche bouton validation (✓)...")
    adb(device, "shell uiautomator dump /sdcard/ui_bio_validate.xml")
    time.sleep(0.5)
    xml_val = adb(device, "shell cat /sdcard/ui_bio_validate.xml").stdout

    validated = False

    # Chercher le bouton Done / checkmark / ✓ en haut à droite
    for val_kw in ["Done", "DONE", "✓", "Save", "SAVE", "Enregistrer", "Valider"]:
        for val_pat in [
            rf'text="{re.escape(val_kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(val_kw)}"',
            rf'content-desc="{re.escape(val_kw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(val_kw)}"',
        ]:
            found = re.findall(val_pat, xml_val)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                adb(device, f"shell input tap {cx} {cy}")
                print(f"  ✅ Validation '{val_kw}' cliquée ({cx},{cy})")
                validated = True
                break
        if validated:
            break

    # Fallback : le checkmark bleu est toujours en haut à droite (≈ x=0.85, y=0.06)
    if not validated:
        cx_v = int(w * 0.85)
        cy_v = int(h * 0.06)
        adb(device, f"shell input tap {cx_v} {cy_v}")
        print(f"  🎯 Fallback validation haut-droite ({cx_v},{cy_v})")
        validated = True

    time.sleep(2)

    # ── 11. Si on est sur Edit profile → cliquer Done/Save de la page ─
    adb(device, "shell uiautomator dump /sdcard/ui_bio_done.xml")
    time.sleep(0.4)
    xml_done = adb(device, "shell cat /sdcard/ui_bio_done.xml").stdout
    if any(kw in xml_done for kw in ["Edit profile", "Name", "Username"]):
        print(f"  🔍 Toujours sur Edit profile — cherche bouton Done global...")
        for dk in ["Done", "Save", "Enregistrer"]:
            for dp in [
                rf'text="{re.escape(dk)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(dk)}"',
            ]:
                dm = re.findall(dp, xml_done)
                if dm:
                    x1, y1, x2, y2 = map(int, dm[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    print(f"  ✅ Done global cliqué")
                    break
        time.sleep(2)

    # ── 12. Arrêter le téléphone ──────────────────────────────────────
    print(f"  ⏹ Arrêt téléphone {phone_id}...")
    stop_phone(phone_id)
    return True


def post_reel_on_device(phone_id: str, media_paths: list) -> bool:
    """
    Ouvre Instagram, clique sur le + en haut à gauche,
    sélectionne l'onglet REEL, sélectionne 1 à 3 médias et publie.
    """
    nb_media = min(len(media_paths), 3)
    is_video = any(p.lower().endswith(('.mp4', '.mov', '.avi', '.mkv')) for p in media_paths)
    print(f"  🎬 Post Reel → téléphone {phone_id} ({nb_media} média(s), vidéo={is_video})")
    if not check_account_age_warning(phone_id, "publication reel"):
        return False

    # ── 1. Démarrer le téléphone ──────────────────────────────────────
    ok = start_phone_with_retry(phone_id)
    if not ok:
        return False
    time.sleep(15)

    # ── 2. Activer ADB ────────────────────────────────────────────────
    enable_adb(phone_id)
    time.sleep(5)

    # ── 3. Attendre ADB ───────────────────────────────────────────────
    device, pwd = wait_for_adb(phone_id, max_wait=150)
    if not device:
        print(f"  ❌ ADB timeout pour {phone_id}")
        stop_phone(phone_id)
        return False

    # ── 4. Connexion glogin ───────────────────────────────────────────
    connected = False
    for attempt in range(30):
        subprocess.run(f'"{ADB_PATH}" connect {device}', shell=True, capture_output=True)
        time.sleep(3)
        result = subprocess.run(
            f'"{ADB_PATH}" -s {device} shell glogin {pwd}',
            shell=True, capture_output=True, text=True
        )
        print(f"  glogin [{attempt+1}] → {result.stdout.strip()}")
        if "success" in result.stdout.lower():
            connected = True
            break
    if not connected:
        print(f"  ❌ glogin échoué pour {phone_id}")
        stop_phone(phone_id)
        return False

    # ── 5. Pousser les médias sur le téléphone ────────────────────────
    remote_dir = "/sdcard/DCIM/post_reel_medias"
    adb(device, f"shell rm -rf {remote_dir}")
    adb(device, f"shell mkdir -p {remote_dir}")
    remote_paths = []
    for media_path in media_paths[:3]:
        filename = os.path.basename(media_path)
        remote = f"{remote_dir}/{filename}"
        push_result = subprocess.run(
            [ADB_PATH, "-s", device, "push", media_path, remote],
            capture_output=True, text=True
        )
        if push_result.returncode == 0:
            print(f"  ✅ Média poussé : {filename}")
            remote_paths.append(remote)
        else:
            print(f"  ❌ Erreur push {filename}")
    if not remote_paths:
        print(f"  ❌ Aucun média poussé")
        stop_phone(phone_id)
        return False

    adb(device, f"shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{remote_dir}/")
    time.sleep(3)

    # ── 6. Ouvrir Instagram ───────────────────────────────────────────
    print(f"  📱 Ouverture Instagram...")
    adb(device, "shell am force-stop com.instagram.android")
    time.sleep(1)
    subprocess.run(
        f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
        f'-c android.intent.category.LAUNCHER 1',
        shell=True, capture_output=True, text=True
    )
    time.sleep(6)
    _click_allow_if_present(device)
    time.sleep(1)
    _click_allow_if_present(device)

    # Attendre le feed — tap home à chaque tick pour sortir de Reels si besoin
    res = adb(device, "shell wm size")
    _m = re.search(r'(\d+)x(\d+)', res.stdout)
    _w, _h = (int(_m.group(1)), int(_m.group(2))) if _m else (1080, 2400)
    for tick in range(20):
        adb(device, "shell uiautomator dump /sdcard/ui_feed_reel.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_feed_reel.xml").stdout
        if _detect_logged_out_and_cleanup(device, phone_id, xml):
            stop_phone(phone_id)
            return False
        if any(kw in xml for kw in ["Your story", "For you"]):
            print(f"  ✅ Feed détecté ({tick+1}s)")
            handle_notifications_popup(device, safe_ui_dump(device, "/sdcard/ui_notif_popup.xml"))
            break
        print(f"  ⏳ Attente feed ({tick+1}/20) — tap home...")
        adb(device, f"shell input tap {int(_w*0.09)} {int(_h*0.895)}")
        time.sleep(1.5)
    # Toujours taper home après la boucle pour être sûr d'être sur le feed
    adb(device, f"shell input tap {int(_w*0.09)} {int(_h*0.895)}")
    time.sleep(1)

    # ── 7. Cliquer sur le + en haut à gauche ─────────────────────────
    print(f"  🔍 Recherche bouton + (nouveau post)...")
    plus_clicked = False
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_plus.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_plus.xml").stdout

        # Dump debug
        import re as _re
        all_descs = _re.findall(r'content-desc="([^"]+)"', xml)
        all_texts = _re.findall(r'text="([^"]+)"', xml)
        print(f"  📋 Descs : {[d for d in all_descs if d.strip()][:15]}")
        print(f"  📋 Textes : {[t for t in all_texts if t.strip()][:15]}")

        # Priorité 1 : content-desc "New post"
        # Priorité 1 : coordonnées proportionnelles directement
        res_size = adb(device, "shell wm size")
        m = re.search(r'(\d+)x(\d+)', res_size.stdout)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        fx, fy = int(w * 0.044), int(h * 0.057)
        print(f"  🎯 Bouton + coordonnées proportionnelles ({fx},{fy})")
        adb(device, f"shell input tap {fx} {fy}")
        plus_clicked = True
        break

    # ── 8. Cliquer sur l'onglet REEL en bas ──────────────────────────
    print(f"  🎬 Clic sur l'onglet REEL...")
    reel_clicked = False
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_reel_tab.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_reel_tab.xml").stdout

        for text in ["REEL", "Reel", "Reels", "REELS"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
                rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ Onglet REEL cliqué ({cx},{cy})")
                    reel_clicked = True
                    break
            if reel_clicked:
                break
        if reel_clicked:
            break

        # Check dialogues de permission système (cachent l'onglet REEL)
        for _perm_text in ["While using the app", "WHILE USING THE APP", "Allow all", "ALLOW ALL", "Allow"]:
            for _perm_pat in [
                rf'text="{re.escape(_perm_text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_perm_text)}"',
            ]:
                _perm_m = re.findall(_perm_pat, xml)
                if _perm_m:
                    _px1, _py1, _px2, _py2 = map(int, _perm_m[0])
                    _pcx, _pcy = (_px1+_px2)//2, (_py1+_py2)//2
                    adb(device, f"shell input tap {_pcx} {_pcy}")
                    print(f"  ✅ Permission '{_perm_text}' cliqué ({_pcx},{_pcy})")
                    time.sleep(0.8)
                    break

        # Check bouton "Start new video" pendant l'attente REEL
        for _snv_text in ["Start new video", "START NEW VIDEO"]:
            for _snv_pat in [
                rf'text="{re.escape(_snv_text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_snv_text)}"',
                rf'content-desc="{re.escape(_snv_text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(_snv_text)}"',
            ]:
                _snv_m = re.findall(_snv_pat, xml)
                if _snv_m:
                    _x1, _y1, _x2, _y2 = map(int, _snv_m[0])
                    _cx, _cy = (_x1+_x2)//2, (_y1+_y2)//2
                    adb(device, f"shell input tap {_cx} {_cy}")
                    print(f"  ✅ 'Start new video' cliqué ({_cx},{_cy})")
                    time.sleep(1.5)
                    break

        print(f"  ⏳ Onglet REEL pas encore ({tick+1}/10)...")
        time.sleep(1)

    if not reel_clicked:
        res_size = adb(device, "shell wm size")
        m = re.search(r'(\d+)x(\d+)', res_size.stdout)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        adb(device, f"shell input tap {int(w*0.75)} {int(h*0.96)}")
        print(f"  🎯 Fallback REEL coordonnées")
    time.sleep(2)

# ── 8b. Check bouton "Start new video" (2 fois) ───────────────────
    print(f"  🔍 Check bouton 'Start new video'...")
    for _snv_round in range(2):
        adb(device, "shell uiautomator dump /sdcard/ui_snv.xml")
        time.sleep(0.5)
        _xml_snv = adb(device, "shell cat /sdcard/ui_snv.xml").stdout
        _snv_found = False
        for _snv_text in ["Start new video", "START NEW VIDEO", "New video", "NEW VIDEO"]:
            for _snv_pat in [
                rf'text="{re.escape(_snv_text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_snv_text)}"',
                rf'content-desc="{re.escape(_snv_text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(_snv_text)}"',
            ]:
                _snv_m = re.findall(_snv_pat, _xml_snv)
                if _snv_m:
                    _x1, _y1, _x2, _y2 = map(int, _snv_m[0])
                    _cx, _cy = (_x1+_x2)//2, (_y1+_y2)//2
                    adb(device, f"shell input tap {_cx} {_cy}")
                    print(f"  ✅ 'Start new video' cliqué ({_cx},{_cy}) [{_snv_round+1}/2]")
                    _snv_found = True
                    time.sleep(1.5)
                    break
            if _snv_found:
                break
        if not _snv_found:
            print(f"  ℹ️ 'Start new video' absent [{_snv_round+1}/2] — OK")
        time.sleep(0.5)

    # ── 9. Permissions galerie avant sélection ────────────────────────
    _perm_texts = ["WHILE USING THE APP", "While using the app", "ALLOW ALL", "Allow all",
                   "ALLOW", "Allow", "While Using the App"]
    for _perm_round in range(3):
        adb(device, "shell uiautomator dump /sdcard/ui_perm_check.xml")
        time.sleep(0.4)
        xml_perm = adb(device, "shell cat /sdcard/ui_perm_check.xml").stdout
        _perm_found = False
        for _pt in _perm_texts:
            for _pp in [
                rf'text="{re.escape(_pt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_pt)}"',
            ]:
                _pm = re.findall(_pp, xml_perm)
                if _pm:
                    _px1, _py1, _px2, _py2 = map(int, _pm[0])
                    adb(device, f"shell input tap {(_px1+_px2)//2} {(_py1+_py2)//2}")
                    print(f"  ✅ Permission '{_pt}' acceptée [{_perm_round+1}/3]")
                    _perm_found = True
                    time.sleep(0.8)
                    break
            if _perm_found:
                break
        if not _perm_found:
            break

    # ── 10. Sélectionner les médias dans la galerie ───────────────────
    print(f"  📸 Sélection des médias ({nb_media})...")

    if is_video or nb_media == 1:
        # 1 seul média (vidéo ou 1 photo) → tap coordonnées fixes comme Story
        print(f"  🎯 Sélection 1 média — coordonnées fixes...")
        res_size = adb(device, "shell wm size")
        m = re.search(r'(\d+)x(\d+)', res_size.stdout)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        cx = w // 2
        cy = int(h * 0.365)
        adb(device, f"shell input tap {cx} {cy}")
        print(f"  ✅ Média sélectionné ({cx},{cy})")
        time.sleep(1.5)

    else:
        # 2 ou 3 photos → cliquer Select puis sélectionner
        print(f"  📸 {nb_media} photos — clic Select...")
        select_clicked = False
        for tick in range(8):
            adb(device, "shell uiautomator dump /sdcard/ui_reel_select.xml")
            time.sleep(0.5)
            xml = adb(device, "shell cat /sdcard/ui_reel_select.xml").stdout
            for text in ["Select", "SELECT", "Sélectionner"]:
                for pattern in [
                    rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
                    rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"',
                ]:
                    matches = re.findall(pattern, xml)
                    if matches:
                        x1, y1, x2, y2 = map(int, matches[0])
                        cx, cy = (x1+x2)//2, (y1+y2)//2
                        adb(device, f"shell input tap {cx} {cy}")
                        print(f"  ✅ Select cliqué ({cx},{cy})")
                        select_clicked = True
                        break
                if select_clicked:
                    break
            if select_clicked:
                break
            print(f"  ⏳ Select pas encore ({tick+1}/8)...")
            time.sleep(1)

        time.sleep(1.5)

        # Dump grille et sélectionner les N premières photos
        adb(device, "shell uiautomator dump /sdcard/ui_reel_grid.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_reel_grid.xml").stdout

        res_size = adb(device, "shell wm size")
        m = re.search(r'(\d+)x(\d+)', res_size.stdout)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        grid_y_min = int(h * 0.25)

        clickables = re.findall(
            r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        if not clickables:
            clickables = re.findall(
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"', xml)

        photo_cells = []
        for coords in clickables:
            x1, y1, x2, y2 = map(int, coords)
            cy_c = (y1+y2)//2
            bw, bh = x2-x1, y2-y1
            if cy_c > grid_y_min and bw > 60 and bh > 60 and 0.7 < (bw/max(bh,1)) < 1.3:
                photo_cells.append(((x1+x2)//2, cy_c))

        photo_cells.sort(key=lambda c: (c[1]//100, c[0]))

        # Exclure la caméra (cellule la plus en haut-gauche)
        if photo_cells:
            first_row_y = photo_cells[0][1]
            first_row = [c for c in photo_cells if abs(c[1] - first_row_y) < 80]
            first_row.sort(key=lambda c: c[0])
            camera_cell = first_row[0] if first_row else None
            if camera_cell:
                print(f"  📷 Caméra exclue : ({camera_cell[0]},{camera_cell[1]})")
                photo_cells = [c for c in photo_cells if c != camera_cell]

        # Dédoublonner
        deduplicated = []
        for cell in photo_cells:
            is_dup = any(abs(cell[0]-k[0]) < 50 and abs(cell[1]-k[1]) < 50 for k in deduplicated)
            if not is_dup:
                deduplicated.append(cell)
        photo_cells = deduplicated

        print(f"  📋 {len(photo_cells)} cellules disponibles")
        for cell in photo_cells[:nb_media]:
            cx_c, cy_c = cell
            print(f"  📸 Tap ({cx_c},{cy_c})")
            adb(device, f"shell input tap {cx_c} {cy_c}")
            time.sleep(0.8)

    time.sleep(1.5)

    # ── 10. Next (1er) ────────────────────────────────────────────────
    print(f"  ➡️ Next 1...")
    _tap_next_or_continue(device, "ui_reel_next1.xml", max_ticks=10)

    time.sleep(2)

    # ── 12. Boucle jusqu'à l'écran caption ────────────────────────────
    print(f"  🔍 Boucle Next/Continue jusqu'à écran caption...")
    for _loop in range(15):
        # Fermer popup sticker si présent
        if _dismiss_sticker_popup(device):
            time.sleep(0.5)
            continue

        adb(device, "shell uiautomator dump /sdcard/ui_reel_loop.xml")
        time.sleep(0.5)
        xml_loop = adb(device, "shell cat /sdcard/ui_reel_loop.xml").stdout

        # Fermer popup "Others can now download and share your reels" en priorité
        _download_popup = any(kw in xml_loop.lower() for kw in ["others can now download", "download and share your reels"])
        if _download_popup:
            for _pat in [
                r'text="Continue"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="Continue"',
            ]:
                _m = re.findall(_pat, xml_loop)
                if _m:
                    _x1, _y1, _x2, _y2 = map(int, _m[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    print(f"  ✅ Popup 'download/share' fermé — Continue ({(_x1+_x2)//2},{(_y1+_y2)//2})")
                    time.sleep(2.0)
                    break
            continue

        # Si caption détecté → on sort (uniquement si pas de popup au-dessus)
        if any(kw in xml_loop.lower() for kw in ["caption", "add a caption", "write a caption"]):
            print(f"  ✅ Écran caption détecté — sortie boucle ({_loop+1})")
            break

        _btn_found = False
        for _bt in ["Next", "NEXT", "Continue", "CONTINUE", "Continuer"]:
            for _pat in [
                rf'text="{re.escape(_bt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_bt)}"',
            ]:
                _m = re.findall(_pat, xml_loop)
                if _m:
                    _x1, _y1, _x2, _y2 = map(int, _m[0])
                    _cx, _cy = (_x1+_x2)//2, (_y1+_y2)//2
                    adb(device, f"shell input tap {_cx} {_cy}")
                    print(f"  ✅ '{_bt}' cliqué ({_cx},{_cy}) [{_loop+1}/15]")
                    _btn_found = True
                    time.sleep(2.0)
                    break
            if _btn_found:
                break

        if not _btn_found:
            print(f"  ℹ️ Rien à cliquer [{_loop+1}/15] — attente 1s...")
            time.sleep(1.0)

    # ── 13. Caption ───────────────────────────────────────────────────
    print(f"  📝 Écran caption reel...")

    # ── 13. Caption ───────────────────────────────────────────────────
    print(f"  📝 Écran caption reel...")
    caption_done = False
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_reel_caption.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_reel_caption.xml").stdout

        all_texts = re.findall(r'text="([^"]*)"', xml)
        all_hints = re.findall(r'hint="([^"]*)"', xml)
        print(f"  📋 Textes : {[t for t in all_texts if t.strip()][:15]}")
        print(f"  📋 Hints  : {[h for h in all_hints if h.strip()][:10]}")

        caption_found = False

        # Méthode 1 : recherche floue caption
        for kw in ["caption", "Caption", "Add a caption", "Write a caption"]:
            for pattern in [
                rf'hint="[^"]*{re.escape(kw)}[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="[^"]*{re.escape(kw)}[^"]*"',
                rf'text="[^"]*{re.escape(kw)}[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="[^"]*{re.escape(kw)}[^"]*"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ Caption via '{kw}' ({cx},{cy})")
                    caption_found = True
                    time.sleep(1.0)
                    break
            if caption_found:
                break

        # Méthode 2 : EditText dans moitié haute
        if not caption_found:
            res_size = adb(device, "shell wm size")
            m2 = re.search(r'(\d+)x(\d+)', res_size.stdout)
            w2, h2 = (int(m2.group(1)), int(m2.group(2))) if m2 else (1080, 2400)
            edits = re.findall(
                r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            for coords in edits:
                x1, y1, x2, y2 = map(int, coords)
                cy2 = (y1+y2)//2
                if cy2 < int(h2 * 0.55):
                    adb(device, f"shell input tap {(x1+x2)//2} {cy2}")
                    print(f"  🎯 Caption EditText ({(x1+x2)//2},{cy2})")
                    caption_found = True
                    time.sleep(1.0)
                    break

        # Méthode 3 : coordonnées fixes
        if not caption_found:
            res_size = adb(device, "shell wm size")
            m3 = re.search(r'(\d+)x(\d+)', res_size.stdout)
            w3, h3 = (int(m3.group(1)), int(m3.group(2))) if m3 else (1080, 2400)
            adb(device, f"shell input tap {w3//2} {int(h3*0.35)}")
            print(f"  🎯 Caption fallback coords ({w3//2},{int(h3*0.35)})")
            caption_found = True
            time.sleep(1.0)

        if caption_found:
            _tag = random.choice(MENTION_TAGS) if MENTION_TAGS else MENTION_TAG
            adb(device, f"shell input text '{_tag}'")
            print(f"  ✅ Caption '{_tag}' saisie")
            time.sleep(0.8)
            adb(device, "shell input keyevent KEYCODE_BACK")
            print(f"  ⌨️ Clavier fermé")
            time.sleep(1.0)
            caption_done = True
            break

        print(f"  ⏳ Caption pas encore ({tick+1}/10)...")
        time.sleep(1)

    # ── 14. Next avant Share ──────────────────────────────────────────
    print(f"  ➡️ Next avant Share...")
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_reel_next_share.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_reel_next_share.xml").stdout
        next_found = False
        for text in ["Next", "NEXT"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ Next avant Share ({cx},{cy})")
                    next_found = True
                    break
            if next_found:
                break
        if next_found:
            break
        if any(kw in xml for kw in ["Share", "SHARE", "Partager"]):
            print(f"  ℹ️ Share déjà visible — Next non nécessaire")
            break
        print(f"  ⏳ Next avant Share pas encore ({tick+1}/10)...")
        time.sleep(1)

    time.sleep(2)

    # ── 15. Share ─────────────────────────────────────────────────────
    print(f"  🔍 Recherche bouton Share...")
    share_found = False
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_reel_share.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_reel_share.xml").stdout
        for text in ["Share", "SHARE", "Partager", "Publish", "PUBLISH"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
                rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    print(f"  ✅ Share cliqué ({cx},{cy})")
                    adb(device, f"shell input tap {cx} {cy}")
                    share_found = True
                    break
            if share_found:
                break
        if share_found:
            break
        print(f"  ⏳ Share pas encore là ({tick+1}/10)...")
        time.sleep(1)

    # ── 16. Confirmation + arrêt ──────────────────────────────────────
    if not share_found:
        print(f"  ❌ Bouton Share jamais trouvé — reel non publié")
        stop_phone(phone_id)
        return False
    print(f"  ⏳ Attente confirmation (5s)...")
    time.sleep(5)
    print(f"  ⏹ Arrêt téléphone {phone_id}...")
    stop_phone(phone_id)
    return True

def insta_step_get_started(device):
    """
    Étape 1 : Cliquer sur 'Get Started' ou 'Create new account'.
    Si l'écran ne change pas après le clic, force-stop + relance Instagram et réessaie.
    Max 3 tentatives. Retourne True si la transition a réussi, False sinon.
    """
    _gs_buttons = ["Get started", "Get Started", "Create new account", "Create New Account"]

    for _attempt in range(3):
        if _attempt > 0:
            print(f"  🔄 Interface bloquée — relance Instagram (tentative {_attempt+1}/3)...")
            adb(device, "shell am force-stop com.instagram.android")
            time.sleep(3)
            subprocess.run(
                f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
                f'-c android.intent.category.LAUNCHER 1',
                shell=True, capture_output=True, text=True
            )
            print(f"  ⏳ Attente interface après relance (5s)...")
            time.sleep(5)

        print(f"  🔍 Scan 'Get Started' / 'Create new account' (tentative {_attempt+1}/3)...")
        _clicked = False
        for tick in range(30):
            adb(device, "shell uiautomator dump /sdcard/ui_insta_home.xml")
            time.sleep(0.5)
            xml = adb(device, "shell cat /sdcard/ui_insta_home.xml").stdout

            # ── Popup permission : si 'Allow' présent → cliquer pour la fermer ──
            _allow_btns = ["Allow", "ALLOW", "Autoriser",
                           "While using the app", "Only this time", "Allow all the time"]
            _allow_clicked = False
            for _ab in _allow_btns:
                for _ap in [
                    rf'text="{re.escape(_ab)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_ab)}"',
                    rf'content-desc="{re.escape(_ab)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                ]:
                    _am = re.findall(_ap, xml)
                    if _am:
                        _ax1,_ay1,_ax2,_ay2 = map(int,_am[0])
                        adb(device, f"shell input tap {(_ax1+_ax2)//2} {(_ay1+_ay2)//2}")
                        print(f"  🔓 Popup '{_ab}' détectée — cliquée pour fermer")
                        _allow_clicked = True
                        time.sleep(1.0)
                        break
                if _allow_clicked:
                    break
            if _allow_clicked:
                continue  # re-dump l'écran après fermeture de la popup

            for text in _gs_buttons:
                matches = re.findall(
                    rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
                if not matches:
                    matches = re.findall(
                        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"', xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ '{text}' cliqué ({cx},{cy})")
                    _clicked = True
                    break

            if _clicked:
                break
            print(f"  ⏳ Bouton pas encore là ({tick+1}/30)...")
            time.sleep(0.7)

        if not _clicked:
            print(f"  ⚠️ Bouton introuvable en 30s — relance...")
            continue

        # Vérifier que l'écran a bien changé après le clic (max ~40s, car la page
        # d'erreur "Page isn't available" + Refresh peut apparaître bien après).
        print(f"  🔍 Vérification transition écran (max 40s)...")
        _transition_ok = False
        for _wt in range(40):
            time.sleep(1)
            xml_after = safe_ui_dump(device, "/sdcard/ui_after_gs.xml")

            # ── Page d'erreur "Page isn't available" → Refresh, et on reste en boucle ──
            if handle_refresh_page(device, xml_after):
                print(f"  ⏳ Page d'erreur / rechargement en cours ({_wt+1}/40)...")
                continue

            # Écran suivant réellement chargé (plus de Get Started/Create, ni page d'erreur)
            if not any(kw in xml_after for kw in _gs_buttons):
                print(f"  ✅ Transition réussie ({_wt+1}s)")
                _transition_ok = True
                return True
            print(f"  ⏳ Écran pas encore changé ({_wt+1}/40)...")

        if not _transition_ok:
            print(f"  ⚠️ Pas de transition après 40s — relance Instagram...")

    print(f"  ❌ Get Started jamais abouti après 3 tentatives")
    return False



def insta_step_create_password(device, password="Alexis06"):
    """
    Étape : Sur l'écran 'Create password',
    cliquer sur le champ Password, saisir le mot de passe, puis Next.
    Retourne True si succès, False sinon.
    """
    print(f"  🔍 Attente écran 'Create password' pendant 20s...")

    for tick in range(20):
        adb(device, "shell uiautomator dump /sdcard/ui_insta_pass.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_insta_pass.xml").stdout

        screen_keywords = [
            "Create password",
            "create password",
            "Create a password",
            "create a password",
            "Password",
        ]
        if not any(kw.lower() in xml.lower() for kw in screen_keywords):
            print(f"  ⏳ Écran password pas encore là ({tick+1}/20)...")
            time.sleep(0.8)
            continue

        print(f"  ✅ Écran 'Create password' détecté ({tick+1}s)")

        # ── Trouver et cliquer le champ password ─────────────────────────────
        field_found = False
        for hint in ["Password", "password", "Create password"]:
            for pattern in [
                rf'hint="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(hint)}"',
                rf'text="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(hint)}"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ Champ password cliqué ({cx},{cy})")
                    field_found = True
                    time.sleep(0.8)
                    break
            if field_found:
                break

        if not field_found:
            print(f"  ⚠️ Champ password non trouvé via hint — fallback EditText...")
            edits = re.findall(
                r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if edits:
                x1, y1, x2, y2 = map(int, edits[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                adb(device, f"shell input tap {cx} {cy}")
                print(f"  ✅ EditText cliqué ({cx},{cy})")
                time.sleep(0.8)
            else:
                print(f"  ❌ Aucun champ trouvé")
                return False

        # ── Vider et saisir le mot de passe ──────────────────────────────────
        adb(device, "shell input keyevent KEYCODE_CTRL_A")
        time.sleep(0.2)
        adb(device, "shell input keyevent KEYCODE_DEL")
        time.sleep(0.2)
        adb(device, f"shell input text '{password}'")
        print(f"  ✅ Mot de passe saisi : {password}")
        time.sleep(0.5)

        # ── Cliquer Next ──────────────────────────────────────────────────────
        insta_step_next(device)
        return True

    print(f"  ❌ Écran 'Create password' jamais apparu")
    return False


def insta_step_enter_phone_number(device, phone_number: str) -> bool:
    """
    Sur l'écran 'What's your mobile number?',
    clique sur le champ Mobile number, saisit le numéro, clique Next.
    """
    print(f"  🔍 Attente écran 'What's your mobile number' (max 30s)...")
    for tick in range(30):
        adb(device, "shell uiautomator dump /sdcard/ui_insta_phone.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_insta_phone.xml").stdout

        screen_keywords = ["mobile number", "phone number", "Mobile number"]
        if not any(kw.lower() in xml.lower() for kw in screen_keywords):
            print(f"  ⏳ Écran pas encore là ({tick+1}/30)...")
            time.sleep(0.8)
            continue

        # Vérifier que le champ de saisie est présent (pas un écran en chargement)
        _has_input = (
            'hint="Mobile number"' in xml or
            'hint="Phone number"' in xml or
            bool(re.findall(r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml))
        )
        if not _has_input:
            print(f"  ⏳ Écran en chargement, champ absent ({tick+1}/30)...")
            time.sleep(0.8)
            continue

        print(f"  ✅ Écran mobile number détecté ({tick+1}s)")

        # Cliquer sur le champ Mobile number
        field_found = False
        for hint in ["Mobile number", "mobile number", "Phone number"]:
            for pattern in [
                rf'hint="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(hint)}"',
                rf'text="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(hint)}"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ Champ '{hint}' cliqué ({cx},{cy})")
                    field_found = True
                    time.sleep(0.8)
                    break
            if field_found:
                break

        if not field_found:
            edits = re.findall(
                r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if edits:
                x1, y1, x2, y2 = map(int, edits[0])
                adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                print(f"  🎯 Fallback EditText")
                time.sleep(0.8)

        # Saisir le numéro via keycodes
        type_number_keycode(device, phone_number)
        time.sleep(0.5)
        return True

    print(f"  ❌ Écran mobile number jamais apparu")
    return False



def wait_for_enter_code_screen(device, timeout=15, retry_at=None, retry_callback=None):
    """
    Attend l'écran de saisie du code SMS.
    Si retry_at et retry_callback sont fournis, appelle le callback une seule fois
    à retry_at secondes pour relancer l'étape précédente (re-soumission du numéro).
    """
    print(f"  ⏳ Attente de l'écran 'Enter your code' (max {timeout}s)...")
    enter_code_keywords = [
        "enter your code",
        "enter code",
        "enter the code",
        "confirmation code",
        "didn't get anything",
        "didn't get the code",
        "i didn't get",
        "resend",
        "saisissez votre code",
        "saisissez le code",
        "code de confirmation",
    ]
    _retry_done = False
    for elapsed in range(timeout):
        adb(device, "shell uiautomator dump /sdcard/ui_code_screen.xml")
        time.sleep(0.5)
        result = adb(device, "shell cat /sdcard/ui_code_screen.xml")
        xml = result.stdout.lower()

        # Priorité 0 : message rouge "experiencing some issues"
        issues_keywords = [
            "we're experiencing some issues",
            "we\u2019re experiencing some issues",
            "experiencing some issues",
            "please try again",
        ]
        if any(kw in xml for kw in issues_keywords):
            # S'assurer que la popup "choose a phone number" est fermée d'abord
            choose_keywords = ["choose a phone number", "choose phone number"]
            if any(kw in xml for kw in choose_keywords):
                print(f"  ⚠️ Popup 'Choose a phone number' présente — fermeture avant issues...")
                x_patterns = [
                    r'content-desc="Close"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="Close"',
                ]
                for xp in x_patterns:
                    found_x = re.findall(xp, result.stdout)
                    if found_x:
                        x1, y1, x2, y2 = map(int, found_x[0])
                        cx, cy = (x1+x2)//2, (y1+y2)//2
                        adb(device, f"shell input tap {cx} {cy}")
                        time.sleep(1.5)
                        break
                else:
                    adb(device, "shell input tap 476 748")
                    time.sleep(1.5)
            print(f"  ❌ Message rouge 'experiencing some issues' détecté → suppression profil")
            return "issues"

        # Priorité 1 : bouton OK = numéro rejeté
        if 'text="ok"' in xml or 'text="Ok"' in result.stdout:
            print(f"  ⚠️ Bouton OK détecté pendant attente écran code → numéro rejeté")
            return "ok_button"

        # Priorité 2 : CAPTCHA
        captcha_keywords = ["drag the element", "most similar", "arkose", "funcaptcha", "funcaptcha"]
        if any(kw in xml for kw in captcha_keywords):
            print(f"  ⚠️ CAPTCHA détecté pendant attente écran code")
            return "captcha"

        # Priorité 3 : écran "Enter your code" visible
        if any(kw in xml for kw in enter_code_keywords):
            print(f"  ✅ Écran 'Enter your code' détecté ({elapsed+1}s)")
            return "ok"

        # Retry mi-chemin : relancer l'étape précédente si l'écran tarde trop
        if (not _retry_done and retry_at is not None and retry_callback is not None
                and elapsed + 1 >= retry_at):
            print(f"  🔄 Écran code absent depuis {elapsed+1}s — re-tentative étape précédente...")
            _retry_done = True
            try:
                retry_callback()
            except Exception as _rc_err:
                print(f"  ⚠️ Erreur retry_callback : {_rc_err}")

        print(f"  ⏳ Écran code pas encore visible... ({elapsed+1}/{timeout}s)")
        time.sleep(1)

    print(f"  ❌ Timeout : écran 'Enter your code' jamais apparu ({timeout}s)")
    return "timeout"



def post_feed_on_device(phone_id: str, media_paths: list) -> bool:
    """
    Ouvre Instagram, clique sur le + en haut à gauche,
    sélectionne 1 à 4 photos et publie le post feed.
    """
    nb_photos = min(len(media_paths), 3)
    print(f"  📸 Post Feed → téléphone {phone_id} ({nb_photos} photo(s), max 3)")

    # ── 1. Démarrer le téléphone ──────────────────────────────────────
    ok = start_phone_with_retry(phone_id)
    if not ok:
        return False
    time.sleep(15)

    # ── 2. Activer ADB ────────────────────────────────────────────────
    enable_adb(phone_id)
    time.sleep(5)

    # ── 3. Attendre ADB ───────────────────────────────────────────────
    device, pwd = wait_for_adb(phone_id, max_wait=150)
    if not device:
        print(f"  ❌ ADB timeout pour {phone_id}")
        stop_phone(phone_id)
        return False

    # ── 4. Connexion glogin ───────────────────────────────────────────
    connected = False
    for attempt in range(30):
        subprocess.run(f'"{ADB_PATH}" connect {device}', shell=True, capture_output=True)
        time.sleep(3)
        result = subprocess.run(
            f'"{ADB_PATH}" -s {device} shell glogin {pwd}',
            shell=True, capture_output=True, text=True
        )
        print(f"  glogin [{attempt+1}] → {result.stdout.strip()}")
        if "success" in result.stdout.lower():
            connected = True
            break
    if not connected:
        print(f"  ❌ glogin échoué pour {phone_id}")
        stop_phone(phone_id)
        return False

    # ── 5. Pousser les photos sur le téléphone ────────────────────────
    remote_dir = "/sdcard/DCIM/post_feed_photos"
    adb(device, f"shell rm -rf {remote_dir}")
    adb(device, f"shell mkdir -p {remote_dir}")
    remote_paths = []
    for media_path in media_paths[:3]:
        filename = os.path.basename(media_path)
        remote = f"{remote_dir}/{filename}"
        push_result = subprocess.run(
            [ADB_PATH, "-s", device, "push", media_path, remote],
            capture_output=True, text=True
        )
        if push_result.returncode == 0:
            print(f"  ✅ Photo poussée : {filename}")
            remote_paths.append(remote)
        else:
            print(f"  ❌ Erreur push {filename}")
    if not remote_paths:
        print(f"  ❌ Aucune photo poussée")
        stop_phone(phone_id)
        return False

    # Scanner la galerie
    adb(device, f"shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{remote_dir}/")
    time.sleep(3)

    # ── 6. Ouvrir Instagram ───────────────────────────────────────────
    print(f"  📱 Ouverture Instagram...")
    adb(device, "shell am force-stop com.instagram.android")
    time.sleep(1)
    subprocess.run(
        f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
        f'-c android.intent.category.LAUNCHER 1',
        shell=True, capture_output=True, text=True
    )
    time.sleep(6)

    # Attendre que le feed soit chargé — tap home à chaque tick pour sortir de Reels
    res = adb(device, "shell wm size")
    _m = re.search(r'(\d+)x(\d+)', res.stdout)
    _w, _h = (int(_m.group(1)), int(_m.group(2))) if _m else (1080, 2400)
    for tick in range(20):
        adb(device, "shell uiautomator dump /sdcard/ui_feed_post.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_feed_post.xml").stdout
        if any(kw in xml for kw in ["Your story", "For you"]):
            print(f"  ✅ Feed détecté ({tick+1}s)")
            handle_notifications_popup(device, safe_ui_dump(device, "/sdcard/ui_notif_popup.xml"))
            break
        print(f"  ⏳ Attente feed ({tick+1}/20) — tap home...")
        adb(device, f"shell input tap {int(_w*0.09)} {int(_h*0.895)}")
        time.sleep(1.5)
    # Toujours taper home après la boucle pour être sûr d'être sur le feed
    adb(device, f"shell input tap {int(_w*0.09)} {int(_h*0.895)}")
    time.sleep(1)

    # ── 7. Cliquer sur le + en haut à gauche ─────────────────────────
    print(f"  🔍 Recherche bouton + (nouveau post)...")
    plus_clicked = False
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_plus.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_plus.xml").stdout

        # Dump debug
        import re as _re
        all_descs = _re.findall(r'content-desc="([^"]+)"', xml)
        all_texts = _re.findall(r'text="([^"]+)"', xml)
        print(f"  📋 Descs : {[d for d in all_descs if d.strip()][:15]}")
        print(f"  📋 Textes : {[t for t in all_texts if t.strip()][:15]}")

        # Priorité 1 : content-desc "New post"
        # Priorité 1 : coordonnées proportionnelles directement
        res_size = adb(device, "shell wm size")
        m = re.search(r'(\d+)x(\d+)', res_size.stdout)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
        fx, fy = int(w * 0.044), int(h * 0.057)
        print(f"  🎯 Bouton + coordonnées proportionnelles ({fx},{fy})")
        adb(device, f"shell input tap {fx} {fy}")
        plus_clicked = True
        break

    # ── 8. Vérifier qu'on est sur "New post" et onglet POST ───────────
    print(f"  🔍 Vérification écran 'New post'...")
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_newpost.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_newpost.xml").stdout
        if any(kw in xml for kw in ["New post", "POST", "STORY", "REEL", "Recents"]):
            print(f"  ✅ Écran 'New post' détecté ({tick+1}s)")
            break
        print(f"  ⏳ Écran new post pas encore là ({tick+1}/10)...")
        time.sleep(1)

    # S'assurer qu'on est sur l'onglet POST
    adb(device, "shell uiautomator dump /sdcard/ui_newpost2.xml")
    time.sleep(0.5)
    xml = adb(device, "shell cat /sdcard/ui_newpost2.xml").stdout
    for text in ["POST", "Post"]:
        for pattern in [
            rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
        ]:
            matches = re.findall(pattern, xml)
            if matches:
                x1, y1, x2, y2 = map(int, matches[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                print(f"  ✅ Onglet POST cliqué ({cx},{cy})")
                adb(device, f"shell input tap {cx} {cy}")
                time.sleep(1.5)
                break

    # ── 9. Sélectionner les photos ────────────────────────────────────
    if nb_photos == 1:
        # 1 photo → elle est déjà sélectionnée par défaut, cliquer Next
        print(f"  ✅ 1 photo — tap Next direct...")
        _tap_next_or_continue(device, "ui_next1.xml", max_ticks=8)

    else:
        # 2-4 photos → cliquer "Select" puis sélectionner chaque photo
        print(f"  📸 {nb_photos} photos — clic Select...")
        select_clicked = False
        for tick in range(8):
            adb(device, "shell uiautomator dump /sdcard/ui_select.xml")
            time.sleep(0.5)
            xml = adb(device, "shell cat /sdcard/ui_select.xml").stdout
            for text in ["Select", "SELECT", "Sélectionner"]:
                for pattern in [
                    rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
                    rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"',
                ]:
                    matches = re.findall(pattern, xml)
                    if matches:
                        x1, y1, x2, y2 = map(int, matches[0])
                        cx, cy = (x1+x2)//2, (y1+y2)//2
                        print(f"  ✅ 'Select' cliqué ({cx},{cy})")
                        adb(device, f"shell input tap {cx} {cy}")
                        select_clicked = True
                        break
                if select_clicked:
                    break
            if select_clicked:
                break
            print(f"  ⏳ 'Select' pas encore là ({tick+1}/8)...")
            time.sleep(1)

        time.sleep(1.5)

        # Sélectionner les N premières photos de la grille
        print(f"  🔍 Sélection de {nb_photos} photos dans la grille...")
        adb(device, "shell uiautomator dump /sdcard/ui_grid.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_grid.xml").stdout

        # Chercher les éléments image cliquables dans la grille
        res_size = adb(device, "shell wm size")
        m = re.search(r'(\d+)x(\d+)', res_size.stdout)
        w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)

        # Les photos de la grille sont dans le bas de l'écran
        # Typiquement dans une zone y > 40% de l'écran
        grid_y_min = int(h * 0.35)
        clickables = re.findall(
            r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        if not clickables:
            clickables = re.findall(
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"', xml)

        # Filtrer : photos de la grille = éléments carrés dans la zone basse
        photo_cells = []
        for coords in clickables:
            x1, y1, x2, y2 = map(int, coords)
            cy_c = (y1+y2)//2
            bw, bh = x2-x1, y2-y1
            # Cellule carrée dans la grille (ratio entre 0.7 et 1.3)
            if cy_c > grid_y_min and bw > 60 and bh > 60 and 0.7 < (bw/max(bh,1)) < 1.3:
                photo_cells.append(((x1+x2)//2, cy_c))

        print(f"  📋 {len(photo_cells)} cellules photo détectées dans la grille")

        # Trier par position (ligne par ligne, gauche à droite)
# Trier par position (ligne par ligne, gauche à droite)
        photo_cells.sort(key=lambda c: (c[1] // 100, c[0]))

        # ── Exclure la cellule caméra ──────────────────────────────────────────
        # La caméra est toujours la cellule la plus en haut-gauche de la grille
        # On l'identifie : x minimal parmi les cellules de la première ligne
        if photo_cells:
            first_row_y = photo_cells[0][1]
            # Toutes les cellules de la première ligne (même y à ±80px)
            first_row = [c for c in photo_cells if abs(c[1] - first_row_y) < 80]
            first_row.sort(key=lambda c: c[0])  # trier par x croissant
            # La caméra = cellule la plus à gauche de la première ligne
            camera_cell = first_row[0] if first_row else None
            if camera_cell:
                print(f"  📷 Cellule caméra exclue : ({camera_cell[0]},{camera_cell[1]})")
                photo_cells = [c for c in photo_cells if c != camera_cell]

        # Sélectionner les N premières photos (caméra déjà exclue)
        selected_count = 0
        for cell in photo_cells[:nb_photos]:
            cx_c, cy_c = cell
            print(f"  📸 Sélection photo {selected_count+1}/{nb_photos} ({cx_c},{cy_c})")
            adb(device, f"shell input tap {cx_c} {cy_c}")
            selected_count += 1
            time.sleep(0.8)

        if selected_count < nb_photos:
            print(f"  ⚠️ Seulement {selected_count}/{nb_photos} photos sélectionnées")

        # Cliquer Next
        time.sleep(1)
        _tap_next_or_continue(device, "ui_next_multi.xml", max_ticks=8)

    time.sleep(2)

    # ── 10. Écran de filtre → Next ────────────────────────────────────
    print(f"  🔍 Écran filtre → Next...")
    _tap_next_or_continue(device, "ui_filter_post.xml", max_ticks=8)

    time.sleep(2)

# ── 11. Écran caption → écrire caption → Share ────────────────────────
    print(f"  🔍 Écran caption — attente...")
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_caption.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_caption.xml").stdout
        caption_found = False

        # Fermer popup "Others can now download and share your reels" si présent
        if any(kw in xml.lower() for kw in ["others can now download", "download and share your reels"]):
            for _pat in [
                r'text="Continue"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="Continue"',
            ]:
                _m = re.findall(_pat, xml)
                if _m:
                    _x1, _y1, _x2, _y2 = map(int, _m[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    print(f"  ✅ Popup 'download/share' fermé — Continue ({(_x1+_x2)//2},{(_y1+_y2)//2})")
                    time.sleep(2.0)
                    break
            continue

        # Chercher le champ "Add a caption..."
# ── Debug : afficher tous les textes visibles pour diagnostic ─────────
        import re as _re
        all_texts = _re.findall(r'text="([^"]*)"', xml)
        all_hints = _re.findall(r'hint="([^"]*)"', xml)
        print(f"  📋 Textes XML : {[t for t in all_texts if t.strip()][:20]}")
        print(f"  📋 Hints XML  : {[h for h in all_hints if h.strip()][:10]}")

        # ── Méthode 1 : recherche floue sur "caption" dans text ou hint ───────
        caption_keywords = ["caption", "Caption", "Add a caption", "Write a caption"]
        for pattern_tmpl in [
            r'hint="([^"]*{kw}[^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="([^"]*{kw}[^"]*)"',
            r'text="([^"]*{kw}[^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="([^"]*{kw}[^"]*)"',
        ]:
            if caption_found:
                break
            for kw in caption_keywords:
                pattern = pattern_tmpl.replace('{kw}', re.escape(kw))
                matches = re.findall(pattern, xml, re.IGNORECASE)
                if matches:
                    # Le groupe bounds est toujours en dernier
                    coords = matches[0][-4:]
                    x1, y1, x2, y2 = map(int, coords)
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    print(f"  ✅ Champ caption trouvé via '{kw}' ({cx},{cy}) — tap")
                    adb(device, f"shell input tap {cx} {cy}")
                    caption_found = True
                    time.sleep(1.0)
                    break

        # ── Méthode 2 : n'importe quel EditText dans la zone haute ────────────
        if not caption_found:
            res_size = adb(device, "shell wm size")
            m = re.search(r'(\d+)x(\d+)', res_size.stdout)
            w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
            edits = re.findall(
                r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not edits:
                edits = re.findall(
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*class="android\.widget\.EditText"', xml)
            for coords in edits:
                x1, y1, x2, y2 = map(int, coords)
                cy = (y1+y2)//2
                cx = (x1+x2)//2
                # Le champ caption est dans la moitié haute de l'écran
                if cy < int(h * 0.55):
                    print(f"  🎯 Fallback EditText caption ({cx},{cy})")
                    adb(device, f"shell input tap {cx} {cy}")
                    caption_found = True
                    time.sleep(1.0)
                    break

        # ── Méthode 3 : fallback coordonnées fixes ────────────────────────────
        # D'après le screenshot : "Add a caption..." est à environ y=35% de l'écran
        if not caption_found:
            res_size = adb(device, "shell wm size")
            m = re.search(r'(\d+)x(\d+)', res_size.stdout)
            w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)
            cx_fb, cy_fb = w // 2, int(h * 0.35)
            print(f"  🎯 Fallback coordonnées fixes caption ({cx_fb},{cy_fb})")
            adb(device, f"shell input tap {cx_fb} {cy_fb}")
            caption_found = True
            time.sleep(1.0)

        if caption_found:
            _tag = random.choice(MENTION_TAGS) if MENTION_TAGS else MENTION_TAG
            adb(device, f"shell input text '{_tag}'")
            print(f"  ✅ Caption '{_tag}' saisie")
            time.sleep(0.8)

            # Fermer le clavier avec BACK
            adb(device, "shell input keyevent KEYCODE_BACK")
            print(f"  ⌨️ Clavier fermé (BACK)")
            time.sleep(1.0)
            break

        print(f"  ⏳ Écran caption pas encore là ({tick+1}/10)...")
        time.sleep(1)

    # ── Cliquer Share ──────────────────────────────────────────────────────
    print(f"  🔍 Recherche bouton Share...")
    share_found = False
    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_share_post.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_share_post.xml").stdout
        for text in ["Share", "SHARE", "Partager", "Publish", "PUBLISH"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
                rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    print(f"  ✅ Share cliqué ({cx},{cy})")
                    adb(device, f"shell input tap {cx} {cy}")
                    share_found = True
                    break
            if share_found:
                break
        if share_found:
            break
        print(f"  ⏳ Share pas encore là ({tick+1}/10)...")
        time.sleep(1)

    # ── 12. Attendre confirmation + arrêter ───────────────────────────
    print(f"  ⏳ Attente confirmation publication (5s)...")
    time.sleep(5)

    adb(device, "shell uiautomator dump /sdcard/ui_post_confirm.xml")
    time.sleep(0.3)
    xml = adb(device, "shell cat /sdcard/ui_post_confirm.xml").stdout
    if any(kw in xml.lower() for kw in ["post shared", "your post", "for you", "feed"]):
        print(f"  ✅ Post publié confirmé !")
    else:
        print(f"  ⚠️ Confirmation non détectée — on continue quand même")

    print(f"  ⏹ Arrêt téléphone {phone_id}...")
    stop_phone(phone_id)
    return True



def insta_step_birthday(device):
    import random

    res = adb(device, "shell wm size")
    m = re.search(r'(\d+)x(\d+)', res.stdout)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2340)

    print(f"  📅 Étape birthday — résolution {w}x{h}")

    # ── Vérifier qu'on est bien sur l'écran "Set date" ───────────────────
    print(f"  🔍 Attente écran 'Set date' (max 20s)...")
    set_date_ok = False
    for _sd_tick in range(20):
        xml_sd = safe_ui_dump(device, "/sdcard/ui_setdate.xml")
        if any(kw in xml_sd.lower() for kw in [
            "set date", "date of birth", "what's your date",
            "numberpicker", "android.widget.numberpicker",
        ]):
            print(f"  ✅ Écran 'Set date' détecté ({_sd_tick+1}s)")
            set_date_ok = True
            break

        # Si pas encore là, cliquer sur le champ date pour ouvrir le picker
        if _sd_tick == 3:
            print(f"  ⚠️ Picker pas encore là — tap sur champ date...")
            adb(device, f"shell input tap {w//2} {int(h*0.42)}")
            time.sleep(1.0)

        print(f"  ⏳ 'Set date' pas encore là ({_sd_tick+1}/20)...")
        time.sleep(0.7)

    if not set_date_ok:
        print(f"  ⚠️ Écran 'Set date' jamais confirmé — on tente quand même")

    time.sleep(1.0)

    # ── Dump pour trouver les colonnes (retries → robuste sous charge) ───────
    col_bounds_list = []
    for _pick_try in range(4):
        xml_picker = safe_ui_dump(device, "/sdcard/ui_picker.xml")
        for pattern in [
            r'class="android\.widget\.NumberPicker"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            r'class="android\.widget\.ScrollView"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        ]:
            found = re.findall(pattern, xml_picker)
            if found:
                col_bounds_list = found
                break
        if col_bounds_list:
            break
        print(f"  ⏳ Colonnes picker pas trouvées (essai {_pick_try+1}/4) — re-dump...")
        time.sleep(1.0)

    if col_bounds_list:
        cols = sorted(col_bounds_list, key=lambda c: int(c[0]))
        print(f"  ✅ {len(cols)} colonne(s) trouvée(s)")
    else:
        print(f"  ⚠️ Colonnes non trouvées après 4 essais — fallback proportionnel")
        picker_y1 = int(h * 0.50)
        picker_y2 = int(h * 0.72)
        cols = [
            (str(int(w*0.15)), str(picker_y1), str(int(w*0.33)), str(picker_y2)),
            (str(int(w*0.36)), str(picker_y1), str(int(w*0.54)), str(picker_y2)),
            (str(int(w*0.57)), str(picker_y1), str(int(w*0.75)), str(picker_y2)),
        ]

    def col_center(bounds):
        x1, y1, x2, y2 = map(int, bounds)
        return (x1+x2)//2, (y1+y2)//2, y2-y1

    # ── Swipe MOIS ──────────────────────────────────────────────────────────
    if len(cols) >= 1:
        cx, cy, ch = col_center(cols[0])
        swipe_dist = random.randint(int(ch * 1.0), int(ch * 2.0))
        direction  = random.choice([-1, 1])
        y_end = max(cy + 50, min(cy + direction * swipe_dist, h - 50))
        print(f"  📅 Swipe MOIS ({cx},{cy}) → ({cx},{y_end})")
        adb(device, f"shell input swipe {cx} {cy} {cx} {y_end} 400")
        time.sleep(0.8)

    # ── Swipe JOUR ──────────────────────────────────────────────────────────
    if len(cols) >= 2:
        cx, cy, ch = col_center(cols[1])
        swipe_dist = random.randint(int(ch * 1.0), int(ch * 2.0))
        direction  = random.choice([-1, 1])
        y_end = max(cy + 50, min(cy + direction * swipe_dist, h - 50))
        print(f"  📅 Swipe JOUR ({cx},{cy}) → ({cx},{y_end})")
        adb(device, f"shell input swipe {cx} {cy} {cx} {y_end} 400")
        time.sleep(0.8)

    # ── ANNÉE ────────────────────────────────────────────────────────────────
    if len(cols) >= 3:
        cx, cy, ch = col_center(cols[2])
        if ANDROID_VERSION == "Android 13":
            birth_year = str(random.randint(1995, 2005))
            print(f"  📅 Année cible : {birth_year} — Android 13 : log DOM + interaction...")

            # DOM avant toute interaction
            adb(device, "shell uiautomator dump /sdcard/ui_year_before.xml")
            time.sleep(0.3)
            _xml_year_before = adb(device, "shell cat /sdcard/ui_year_before.xml").stdout
            print(f"  [DEBUG YEAR BEFORE] {_xml_year_before}")

            # Premier tap
            adb(device, f"shell input tap {cx} {cy}")
            time.sleep(0.5)
            adb(device, "shell uiautomator dump /sdcard/ui_year_tap1.xml")
            time.sleep(0.3)
            _xml_year_tap1 = adb(device, "shell cat /sdcard/ui_year_tap1.xml").stdout
            print(f"  [DEBUG YEAR TAP1] {_xml_year_tap1}")

            # Deuxième tap
            adb(device, f"shell input tap {cx} {cy}")
            time.sleep(0.5)
            adb(device, "shell uiautomator dump /sdcard/ui_year_tap2.xml")
            time.sleep(0.3)
            _xml_year_tap2 = adb(device, "shell cat /sdcard/ui_year_tap2.xml").stdout
            print(f"  [DEBUG YEAR TAP2] {_xml_year_tap2}")

            # Effacer + saisir
            adb(device, "shell input keyevent KEYCODE_CTRL_A")
            time.sleep(0.2)
            for _ in range(6):
                adb(device, "shell input keyevent KEYCODE_DEL")
                time.sleep(0.05)
            adb(device, f"shell input text '{birth_year}'")
            print(f"  ✅ Année '{birth_year}' saisie")
            time.sleep(0.3)

            # ENTER pour confirmer
            adb(device, "shell input keyevent KEYCODE_ENTER")
            time.sleep(0.5)

            # DOM après ENTER
            adb(device, "shell uiautomator dump /sdcard/ui_year_after_enter.xml")
            time.sleep(0.3)
            _xml_year_enter = adb(device, "shell cat /sdcard/ui_year_after_enter.xml").stdout
            print(f"  [DEBUG YEAR AFTER ENTER] {_xml_year_enter}")
        else:
            birth_year = str(random.randint(1995, 2005))
            print(f"  📅 Année cible : {birth_year} — double-tap colonne année...")
            adb(device, f"shell input tap {cx} {cy}")
            time.sleep(0.3)
            adb(device, f"shell input tap {cx} {cy}")
            time.sleep(0.5)
            adb(device, "shell input keyevent KEYCODE_CTRL_A")
            time.sleep(0.2)
            for _ in range(6):
                adb(device, "shell input keyevent KEYCODE_DEL")
                time.sleep(0.05)
            adb(device, f"shell input text '{birth_year}'")
            print(f"  ✅ Année '{birth_year}' saisie")
            time.sleep(0.5)
            adb(device, "shell input keyevent KEYCODE_BACK")
            time.sleep(0.5)

    # ── Cliquer Set puis Next ────────────────────────────────────────────────
    time.sleep(0.5)

    def _find_bday_btn(xml, labels):
        """Cherche un bouton par text ou content-desc."""
        for lbl in labels:
            for pat in [
                rf'text="{re.escape(lbl)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(lbl)}"',
                rf'content-desc="{re.escape(lbl)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(lbl)}"',
            ]:
                m = re.findall(pat, xml)
                if m:
                    return m[0], lbl
        return None, None

    # Tentatives pour cliquer "Set" (jusqu'à 4 essais avec re-dump fiable)
    btn_found = False
    for _btn_try in range(4):
        xml_next = safe_ui_dump(device, f"/sdcard/ui_insta_bday_next_{_btn_try}.xml")
        coords, found_lbl = _find_bday_btn(xml_next, ["Set", "SET", "OK", "ok", "Done", "DONE"])
        if coords:
            x1, y1, x2, y2 = map(int, coords)
            cx_btn, cy_btn = (x1+x2)//2, (y1+y2)//2
            adb(device, f"shell input tap {cx_btn} {cy_btn}")
            print(f"  ✅ '{found_lbl}' cliqué ({cx_btn},{cy_btn})")
            btn_found = True
            time.sleep(1.5)
            break
        time.sleep(0.5)

    if not btn_found:
        print(f"  ⚠️ 'Set' non trouvé — fallback bas-centre")
        adb(device, f"shell input tap {int(w*0.50)} {int(h*0.88)}")
        time.sleep(1.5)

    # ── Next après Set (jusqu'à 4 essais avec re-dump fiable) ───────────────
    next_found = False
    for _next_try in range(4):
        xml_next2 = safe_ui_dump(device, f"/sdcard/ui_insta_bday_next2_{_next_try}.xml")
        coords2, found_lbl2 = _find_bday_btn(xml_next2, ["Next", "NEXT"])
        if coords2:
            x1, y1, x2, y2 = map(int, coords2)
            cx_btn, cy_btn = (x1+x2)//2, (y1+y2)//2
            adb(device, f"shell input tap {cx_btn} {cy_btn}")
            print(f"  ✅ '{found_lbl2}' cliqué ({cx_btn},{cy_btn})")
            next_found = True
            time.sleep(1.5)
            break
        time.sleep(0.5)

    if not next_found:
        print(f"  ⚠️ 'Next' non trouvé — fallback bas-centre")
        adb(device, f"shell input tap {int(w*0.50)} {int(h*0.88)}")
        time.sleep(1.5)

    print(f"  ✅ Birthday terminé")
    return True



def _tap_and_verify(device, texts, timeout=5, dump_file="ui_verify.xml"):
    """
    Cherche et tape un bouton. Vérifie après timeout secondes qu'il a disparu.
    Retourne True si trouvé et cliqué, False sinon.
    """
    adb(device, f"shell uiautomator dump /sdcard/{dump_file}")
    time.sleep(0.4)
    xml = adb(device, f"shell cat /sdcard/{dump_file}").stdout

    for text in texts:
        for pattern in [
            rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
            rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"',
        ]:
            found = re.findall(pattern, xml)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                adb(device, f"shell input tap {cx} {cy}")
                print(f"  ✅ '{text}' cliqué ({cx},{cy})")
                # Vérification : attendre que le bouton disparaisse
                time.sleep(timeout)
                adb(device, f"shell uiautomator dump /sdcard/{dump_file}")
                time.sleep(0.4)
                xml_after = adb(device, f"shell cat /sdcard/{dump_file}").stdout
                if text not in xml_after:
                    print(f"  ✅ '{text}' bien disparu → étape validée")
                else:
                    print(f"  ⚠️ '{text}' encore présent après {timeout}s → on continue quand même")
                return True

    print(f"  ⚠️ Aucun bouton trouvé parmi {texts} → étape passée")
    return False


def _wait_and_tap(device, texts, wait_max=2, dump_file="ui_wait.xml"):
    """
    Attend qu'un bouton apparaisse (max wait_max secondes) puis le tape.
    Vérifie après 5s qu'il a disparu.
    """
    print(f"  🔍 Attente bouton {texts} (max {wait_max}s)...")
    for tick in range(wait_max):
        adb(device, f"shell uiautomator dump /sdcard/{dump_file}")
        time.sleep(0.5)
        xml = adb(device, f"shell cat /sdcard/{dump_file}").stdout

        for text in texts:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
                rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"',
            ]:
                found = re.findall(pattern, xml)
                if found:
                    x1, y1, x2, y2 = map(int, found[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ '{text}' cliqué ({cx},{cy}) après {tick+1}s")
                    # Vérification post-clic
                    time.sleep(5)
                    adb(device, f"shell uiautomator dump /sdcard/{dump_file}")
                    time.sleep(0.4)
                    xml_after = adb(device, f"shell cat /sdcard/{dump_file}").stdout
                    if text not in xml_after:
                        print(f"  ✅ '{text}' disparu → validé")
                    else:
                        print(f"  ⚠️ '{text}' encore là → on continue")
                    return True

        print(f"  ⏳ Pas encore là ({tick+1}/{wait_max})...")
        time.sleep(0.5)

    print(f"  ⚠️ Timeout — bouton {texts} jamais apparu → étape passée")
    return False


def _handle_notifications_permission(device: str):
    """
    Après sélection du username : gère la popup ALLOW (permission Android notifications)
    puis l'écran 'All Instagram notifications'. Fait un BACK pour revenir sur Instagram.
    """
    print(f"  🔔 Vérification popup ALLOW notifications...")

    # Phase 1 : chercher et cliquer ALLOW (popup système Android)
    _allow_clicked = False
    for _tick in range(8):
        adb(device, "shell uiautomator dump /sdcard/ui_notif_allow.xml")
        time.sleep(0.5)
        xml_a = adb(device, "shell cat /sdcard/ui_notif_allow.xml").stdout

        for _at in ["ALLOW", "Allow", "Allow all", "ALLOW ALL"]:
            for _ap in [
                rf'text="{re.escape(_at)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_at)}"',
            ]:
                _am = re.findall(_ap, xml_a)
                if _am:
                    _x1, _y1, _x2, _y2 = map(int, _am[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    print(f"  ✅ ALLOW cliqué (popup notifications)")
                    _allow_clicked = True
                    time.sleep(1.5)
                    break
            if _allow_clicked:
                break

        if _allow_clicked:
            break

        # Déjà sur écran flow normal → pas de popup Android
        if any(kw in xml_a for kw in [
            "I agree", "I Agree", "AGREE", "Username", "Skip", "SKIP", "Next", "NEXT",
        ]):
            print(f"  ℹ️ Pas de popup ALLOW — flow normal détecté")
            return
        time.sleep(0.5)

    if not _allow_clicked:
        print(f"  ℹ️ Popup ALLOW non trouvée — on continue")
        return

    # Phase 2 : chercher "All Instagram notifications" et cliquer + BACK
    print(f"  🔔 Recherche 'All Instagram notifications'...")
    for _tick in range(12):
        adb(device, "shell uiautomator dump /sdcard/ui_notif_insta.xml")
        time.sleep(0.5)
        xml_n = adb(device, "shell cat /sdcard/ui_notif_insta.xml").stdout

        _found = False
        for _nt in ["All Instagram notifications", "All Instagram"]:
            for _np in [
                rf'text="{re.escape(_nt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nt)}"',
                rf'content-desc="{re.escape(_nt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(_nt)}"',
            ]:
                _nm = re.findall(_np, xml_n)
                if _nm:
                    _x1, _y1, _x2, _y2 = map(int, _nm[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    print(f"  ✅ 'All Instagram notifications' cliqué")
                    _found = True
                    time.sleep(1.0)
                    break
            if _found:
                break

        if _found:
            adb(device, "shell input keyevent KEYCODE_BACK")
            print(f"  ↩️ Back — retour sur Instagram")
            time.sleep(1.5)
            return

        # Déjà de retour sur flow Instagram
        if any(kw in xml_n for kw in [
            "I agree", "I Agree", "AGREE", "Skip", "SKIP", "Next", "NEXT",
        ]):
            print(f"  ℹ️ Retour flow Instagram — notifications gérées")
            return

        print(f"  ⏳ Attente 'All Instagram notifications' ({_tick+1}/12)...")
        time.sleep(0.5)

    print(f"  ℹ️ Écran notifications Instagram non trouvé — on continue")


def insta_step_name_and_flow(device, phone_id=None):
    _username = _phone_usernames.get(str(phone_id)) if phone_id else None
    res = adb(device, "shell wm size")
    m = re.search(r'(\d+)x(\d+)', res.stdout)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2340)

    # ── Étape 1 : What's your name → saisir 'Mia' → Next ────────────────────
    # ── Étape 1 : What's your name / Edit how you'll appear ──────────────────
    # ── Étape 1 : Nom + Username ──────────────────────────────────────────────
    print(f"\n  👤 Étape 1 : Attente écran nom (max 20s)...")

    _name_screen_ok = False
    for _ns_tick in range(20):
        adb(device, "shell uiautomator dump /sdcard/ui_name_wait.xml")
        time.sleep(0.5)
        xml_ns = adb(device, "shell cat /sdcard/ui_name_wait.xml").stdout
        if any(kw in xml_ns for kw in [
            "Full name", "full name", "What's your name",
            "Edit how you'll appear", "Edit how you", "Username"
        ]):
            print(f"  ✅ Écran nom détecté ({_ns_tick+1}s)")
            _name_screen_ok = True
            break
        print(f"  ⏳ Écran nom pas encore là ({_ns_tick+1}/20)...")
        time.sleep(0.7)

    adb(device, "shell uiautomator dump /sdcard/ui_name_field.xml")
    time.sleep(0.5)
    xml_name = adb(device, "shell cat /sdcard/ui_name_field.xml").stdout

    # Détecter le type d'interface
    has_username_field = bool(re.findall(
        r'hint="Username"[^>]*bounds=|bounds=[^>]*hint="Username"', xml_name))
    print(f"  📋 Interface username={has_username_field}")

    # Saisir Full name
    _name_field_clicked = False
    for _nf_hint in ["Full name", "full name", "Name"]:
        for _nf_pat in [
            rf'hint="{re.escape(_nf_hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(_nf_hint)}"',
            rf'text="{re.escape(_nf_hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nf_hint)}"',
        ]:
            _nf_m = re.findall(_nf_pat, xml_name)
            if _nf_m:
                _x1,_y1,_x2,_y2 = map(int,_nf_m[0])
                adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                print(f"  ✅ Champ 'Full name' cliqué")
                _name_field_clicked = True
                time.sleep(1.0)
                break
        if _name_field_clicked:
            break

    if not _name_field_clicked:
        _edits = re.findall(
            r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml_name)
        if _edits:
            _x1,_y1,_x2,_y2 = map(int,_edits[0])
            adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
            time.sleep(1.0)

    adb(device, "shell input keyevent KEYCODE_CTRL_A")
    time.sleep(0.2)
    adb(device, "shell input keyevent KEYCODE_DEL")
    time.sleep(0.2)
    _chosen_name = random.choice(FIRST_NAMES) if FIRST_NAMES else FIRST_NAME
    adb(device, f"shell input text '{_chosen_name}'")
    print(f"  ✅ '{_chosen_name}' saisi")
    time.sleep(0.8)

    # ── Interface A : champ Username présent ─────────────────────────────────
    if has_username_field:
        print(f"  👤 Interface A — saisie username...")
        if not _username:
            import string as _str
            _suffix = ''.join(random.choices(_str.ascii_lowercase, k=4)) + ''.join(random.choices('0123456789', k=2))
            _username = f"mia{_suffix}"

        # Cliquer champ Username
        _usr_clicked = False
        for _up in [
            r'hint="Username"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="Username"',
        ]:
            _um = re.findall(_up, xml_name)
            if _um:
                _x1,_y1,_x2,_y2 = map(int,_um[0])
                adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                _usr_clicked = True
                time.sleep(1.0)
                break

        if not _usr_clicked:
            _edits2 = re.findall(
                r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                xml_name)
            if len(_edits2) >= 2:
                _x1,_y1,_x2,_y2 = map(int,_edits2[1])
                adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                time.sleep(1.0)

        # Effacer le username pré-rempli (méthode fiable : curseur en fin → backspaces)
        try:
            _ucx2, _ucy2 = (_x1+_x2)//2, (_y1+_y2)//2
            adb(device, f"shell input tap {_ucx2} {_ucy2}")
            time.sleep(0.3)
        except NameError:
            pass
        # Curseur à la FIN du texte puis suppression de droite à gauche (aucun résidu)
        adb(device, "shell input keyevent KEYCODE_MOVE_END")
        time.sleep(0.15)
        adb(device, "shell input keyevent " + " ".join(["KEYCODE_DEL"] * 40))
        time.sleep(0.15)
        # Sécurité : retour début + forward-delete au cas où il resterait du texte
        adb(device, "shell input keyevent KEYCODE_MOVE_HOME")
        time.sleep(0.15)
        adb(device, "shell input keyevent " + " ".join(["KEYCODE_FORWARD_DEL"] * 40))
        time.sleep(0.2)
        adb(device, f"shell input text '{_username}'")
        print(f"  ✅ Username '{_username}' saisi")
        time.sleep(1.5)



        adb(device, "shell input keyevent KEYCODE_BACK")
        time.sleep(0.5)

        # Cliquer Next
        for _n1_tick in range(15):
            adb(device, "shell uiautomator dump /sdcard/ui_name_next.xml")
            time.sleep(0.5)
            xml_n1 = adb(device, "shell cat /sdcard/ui_name_next.xml").stdout
            _next1_clicked = False
            for _nt in ["Next", "NEXT"]:
                for _np in [
                    rf'text="{re.escape(_nt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nt)}"',
                ]:
                    _nm = re.findall(_np, xml_n1)
                    if _nm:
                        _node_str = xml_n1[max(0, xml_n1.find(_nt)-200):xml_n1.find(_nt)+50]
                        if 'enabled="false"' in _node_str:
                            print(f"  ⏳ Next grisé ({_n1_tick+1}/15)...")
                            break
                        _x1,_y1,_x2,_y2 = map(int,_nm[0])
                        adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                        print(f"  ✅ Next cliqué")
                        _next1_clicked = True
                        time.sleep(2.0)
                        break
                if _next1_clicked:
                    break
            if _next1_clicked:
                break
            time.sleep(0.5)

        # ── Vérif : a-t-on quitté l'écran nom ? (5s) — sinon compte bugué ──
        time.sleep(5)
        if still_on_name_screen(device):
            print(f"  🐛 Toujours sur l'écran nom 5s après Next — compte bugué, suppression du téléphone")
            if phone_id:
                try:
                    delete_phone_geelark(phone_id)
                    print(f"  🗑️ Profil supprimé (bloqué écran nom) : {phone_id}")
                except Exception as _e_stuck:
                    print(f"  ⚠️ Erreur suppression : {_e_stuck}")
            return "name_screen_stuck"

    # ── Interface B : Full name seulement → Next direct ──────────────────────
    else:
        print(f"  👤 Interface B — Next direct après Full name")
        adb(device, "shell input keyevent KEYCODE_BACK")
        time.sleep(0.5)

        _next1_clicked = False
        for _n1_tick in range(15):
            adb(device, "shell uiautomator dump /sdcard/ui_name_next.xml")
            time.sleep(0.5)
            xml_n1 = adb(device, "shell cat /sdcard/ui_name_next.xml").stdout
            for _nt in ["Next", "NEXT"]:
                for _np in [
                    rf'text="{re.escape(_nt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nt)}"',
                ]:
                    _nm = re.findall(_np, xml_n1)
                    if _nm:
                        _node_str = xml_n1[max(0, xml_n1.find(_nt)-200):xml_n1.find(_nt)+50]
                        if 'enabled="false"' in _node_str:
                            print(f"  ⏳ Next grisé ({_n1_tick+1}/15)...")
                            break
                        _x1,_y1,_x2,_y2 = map(int,_nm[0])
                        adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                        print(f"  ✅ Next (Interface B) cliqué")
                        _next1_clicked = True
                        time.sleep(2.0)
                        break
                if _next1_clicked:
                    break
            if _next1_clicked:
                break
            time.sleep(0.5)

        # ── Vérif : a-t-on quitté l'écran nom ? (5s) — sinon compte bugué ──
        time.sleep(5)
        if still_on_name_screen(device):
            print(f"  🐛 Toujours sur l'écran nom 5s après Next — compte bugué, suppression du téléphone")
            if phone_id:
                try:
                    delete_phone_geelark(phone_id)
                    print(f"  🗑️ Profil supprimé (bloqué écran nom) : {phone_id}")
                except Exception as _e_stuck:
                    print(f"  ⚠️ Erreur suppression : {_e_stuck}")
            return "name_screen_stuck"

        # ── Interface B étape 2 : écran Username seul ─────────────────────────
        print(f"  👤 Interface B étape 2 : attente écran Username...")
        if not _username:
            import string as _str
            _suffix = ''.join(random.choices(_str.ascii_lowercase, k=4)) + ''.join(random.choices('0123456789', k=2))
            _username = f"mia{_suffix}"

        for _us_tick in range(15):
            adb(device, "shell uiautomator dump /sdcard/ui_username_screen.xml")
            time.sleep(0.5)
            xml_us = adb(device, "shell cat /sdcard/ui_username_screen.xml").stdout

            if not any(kw in xml_us for kw in ["Username", "username"]):
                print(f"  ⏳ Écran username pas encore ({_us_tick+1}/15)...")
                time.sleep(0.7)
                continue

            print(f"  ✅ Écran Username détecté")

            # Cliquer le champ
            _us_clicked = False
            for _up in [
                r'hint="Username"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="Username"',
            ]:
                _um = re.findall(_up, xml_us)
                if _um:
                    _x1,_y1,_x2,_y2 = map(int,_um[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    _us_clicked = True
                    time.sleep(1.0)
                    break

            if not _us_clicked:
                _edits_us = re.findall(
                    r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    xml_us)
                if _edits_us:
                    _x1,_y1,_x2,_y2 = map(int,_edits_us[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    time.sleep(1.0)

            # Effacer le username pré-rempli (méthode fiable : curseur en fin → backspaces)
            adb(device, "shell input keyevent KEYCODE_MOVE_END")
            time.sleep(0.15)
            adb(device, "shell input keyevent " + " ".join(["KEYCODE_DEL"] * 40))
            time.sleep(0.15)
            # Sécurité : retour début + forward-delete au cas où il resterait du texte
            adb(device, "shell input keyevent KEYCODE_MOVE_HOME")
            time.sleep(0.15)
            adb(device, "shell input keyevent " + " ".join(["KEYCODE_FORWARD_DEL"] * 40))
            time.sleep(0.2)
            adb(device, f"shell input text '{_username}'")
            print(f"  ✅ Username '{_username}' saisi")
            time.sleep(1.5)



            adb(device, "shell input keyevent KEYCODE_BACK")
            time.sleep(0.5)

            # Next
            for _n2_tick in range(15):
                adb(device, "shell uiautomator dump /sdcard/ui_username_next.xml")
                time.sleep(0.5)
                xml_n2 = adb(device, "shell cat /sdcard/ui_username_next.xml").stdout
                _next2_clicked = False
                for _nt in ["Next", "NEXT"]:
                    for _np in [
                        rf'text="{re.escape(_nt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nt)}"',
                    ]:
                        _nm2 = re.findall(_np, xml_n2)
                        if _nm2:
                            _node_str2 = xml_n2[max(0, xml_n2.find(_nt)-200):xml_n2.find(_nt)+50]
                            if 'enabled="false"' in _node_str2:
                                print(f"  ⏳ Next grisé ({_n2_tick+1}/15)...")
                                break
                            _x1,_y1,_x2,_y2 = map(int,_nm2[0])
                            adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                            print(f"  ✅ Next username cliqué")
                            _next2_clicked = True
                            time.sleep(2.0)
                            break
                    if _next2_clicked:
                        break
                if _next2_clicked:
                    break
                time.sleep(0.5)
            break



    # ── Étape 2 : Next (écran username — rien à saisir, juste Next) ──────────
# ── Étape 2 : Next (écran username — rien à saisir, juste Next) ──────────
    print(f"\n  👤 Étape 2 : Écran username — Next direct")
    _next2_clicked = False
    _jump_to_agree = False  # si I agree déjà visible → sauter l'étape Next
    for _n2_tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_username.xml")
        time.sleep(0.5)
        xml_n2 = adb(device, "shell cat /sdcard/ui_username.xml").stdout

        # ── Raccourci : si "I agree" déjà visible → on saute direct au clic I agree ──
        if any(kw in xml_n2 for kw in ["I agree", "I Agree", "AGREE"]):
            print(f"  ⚡ 'I agree' détecté — on saute l'étape Next et on va direct au clic I agree")
            _next2_clicked = True
            _jump_to_agree = True
            break

        # Détecter si on est sur l'écran username
        _on_username = any(kw in xml_n2 for kw in [
            "Username", "username", "Suggest", "suggest"
        ])
        _on_name = any(kw in xml_n2 for kw in [
            "Full name", "What's your name", "Edit how you"
        ])

        if _on_name and not _on_username:
            print(f"  ⚠️ Encore sur écran nom ({_n2_tick+1}/15) — retry Next...")
            for _nt in ["Next", "NEXT"]:
                for _np in [
                    rf'text="{re.escape(_nt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nt)}"',
                ]:
                    _nm = re.findall(_np, xml_n2)
                    if _nm:
                        _x1,_y1,_x2,_y2 = map(int,_nm[0])
                        adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                        print(f"  🔄 Next retry sur écran nom")
                        time.sleep(1.5)
                        break
            continue

        for _nt in ["Next", "NEXT"]:
            for _np in [
                rf'text="{re.escape(_nt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nt)}"',
            ]:
                _nm = re.findall(_np, xml_n2)
                if _nm:
                    _x1,_y1,_x2,_y2 = map(int,_nm[0])
                    _cx,_cy = (_x1+_x2)//2, (_y1+_y2)//2
                    adb(device, f"shell input tap {_cx} {_cy}")
                    print(f"  ✅ Next (username) cliqué ({_cx},{_cy})")
                    _next2_clicked = True
                    time.sleep(2.0)
                    break
            if _next2_clicked:
                break
        if _next2_clicked:
            break
        print(f"  ⏳ Écran username pas encore là ({_n2_tick+1}/10)...")
        time.sleep(0.7)

    # ── Étape 2 : Next (écran username/suggestion) ───────────────────────────
    #    Sautée si 'I agree' déjà détecté à l'écran username
    if not _jump_to_agree:
        print(f"\n  👤 Étape 2 : Next")
        _wait_and_tap(device, ["Next", "NEXT"], wait_max=3, dump_file="ui_step2.xml")
    else:
        print(f"\n  ⏭️ Étape 2 (Next) sautée — 'I agree' déjà présent")

    # ── Étape 3 : I agree ────────────────────────────────────────────────────
    print(f"\n  ✅ Étape 3 : I agree")
    _wait_and_tap(device, ["I agree", "I Agree", "AGREE"], wait_max=3, dump_file="ui_agree.xml")

    # ── Démarrage direct si 'Next' déjà présent ───────────────────────────────
    #    Si un bouton Next apparaît tout de suite après I agree, on lance le flow
    #    immédiatement, sans attendre 10s ni checker les popups human/try-again.
    adb(device, "shell uiautomator dump /sdcard/ui_post_agree.xml")
    time.sleep(0.5)
    _xml_post_agree = adb(device, "shell cat /sdcard/ui_post_agree.xml").stdout
    _next_present = False
    for _nt in ["Next", "NEXT"]:
        if (re.findall(rf'text="{re.escape(_nt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', _xml_post_agree)
                or re.findall(rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nt)}"', _xml_post_agree)):
            _next_present = True
            break

    if _next_present:
        print(f"  ⚡ Bouton 'Next' présent — démarrage direct du flow (sans attente ni check popup)")
    else:
        print(f"  ⏳ Attente création compte (12s)...")
        time.sleep(10)

        print(f"  🔍 Vérification human / try-again...")
        adb(device, "shell uiautomator dump /sdcard/ui_human_check.xml")
        time.sleep(0.5)
        xml_human = adb(device, "shell cat /sdcard/ui_human_check.xml").stdout
        if any(kw in xml_human for kw in [
            "Confirm you're human", "Confirm you\u2019re human",
            "to use your account",
        ]):
            print(f"  🚫 'Confirm you're human' détecté — suppression profil...")
            try:
                delete_phone_geelark(phone_id)
            except Exception as e:
                print(f"  ⚠️ Erreur suppression : {e}")
            return "human_check_banned"
        time.sleep(3)

        # ── Vérification popup "Try again later" après I agree ───────────────────
        print(f"  🔍 Vérification popup 'Try again later'...")
        adb(device, "shell uiautomator dump /sdcard/ui_try_again.xml")
        time.sleep(0.5)
        xml_try = adb(device, "shell cat /sdcard/ui_try_again.xml").stdout
        if any(kw in xml_try for kw in ["Try again later", "We limit how often", "try again later"]):
            print(f"  🚫 Popup 'Try again later' détectée — numéro banni, suppression profil...")
            try:
                delete_phone_geelark(phone_id)
                print(f"  ✅ Profil supprimé : {phone_id}")
            except Exception as e:
                print(f"  ⚠️ Erreur suppression : {e}")
            return "try_again_later_banned"

    # ── Étape 4 → 8B : boucle unifiée prioritaire (20 rounds) ───────────────
    _human_kw = [
        "Confirm you're human", "Confirm you're human",
        "community standards on account integrity",
    ]
    _try_later_kw = ["Try again later", "We limit how often", "try again later"]
    _add_photo_kw = [
        "Add a photo", "add a photo", "ADD A PHOTO",
        "Add photo", "add photo",
        "Choose photo", "choose photo",
        "Upload photo", "upload photo",
        "Add picture", "Add a picture",
        "Add profile photo", "add profile photo",
        "Set a photo",
    ]
    _search_kw = ["Search", "SEARCH", "com.instagram.android:id/search"]
    _residual_buttons = [
        "Allow All", "ALLOW ALL", "Allow all",
        "Allow", "ALLOW",
        "Done", "DONE",
        "Skip", "SKIP", "No, skip",
        "Next", "NEXT",
        "Not now", "NOT NOW",
        "No Thanks", "No thanks",
        "Continue", "CONTINUE",
    ]

    print(f"\n  ⏭️ Étape 4→8B : boucle unifiée (max 20 rounds)...")
    _main_loop_done = False
    _flow_suggestions_done = False
    for _round in range(20):
        adb(device, f"shell uiautomator dump /sdcard/ui_main_{_round}.xml")
        time.sleep(0.5)
        _xml = adb(device, f"shell cat /sdcard/ui_main_{_round}.xml").stdout

        # ── Ban checks ──────────────────────────────────────────────────────
        if any(kw in _xml for kw in _human_kw):
            print(f"  🚫 ‘Confirm you're human' (round {_round+1}) — suppression profil...")
            try:
                delete_phone_geelark(phone_id)
            except Exception:
                pass
            return "human_check_banned"
        if any(kw in _xml for kw in _try_later_kw):
            print(f"  🚫 ‘Try again later' (round {_round+1}) — suppression profil...")
            try:
                delete_phone_geelark(phone_id)
            except Exception:
                pass
            return "try_again_later_banned"

        # ── PRIORITÉ 0 : Popup Google "Choose a phone number" → tap en haut ──
        #    Cette popup (sélecteur de numéro Google) recouvre l'écran. On la
        #    ferme en tapant tout en haut de l'écran (zone grisée hors popup).
        _google_popup_kw = [
            "Choose a phone number",
            "Google won't store", "Google won’t store",
            "choose a phone number that",
            "phone number sharing",
        ]
        if any(kw in _xml for kw in _google_popup_kw):
            _gres = adb(device, "shell wm size")
            _gm_size = re.search(r'(\d+)x(\d+)', _gres.stdout)
            _gw, _gh = (int(_gm_size.group(1)), int(_gm_size.group(2))) if _gm_size else (1080, 2340)
            adb(device, f"shell input tap {_gw//2} {int(_gh*0.06)}")
            print(f"  🔕 Popup Google 'Choose a phone number' détectée — tap en haut pour fermer")
            time.sleep(1.2)
            continue

        # ── PRIORITÉ 1 : Got it → clic + force-stop + fin ──────────────────
        _gotit_found = False
        for _gkw in ["Got it", "GOT IT", "Got It"]:
            for _gp in [
                rf'text="{re.escape(_gkw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_gkw)}"',
            ]:
                _gm = re.findall(_gp, _xml)
                if _gm:
                    _x1, _y1, _x2, _y2 = map(int, _gm[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    print(f"  ✅ ‘Got it' cliqué (round {_round+1}) — fermeture Instagram")
                    time.sleep(1)
                    adb(device, "shell am force-stop com.instagram.android")
                    _gotit_found = True
                    _main_loop_done = True
                    break
            if _gotit_found:
                break
        if _main_loop_done:
            break

        # ── PRIORITÉ 1.5 : Bottom-sheet "Add picture" → Choose from Gallery ──
        #    Si on voit "Choose from Gallery", on lance le flow complet :
        #    gallery → Allow all → sélection photo → Next/Done.
        if any(kw in _xml for kw in ["Choose from Gallery", "Choose from gallery"]):
            print(f"  🖼️ 'Choose from Gallery' détecté (round {_round+1}) → flow photo profil")
            _add_profile_picture_from_gallery(device)
            time.sleep(1.5)
            continue

        # ── PRIORITÉ 2 : Photo de profil → tap + continue ──────────────────
        _photo_found = False
        for _pk in _add_photo_kw:
            for _pp in [
                rf'text="{re.escape(_pk)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_pk)}"',
                rf'content-desc="{re.escape(_pk)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(_pk)}"',
            ]:
                _pm = re.findall(_pp, _xml)
                if _pm:
                    _x1, _y1, _x2, _y2 = map(int, _pm[0])
                    _cx, _cy = (_x1+_x2)//2, (_y1+_y2)//2
                    print(f"  📸 Photo profil ‘{_pk}' ({_cx},{_cy}) — tap (round {_round+1})")
                    adb(device, f"shell input tap {_cx} {_cy}")
                    _photo_found = True
                    time.sleep(2.0)
                    break
            if _photo_found:
                break
        if _photo_found:
            continue

        # ── PRIORITÉ 3 : Search → flow follow suggestions ──────────────────
        if not _flow_suggestions_done and any(kw in _xml for kw in _search_kw):
            print(f"  ✅ Search détecté (round {_round+1}) → flow follow suggestions")
            _flow_follow_suggestions(device)
            _flow_suggestions_done = True
            time.sleep(1)
            continue

        # ── PRIORITÉ 4 : "All Instagram notifications" → tap + back ────────
        _notif_found = False
        for _nkw in ["All Instagram notifications", "All Instagram"]:
            for _np in [
                rf'text="{re.escape(_nkw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nkw)}"',
                rf'content-desc="{re.escape(_nkw)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            ]:
                _nm = re.findall(_np, _xml)
                if _nm:
                    _x1, _y1, _x2, _y2 = map(int, _nm[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    print(f"  🔔 ‘All Instagram notifications' → back (round {_round+1})")
                    time.sleep(1.0)
                    adb(device, "shell input keyevent KEYCODE_BACK")
                    time.sleep(1.5)
                    _notif_found = True
                    break
            if _notif_found:
                break
        if _notif_found:
            continue

        # ── PRIORITÉ 5 : Boutons résiduels ─────────────────────────────────
        _res_found = False
        for _rb in _residual_buttons:
            for _rp in [
                rf'text="{re.escape(_rb)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_rb)}"',
                rf'content-desc="{re.escape(_rb)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(_rb)}"',
            ]:
                _rm = re.findall(_rp, _xml)
                if _rm:
                    _x1, _y1, _x2, _y2 = map(int, _rm[0])
                    _cx, _cy = (_x1+_x2)//2, (_y1+_y2)//2
                    adb(device, f"shell input tap {_cx} {_cy}")
                    print(f"  ⏭️ ‘{_rb}' cliqué ({_cx},{_cy}) [round {_round+1}/20]")
                    _res_found = True
                    time.sleep(2.0)
                    break
            if _res_found:
                break
        if not _res_found:
            print(f"  ℹ️ Rien à cliquer (round {_round+1}/20) — attente 1.5s...")
            time.sleep(1.5)

    # ── Validation finale : "Got it" cliqué = compte réellement terminé ──────
    #    Si "Got it" n'a jamais été atteint, la création n'a PAS abouti →
    #    on supprime le profil (compte incomplet) au lieu de le sauvegarder.
    if not _main_loop_done:
        print(f"  ⚠️ 'Got it' jamais atteint après 20 rounds — compte INCOMPLET")
        try:
            delete_phone_geelark(phone_id)
            print(f"  🗑️ Profil supprimé (création non terminée) : {phone_id}")
        except Exception as _e_inc:
            print(f"  ⚠️ Erreur suppression profil incomplet : {_e_inc}")
        return "incomplete_no_gotit"

    save_created_account(_username, phone_id)
    print(f"\n  ✅ Flow Instagram complet !")
    return True  # succès : 'Got it' cliqué + compte sauvegardé



def post_story_on_device(phone_id: str, media_path: str,
                         add_to_highlight: bool = False,
                         highlight_name: str = "tuto 1") -> bool:
    """
    Lance le téléphone, ouvre Instagram, clique sur 'Your story',
    sélectionne le média et publie la story.
    Si add_to_highlight=True, ajoute ensuite la story à la une highlight_name.
    """
    print(f"  📸 Story → téléphone {phone_id} | highlight={add_to_highlight} '{highlight_name}'")
    if not check_account_age_warning(phone_id, "publication story"):
        return False

    # ── 1. Démarrer le téléphone ──────────────────────────────────────
    ok = start_phone_with_retry(phone_id)
    if not ok:
        return False
    time.sleep(15)

    # ── 2. Activer ADB ────────────────────────────────────────────────
    enable_adb(phone_id)
    time.sleep(5)

    # ── 3. Attendre ADB ───────────────────────────────────────────────
    device, pwd = wait_for_adb(phone_id, max_wait=150)
    if not device:
        print(f"  ❌ ADB timeout pour {phone_id}")
        stop_phone(phone_id)
        return False

    # ── 4. Connexion glogin ───────────────────────────────────────────
    connected = False
    for attempt in range(30):
        subprocess.run(f'"{ADB_PATH}" connect {device}', shell=True, capture_output=True)
        time.sleep(3)
        result = subprocess.run(
            f'"{ADB_PATH}" -s {device} shell glogin {pwd}',
            shell=True, capture_output=True, text=True
        )
        print(f"  glogin [{attempt+1}] → {result.stdout.strip()}")
        if "success" in result.stdout.lower():
            connected = True
            break

    if not connected:
        print(f"  ❌ glogin échoué pour {phone_id}")
        stop_phone(phone_id)
        return False

    # ── 5. Pousser le média sur le téléphone ─────────────────────────
    import os
    filename  = os.path.basename(media_path)
    remote    = f"/sdcard/DCIM/story_media/{filename}"
    adb(device, "shell mkdir -p /sdcard/DCIM/story_media")
    push_result = subprocess.run(
        [ADB_PATH, "-s", device, "push", media_path, remote],
        capture_output=True, text=True
    )
    if push_result.returncode != 0:
        print(f"  ❌ Push média échoué : {push_result.stderr.strip()[:80]}")
        stop_phone(phone_id)
        return False
    print(f"  ✅ Média poussé : {remote}")

    # Scanner la galerie
    adb(device, f"shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{remote}")
    time.sleep(3)

    # ── 6. Ouvrir Instagram ───────────────────────────────────────────
    print(f"  📱 Ouverture Instagram...")
    adb(device, "shell am force-stop com.instagram.android")
    time.sleep(1)
    subprocess.run(
        f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
        f'-c android.intent.category.LAUNCHER 1',
        shell=True, capture_output=True, text=True
    )
    time.sleep(5)
    _click_allow_if_present(device)
    time.sleep(1)
    _click_allow_if_present(device)

    # Attendre que le feed soit chargé
    feed_ok = False
    for tick in range(20):
        adb(device, "shell uiautomator dump /sdcard/ui_feed.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_feed.xml").stdout
        if _detect_logged_out_and_cleanup(device, phone_id, xml):
            stop_phone(phone_id)
            return False
        if any(kw in xml for kw in [
            "Your story", "your story",
            "com.instagram.android:id/feed_tab",
            "For you", "Suggested for you",
        ]):
            print(f"  ✅ Feed Instagram détecté ({tick+1}s)")
            feed_ok = True
            break
        print(f"  ⏳ Attente feed ({tick+1}/20)...")
        time.sleep(1)

    if not feed_ok:
        print(f"  ⚠️ Feed jamais détecté — on tente quand même")

    # ── 7. Cliquer sur 'Your story' ───────────────────────────────────
# ── 7. Dump page feed pour trouver le bon bouton 'Your story' ─────────────
    print(f"  🔍 Dump page feed pour identifier bouton 'Your story'...")
    story_clicked = False

    for tick in range(15):
        adb(device, "shell uiautomator dump /sdcard/ui_story.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_story.xml").stdout

        # Debug complet de la page
        import re as _re3
        all_texts3  = _re3.findall(r'text="([^"]+)"', xml)
        all_descs3  = _re3.findall(r'content-desc="([^"]+)"', xml)
        all_rids3   = _re3.findall(r'resource-id="([^"]+)"', xml)
        clickables3 = _re3.findall(
            r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        if not clickables3:
            clickables3 = _re3.findall(
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"', xml)

        print(f"  📋 Textes  : {[t for t in all_texts3 if t.strip()][:30]}")
        print(f"  📋 Descs   : {[d for d in all_descs3 if d.strip()][:30]}")
        print(f"  📋 Ids     : {[r for r in all_rids3 if r.strip() and 'instagram' in r][:20]}")
        print(f"  📋 Cliquables ({len(clickables3)}) :")
        for coords in clickables3[:25]:
            x1, y1, x2, y2 = map(int, coords)
            cx, cy = (x1+x2)//2, (y1+y2)//2
            bw, bh = x2-x1, y2-y1
            print(f"    ({cx:4d},{cy:4d}) {bw:4d}x{bh:4d}")

        # Attendre que la page soit chargée (au moins 3 cliquables)
        if len(clickables3) >= 3:
            print(f"  ✅ Page chargée ({tick+1}s) — long press 'Your story'...")

            # Étape 1 : long press sur le cercle "Your story" pour ouvrir le menu
            print(f"  👆 Long press sur 'Your story' pour ouvrir le menu...")
            adb(device, f"shell input swipe 131 280 131 280 800")
            time.sleep(2.0)

            # Étape 2 : dump pour voir le menu apparu
            adb(device, "shell uiautomator dump /sdcard/ui_story_menu.xml")
            time.sleep(0.5)
            xml_menu = adb(device, "shell cat /sdcard/ui_story_menu.xml").stdout
            all_texts_menu = _re3.findall(r'text="([^"]+)"', xml_menu)
            all_descs_menu = _re3.findall(r'content-desc="([^"]+)"', xml_menu)
            print(f"  📋 Menu textes : {[t for t in all_texts_menu if t.strip()][:20]}")
            print(f"  📋 Menu descs  : {[d for d in all_descs_menu if d.strip()][:20]}")

            # Étape 3 : cliquer sur "Add to your story" dans le menu
            menu_clicked = False
            for text in ["Add to your story", "Add to story", "New story", "Nouvelle story"]:
                for pattern in [
                    rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
                    rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"',
                ]:
                    matches = _re3.findall(pattern, xml_menu)
                    if matches:
                        x1, y1, x2, y2 = map(int, matches[0])
                        cx, cy = (x1+x2)//2, (y1+y2)//2
                        print(f"  ✅ '{text}' trouvé ({cx},{cy}) — tap")
                        adb(device, f"shell input tap {cx} {cy}")
                        menu_clicked = True
                        story_clicked = True
                        break
                if menu_clicked:
                    break

            if not menu_clicked:
                print(f"  ⚠️ Menu non trouvé — fallback long press + coordonnées fixes")
                adb(device, f"shell input swipe 131 280 131 280 800")
                time.sleep(2.0)
                adb(device, f"shell input tap 131 280")
                story_clicked = True

            break

        print(f"  ⏳ Page pas encore chargée ({tick+1}/15)...")
        time.sleep(1)

    if not story_clicked:
        print(f"  ❌ 'Your story' jamais cliqué — arrêt")
        stop_phone(phone_id)
        return False

    time.sleep(3)

    # ── 7b. Gérer permissions "WHILE USING THE APP" et "ALLOW ALL" ───────────
    print(f"  🔍 Vérification permissions après tap 'Your story'...")
    for _perm_round in range(2):
        for _tick in range(2):
            adb(device, "shell uiautomator dump /sdcard/ui_perm.xml")
            time.sleep(0.5)
            xml_perm = adb(device, "shell cat /sdcard/ui_perm.xml").stdout
            perm_found = False
            for text in ["WHILE USING THE APP", "While using the app", "While Using the App"]:
                for pattern in [
                    rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
                ]:
                    matches = re.findall(pattern, xml_perm)
                    if matches:
                        x1, y1, x2, y2 = map(int, matches[0])
                        cx, cy = (x1+x2)//2, (y1+y2)//2
                        print(f"  ✅ 'WHILE USING THE APP' trouvé ({_perm_round+1}/2) ({cx},{cy}) — tap")
                        adb(device, f"shell input tap {cx} {cy}")
                        perm_found = True
                        time.sleep(1.5)
                        break
                if perm_found:
                    break
            if perm_found:
                break
            print(f"  ⏳ 'WHILE USING THE APP' pas encore là ({_tick+1}/5)...")
            time.sleep(0.5)

    # "ALLOW ALL" si présent
    for _tick in range(2):
        adb(device, "shell uiautomator dump /sdcard/ui_allowall.xml")
        time.sleep(0.5)
        xml_allow = adb(device, "shell cat /sdcard/ui_allowall.xml").stdout
        allow_found = False
        for text in ["ALLOW ALL", "Allow All", "Allow all"]:
            for pattern in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
            ]:
                matches = re.findall(pattern, xml_allow)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    print(f"  ✅ 'ALLOW ALL' trouvé ({cx},{cy}) — tap")
                    adb(device, f"shell input tap {cx} {cy}")
                    allow_found = True
                    time.sleep(1.5)
                    break
            if allow_found:
                break
        if allow_found:
            break
        print(f"  ⏳ 'ALLOW ALL' pas encore là ({_tick+1}/5)...")
        time.sleep(0.5)

    # ── 8. Sélectionner la dernière photo poussée ─────────────────────────────
    print(f"  🔍 Recherche de la dernière photo dans la galerie story...")
    import os as _os
    filename = _os.path.basename(media_path)

    photo_clicked = False

    print(f"  ⏳ Attente chargement galerie (3s)...")


    # Dump XML pour debug — voir ce qui est sur l'écran
    adb(device, "shell uiautomator dump /sdcard/ui_debug_gallery.xml")
    time.sleep(0.5)
    xml_debug = adb(device, "shell cat /sdcard/ui_debug_gallery.xml").stdout
    # Extraire et afficher tous les éléments avec leurs bounds
    import re as _re
    all_nodes = _re.findall(
        r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*(?:text|content-desc)="([^"]*)"',
        xml_debug
    )
    if not all_nodes:
        all_nodes = _re.findall(
            r'(?:text|content-desc)="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml_debug
        )
        print(f"  📋 XML galerie ({len(all_nodes)} éléments) :")
        for t, x1, y1, x2, y2 in all_nodes[:20]:
            cx, cy = (int(x1)+int(x2))//2, (int(y1)+int(y2))//2
            if t.strip():
                print(f"    ({cx},{cy}) '{t[:50]}'")
    else:
        print(f"  📋 XML galerie ({len(all_nodes)} éléments) :")
        for x1, y1, x2, y2, t in all_nodes[:20]:
            cx, cy = (int(x1)+int(x2))//2, (int(y1)+int(y2))//2
            if t.strip():
                print(f"    ({cx},{cy}) '{t[:50]}'")

    # Aussi afficher tous les cliquables sans texte
    clickables = _re.findall(
        r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        xml_debug
    )
    print(f"  📋 Cliquables ({len(clickables)}) :")
    for x1, y1, x2, y2 in clickables[:15]:
        cx, cy = (int(x1)+int(x2))//2, (int(y1)+int(y2))//2
        bw, bh = int(x2)-int(x1), int(y2)-int(y1)
        print(f"    ({cx},{cy}) {bw}×{bh}")

    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_story_gallery.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_story_gallery.xml").stdout

        # Méthode 1 : chercher par nom de fichier exact dans content-desc
        for pattern in [
            rf'content-desc="[^"]*{re.escape(filename)}[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="[^"]*{re.escape(filename)}[^"]*"',
        ]:
            matches = re.findall(pattern, xml)
            if matches:
                x1, y1, x2, y2 = map(int, matches[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                print(f"  ✅ Photo '{filename}' trouvée par nom ({cx},{cy})")
                adb(device, f"shell input tap {cx} {cy}")
                photo_clicked = True
                break

        if photo_clicked:
            break

        # Méthode 2 : prendre la première image cliquable dans la grille
       # Méthode 2 : coordonnées fixes — dernière photo toujours au centre, 60% hauteur
        if not photo_clicked:
            res_size = adb(device, "shell wm size")
            m = re.search(r'(\d+)x(\d+)', res_size.stdout)
            w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2340)

            cx = w // 2
            cy = int(h * 0.365)  # 877/2400 = 36.5%
            print(f"  🎯 Tap coordonnées fixes dernière photo ({cx},{cy})")
            adb(device, f"shell input tap {cx} {cy}")
            photo_clicked = True

        if photo_clicked:
            break

        print(f"  ⏳ Photo pas trouvée ({tick+1}/10)...")
        time.sleep(1)

    if not photo_clicked:
        print(f"  ⚠️ Photo non trouvée — on continue quand même")

    time.sleep(2)

    # ── 8b. Fermer toutes les popups après sélection photo ───────────────────────
    print(f"  🔍 Fermeture des popups après sélection photo (max 3 tentatives)...")
    for _popup_round in range(3):
        adb(device, "shell uiautomator dump /sdcard/ui_ok_popup.xml")
        time.sleep(0.5)
        xml_ok = adb(device, "shell cat /sdcard/ui_ok_popup.xml").stdout

        _popup_found = False

        # ── Détecter dialog popup (story-to-story sharing, cookies, etc.) ────────
        _has_dialog = any(kw in xml_ok for kw in [
            "dialog_container", "igds_promo_dialog",
            "story sharing", "story-to-story",
            "Introducing", "cookie", "Cookie",
            "decline", "Decline",
        ])

        if _has_dialog:
            print(f"  🔔 Dialog popup détecté [{_popup_round+1}/3] — tap haut écran...")
            _res_p = adb(device, "shell wm size")
            _mp = re.search(r'(\d+)x(\d+)', _res_p.stdout)
            if _mp:
                _wp, _hp = int(_mp.group(1)), int(_mp.group(2))
                adb(device, f"shell input tap {_wp // 2} {int(_hp * 0.08)}")
                print(f"  ✅ Tap haut écran ({_wp // 2},{int(_hp * 0.08)})")
                time.sleep(1.5)

            # Vérifier si le dialog est fermé
            adb(device, "shell uiautomator dump /sdcard/ui_dialog_verify.xml")
            time.sleep(0.4)
            xml_dv = adb(device, "shell cat /sdcard/ui_dialog_verify.xml").stdout
            if "dialog_container" not in xml_dv and "igds_promo_dialog" not in xml_dv:
                print(f"  ✅ Dialog fermé")
                _popup_found = True

        # ── Chercher bouton "Decline optional cookies" / "Decline" ───────────────
        for _dcl in ["Decline optional cookies", "Decline Optional Cookies", "Decline"]:
            for _dpat in [
                rf'text="{re.escape(_dcl)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_dcl)}"',
                rf'content-desc="{re.escape(_dcl)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(_dcl)}"',
            ]:
                _dm = re.findall(_dpat, xml_ok)
                if _dm:
                    _x1, _y1, _x2, _y2 = map(int, _dm[0])
                    _cx, _cy = (_x1+_x2)//2, (_y1+_y2)//2
                    adb(device, f"shell input tap {_cx} {_cy}")
                    print(f"  ✅ '{_dcl}' cliqué ({_cx},{_cy})")
                    _popup_found = True
                    time.sleep(1.5)
                    break
            if _popup_found:
                break

        # ── Chercher bouton ALLOW (vérifié 2 fois) ───────────────────────────────
        for _allow_check in range(2):
            adb(device, "shell uiautomator dump /sdcard/ui_allow_popup.xml")
            time.sleep(0.5)
            xml_allow = adb(device, "shell cat /sdcard/ui_allow_popup.xml").stdout
            _allow_found = False
            for _at in [
                "ALLOW", "Allow", "Allow all", "ALLOW ALL",
                "While using the app", "WHILE USING THE APP",
            ]:
                for _ap in [
                    rf'text="{re.escape(_at)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_at)}"',
                    rf'content-desc="{re.escape(_at)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(_at)}"',
                ]:
                    _am = re.findall(_ap, xml_allow)
                    if _am:
                        _x1, _y1, _x2, _y2 = map(int, _am[0])
                        _cx, _cy = (_x1+_x2)//2, (_y1+_y2)//2
                        adb(device, f"shell input tap {_cx} {_cy}")
                        print(f"  ✅ '{_at}' cliqué ({_cx},{_cy}) [check {_allow_check+1}/2]")
                        _allow_found = True
                        _popup_found = True
                        time.sleep(1.5)
                        break
                if _allow_found:
                    break
            if not _allow_found:
                break  # Pas d'ALLOW → inutile de checker une 2ème fois

        # ── Chercher bouton OK classique ──────────────────────────────────────────
        if not _popup_found:
            for _ot in ["OK", "Ok", "ok", "Done", "DONE"]:
                for _op in [
                    rf'text="{re.escape(_ot)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_ot)}"',
                ]:
                    _om = re.findall(_op, xml_ok)
                    if _om:
                        _x1, _y1, _x2, _y2 = map(int, _om[0])
                        _cx, _cy = (_x1+_x2)//2, (_y1+_y2)//2
                        adb(device, f"shell input tap {_cx} {_cy}")
                        print(f"  ✅ '{_ot}' cliqué ({_cx},{_cy})")
                        _popup_found = True
                        time.sleep(1.5)

                        # Vérifier que la popup a bien disparu
                        adb(device, "shell uiautomator dump /sdcard/ui_ok_verify.xml")
                        time.sleep(0.4)
                        xml_verify = adb(device, "shell cat /sdcard/ui_ok_verify.xml").stdout
                        if _ot not in xml_verify:
                            print(f"  ✅ Popup disparue — validé")
                        else:
                            print(f"  ⚠️ Popup encore présente — tap haut écran...")
                            _res_p2 = adb(device, "shell wm size")
                            _mp2 = re.search(r'(\d+)x(\d+)', _res_p2.stdout)
                            if _mp2:
                                _wp2, _hp2 = int(_mp2.group(1)), int(_mp2.group(2))
                                adb(device, f"shell input tap {_wp2 // 2} {int(_hp2 * 0.08)}")
                                time.sleep(1.0)
                        break
                if _popup_found:
                    break

        if not _popup_found:
            print(f"  ℹ️ Aucune popup détectée ({_popup_round+1}/3) — sortie boucle")
            break

        print(f"  🔄 Popup traitée ({_popup_round+1}/3) — re-vérification...")

    # ── 8c. Vérification feed après popups + relance si nécessaire ───────────────
    print(f"  🔍 Vérification chargement feed après popups...")
    _feed_ok = False
    for _feed_attempt in range(3):
        adb(device, "shell uiautomator dump /sdcard/ui_feed_check.xml")
        time.sleep(0.5)
        xml_feed = adb(device, "shell cat /sdcard/ui_feed_check.xml").stdout

        _feed_keywords = [
            "Your story", "your story",
            "com.instagram.android:id/feed_tab",
            "For you", "Suggested for you",
            "com.instagram.android:id/clips_tab",
        ]
        if any(kw in xml_feed for kw in _feed_keywords):
            print(f"  ✅ Feed détecté ({_feed_attempt+1}/3)")
            handle_notifications_popup(device, safe_ui_dump(device, "/sdcard/ui_notif_popup.xml"))
            _feed_ok = True
            break

        print(f"  ⚠️ Feed non détecté ({_feed_attempt+1}/3) — relance Instagram...")
        adb(device, "shell am force-stop com.instagram.android")
        time.sleep(2)
        subprocess.run(
            f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
            f'-c android.intent.category.LAUNCHER 1',
            shell=True, capture_output=True, text=True
        )
        time.sleep(6)
        _click_allow_if_present(device)
        time.sleep(2)

        # Re-fermer les popups après relance
        for _rp in range(2):
            adb(device, "shell uiautomator dump /sdcard/ui_relaunch_popup.xml")
            time.sleep(0.5)
            xml_rp = adb(device, "shell cat /sdcard/ui_relaunch_popup.xml").stdout
            _click_allow_if_present(device)
            for _bt in ["OK", "Ok", "ALLOW", "Allow", "Decline optional cookies", "Decline"]:
                for _bp in [
                    rf'text="{re.escape(_bt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_bt)}"',
                ]:
                    _bm = re.findall(_bp, xml_rp)
                    if _bm:
                        _x1, _y1, _x2, _y2 = map(int, _bm[0])
                        adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                        print(f"  ✅ '{_bt}' cliqué après relance [{_rp+1}/2]")
                        time.sleep(1.5)
                        break

    if not _feed_ok:
        print(f"  ⚠️ Feed jamais chargé après 3 tentatives — on continue quand même")

# ── 9. Cliquer sur "Your story" pour publier ──────────────────────────────
    print(f"  🔍 Recherche bouton 'Your story' pour publier...")
    ok_clicked = False

    for tick in range(10):
        adb(device, "shell uiautomator dump /sdcard/ui_story_ok.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_story_ok.xml").stdout

        # Priorité 1 : bouton "Your story" via content-desc ou text, bas gauche
        for pattern in [
            r'content-desc="Your story"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="Your story"',
            r'text="Your story"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="Your story"',
        ]:
            matches = re.findall(pattern, xml)
            if matches:
                x1, y1, x2, y2 = map(int, matches[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                # Bouton "Your story" bas gauche = cy > 1800 et cx < 500
                if cy > 1800 and cx < 500:
                    print(f"  ✅ Bouton 'Your story' publication ({cx},{cy}) — tap")
                    adb(device, f"shell input tap {cx} {cy}")
                    ok_clicked = True
                    break
        if ok_clicked:
            break

        # Priorité 2 : resource-id connu
        for rid in [
            "com.instagram.android:id/share_button",
            "com.instagram.android:id/story_share_button",
            "com.instagram.android:id/post_capture_button",
        ]:
            for pattern in [
                rf'resource-id="{re.escape(rid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(rid)}"',
            ]:
                matches = re.findall(pattern, xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    print(f"  ✅ Bouton resource-id '{rid}' ({cx},{cy}) — tap")
                    adb(device, f"shell input tap {cx} {cy}")
                    ok_clicked = True
                    break
            if ok_clicked:
                break
        if ok_clicked:
            break

        # Priorité 3 : fallback coordonnées fixes après 3 tentatives
        # "Your story" = bounds [32,2135][468,2250] → center (250, 2192)
        if tick >= 3:
            print(f"  🎯 Fallback coordonnées fixes 'Your story' (250, 2192)")
            adb(device, "shell input tap 250 2192")
            ok_clicked = True
            break

        print(f"  ⏳ Bouton 'Your story' pas encore là ({tick+1}/10)...")
        time.sleep(1)

    if not ok_clicked:
        print(f"  ⚠️ Bouton 'Your story' non trouvé — on passe quand même")

    # ── 10. Vérification pendant 3s ───────────────────────────────────────────
    print(f"  ⏳ Vérification story publiée (3s)...")
    story_published = False
    for tick in range(3):
        time.sleep(1)
        adb(device, "shell uiautomator dump /sdcard/ui_story_confirm.xml")
        time.sleep(0.3)
        xml = adb(device, "shell cat /sdcard/ui_story_confirm.xml").stdout

        confirm_keywords = [
            "Story shared", "story shared",
            "Your story", "Seen by",
            "Add to story",  # si on est revenu sur la story
            "For you",       # si on est retourné au feed
            "Suggested for you",
        ]
        if any(kw.lower() in xml.lower() for kw in confirm_keywords):
            print(f"  ✅ Story publiée confirmée ({tick+1}s) !")
            story_published = True
            break

        print(f"  ⏳ Confirmation pas encore visible ({tick+1}/3)...")

    if not story_published:
        print(f"  ⚠️ Confirmation non détectée — on continue quand même")

    # ── 11. Arrêter le téléphone ──────────────────────────────────────────────
    # ── 11. Ajouter à la une si demandé ──────────────────────────────────────
    if add_to_highlight and ok_clicked:
        print(f"  ⭐ Ajout story à la une '{highlight_name}'...")
        try:
            add_story_to_highlight(device, str(phone_id), highlight_name)
        except Exception as _hl_e:
            print(f"  ⚠️ Erreur highlight : {_hl_e}")

    # ── 12. Arrêter le téléphone ──────────────────────────────────────────────
    print(f"  ⏹ Arrêt téléphone {phone_id}...")
    stop_phone(phone_id)
    return True


def _flow_follow_suggestions(device):
    """
    Partie commune aux deux variantes :
    Décoche les profils pré-sélectionnés → Search → Salmunoz → Lena The Plug → Back → Follow
    """
    # ── Décocher les profils pré-sélectionnés sur "Follow 5 or more people" ──
    print(f"\n  ☑️ Décochage profils pré-sélectionnés...")
    adb(device, "shell uiautomator dump /sdcard/ui_follow5.xml")
    time.sleep(0.5)
    xml_f5 = adb(device, "shell cat /sdcard/ui_follow5.xml").stdout

    # Log XML complet pour debug
    print(f"  [DEBUG XML follow5] {xml_f5}")

    # Décocher tous les éléments checked="true" ou selected="true", sans exiger "Follow 5" dans le texte
    _checked_bounds = re.findall(
        r'checked="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
        r'|bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*checked="true"',
        xml_f5
    )
    # Fallback : selected="true"
    if not _checked_bounds:
        _checked_bounds = re.findall(
            r'selected="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
            r'|bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*selected="true"',
            xml_f5
        )
    _unchecked_count = 0
    for _cb in _checked_bounds:
        _coords = [c for c in _cb if c]
        if len(_coords) == 4:
            _x1, _y1, _x2, _y2 = map(int, _coords)
            _cx, _cy = (_x1+_x2)//2, (_y1+_y2)//2
            adb(device, f"shell input tap {_cx} {_cy}")
            print(f"  ☑️ Profil décoché ({_cx},{_cy})")
            _unchecked_count += 1
            time.sleep(0.4)
    if _unchecked_count == 0:
        print(f"  ℹ️ Aucun profil pré-coché trouvé")
    else:
        print(f"  ✅ {_unchecked_count} profil(s) décoché(s)")
        time.sleep(0.5)

    # ── Search → 'Salmunoz' ──────────────────────────────────────────────────
    print(f"\n  🔍 Search : Salmunoz")
    _wait_and_tap(device, ["Search", "SEARCH"], wait_max=2, dump_file="ui_search.xml")
    time.sleep(1)
    adb(device, "shell input text 'Salmunoz'")
    print(f"  ✅ 'Salmunoz' tapé")
    time.sleep(2)
    _wait_and_tap(device, ["Salome Munoz", "salome munoz", "salmunoz"],
                  wait_max=10, dump_file="ui_result1.xml")

    # ── Effacer → 'Lenatheplug' ───────────────────────────────────────────────
    print(f"\n  🔍 Search : Lenatheplug")
    time.sleep(1)
    adb(device, "shell input keyevent KEYCODE_CTRL_A")
    time.sleep(0.2)
    for _ in range(20):
        adb(device, "shell input keyevent KEYCODE_DEL")
        time.sleep(0.03)
    time.sleep(0.3)
    adb(device, "shell input text 'Lenatheplug'")
    print(f"  ✅ 'Lenatheplug' tapé")
    time.sleep(2)
    print(f"  🔍 Tap résultat 'Lena The Plug'...")
    _wait_and_tap(device, ["Lena The Plug", "lena the plug", "lenatheplug", "Lenatheplug"],
                  wait_max=3, dump_file="ui_lena.xml")

    # ── Back (fermer clavier) ─────────────────────────────────────────────────
    print(f"\n  🔙 Back clavier")
    adb(device, "shell input keyevent KEYCODE_BACK")
    time.sleep(1.5)

    # ── Follow ────────────────────────────────────────────────────────────────
    print(f"\n  ➕ Follow")
    _wait_and_tap(device, ["Follow", "FOLLOW"], wait_max=2, dump_file="ui_follow.xml")


def _flow_variant_a(device, w, h):
    """
    Variante A (interface classique) :
    Next → All Instagram notifications → Back → Skip → Skip → Search...
    """
    # Étape 7A : Next
    print(f"\n  ➡️ Étape 7A : Next")
    _wait_and_tap(device, ["Next", "NEXT"], wait_max=2, dump_file="ui_step7.xml")

    # Étape 8A : All Instagram notifications
    print(f"\n  🔔 Étape 8A : All Instagram notifications")
    _wait_and_tap(device, [
        "All Instagram notifications",
        "All notifications",
        "Turn on notifications",
    ], wait_max=2, dump_file="ui_notif.xml")

    # Étape 9A : Back
    print(f"\n  🔙 Étape 9A : Back")
    adb(device, "shell input keyevent KEYCODE_BACK")
    time.sleep(3)

    # Étape 10A : Skip x2
    print(f"\n  ⏭️ Étape 10A : Skip (1)")
    _wait_and_tap(device, ["Skip", "SKIP"], wait_max=2, dump_file="ui_skip2a.xml")
    time.sleep(1)
    print(f"\n  ⏭️ Étape 10A : Skip (2)")
    _wait_and_tap(device, ["Skip", "SKIP"], wait_max=2, dump_file="ui_skip2b.xml")

    # Étape 11A+ : flow commun
    _flow_follow_suggestions(device)


def _flow_variant_b(device, w, h):
    """
    Variante B (nouvelle interface) :
    Skip + No → Skip → +5 people (même flow search) → Skip → Got it
    """
    _search_kw = ["Search", "SEARCH", "search", "com.instagram.android:id/search"]

    # ── Vérification anticipée : Search déjà visible avant Étape 7B ──────
    adb(device, "shell uiautomator dump /sdcard/ui_varb_precheck.xml")
    time.sleep(0.5)
    xml_precheck = adb(device, "shell cat /sdcard/ui_varb_precheck.xml").stdout
    if any(kw in xml_precheck for kw in _search_kw):
        print(f"  🔍 Search détecté dès l'entrée variant B — passage direct à _flow_follow_suggestions")
        _flow_follow_suggestions(device)
        return

    # Étape 7B : Skip ou No (suggestions) — Search prioritaire à chaque tick
    print(f"\n  ⏭️ Étape 7B : Skip / No (suggestions)")
    _7b_tapped = False
    for _7b_tick in range(6):
        adb(device, "shell uiautomator dump /sdcard/ui_step7b.xml")
        time.sleep(0.5)
        xml_7b = adb(device, "shell cat /sdcard/ui_step7b.xml").stdout
        if any(kw in xml_7b for kw in _search_kw):
            print(f"  🔍 Search détecté à Étape 7B — passage direct à _flow_follow_suggestions")
            _flow_follow_suggestions(device)
            return
        for _bt in ["Skip", "SKIP", "No", "NO"]:
            for _pat in [
                rf'text="{re.escape(_bt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_bt)}"',
            ]:
                _m = re.findall(_pat, xml_7b)
                if _m:
                    _x1, _y1, _x2, _y2 = map(int, _m[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    print(f"  ⏭️ '{_bt}' cliqué à Étape 7B")
                    _7b_tapped = True
                    break
            if _7b_tapped:
                break
        if _7b_tapped:
            time.sleep(1)
            break
        time.sleep(0.5)

    # Deuxième bouton sur le même écran si besoin (Skip + No côte à côte)
    adb(device, "shell uiautomator dump /sdcard/ui_step7b2.xml")
    time.sleep(0.4)
    xml_7b2 = adb(device, "shell cat /sdcard/ui_step7b2.xml").stdout
    if any(kw in xml_7b2 for kw in _search_kw):
        print(f"  🔍 Search détecté après Étape 7B — passage direct à _flow_follow_suggestions")
        _flow_follow_suggestions(device)
        return
    if any(kw in xml_7b2 for kw in ["Skip", "SKIP", "No", "NO"]):
        _wait_and_tap(device, ["Skip", "SKIP", "No", "NO"],
                      wait_max=5, dump_file="ui_step7b2b.xml")
        time.sleep(1)

    # Étape 8B : Skip (deuxième écran) — vérifier Search d'abord
    adb(device, "shell uiautomator dump /sdcard/ui_step8b_check.xml")
    time.sleep(0.5)
    xml_8b_check = adb(device, "shell cat /sdcard/ui_step8b_check.xml").stdout
    if any(kw in xml_8b_check for kw in _search_kw):
        print(f"  🔍 Search détecté à Étape 8B — passage direct à _flow_follow_suggestions")
        _flow_follow_suggestions(device)
        return
    print(f"\n  ⏭️ Étape 8B : Skip")
    _wait_and_tap(device, ["Skip", "SKIP"], wait_max=2, dump_file="ui_skip_b2.xml")
    time.sleep(1)

    # Étape 9B : +5 people — même flow search que variante A
    # Étape 9B : +5 people — mais d'abord vider les Skip résiduels
# Étape 9B : vidage des Skip résiduels avant Search
    print(f"\n  👥 Étape 9B : vidage des Skip résiduels avant Search...")
    for _skip_round in range(5):
        adb(device, "shell uiautomator dump /sdcard/ui_pre9b.xml")
        time.sleep(0.5)
        _xml_pre = adb(device, "shell cat /sdcard/ui_pre9b.xml").stdout

        _has_search = any(kw in _xml_pre for kw in [
            "Search", "SEARCH", "search",
            "com.instagram.android:id/search",
        ])
        
        _has_skip_or_no = False
        for _st in ["Skip", "SKIP"]:
            for _pat in [
                rf'text="{re.escape(_st)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_st)}"',
            ]:
                if re.findall(_pat, _xml_pre):
                    _has_skip_or_no = True
                    break
        for _nt in ["No, skip", "NO, SKIP"]:
            for _pat in [
                rf'text="{re.escape(_nt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nt)}"',
            ]:
                if re.findall(_pat, _xml_pre):
                    _has_skip_or_no = True
                    break

        # ── CAS 1 : Search présent ET Skip présent → on ne skip pas, on va à Search ──
        if _has_search and _has_skip_or_no:
            print(f"  ℹ️ Search ET Skip présents — on passe directement à Search")
            break

        # ── CAS 2 : Search présent sans Skip → on passe à Search ──────────────────
        if _has_search and not _has_skip_or_no:
            print(f"  ✅ Search détecté — pas de Skip supplémentaire")
            break

        # ── CAS 3 : Skip présent sans Search → on skip ────────────────────────────
        if _has_skip_or_no and not _has_search:
            _skip_found = False
            for _st in ["Skip", "SKIP"]:
                for _pat in [
                    rf'text="{re.escape(_st)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_st)}"',
                ]:
                    _m = re.findall(_pat, _xml_pre)
                    if _m:
                        _x1, _y1, _x2, _y2 = map(int, _m[0])
                        _cx, _cy = (_x1+_x2)//2, (_y1+_y2)//2
                        adb(device, f"shell input tap {_cx} {_cy}")
                        print(f"  ⏭️ Skip résiduel cliqué ({_cx},{_cy}) [{_skip_round+1}/5]")
                        _skip_found = True
                        time.sleep(1.5)
                        break
                if _skip_found:
                    break
            if not _skip_found:
                for _nt in ["No, skip", "NO, SKIP"]:
                    for _pat in [
                        rf'text="{re.escape(_nt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_nt)}"',
                    ]:
                        _m = re.findall(_pat, _xml_pre)
                        if _m:
                            _x1, _y1, _x2, _y2 = map(int, _m[0])
                            _cx, _cy = (_x1+_x2)//2, (_y1+_y2)//2
                            adb(device, f"shell input tap {_cx} {_cy}")
                            print(f"  ⏭️ 'No' résiduel cliqué ({_cx},{_cy}) [{_skip_round+1}/5]")
                            time.sleep(1.5)
                            break
            continue

        # ── CAS 4 : ni Search ni Skip → on passe à Search ─────────────────────────
        print(f"  ℹ️ Plus de Skip/No sans Search — on passe à Search")
        break

    _flow_follow_suggestions(device)



def _add_profile_picture_from_gallery(device):
    """
    Flow photo de profil depuis le bottom-sheet "Add picture" :
      1. Clic sur "Choose from Gallery"
      2. Clic sur "Allow all" / "Allow" (permission galerie)
      3. Sélection de la photo (la plus récente = celle qu'on a poussée)
      4. Validation : Next puis Done
    Retourne True si mené à terme, False sinon.
    """
    # ── 1. Clic "Choose from Gallery" ────────────────────────────────────────
    _gallery_clicked = False
    adb(device, "shell uiautomator dump /sdcard/ui_pic_sheet.xml")
    time.sleep(0.4)
    _xml_sheet = adb(device, "shell cat /sdcard/ui_pic_sheet.xml").stdout
    for _g in ["Choose from Gallery", "Choose From Gallery", "Choose from gallery"]:
        for _gp in [
            rf'text="{re.escape(_g)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_g)}"',
            rf'content-desc="{re.escape(_g)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        ]:
            _m = re.findall(_gp, _xml_sheet)
            if _m:
                _x1, _y1, _x2, _y2 = map(int, _m[0])
                adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                print(f"  🖼️ 'Choose from Gallery' cliqué")
                _gallery_clicked = True
                time.sleep(2.0)
                break
        if _gallery_clicked:
            break
    if not _gallery_clicked:
        print(f"  ⚠️ 'Choose from Gallery' introuvable")
        return False

    # ── 2. Permission galerie : Allow all / Allow ────────────────────────────
    _perm_texts = ["Allow all", "ALLOW ALL", "Allow All",
                   "While using the app", "WHILE USING THE APP",
                   "Allow", "ALLOW", "Autoriser"]
    for _ptick in range(6):
        adb(device, "shell uiautomator dump /sdcard/ui_pic_perm.xml")
        time.sleep(0.4)
        _xml_perm = adb(device, "shell cat /sdcard/ui_pic_perm.xml").stdout
        _perm_clicked = False
        for _pt in _perm_texts:
            for _pp in [
                rf'text="{re.escape(_pt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_pt)}"',
                rf'content-desc="{re.escape(_pt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            ]:
                _m = re.findall(_pp, _xml_perm)
                if _m:
                    _x1, _y1, _x2, _y2 = map(int, _m[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    print(f"  🔓 Permission '{_pt}' cliquée")
                    _perm_clicked = True
                    time.sleep(1.5)
                    break
            if _perm_clicked:
                break
        if _perm_clicked:
            break
        time.sleep(0.8)

    # ── 3. Sélection de la photo (la plus récente) ───────────────────────────
    time.sleep(2.0)
    res_size = adb(device, "shell wm size")
    _ms = re.search(r'(\d+)x(\d+)', res_size.stdout)
    _w, _h = (int(_ms.group(1)), int(_ms.group(2))) if _ms else (1080, 2340)

    _photo_selected = False
    for _gtick in range(8):
        adb(device, "shell uiautomator dump /sdcard/ui_pic_grid.xml")
        time.sleep(0.4)
        _xml_grid = adb(device, "shell cat /sdcard/ui_pic_grid.xml").stdout

        # Chercher les cellules image cliquables de la grille
        _cells = []
        for _x1, _y1, _x2, _y2 in re.findall(
                r'class="android\.widget\.ImageView"[^>]*clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                _xml_grid):
            _x1, _y1, _x2, _y2 = int(_x1), int(_y1), int(_x2), int(_y2)
            _bw, _bh = _x2-_x1, _y2-_y1
            # cellule carrée dans la moitié basse de l'écran (la grille)
            if _bw > 80 and _bh > 80 and 0.7 < (_bw/max(_bh, 1)) < 1.3 and (_y1+_y2)//2 > _h*0.30:
                _cells.append(((_x1+_x2)//2, (_y1+_y2)//2))
        _cells.sort(key=lambda c: (c[1] // 100, c[0]))  # ligne par ligne, gauche→droite

        if _cells:
            # La 1ʳᵉ cellule = photo la plus récente (celle qu'on a poussée)
            _cx, _cy = _cells[0]
            print(f"  📸 Sélection photo récente ({_cx},{_cy}) — {len(_cells)} cellules")
            adb(device, f"shell input tap {_cx} {_cy}")
            _photo_selected = True
            time.sleep(2.0)
            break
        print(f"  ⏳ Grille galerie pas encore là ({_gtick+1}/8)...")
        time.sleep(0.8)

    if not _photo_selected:
        # Fallback : coordonnées fixes (1ʳᵉ photo généralement haut-gauche de la grille)
        _cx, _cy = int(_w*0.17), int(_h*0.42)
        print(f"  🎯 Fallback sélection photo ({_cx},{_cy})")
        adb(device, f"shell input tap {_cx} {_cy}")
        time.sleep(2.0)

    # ── 4. Validation : Next puis Done ───────────────────────────────────────
    for _vlabel in [["Next", "NEXT"], ["Done", "DONE", "Confirm", "CONFIRM"]]:
        for _vtick in range(6):
            adb(device, "shell uiautomator dump /sdcard/ui_pic_confirm.xml")
            time.sleep(0.4)
            _xml_conf = adb(device, "shell cat /sdcard/ui_pic_confirm.xml").stdout
            _v_clicked = False
            for _v in _vlabel:
                for _vp in [
                    rf'text="{re.escape(_v)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_v)}"',
                    rf'content-desc="{re.escape(_v)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                ]:
                    _m = re.findall(_vp, _xml_conf)
                    if _m:
                        _x1, _y1, _x2, _y2 = map(int, _m[0])
                        adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                        print(f"  ✅ '{_v}' cliqué (validation photo)")
                        _v_clicked = True
                        time.sleep(2.0)
                        break
                if _v_clicked:
                    break
            if _v_clicked:
                break
            time.sleep(0.6)

    print(f"  ✅ Flow photo de profil terminé")
    return True



def open_instagram(device, photo_folder, city=None, lat=None, lon=None,
                phone_id=None, pwd=None, bio="", ban_on_existing_email=False):
   


    # ── ÉTAPE 0 : Après push photos, ouvrir Instagram ──────────────────────
    # ── Push photo de profil (avec vérification réelle sur le téléphone) ───
    _claimed_stock_path = None   # photo réservée dans le stock (.claiming_*)
    _claimed_orig_name  = None   # nom original de la photo réservée
    profile_photos = []

    if PROFILE_STOCK_DIR:
        # Chaque worker réserve SA propre photo → 2 comptes = 2 photos consommées
        _claimed_stock_path, _claimed_orig_name = claim_profile_photo_from_stock()
        if _claimed_stock_path:
            import uuid as _uuid
            _ext = os.path.splitext(_claimed_orig_name)[1] or ".jpg"
            _tmp_copy = os.path.join(_TMP_DIR, f"profile_photo_{int(time.time())}_{_uuid.uuid4().hex[:8]}_w{_ext}")
            try:
                import shutil as _sh
                _sh.copyfile(_claimed_stock_path, _tmp_copy)
                profile_photos = [_tmp_copy]
                print(f"  📸 Photo réservée du stock : {_claimed_orig_name}")
            except Exception as _e_copy:
                print(f"  ⚠️ Copie photo réservée échouée : {_e_copy}")
                try:
                    os.rename(_claimed_stock_path,
                              os.path.join(PROFILE_STOCK_DIR, _claimed_orig_name))
                except Exception:
                    pass
                _claimed_stock_path = None
        else:
            print(f"  ⚠️ Stock de photos de profil vide — création sans photo")
    else:
        # Compat : ancienne méthode via /tmp (photo pré-copiée par le panel)
        import glob as _glob
        profile_photos = _glob.glob(os.path.join(_TMP_DIR, "profile_photo_*"))
        profile_photos = [p for p in profile_photos
                          if not p.endswith(".src")
                          and os.path.isfile(p) and os.path.getsize(p) > 0]

    if profile_photos:
        _push_ok = False
        # Jusqu'à 3 essais avec un fichier différent à chaque fois
        for _pp_try in range(3):
            profile_photo_path = random.choice(profile_photos)
            _local_size = os.path.getsize(profile_photo_path)
            remote_profile = f"/sdcard/DCIM/profile_photo/{os.path.basename(profile_photo_path)}"
            adb(device, "shell mkdir -p /sdcard/DCIM/profile_photo")
            push_res = subprocess.run(
                [ADB_PATH, "-s", device, "push", profile_photo_path, remote_profile],
                capture_output=True, text=True
            )
            if push_res.returncode != 0:
                print(f"  ⚠️ Erreur push photo profil (essai {_pp_try+1}/3) : {push_res.stderr.strip()[:60]}")
                time.sleep(1)
                continue
            # ── Vérification réelle : le fichier existe-t-il sur le téléphone ? ──
            _ls = adb(device, f"shell ls -l {remote_profile}").stdout.strip()
            _remote_size_m = re.search(rf'\s(\d+)\s.*{re.escape(os.path.basename(remote_profile))}', _ls)
            _remote_size = int(_remote_size_m.group(1)) if _remote_size_m else 0
            if remote_profile.split("/")[-1] in _ls and _remote_size > 0:
                # Déclencher le scan média pour qu'Instagram voie la photo
                adb(device, "shell am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE "
                            f"-d file://{remote_profile}")
                print(f"  ✅ Photo de profil vérifiée sur le téléphone : {remote_profile} "
                      f"({_remote_size} octets, local {_local_size})")
                _push_ok = True
                # ── Photo poussée avec succès → la retirer définitivement du stock ──
                if _claimed_stock_path:
                    # Nouvelle méthode : supprimer la photo réservée (déjà sortie du stock)
                    try:
                        os.remove(_claimed_stock_path)
                        print(f"  🗑️ Photo retirée du stock : {_claimed_orig_name}")
                    except Exception as _e_del:
                        print(f"  ⚠️ Impossible de supprimer la photo réservée : {_e_del}")
                    _claimed_stock_path = None  # ne pas restaurer ensuite
                    try:
                        os.remove(profile_photo_path)  # nettoyer la copie /tmp
                    except Exception:
                        pass
                else:
                    # Compat : ancienne méthode via fichier compagnon ".src"
                    _src_sidecar = profile_photo_path + ".src"
                    try:
                        if os.path.isfile(_src_sidecar):
                            with open(_src_sidecar, "r", encoding="utf-8") as _sf:
                                _stock_path = _sf.read().strip()
                            if _stock_path and os.path.isfile(_stock_path):
                                os.remove(_stock_path)
                                print(f"  🗑️ Photo retirée du stock : {os.path.basename(_stock_path)}")
                            os.remove(_src_sidecar)
                    except Exception as _e_del:
                        print(f"  ⚠️ Impossible de retirer la photo du stock : {_e_del}")
                break
            else:
                print(f"  ⚠️ Photo absente/vide sur le téléphone après push "
                      f"(essai {_pp_try+1}/3) — ls='{_ls[:80]}'")
                time.sleep(1)
        if not _push_ok:
            print(f"  ❌ Photo de profil NON transférée après 3 essais — création sans photo")
            # Échec → remettre la photo réservée dans le stock (ne pas la perdre)
            if _claimed_stock_path:
                try:
                    os.rename(_claimed_stock_path,
                              os.path.join(PROFILE_STOCK_DIR, _claimed_orig_name))
                    print(f"  ↩️ Photo réservée remise dans le stock : {_claimed_orig_name}")
                except Exception:
                    pass
    else:
        if not PROFILE_STOCK_DIR:
            print(f"  ⚠️ Aucune photo de profil valide trouvée dans /tmp/ "
                  f"(vide ou supprimée) — vérifie l'upload dans le panel")

    # ── ÉTAPE 0 : Après push photos, ouvrir Instagram ──────────────────────
    if wait_next("Étape 0 : Ouvrir Instagram après envoi photos"):
        _insta_result = open_instagram_after_media(device, phone_id=phone_id, wait_sec=3)
        if isinstance(_insta_result, tuple):
            insta_ok, _new_device = _insta_result
            if _new_device:
                device = _new_device
        else:
            insta_ok = _insta_result
        if insta_ok:
            print(f"  ✅ Instagram prêt — on passe à la création de compte")
        else:
            print(f"  ⚠️ Instagram non détecté — vérifiez l'installation")
    else:
        print("  ⏭️ Étape passée")


    if wait_next("Étape 1 : Get Started / Create new account"):
        ok = insta_step_get_started(device)
        if not ok:
            print(f"  ❌ Abandon — bouton d'accueil non trouvé")
            return "no_start_button"
    else:
        print("  ⏭️ Étape passée")

    # ── Étapes 2-5 : Vérification selon le mode de création ─────────────────
    global _pre_fetched_email, _pre_fetched_mail_id
    _creation_email  = None   # email SMSBower (mode email)
    _creation_mail_id = None  # mailId SMSBower (mode email)

    if CREATION_MODE == "email":
        # ── MODE EMAIL ─────────────────────────────────────────────────────
        # Utiliser l'email pré-récupéré avant la création GeeLark si disponible
        if _pre_fetched_email:
            _creation_email   = _pre_fetched_email
            _creation_mail_id = _pre_fetched_mail_id
            _pre_fetched_email  = None
            _pre_fetched_mail_id = None
            pool_log(f"✅ Gmail pré-récupéré utilisé : {_creation_email}")
        else:
            pool_log(f"📧 Mode email — récupération Gmail via SMSBower...")
            _mail_start = time.time()
            while time.time() - _mail_start < 120:
                _creation_email, _creation_mail_id = get_smsbower_email()
                if _creation_email:
                    break
                pool_log(f"⏳ Pas d'email dispo — retry dans 1s...")
                time.sleep(1)
            if not _creation_email:
                pool_log(f"❌ Impossible d'obtenir un email après 2 min — abandon")
                return "no_email"
            pool_log(f"✅ Gmail obtenu : {_creation_email}")

        if wait_next("Étape 2 : Basculer vers email + saisir l'adresse"):
            # Sur l'écran 'What's your mobile number', taper le lien 'Use email address'
            switched = insta_step_switch_to_email(device)
            if not switched:
                cancel_smsbower_email(_creation_mail_id)
                return "email_switch_failed"

            ok = insta_step_enter_email(device, _creation_email)
            if not ok:
                cancel_smsbower_email(_creation_mail_id)
                return "email_field_not_found"

        if wait_next("Étape 3 : Cliquer Next après email"):
            _next_ok = False
            for _next_try in range(5):
                if insta_step_next(device):
                    _next_ok = True
                    break
                print(f"  ⏳ 'Next' non trouvé — tentative {_next_try+1}/5, attente 2s...")
                time.sleep(2)
            if not _next_ok:
                print(f"  ❌ 'Next' introuvable après 5 tentatives")
            time.sleep(3)

        if wait_next("Étape 4 : Vérifier ban après email"):
            adb(device, "shell uiautomator dump /sdcard/ui_after_next_email.xml")
            time.sleep(0.5)
            xml_after = adb(device, "shell cat /sdcard/ui_after_next_email.xml").stdout
            if any(kw in xml_after.lower() for kw in ["we restrict certain activity", "protect our community"]):
                print(f"  🚫 Email banni ou restreint — annulation...")
                cancel_smsbower_email(_creation_mail_id)
                try:
                    delete_phone_geelark(phone_id)
                except Exception:
                    pass
                return "phone_banned_restrict"

        if wait_next("Étape 5 : Saisir le code de confirmation email"):
            email_code_ok, _final_mail_id = insta_step_email_confirmation_code(
                device, _creation_mail_id,
                get_new_email_fn=_get_email_pool_or_api,
            )
            if email_code_ok:
                confirm_smsbower_email(_final_mail_id or _creation_mail_id)
                if _final_mail_id and _final_mail_id != _creation_mail_id:
                    _creation_email_used = True  # mail intermédiaire déjà géré
                pool_log(f"✅ Code email confirmé — solde débité")
                _creation_mail_id = _final_mail_id or _creation_mail_id
            else:
                cancel_smsbower_email(_creation_mail_id)
                pool_log(f"⚠️ Code email non reçu — activation annulée")
                return "email_code_failed"

    else:
        # ── MODE TÉLÉPHONE (défaut) ────────────────────────────────────────
        global _pre_fetched_number
        phone_result = None
        if _pre_fetched_number:
            phone_result = _pre_fetched_number
            _pre_fetched_number = None
            pool_log(f"✅ Numéro pré-récupéré utilisé : {phone_result[1]} ({phone_result[2]})")
        else:
            pool_log(f"📱 Récupération numéro téléphone...")
            _num_start = time.time()
            while time.time() - _num_start < 300:
                phone_result = get_hero_number()
                if phone_result:
                    break
                pool_log(f"⏳ Pas de numéro dispo — retry dans 5s...")
                time.sleep(5)
            if not phone_result:
                pool_log(f"❌ Impossible d'obtenir un numéro après 5 min — abandon")
                return "no_phone_number"
        activation_id, raw_number, provider = phone_result
        phone_number = format_number(raw_number)
        if not phone_number:
            cancel_bower_number(activation_id)
            return "invalid_phone_number"
        pool_log(f"✅ Numéro formaté : {phone_number}")

        if wait_next("Étape 2 : Saisir le numéro de téléphone"):
            ok = insta_step_enter_phone_number(device, phone_number)
            if not ok:
                cancel_bower_number(activation_id)
                return "phone_field_not_found"

        if wait_next("Étape 3 : Cliquer Next après numéro"):
            _next_ok = False
            for _next_try in range(5):
                if insta_step_next(device):
                    _next_ok = True
                    break
                print(f"  ⏳ 'Next' non trouvé — tentative {_next_try+1}/5, attente 2s...")
                time.sleep(2)
            if not _next_ok:
                print(f"  ❌ 'Next' introuvable après 5 tentatives")
            time.sleep(3)

        if wait_next("Étape 4 : Vérifier écran après Next numéro"):
            adb(device, "shell uiautomator dump /sdcard/ui_after_next_phone.xml")
            time.sleep(0.5)
            xml_after = adb(device, "shell cat /sdcard/ui_after_next_phone.xml").stdout
            restrict_keywords = [
                "we restrict certain activity",
                "protect our community",
            ]
            if any(kw in xml_after.lower() for kw in restrict_keywords):
                print(f"  🚫 Numéro banni — annulation...")
                cancel_bower_number(activation_id)
                try:
                    delete_phone_geelark(phone_id)
                except Exception:
                    pass
                return "phone_banned_restrict"

        if wait_next("Étape 5 : Saisir le code SMS"):
            def _retry_phone_entry():
                _phone_screen_kw = [
                    "Phone number", "phone number", "Enter your phone",
                    "numéro de téléphone", "Mobile number", "Enter phone",
                ]
                print(f"  🔙 Retry: retour à l'écran numéro...")
                for _b in range(4):
                    adb(device, "shell input keyevent KEYCODE_BACK")
                    time.sleep(1.5)
                    adb(device, "shell uiautomator dump /sdcard/ui_retry_back.xml")
                    time.sleep(0.3)
                    _xml_b = adb(device, "shell cat /sdcard/ui_retry_back.xml").stdout
                    if any(kw in _xml_b for kw in _phone_screen_kw):
                        print(f"  ✅ Écran numéro détecté — re-saisie du numéro...")
                        insta_step_enter_phone_number(device, phone_number)
                        time.sleep(1)
                        insta_step_next(device)
                        time.sleep(2)
                        return
                print(f"  ⚠️ Retry: impossible de revenir à l'écran numéro")

            _screen_result = wait_for_enter_code_screen(device, timeout=60, retry_at=35, retry_callback=_retry_phone_entry)
            if _screen_result == "issues":
                cancel_bower_number(activation_id)
                try: delete_phone_geelark(phone_id)
                except: pass
                return "shadowban"
            if _screen_result == "captcha":
                cancel_bower_number(activation_id)
                try: delete_phone_geelark(phone_id)
                except: pass
                return "shadowban"
            if _screen_result == "ok_button":
                cancel_bower_number(activation_id)
                return "phone_banned_restrict"
            if _screen_result != "ok":
                print(f"  ❌ Écran code SMS jamais apparu — abandon")
                cancel_bower_number(activation_id)
                return "sms_code_failed"

            _sms_success = False
            for _sms_attempt in range(3):
                code = get_hero_sms(activation_id, provider=provider, number=raw_number)
                if code:
                    adb(device, "shell uiautomator dump /sdcard/ui_sms_code.xml")
                    time.sleep(0.5)
                    xml_code = adb(device, "shell cat /sdcard/ui_sms_code.xml").stdout
                    edits = re.findall(
                        r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_code)
                    if edits:
                        x1, y1, x2, y2 = map(int, edits[0])
                        adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                        time.sleep(0.5)
                    type_number_keycode(device, code)
                    time.sleep(0.5)
                    insta_step_next(device)
                    _sms_success = True
                    break

                print(f"  ⏳ SMS timeout tentative {_sms_attempt+1}/3...")
                cancel_bower_number(activation_id)
                time.sleep(2)

                if _sms_attempt >= 2:
                    print(f"  ❌ 3 SMS timeouts consécutifs — abandon")
                    break

                print(f"  🔙 Retour écran numéro de téléphone...")
                _phone_screen_keywords = [
                    "Phone number", "phone number", "Enter your phone",
                    "numéro de téléphone", "Mobile number", "Enter phone",
                ]
                _page_error_kw = ["Page isn't available", "Page isn", "isn't available", "Try reloading this page", "Refresh"]
                _on_phone_screen = False
                for _back_try in range(5):
                    adb(device, "shell input keyevent KEYCODE_BACK")
                    time.sleep(2)
                    adb(device, "shell uiautomator dump /sdcard/ui_back_check.xml")
                    time.sleep(0.5)
                    xml_back = adb(device, "shell cat /sdcard/ui_back_check.xml").stdout
                    if any(kw in xml_back for kw in _page_error_kw):
                        print(f"  🚫 'Page isn't available' détecté — suppression profil GeeLark")
                        try:
                            delete_phone_geelark(phone_id)
                        except Exception:
                            pass
                        return "page_unavailable"
                    if any(kw in xml_back for kw in _phone_screen_keywords):
                        print(f"  ✅ Écran numéro détecté après {_back_try+1} back(s)")
                        _on_phone_screen = True
                        break
                    print(f"  ⏳ Pas encore sur l'écran numéro ({_back_try+1}/5)...")

                if not _on_phone_screen:
                    print(f"  🔄 Back échoué — relance Instagram complète...")
                    adb(device, "shell am force-stop com.instagram.android")
                    time.sleep(2)
                    subprocess.run(
                        f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
                        f'-c android.intent.category.LAUNCHER 1',
                        shell=True, capture_output=True, text=True
                    )
                    print(f"  ⏳ Attente interface Instagram après relance (max 30s)...")
                    _launch_keywords = [
                        "Get started", "Get Started", "Create new account", "Create New Account",
                        "Log in", "Sign in", "Mobile number", "Phone number",
                    ]
                    xml_relaunch = ""
                    for _rs_tick in range(30):
                        adb(device, "shell uiautomator dump /sdcard/ui_relaunch_wait.xml")
                        time.sleep(0.5)
                        xml_relaunch = adb(device, "shell cat /sdcard/ui_relaunch_wait.xml").stdout
                        if any(kw in xml_relaunch for kw in _launch_keywords):
                            print(f"  ✅ Interface détectée après relance ({_rs_tick+1}s)")
                            break
                        print(f"  ⏳ Chargement ({_rs_tick+1}/30)...")
                        time.sleep(0.8)
                    if any(kw in xml_relaunch for kw in [
                        "Get started", "Get Started", "Create new account", "Create New Account",
                    ]):
                        print(f"  📱 Clic Get Started après relance...")
                        insta_step_get_started(device)
                        time.sleep(2)
                    adb(device, "shell uiautomator dump /sdcard/ui_after_relaunch.xml")
                    time.sleep(0.5)
                    xml_after_relaunch = adb(device, "shell cat /sdcard/ui_after_relaunch.xml").stdout
                    if any(kw in xml_after_relaunch for kw in _phone_screen_keywords):
                        print(f"  ✅ Écran numéro retrouvé après relance Instagram")
                        _on_phone_screen = True
                    else:
                        print(f"  ❌ Toujours pas sur l'écran numéro après relance — abandon")
                        return "sms_code_failed"

                _new_result = None
                _num_start2 = time.time()
                while time.time() - _num_start2 < 300:
                    _new_result = get_hero_number()
                    if _new_result:
                        break
                    pool_log(f"⏳ Pas de numéro dispo (retry) — retry dans 5s...")
                    time.sleep(5)

                if not _new_result:
                    pool_log(f"❌ Pas de nouveau numéro après 5 min — abandon")
                    return "sms_code_failed"

                activation_id, raw_number, provider = _new_result
                phone_number = format_number(raw_number)
                if not phone_number:
                    cancel_bower_number(activation_id)
                    return "sms_code_failed"

                adb(device, "shell uiautomator dump /sdcard/ui_phone_retry.xml")
                time.sleep(0.5)
                xml_retry = adb(device, "shell cat /sdcard/ui_phone_retry.xml").stdout
                edits_retry = re.findall(
                    r'class="android\.widget\.EditText"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_retry)
                if edits_retry:
                    x1, y1, x2, y2 = map(int, edits_retry[0])
                    adb(device, f"shell input tap {(x1+x2)//2} {(y1+y2)//2}")
                    time.sleep(0.5)
                adb(device, "shell input keyevent KEYCODE_CTRL_A")
                time.sleep(0.2)
                adb(device, "shell input keyevent KEYCODE_DEL")
                time.sleep(0.2)
                type_number_keycode(device, phone_number)
                time.sleep(0.5)
                insta_step_next(device)
                time.sleep(3)

            if not _sms_success:
                cancel_bower_number(activation_id)
                return "sms_code_failed"



    if wait_next("Étapes 6-8 : Détection automatique des écrans (password, birthday, name...)"):
        _handled = set()
        _name_flow_done = False

        for _loop in range(30):
            xml_cur = safe_ui_dump(device, "/sdcard/ui_autodetect.xml")

            # ── Priorité 0 : Page d'erreur "Page isn't available" → Refresh ──
            if handle_refresh_page(device, xml_cur):
                print(f"  🔄 [Dispatcher] Page d'erreur gérée (round {_loop+1})")
                continue

            # ── Priorité 1 : Ban / restriction ──────────────────────────
            if any(kw in xml_cur.lower() for kw in ["we restrict certain activity", "protect our community"]):
                print("  🚫 [Dispatcher] Ban détecté — abandon")
                try:
                    delete_phone_geelark(phone_id)
                except Exception:
                    pass
                return "phone_banned_restrict"

            # ── Priorité 2 : Confirm you're human ───────────────────────
            if any(kw in xml_cur.lower() for kw in [
                "confirm you're human", "confirm you're human",
                "community standards on account integrity",
                "you won't be able to use your account",
            ]):
                print("  🚫 [Dispatcher] Confirm human — compte inutilisable")
                try:
                    delete_phone_geelark(phone_id)
                except Exception:
                    pass
                return "human_verification_required"

            # ── Priorité 3 : Password ────────────────────────────────────
            if any(kw in xml_cur for kw in [
                "Create password", "create password",
                "Create a password", "create a password",
            ]) and "password" not in _handled:
                print("  🔑 [Dispatcher] Écran Password détecté")
                insta_step_create_password(device, password="Alexis06")
                _handled.add("password")
                time.sleep(2)
                continue

            # ── Priorité 4 : Birthday ────────────────────────────────────
            _on_birthday = any(kw in xml_cur.lower() for kw in [
                "date of birth", "birthday", "what's your birthday",
                "numberpicker", "android.widget.numberpicker",
            ])
            if _on_birthday and "birthday" not in _handled:
                print("  🎂 [Dispatcher] Écran Birthday détecté")
                insta_step_birthday(device)
                _handled.add("birthday")
                time.sleep(2)
                continue
            # Si birthday déjà géré mais écran encore présent → re-tap bas-centre
            if _on_birthday and "birthday" in _handled:
                print("  🎂 [Dispatcher] Birthday bloqué — re-tap bas-centre...")
                _bday_w = int(re.search(r'(\d+)x\d+', adb(device, "shell wm size").stdout).group(1)) if re.search(r'(\d+)x\d+', adb(device, "shell wm size").stdout) else 1080
                _bday_h = int(re.search(r'\d+x(\d+)', adb(device, "shell wm size").stdout).group(1)) if re.search(r'\d+x(\d+)', adb(device, "shell wm size").stdout) else 2340
                adb(device, f"shell input tap {_bday_w//2} {int(_bday_h*0.88)}")
                time.sleep(2)
                continue

            # ── Priorité 5 : I agree (standalone) ───────────────────────
            if any(kw in xml_cur for kw in ["I agree", "I Agree", "AGREE"]) and "i_agree" not in _handled:
                print("  ✅ [Dispatcher] Écran I Agree détecté")
                _wait_and_tap(device, ["I agree", "I Agree", "AGREE"], wait_max=3, dump_file="ui_iagree_auto.xml")
                _handled.add("i_agree")
                time.sleep(1)
                continue

            # ── Priorité 6 : Email (si Instagram le demande mid-flow) ──────
            if any(kw in xml_cur.lower() for kw in [
                "email address", "enter your email", "add your email",
            ]) and "email" not in _handled:
                print("  📧 [Dispatcher] Écran Email détecté")
                # En mode email on a déjà l'adresse, sinon on en récupère une nouvelle
                _disp_email = _creation_email
                _disp_mail_id = _creation_mail_id
                if not _disp_email:
                    _disp_email, _disp_mail_id = get_smsbower_email()
                if _disp_email:
                    insta_step_enter_email(device, _disp_email)
                    time.sleep(1)
                    insta_step_next(device)
                    time.sleep(2)
                    # Attendre et entrer le code de confirmation
                    if _disp_mail_id:
                        _disp_code_ok, _disp_final_id = insta_step_email_confirmation_code(
                            device, _disp_mail_id, get_new_email_fn=_get_email_pool_or_api)
                        if _disp_code_ok:
                            confirm_smsbower_email(_disp_final_id or _disp_mail_id)
                        else:
                            cancel_smsbower_email(_disp_mail_id)
                else:
                    print("  ⚠️ [Dispatcher] Impossible d'obtenir un email — écran ignoré")
                _handled.add("email")
                time.sleep(2)
                continue

            # ── Priorité 7 : Name/Username → name_flow ──────────────────
            if any(kw in xml_cur for kw in [
                "Full name", "What's your name",
                "Edit how you'll appear", "Edit how you", "Username",
            ]):
                print("  👤 [Dispatcher] Écran Name/Username → name_flow")
                ok = insta_step_name_and_flow(device, phone_id=phone_id)
                if ok == "incomplete_no_gotit":
                    # Profil déjà supprimé par insta_step_name_and_flow
                    return "incomplete_no_gotit"
                if ok is not True:
                    return "name_flow_failed"
                _name_flow_done = True
                break

            # ── Écran non reconnu ────────────────────────────────────────
            print(f"  🔍 [Dispatcher] Écran non reconnu ({_loop+1}/30) — attente 2s...")
            time.sleep(2)

        if not _name_flow_done:
            print("  ⚠️ [Dispatcher] 30 loops sans name_flow — tentative directe...")
            _direct = insta_step_name_and_flow(device, phone_id=phone_id)
            if _direct == "incomplete_no_gotit":
                return "incomplete_no_gotit"  # profil déjà supprimé
            if _direct is not True:
                return "name_flow_failed"
    else:
        print("  ⏭️ Étapes 6-8 passées")

    # ── Vérification finale : "Confirm you're human" ──────────────────────
    print(f"  🔍 Vérification finale — popup 'Confirm you're human'...")
    time.sleep(2.0)
    adb(device, "shell uiautomator dump /sdcard/ui_confirm_human.xml")
    time.sleep(0.5)
    xml_human = adb(device, "shell cat /sdcard/ui_confirm_human.xml").stdout
    human_keywords = [
        "confirm you're human",
        "confirm you\u2019re human",
        "you won't be able to use your account",
        "community standards on account integrity",
        "account is not visible to people",
    ]
    if any(kw in xml_human.lower() for kw in human_keywords):
        print(f"  🚫 'Confirm you're human' détecté — compte inutilisable, suppression...")
        try:
            delete_phone_geelark(phone_id)
            print(f"  ✅ Profil supprimé : {phone_id}")
        except Exception as e:
            print(f"  ⚠️ Erreur suppression : {e}")
        return "human_verification_required"
    print(f"  ✅ Pas de popup humain — compte OK")
















    
#  SECTION SWIPE
# ═══════════════════════════════════════════════════════════

def change_phone_proxy(phone_id: str, proxy_host: str, proxy_port: str,
                        proxy_user: str, proxy_pass: str,
                        proxy_type: str = "socks5") -> bool:
    """
    Change le proxy d'un cloud phone via /open/v1/phone/detail/update.
    typeId : 1=socks5 | 2=http | 3=https
    ⚠️ Ne pas appeler pendant le démarrage du téléphone.
    """
    TYPE_MAP = {"socks5": 1, "http": 2, "https": 3}
    type_id = TYPE_MAP.get(proxy_type.lower(), 1)
    print(f"  🔄 Changement proxy → {proxy_host}:{proxy_port} (typeId={type_id})...")
    try:
        result = geelark_request("POST", "/open/v1/phone/detail/update", {
            "id": str(phone_id),
            "proxyConfig": {
                "typeId":   type_id,
                "server":   proxy_host,
                "port":     int(proxy_port),
                "username": proxy_user,
                "password": proxy_pass,
            }
        })
    except Exception as e:
        print(f"  ❌ Erreur phone/detail/update : {e}")
        return False
    if result.get("code") == 0:
        print(f"  ✅ Proxy changé avec succès !")
        return True
    else:
        print(f"  ❌ phone/detail/update échoué : code={result.get('code')} msg={result.get('msg')}")
        return False


def _get_instagram_buttons(device: str):
    """
    Trouve les boutons NOPE et LIKE via dump XML — 100% fiable quelle que soit la résolution.
    Fallback sur calcul proportionnel si XML échoue.
    """
    try:
        adb(device, "shell uiautomator dump /sdcard/ui_btns.xml")
        time.sleep(0.4)
        result = adb(device, "shell cat /sdcard/ui_btns.xml")
        xml = result.stdout

        # Chercher les boutons par resource-id instagram
        nope_ids = ["com.instagram:id/nope_button", "com.instagram:id/dislike_button"]
        like_ids = ["com.instagram:id/like_button", "com.instagram:id/like"]

        btn_nope, btn_like = None, None

        for rid in nope_ids:
            found = re.findall(rf'resource-id="{re.escape(rid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not found:
                found = re.findall(rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(rid)}"', xml)
            if found:
                x1,y1,x2,y2 = map(int,found[0])
                btn_nope = ((x1+x2)//2, (y1+y2)//2)
                print(f"  ✅ Bouton NOPE trouvé via XML : {btn_nope}")
                break

        for rid in like_ids:
            found = re.findall(rf'resource-id="{re.escape(rid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not found:
                found = re.findall(rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(rid)}"', xml)
            if found:
                x1,y1,x2,y2 = map(int,found[0])
                btn_like = ((x1+x2)//2, (y1+y2)//2)
                print(f"  ✅ Bouton LIKE trouvé via XML : {btn_like}")
                break

        if btn_nope and btn_like:
            return btn_nope, btn_like

        # Fallback : chercher par content-desc
        for text in ["Nope", "NOPE", "Dislike"]:
            found = re.findall(rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not found:
                found = re.findall(rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"', xml)
            if found:
                x1,y1,x2,y2 = map(int,found[0])
                btn_nope = ((x1+x2)//2, (y1+y2)//2)
                print(f"  ✅ Bouton NOPE (content-desc) : {btn_nope}")
                break

        for text in ["Like", "LIKE"]:
            found = re.findall(rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not found:
                found = re.findall(rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"', xml)
            if found:
                x1,y1,x2,y2 = map(int,found[0])
                btn_like = ((x1+x2)//2, (y1+y2)//2)
                print(f"  ✅ Bouton LIKE (content-desc) : {btn_like}")
                break

        if btn_nope and btn_like:
            return btn_nope, btn_like

    except Exception as e:
        print(f"  ⚠️ _get_instagram_buttons XML erreur : {e}")

    # Fallback proportionnel sur résolution réelle
    try:
        result = adb(device, "shell wm size")
        match = re.search(r'(\d+)x(\d+)', result.stdout)
        if match:
            w, h = int(match.group(1)), int(match.group(2))
            btn_nope = (int(w*0.255), int(h*0.735))
            btn_like = (int(w*0.745), int(h*0.735))
            print(f"  📐 Fallback proportionnel {w}x{h} → NOPE{btn_nope} LIKE{btn_like}")
            return btn_nope, btn_like
    except:
        pass

    # Dernier fallback absolu pour 1080x2640
    print(f"  ⚠️ Fallback absolu 1080x2640")
    return (275, 1940), (804, 1940)


def _tap_by_text(device: str, xml: str, text: str) -> bool:
    """Trouve un élément par text ou content-desc et tape dessus."""
    patterns = [
        rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
        rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"',
    ]
    for pattern in patterns:
        found = re.findall(pattern, xml)
        if found:
            x1, y1, x2, y2 = map(int, found[0])
            cx, cy = (x1+x2)//2, (y1+y2)//2
            print(f"  ✅ '{text}' trouvé ({cx},{cy}) → tap")
            adb(device, f"shell input tap {cx} {cy}")
            return True
    return False


def _tap_close_button(device: str, xml: str) -> bool:
    """
    Cherche un bouton de fermeture (croix) dans le XML.
    1. Par resource-id connu
    2. Par content-desc
    3. Par petit élément cliquable dans les 20% hauts de l'écran
    """
    close_ids = [
        "com.instagram:id/close_button",
        "com.instagram:id/dismiss_button",
        "com.instagram:id/cancel_button",
        "com.instagram:id/x_button",
        "com.instagram:id/back_button",
    ]
    for rid in close_ids:
        found = re.findall(
            rf'resource-id="{re.escape(rid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        if not found:
            found = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(rid)}"', xml)
        if found:
            x1, y1, x2, y2 = map(int, found[0])
            cx, cy = (x1+x2)//2, (y1+y2)//2
            print(f"  ✅ Close resource-id ({cx},{cy})")
            adb(device, f"shell input tap {cx} {cy}")
            time.sleep(0.8)
            return True

    for text in ["Close", "Dismiss", "close", "dismiss", "×", "✕"]:
        found = re.findall(
            rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        if not found:
            found = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"', xml)
        if found:
            x1, y1, x2, y2 = map(int, found[0])
            cx, cy = (x1+x2)//2, (y1+y2)//2
            print(f"  ✅ Close content-desc '{text}' ({cx},{cy})")
            adb(device, f"shell input tap {cx} {cy}")
            time.sleep(0.8)
            return True

    try:
        res = adb(device, "shell wm size")
        m = re.search(r'(\d+)x(\d+)', res.stdout)
        h_limit = int(m.group(2)) * 0.20 if m else 400
    except:
        h_limit = 400

    clickables = re.findall(
        r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
    if not clickables:
        clickables = re.findall(
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"', xml)
    for coords in clickables:
        x1, y1, x2, y2 = map(int, coords)
        cy = (y1+y2)//2
        cx = (x1+x2)//2
        btn_w = x2-x1
        btn_h = y2-y1
        if cy < h_limit and btn_w < 200 and btn_h < 200:
            print(f"  🎯 Close zone haute ({cx},{cy})")
            adb(device, f"shell input tap {cx} {cy}")
            time.sleep(0.8)
            return True

    return False


def _get_instagram_buttons(device: str):
    """
    Trouve les boutons NOPE et LIKE via dump XML.
    Fallback proportionnel si XML échoue.
    """
    try:
        adb(device, "shell uiautomator dump /sdcard/ui_btns.xml")
        time.sleep(0.4)
        result = adb(device, "shell cat /sdcard/ui_btns.xml")
        xml = result.stdout

        btn_nope, btn_like = None, None

        for rid in ["com.instagram:id/nope_button", "com.instagram:id/dislike_button"]:
            found = re.findall(
                rf'resource-id="{re.escape(rid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not found:
                found = re.findall(
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(rid)}"', xml)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                btn_nope = ((x1+x2)//2, (y1+y2)//2)
                print(f"  ✅ NOPE resource-id : {btn_nope}")
                break

        for rid in ["com.instagram:id/like_button", "com.instagram:id/like"]:
            found = re.findall(
                rf'resource-id="{re.escape(rid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not found:
                found = re.findall(
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(rid)}"', xml)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                btn_like = ((x1+x2)//2, (y1+y2)//2)
                print(f"  ✅ LIKE resource-id : {btn_like}")
                break

        if btn_nope and btn_like:
            return btn_nope, btn_like

        for text in ["Nope", "NOPE", "Dislike"]:
            found = re.findall(
                rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not found:
                found = re.findall(
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"', xml)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                btn_nope = ((x1+x2)//2, (y1+y2)//2)
                print(f"  ✅ NOPE content-desc : {btn_nope}")
                break

        for text in ["Like", "LIKE"]:
            found = re.findall(
                rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not found:
                found = re.findall(
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"', xml)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                btn_like = ((x1+x2)//2, (y1+y2)//2)
                print(f"  ✅ LIKE content-desc : {btn_like}")
                break

        if btn_nope and btn_like:
            return btn_nope, btn_like

        print(f"  ⚠️ Boutons non trouvés via XML, fallback proportionnel...")

    except Exception as e:
        print(f"  ⚠️ _get_instagram_buttons erreur : {e}")

    try:
        result = adb(device, "shell wm size")
        match = re.search(r'(\d+)x(\d+)', result.stdout)
        if match:
            w, h = int(match.group(1)), int(match.group(2))
            btn_nope = (int(w*0.255), int(h*0.735))
            btn_like = (int(w*0.745), int(h*0.735))
            print(f"  📐 Fallback proportionnel {w}x{h} → NOPE{btn_nope} LIKE{btn_like}")
            return btn_nope, btn_like
    except:
        pass

    print(f"  ⚠️ Fallback absolu 1080x2640")
    return (275, 1940), (804, 1940)


# ─────────────────────────────────────────
#  SWIPES
# ─────────────────────────────────────────

def _send_match_message(device: str, xml: str = None) -> bool:
    """
    Détecte le champ 'Say something nice', écrit un message aléatoire et clique Send.
    Retourne True si le message a été envoyé.
    """
    import unicodedata

    def strip_accents(s):
        return ''.join(
            c for c in unicodedata.normalize('NFD', s)
            if unicodedata.category(c) != 'Mn'
        )

    # Choisir un message aléatoire parmi OPENING_MESSAGES
    message_raw = random.choice(OPENING_MESSAGES)
    # Nettoyer les accents et caractères spéciaux pour ADB input text
    message_clean = strip_accents(message_raw)
    message_escaped = (message_clean
        .replace("'", "")
        .replace('"', '')
        .replace('`', '')
        .replace('&', 'and')
        .replace('<', '')
        .replace('>', '')
        .replace(' ', '%s')
    )

    print(f"  💬 Message choisi : {message_raw[:60]}...")

    # Dump XML si pas fourni
    if xml is None:
        adb(device, "shell uiautomator dump /sdcard/ui_match.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_match.xml").stdout

    # ── Trouver le champ "Say something nice" ─────────────────────────────────
    field_found = False
    for hint in ["Say something nice", "say something nice"]:
        # Par hint
        matches = re.findall(
            rf'hint="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        if not matches:
            matches = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(hint)}"', xml)
        if not matches:
            # Par text
            matches = re.findall(
                rf'text="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        if not matches:
            matches = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(hint)}"', xml)
        if matches:
            x1, y1, x2, y2 = map(int, matches[0])
            cx, cy = (x1+x2)//2, (y1+y2)//2
            print(f"  ✅ Champ 'Say something nice' trouvé ({cx},{cy}) → tap")
            adb(device, f"shell input tap {cx} {cy}")
            field_found = True
            time.sleep(0.8)
            break

    # Par resource-id connu instagram
    if not field_found:
        for rid in [
            "com.instagram:id/match_message_edit_text",
            "com.instagram:id/message_edit_text",
            "com.instagram:id/editText",
            "com.instagram:id/send_message_input",
        ]:
            found = re.findall(
                rf'resource-id="{re.escape(rid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not found:
                found = re.findall(
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(rid)}"', xml)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                print(f"  ✅ Champ resource-id '{rid}' ({cx},{cy})")
                adb(device, f"shell input tap {cx} {cy}")
                field_found = True
                time.sleep(0.8)
                break

    # Fallback coordonnées fixes (basé sur la screenshot : champ en bas vers y≈900)
    if not field_found:
        print(f"  ⚠️ Champ non trouvé via XML — fallback coordonnées fixes (330, 900)")
        try:
            res = adb(device, "shell wm size")
            m = re.search(r'(\d+)x(\d+)', res.stdout)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                cx_fb = w // 2
                cy_fb = int(h * 0.86)  # La barre de message est à ~86% de la hauteur
                adb(device, f"shell input tap {cx_fb} {cy_fb}")
                print(f"  🎯 Tap fallback ({cx_fb},{cy_fb})")
            else:
                adb(device, "shell input tap 330 900")
        except:
            adb(device, "shell input tap 330 900")
        time.sleep(0.8)

    # ── Écrire le message ──────────────────────────────────────────────────────
    adb(device, f"shell input text \"{message_escaped}\"")
    print(f"  ✅ Message saisi")
    time.sleep(0.8)

    # ── Trouver et cliquer Send ────────────────────────────────────────────────
    # Re-dump XML après saisie
    adb(device, "shell uiautomator dump /sdcard/ui_match2.xml")
    time.sleep(0.4)
    xml2 = adb(device, "shell cat /sdcard/ui_match2.xml").stdout

    send_found = False
    for text in ["Send", "SEND", "Envoyer"]:
        matches = re.findall(
            rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml2)
        if not matches:
            matches = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"', xml2)
        if not matches:
            matches = re.findall(
                rf'content-desc="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml2)
        if not matches:
            matches = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(text)}"', xml2)
        if matches:
            x1, y1, x2, y2 = map(int, matches[0])
            cx, cy = (x1+x2)//2, (y1+y2)//2
            print(f"  ✅ Bouton 'Send' trouvé ({cx},{cy}) → tap")
            adb(device, f"shell input tap {cx} {cy}")
            send_found = True
            time.sleep(1.0)
            break

    # Fallback Send : coordonnée fixe à droite de la barre de message
    if not send_found:
        print(f"  ⚠️ 'Send' non trouvé — fallback coordonnées")
        try:
            res = adb(device, "shell wm size")
            m = re.search(r'(\d+)x(\d+)', res.stdout)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                cx_send = int(w * 0.88)
                cy_send = int(h * 0.86)
                adb(device, f"shell input tap {cx_send} {cy_send}")
                print(f"  🎯 Tap Send fallback ({cx_send},{cy_send})")
                send_found = True
        except:
            adb(device, "shell input tap 600 900")
            send_found = True
        time.sleep(1.0)

    if send_found:
        print(f"  ✅ Message envoyé au match !")
        time.sleep(1.5)
        return True

    return False

OPENING_MESSAGES = [
    "Coucou cavaaa ? On peut décaler tele gram, cherche Miahyvina",
    "Coucou cava toii ? On peut décaler tele gram, cherche Miahyvina",
    "Coucou cava toiii ?? On peut décaler tele gram, cherches Miahyvina",
    "Heyyy cava toiii ? On peut décaler tele gram, cherche Miahyvina",
    "Coucouuu cava toiii ? On peut décaler tele gram, cherche Miahyvina",
    "Coucouuuu cava toiiiii ? On peut décaler tele gram, cherche Miahyvina",
    "Coucouuu cava toiiii ? On peut décaler tele gram, cherche Miahyvina",
        "Coucooou cava toi ? On peut décaler tele gram, cherche Miahyvina",

    "Coucooou cavaaaa toi ? On peut décaler tele gram, cherche Miahyvina",

    "Coucooou cavaaaa toi ? On peut décaler tele gram, cherche Miahyvina",

    "Coucoou cavaa toi ? On peut décaler tele gram, cherche Miahyvina",

    "Coucooou cavaaa toi ? On peut décaler tele gram, cherche Miahyvina",

    "Coucooou cavaa toi ? On peut décaler tele gram, cherche Miahyvina",







]
# Utilisation :
# import random
# message = random.choice(OPENING_MESSAGES)

def _close_popups(device: str) -> bool:
    try:
        time.sleep(1.0)
        adb(device, "shell uiautomator dump /sdcard/ui_popup.xml")
        time.sleep(0.4)
        result = adb(device, "shell cat /sdcard/ui_popup.xml")
        xml = result.stdout

        if not xml or len(xml) < 50:
            return False

        # ── PRIORITÉ ABSOLUE 0 : Captcha "Let's verify you're a human" ────────
        human_captcha_keywords = [
            "verify you're a human",
            "verify you\u2019re a human",
            "solve this puzzle",
            "start puzzle",
            "please solve",
            "know you are a real person",
            "funcaptcha",
            "arkose",
            "let's verify",
            "let\u2019s verify",
        ]
        if any(kw in xml.lower() for kw in human_captcha_keywords):
            print(f"  🤖 CAPTCHA HUMAIN détecté dans _close_popups — signal bannissement")
            return "captcha_human"
        

        # ── PRIORITÉ 0.5 : Popup "Our members' safety is a key priority" ──────
        safety_keywords = [
            "our members' safety",
            "our members&#39; safety",
            "safe message filters",
            "date safely",
            "don't send message",
            "dont send message",
        ]
        if any(kw in xml.lower() for kw in safety_keywords):
            print(f"  🛡️ Popup 'Our members safety' détectée — clic I AGREE...")
            # Chercher "I AGREE" par text
            agree_found = False
            for text in ["I AGREE", "I Agree", "i agree", "AGREE"]:
                matches = re.findall(
                    rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
                if not matches:
                    matches = re.findall(
                        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"', xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    print(f"  ✅ 'I AGREE' trouvé ({cx},{cy}) → tap")
                    adb(device, f"shell input tap {cx} {cy}")
                    agree_found = True
                    time.sleep(1.5)
                    break
            if not agree_found:
                # Fallback proportionnel : le bouton I AGREE est à ~65% de la hauteur
                try:
                    res = adb(device, "shell wm size")
                    m = re.search(r'(\d+)x(\d+)', res.stdout)
                    if m:
                        w, h = int(m.group(1)), int(m.group(2))
                        adb(device, f"shell input tap {w//2} {int(h*0.65)}")
                        print(f"  🎯 I AGREE fallback ({w//2},{int(h*0.65)})")
                except:
                    adb(device, "shell input tap 300 737")
                time.sleep(1.5)
            return True



        # ── PRIORITÉ 0.4 : Popup Privacy Preference Center ────────────────
        privacy_keywords = [
            "privacy preference center",
            "tcf purposes",
            "list of tcf partners",
        ]
        if any(kw in xml.lower() for kw in privacy_keywords):
            print(f"  🛡️ Popup Privacy/TCF détectée — clic 'I accept'...")
            for text in ["I accept", "I Accept", "ACCEPT"]:
                matches = re.findall(
                    rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
                if not matches:
                    matches = re.findall(
                        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"', xml)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ Privacy 'I accept' cliqué ({cx},{cy})")
                    time.sleep(2)
                    return True
            # Fallback coordonnées
            adb(device, "shell input tap 310 1013")
            time.sleep(2)
            return True
        # ── PRIORITÉ 0 ABSOLUE : Popup "Get more Likes" / upsell bottom sheet ──




        if any(kw in xml.lower() for kw in [
            "instagram u",
            "see more students",
            "students at your school",
        ]):
            print(f"  🎓 Popup instagram U — flow Let's Do It → bts → Continue...")

            # ── Étape 1 : cliquer "Let's Do It" ──────────────────────────────
            _ldi_clicked = False
            for _ldi_text in ["Let's Do It", "Let\u2019s Do It", "LET'S DO IT"]:
                _ldi_m = re.findall(
                    rf'text="{re.escape(_ldi_text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
                if not _ldi_m:
                    _ldi_m = re.findall(
                        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_ldi_text)}"', xml)
                if _ldi_m:
                    _x1,_y1,_x2,_y2 = map(int,_ldi_m[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    print(f"  ✅ 'Let's Do It' cliqué ({(_x1+_x2)//2},{(_y1+_y2)//2})")
                    _ldi_clicked = True
                    break
            if not _ldi_clicked:
                _res_u = adb(device, "shell wm size")
                _mu = re.search(r'(\d+)x(\d+)', _res_u.stdout)
                if _mu:
                    _wu,_hu = int(_mu.group(1)),int(_mu.group(2))
                    adb(device, f"shell input tap {_wu//2} {int(_hu*0.68)}")
                    print(f"  🎯 Let's Do It fallback")
            time.sleep(2.0)

            # ── Étape 2 : attendre "School Name" puis cliquer dessus ──────────
            _school_found = False
            for _tick in range(10):
                adb(device, "shell uiautomator dump /sdcard/ui_instagramu.xml")
                time.sleep(0.8)
                _xml_tu = adb(device, "shell cat /sdcard/ui_instagramu.xml").stdout
                if any(kw in _xml_tu for kw in ["School Name", "school name", "ADD SCHOOL"]):
                    print(f"  ✅ Champ School Name détecté ({_tick+1}s)")
                    # Cliquer sur le champ School Name
                    for _sp in [
                        r'hint="School Name"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                        r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="School Name"',
                        r'text="School Name"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                        r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="School Name"',
                    ]:
                        _sm2 = re.findall(_sp, _xml_tu)
                        if _sm2:
                            _x1,_y1,_x2,_y2 = map(int,_sm2[0])
                            adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                            print(f"  ✅ Champ cliqué ({(_x1+_x2)//2},{(_y1+_y2)//2})")
                            _school_found = True
                            break
                    if not _school_found:
                        _res_s = adb(device, "shell wm size")
                        _ms = re.search(r'(\d+)x(\d+)', _res_s.stdout)
                        if _ms:
                            _ws,_hs = int(_ms.group(1)),int(_ms.group(2))
                            adb(device, f"shell input tap {_ws//2} {int(_hs*0.68)}")
                        _school_found = True
                    break
                print(f"  ⏳ School Name pas encore là ({_tick+1}/10)...")
            time.sleep(1.0)

            # ── Étape 3 : attendre popup "My school is" puis taper 'bts' ──────
            _myschool_found = False
            for _tick in range(10):
                adb(device, "shell uiautomator dump /sdcard/ui_myschool.xml")
                time.sleep(0.8)
                _xml_ms = adb(device, "shell cat /sdcard/ui_myschool.xml").stdout
                if "My school is" in _xml_ms or "my school is" in _xml_ms.lower():
                    print(f"  ✅ Popup 'My school is' détectée ({_tick+1}s)")
                    _myschool_found = True
                    break
                print(f"  ⏳ 'My school is' pas encore là ({_tick+1}/10)...")
            
            if _myschool_found:
                # Taper 'bts' dans le champ actif
                adb(device, "shell input text 'bts'")
                print(f"  ✅ 'bts' tapé")
                time.sleep(1.5)

                # ── Étape 4 : cliquer sur "Add bts" ──────────────────────────
                _add_clicked = False
                for _tick in range(8):
                    adb(device, "shell uiautomator dump /sdcard/ui_addbts.xml")
                    time.sleep(0.8)
                    _xml_ab = adb(device, "shell cat /sdcard/ui_addbts.xml").stdout
                    for _abt in ["Add bts", "add bts", "ADD bts"]:
                        _abm = re.findall(
                            rf'text="{re.escape(_abt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', _xml_ab)
                        if not _abm:
                            _abm = re.findall(
                                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_abt)}"', _xml_ab)
                        if _abm:
                            _x1,_y1,_x2,_y2 = map(int,_abm[0])
                            adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                            print(f"  ✅ 'Add bts' cliqué ({(_x1+_x2)//2},{(_y1+_y2)//2})")
                            _add_clicked = True
                            break
                    if _add_clicked:
                        break
                    print(f"  ⏳ 'Add bts' pas encore là ({_tick+1}/8)...")

                if not _add_clicked:
                    # Fallback : premier résultat de la liste
                    _res_ab = adb(device, "shell wm size")
                    _mab = re.search(r'(\d+)x(\d+)', _res_ab.stdout)
                    if _mab:
                        _wab,_hab = int(_mab.group(1)),int(_mab.group(2))
                        adb(device, f"shell input tap {_wab//2} {int(_hab*0.28)}")
                        print(f"  🎯 Add bts fallback")
                time.sleep(2.0)

            # ── Étape 5 : attendre "Continue" et cliquer ─────────────────────
            _continue_clicked = False
            for _tick in range(15):
                adb(device, "shell uiautomator dump /sdcard/ui_instagramu_cont.xml")
                time.sleep(0.8)
                _xml_cont = adb(device, "shell cat /sdcard/ui_instagramu_cont.xml").stdout
                for _ct in ["Continue", "CONTINUE", "Continuer"]:
                    _cm = re.findall(
                        rf'text="{re.escape(_ct)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', _xml_cont)
                    if not _cm:
                        _cm = re.findall(
                            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_ct)}"', _xml_cont)
                    if _cm:
                        _x1,_y1,_x2,_y2 = map(int,_cm[0])
                        adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                        print(f"  ✅ 'Continue' cliqué ({(_x1+_x2)//2},{(_y1+_y2)//2})")
                        _continue_clicked = True
                        break
                if _continue_clicked:
                    break
                print(f"  ⏳ 'Continue' pas encore là ({_tick+1}/15)...")

            if not _continue_clicked:
                _res_c = adb(device, "shell wm size")
                _mc = re.search(r'(\d+)x(\d+)', _res_c.stdout)
                if _mc:
                    _wc,_hc = int(_mc.group(1)),int(_mc.group(2))
                    adb(device, f"shell input tap {_wc//2} {int(_hc*0.65)}")
                    print(f"  🎯 Continue fallback")

            time.sleep(1.0)

            # ── Étape 6 : reclicker Let's Do It (s'il est encore visible) ──
            _ldi2_clicked = False
            for _tick2 in range(5):
                adb(device, "shell uiautomator dump /sdcard/ui_instagramu_ldi2.xml")
                time.sleep(0.8)
                _xml_ldi2 = adb(device, "shell cat /sdcard/ui_instagramu_ldi2.xml").stdout
                for _ldi2 in ["Let's Do It", "Let\u2019s Do It", "LET'S DO IT"]:
                    _lm2 = re.findall(
                        rf'text="{re.escape(_ldi2)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', _xml_ldi2)
                    if not _lm2:
                        _lm2 = re.findall(
                            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_ldi2)}"', _xml_ldi2)
                    if _lm2:
                        _x1,_y1,_x2,_y2 = map(int,_lm2[0])
                        adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                        print(f"  ✅ 'Let's Do It' (2ème) cliqué ({(_x1+_x2)//2},{(_y1+_y2)//2})")
                        _ldi2_clicked = True
                        break
                if _ldi2_clicked:
                    break
                # Si pas trouvé, la popup est peut-être déjà fermée → sortir
                if "instagram u" not in _xml_ldi2.lower() and "see more students" not in _xml_ldi2.lower():
                    print(f"  ✅ Popup instagram U disparue — pas besoin de reclicker")
                    break
                print(f"  ⏳ 'Let's Do It' pas trouvé ({_tick2+1}/5)...")

            if _ldi2_clicked:
                time.sleep(1.5)
                # ── Étape 7 : cliquer Continue une 2ème fois ─────────────────
                _cont2_clicked = False
                for _tick3 in range(10):
                    adb(device, "shell uiautomator dump /sdcard/ui_instagramu_cont2.xml")
                    time.sleep(0.8)
                    _xml_cont2 = adb(device, "shell cat /sdcard/ui_instagramu_cont2.xml").stdout
                    for _ct2 in ["Continue", "CONTINUE", "Continuer"]:
                        _cm2 = re.findall(
                            rf'text="{re.escape(_ct2)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', _xml_cont2)
                        if not _cm2:
                            _cm2 = re.findall(
                                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_ct2)}"', _xml_cont2)
                        if _cm2:
                            _x1,_y1,_x2,_y2 = map(int,_cm2[0])
                            adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                            print(f"  ✅ 'Continue' (2ème) cliqué ({(_x1+_x2)//2},{(_y1+_y2)//2})")
                            _cont2_clicked = True
                            break
                    if _cont2_clicked:
                        break
                    print(f"  ⏳ 'Continue' (2ème) pas encore là ({_tick3+1}/10)...")

                if not _cont2_clicked:
                    _res_c2 = adb(device, "shell wm size")
                    _mc2 = re.search(r'(\d+)x(\d+)', _res_c2.stdout)
                    if _mc2:
                        _wc2,_hc2 = int(_mc2.group(1)),int(_mc2.group(2))
                        adb(device, f"shell input tap {_wc2//2} {int(_hc2*0.65)}")
                        print(f"  🎯 Continue (2ème) fallback")
                time.sleep(1.5)

            return True


        upsell_keywords = [
            "get more likes",
            "we found",
            "photo you can swap",
            "review",
            "close sheet",
        ]
        if any(kw in xml.lower() for kw in upsell_keywords):
            print(f"  📊 Popup 'Get more Likes' — recherche bouton X...")
            closed = False

            # Méthode 1 : chercher le X via XML (content-desc ou text)
            x_patterns = [
                r'content-desc="[Cc]lose"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="[Cc]lose"',
                r'content-desc="[Dd]ismiss"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="[Dd]ismiss"',
                r'text="×"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'text="✕"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            ]
            for xp in x_patterns:
                found_x = re.findall(xp, xml)
                if found_x:
                    x1, y1, x2, y2 = map(int, found_x[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    print(f"  ✅ Bouton X trouvé via XML ({cx},{cy}) → tap")
                    adb(device, f"shell input tap {cx} {cy}")
                    closed = True
                    time.sleep(0.8)
                    break

            if not closed:
                # Méthode 2 : tap au même endroit que la fermeture popup "Choose a phone number"
                try:
                    adb(device, f"shell input tap 476 748")
                    print(f"  🎯 Tap fixe fermeture popup (476, 748)")
                    closed = True
                    time.sleep(0.8)
                except Exception as e:
                    print(f"  ⚠️ Erreur : {e}")
                    adb(device, "shell input keyevent KEYCODE_BACK")
                    time.sleep(0.8)

            return True

        # ── PRIORITÉ 0 : Popup instagram Gold / upsell ───────────────────────────
        instagram_gold_keywords = [
            "instagram gold", "instagram platinum",
            "can't wait to see who", "who else likes you",
            "save time (and energy)", "boost your profile",
            "get instagram gold", "get gold",
        ]
        if any(kw in xml.lower() for kw in instagram_gold_keywords):
            print(f"  💛 Popup instagram Gold détectée — recherche du X...")
            closed = _tap_close_button(device, xml)
            if not closed:
                print(f"  🔙 X non trouvé — BACK...")
                adb(device, "shell input keyevent KEYCODE_BACK")
            time.sleep(1.0)
            adb(device, "shell uiautomator dump /sdcard/ui_popup.xml")
            time.sleep(0.3)
            xml2 = adb(device, "shell cat /sdcard/ui_popup.xml").stdout
            if any(kw in xml2.lower() for kw in instagram_gold_keywords):
                print(f"  🔙 Popup encore présente — BACK forcé...")
                adb(device, "shell input keyevent KEYCODE_BACK")
                time.sleep(1.0)
            return True

        # ── PRIORITÉ 0b : Popup "Say more about yourself" ─────────────────────
        photo_prompt_keywords = [
            "say more about yourself",
            "add photo prompt",
            "photo prompts to highlight",
        ]
        if any(kw in xml.lower() for kw in photo_prompt_keywords):
            print(f"  📸 Popup 'Say more about yourself' — X...")
            closed = _tap_close_button(device, xml)
            if not closed:
                adb(device, "shell input keyevent KEYCODE_BACK")
            time.sleep(1.0)
            return True

        # ── Variables communes ─────────────────────────────────────────────────
        has_maybe_later = any(kw in xml for kw in [
            "Maybe later", "Maybe Later", "MAYBE LATER"
        ])
        has_invite = "Invite friends" in xml or "invite friends" in xml.lower()
        has_double_date_context = any(kw in xml.lower() for kw in [
            "you're in", "you\u2019re in",
            "friends make everything better",
            "just one like to match",
            "double match",
            "it's a double",
            "try out double date",
            "double date!",
            "pair up with",
            "first-date stress",
        ])
        has_swipe_buttons = any(kw in xml for kw in [
            "com.instagram:id/nope_button", "com.instagram:id/like_button",
            "com.instagram:id/dislike_button",
        ])

        # ── PRIORITÉ 1 : Popup Double Date ────────────────────────────────────
        if has_maybe_later or has_double_date_context or (has_invite and not has_swipe_buttons):
            print(f"  📅 Popup Double Date / 'You're in' détectée — dump XML précis...")

            adb(device, "shell uiautomator dump /sdcard/ui_dd.xml")
            time.sleep(0.5)
            xml_dd = adb(device, "shell cat /sdcard/ui_dd.xml").stdout

            x_patterns = [
                r'content-desc="[^"]*[Cc]lose[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="[^"]*[Cc]lose[^"]*"',
                r'content-desc="[^"]*[Dd]ismiss[^"]*"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="[^"]*[Dd]ismiss[^"]*"',
                r'text="×"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'text="✕"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            ]
            for xp in x_patterns:
                found_x = re.findall(xp, xml_dd)
                if found_x:
                    x1, y1, x2, y2 = map(int, found_x[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    print(f"  ✅ Croix close trouvée ({cx},{cy})")
                    adb(device, f"shell input tap {cx} {cy}")
                    time.sleep(1.5)
                    return True

            if any(kw in xml_dd.lower() for kw in [
                "you're in", "you\u2019re in", "try out double date",
                "pair up with", "first-date stress"
            ]):
                maybe_patterns = [
                    r'text="Maybe later"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="Maybe later"',
                    r'text="Maybe Later"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="Maybe Later"',
                    r'content-desc="Maybe later"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="Maybe later"',
                ]
                for pattern in maybe_patterns:
                    found = re.findall(pattern, xml_dd)
                    if found:
                        x1, y1, x2, y2 = map(int, found[0])
                        cx, cy = (x1+x2)//2, (y1+y2)//2
                        print(f"  🎯 'Maybe later' trouvé ({cx},{cy})")
                        adb(device, f"shell input tap {cx} {cy}")
                        time.sleep(1.5)
                        return True

                invite_patterns = [
                    r'text="Invite friends"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="Invite friends"',
                ]
                for pattern in invite_patterns:
                    found = re.findall(pattern, xml_dd)
                    if found:
                        x1, y1, x2, y2 = map(int, found[0])
                        cy_below = int(y2) + (int(y2) - int(y1))
                        cx = (int(x1)+int(x2))//2
                        print(f"  🎯 Tap sous 'Invite friends' ({cx},{cy_below})")
                        adb(device, f"shell input tap {cx} {cy_below}")
                        time.sleep(1.5)
                        return True

                try:
                    res = adb(device, "shell wm size")
                    m = re.search(r'(\d+)x(\d+)', res.stdout)
                    if m:
                        w, h = int(m.group(1)), int(m.group(2))
                        h_limit = int(h * 0.18)
                        clickables = re.findall(
                            r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_dd)
                        if not clickables:
                            clickables = re.findall(
                                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"', xml_dd)
                        for coords in clickables:
                            x1, y1, x2, y2 = map(int, coords)
                            cx, cy = (x1+x2)//2, (y1+y2)//2
                            btn_w, btn_h = x2-x1, y2-y1
                            if cy < h_limit and btn_w < 200 and btn_h < 200:
                                print(f"  🎯 Petit bouton zone haute ({cx},{cy}) — tap (X)")
                                adb(device, f"shell input tap {cx} {cy}")
                                time.sleep(1.5)
                                return True
                except Exception as e:
                    print(f"  ⚠️ Erreur recherche X zone haute : {e}")

                print(f"  🔙 'You're in' — rien trouvé → BACK")
                adb(device, "shell input keyevent KEYCODE_BACK")
                time.sleep(1.5)
                return True

            maybe_patterns = [
                r'text="Maybe later"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="Maybe later"',
                r'text="Maybe Later"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="Maybe Later"',
                r'content-desc="Maybe later"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="Maybe later"',
            ]
            found_maybe = None
            for pattern in maybe_patterns:
                found = re.findall(pattern, xml_dd)
                if found:
                    found_maybe = found[0]
                    break

            if found_maybe:
                x1, y1, x2, y2 = map(int, found_maybe)
                cx, cy = (x1+x2)//2, (y1+y2)//2
                print(f"  🎯 'Maybe later' ({cx},{cy})")
                adb(device, f"shell input tap {cx} {cy}")
                time.sleep(1.5)
                return True

            invite_patterns = [
                r'text="Invite friends"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="Invite friends"',
            ]
            found_invite = None
            for pattern in invite_patterns:
                found = re.findall(pattern, xml_dd)
                if found:
                    found_invite = found[0]
                    break

            if found_invite:
                x1, y1, x2, y2 = map(int, found_invite)
                btn_h = y2 - y1
                cy_below = y2 + btn_h
                cx = (x1 + x2) // 2
                print(f"  🎯 Tap sous 'Invite friends' ({cx},{cy_below})")
                adb(device, f"shell input tap {cx} {cy_below}")
                time.sleep(1.5)
                adb(device, "shell uiautomator dump /sdcard/ui_dd.xml")
                time.sleep(0.3)
                xml_check = adb(device, "shell cat /sdcard/ui_dd.xml").stdout
                if "Invite friends" not in xml_check:
                    print(f"  ✅ Popup Double Date fermée")
                    return True
                print(f"  ⚠️ Popup encore présente — BACK")
                adb(device, "shell input keyevent KEYCODE_BACK")
                time.sleep(1.5)
                return True

            print(f"  🔙 Rien trouvé — BACK")
            adb(device, "shell input keyevent KEYCODE_BACK")
            time.sleep(1.5)
            return True

        # ── PRIORITÉ 2 : Popup MATCH ──────────────────────────────────────────
        # ── PRIORITÉ 2 : Popup MATCH ──────────────────────────────────────────
        is_match = any(kw in xml for kw in [
            "It's a Match", "C'est un Match",
            "Keep Swiping", "KEEP SWIPING",
            "Say something nice",
        ])
        if is_match:
            print(f"  💥 Popup Match détectée — fermeture rapide...")
            for text in ["Keep Swiping", "KEEP SWIPING", "Not Now", "Continue", "Continuer"]:
                if _tap_by_text(device, xml, text):
                    time.sleep(0.5)
                    return True
            if _tap_close_button(device, xml):
                time.sleep(0.5)
                return True
            adb(device, "shell input keyevent KEYCODE_BACK")
            time.sleep(0.5)
            return True

        # ── PRIORITÉ 3 : Popup SUPER LIKE ─────────────────────────────────────
        is_superlike = any(kw in xml for kw in [
            "might hit it off", "Send Super Like", "No Thanks", "No thanks",
        ])
        if is_superlike:
            print(f"  ⭐ Popup Super Like — No Thanks...")
            for text in ["No Thanks", "No thanks", "NO THANKS"]:
                if _tap_by_text(device, xml, text):
                    time.sleep(1.0)
                    return True
            if _tap_close_button(device, xml):
                return True
            adb(device, "shell input keyevent KEYCODE_BACK")
            time.sleep(1.0)
            return True

        # ── PRIORITÉ 4 : Popup générique bloquante ────────────────────────────
        has_blocker = any(kw in xml for kw in [
            "No Thanks", "Not Now",
        ])
        if has_blocker and not has_swipe_buttons:
            print(f"  ⚠️ Popup bloquante — BACK")
            adb(device, "shell input keyevent KEYCODE_BACK")
            time.sleep(1.0)
            return True

        return False

    except Exception as e:
        print(f"  ⚠️ _close_popups erreur : {e}")
        return False

def _ensure_swipe_screen(device: str, max_attempts: int = 3) -> bool:
    """Ferme toutes les popups avant de swiper."""
    for attempt in range(max_attempts):
        closed = _close_popups(device)
        if not closed:
            return True
        print(f"  🔄 Popup fermée ({attempt+1}/{max_attempts}), re-vérification...")
        time.sleep(0.8)
    _close_popups(device)
    return True


def _get_swipe_zone(device: str):
    """
    Calcule la zone de swipe au centre de la photo.
    Y = 40% de la hauteur = milieu de la carte photo, loin des boutons.
    """
    try:
        result = adb(device, "shell wm size")
        match = re.search(r'(\d+)x(\d+)', result.stdout)
        if match:
            w, h = int(match.group(1)), int(match.group(2))
            cy      = int(h * 0.40)
            x_left  = int(w * 0.15)
            x_right = int(w * 0.85)
            print(f"  📐 Zone swipe {w}x{h} → y={cy} x_like={x_right} x_nope={x_left}")
            return cy, x_left, x_right
    except Exception as e:
        print(f"  ⚠️ _get_swipe_zone erreur : {e}")
    # Fallback 1080x2640
    return 1056, 162, 918


def _dismiss_notifications_bar(device: str) -> bool:
    try:
        adb(device, "shell service call statusbar 2")
        time.sleep(0.4)
        adb(device, "shell cmd notification dismiss-notifications com.instagram 0")
        time.sleep(0.3)
    except Exception as e:
        print(f"  ⚠️ _dismiss_notifications_bar : {e}")
    return True


def _find_filter_button(device: str):
    try:
        adb(device, "shell uiautomator dump /sdcard/ui_filter.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_filter.xml").stdout

        filter_ids = [
            "com.instagram:id/filter_button",
            "com.instagram:id/discovery_filter_button",
            "com.instagram:id/preference_button",
            "com.instagram:id/settings_icon",
            "com.instagram:id/explore_filter",
            "com.instagram:id/swipe_filter",
            "com.instagram:id/toolbar_filter",
            "com.instagram:id/top_picks_toolbar_settings",
        ]
        for rid in filter_ids:
            found = re.findall(
                rf'resource-id="{re.escape(rid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not found:
                found = re.findall(
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(rid)}"', xml)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                print(f"  ✅ Filtre resource-id '{rid}' → ({cx},{cy})")
                return cx, cy

        for desc in ["Filter", "Filters", "Settings", "Preferences",
                     "Discovery preferences", "Adjust your preferences",
                     "Filtres", "Paramètres"]:
            found = re.findall(
                rf'content-desc="{re.escape(desc)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not found:
                found = re.findall(
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(desc)}"', xml)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                cx, cy = (x1+x2)//2, (y1+y2)//2
                print(f"  ✅ Filtre content-desc '{desc}' → ({cx},{cy})")
                return cx, cy

        res = adb(device, "shell wm size")
        m = re.search(r'(\d+)x(\d+)', res.stdout)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            h_limit = int(h * 0.12)
            x_limit = int(w * 0.22)
            clickables = re.findall(
                r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
            if not clickables:
                clickables = re.findall(
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"', xml)
            for coords in clickables:
                x1, y1, x2, y2 = map(int, coords)
                cx, cy = (x1+x2)//2, (y1+y2)//2
                btn_w, btn_h = x2-x1, y2-y1
                if cy < h_limit and cx < x_limit and 10 < btn_w < 220 and 10 < btn_h < 220:
                    print(f"  🎯 Filtre zone haut-gauche ({cx},{cy}) {btn_w}×{btn_h}")
                    return cx, cy

        print(f"  ⚠️ Bouton filtre non trouvé via XML")
        return None

    except Exception as e:
        print(f"  ⚠️ _find_filter_button erreur : {e}")
        return None
    

def _find_profile_in_nav(xml_content, w, h):
    """Retourne (x,y) du bouton Profile dans la nav bar — méthode robuste."""
    
    # Méthode 1 : resource-id de la bottom navigation
    nav_ids = [
        "com.instagram:id/profile_tab",
        "com.instagram:id/nav_profile",
        "com.instagram:id/bottom_nav_profile",
        "com.instagram:id/tab_profile",
        "com.instagram:id/profile_icon",
        "com.instagram:id/menu_profile",
    ]
    for rid in nav_ids:
        found = re.findall(
            rf'resource-id="{re.escape(rid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_content)
        if not found:
            found = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(rid)}"', xml_content)
        if found:
            x1, y1, x2, y2 = map(int, found[0])
            cx, cy = (x1+x2)//2, (y1+y2)//2
            print(f"  ✅ Profile resource-id '{rid}' ({cx},{cy})")
            return cx, cy

    # Méthode 2 : chercher tous les éléments "Profile"/"Profil"
    # et garder UNIQUEMENT celui dans le quart droit + bas de l'écran
    y_min = int(h * 0.85)
    x_min = int(w * 0.70)  # Profile = dernier onglet, forcément à droite
    
    for desc in ["Profile", "Profil"]:
        for pattern in [
            rf'content-desc="{re.escape(desc)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(desc)}"',
            rf'text="{re.escape(desc)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(desc)}"',
        ]:
            for coords in re.findall(pattern, xml_content):
                x1, y1, x2, y2 = map(int, coords)
                cy = (y1 + y2) // 2
                cx = (x1 + x2) // 2
                # DOUBLE filtre : bas de l'écran ET côté droit
                if cy >= y_min and cx >= x_min:
                    print(f"  ✅ Profile nav bar ({cx},{cy}) — bas+droite confirmé")
                    return cx, cy

    # Méthode 3 : parser la bottom navigation bar complète
    # Trouver le container de la nav bar et prendre le dernier élément cliquable
    nav_container_ids = [
        "com.instagram:id/bottom_navigation",
        "com.instagram:id/bottom_nav",
        "com.instagram:id/navigation_bar",
        "com.instagram:id/tab_bar",
        "com.instagram:id/main_tab_bar",
    ]
    for rid in nav_container_ids:
        # Extraire le contenu du container nav
        container_match = re.search(
            rf'resource-id="{re.escape(rid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml_content)
        if not container_match:
            container_match = re.search(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(rid)}"',
                xml_content)
        if container_match:
            # Prendre la zone après ce container dans le XML
            idx = container_match.start()
            nav_zone = xml_content[idx:idx+3000]
            # Tous les éléments cliquables dans cette zone
            clickables = re.findall(
                r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', nav_zone)
            if not clickables:
                clickables = re.findall(
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"', nav_zone)
            valid = []
            for coords in clickables:
                x1, y1, x2, y2 = map(int, coords)
                cy = (y1+y2)//2
                cx = (x1+x2)//2
                if cy >= int(h * 0.85):
                    valid.append((cx, cy))
            if valid:
                # Le dernier = Profile (ordre : Swipe, Explore, Likes, Chat, Profile)
                cx, cy = max(valid, key=lambda c: c[0])
                print(f"  ✅ Profile nav container dernier onglet ({cx},{cy})")
                return cx, cy

    # Méthode 4 : tous les petits boutons cliquables en bas à droite
    y_min = int(h * 0.85)
    x_min = int(w * 0.75)
    clickables = re.findall(
        r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_content)
    if not clickables:
        clickables = re.findall(
            r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"', xml_content)
    candidates = []
    for coords in clickables:
        x1, y1, x2, y2 = map(int, coords)
        cy = (y1+y2)//2
        cx = (x1+x2)//2
        btn_w, btn_h = x2-x1, y2-y1
        if cy >= y_min and cx >= x_min and btn_w < 300 and btn_h < 300:
            candidates.append((cx, cy))
    if candidates:
        cx, cy = max(candidates, key=lambda c: c[0])
        print(f"  🎯 Profile fallback bas-droite ({cx},{cy})")
        return cx, cy

    print(f"  ⚠️ Profile introuvable dans nav bar")
    return None


def _find_show_further_toggle(device: str):
    try:
        adb(device, "shell uiautomator dump /sdcard/ui_pref.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_pref.xml").stdout

        all_nodes = re.findall(r'text="([^"]*)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml)
        if not all_nodes:
            all_nodes_rev = re.findall(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="([^"]*)"', xml)
            all_nodes = [(t, x1, y1, x2, y2) for x1, y1, x2, y2, t in all_nodes_rev]
        else:
            all_nodes = [(t, x1, y1, x2, y2) for t, x1, y1, x2, y2 in all_nodes]

        for t, x1, y1, x2, y2 in all_nodes:
            if t.strip():
                print(f"  📋 Texte trouvé : '{t[:60]}'")

        # Priorité 1 : chercher par texte exact
        keywords = ["show people", "further away", "run out of profiles", "profiles to see"]
        for kw in keywords:
            for t, x1, y1, x2, y2 in all_nodes:
                if kw.lower() in t.lower():
                    cx, cy = (int(x1)+int(x2))//2, (int(y1)+int(y2))//2
                    print(f"  ✅ Match '{kw}' dans '{t[:50]}' → tap ({cx},{cy})")
                    return cx, cy

        # Priorité 2 : fallback XML brut — bounds AVANT le texte
        xml_lower = xml.lower()
        for kw in keywords:
            idx = xml_lower.find(kw)
            if idx != -1:
                nearby_before = xml[max(0, idx-400):idx+50]
                found = re.findall(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', nearby_before)
                if found:
                    x1, y1, x2, y2 = map(int, found[-1])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    if cx == 0 and cy == 0:
                        print(f"  ⚠️ Fallback XML brut '{kw}' → bounds (0,0) ignorés")
                        continue
                    print(f"  ✅ Fallback XML brut '{kw}' → tap ({cx},{cy})")
                    return cx, cy

        # Priorité 3 : chercher le switch APRES "Age Range" dans le XML
        # "Show people further away" est toujours après "Age Range" dans Settings
        age_range_idx = xml_lower.find("age range")
        if age_range_idx != -1:
            xml_after_age = xml[age_range_idx:]
            checkables_after = re.findall(
                r'checkable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_after_age)
            if not checkables_after:
                checkables_after = re.findall(
                    r'class="android\.widget\.Switch"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_after_age)
            for coords in checkables_after:
                x1, y1, x2, y2 = map(int, coords)
                cx, cy = (x1+x2)//2, (y1+y2)//2
                if cx > 10 and cy > 10:
                    print(f"  🎯 Switch après 'Age Range' → tap ({cx},{cy})")
                    return cx, cy

        print(f"  ⚠️ 'Show people further' introuvable")
        return None

    except Exception as e:
        print(f"  ⚠️ _find_show_further_toggle erreur : {e}")
        return None



def debug_dump_nav(device):
    """Dump complet de la nav bar pour trouver Profile une fois pour toutes."""
    adb(device, "shell uiautomator dump /sdcard/ui_debug.xml")
    time.sleep(1.0)
    result = adb(device, "shell cat /sdcard/ui_debug.xml")
    xml = result.stdout
    
    # Sauvegarder le XML complet dans un fichier local
    with open("debug_nav.xml", "w", encoding="utf-8") as f:
        f.write(xml)
    print(f"  📄 XML sauvegardé dans debug_nav.xml ({len(xml)} chars)")
    
    # Afficher TOUS les éléments cliquables avec leurs coordonnées
    print(f"\n  === TOUS LES ÉLÉMENTS CLIQUABLES ===")
    clickables = re.findall(
        r'<node[^>]*clickable="true"[^>]*>', xml)
    for node in clickables:
        text = re.search(r'text="([^"]*)"', node)
        desc = re.search(r'content-desc="([^"]*)"', node)
        rid  = re.search(r'resource-id="([^"]*)"', node)
        bounds = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', node)
        if bounds:
            x1,y1,x2,y2 = map(int, bounds.groups())
            cx,cy = (x1+x2)//2, (y1+y2)//2
            t  = text.group(1)  if text  else ""
            d  = desc.group(1)  if desc  else ""
            r  = rid.group(1)   if rid   else ""
            print(f"  ({cx:4d},{cy:4d}) | text='{t[:30]}' | desc='{d[:30]}' | id='{r[:40]}'")
    
    print(f"  =====================================\n")

def _quick_captcha_check(device: str) -> bool:
    """
    Vérification rapide du captcha humain instagram.
    Dump XML + check keywords. Retourne True si captcha détecté.
    Optimisé pour être le plus rapide possible (un seul dump).
    """
    try:
        adb(device, "shell uiautomator dump /sdcard/ui_cap.xml")
        time.sleep(0.3)
        result = adb(device, "shell cat /sdcard/ui_cap.xml")
        xml_low = result.stdout.lower()
        return any(kw in xml_low for kw in [
            "verify you're a human",
            "verify you\u2019re a human",
            "solve this puzzle",
            "start puzzle",
            "know you are a real person",
            "funcaptcha",
            "arkose",
            "let's verify",
            "let\u2019s verify",
        ])
    except Exception:
        return False
    

def _open_filter_and_toggle(device: str) -> bool:
    print(f"  🔧 Ouverture Profile → Settings...")
# ── Étape 1 : cliquer sur 'Profile' dans la barre du bas ──────────
    res = adb(device, "shell wm size")
    m = re.search(r'(\d+)x(\d+)', res.stdout)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2340)

    # PAS de tap parasite — chercher directement Profile
    for _attempt in range(5):
        adb(device, "shell uiautomator dump /sdcard/ui_nav.xml")
        time.sleep(0.5)
        xml_nav = adb(device, "shell cat /sdcard/ui_nav.xml").stdout

        profile_pos = None
        found = re.findall(
            r'resource-id="com\.instagram:id/action_profile"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            xml_nav)
        if not found:
            found = re.findall(
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="com\.instagram:id/action_profile"',
                xml_nav)
        if found:
            x1, y1, x2, y2 = map(int, found[0])
            profile_pos = ((x1+x2)//2, (y1+y2)//2)
            print(f"  ✅ Profile resource-id trouvé ({profile_pos[0]},{profile_pos[1]})")
        else:
            found2 = re.findall(
                r'content-desc="Profile"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_nav)
            if not found2:
                found2 = re.findall(
                    r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="Profile"', xml_nav)
            if found2:
                x1, y1, x2, y2 = map(int, found2[0])
                profile_pos = ((x1+x2)//2, (y1+y2)//2)
                print(f"  ✅ Profile content-desc trouvé ({profile_pos[0]},{profile_pos[1]})")

        if not profile_pos:
            print(f"  ⚠️ Profile non trouvé ({_attempt+1}/5) — retry...")
            time.sleep(1.0)
            continue

        adb(device, f"shell input tap {profile_pos[0]} {profile_pos[1]}")
        time.sleep(3.0)
        adb(device, "shell uiautomator dump /sdcard/ui_profile_check.xml")
        time.sleep(0.5)
        xml_check = adb(device, "shell cat /sdcard/ui_profile_check.xml").stdout
        if any(kw in xml_check.lower() for kw in [
            "edit profile", "Miahyvina", "subscriptions", "my boosts", "super likes"
        ]):
            print(f"  ✅ Page Profile confirmée ({_attempt+1}/5)")
            break
        print(f"  ⚠️ Mauvaise page ({_attempt+1}/5) — retry...")
        time.sleep(1.0)
    else:
        print(f"  ⚠️ Profile jamais confirmé — on continue quand même")

    # ── Étape 2 : cliquer sur l'écrou (Settings) ──────────────────────
    adb(device, "shell uiautomator dump /sdcard/ui_profile.xml")
    time.sleep(0.5)
    xml_profile = adb(device, "shell cat /sdcard/ui_profile.xml").stdout

    settings_pos = (int(w * 0.92), int(h * 0.10))

    settings_ids = [
        "com.instagram:id/settings_button",
        "com.instagram:id/settings_icon",
        "com.instagram:id/toolbar_settings",
        "com.instagram:id/gear_button",
        "com.instagram:id/profile_settings",
        "com.instagram:id/action_settings",
        "com.instagram:id/menu_settings",
    ]
    found_gear = False
    for rid in settings_ids:
        found = re.findall(
            rf'resource-id="{re.escape(rid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_profile)
        if not found:
            found = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(rid)}"', xml_profile)
        if found:
            x1, y1, x2, y2 = map(int, found[0])
            settings_pos = ((x1+x2)//2, (y1+y2)//2)
            print(f"  ✅ Écrou resource-id '{rid}' ({settings_pos[0]},{settings_pos[1]})")
            found_gear = True
            break

    if not found_gear:
        for desc in ["Settings", "Paramètres", "settings", "Gear", "gear"]:
            found = re.findall(
                rf'content-desc="{re.escape(desc)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_profile)
            if not found:
                found = re.findall(
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(desc)}"', xml_profile)
            if found:
                x1, y1, x2, y2 = map(int, found[0])
                settings_pos = ((x1+x2)//2, (y1+y2)//2)
                print(f"  ✅ Écrou content-desc '{desc}' ({settings_pos[0]},{settings_pos[1]})")
                found_gear = True
                break

    if not found_gear:
        h_limit = int(h * 0.15)
        x_min   = int(w * 0.70)
        clickables = re.findall(
            r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_profile)
        if not clickables:
            clickables = re.findall(
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"', xml_profile)
        small_candidates = []
        for coords in clickables:
            x1, y1, x2, y2 = map(int, coords)
            cx, cy = (x1+x2)//2, (y1+y2)//2
            btn_w, btn_h = x2-x1, y2-y1
            if cy < h_limit and cx > x_min and 10 < btn_w < 80 and 10 < btn_h < 80:
                small_candidates.append((cx, cy, btn_w, btn_h))
                print(f"  📋 Candidat écrou (petit) : ({cx},{cy}) {btn_w}×{btn_h}")
        if small_candidates:
            best = max(small_candidates, key=lambda c: c[0])
            settings_pos = (best[0], best[1])
            print(f"  🎯 Écrou petit bouton haut-droite ({settings_pos[0]},{settings_pos[1]})")
            found_gear = True
        else:
            print(f"  ⚠️ Aucun petit bouton trouvé — fallback absolu")

    if not found_gear:
        print(f"  🎯 Écrou fallback absolu ({settings_pos[0]},{settings_pos[1]})")

    adb(device, f"shell input tap {settings_pos[0]} {settings_pos[1]}")
    # Attendre que Settings s'ouvre — instagram est lent
    time.sleep(5.0)

    # ── Étape 3 : vérifier qu'on est dans Settings ────────────────────
    adb(device, "shell uiautomator dump /sdcard/ui_settings_check.xml")
    time.sleep(0.4)
    xml_settings = adb(device, "shell cat /sdcard/ui_settings_check.xml").stdout
    in_settings = any(kw in xml_settings.lower() for kw in [
        "maximum distance", "show people", "interested in",
        "age range", "global", "location", "discovery"
    ])
    if not in_settings:
        print(f"  ⚠️ Pas dans Settings — retry écrou...")
        res2 = adb(device, "shell wm size")
        m2 = re.search(r'(\d+)x(\d+)', res2.stdout)
        if m2:
            w2, h2 = int(m2.group(1)), int(m2.group(2))
            adb(device, f"shell input tap {int(w2*0.92)} {int(h2*0.10)}")
            time.sleep(4.0)

    # ── Étape 4 : scroll OBLIGATOIRE vers le bas puis recherche toggle ─
    print(f"  📜 Scroll vers le bas pour atteindre 'Show people further'...")
    # Remonter en haut d'abord
    adb(device, f"shell input swipe {w//2} {int(h*0.30)} {w//2} {int(h*0.70)} 400")
    time.sleep(0.8)
    adb(device, f"shell input swipe {w//2} {int(h*0.30)} {w//2} {int(h*0.70)} 400")
    time.sleep(0.8)

    # Scroller progressivement vers le bas jusqu'à trouver le toggle
    toggle_pos = None
    for step in range(6):
        toggle_pos = _find_show_further_toggle(device)
        if toggle_pos:
            print(f"  ✅ Toggle trouvé après {step} scroll(s)")
            break
        print(f"  📜 Scroll bas [{step+1}/6]...")
        adb(device, f"shell input swipe {w//2} {int(h*0.65)} {w//2} {int(h*0.30)} 700")
        time.sleep(1.2)

    # ── Étape 5 : cliquer sur le toggle en vérifiant qu'il est visible ─
    if toggle_pos:
        tx, ty = toggle_pos
        visible_limit = int(h * 0.88)
        if ty > visible_limit:
            print(f"  ⚠️ Toggle hors zone visible (y={ty} > {visible_limit}) — scroll supplémentaire...")
            scroll_amount = ty - int(h * 0.55)
            adb(device, f"shell input swipe {w//2} {int(h*0.55)} {w//2} {int(h*0.55) - scroll_amount} 500")
            time.sleep(1.2)
            new_pos = _find_show_further_toggle(device)
            if new_pos and new_pos[1] <= visible_limit:
                tx, ty = new_pos
                print(f"  ✅ Toggle repositionné ({tx},{ty})")
            else:
                adb(device, f"shell input swipe {w//2} {int(h*0.65)} {w//2} {int(h*0.30)} 600")
                time.sleep(1.0)
                new_pos2 = _find_show_further_toggle(device)
                if new_pos2:
                    tx, ty = new_pos2
                    print(f"  ✅ Toggle repositionné 2ème tentative ({tx},{ty})")
        adb(device, f"shell input tap {tx} {ty}")
        print(f"  ✅ Toggle cliqué ({tx},{ty})")
        time.sleep(1.5)
    else:
        print(f"  ⚠️ Toggle introuvable — on continue quand même")

    # ── Étape 6 : retour arrière ───────────────────────────────────────
    adb(device, "shell input keyevent KEYCODE_BACK")
    time.sleep(1.5)

    # Vérifier qu'on est revenu sur Profile
    adb(device, "shell uiautomator dump /sdcard/ui_back_check.xml")
    time.sleep(0.3)
    xml_back = adb(device, "shell cat /sdcard/ui_back_check.xml").stdout
    on_profile = any(kw in xml_back.lower() for kw in [
        "edit profile", "complete your profile", "super likes",
        "my boosts", "subscriptions", "Miahyvina"
    ])
    if not on_profile:
        print(f"  ⚠️ Pas sur Profile — BACK supplémentaire")
        adb(device, "shell input keyevent KEYCODE_BACK")
        time.sleep(1.0)

    # ── Étape 7 : cliquer sur 'Swipe' dans la barre du bas ───────────
    adb(device, "shell uiautomator dump /sdcard/ui_nav2.xml")
    time.sleep(0.4)
    xml_nav2 = adb(device, "shell cat /sdcard/ui_nav2.xml").stdout

    swipe_pos = None
    for desc in ["Swipe", "swipe"]:
        found = re.findall(
            rf'content-desc="{re.escape(desc)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_nav2)
        if not found:
            found = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(desc)}"', xml_nav2)
        if not found:
            found = re.findall(
                rf'text="{re.escape(desc)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_nav2)
        if not found:
            found = re.findall(
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(desc)}"', xml_nav2)
        if found:
            x1, y1, x2, y2 = map(int, found[0])
            # Filtrer : doit être dans la nav bar (bas de l'écran)
            cy = (y1+y2)//2
            if cy >= int(h * 0.88):
                swipe_pos = ((x1+x2)//2, cy)
                print(f"  ✅ 'Swipe' trouvé ({swipe_pos[0]},{swipe_pos[1]})")
                break

    if not swipe_pos:
        swipe_pos = (int(w * 0.09), int(h * 0.965))
        print(f"  🎯 Swipe fallback ({swipe_pos[0]},{swipe_pos[1]})")

    adb(device, f"shell input tap {swipe_pos[0]} {swipe_pos[1]}")
    time.sleep(2.0)

    print(f"  ✅ Cycle Profile→Settings→Toggle→Swipe terminé")
    return True


def do_force_match_swipes(device: str, swipe_count: int = 2, stop_flag: list = None, phone_id=None):
    """
    phone_id est maintenant requis pour pouvoir supprimer le profil si captcha détecté.
    """
    if stop_flag is None:
        stop_flag = [False]
    print(f"  💘 Force Match démarré — {swipe_count} cycle(s)")
 
    adb(device, "shell am force-stop com.instagram")
    time.sleep(1)
    adb(device, "shell monkey -p com.instagram -c android.intent.category.LAUNCHER 1")
    time.sleep(5)
    adb(device, "shell am force-stop com.instagram")
    time.sleep(1)
    adb(device, "shell monkey -p com.instagram -c android.intent.category.LAUNCHER 1")
    time.sleep(6)

    # ── Blocage images pour économiser le proxy ────────────────────────────
    _block_instagram_images(device)

    # ── Check réseau indisponible ──────────────────────────────────────────
    if _check_network_error(device):
        print(f"  🌐 Erreur réseau détectée — arrêt propre (pas un ban)")
        adb(device, "shell am force-stop com.instagram")
        stop_phone(phone_id)
        return {"liked": 0, "noped": 0, "errors": 0, "banned": False, "reason": "network_error"}
 
    print(f"  📧 Vérification popup email avant check...")
    handle_verify_email_popup(device)
    time.sleep(2)
 
    # ── Check captcha dès l'ouverture ─────────────────────────────────────────
    if _quick_captcha_check(device):
        print(f"  🤖 CAPTCHA HUMAIN détecté à l'ouverture — suppression profil...")
        adb(device, "shell am force-stop com.instagram")
        if phone_id:
            try:
                #delete_phone_geelark(phone_id)
                print(f"  ✅ Profil supprimé : {phone_id}")
            except Exception as e:
                print(f"  ⚠️ Erreur suppression : {e}")
        return {"liked": 0, "noped": 0, "errors": 0, "banned": True, "reason": "captcha"}
 
    adb(device, f"shell input tap 540 50")
    time.sleep(0.8)
    statut = check_instagram_account(device)
    if statut == "banned":
        print(f"  🚫 Compte BANNI avant force match — session annulée")
        screenshot_ban = take_screenshot(device)
        phone_label = str(phone_id) if phone_id else device
        caption_ban = f"🚫 <b>Compte BANNI avant force match</b>\n📱 Téléphone : {phone_label}\n❤️ Likes : 0 | 👎 Nopes : 0"
        if screenshot_ban:
            telegram_send_photo(screenshot_ban, caption_ban)
            
        else:
            telegram_send_message(caption_ban)
        adb(device, "shell am force-stop com.instagram")
        return {"liked": 0, "noped": 0, "errors": 0, "banned": True}
 
    print(f"  ✅ Compte vivant — démarrage Force Match")
 
    _ensure_swipe_screen(device)
    cy, x_left, x_right = _get_swipe_zone(device)
 
    liked = 0
    noped = 0
 
    for i in range(swipe_count):
        if stop_flag[0]:
            print(f"  ⛔ Stop demandé — arrêt force match")
            break
        print(f"\n  ── Cycle Force Match {i+1}/{swipe_count} ──")
 
        _ensure_swipe_screen(device)
 
        # ── NOPE ──────────────────────────────────────────────────────────────
        print(f"  👎 NOPE...")
        y_nope = cy + random.randint(-45, 45)
        adb(device,
            f"shell input swipe "
            f"{x_right + random.randint(-15,15)} {y_nope} "
            f"{x_left + random.randint(-15,15)} {y_nope} "
            f"{random.randint(220, 380)}")
        noped += 1
        time.sleep(random.uniform(1.0, 1.8))
 
       
        
        # ── Check captcha après LIKE (rapide) ─────────────────────────────────
        if _quick_captcha_check(device):

            print(f"  🤖 CAPTCHA HUMAIN détecté après NOPE — suppression profil...")
            adb(device, "shell am force-stop com.instagram")
            _send_ban_telegram(device, phone_id, liked, noped, reason="captcha")  # ← AJOUT
            time.sleep(3)
            if phone_id:
                try:
                    delete_phone_geelark(phone_id)
                    print(f"  ✅ Profil supprimé : {phone_id}")
                except Exception as e:
                    print(f"  ⚠️ Erreur suppression : {e}")
            return {"liked": liked, "noped": noped, "errors": 0, "banned": True, "reason": "captcha"}

        _close_popups(device)
        time.sleep(0.5)

        # ── LIKE ──────────────────────────────────────────────────────────────
        print(f"  ❤️ LIKE...")
        _ensure_swipe_screen(device)
        y_like = cy + random.randint(-45, 45)
        adb(device,
            f"shell input swipe "
            f"{x_left + random.randint(-15,15)} {y_like} "
            f"{x_right + random.randint(-15,15)} {y_like} "
            f"{random.randint(220, 380)}")
        liked += 1
        time.sleep(random.uniform(1.5, 2.5))  # laisser le temps à la popup match d'apparaître

        # ── Check captcha après LIKE (rapide) ─────────────────────────────────
        if _quick_captcha_check(device):
            print(f"  🤖 CAPTCHA HUMAIN détecté après LIKE — suppression profil...")
            adb(device, "shell am force-stop com.instagram")
            _send_ban_telegram(device, phone_id, liked, noped, reason="captcha")  # ← AJOUT
            time.sleep(3)
            if phone_id:
                try:
                    delete_phone_geelark(phone_id)
                    print(f"  ✅ Profil supprimé : {phone_id}")
                except Exception as e:
                    print(f"  ⚠️ Erreur suppression : {e}")
            return {"liked": liked, "noped": noped, "errors": 0, "banned": True, "reason": "captcha"}
        

         # ── Envoi message au match ─────────────────────────────────────────────
        # ── Envoi message au match ─────────────────────────────────────────────
        print(f"  💬 Attente popup match (max 5s)...")
        _match_xml = None
        for _tick in range(5):
            adb(device, "shell uiautomator dump /sdcard/ui_match_check.xml")
            time.sleep(1.0)
            _xml_tmp = adb(device, "shell cat /sdcard/ui_match_check.xml").stdout
            if any(kw in _xml_tmp for kw in [
                "Say something nice", "say something nice",
            ]):
                _match_xml = _xml_tmp
                print(f"  💥 Popup Match détectée ({_tick+1}s) — envoi message...")
                break
            print(f"  ⏳ Pas encore ({_tick+1}/5)...")

        if _match_xml:
            # Clic sur le champ texte
            import unicodedata as _udata
            def _strip(s):
                return ''.join(c for c in _udata.normalize('NFD', s) if _udata.category(c) != 'Mn')
            _msg_raw = random.choice(OPENING_MESSAGES)
            _msg_esc = (_strip(_msg_raw)
                .replace("'", "").replace('"', '').replace('`', '')
                .replace('&', 'and').replace('<', '').replace('>', '')
                .replace(' ', '%s'))

            # Trouver et cliquer le champ
            _field_found = False
            for _hint in ["Say something nice"]:
                for _pat in [
                    rf'hint="{re.escape(_hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(_hint)}"',
                    rf'text="{re.escape(_hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_hint)}"',
                ]:
                    _m = re.findall(_pat, _match_xml)
                    if _m:
                        _x1,_y1,_x2,_y2 = map(int,_m[0])
                        adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                        _field_found = True
                        time.sleep(0.8)
                        break
                if _field_found:
                    break
            if not _field_found:
                _res = adb(device, "shell wm size")
                _mw = re.search(r'(\d+)x(\d+)', _res.stdout)
                if _mw:
                    _w,_h = int(_mw.group(1)),int(_mw.group(2))
                    adb(device, f"shell input tap {_w//2} {int(_h*0.86)}")
                time.sleep(0.8)

            # Saisir le message
            adb(device, f"shell input text \"{_msg_esc}\"")
            time.sleep(0.8)

            # Trouver et cliquer Send — bloquer jusqu'à confirmation
            _send_clicked = False
            for _stry in range(8):
                adb(device, "shell uiautomator dump /sdcard/ui_send.xml")
                time.sleep(0.4)
                _xml_send = adb(device, "shell cat /sdcard/ui_send.xml").stdout
                for _stxt in ["Send", "SEND", "Envoyer"]:
                    for _sp in [
                        rf'text="{re.escape(_stxt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(_stxt)}"',
                        rf'content-desc="{re.escape(_stxt)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(_stxt)}"',
                    ]:
                        _sm = re.findall(_sp, _xml_send)
                        if _sm:
                            _x1,_y1,_x2,_y2 = map(int,_sm[0])
                            adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                            print(f"  ✅ Send cliqué ({_stry+1}) — message envoyé !")
                            _send_clicked = True
                            break
                    if _send_clicked:
                        break
                if _send_clicked:
                    break
                print(f"  ⏳ Send pas encore là ({_stry+1}/8)...")
                time.sleep(0.5)

            if not _send_clicked:
                # Fallback coordonnées
                _res2 = adb(device, "shell wm size")
                _mw2 = re.search(r'(\d+)x(\d+)', _res2.stdout)
                if _mw2:
                    _w2,_h2 = int(_mw2.group(1)),int(_mw2.group(2))
                    adb(device, f"shell input tap {int(_w2*0.88)} {int(_h2*0.86)}")
                    print(f"  🎯 Send fallback coordonnées")
            time.sleep(1.5)
        else:
            print(f"  ⏭️ Pas de popup match en 5s — on continue")

        # ── Redémarrage instagram après LIKE ─────────────────────────────────────
        print(f"  🔄 Redémarrage instagram après LIKE...")
        adb(device, "shell am force-stop com.instagram")
        time.sleep(2)
        adb(device, "shell monkey -p com.instagram -c android.intent.category.LAUNCHER 1")
        time.sleep(6)

        if _check_network_error(device):
            print(f"  🌐 Erreur réseau après relance — arrêt")
            stop_phone(phone_id)
            return {"liked": liked, "noped": noped, "errors": 0, "banned": False, "reason": "network_error"}

        if _quick_captcha_check(device):
            print(f"  🤖 CAPTCHA détecté après relance — suppression profil...")
            adb(device, "shell am force-stop com.instagram")
            _send_ban_telegram(device, phone_id, liked, noped, reason="captcha")
            time.sleep(3)
            if phone_id:
                try:
                    delete_phone_geelark(phone_id)
                except Exception as e:
                    print(f"  ⚠️ Erreur suppression : {e}")
            return {"liked": liked, "noped": noped, "errors": 0, "banned": True, "reason": "captcha"}

        _ensure_swipe_screen(device)
        cy, x_left, x_right = _get_swipe_zone(device)
 
    # ── Vérification finale ────────────────────────────────────────────────────
    print(f"  🔍 Vérification statut après force match...")
    adb(device, "shell am force-stop com.instagram")
    time.sleep(2)
    adb(device, "shell monkey -p com.instagram -c android.intent.category.LAUNCHER 1")
    time.sleep(15)  # était 8, augmenté à 15

    # Fermer les popups AVANT de vérifier
    _ensure_swipe_screen(device, max_attempts=3)
    time.sleep(3)  # ← AJOUTER CETTE LIGNE

    if _quick_captcha_check(device):
        print(f"  🤖 CAPTCHA HUMAIN détecté en fin de session — suppression profil...")
        adb(device, "shell am force-stop com.instagram")
        _send_ban_telegram(device, phone_id, liked, noped, reason="captcha")  # ← AJOUT
        time.sleep(3)
        if phone_id:
            try:
                delete_phone_geelark(phone_id)
            except Exception as e:
                print(f"  ⚠️ Erreur suppression : {e}")
        return {"liked": liked, "noped": noped, "errors": 0, "banned": True, "reason": "captcha"}
 
    # APRÈS
    adb(device, f"shell input tap {540} {50}")
    time.sleep(2)
    statut_apres = check_instagram_account(device)
    if statut_apres == "network_error":
        print(f"  🌐 Erreur réseau après swipe — extinction téléphone (pas un ban)")
        adb(device, "shell am force-stop com.instagram")
        stop_phone(phone_id)
        return {"liked": liked, "noped": noped, "errors": 0, "banned": False, "reason": "network_error"}


    # ── Screenshot + Telegram après force match ────────────────────────────
    print(f"  📸 Screenshot bilan force match...")
    time.sleep(3)
    screenshot_fm = take_screenshot(device)
    phone_label = str(phone_id) if phone_id else device

    statut_apres = check_instagram_account(device)
    phone_label = str(phone_id) if phone_id else device

    if statut_apres == "banned":
        caption_fm = (
            f"🚫 <b>Compte BANNI après force match</b>\n"
            f"📱 Téléphone : {phone_label}\n"
            f"❤️ Likes : {liked} | 👎 Nopes : {noped}"
        )
    else:
        caption_fm = (
            f"✅ <b>Compte vivant après force match</b>\n"
            f"📱 Téléphone : {phone_label}\n"
            f"❤️ Likes : {liked} | 👎 Nopes : {noped}"
        )

    if screenshot_fm:
        telegram_send_photo(screenshot_fm, caption_fm)
    else:
        telegram_send_message(caption_fm)
    # ───────────────────────────────────────────────────────────────────────

    if statut_apres == "banned":
        print(f"  🚫 Compte BANNI après force match")
        _send_ban_telegram(device, phone_id, liked, noped, reason="ban détecté fin de session")
        time.sleep(5)
        return {"liked": liked, "noped": noped, "errors": 0, "banned": True}
 
    print(f"\n  ✅ Force Match terminé ! ❤️ {liked} likes | 👎 {noped} nopes")
    return {"liked": liked, "noped": noped, "errors": 0, "banned": False}


def do_swipes(device: str,
              swipe_count: int = 50,
              like_ratio: float = 0.85,
              delay_min: float = 0.9,
              delay_max: float = 2.8,
              stop_flag: list = None,
              phone_id=None):
    if stop_flag is None:
        stop_flag = [False]
 
    print(f"  📲 Arrêt & relance de instagram...")
    adb(device, "shell am force-stop com.instagram")
    time.sleep(2)
    adb(device, "shell monkey -p com.instagram -c android.intent.category.LAUNCHER 1")
    time.sleep(6)
 
    print(f"  📧 Vérification popup email avant check...")
    handle_verify_email_popup(device)
    time.sleep(2)
 
    # ── Check captcha à l'ouverture ───────────────────────────────────────────
    if _quick_captcha_check(device):
        print(f"  🤖 CAPTCHA HUMAIN détecté à l'ouverture — suppression profil...")
        adb(device, "shell am force-stop com.instagram")
        if phone_id:
            try:
                delete_phone_geelark(phone_id)
                print(f"  ✅ Profil supprimé : {phone_id}")
            except Exception as e:
                print(f"  ⚠️ Erreur suppression : {e}")
        return {"liked": 0, "noped": 0, "errors": 0, "banned": True, "reason": "captcha"}
 
    print(f"  🔍 Vérification statut compte AVANT swipe...")
    adb(device, f"shell input tap {540} {50}")
    time.sleep(0.8)
    statut = check_instagram_account(device)
    if statut == "network_error":
        print(f"  🌐 Erreur réseau avant swipe — extinction téléphone (pas un ban)")
        adb(device, "shell am force-stop com.instagram")
        stop_phone(phone_id)
        return {"liked": 0, "noped": 0, "errors": 0, "banned": False, "reason": "network_error"}
    if statut == "banned":
        print(f"  🚫 Compte BANNI avant swipe — annulé")
        adb(device, "shell am force-stop com.instagram")
        return {"liked": 0, "noped": 0, "errors": 0, "banned": True}
 
    print(f"  ✅ Compte vivant — démarrage du swipe")
 
    _ensure_swipe_screen(device)
    cy, x_left, x_right = _get_swipe_zone(device)
 
    print(f"  💘 Swipe démarré : {swipe_count} profils | "
          f"{int(like_ratio*100)}% like | délai {delay_min}–{delay_max}s")
 
    liked, noped = 0, 0
    # Check captcha toutes les N swipes pour ne pas trop ralentir
    CAPTCHA_CHECK_EVERY = 5
 
    for i in range(swipe_count):
        if stop_flag[0]:
            print(f"  ⛔ Stop demandé — arrêt swipe")
            break
 
        # ── Check captcha périodique (toutes les CAPTCHA_CHECK_EVERY swipes) ──
        if i > 0 and i % CAPTCHA_CHECK_EVERY == 0:
            if _quick_captcha_check(device):
                print(f"  🤖 CAPTCHA HUMAIN détecté au swipe {i+1} — suppression profil...")
                adb(device, "shell am force-stop com.instagram")
                _send_ban_telegram(device, phone_id, liked, noped, reason="captcha")  # ← AJOUT
                time.sleep(3)
                if phone_id:
                    try:
                        delete_phone_geelark(phone_id)
                        print(f"  ✅ Profil supprimé : {phone_id}")
                    except Exception as e:
                        print(f"  ⚠️ Erreur suppression : {e}")
                return {"liked": liked, "noped": noped, "errors": 0, "banned": True, "reason": "captcha"}
 
        _ensure_swipe_screen(device)
 
        # ── Détection "Likes You" → LIKE forcé ────────────────────────────────
        adb(device, "shell uiautomator dump /sdcard/ui_swipe.xml")
        time.sleep(0.3)
        xml_check = adb(device, "shell cat /sdcard/ui_swipe.xml").stdout
 
        # Check captcha dans le même dump
        if any(kw in xml_check.lower() for kw in [
            "verify you're a human", "verify you\u2019re a human",
            "solve this puzzle", "start puzzle", "funcaptcha", "arkose",
        ]):
            print(f"  🤖 CAPTCHA HUMAIN détecté dans dump swipe — suppression profil...")
            adb(device, "shell am force-stop com.instagram")
            _send_ban_telegram(device, phone_id, liked, noped, reason="captcha")  # ← AJOUT
            time.sleep(3)
            if phone_id:
                try:
                    delete_phone_geelark(phone_id)
                    print(f"  ✅ Profil supprimé : {phone_id}")
                except Exception as e:
                    print(f"  ⚠️ Erreur suppression : {e}")
            return {"liked": liked, "noped": noped, "errors": 0, "banned": True, "reason": "captcha"}
 
        has_likes_you = any(kw in xml_check for kw in [
            "Likes You", "Likes you", "likes you",
            "liked you", "Liked You",
        ])
 
        if has_likes_you:
            go_like = True
            print(f"  💖 'Likes You' détecté — LIKE forcé !")
        else:
            go_like = random.random() < like_ratio
 
        y = cy + random.randint(-50, 50)
        duration = random.randint(200, 400)
 
        if go_like:
            sx = x_left + random.randint(-20, 20)
            ex = x_right + random.randint(-20, 20)
            adb(device, f"shell input swipe {sx} {y} {ex} {y} {duration}")
            liked += 1
        else:
            sx = x_right + random.randint(-20, 20)
            ex = x_left + random.randint(-20, 20)
            adb(device, f"shell input swipe {sx} {y} {ex} {y} {duration}")
            noped += 1
 
        time.sleep(0.8)
        popup_closed = _close_popups(device)
        if popup_closed:
            print(f"  💚 Popup fermée — reprise")
        else:
            wait = random.uniform(delay_min, delay_max)
            if random.random() < 0.04:
                wait += random.uniform(3, 7)
                print(f"  ⏸  Pause naturelle ({wait:.1f}s)...")
            time.sleep(wait)
 
        like_label = "❤️  LIKE" if go_like else "👎 NOPE"
        likes_you_tag = " 💖[LIKES YOU]" if has_likes_you else ""
        print(f"  [{i+1:>3}/{swipe_count}] "
            f"{like_label}{likes_you_tag} "
            f"— cumul ❤️{liked} 👎{noped}")
 
    # ── Vérification finale ────────────────────────────────────────────────────
    print(f"  🔍 Vérification statut compte APRÈS swipe...")
    adb(device, "shell am force-stop com.instagram")
    time.sleep(2)
    adb(device, "shell monkey -p com.instagram -c android.intent.category.LAUNCHER 1")
    time.sleep(8)
 
    if _quick_captcha_check(device):
        print(f"  🤖 CAPTCHA HUMAIN détecté en fin de session — suppression profil...")
        adb(device, "shell am force-stop com.instagram")
        _send_ban_telegram(device, phone_id, liked, noped, reason="captcha")  # ← AJOUT
        time.sleep(3)
        if phone_id:
            try:
                delete_phone_geelark(phone_id)
            except Exception as e:
                print(f"  ⚠️ Erreur suppression : {e}")
        return {"liked": liked, "noped": noped, "errors": 0, "banned": True, "reason": "captcha"}
 
    adb(device, f"shell input tap {540} {50}")
    time.sleep(0.8)
    statut_apres = check_instagram_account(device)
    if statut_apres == "network_error":
        print(f"  🌐 Erreur réseau après swipe — extinction téléphone (pas un ban)")
        adb(device, "shell am force-stop com.instagram")
        stop_phone(phone_id)
        return {"liked": liked, "noped": noped, "errors": 0, "banned": False, "reason": "network_error"}


    # ── Screenshot + Telegram après swipe ─────────────────────────────────
    print(f"  📸 Screenshot bilan swipe...")
    time.sleep(2)
    screenshot_swipe = take_screenshot(device)
    phone_label = str(phone_id) if phone_id else device

    if statut_apres == "banned":
        caption_swipe = (
            f"🚫 <b>Compte BANNI après swipe</b>\n"
            f"📱 Téléphone : {phone_label}\n"
            f"❤️ Likes : {liked} | 👎 Nopes : {noped}"
        )
    else:
        caption_swipe = (
            f"✅ <b>Compte vivant après swipe</b>\n"
            f"📱 Téléphone : {phone_label}\n"
            f"❤️ Likes : {liked} | 👎 Nopes : {noped}"
        )

    if screenshot_swipe:
        telegram_send_photo(screenshot_swipe, caption_swipe)
    else:
        telegram_send_message(caption_swipe)
    # ───────────────────────────────────────────────────────────────────────

    adb(device, "shell am force-stop com.instagram")

    if statut_apres == "banned":
        print(f"  🚫 Compte BANNI après swipe")
        return {"liked": liked, "noped": noped, "errors": 0, "banned": True}

    print(f"  ✅ Compte toujours vivant après swipe")
    print(f"\n  ✅ Swipe terminé ! ❤️ {liked} likes | 👎 {noped} nopes")
    return {"liked": liked, "noped": noped, "errors": 0, "banned": False}



def run_swipe_session(phone_id: str,
                       swipe_proxy: dict,
                       swipe_count: int = 50,
                       like_ratio: float = 0.85,
                       delay_min: float = 0.9,
                       delay_max: float = 2.8,
                       stop_flag: list = None,
                       rotate_url: str = "",
                       rotate_wait_sec: int = 15,
                       force_match: bool = False):

    if stop_flag is None:
        stop_flag = [False]

    def stopped():
        return stop_flag[0]

    print(f"\n{'='*55}")
    print(f"  SWIPE SESSION — téléphone {phone_id}")
    print(f"  Proxy swipe : {swipe_proxy.get('host')}:{swipe_proxy.get('port')}")
    print(f"{'='*55}")

    # 1. Changer le proxy
    proxy_ok = change_phone_proxy(
        phone_id,
        swipe_proxy.get("host", ""),
        swipe_proxy.get("port", ""),
        swipe_proxy.get("user", ""),
        swipe_proxy.get("pass", ""),
        swipe_proxy.get("type", "socks5"),
    )
    if not proxy_ok:
        print(f"  ⚠️ Proxy non changé — on continue quand même")

    # 1b. Rotation IP
    if rotate_url:
        print(f"  🔁 Rotation IP swipe...")
        try:
            r = requests.get(rotate_url, timeout=15)
            print(f"  ✅ Rotation OK : {r.text.strip()[:60]}")
        except Exception as e:
            print(f"  ⚠️ Rotation échouée : {e}")
        print(f"  ⏳ Attente {rotate_wait_sec}s...")
        time.sleep(rotate_wait_sec)

    if stopped():
        return {"success": False, "reason": "stopped"}

    # 2. Démarrer le téléphone
    ok = start_phone_with_retry(phone_id)
    if not ok:
        return {"success": False, "reason": "start_failed"}

    time.sleep(15)
    if stopped():
        stop_phone(phone_id)
        return {"success": False, "reason": "stopped"}

    # 3. Activation ADB
    print(f"  🔧 Activation ADB...")
    enable_adb(phone_id)
    time.sleep(5)

    # 4. Attendre ADB
    # APRÈS :
    # ── ADB avec retry automatique si timeout ──────────────────────────────
    ADB_MAX_RETRIES = 3
    device, pwd = None, None
    for _adb_attempt in range(ADB_MAX_RETRIES):
        device, pwd = wait_for_adb(phone_id, max_wait=120)
        if device:
            break
        print(f"  ⚠️ ADB timeout (tentative {_adb_attempt+1}/{ADB_MAX_RETRIES}) — relance du téléphone...")
        stop_phone(phone_id)
        time.sleep(8)
        if _adb_attempt < ADB_MAX_RETRIES - 1:
            ok_retry = start_phone(phone_id)
            if not ok_retry:
                print(f"  ❌ Impossible de relancer le téléphone {phone_id}")
                break
            print(f"  ⏳ Attente boot après relance ({15}s)...")
            time.sleep(15)
            enable_adb(phone_id)
            time.sleep(5)

    if not device:
        print(f"  ❌ ADB définitivement indisponible après {ADB_MAX_RETRIES} tentatives — abandon")
        stop_phone(phone_id)
        return {"success": False, "reason": "adb_timeout"}

    if stopped():
        stop_phone(phone_id)
        return {"success": False, "reason": "stopped"}

    # 5. Connexion glogin
    print(f"  🔗 Connexion ADB : {device}...")

    def try_glogin(device, pwd, max_attempts=30):
        for attempt in range(max_attempts):
            if stopped():
                return False

            # Déconnexion forcée avant reconnexion
            subprocess.run(f'"{ADB_PATH}" disconnect {device}', shell=True, capture_output=True)
            time.sleep(1)
            subprocess.run(f'"{ADB_PATH}" connect {device}', shell=True, capture_output=True)
            time.sleep(4)

            try:
                result = subprocess.run(
                    f'"{ADB_PATH}" -s {device} shell glogin {pwd}',
                    shell=True, capture_output=True, text=True, timeout=15
                )
                output = (result.stdout + result.stderr).strip()
            except subprocess.TimeoutExpired:
                output = ""
                print(f"  ⏱️ glogin [{attempt+1}] → timeout commande")

            print(f"  glogin [{attempt+1}] → '{output}'")

            if "success" in output.lower():
                return True

            # Réponse vide = shell pas encore prêt
            if not output:
                print(f"  ⏳ Réponse vide — attente 8s...")
                time.sleep(8)
            else:
                time.sleep(3)

        return False

    # APRÈS :
    connected = try_glogin(device, pwd, max_attempts=30)

    # Si toujours pas connecté → re-fetch ADB info et retry
    if not connected:
        print(f"  🔄 Échec glogin — re-fetch ADB info...")
        device2, pwd2 = wait_for_adb(phone_id, max_wait=60)
        if device2 and (device2 != device or pwd2 != pwd):
            print(f"  🔄 Nouvelles infos ADB : {device2} — retry glogin...")
            device, pwd = device2, pwd2
            connected = try_glogin(device, pwd, max_attempts=10)
        elif device2:
            print(f"  🔄 Mêmes infos ADB — retry glogin avec délai plus long...")
            connected = try_glogin(device, pwd, max_attempts=10)

    if not connected:
        # ── Relance complète du téléphone si glogin définitivement échoué ──
        print(f"  🔄 glogin échoué — relance complète du téléphone {phone_id}...")
        stop_phone(phone_id)
        time.sleep(10)
        ok_reboot = start_phone(phone_id)
        if ok_reboot:
            time.sleep(20)
            enable_adb(phone_id)
            time.sleep(5)
            device3, pwd3 = wait_for_adb(phone_id, max_wait=150)
            if device3:
                device, pwd = device3, pwd3
                connected = try_glogin(device, pwd, max_attempts=15)
                print(f"  {'✅ Reconnecté après relance !' if connected else '❌ glogin définitivement échoué après relance'}")

    if not connected:
        print(f"  ❌ glogin définitivement échoué — abandon")
        stop_phone(phone_id)
        return {"success": False, "reason": "glogin_failed"}

    if stopped():
        stop_phone(phone_id)
        return {"success": False, "reason": "stopped"}

    # 6. Exécuter le bon mode
    if force_match:
            result = do_force_match_swipes(device, swipe_count, stop_flag=stop_flag, phone_id=phone_id)
    else:
            result = do_swipes(device, swipe_count, like_ratio, delay_min, delay_max, stop_flag=stop_flag, phone_id=phone_id)

    # 7. Si banni → supprimer le profil GeeLark
    # 7. Si banni → screenshot + Telegram AVANT suppression
    if result.get("banned"):
        print(f"  ⏳ Attente envoi Telegram avant suppression (15s)...")
        time.sleep(15)
        print(f"  🗑️ Suppression du profil GeeLark banni : {phone_id}...")
        try:
            delete_phone_geelark(phone_id)
            print(f"  ✅ Profil supprimé : {phone_id}")
        except Exception as e:
            print(f"  ⚠️ Erreur suppression : {e}")
            stop_phone(phone_id)
        return {
            "success": False,
            "reason": "banned",
            "liked": result["liked"],
            "noped": result["noped"],
            "errors": 0,
        }

    # 8. Arrêter normalement
    stop_phone(phone_id)

    return {
        "success": True,
        "liked": result["liked"],
        "noped": result["noped"],
        "errors": result["errors"],
    }

# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════


def get_number_from_smsbower_phone():
    """Obtient un numéro de téléphone via SMSBower pour Instagram."""
    try:
        response = requests.get(SMSBOWER_URL, params={
            "api_key": SMSBOWER_API_KEY,
            "action":  "getNumberV2",
            "service": "ig",
            "country": "187",
        }, timeout=20)
        print(f"  SMSBower Phone → {response.text}")
        try:
            data = response.json()
        except:
            print(f"  ⚠️ Réponse non-JSON : {response.text[:80]}")
            return None
        activation_id = str(data.get("activationId", ""))
        number = str(data.get("phoneNumber", ""))
        if not activation_id or not number:
            print(f"  ⚠️ Pas de numéro : {response.text[:80]}")
            return None
        if is_blacklisted(number):
            cancel_bower_number(activation_id)
            return None
        print(f"  ✅ [BOWER PHONE] Numéro : {number} (ID: {activation_id})")
        return activation_id, number, "bower"
    except Exception as e:
        print(f"  ⚠️ SMSBower phone erreur : {e}")
        return None
    

def run():
    global DEBUG_MODE, all_phones

    print("\n" + "="*60)
    print("  instagram AUTO - Création de comptes")
    print("="*60)

    load_photo_folders()

    print("\n🚀 Choisir le mode de lancement :")
    print("  1 - Mode AUTO (GeeLark + tout automatique)")
    print("  2 - Mode DEBUG (GeeLark + étape par étape)")
    print("  3 - Mode MANUEL (ADB manuel + debug, sans GeeLark)")
    choix = input("Ton choix (1, 2 ou 3) : ").strip()

    if choix == "3":
        DEBUG_MODE = True
        print("🔧 Mode MANUEL activé\n")
        nb = int(input("Combien d'instances ? "))
        for i in range(nb):
            raw = input(f"instance {i+1} (ex: 199.190.44.226:25894:f82a0c) : ").strip()
            parts = raw.strip().split(":")
            device = f"{parts[0]}:{parts[1]}"
            code = parts[2]
            subprocess.run(f'"{ADB_PATH}" connect {device}', shell=True, capture_output=True)
            time.sleep(1)
            result = subprocess.run(f'"{ADB_PATH}" -s {device} shell glogin {code}', shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
            print(f"  glogin : {result.stdout.strip()}")
            if "success" in result.stdout.lower() or "already logged" in result.stdout.lower():
                photo_folder = get_next_photo_folder()
                open_instagram(device, photo_folder, city="manuel", lat=0, lon=0)
        return

    DEBUG_MODE = choix == "2"
    print("🐛 Mode DEBUG activé\n" if DEBUG_MODE else "⚡ Mode AUTO activé\n")

    print("\n  Récupération des téléphones GeeLark...")
    all_phones = get_all_phones()
    if not all_phones:
        print("  Aucun téléphone trouvé.")
        return

    print(f"\n  Quels téléphones lancer ? (ex: 1,3,5 ou 'all')")
    choice = input("  > ").strip()
    if choice.lower() == "all":
        selected = all_phones
    else:
        try:
            indices  = [int(x.strip()) - 1 for x in choice.split(",")]
            selected = [all_phones[i] for i in indices if 0 <= i < len(all_phones)]
        except ValueError:
            print("  Saisie invalide.")
            return

    phone_ids = [p["id"] for p in selected]
    nb        = len(phone_ids)
    print(f"\n  {nb} téléphone(s) sélectionné(s)")

    # ── Démarrage du pool scraper ──────────────────────────────────────
    nb_comptes = len(phone_ids)
    pool_target = max(nb_comptes + 2, 5)   # toujours quelques numéros d'avance
    pool_stop   = start_pool_scraper(target_size=pool_target)
    print(f"  🔄 Pool scraper démarré (target={pool_target} numéros)")

    # ── Console 1 : logs du pool scraper ─────────────────────────────
    def _console_pool_logs():
        print("\n" + "═"*55)
        print("  CONSOLE — Recherche de numéros (pool)")
        print("═"*55)
        while True:
            try:
                msg = _pool_log_queue.get(timeout=1)
                print(f"  [POOL] {msg}")
            except _queue.Empty:
                pass

    # ── Console 2 : inventaire en temps réel ─────────────────────────
    def _console_pool_inventory():
        print("\n" + "═"*55)
        print("  CONSOLE — Inventaire numéros disponibles")
        print("═"*55)
        while True:
            _pool_inventory_event.wait(timeout=5)
            _pool_inventory_event.clear()
            now = time.time()
            with _number_pool_lock:
                entries = list(_number_pool)
            valid = [(e, e["expires_at"] - now) for e in entries if e["expires_at"] > now]
            print(f"\n  ┌─ Numéros disponibles : {len(valid)} ────────────────")
            if valid:
                for i, (e, remaining) in enumerate(valid, 1):
                    mins = int(remaining // 60)
                    secs = int(remaining % 60)
                    print(f"  │ {i}. {e['number']:>15}  [{e['provider']:12}]  expire dans {mins:02d}m{secs:02d}s")
            else:
                print(f"  │  (aucun numéro en stock)")
            print(f"  └────────────────────────────────────────────────")

    _t_logs      = _threading.Thread(target=_console_pool_logs,      daemon=True, name="ConsoleLogs")
    _t_inventory = _threading.Thread(target=_console_pool_inventory,  daemon=True, name="ConsoleInventory")
    _t_logs.start()
    _t_inventory.start()

    # Laisser le pool se remplir un peu avant de commencer
    print(f"  ⏳ Préchauffage du pool (10s)...")
    time.sleep(10)

    def _run_one_phone(idx, phone_id, phone_label):
        print(f"\n{'='*60}")
        print(f"  [{idx+1}/{nb}] Téléphone : {phone_label}")
        print(f"{'='*60}")

        photo_folder = get_next_photo_folder()

        # Attendre un numéro disponible avant de démarrer le profil
        global _pre_fetched_number
        pool_log(f"[{phone_label}] ⏳ Attente d'un numéro avant démarrage...")
        _pre_fetched_number = None
        while True:
            _pf = pool_get_number()
            if not _pf:
                _tmp = get_hero_number()
                if _tmp:
                    _aid, _num, _prov = _tmp
                    _fmt = format_number(_num)
                    if _fmt:
                        _pf = (_aid, _fmt, _prov)
            if _pf:
                _pre_fetched_number = _pf
                pool_log(f"[{phone_label}] ✅ Numéro prêt : {_pf[1]} ({_pf[2]}) — démarrage téléphone")
                break
            pool_log(f"[{phone_label}] ⏳ Pas de numéro dispo — retry 5s...")
            time.sleep(5)

        started = start_phone(phone_id)
        if not started:
            _pre_fetched_number = None
            return

        device, pwd = wait_for_adb(phone_id, max_wait=150)
        if not device:
            stop_phone(phone_id)
            return

        started_phones.append(phone_id)
        connected = False
        for attempt in range(10):
            subprocess.run(f'"{ADB_PATH}" connect {device}', shell=True, capture_output=True)
            time.sleep(3)
            result = subprocess.run(f'"{ADB_PATH}" -s {device} shell glogin {pwd}', shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
            print(f"  [{phone_label}] glogin [{attempt+1}] : {result.stdout.strip()}")
            if "success" in result.stdout.lower():
                connected = True
                break

        if not connected:
            stop_phone(phone_id)
            return

        city, lat, lon = apply_random_city_gps(phone_id)
        time.sleep(2)

        try:
            open_instagram(device, photo_folder, city, lat, lon, phone_id=phone_id)
        except Exception as e:
            print(f"  [{phone_label}] Exception : {e}")
        finally:
            if not DEBUG_MODE:
                stop_phone(phone_id)

    threads = []
    for idx, phone_id in enumerate(phone_ids):
        t = _threading.Thread(
            target=_run_one_phone,
            args=(idx, phone_id, selected[idx]['label']),
            daemon=True,
            name=f"Phone-{phone_id}"
        )
        threads.append(t)
        t.start()
        time.sleep(5)  # petit décalage entre chaque lancement

    # Attendre que tous les threads finissent
    for t in threads:
        t.join()

    print(f"\n{'='*60}")
    print(f"  Terminé ! {nb} téléphone(s) traité(s)")
    print(f"{'='*60}")




# ─────────────────────────────────────────
#  VÉRIFICATION COMPTES
# ─────────────────────────────────────────

TELEGRAM_TOKEN   = "7108650754:AAGYfDhJc1GDlj_U_urSN5grp_dLYpFPmZo"
TELEGRAM_CHAT_ID = "5899192308"
PROXY_CHANGE_URL = "https://i.fxdx.in/actionlinks/do/changeip/eTRgHKFVSXWA3-eY4hRBwQ"

check_stop_flag = [False]

def get_phone_ip(device):
    try:
        result = subprocess.run(
            f'"{ADB_PATH}" -s {device} shell curl -s --max-time 10 https://api.ipify.org',
            shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
        ip = result.stdout.strip()
        if ip and len(ip) > 6 and '.' in ip:
            return ip
        return "inconnu"
    except Exception as e:
        print(f"  Erreur IP téléphone : {e}")
        return "erreur"

def rotate_proxy_check(device):
    print("\n  === Rotation Proxy ===")
    ip_avant = get_phone_ip(device)
    print(f"  IP téléphone AVANT : {ip_avant}")
    try:
        r = requests.get(PROXY_CHANGE_URL, timeout=12)
        print(f"  Rotation envoyée : {r.text.strip()[:80]}")
    except Exception as e:
        print(f"  Erreur rotation : {e}")
    print("  Attente 18s...")
    time.sleep(18)
    ip_apres = get_phone_ip(device)
    print(f"  IP téléphone APRÈS : {ip_apres}")
    if ip_avant == ip_apres and ip_avant != "erreur":
        print("  ⚠️ IP n'a PAS changé !")
        return False
    print("  ✅ IP changée")
    return True

def close_all_popups_check(device):
    for _ in range(5):
        adb(device, "shell uiautomator dump /sdcard/ui.xml")
        result = adb(device, "shell cat /sdcard/ui.xml")
        xml = result.stdout.lower()
        if "double date" in xml or "invite friends" in xml:
            subprocess.run(f'"{ADB_PATH}" -s {device} shell input tap 147 248', shell=True)
            time.sleep(2)
            continue
        if "want to avoid" in xml or "avoid someone" in xml:
            subprocess.run(f'"{ADB_PATH}" -s {device} shell input tap 124 181', shell=True)
            time.sleep(2)
            continue
        if "maybe later" in xml:
            click_button(device, ["Maybe later", "Maybe Later"])
            time.sleep(2)
            continue
        if "no thanks" in xml.lower():
            click_button(device, ["No thanks", "No Thanks", "NO THANKS"])
            time.sleep(2)
            continue
        if "skip" in xml:
            click_button(device, ["Skip", "SKIP"])
            time.sleep(2)
            continue
        if "instagram u" in xml.lower() or "see more students" in xml.lower():
            click_button(device, ["NO THANKS", "No Thanks", "No thanks"])
            time.sleep(2)
            continue
        break

def check_instagram_account(device):
    print("  Lancement instagram...")
    adb(device, "shell monkey -p com.instagram -c android.intent.category.LAUNCHER 1")
    time.sleep(2)
    close_all_popups_check(device)
    time.sleep(3)

    handle_verify_email_popup(device)
    time.sleep(2)

    for _attempt in range(3):
        adb(device, "shell uiautomator dump /sdcard/ui_privacy.xml")
        time.sleep(0.4)
        xml_priv = adb(device, "shell cat /sdcard/ui_privacy.xml").stdout
        privacy_keywords = [
            "privacy preference center",
            "tcf purposes",
            "i accept",
            "personalize",
            "list of tcf partners",
        ]
        if any(kw in xml_priv.lower() for kw in privacy_keywords):
            print(f"  🛡️ Popup Privacy détectée — clic 'I accept'...")
            found = False
            for text in ["I accept", "I Accept", "i accept", "ACCEPT"]:
                matches = re.findall(
                    rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_priv)
                if not matches:
                    matches = re.findall(
                        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"', xml_priv)
                if matches:
                    x1, y1, x2, y2 = map(int, matches[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ 'I accept' cliqué ({cx},{cy})")
                    found = True
                    time.sleep(2)
                    break
            if not found:
                print(f"  ⚠️ 'I accept' non trouvé — fallback coordonnées")
                adb(device, "shell input tap 310 1013")
                time.sleep(2)
        else:
            break

    print("  🧹 Fermeture des popups éventuelles avant vérification...")
    _ensure_swipe_screen(device, max_attempts=3)
    time.sleep(1)

    adb(device, "shell uiautomator dump /sdcard/ui.xml")
    result = adb(device, "shell cat /sdcard/ui.xml")
    xml = result.stdout.lower()

    # ── Réseau indisponible → pas un ban ──────────────────────────────────
    network_keywords = [
        "network connection unavailable",
        "check that you have a data connection",
        "connexion réseau indisponible",
    ]
    if any(kw in xml for kw in network_keywords):
        print(f"  🌐 Erreur réseau — compte non vérifié (réseau KO)")
        return "network_error"

    # ══════════════════════════════════════════════════════════════════════
    # SEULS CES 2 ÉCRANS = BANNI
    # ── "Something went wrong" = shadowban/ban ────────────────────────────
    something_wrong_keywords = [
        "something went wrong",
        "something went wrong. please try again later",
    ]
    if any(kw in xml for kw in something_wrong_keywords):
        print(f"  🚫 Compte BANNI — 'Something went wrong' détecté")
        return "banned"

    human_verify_keywords = [
        "confirm you're human",
        "confirm you\u2019re human",
        "you won't be able to use your account",
        "community standards on account integrity",
        "account is not visible to people",
    ]
    if any(kw in xml for kw in human_verify_keywords):
        print(f"  🚫 Compte BANNI — 'Confirm you're human' détecté")
        return "banned"

    # ══════════════════════════════════════════════════════════════════════
    # SEULS CES 2 ÉCRANS = BANNI
    # ══════════════════════════════════════════════════════════════════════

    # Écran 1 : login "It Starts with a Swipe"
    login_screen = (
        "it starts with a swipe" in xml
        or (
            "continue with phone number" in xml
            and ("continue with google" in xml or "trouble signing in" in xml)
        )
    )

    # Écran 2 : captcha "Let's Verify You're a Human"
    captcha_screen = any(kw in xml for kw in [
        "let's verify you're a human",
        "let\u2019s verify you\u2019re a human",
        "please solve this puzzle",
        "start puzzle",
        "know you are a real person",
    ])

    if login_screen:
        print(f"  🚫 Compte BANNI — écran login détecté")
        return "banned"

    if captcha_screen:
        print(f"  🚫 Compte BANNI — captcha Arkose détecté")
        return "banned"

    # ══════════════════════════════════════════════════════════════════════
    # TOUT LE RESTE = VIVANT
    # ══════════════════════════════════════════════════════════════════════
    print(f"  ✅ Compte VIVANT — aucun écran de ban détecté")
    return "alive"


def telegram_send_message_check(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"  Erreur Telegram : {e}")

def take_screenshot_check(device):
    try:
        ts     = int(time.time())
        remote = f"/sdcard/screenshot_{ts}.png"
        local  = os.path.join(_TMP_DIR, f"screenshot_{ts}.png")
        adb(device, f"shell screencap -p {remote}")
        time.sleep(1)
        subprocess.run(
            f'"{ADB_PATH}" -s {device} pull {remote} "{local}"',
            shell=True, capture_output=True
        )
        adb(device, f"shell rm {remote}")
        return local if os.path.exists(local) else None
    except Exception as e:
        print(f"  Erreur screenshot : {e}")
        return None

def telegram_send_photo_check(photo_path, caption=""):
    try:
        with open(photo_path, "rb") as f:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                files={"photo": f}, timeout=15
            )
        os.remove(photo_path)
    except Exception as e:
        print(f"  Erreur Telegram photo : {e}")

def get_instagram_group_phones():
    all_items = []
    page = 1
    while True:
        result = geelark_request("POST", "/open/v1/phone/list", {"page": page, "pageSize": 100})
        if result.get("code") != 0:
            break
        data  = result.get("data") or {}
        items = data.get("items") or []
        all_items.extend(items)
        if len(items) < 100:
            break
        page += 1
    phones = []
    for phone in all_items:
        group_raw = phone.get("profileGroup") or phone.get("groupName") or phone.get("group") or ""
        if isinstance(group_raw, dict):
            group = (group_raw.get("name") or "").lower()
        else:
            group = str(group_raw).lower()
        if group == "instagram":
            phones.append({
                "id":   str(phone.get("id")),
                "name": phone.get("serialName", "?"),
            })
    return phones

def delete_phone_geelark(phone_id: str) -> bool:
    print(f"  ⏹ Arrêt avant suppression : {phone_id}...")
    try:
        stop_phone(phone_id)
    except:
        pass

    # Attendre statut OFF confirmé
    print(f"  ⏳ Attente statut OFF...")
    for attempt in range(10):  # Max 50s
        time.sleep(5)
        try:
            result = geelark_request("POST", "/open/v1/phone/list", {"page": 1, "pageSize": 100})
            phones = result.get("data", {}).get("items", [])
            phone = next((p for p in phones if str(p.get("id")) == str(phone_id)), None)
            if not phone:
                print(f"  ℹ️ Téléphone introuvable — déjà supprimé ?")
                return True
            status = phone.get("status")
            print(f"  Status : {status} ({attempt+1}/10)")
            if status == 2:  # OFF
                print(f"  ✅ Téléphone OFF — suppression...")
                break
        except Exception as e:
            print(f"  ⚠️ Erreur vérif : {e}")
    else:
        print(f"  ⚠️ Toujours pas OFF après 50s")

    # Delete
    for attempt in range(3):
        try:
            result = geelark_request("POST", "/open/v1/phone/delete", {"ids": [str(phone_id)]})
            print(f"  Réponse delete : {result}")
            data = result.get("data", {})
            if result.get("code") == 0 and data.get("successAmount", 0) > 0:
                print(f"  ✅ Supprimé !")
                return True
            details = data.get("failDetails", [])
            for d in details:
                err = d.get("code")
                print(f"  Erreur delete : {err} — {d.get('msg')}")
                if err == 42001:
                    return True  # Déjà supprimé
                if err in (43009, 43010, 43021):
                    print(f"  ⏳ Encore actif, re-stop + attente 10s...")
                    try: stop_phone(phone_id)
                    except: pass
                    time.sleep(10)
                    break
        except Exception as e:
            print(f"  Exception delete : {e}")
            time.sleep(5)

    return False

def run_check_session(config, stop_flag):
    """Session de vérification des comptes instagram."""
    phones     = get_instagram_group_phones()
    total      = len(phones)
    vivants    = 0
    bannis     = 0
    delete_ban = config.get("delete_banned", True)

    if not phones:
        print("  Aucun téléphone dans le groupe 'instagram'")
        return {"total": 0, "vivants": 0, "bannis": 0}

    print(f"  {total} compte(s) à vérifier")
    telegram_send_message_check(f"🔍 <b>Vérification démarrée</b>\n{total} compte(s) à vérifier...")

    for idx, phone in enumerate(phones):
        if stop_flag[0]:
            print("  ⛔ Arrêt demandé")
            break

        phone_id   = phone["id"]
        phone_name = phone["name"]
        print(f"\n{'='*55}")
        print(f"  [{idx+1}/{total}] {phone_name}")
        print(f"{'='*55}")

        if not start_phone(phone_id):
            continue

        enable_adb(phone_id)
        device, pwd = wait_for_adb(phone_id)

        if not device:
            stop_phone(phone_id)
            continue

        # Connexion ADB
        connected = False
        for attempt in range(8):
            subprocess.run(f'"{ADB_PATH}" connect {device}', shell=True, capture_output=True)
            time.sleep(3)
            result = subprocess.run(
                f'"{ADB_PATH}" -s {device} shell glogin {pwd}',
                shell=True, capture_output=True, text=True
            )
            if "success" in result.stdout.lower():
                connected = True
                break
            time.sleep(4)

        if not connected:
            print("  ❌ Connexion ADB impossible")
            stop_phone(phone_id)
            continue

        current_ip = get_phone_ip(device)
        print(f"  🌐 IP téléphone : {current_ip}")

        rotate_proxy_check(device)

        statut    = check_instagram_account(device)
        screenshot = take_screenshot_check(device)

        if statut == "banned":
            bannis += 1
            caption = f"🚫 <b>Compte banni</b>\n📱 {phone_name}\n🌐 IP : {current_ip}"
            if screenshot:
                telegram_send_photo_check(screenshot, caption)
                time.sleep(5)
            else:
                telegram_send_message_check(caption)
                time.sleep(5)
            if delete_ban:
                delete_phone_geelark(phone_id)
            else:
                stop_phone(phone_id)
        else:
            vivants += 1
            caption = f"✅ <b>Compte vivant</b>\n📱 {phone_name}\n🌐 IP : {current_ip}"
            if screenshot:
                telegram_send_photo_check(screenshot, caption)
            else:
                telegram_send_message_check(caption)
            adb(device, "shell am force-stop com.instagram")
            stop_phone(phone_id)

        time.sleep(6)

    summary = f"✅ <b>Vérification terminée</b>\nTotal : {total}\n✅ Vivants : {vivants}\n🚫 Bannis : {bannis}"
    print(f"\n  Terminé ! {vivants} vivants | {bannis} bannis")
    telegram_send_message_check(summary)
    return {"total": total, "vivants": vivants, "bannis": bannis}


def warmup_account_on_device(phone_id: str, duration_minutes: int, usernames: list) -> bool:
    """
    Warmup un compte instagram :
    - Scroll le feed, like des posts
    - Va parfois sur l'explore (loupe), cherche un username, follow si pas déjà suivi
    - Clique sur des profils, scroll, back
    - S'arrête après duration_minutes minutes
    """
    import time as _time
    print(f"  ⚡ Warmup → téléphone {phone_id} | {duration_minutes} min | {len(usernames)} username(s)")

    # ── 1. Démarrer le téléphone ──────────────────────────────────────
    ok = start_phone_with_retry(phone_id)
    if not ok:
        return False
    time.sleep(15)

    enable_adb(phone_id)
    time.sleep(5)
    device, pwd = wait_for_adb(phone_id, max_wait=150)
    if not device:
        print(f"  ❌ ADB timeout pour {phone_id}")
        stop_phone(phone_id)
        return False

    connected = False
    for attempt in range(30):
        subprocess.run(f'"{ADB_PATH}" connect {device}', shell=True, capture_output=True)
        time.sleep(3)
        result = subprocess.run(
            f'"{ADB_PATH}" -s {device} shell glogin {pwd}',
            shell=True, capture_output=True, text=True
        )
        if "success" in result.stdout.lower():
            connected = True
            break
    if not connected:
        print(f"  ❌ glogin échoué pour {phone_id}")
        stop_phone(phone_id)
        return False

    # ── 2. Ouvrir Instagram ───────────────────────────────────────────
    print(f"  📱 Ouverture Instagram...")
    adb(device, "shell am force-stop com.instagram.android")
    time.sleep(1)
    subprocess.run(
        f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
        f'-c android.intent.category.LAUNCHER 1',
        shell=True, capture_output=True, text=True
    )
    time.sleep(6)
    _click_allow_if_present(device)

    res = adb(device, "shell wm size")
    m = re.search(r'(\d+)x(\d+)', res.stdout)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (1080, 2400)

    # Attendre feed — tap home à chaque tick raté (robuste même si XML vide sur Reels)
    for tick in range(15):
        adb(device, "shell uiautomator dump /sdcard/ui_warmup_feed.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_warmup_feed.xml").stdout
        # Détecter page login / création compte → supprimer et stopper
        if _detect_logged_out_and_cleanup(device, phone_id, xml):
            stop_phone(phone_id)
            return False
        if any(kw in xml for kw in ["Your story", "For you"]):
            print(f"  ✅ Feed détecté ({tick+1}s)")
            handle_notifications_popup(device, safe_ui_dump(device, "/sdcard/ui_notif_popup.xml"))
            break
        # Feed pas encore visible : tap home (gère Reels, chargement lent, etc.)
        print(f"  ⏳ Attente feed ({tick+1}/15) — tap home...")
        adb(device, f"shell input tap {int(w*0.09)} {int(h*0.895)}")
        time.sleep(2)

    followed_users = set()  # usernames déjà suivis cette session
    start_ts      = _time.time()
    end_ts        = start_ts + duration_minutes * 60
    # Déclencher un follow dès le 1er cycle
    last_follow_ts = start_ts - 46
    cycle_num     = 0

    print(f"  ⏱ Warmup jusqu'à {duration_minutes} min — départ")

    while _time.time() < end_ts:
        cycle_num += 1
        remaining = int((end_ts - _time.time()) / 60)
        print(f"\n  ── Cycle warmup {cycle_num} ({remaining} min restantes) ──")

        # ── Vérifier qu'Instagram est toujours actif ───────────────────
        if not _warmup_insta_alive(device, w, h):
            print(f"  ❌ Instagram irrécupérable — arrêt warmup")
            break

        # ── Sortir de l'interface Reels si on y est tombé ──────────────
        _warmup_exit_reels_if_needed(device, w, h)

        # ── Détecter page login/création en cours de session ──────────
        adb(device, "shell uiautomator dump /sdcard/ui_wm_cycle.xml")
        time.sleep(0.4)
        xml_cycle = adb(device, "shell cat /sdcard/ui_wm_cycle.xml").stdout
        if _detect_logged_out_and_cleanup(device, phone_id, xml_cycle):
            stop_phone(phone_id)
            return False

        # ── Forcer un follow si aucun depuis +45s ─────────────────────
        force_follow = (_time.time() - last_follow_ts) >= 45

        # ── Décider ce qu'on fait ce cycle ────────────────────────────
        roll = random.random()

        if force_follow or roll < 0.25:
            # Explore + follow username (prioritaire si délai dépassé)
            print(f"  🔍 Explore + follow{'  ⚡ forcé' if force_follow else ''}...")
            candidates = [u for u in usernames if u not in followed_users]
            if not candidates:
                print(f"  ℹ️ Tous les usernames déjà suivis cette session — reset")
                followed_users.clear()
                candidates = list(usernames)
            if candidates:
                username = random.choice(candidates)
                did_follow = _warmup_explore_and_follow(device, w, h, username)
                if did_follow:
                    followed_users.add(username)
                    print(f"  ✅ Suivi : {username}")
                last_follow_ts = _time.time()

        elif roll < 0.80:
            # Scroll feed + quelques likes
            print(f"  📜 Scroll feed...")
            _warmup_scroll_feed(device, w, h)

        else:
            # Cliquer sur un profil, scroller, back
            print(f"  👤 Visite profil...")
            _warmup_visit_profile(device, w, h)

        # Revenir sur le feed home
        _warmup_go_home(device, w, h)

    print(f"  ✅ Warmup terminé ({cycle_num} cycles, {duration_minutes} min)")
    stop_phone(phone_id)
    return True


def _warmup_dismiss_meta_popup(device, w, h):
    """Ferme le popup 'Get more from your next reel' / Meta Verified en tapant en haut de l'écran."""
    try:
        adb(device, "shell uiautomator dump /sdcard/ui_meta_popup.xml")
        time.sleep(0.3)
        xml = adb(device, "shell cat /sdcard/ui_meta_popup.xml").stdout
        _popup_kw = [
            "Get more from your next reel", "Meta Verified",
            "Sign up for Meta Verified", "verified badge",
            "Get started",  # bouton du popup
        ]
        if any(kw in xml for kw in _popup_kw) and "Your story" not in xml:
            # Taper en haut de l'écran pour fermer le bottom-sheet
            adb(device, f"shell input tap {w//2} {int(h * 0.06)}")
            print(f"  ✖️ Popup Meta fermé (tap haut)")
            time.sleep(0.5)
            return True
    except Exception:
        pass
    return False


def _warmup_exit_reels_if_needed(device, w, h):
    """Si on est dans l'interface Reels (vue vidéo), fait un back pour revenir au feed."""
    try:
        adb(device, "shell uiautomator dump /sdcard/ui_reels_check.xml")
        time.sleep(0.3)
        xml = adb(device, "shell cat /sdcard/ui_reels_check.xml").stdout
        # Indicateurs qu'on est plongé dans Reels (pas le feed)
        on_reels = (
            "Add comment" in xml or
            ("Reels" in xml and "Friends" in xml and "Your story" not in xml)
        )
        if on_reels:
            print(f"  ⬅️ Interface Reels détectée — back vers feed")
            adb(device, "shell input keyevent KEYCODE_BACK")
            time.sleep(1)
            return True
    except Exception:
        pass
    return False


def _warmup_relaunch_insta(device, w, h):
    """Relance Instagram et attend le feed. Retourne True si réussi."""
    print(f"  🔄 Instagram fermé — relance...")
    subprocess.run(
        f'"{ADB_PATH}" -s {device} shell monkey -p com.instagram.android '
        f'-c android.intent.category.LAUNCHER 1',
        shell=True, capture_output=True, text=True
    )
    time.sleep(5)
    _click_allow_if_present(device)
    for _ in range(8):
        adb(device, "shell uiautomator dump /sdcard/ui_wr_check.xml")
        time.sleep(0.5)
        xml = adb(device, "shell cat /sdcard/ui_wr_check.xml").stdout
        if any(kw in xml for kw in ["Your story", "For you"]):
            adb(device, f"shell input tap {int(w*0.09)} {int(h*0.895)}")
            time.sleep(0.5)
            return True
        adb(device, f"shell input tap {int(w*0.09)} {int(h*0.895)}")
        time.sleep(1.5)
    return False


def _warmup_insta_alive(device, w, h):
    """Vérifie qu'Instagram est au premier plan, le relance sinon. Retourne True si OK."""
    res = adb(device, "shell dumpsys window windows")
    if "com.instagram.android" not in res.stdout:
        return _warmup_relaunch_insta(device, w, h)
    return True


def _warmup_scroll_feed(device, w, h):
    """Scroll le feed et like aléatoirement des posts."""
    nb_scrolls = random.randint(6, 12)
    likes_this_session = 0
    max_likes = random.randint(2, 3)  # max 2-3 likes par session
    for i in range(nb_scrolls):
        # Scroll vers le bas
        sy = random.randint(int(h * 0.55), int(h * 0.70))
        ey = random.randint(int(h * 0.25), int(h * 0.40))
        adb(device, f"shell input swipe {w//2} {sy} {w//2} {ey} {random.randint(400, 700)}")

        # Micro-pause de lecture aléatoire (simule qu'on lit le post)
        if random.random() < 0.5:
            time.sleep(random.uniform(0.3, 0.8))

        # Like aléatoire : 35% de chance ET quota non atteint
        if likes_this_session < max_likes and random.random() < 0.35:
            print(f"  ❤️ Like post {i+1}...")
            adb(device, "shell uiautomator dump /sdcard/ui_like_btn.xml")
            time.sleep(0.4)
            xml_like = adb(device, "shell cat /sdcard/ui_like_btn.xml").stdout
            _liked_post = False
            for _lid in [
                "com.instagram.android:id/row_feed_button_like",
                "com.instagram.android:id/like_button",
            ]:
                _lf = re.findall(
                    rf'resource-id="{re.escape(_lid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                    xml_like)
                if not _lf:
                    _lf = re.findall(
                        rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(_lid)}"',
                        xml_like)
                if _lf:
                    _x1,_y1,_x2,_y2 = map(int,_lf[0])
                    adb(device, f"shell input tap {(_x1+_x2)//2} {(_y1+_y2)//2}")
                    print(f"  ❤️ Like cliqué via resource-id ({(_x1+_x2)//2},{(_y1+_y2)//2})")
                    _liked_post = True
                    likes_this_session += 1
                    break
            if not _liked_post:
                print(f"  ⏭️ Like ignoré (resource-id non trouvé — évite Reels)")

        _click_allow_if_present(device)
        _warmup_dismiss_meta_popup(device, w, h)


def _warmup_explore_and_follow(device, w, h, username: str) -> bool:
    """
    Va sur l'onglet Explore (loupe), cherche username,
    vérifie qu'on ne le suit pas déjà, puis Follow.
    """
    # ── Cliquer sur la loupe (4ème onglet en bas) ─────────────────────
    print(f"  🔍 Tap loupe (explore)...")
    # Explore = 4ème icône de la nav bar (position ~80% largeur, ~96% hauteur)
    explore_x = int(w * 0.62)  # position correcte de la loupe
    explore_y = int(h * 0.895)

    # Essai via XML
    adb(device, "shell uiautomator dump /sdcard/ui_nav_explore.xml")
    time.sleep(0.5)
    xml_nav = adb(device, "shell cat /sdcard/ui_nav_explore.xml").stdout
    explore_clicked = False
    for desc in ["Explore", "Search"]:
        for pat in [
            rf'content-desc="{re.escape(desc)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(desc)}"',
        ]:
            found = re.findall(pat, xml_nav)
            for coords in found:
                x1, y1, x2, y2 = map(int, coords)
                cy = (y1+y2)//2
                cx = (x1+x2)//2
                # Doit être dans la nav bar (bas de l'écran)
                if cy >= int(h * 0.85):
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ Loupe cliquée ({cx},{cy})")
                    explore_clicked = True
                    break
            if explore_clicked:
                break
        if explore_clicked:
            break

    if not explore_clicked:
        adb(device, f"shell input tap {explore_x} {explore_y}")
        print(f"  🎯 Loupe fallback ({explore_x},{explore_y})")

    # ── Cliquer sur "Search" en haut ───────────────────────────────────
    print(f"  🔍 Tap champ Search...")
    search_clicked = False
    for tick in range(8):
        adb(device, "shell uiautomator dump /sdcard/ui_explore_search.xml")
        time.sleep(0.5)
        xml_ex = adb(device, "shell cat /sdcard/ui_explore_search.xml").stdout or ""
        for hint in ["Search", "Search with Meta AI", "search", "Rechercher"]:
            for pat in [
                rf'hint="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*hint="{re.escape(hint)}"',
                rf'text="{re.escape(hint)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(hint)}"',
            ]:
                found = re.findall(pat, xml_ex)
                if found:
                    x1, y1, x2, y2 = map(int, found[0])
                    cy = (y1+y2)//2
                    # Doit être dans la zone haute (barre de recherche)
                    if cy < int(h * 0.20):
                        adb(device, f"shell input tap {(x1+x2)//2} {cy}")
                        print(f"  ✅ Champ Search cliqué ({(x1+x2)//2},{cy})")
                        search_clicked = True
                        break
            if search_clicked:
                break
        if search_clicked:
            break
        if tick == 3:
            # Fallback : tap en haut centre
            adb(device, f"shell input tap {w//2} {int(h*0.07)}")
            print(f"  🎯 Search fallback haut centre")
            search_clicked = True
            break
        print(f"  ⏳ Search pas encore ({tick+1}/8)...")
        time.sleep(1)

    # ── Taper le username ──────────────────────────────────────────────
    clean_username = username.strip().lstrip('@')
    print(f"  ⌨️ Saisie username : {clean_username}")
    adb(device, "shell input keyevent KEYCODE_CTRL_A")
    adb(device, "shell input keyevent KEYCODE_DEL")
    adb(device, f"shell input text '{clean_username}'")

    # ── Cliquer sur le premier résultat ───────────────────────────────
    print(f"  👆 Sélection premier résultat...")
    result_clicked = False
    for tick in range(8):
        adb(device, "shell uiautomator dump /sdcard/ui_search_results.xml")
        time.sleep(0.5)
        xml_res = adb(device, "shell cat /sdcard/ui_search_results.xml").stdout

        # Chercher un résultat qui contient le username
        # Les résultats sont des éléments cliquables sous la barre de recherche
        clickables = re.findall(
            r'clickable="true"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_res)
        if not clickables:
            clickables = re.findall(
                r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*clickable="true"', xml_res)

        candidates = []
        for coords in clickables:
            x1, y1, x2, y2 = map(int, coords)
            cy = (y1+y2)//2
            cx = (x1+x2)//2
            bw, bh = x2-x1, y2-y1
            # Résultats de recherche = éléments larges entre 15% et 85% hauteur
            if cy > int(h * 0.15) and cy < int(h * 0.80) and bw > int(w * 0.6):
                candidates.append((cx, cy))

        if candidates:
            # Prendre le premier (le plus haut)
            candidates.sort(key=lambda c: c[1])
            cx, cy = candidates[0]
            adb(device, f"shell input tap {cx} {cy}")
            print(f"  ✅ Premier résultat cliqué ({cx},{cy})")
            result_clicked = True
            break
        print(f"  ⏳ Résultats pas encore ({tick+1}/8)...")
        time.sleep(1)

    if not result_clicked:
        print(f"  ❌ Aucun résultat — abandon follow pour {clean_username}")
        adb(device, "shell input keyevent KEYCODE_BACK")
        adb(device, "shell input keyevent KEYCODE_BACK")
        _warmup_insta_alive(device, w, h)
        return False

    _click_allow_if_present(device)

    # ── Vérifier si déjà suivi ─────────────────────────────────────────
    print(f"  🔍 Vérification si déjà suivi...")
    adb(device, "shell uiautomator dump /sdcard/ui_profile_check_follow.xml")
    time.sleep(0.5)
    xml_prof = adb(device, "shell cat /sdcard/ui_profile_check_follow.xml").stdout

    already_following = any(kw in xml_prof for kw in [
        "Following", "following", "Message", "message",
    ]) and not any(kw in xml_prof for kw in ["Follow", "follow"])

    # Plus précis : chercher le bouton "Following" (déjà suivi) vs "Follow" (pas suivi)
    has_following_btn = bool(re.findall(
        r'text="Following"[^>]*bounds=|bounds=[^>]*text="Following"', xml_prof))
    has_follow_btn = bool(re.findall(
        r'text="Follow"[^>]*bounds=|bounds=[^>]*text="Follow"', xml_prof))

    if has_following_btn and not has_follow_btn:
        print(f"  ℹ️ Déjà suivi ({clean_username}) — skip")
        adb(device, "shell input keyevent KEYCODE_BACK")
        adb(device, "shell input keyevent KEYCODE_BACK")
        _warmup_insta_alive(device, w, h)
        return False

    # ── Cliquer Follow ─────────────────────────────────────────────────
    follow_clicked = False
    for tick in range(8):
        adb(device, "shell uiautomator dump /sdcard/ui_follow_btn.xml")
        time.sleep(0.5)
        xml_follow = adb(device, "shell cat /sdcard/ui_follow_btn.xml").stdout

        for text in ["Follow", "FOLLOW", "Suivre"]:
            for pat in [
                rf'text="{re.escape(text)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*text="{re.escape(text)}"',
            ]:
                found = re.findall(pat, xml_follow)
                if found:
                    x1, y1, x2, y2 = map(int, found[0])
                    cx, cy = (x1+x2)//2, (y1+y2)//2
                    # Ne pas cliquer sur "Following" (déjà suivi)
                    node_ctx = xml_follow[max(0, xml_follow.find(f'[{x1},{y1}]')-100):xml_follow.find(f'[{x1},{y1}]')+50]
                    if 'Following' in node_ctx and text == 'Follow':
                        continue
                    adb(device, f"shell input tap {cx} {cy}")
                    print(f"  ✅ Follow cliqué ({cx},{cy})")
                    follow_clicked = True
                    break
            if follow_clicked:
                break
        if follow_clicked:
            break
        print(f"  ⏳ Follow pas encore ({tick+1}/8)...")
        time.sleep(1)

    if not follow_clicked:
        print(f"  ⚠️ Bouton Follow non trouvé pour {clean_username}")
        adb(device, "shell input keyevent KEYCODE_BACK")
        adb(device, "shell input keyevent KEYCODE_BACK")
        _warmup_insta_alive(device, w, h)
        return False

    # ── 2x Back pour revenir ───────────────────────────────────────────
    adb(device, "shell input keyevent KEYCODE_BACK")
    adb(device, "shell input keyevent KEYCODE_BACK")
    _warmup_insta_alive(device, w, h)
    print(f"  ✅ Follow {clean_username} OK — retour")
    return True


def _warmup_visit_profile(device, w, h):
    """Clique sur un profil dans le feed, scroll, puis back."""
    print(f"  👤 Visite profil aléatoire...")
    # Tap sur une zone de l'écran où se trouvent les photos de posts
    adb(device, "shell uiautomator dump /sdcard/ui_warmup_username.xml")
    time.sleep(0.4)
    xml_usr = adb(device, "shell cat /sdcard/ui_warmup_username.xml").stdout

    # Collecter TOUS les candidats username visibles pour en choisir un au hasard
    _all_candidates = []
    for _uid in [
        "com.instagram.android:id/row_feed_photo_profile_name",
        "com.instagram.android:id/feed_caption_username",
        "com.instagram.android:id/username",
    ]:
        for pat in [
            rf'resource-id="{re.escape(_uid)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*resource-id="{re.escape(_uid)}"',
        ]:
            for coords in re.findall(pat, xml_usr):
                x1, y1, x2, y2 = map(int, coords)
                cy = (y1+y2)//2
                # Garder seulement les éléments dans la zone centrale (pas navbar)
                if int(h * 0.05) < cy < int(h * 0.85):
                    _all_candidates.append(((x1+x2)//2, cy))

    _usr_clicked = False
    if _all_candidates:
        # Choisir aléatoirement parmi les profils visibles
        _cx, _cy = random.choice(_all_candidates)
        adb(device, f"shell input tap {_cx} {_cy}")
        print(f"  👤 Nom user cliqué ({_cx},{_cy})")
        _usr_clicked = True

    if not _usr_clicked:
        _name_x = int(w * 0.22)
        _name_y = int(h * 0.795)
        adb(device, f"shell input tap {_name_x} {_name_y}")
        print(f"  👤 Nom fallback ({_name_x},{_name_y})")

    # Attendre que le profil/post charge
    time.sleep(1.5)

    # Vérifier si on est sur un profil (ou un post)
    adb(device, "shell uiautomator dump /sdcard/ui_warmup_profile.xml")
    time.sleep(0.4)
    xml_p = adb(device, "shell cat /sdcard/ui_warmup_profile.xml").stdout

    on_profile = any(kw in xml_p for kw in [
        "Follow", "Following", "Posts", "Followers",
        "Edit profile", "Message",
    ])
    on_post = any(kw in xml_p for kw in [
        "Like", "Comment", "Share", "Save",
        "com.instagram.android:id/like_button",
    ])

    if on_profile or on_post:
        # Scroll un peu
        nb_scroll = random.randint(1, 3)
        for _ in range(nb_scroll):
            sy = random.randint(int(h * 0.55), int(h * 0.70))
            ey = random.randint(int(h * 0.25), int(h * 0.40))
            adb(device, f"shell input swipe {w//2} {sy} {w//2} {ey} {random.randint(400, 600)}")
        print(f"  ✅ Profil/post visité — back")
    else:
        print(f"  ℹ️ Rien d'intéressant — back")

    adb(device, "shell input keyevent KEYCODE_BACK")
    _warmup_insta_alive(device, w, h)
    _click_allow_if_present(device)


def _warmup_go_home(device, w, h):
    """Revient sur l'onglet Home (1ère icône en bas à gauche)."""
    adb(device, "shell uiautomator dump /sdcard/ui_warmup_nav.xml")
    time.sleep(0.4)
    xml_nav = adb(device, "shell cat /sdcard/ui_warmup_nav.xml").stdout

    home_clicked = False
    for desc in ["Home", "Feed", "home"]:
        for pat in [
            rf'content-desc="{re.escape(desc)}"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
            rf'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*content-desc="{re.escape(desc)}"',
        ]:
            found = re.findall(pat, xml_nav)
            for coords in found:
                x1, y1, x2, y2 = map(int, coords)
                cy = (y1+y2)//2
                cx = (x1+x2)//2
                if cy >= int(h * 0.85):
                    adb(device, f"shell input tap {cx} {cy}")
                    home_clicked = True
                    break
            if home_clicked:
                break
        if home_clicked:
            break

    if not home_clicked:
        # Fallback : bas gauche
        adb(device, f"shell input tap {int(w*0.09)} {int(h*0.895)}")
        print(f"  🎯 Home fallback bas-gauche")

    time.sleep(0.8)
    # Si on a atterri sur Reels au lieu du feed, faire un back
    _warmup_exit_reels_if_needed(device, w, h)

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n  Interruption — arrêt des téléphones...")
        for pid in started_phones:
            try:
                stop_phone(pid)
            except:
                pass
    except Exception as e:
        print(f"\n  Erreur : {e}")
        for pid in started_phones:
            try:
                stop_phone(pid)
            except:
                pass

