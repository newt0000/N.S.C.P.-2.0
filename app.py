# app.py
import os
import shlex
import json
import time
from datetime import datetime, timedelta
from functools import wraps
import re
import sqlite3
import secrets
import subprocess
import shutil
import hashlib
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

import yaml
import psutil
import requests

from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify, flash, send_file, abort
)
EDITABLE_EXTS = {
    ".properties",
    ".yml",
    ".yaml",
    ".json",
    ".txt",
    ".md",
    ".log",
    ".cfg",
    ".ini",
    ".py",
    ".sh",
}

from apscheduler.schedulers.background import BackgroundScheduler

from server_manager import MinecraftServerManager

# -------------------------------------------------
# Paths
# -------------------------------------------------
APP_VERSION = "2.1.2"

CONFIG_PATH = "config.yml"
DATA_DIR = "data"
EULA_FILE = os.path.join(DATA_DIR, "eula.txt")
SCHEDULES_PATH = os.path.join(DATA_DIR, "schedules.json")
CONFIG_STATE_PATH = os.path.join(DATA_DIR, "config_state.json")
UUID_CACHE_PATH = os.path.join(DATA_DIR, "uuid_cache.json")
TEMPBANS_PATH = os.path.join(DATA_DIR, "tempbans.json")



os.makedirs(DATA_DIR, exist_ok=True)

# -------------------------------------------------
# Load Config
# -------------------------------------------------

with open(CONFIG_PATH, "r") as f:
    cfg = yaml.safe_load(f)

app = Flask(__name__)
app.secret_key = cfg["panel"]["secret_key"]
@app.context_processor
def inject_version():
    return {"APP_VERSION": APP_VERSION}

# Runtime mutable config
runtime_state = {
    "discord_webhook_url": cfg.get("discord", {}).get("webhook_url", ""),
    "auto_restart": cfg.get("minecraft", {}).get("auto_restart", True),
    "start_command": cfg["minecraft"]["start_command"],
    "server_mode": "vanilla",
    "server_jar": "",
    "java_runtime": "system",
    "server_owner_username": "",
    "server_owner_auto_op": False,
}

def available_server_jars():
    """List regular .jar files directly inside the server's versions directory."""
    versions = os.path.join(cfg["minecraft"]["server_dir"], "versions")
    try:
        return sorted((item.name for item in os.scandir(versions)
                       if item.is_file(follow_symlinks=False) and item.name.lower().endswith(".jar")), key=str.casefold)
    except OSError:
        return []


JAVA_ROOT = Path(__file__).resolve().parent / "data" / "java_runtimes"
JAVA_RELEASES = (17, 21, 25)

def java_executable(selection):
    if selection == "system":
        return shutil.which("java")
    if selection not in [str(v) for v in JAVA_RELEASES]:
        return None
    candidate = JAVA_ROOT / ("temurin-" + selection) / "bin" / "java"
    return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None

def java_version(path):
    if not path:
        return "Not installed"
    try:
        p = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=8)
        return (p.stderr or p.stdout).splitlines()[0][:160]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return "Unavailable"

def java_runtime_options():
    return [("system", "System Java", java_version(java_executable("system")))] + [
        (str(v), "Eclipse Temurin " + str(v), java_version(java_executable(str(v)))) for v in JAVA_RELEASES
    ]

def install_temurin(major, progress=None):
    progress = progress or (lambda **kw: None)
    """Download verified Temurin JDK into panel-owned data directory (no sudo)."""
    if major not in JAVA_RELEASES:
        raise ValueError("Unsupported Java release")
    import platform
    arch = {"x86_64": "x64", "aarch64": "aarch64"}.get(platform.machine().lower())
    if platform.system() != "Linux" or not arch:
        raise ValueError("Automatic installation supports Linux x86_64 and aarch64 only")
    endpoint = f"https://api.adoptium.net/v3/assets/latest/{major}/hotspot"
    progress(stage="Checking available releases", percent=2)
    resp = requests.get(endpoint, params={"architecture": arch, "image_type": "jdk", "os": "linux", "vendor": "eclipse"}, timeout=25)
    resp.raise_for_status()
    assets = resp.json()
    if not assets:
        raise ValueError("No matching Temurin release available")
    pkg = assets[0]["binary"]["package"]
    url, expected = pkg["link"], pkg["checksum"].lower()
    if not url.startswith("https://") or len(expected) != 64:
        raise ValueError("Invalid download metadata")
    JAVA_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="java-install-", dir=JAVA_ROOT) as temp:
        archive = Path(temp) / "jdk.tar.gz"
        digest = hashlib.sha256()
        progress(stage="Downloading Java", percent=5)
        with requests.get(url, stream=True, timeout=(15, 120)) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0)
            with archive.open("wb") as output:
                size = 0
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > 550 * 1024 * 1024:
                        raise ValueError("Java archive exceeds download limit")
                    digest.update(chunk)
                    output.write(chunk)
                    if total:
                        progress(stage="Downloading Java", percent=min(75, 5 + int(70 * size / total)), downloaded=size, total=total)
                    else:
                        progress(stage="Downloading Java", downloaded=size, total=0)
        progress(stage="Verifying checksum", percent=78)
        if digest.hexdigest() != expected:
            raise ValueError("Downloaded Java checksum mismatch")
        progress(stage="Extracting runtime", percent=83)
        unpack = Path(temp) / "unpacked"
        unpack.mkdir()
        with tarfile.open(archive, "r:gz") as tf:
            members = tf.getmembers()
            for member in members:
                p = Path(member.name)
                if p.is_absolute() or ".." in p.parts or not p.parts:
                    raise ValueError("Unsafe Java archive entry")
                if member.issym() or member.islnk():
                    # Verify the resolved link target remains inside extraction root.
                    # Symlink targets are relative to the link's parent; hardlinks
                    # are relative to the archive root.
                    import posixpath
                    target = member.linkname
                    if not target or target.startswith("/"):
                        raise ValueError("Unsafe Java archive link")
                    base = posixpath.dirname(member.name) if member.issym() else ""
                    resolved = posixpath.normpath(posixpath.join(base, target))
                    if resolved == ".." or resolved.startswith("../") or resolved.startswith("/"):
                        raise ValueError("Unsafe Java archive link")
                elif not (member.isdir() or member.isfile()):
                    raise ValueError("Unsafe Java archive entry")
            tf.extractall(unpack, filter="data")
        candidates = list(unpack.glob("*/bin/java"))
        if len(candidates) != 1:
            raise ValueError("Java executable not found in archive")
        source = candidates[0].parent.parent
        destination = JAVA_ROOT / f"temurin-{major}"
        staged = JAVA_ROOT / f".temurin-{major}-new"
        if staged.exists():
            shutil.rmtree(staged)
        shutil.move(str(source), str(staged))
        if not java_version(str(staged / "bin" / "java")).startswith('openjdk version'):
            shutil.rmtree(staged)
            raise ValueError("Downloaded runtime failed verification")
        progress(stage="Activating runtime", percent=95)
        if destination.exists():
            shutil.rmtree(destination)
        staged.rename(destination)

def resolved_start_command():
    command = runtime_state["start_command"]
    selection = runtime_state.get("java_runtime", "system")
    if selection != "system":
        executable = java_executable(selection)
        if not executable:
            raise ValueError("Selected Java runtime is not installed")
        # Replace only the java executable token, not arbitrary arguments.
        command, count = re.subn(r"(?<!\S)java(?=\s)", lambda _: shlex.quote(executable), command, count=1)
        if not count:
            raise ValueError("Startup command must contain the 'java' executable")
    if "%srvjar%" not in command:
        return command
    selected = runtime_state.get("server_jar", "")
    if selected not in available_server_jars():
        raise ValueError("Selected server JAR is missing from the versions directory. Choose a version in Settings.")
    # Shell-quote the filename, including spaces and special characters.
    return command.replace("%srvjar%", shlex.quote(os.path.join("versions", selected)))


def sync_start_command():
    server_mgr.start_command = resolved_start_command()

PLUGINS_DIR = os.path.join(cfg["minecraft"]["server_dir"], "plugins")
MODS_DIR = os.path.join(cfg["minecraft"]["server_dir"], "mods")
USERS_DB = os.path.join(DATA_DIR, "users.sqlite3")
PERMISSIONS = {"dashboard":"Dashboard", "console":"Console and server controls", "backups":"Backups", "schedule":"Scheduler", "properties":"Properties", "players":"Players", "bans":"Bans", "extensions":"Plugins / Mods", "files":"File Manager", "settings":"Settings"}
ROUTE_PERMISSIONS = {"dashboard":"dashboard", "console":"console", "api_console_logs":"console", "api_console_clear":"console", "api_console_send":"console", "api_server_start":"console", "api_server_stop":"console", "api_server_restart":"console", "api_stats":"dashboard", "api_players_online":"players", "api_players_history":"players", "players_page":"players", "bans_page":"bans", "plugins_page":"extensions", "plugins_toggle":"extensions", "mods_page":"extensions", "mods_toggle":"extensions", "files_edit":"files", "files_page":"files", "files_download":"files", "files_delete":"files", "files_rename":"files", "backups_page":"backups", "run_backup":"backups", "schedule_page":"schedule", "delete_schedule":"schedule", "run_schedule_now":"schedule", "properties_page":"properties", "settings_page":"settings"}

def db():
    con=sqlite3.connect(USERS_DB, timeout=10)
    con.row_factory=sqlite3.Row
    return con

def init_users():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL COLLATE NOCASE, password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, permissions TEXT NOT NULL DEFAULT '[]', auth_version TEXT NOT NULL)")
        if not con.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            con.execute("INSERT INTO users(username,password_hash,is_admin,permissions,auth_version) VALUES (?,?,?,?,?)", ("admin",generate_password_hash(cfg["panel"]["admin_password"]),1,"[]",secrets.token_hex(16)))

init_users()

def current_user():
    uid=session.get("user_id")
    if not uid: return None
    with db() as con: user=con.execute("SELECT * FROM users WHERE id=?",(uid,)).fetchone()
    if not user or session.get("auth_version") != user["auth_version"]: return None
    return dict(user)

def allowed(permission):
    user=current_user()
    return bool(user and (user["is_admin"] or permission in json.loads(user["permissions"])))

@app.context_processor
def inject_access():
    return {"can":allowed, "current_user":current_user(), "server_mode":runtime_state.get("server_mode","vanilla")}

# -------------------------------------------------
# Server Manager
# -------------------------------------------------

server_mgr = MinecraftServerManager(
    start_command=runtime_state["start_command"],
    server_dir=cfg["minecraft"]["server_dir"],
    auto_restart=runtime_state["auto_restart"]
)

# -------------------------------------------------
# Authentication
# -------------------------------------------------

# Load saved settings at startup, including the selected server mode.
try:
    with open(CONFIG_STATE_PATH) as state_file:
        saved_state=json.load(state_file)
    for key in runtime_state:
        if key in saved_state: runtime_state[key]=saved_state[key]
except (OSError, ValueError): pass

# Normalize startup configuration after both YAML defaults and saved settings load.
# MinecraftServerManager already sets cwd=server_dir; an explicit cd is unnecessary.
def repair_startup_configuration():
    import shlex as _shlex
    import re as _re
    default_command = "java -Xms2G -Xmx14G -jar %srvjar% nogui"
    command = runtime_state.get("start_command")
    changed = False
    if not isinstance(command, str) or not command.strip() or "PATH-TO-SERVER-DIRECTORY" in command:
        command = default_command
        changed = True
    else:
        # Remove only an obsolete cd prefix pointing to a missing directory.
        # Existing valid custom commands remain untouched.
        match = _re.match(r"^\s*cd\s+(.+?)\s*&&\s*(.+)$", command, _re.S)
        if match:
            try:
                parts = _shlex.split(match.group(1))
                old_dir = parts[0] if len(parts) == 1 else None
            except ValueError:
                old_dir = None
            if old_dir and not os.path.isdir(old_dir):
                command = match.group(2).strip()
                changed = True
    if not changed:
        return
    runtime_state["start_command"] = command
    cfg["minecraft"]["start_command"] = command
    # Write both files atomically and preserve all unrelated settings.
    # Do not overwrite a config.yml that cannot be safely parsed.
    for path, obj, dumper in (
        (CONFIG_PATH, cfg, lambda v: yaml.safe_dump(v, sort_keys=False)),
        (CONFIG_STATE_PATH, runtime_state, lambda v: json.dumps(v, indent=2)),
    ):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(dumper(obj))
            os.replace(tmp, path)
        except OSError as exc:
            app.logger.warning("Could not update %s: %s", path, exc)
            try:
                os.unlink(tmp)
            except OSError:
                pass

repair_startup_configuration()

def whitelist_server_owner():
    """Called once when the Minecraft console reports a completed startup."""
    owner = str(runtime_state.get("server_owner_username", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", owner):
        return
    if server_mgr.send_command(f"whitelist add {owner}"):
        app.logger.info("Owner whitelist command sent for %s", owner)
    if runtime_state.get("server_owner_auto_op"):
        server_mgr.send_command(f"op {owner}")

server_mgr.on_server_ready = whitelist_server_owner

# Apply persisted settings before any start or scheduled operation.
server_mgr.auto_restart = runtime_state["auto_restart"]
try:
    sync_start_command()
except ValueError:
    server_mgr.start_command = ""  # Invalid selection must never launch an unintended JAR.


def license_ok():
    # Either globally accepted (eula.txt) OR accepted in this session
    return is_license_globally_accepted() or session.get("license_accepted")


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # gate everything except login/license routes (which don't use this decorator)
        if not license_ok():
            return redirect(url_for("license_page"))
        if not current_user():
            session.clear()
            return redirect(url_for("login"))
        permission=ROUTE_PERMISSIONS.get(request.endpoint)
        if not permission and request.endpoint not in ("logout","license_page","license_accept","users_page"):
            permission=next((v for k,v in (("/api/server/","console"),( "/api/console/","console"),("/schedule","schedule"),("/backups","backups"),("/files","files"),("/players","players"),("/bans","bans")) if request.path.startswith(k)), "dashboard")
        if permission and not allowed(permission): abort(403)
        return f(*args, **kwargs)
    return wrapper





# -------------------------------------------------
# Helpers
# -------------------------------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default

def load_tempbans():
    return load_json(TEMPBANS_PATH, [])

def save_tempbans(items):
    save_json(TEMPBANS_PATH, items)
def get_banned_players():
    path = os.path.join(cfg["minecraft"]["server_dir"], "banned-players.json")
    data = load_json(path, [])
    # Normalize
    out = []
    if isinstance(data, list):
        for e in data:
            if not isinstance(e, dict):
                continue
            out.append({
                "name": e.get("name") or "unknown",
                "uuid": e.get("uuid"),
                "reason": e.get("reason") or "",
                "created": e.get("created") or "",
                "expires": e.get("expires") or "forever",
                "source": e.get("source") or "",
            })
    return out

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
def resolve_uuid_to_name(uuid_str: str) -> str | None:
    """
    Resolve a player UUID (with or without dashes) to the current username
    using Mojang's sessionserver API. Results are cached in uuid_cache.json.
    """
    # Normalise UUID key
    key = uuid_str.lower()
    key_nodash = key.replace("-", "")

    cache = load_json(UUID_CACHE_PATH, {})

    # Cached result (may be None if previously failed)
    if key in cache:
        return cache[key] or None

    try:
        resp = requests.get(
            f"https://sessionserver.mojang.com/session/minecraft/profile/{key_nodash}",
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            name = data.get("name")
            cache[key] = name
            save_json(UUID_CACHE_PATH, cache)
            return name
        else:
            # Cache negative result to avoid hammering
            cache[key] = None
            save_json(UUID_CACHE_PATH, cache)
            return None
    except Exception:
        # Don't kill the panel if Mojang is unreachable
        return None

def is_license_globally_accepted() -> bool:
    """
    Returns True if eula.txt contains 'accepted=true' (case-insensitive).
    Missing file or any other value -> False.
    """
    try:
        with open(EULA_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().lower()
                if line.startswith("accepted="):
                    value = line.split("=", 1)[1].strip()
                    return value == "true"
    except FileNotFoundError:
        return False
    except Exception:
        return False
    return False


def set_license_accepted_flag():
    """
    Write accepted=true and a timestamp to eula.txt.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(EULA_FILE, "w", encoding="utf-8") as f:
        f.write("accepted=true\n")
        f.write(f"timestamp={datetime.utcnow().isoformat()}Z\n")


def send_discord_embed(title, description, color=0xff00ff):
    url = runtime_state.get("discord_webhook_url")
    if not url:
        return

    payload = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "timestamp": datetime.utcnow().isoformat()
            }
        ]
    }

    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        # Don't break panel if Discord is down
        pass


def get_ops():
    """Load current operator list from ops.json (if present)."""
    ops_path = os.path.join(cfg["minecraft"]["server_dir"], "ops.json")
    data = load_json(ops_path, [])
    norm = []
    for entry in data:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("uuid") or "unknown"
            level = entry.get("level", 0)
            norm.append({
                "name": name,
                "level": level,
                "raw": entry,
            })
    return norm

def safe_join_server_dir(rel_path: str) -> str:
    """
    Safely join a relative path to the Minecraft server directory.
    Prevents path traversal outside server_dir.
    """
    base = os.path.abspath(cfg["minecraft"]["server_dir"])
    rel_path = rel_path or ""
    candidate = os.path.normpath(os.path.join(base, rel_path))
    if not candidate.startswith(base):
        abort(400)  # invalid path
    return candidate

def get_known_players():
    """Combine recent players, usercache, and playerdata into a single list.

    Returns a list of dicts:
      {name, last_seen, last_seen_human, source, is_op, avatar_url, uuid?}
    """
    players: dict[str, dict] = {}

    # 1) Recent players from our in-memory tracker (logs)
    from_tracker = server_mgr.get_recent_players(limit=1000)
    for p in from_tracker:
        name = p.get("name")
        if not name:
            continue
        ts = p.get("last_seen")
        players[name] = {
            "name": name,
            "last_seen": ts,
            "source": "logs",
        }

    server_dir = cfg["minecraft"]["server_dir"]

    # 2) usercache / usernamecache on disk (contains name + uuid)
    for fname in ("usercache.json", "usernamecache.json"):
        path = os.path.join(server_dir, fname)
        if not os.path.exists(path):
            continue
        data = load_json(path, [])
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name") or entry.get("username")
                if not name:
                    continue
                info = players.setdefault(name, {
                    "name": name,
                    "last_seen": None,
                    "source": "cache",
                })
                # Prefer logs if we have them; otherwise mark as cache
                if info.get("source") != "logs":
                    info["source"] = "cache"
                if entry.get("expiresOn"):
                    info["expires_on"] = entry["expiresOn"]
                if entry.get("uuid"):
                    info["uuid"] = entry["uuid"]

    # 3) playerdata folder (UUIDs only, used as a fallback if no name info)
    world_dir = cfg["backups"]["world_dir"]
    playerdata_dir = os.path.join(world_dir, "playerdata")
    if os.path.isdir(playerdata_dir):
        for fname in os.listdir(playerdata_dir):
            if not fname.endswith(".dat"):
                continue
            uuid = fname[:-4]
            # Only add if we don't already have a record for this UUID/name key
            # (we may reconcile later by name)
            if uuid not in players:
                players[uuid] = {
                    "name": uuid,          # temporary; may be a UUID
                    "uuid": uuid,
                    "last_seen": None,
                    "source": "playerdata",
                }

    # 4) Translate UUID-looking names → usernames via Mojang API
    uuid_pattern = re.compile(
        r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
    )
    for info in players.values():
        name = info.get("name", "")
        if uuid_pattern.match(name):
            resolved = resolve_uuid_to_name(name)
            if resolved:
                info["uuid"] = name
                info["name"] = resolved
                # from UI POV this is still just "playerdata"
                if info.get("source") == "playerdata":
                    info["source"] = "playerdata"

    # 5) Merge duplicates by final name, combine sources + last_seen + uuid
    merged: dict[str, dict] = {}
    for info in players.values():
        name = info.get("name") or "unknown"
        src = info.get("source") or "unknown"
        ts = info.get("last_seen")
        uuid_val = info.get("uuid")

        if name in merged:
            existing = merged[name]

            # last_seen: keep the most recent timestamp
            old_ts = existing.get("last_seen")
            if ts and (not old_ts or ts > old_ts):
                existing["last_seen"] = ts

            # combine source strings into "cache & playerdata" style
            old_sources = set(
                s.strip()
                for s in str(existing.get("source", "")).split("&")
                if s.strip()
            )
            new_sources = set(
                s.strip()
                for s in str(src).split("&")
                if s.strip()
            )
            combined_sources = sorted(old_sources.union(new_sources))
            existing["source"] = " & ".join(combined_sources) if combined_sources else "unknown"

            # keep uuid if not already present
            if not existing.get("uuid") and uuid_val:
                existing["uuid"] = uuid_val

        else:
            merged[name] = dict(info)  # shallow copy

    # 6) Mark operators + last_seen_human + avatar_url
    ops = get_ops()
    op_names = {op["name"] for op in ops}

    for info in merged.values():
        info["is_op"] = info["name"] in op_names

        ts = info.get("last_seen")
        if ts:
            info["last_seen_human"] = datetime.utcfromtimestamp(ts).strftime(
                "%Y-%m-%d %H:%M:%S UTC"
            )
        else:
            info["last_seen_human"] = "Unknown"

        # Head image: use username. If head doesn't exist, we'll fall back to Steve via onerror in the template.
        info["avatar_url"] = f"https://minotar.net/avatar/{info['name']}/32"

    # Return sorted by (resolved) name
    return sorted(merged.values(), key=lambda p: p["name"].lower())




# -------------------------------------------------
# Schedules + APScheduler
# -------------------------------------------------

def get_schedules():
    return load_json(SCHEDULES_PATH, [])


def set_schedules(schedules):
    save_json(SCHEDULES_PATH, schedules)


def scheduler_tick():
    schedules = get_schedules()
    # Use local server time, not UTC, to match what you enter in the form
    now = datetime.now()
    changed = False

    for sched in schedules:
        if not sched.get("enabled", True):
            continue

        stype = sched.get("type", "once")
        last_run_str = sched.get("last_run_iso")
        last_run = datetime.fromisoformat(last_run_str) if last_run_str else None

        if stype == "once":
            run_at = datetime.fromisoformat(sched["run_at_iso"])
            if run_at <= now and not sched.get("ran", False):
                if execute_schedule(sched):
                    sched["ran"] = True
                    changed = True

        elif stype == "daily":
            hh, mm = map(int, sched["time_str"].split(":"))
            today_run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if today_run <= now and (not last_run or last_run < today_run):
                if execute_schedule(sched):
                    sched["last_run_iso"] = now.isoformat()
                    changed = True

        elif stype == "interval":
            minutes = int(sched.get("interval_minutes", 60))
            if not last_run or (now - last_run) >= timedelta(minutes=minutes):
                if execute_schedule(sched):
                    sched["last_run_iso"] = now.isoformat()
                    changed = True

    if changed:
        set_schedules(schedules)
    # NEW: temp-ban expiration processing
    process_tempbans()

def process_tempbans():
    items = load_tempbans()
    if not items:
        return

    now = datetime.utcnow()
    keep = []
    for tb in items:
        try:
            expires = datetime.fromisoformat(tb["expires_at_iso"].replace("Z", ""))
        except Exception:
            keep.append(tb)
            continue

        if now >= expires:
            name = tb.get("name")
            if name:
                server_mgr.send_command(f"pardon {name}")
                send_discord_embed("Temp-ban expired", f"Unbanned `{name}` (temp-ban expired).", color=0x00ffff)
        else:
            keep.append(tb)

    if keep != items:
        save_tempbans(keep)

def execute_schedule(sched: dict) -> bool:
    """
    Execute a single schedule object.
    Supports modes: command (default), backup, restart.
    Returns True if something was executed successfully.
    """
    mode = sched.get("mode", "command")

    if mode == "backup":
        ok = run_backup_task()
        if ok:
            send_discord_embed(
                "Scheduled backup executed",
                "A scheduled world backup has completed."
            )
        return ok

    if mode == "restart":
        # simple restart: stop then start
        send_discord_embed(
            "Scheduled restart",
            "Scheduled server restart has been triggered."
        )
        # Don't block forever; this is in the scheduler thread
        server_mgr.stop()
        # small delay before restart
        time.sleep(5)
        return server_mgr.start()

    # Default: execute a console command
    cmd = (sched.get("command") or "").strip()
    if not cmd:
        return False

    ok = server_mgr.send_command(cmd)
    if ok:
        send_discord_embed(
            "Scheduled command executed",
            f"`{cmd}` ran by scheduler."
        )
    return ok

def run_backup_task() -> bool:
    """Run a world backup without any Flask flashing/redirects (for scheduler)."""
    backup_dir = cfg["backups"]["backup_dir"]
    world_dir = cfg["backups"]["world_dir"]

    os.makedirs(backup_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"world-backup-{ts}.tar.gz"
    out_path = os.path.join(backup_dir, name)

    import tarfile
    try:
        with tarfile.open(out_path, "w:gz") as tar:
            tar.add(world_dir, arcname=os.path.basename(world_dir))
    except Exception as e:
        send_discord_embed("Backup Failed", f"Error creating backup: `{e}`", color=0xff0000)
        return False

    # Cleanup old backups
    keep_last = int(cfg["backups"].get("keep_last", 10))
    files = sorted(
        [f for f in os.listdir(backup_dir) if f.endswith(".tar.gz")],
        reverse=True
    )
    for f in files[keep_last:]:
        os.remove(os.path.join(backup_dir, f))

    send_discord_embed("Backup Completed", f"Created backup `{name}`.")
    return True
  


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(
    scheduler_tick,
    "interval",
    seconds=cfg.get("scheduler", {}).get("tick_seconds", 30)
)
scheduler.start()


# -------------------------------------------------
# Routes: Authentication
# -------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username=request.form.get("username", "").strip()
        password=request.form.get("password", "")
        with db() as con: user=con.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"]=user["id"]
            session["auth_version"]=user["auth_version"]
            session["logged_in"]=True
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html", title="Login")

@app.route("/users", methods=["GET", "POST"])
@login_required
def users_page():
    if not current_user()["is_admin"]: abort(403)
    if request.method == "POST":
        action=request.form.get("action")
        username=request.form.get("username", "").strip()
        password=request.form.get("password", "")
        permissions=[x for x in request.form.getlist("permissions") if x in PERMISSIONS]
        target=request.form.get("user_id", type=int)
        try:
            with db() as con:
                if action=="admin_credentials":
                    me = con.execute("SELECT * FROM users WHERE id=?", (current_user()["id"],)).fetchone()
                    old_password = request.form.get("current_password", "")
                    new_password = request.form.get("new_password", "")
                    new_username = request.form.get("admin_username", "").strip()
                    if not me or not me["is_admin"] or not check_password_hash(me["password_hash"], old_password):
                        raise ValueError("Current administrator password is incorrect")
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", new_username):
                        raise ValueError("Username must be 3-32 characters (letters, numbers, _, . or -)")
                    if new_password and len(new_password) < 8:
                        raise ValueError("New password must be at least 8 characters")
                    if new_password != request.form.get("confirm_password", ""):
                        raise ValueError("New passwords do not match")
                    new_version = secrets.token_hex(16)
                    con.execute("UPDATE users SET username=?, password_hash=?, auth_version=? WHERE id=?",
                        (new_username, generate_password_hash(new_password) if new_password else me["password_hash"], new_version, me["id"]))
                    session["auth_version"] = new_version
                elif action=="create":
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}",username) or len(password)<8: raise ValueError("Username must be 3-32 characters (letters, numbers, _, . or -) and password at least 8 characters.")
                    con.execute("INSERT INTO users(username,password_hash,is_admin,permissions,auth_version) VALUES (?,?,?,?,?)",(username,generate_password_hash(password),int(request.form.get("is_admin")=="on"),json.dumps(permissions),secrets.token_hex(16)))
                elif action in ("edit","delete"):
                    row=con.execute("SELECT * FROM users WHERE id=?",(target,)).fetchone()
                    if not row: raise ValueError("User not found")
                    if action=="delete":
                        if row["id"]==current_user()["id"]: raise ValueError("Cannot delete your own account")
                        if row["is_admin"] and con.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]<=1: raise ValueError("Cannot delete the last admin")
                        con.execute("DELETE FROM users WHERE id=?",(target,))
                    else:
                        admin=int(request.form.get("is_admin")=="on")
                        if row["is_admin"] and not admin and con.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0]<=1: raise ValueError("Cannot demote the last admin")
                        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}",username): raise ValueError("Invalid username")
                        if password and len(password)<8: raise ValueError("Password must have at least 8 characters")
                        hash_value=generate_password_hash(password) if password else row["password_hash"]
                        version=secrets.token_hex(16) if password or admin!=row["is_admin"] or json.dumps(permissions)!=row["permissions"] else row["auth_version"]
                        con.execute("UPDATE users SET username=?,password_hash=?,is_admin=?,permissions=?,auth_version=? WHERE id=?",(username,hash_value,admin,json.dumps(permissions),version,target))
                        if target==current_user()["id"]: session["auth_version"]=version
                else: raise ValueError("Unknown action")
            flash("User management changes saved.","success")
        except (ValueError,sqlite3.IntegrityError) as exc: flash(str(exc),"error")
        return redirect(url_for("users_page"))
    with db() as con: users=[dict(x) for x in con.execute("SELECT * FROM users ORDER BY username")]
    for user in users: user["grants"]=json.loads(user["permissions"])
    return render_template("users.html",title="User Management",users=users,permissions=PERMISSIONS)

@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


# -------------------------------------------------
# Routes: Main Pages
# -------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    stats = server_mgr.get_stats()
    return render_template("dashboard.html", stats=stats, title="Dashboard")


@app.route("/console")
@login_required
def console():
    return render_template("console.html", title="Console")


# -------------------------------------------------
# API: Console
# -------------------------------------------------

@app.route("/api/console/logs")
@login_required
def api_console_logs():
    last_id = int(request.args.get("last_id", 0))
    return jsonify({"logs": server_mgr.get_logs_since(last_id)})


@app.route("/api/console/clear", methods=["POST"])
@login_required
def api_console_clear():
    return jsonify({"ok": True, "last_id": server_mgr.clear_console()})


@app.route("/api/console/send", methods=["POST"])
@login_required
def api_console_send():
    data = request.get_json()
    cmd = data.get("command", "").strip()
    if not cmd:
        return jsonify({"ok": False, "error": "Empty command"}), 400

    ok = server_mgr.send_command(cmd)
    return jsonify({"ok": ok})


# -------------------------------------------------
# API: Server Control
# -------------------------------------------------

@app.route("/api/server/start", methods=["POST"])
@login_required
def api_server_start():
    ok = server_mgr.start()
    if ok:
        send_discord_embed("Server Started", "Minecraft server started.")
    return jsonify({"ok": ok})


@app.route("/api/server/stop", methods=["POST"])
@login_required
def api_server_stop():
    ok = server_mgr.stop()
    if ok:
        send_discord_embed("Server Stopped", "Minecraft server stopped.")
    return jsonify({"ok": ok})
@app.route("/api/server/restart", methods=["POST"])
@login_required
def api_server_restart():
    ok = server_mgr.restart()
    if ok:
        send_discord_embed("Server Restarted", "Minecraft server restarted.")
    return jsonify({"ok": ok})


@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(server_mgr.get_stats())


# -------------------------------------------------
# API: Player Tracking
# -------------------------------------------------

@app.route("/api/players/online")
@login_required
def api_players_online():
    players = server_mgr.get_online_players()
    for p in players:
        if p["last_seen"]:
            p["last_seen_iso"] = datetime.utcfromtimestamp(p["last_seen"]).isoformat() + "Z"
        else:
            p["last_seen_iso"] = None
    return jsonify({"players": players})


@app.route("/api/players/history")
@login_required
def api_players_history():
    players = server_mgr.get_recent_players(limit=30)
    for p in players:
        if p["last_seen"]:
            p["last_seen_iso"] = datetime.utcfromtimestamp(p["last_seen"]).isoformat() + "Z"
        else:
            p["last_seen_iso"] = None
    return jsonify({"players": players})


# -------------------------------------------------
# Player Management Page
# -------------------------------------------------

@app.route("/players", methods=["GET", "POST"])
@login_required
def players_page():
    if request.method == "POST":
        action = request.form.get("action")
        name = (request.form.get("player_name") or "").strip()

        if not name or not action:
            flash("Please provide a player name and action.", "error")
            return redirect(url_for("players_page"))

        cmd_map = {
            "op": f"op {name}",
            "deop": f"deop {name}",
            "whitelist_add": f"whitelist add {name}",
            "whitelist_remove": f"whitelist remove {name}",
        }
        cmd = cmd_map.get(action)
        if not cmd:
            flash("Unknown action.", "error")
            return redirect(url_for("players_page"))

        ok = server_mgr.send_command(cmd)
        if ok:
            flash(f"Sent command: {cmd}", "success")
        else:
            flash("Server is not running or command could not be sent.", "error")
        return redirect(url_for("players_page"))

    ops = get_ops()
    players = get_known_players()
    return render_template(
        "players.html",
        title="Player Management",
        ops=ops,
        players=players,
    )
    
    
# -------------------------------------------------
# EULA & DMCA Agreement
# -------------------------------------------------
@app.route("/license", methods=["GET", "POST"])
def license_page():
    # If globally accepted already, just mark this session and move on
    if is_license_globally_accepted():
        session["license_accepted"] = True
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        accepted = request.form.get("accept") == "on"
        if not accepted:
            flash("You must accept the license to continue.", "error")
            return render_template("license.html", title="License")

        # Persist acceptance + mark this session
        set_license_accepted_flag()
        session["license_accepted"] = True
        flash("Thank you for accepting the license.", "success")
        return redirect(url_for("dashboard"))

    return render_template("license.html", title="License")



@app.route("/license/accept", methods=["POST"])
@login_required
def license_accept():
    if request.form.get("accept") == "yes":
        session["license_accepted"] = True
        flash("License accepted.", "success")
        return redirect(url_for("dashboard"))
    flash("You must accept the license to continue.", "error")
    return redirect(url_for("license_page"))

@app.before_request
def enforce_license_acceptance():
    # Endpoints that should be allowed *without* license acceptance
    exempt_endpoints = {
        "static",          # static files
        "license_page",    # the license screen itself
        "login",           # login page
        "logout",          # let them log out if needed
    }

    # Sometimes request.endpoint can be None (e.g. 404), so guard for that
    if request.endpoint in exempt_endpoints or request.endpoint is None:
        return

    # If user hasn't accepted license, always send them to /license
    if not session.get("license_accepted"):
        return redirect(url_for("license_page"))
#---------------------------
#     bans page actions
#---------------------------
@app.route("/bans", methods=["GET", "POST"])
@login_required
def bans_page():
    if request.method == "POST":
        action = request.form.get("action")
        name = (request.form.get("player_name") or "").strip()
        reason = (request.form.get("reason") or "").strip()
        duration_minutes = (request.form.get("duration_minutes") or "").strip()

        if action == "ban":
            if not name:
                flash("Select a player to ban.", "error")
                return redirect(url_for("bans_page"))

            # Apply ban (reason optional)
            cmd = f"ban {name} {reason}".strip()
            ok = server_mgr.send_command(cmd)
            if not ok:
                flash("Could not send ban command (server not running?).", "error")
                return redirect(url_for("bans_page"))

            # Optional temp-ban
            if duration_minutes:
                try:
                    mins = int(duration_minutes)
                    if mins > 0:
                        expires = datetime.utcnow() + timedelta(minutes=mins)
                        items = load_tempbans()
                        # replace existing tempban record for same player
                        items = [i for i in items if i.get("name") != name]
                        items.append({
                            "name": name,
                            "reason": reason,
                            "created_at_iso": datetime.utcnow().isoformat() + "Z",
                            "expires_at_iso": expires.isoformat() + "Z",
                        })
                        save_tempbans(items)
                except Exception:
                    flash("Temp-ban duration must be a whole number of minutes.", "error")

            flash(f"Banned {name}.", "success")
            return redirect(url_for("bans_page"))

        elif action == "unban":
            if not name:
                flash("Select a player to unban.", "error")
                return redirect(url_for("bans_page"))

            ok = server_mgr.send_command(f"pardon {name}")
            if ok:
                # remove any tempban record
                items = [i for i in load_tempbans() if i.get("name") != name]
                save_tempbans(items)
                flash(f"Unbanned {name}.", "success")
            else:
                flash("Could not send unban command (server not running?).", "error")
            return redirect(url_for("bans_page"))

    banned = get_banned_players()
    known_players = get_known_players()  # your existing function
    tempbans = load_tempbans()
    temp_index = {t["name"]: t for t in tempbans if "name" in t}

    # attach temp-ban expiry (if tracked)
    for b in banned:
        tb = temp_index.get(b["name"])
        if tb:
            b["temp_expires_at"] = tb.get("expires_at_iso")

    return render_template(
        "bans.html",
        title="Bans",
        banned=banned,
        players=known_players,
    )

# -------------------------------------------------
# Plugin Manager
# -------------------------------------------------

def extension_listing(mode):
    directory=MODS_DIR if mode=="modded" else PLUGINS_DIR
    os.makedirs(directory,exist_ok=True)
    entries=[]
    for entry in os.scandir(directory):
        if entry.is_file() and (entry.name.endswith(".jar") or entry.name.endswith(".jar.disabled")):
            name=entry.name
            entries.append({"file":name,"plugin":name.removesuffix(".disabled").removesuffix(".jar"),"enabled":name.endswith(".jar")})
    return sorted(entries,key=lambda x:x["plugin"].lower())

def extension_toggle(mode, endpoint):
    filename=request.form.get("file","")
    directory=MODS_DIR if mode=="modded" else PLUGINS_DIR
    if not filename or filename!=os.path.basename(filename) or not (filename.endswith(".jar") or filename.endswith(".jar.disabled")):
        abort(400)
    source=os.path.join(directory,filename)
    target=os.path.join(directory,filename.removesuffix(".disabled") if filename.endswith(".jar.disabled") else filename+".disabled")
    if not os.path.isfile(source): abort(404)
    if os.path.exists(target): abort(409)
    os.rename(source,target)
    flash("Extension enabled." if target.endswith(".jar") else "Extension disabled.","success")
    return redirect(url_for(endpoint))

@app.route("/plugins")
@login_required
def plugins_page():
    if runtime_state.get("server_mode")!="vanilla": return redirect(url_for("mods_page"))
    return render_template("plugins.html",title="Plugins",plugins=extension_listing("vanilla"),kind="Plugin",toggle_endpoint="plugins_toggle")

@app.route("/plugins/toggle",methods=["POST"])
@login_required
def plugins_toggle():
    if runtime_state.get("server_mode")!="vanilla": abort(403)
    return extension_toggle("vanilla","plugins_page")

@app.route("/mods")
@login_required
def mods_page():
    if runtime_state.get("server_mode")!="modded": return redirect(url_for("plugins_page"))
    return render_template("plugins.html",title="Mods",plugins=extension_listing("modded"),kind="Mod",toggle_endpoint="mods_toggle")

@app.route("/mods/toggle",methods=["POST"])
@login_required
def mods_toggle():
    if runtime_state.get("server_mode")!="modded": abort(403)
    return extension_toggle("modded","mods_page")

# -------------------------------------------------
# File Manager
# -------------------------------------------------
@app.route("/files/edit", methods=["GET", "POST"])
@login_required
def files_edit():
    rel_path = request.values.get("path", "")
    abs_path = safe_join_server_dir(rel_path)

    # Only allow editing of text-like files
    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in EDITABLE_EXTS:
        flash("This file type is not editable from the panel.", "error")
        # Go back to the directory listing
        base_dir = cfg["minecraft"]["server_dir"]
        parent_rel = os.path.relpath(os.path.dirname(abs_path), base_dir)
        if parent_rel == ".":
            parent_rel = ""
        return redirect(url_for("files_page", path=parent_rel))

    if not os.path.isfile(abs_path):
        flash("File not found.", "error")
        return redirect(url_for("files_page"))

    base_dir = cfg["minecraft"]["server_dir"]
    parent_rel = os.path.relpath(os.path.dirname(abs_path), base_dir)
    if parent_rel == ".":
        parent_rel = ""

    if request.method == "POST":
        new_content = request.form.get("content", "")
        try:
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            flash("File saved successfully.", "success")
        except Exception as e:
            flash(f"Error saving file: {e}", "error")
        # After saving, stay on editor page
        return redirect(url_for("files_edit", path=rel_path))

    # GET: load current file contents
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        flash(f"Error reading file: {e}", "error")
        content = ""

    filename = os.path.basename(abs_path)

    return render_template(
        "file_edit.html",
        title=f"Edit: {filename}",
        rel_path=rel_path,
        parent_rel=parent_rel,
        filename=filename,
        content=content,
    )

@app.route("/files", methods=["GET", "POST"])
@login_required
def files_page():
    rel_path = request.args.get("path", "")
    current_abs = safe_join_server_dir(rel_path)

    # Handle upload
    if request.method == "POST":
        file = request.files.get("file")
        if file and file.filename:
            dest = os.path.join(current_abs, file.filename)
            file.save(dest)
            flash(f"Uploaded {file.filename}", "success")
        else:
            flash("No file selected for upload.", "error")
        return redirect(url_for("files_page", path=rel_path))

    # Build directory listing
    entries = []
    try:
        with os.scandir(current_abs) as it:
            for entry in it:
                is_dir = entry.is_dir()
                name = entry.name
                size = entry.stat().st_size
                mtime = datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")

                ext = os.path.splitext(name)[1].lower()
                can_edit = (not is_dir) and (ext in EDITABLE_EXTS)

                entries.append({
                    "name": name,
                    "is_dir": is_dir,
                    "size": size,
                    "mtime": mtime,
                    "can_edit": can_edit,
                })
    except FileNotFoundError:
        flash("Directory not found.", "error")
        return redirect(url_for("files_page"))

    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))

    # Parent path
    base = os.path.abspath(cfg["minecraft"]["server_dir"])
    parent_rel = ""
    if os.path.abspath(current_abs) != base:
        parent_rel = os.path.relpath(os.path.dirname(current_abs), base)
        if parent_rel == ".":
            parent_rel = ""

    return render_template(
        "files.html",
        title="File Manager",
        entries=entries,
        rel_path=rel_path,
        parent_rel=parent_rel
    )



@app.route("/files/download")
@login_required
def files_download():
    rel_path = request.args.get("path", "")
    abs_path = safe_join_server_dir(rel_path)
    if not os.path.isfile(abs_path):
        flash("File not found.", "error")
        return redirect(url_for("files_page"))
    return send_file(abs_path, as_attachment=True)


@app.route("/files/delete", methods=["POST"])
@login_required
def files_delete():
    rel_path = request.form.get("path", "")
    abs_path = safe_join_server_dir(rel_path)
    base_dir = cfg["minecraft"]["server_dir"]
    if os.path.isdir(abs_path):
        flash("Deleting directories is disabled from this panel.", "error")
    else:
        try:
            os.remove(abs_path)
            flash("File deleted.", "success")
        except Exception as e:
            flash(f"Failed to delete file: {e}", "error")
    parent_rel = os.path.relpath(os.path.dirname(abs_path), base_dir)
    if parent_rel == ".":
        parent_rel = ""
    return redirect(url_for("files_page", path=parent_rel))


@app.route("/files/rename", methods=["POST"])
@login_required
def files_rename():
    rel_path = request.form.get("path", "")
    new_name = (request.form.get("new_name") or "").strip()
    if not new_name:
        flash("New name cannot be empty.", "error")
        return redirect(url_for("files_page", path=os.path.dirname(rel_path)))

    abs_path = safe_join_server_dir(rel_path)
    base_dir = cfg["minecraft"]["server_dir"]
    parent_dir = os.path.dirname(abs_path)
    new_abs = safe_join_server_dir(os.path.join(os.path.dirname(rel_path), new_name))

    try:
        os.rename(abs_path, new_abs)
        flash("Renamed successfully.", "success")
    except Exception as e:
        flash(f"Failed to rename: {e}", "error")

    parent_rel = os.path.relpath(parent_dir, base_dir)
    if parent_rel == ".":
        parent_rel = ""
    return redirect(url_for("files_page", path=parent_rel))

# -------------------------------------------------
# Backups
# -------------------------------------------------

@app.route("/backups")
@login_required
def backups_page():
    backup_dir = cfg["backups"]["backup_dir"]
    os.makedirs(backup_dir, exist_ok=True)

    files = sorted(
        [f for f in os.listdir(backup_dir) if f.endswith(".tar.gz")],
        reverse=True
    )

    return render_template("backups.html", backups=files, title="Backups")


@app.route("/backups/run", methods=["POST"])
@login_required
def run_backup():
    backup_dir = cfg["backups"]["backup_dir"]
    world_dir = cfg["backups"]["world_dir"]

    os.makedirs(backup_dir, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"world-backup-{ts}.tar.gz"
    out_path = os.path.join(backup_dir, name)

    import tarfile
    with tarfile.open(out_path, "w:gz") as tar:
        tar.add(world_dir, arcname=os.path.basename(world_dir))

    keep_last = int(cfg["backups"].get("keep_last", 10))

    files = sorted(
        [f for f in os.listdir(backup_dir) if f.endswith(".tar.gz")],
        reverse=True
    )

    for f in files[keep_last:]:
        os.remove(os.path.join(backup_dir, f))

    send_discord_embed("Backup Completed", f"Created backup `{name}`.")
    flash(f"Backup created: {name}", "success")

    return redirect(url_for("backups_page"))


# -------------------------------------------------
# Scheduler UI
# -------------------------------------------------

@app.route("/schedule", methods=["GET", "POST"])
@login_required
def schedule_page():
    schedules = get_schedules()

    if request.method == "POST":
        stype = request.form.get("type", "once")
        enabled = request.form.get("enabled") == "on"
        mode = request.form.get("mode", "command")

        # Only read a command if mode=='command'
        raw_cmd = request.form.get("command", "").strip()
        command = raw_cmd if mode == "command" else ""

        new = {
            "id": int(time.time() * 1000),
            "type": stype,
            "enabled": enabled,
            "mode": mode,
            "command": command,
        }

        if stype == "once":
            new["run_at_iso"] = request.form.get("run_at")  # datetime-local string
        elif stype == "daily":
            new["time_str"] = request.form.get("time_str")   # "HH:MM"
        elif stype == "interval":
            new["interval_minutes"] = int(request.form.get("interval_minutes") or 60)

        schedules.append(new)
        set_schedules(schedules)

        flash("Schedule added.", "success")
        return redirect(url_for("schedule_page"))

    return render_template("schedule.html", schedules=schedules, title="Scheduler")



@app.route("/schedule/delete/<int:sched_id>", methods=["POST"])
@login_required
def delete_schedule(sched_id):
    schedules = [s for s in get_schedules() if s["id"] != sched_id]
    set_schedules(schedules)
    flash("Schedule deleted.", "success")
    return redirect(url_for("schedule_page"))

@app.route("/schedule/run/<int:sched_id>", methods=["POST"])
@login_required
def run_schedule_now(sched_id):
    schedules = get_schedules()
    target = next((s for s in schedules if s["id"] == sched_id), None)
    if not target:
        flash("Schedule not found.", "error")
        return redirect(url_for("schedule_page"))

    ok = execute_schedule(target)
    if ok:
        flash("Schedule executed successfully.", "success")
    else:
        flash("Schedule execution failed or did nothing.", "error")

    return redirect(url_for("schedule_page"))

# -------------------------------------------------
# Properties Editor
# -------------------------------------------------

def load_properties(path):
    props = {}
    raw_lines = []
    if not os.path.exists(path):
        return props, raw_lines

    with open(path, "r") as f:
        for line in f:
            raw_lines.append(line)
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()

    return props, raw_lines


def save_properties(path, updated, raw_lines):
    new_lines = []
    used_keys = set()

    for line in raw_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k, _ = stripped.split("=", 1)
            k = k.strip()
            if k in updated:
                new_lines.append(f"{k}={updated[k]}\n")
                used_keys.add(k)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    for k, v in updated.items():
        if k not in used_keys:
            new_lines.append(f"{k}={v}\n")

    with open(path, "w") as f:
        f.writelines(new_lines)


@app.route("/properties", methods=["GET", "POST"])
@login_required
def properties_page():
    path = cfg["minecraft"]["properties_file"]
    props, raw_lines = load_properties(path)

    if request.method == "POST":
        new_props = {
            k[5:]: request.form.get(k)
            for k in request.form if k.startswith("prop_")
        }
        save_properties(path, new_props, raw_lines)
        flash("server.properties updated.", "success")
        return redirect(url_for("properties_page"))

    return render_template("properties.html", props=props, title="Properties")


# -------------------------------------------------
# Settings Page
# -------------------------------------------------

# Installation status is process-local; the worker and status endpoint must run in the same process.
_java_jobs = {}
_java_jobs_lock = threading.Lock()
_java_install_lock = threading.Lock()

def _java_job_update(job_id, **changes):
    with _java_jobs_lock:
        if job_id in _java_jobs:
            _java_jobs[job_id].update(changes)

def _java_install_worker(job_id, major):
    with _java_install_lock:
        try:
            _java_job_update(job_id, stage="Preparing installation", percent=1)
            install_temurin(major, lambda **changes: _java_job_update(job_id, **changes))
            runtime_state["java_runtime"] = str(major)
            save_json(CONFIG_STATE_PATH, runtime_state)
            sync_start_command()
            _java_job_update(job_id, stage="Installed and activated", percent=100, status="complete")
        except Exception as exc:
            app.logger.exception("Java installation failed")
            _java_job_update(job_id, stage="Installation failed", status="error", error=str(exc))

@app.route("/settings/java/install", methods=["POST"])
@login_required
def install_java_route():
    user = current_user()
    if not user or not user["is_admin"]:
        abort(403)
    try:
        major = int(request.form.get("java_major", "0"))
        if major not in JAVA_RELEASES:
            raise ValueError("Unsupported Java version")
    except ValueError:
        return jsonify({"error": "Unsupported Java version"}), 400
    with _java_jobs_lock:
        if any(j["status"] in ("queued", "running") for j in _java_jobs.values()):
            return jsonify({"error": "Another Java installation is in progress"}), 409
        job_id = uuid.uuid4().hex
        _java_jobs[job_id] = {"status": "running", "stage": "Starting", "percent": 0, "downloaded": 0, "total": 0, "owner": user["id"]}
    threading.Thread(target=_java_install_worker, args=(job_id, major), daemon=True).start()
    return jsonify({"job_id": job_id}), 202

@app.route("/settings/java/install/status/<job_id>")
@login_required
def java_install_status(job_id):
    user = current_user()
    if not user or not user["is_admin"]:
        abort(403)
    with _java_jobs_lock:
        job = _java_jobs.get(job_id)
        if not job or job["owner"] != user["id"]:
            abort(404)
        return jsonify({k: v for k, v in job.items() if k != "owner"})

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if request.method == "POST":
        owner_name = request.form.get("server_owner_username", "").strip()
        if owner_name and not re.fullmatch(r"[A-Za-z0-9_]{3,16}", owner_name):
            flash("Minecraft owner username must be 3-16 letters, numbers or underscores.", "error")
            return redirect(url_for("settings_page"))
        runtime_state["server_owner_username"] = owner_name
        runtime_state["server_owner_auto_op"] = request.form.get("server_owner_auto_op") == "on"
        runtime_state["discord_webhook_url"] = request.form.get("webhook_url", "").strip()
        requested_jar = request.form.get("server_jar", "")
        if requested_jar and requested_jar not in available_server_jars():
            flash("Invalid server version. Refresh the page and select an available JAR.", "error")
            return redirect(url_for("settings_page"))
        runtime_state["server_jar"] = requested_jar
        runtime_state["start_command"] = request.form.get("start_command", runtime_state["start_command"])
        requested_java = request.form.get("java_runtime", "system")
        if requested_java not in ["system", *[str(v) for v in JAVA_RELEASES]] or not java_executable(requested_java):
            flash("Selected Java runtime is unavailable. Install it first.", "error")
            return redirect(url_for("settings_page"))
        runtime_state["java_runtime"] = requested_java
        runtime_state["auto_restart"] = request.form.get("auto_restart") == "on"
        runtime_state["server_mode"] = "modded" if request.form.get("server_mode")=="modded" else "vanilla"

        # Update live server manager
        try:
            sync_start_command()
        except ValueError:
            server_mgr.start_command = ""
        server_mgr.auto_restart = runtime_state["auto_restart"]

        save_json(CONFIG_STATE_PATH, runtime_state)
        flash("Settings saved.", "success")

        return redirect(url_for("settings_page"))

    saved = load_json(CONFIG_STATE_PATH, {})
    for k, v in saved.items():
        runtime_state[k] = v

    try:
        sync_start_command()
    except ValueError:
        server_mgr.start_command = ""
    return render_template("settings.html", state=runtime_state, server_jars=available_server_jars(), java_options=java_runtime_options(), is_admin=bool(current_user() and current_user()["is_admin"]), title="Settings")


# -------------------------------------------------
# App Entry
# -------------------------------------------------

if __name__ == "__main__":
    app.run(
        host=cfg["panel"]["host"],
        port=cfg["panel"]["port"],
        debug=False,
        threaded=True
    )
