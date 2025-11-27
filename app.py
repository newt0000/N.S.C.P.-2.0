# app.py
import os
import json
import time
from datetime import datetime, timedelta
from functools import wraps
import re

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
APP_VERSION = "2.2.11"

CONFIG_PATH = "config.yml"
DATA_DIR = "data"
EULA_FILE = os.path.join(DATA_DIR, "eula.txt")
SCHEDULES_PATH = os.path.join(DATA_DIR, "schedules.json")
CONFIG_STATE_PATH = os.path.join(DATA_DIR, "config_state.json")
UUID_CACHE_PATH = os.path.join(DATA_DIR, "uuid_cache.json")


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
}

PLUGINS_DIR = os.path.join(cfg["minecraft"]["server_dir"], "plugins")
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

ADMIN_PASSWORD = cfg["panel"]["admin_password"]


def license_ok():
    # Either globally accepted (eula.txt) OR accepted in this session
    return is_license_globally_accepted() or session.get("license_accepted")


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # gate everything except login/license routes (which don't use this decorator)
        if not license_ok():
            return redirect(url_for("license_page"))
        if not session.get("logged_in"):
            return redirect(url_for("login"))
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
        pw = request.form.get("password")
        if pw == ADMIN_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("Invalid password.", "error")

    return render_template("login.html", title="Login")


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

# -------------------------------------------------
# Plugin Manager
# -------------------------------------------------

@app.route("/plugins")
@login_required
def plugins_page():
    os.makedirs(PLUGINS_DIR, exist_ok=True)
    plugins = []

    try:
        with os.scandir(PLUGINS_DIR) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                name = entry.name

                # Only care about .jar and .jar.disabled
                if not (name.endswith(".jar") or name.endswith(".jar.disabled")):
                    continue

                # Enabled if plain .jar (NOT .jar.disabled)
                enabled = name.endswith(".jar") and not name.endswith(".jar.disabled")

                # Logical plugin name (strip .disabled and .jar)
                base = name
                if base.endswith(".disabled"):
                    base = base[:-9]          # strip ".disabled"
                if base.endswith(".jar"):
                    base = base[:-4]          # strip ".jar"

                plugins.append({
                    "file": name,
                    "plugin": base,
                    "enabled": enabled,
                })
    except FileNotFoundError:
        flash("Plugins directory not found.", "error")

    plugins.sort(key=lambda p: p["plugin"].lower())
    return render_template("plugins.html", title="Plugins", plugins=plugins)


@app.route("/plugins/toggle", methods=["POST"])
@login_required
def plugins_toggle():
    plugin_file = request.form.get("file")
    if not plugin_file:
        flash("No plugin file specified.", "error")
        return redirect(url_for("plugins_page"))

    current_path = os.path.join(PLUGINS_DIR, plugin_file)
    if not os.path.isfile(current_path):
        flash("Plugin file not found.", "error")
        return redirect(url_for("plugins_page"))

    # Decide new filename
    if plugin_file.endswith(".jar.disabled"):
        # Enable: remove ".disabled"
        new_name = plugin_file[:-9]
    elif plugin_file.endswith(".jar"):
        # Disable: add ".disabled"
        new_name = plugin_file + ".disabled"
    else:
        flash("Unsupported plugin file type.", "error")
        return redirect(url_for("plugins_page"))

    new_path = os.path.join(PLUGINS_DIR, new_name)

    try:
        os.rename(current_path, new_path)
        if new_name.endswith(".disabled"):
            flash(f"Plugin disabled: {new_name}", "success")
        else:
            flash(f"Plugin enabled: {new_name}", "success")
    except Exception as e:
        flash(f"Failed to toggle plugin: {e}", "error")

    return redirect(url_for("plugins_page"))

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

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if request.method == "POST":
        runtime_state["discord_webhook_url"] = request.form.get("webhook_url", "").strip()
        runtime_state["start_command"] = request.form.get("start_command", runtime_state["start_command"])
        runtime_state["auto_restart"] = request.form.get("auto_restart") == "on"

        # Update live server manager
        server_mgr.start_command = runtime_state["start_command"]
        server_mgr.auto_restart = runtime_state["auto_restart"]

        save_json(CONFIG_STATE_PATH, runtime_state)
        flash("Settings saved.", "success")

        return redirect(url_for("settings_page"))

    saved = load_json(CONFIG_STATE_PATH, {})
    for k, v in saved.items():
        runtime_state[k] = v

    return render_template("settings.html", state=runtime_state, title="Settings")


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
