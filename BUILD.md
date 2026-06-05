# Compiler InstagramOps en `.exe` Windows (code protégé)

Objectif : produire `InstagramOps.exe` à déployer sur un VPS Windows, avec le
code source **compilé en langage machine (Nuitka)** donc non lisible.

> ⚠️ On ne peut **pas** compiler un `.exe` Windows depuis macOS. La compilation
> se fait **sur Windows** (VPS, PC, ou runner GitHub Actions).

---

## Option A — Compiler sur une machine Windows (le plus simple)

1. Copier le dossier du projet sur la machine Windows.
2. Installer [Python 3.11](https://www.python.org/downloads/) (cocher **Add to PATH**).
3. Double-cliquer **`build_windows.bat`** (ou le lancer dans un terminal).
4. Récupérer le résultat : **`dist\InstagramOps.exe`**.

La première compilation télécharge MinGW (compilateur C) automatiquement.
Durée : ~5–15 min.

## Option B — Compiler dans le cloud (GitHub Actions, source jamais sur le VPS)

1. Mettre le projet dans un repo GitHub **privé**.
2. Onglet **Actions** → **Build Windows EXE** → **Run workflow**.
3. Télécharger l'artefact `InstagramOps-windows` (contient `InstagramOps.exe`).

Le workflow est déjà prêt dans `.github/workflows/build-windows.yml`.

---

## Déploiement sur le VPS Windows

À côté de `InstagramOps.exe`, placer :

| Fichier | Rôle |
|---------|------|
| `proxies.json`, `swipe_proxies.json` | tes proxies |
| `config.json`, `panel_settings.json` | ta config |
| `accounts_created.json` | créé automatiquement si absent |

Puis installer **adb** sur le VPS : soit [platform-tools](https://developer.android.com/tools/releases/platform-tools)
ajouté au PATH, soit `adb.exe` posé à côté de l'exe.

Lancer `InstagramOps.exe`, puis ouvrir `http://localhost:5004`.

---

## Ce qui a été adapté pour Windows / la compilation

- Module `code.py` **renommé `insta_core.py`** (collision avec le module standard `code`).
- Chemins `/tmp/...` → `tempfile.gettempdir()` (portable).
- `ADB_PATH` auto-détecté (`adb.exe` dans le PATH sur Windows).
- `lsof/kill` (libération du port) désactivé sous Windows.
- Lecture de `panel.html` et données `.json` via des helpers compatibles binaire
  compilé (`_bundle_dir()` pour les ressources, `_app_dir()` pour les données).
- Le worker (1 process par compte) : en `.exe`, le binaire se relance via
  `InstagramOps.exe --worker <config>` au lieu de `python worker.py`.

## Notes de protection du code

- Nuitka compile en code machine → décompilation quasi impossible.
- ⚠️ Les **clés API restent extractibles** en clair (`strings InstagramOps.exe`).
  Acceptable ici car l'exe tourne sur **ton** VPS. Ne pas distribuer à des tiers
  sans déplacer les clés (backend proxy).
