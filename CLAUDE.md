# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the project

**CLI mode** (interactive terminal, 3 operation modes):
```bash
python insta_core.py
```

**Web panel** (Flask API + HTML control panel at `http://localhost:5004`):
```bash
python server.py
```

**Dependencies** (no requirements.txt — install manually):
```bash
pip install flask flask-cors requests
brew install android-platform-tools  # for adb
```

`ADB_PATH` is auto-detected per-OS in `insta_core.py` (`_default_adb_path()`): macOS/Linux common paths, `adb.exe` from PATH on Windows. Can be overridden via config (`system.adb_path`).

**Windows `.exe` build**: see [BUILD.md](BUILD.md). Compiled with Nuitka (`build_windows.bat` or the GitHub Actions workflow). The core module was renamed `code.py` → `insta_core.py` to avoid colliding with Python's stdlib `code` module during compilation.

## Architecture

The project has two runtime modes that share the same core logic:

- **`insta_core.py`** (formerly `code.py`) — 11 000+ lines, all core logic. Contains every function for phone control, Instagram automation, SMS providers, and account operations. Run directly for CLI mode.
- **`server.py`** — Flask wrapper that imports `insta_core.py` (`import insta_core as _tc`) and exposes its functions as REST endpoints. Runs workers in background threads. Uses SSE (`/api/logs/stream`) to push real-time logs to the frontend.
- **`panel.html`** — Single-file frontend served statically by Flask. Communicates exclusively through the REST API.

### Phone control layer (GeeLark)

All phone emulator operations go through `geelark_request()` which calls `https://openapi.geelark.com`. Credentials are hardcoded: `GEELARK_APP_ID` and `GEELARK_API_KEY`.

Key functions: `start_phone()`, `stop_phone()`, `get_all_phones()`, `wait_for_adb()`, `enable_adb()`, `restart_phone()`.

After a phone starts, the script connects via ADB (`adb connect {ip}:{port}`) then authenticates with `glogin {pwd}` (GeeLark's proprietary auth command).

### UI automation pattern

Every screen interaction follows the same pattern:
1. `adb shell uiautomator dump /sdcard/ui_NAME.xml` — dump the current screen XML
2. `adb shell cat /sdcard/ui_NAME.xml` — read it back
3. Parse with `re.findall()` — extract `bounds="[x1,y1][x2,y2]"` from matched elements
4. `adb shell input tap {cx} {cy}` — tap the center of the found bounds

The helper `_wait_and_tap(device, texts, wait_max, dump_file)` encapsulates this loop. `adb(device, command)` is the low-level wrapper around `subprocess.run`.

### Instagram account creation flow

Entry point: `open_instagram(device, photo_folder, city, lat, lon, phone_id)` in `code.py`.

Sequence of steps, each gated by `wait_next()` (which pauses in DEBUG mode):
1. `open_instagram_after_media()` — detect Instagram open; if `phone_id` is set, **restarts the phone** via `restart_phone()` and reconnects ADB before continuing
2. `insta_step_get_started()` — tap Get Started / Create new account
3. Phone number acquisition from SMS pool → enter on screen
4. `insta_step_create_password()` — default password is `"Alexis06"`
5. Birthday, name/username (`insta_step_name_and_flow()`)
6. Flow variants (A/B/search-visible) all converge on a common Skip + Got it before calling `save_created_account()`
7. Account saved to `accounts_created.json` with creation timestamp

### SMS provider fallback chain

`get_number_from_bower_v2()` is the primary number source (SMSBower). Fallbacks exist for Hero, SMSPin, PVAPins, SimAggregator. A background thread (`start_pool_scraper()`) pre-fetches numbers into a queue to avoid blocking during creation.

### Account age warning system

`accounts_created.json` (in the project root) stores every created account with timestamp. Functions `add_link_on_device()`, `add_bio_on_device()`, `post_story_on_device()`, `post_reel_on_device()` all call `check_account_age_warning(phone_id, action)` which blocks with an interactive `input()` prompt if the account is < 24h old.

### Threading model (CLI)

`_run_one_phone(idx, phone_id, phone_label)` is the per-phone worker function. Multiple threads (one per selected phone) run it in parallel with a 5-second stagger. `wait_next()` uses `DEBUG_MODE` to decide whether to block for user input between steps.

### Key hardcoded configuration

All API keys and paths are in `code.py` around lines 939–971:
- `GEELARK_APP_ID`, `GEELARK_API_KEY`
- `HERO_API_KEY`, `BOWER_API_KEY`, SMS provider keys
- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
- `ADB_PATH = "/opt/homebrew/bin/adb"`
- `ACCOUNTS_FILE` = `accounts_created.json` in the project directory

### Proxy configuration

- `proxies.json` — proxies used for Instagram account creation
- `swipe_proxies.json` — proxies used for swipe/engagement sessions

Both are managed through the web panel (`/api/proxies`, `/api/swipe/proxies`).
