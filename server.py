"""
NHL Goal Light — Flask server + monitor supervisor
===================================================

Run:  python server.py
Then open http://localhost:5000

The server keeps its settings in settings.json and runs the monitor
automatically (every game, every day).  Settings changed in the GUI are saved
and pushed to the running monitor instantly — no stop/start needed.

Dependencies:  pip install -r requirements.txt
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

import main  # AppConfig, team data, BulbController, probe_bulb

# ── HTTP helper (requests if available, urllib otherwise) ────────────────────
try:
    import requests as _requests

    _session = _requests.Session()

    def _fetch_json(url: str, timeout: int = 10) -> dict:
        r = _session.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
except ImportError:  # pragma: no cover
    import urllib.request

    def _fetch_json(url: str, timeout: int = 10) -> dict:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())

# ─────────────────────────────────────────────────────────────────────────────

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = os.path.join(HERE, "main.py")
SETTINGS_PATH = os.getenv("SETTINGS_FILE", os.path.join(HERE, "settings.json"))
NHL_API_BASE = main.NHL_API_BASE
EASTERN_TZ = ZoneInfo("America/New_York")
RESTART_DELAY = 10.0           # min seconds between monitor (re)starts

# static_folder=None: otherwise /static/<file> would expose the source tree
app = Flask(__name__, static_folder=None)

# ── subprocess state ─────────────────────────────────────────────────────────
_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()
_last_start = 0.0

# ── log buffer: sequence-numbered so every client/tab gets every line ────────
MAX_LOG_HISTORY = 2000
_log_cond = threading.Condition()
_log_buf: deque[tuple[int, str]] = deque(maxlen=MAX_LOG_HISTORY)
_log_seq = 0

# ── games cache ──────────────────────────────────────────────────────────────
_GAMES_CACHE_TTL = 60.0
_games_lock = threading.Lock()
_games_cache: list[dict] | None = None
_games_cache_ts = 0.0

_sim_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# Settings (persistent, validated)
# ══════════════════════════════════════════════════════════════════════════════

SETTINGS_VERSION = 2          # v2: primary-team delay default 35s -> 60s


def _default_settings() -> dict:
    base = main.AppConfig()
    s: dict = {
        "settings_version": SETTINGS_VERSION,
        "enabled": True,                     # auto-run unless the user pauses it
        "bulb_ips": [],
        "primary_team": main.DEFAULT_PRIMARY_TEAM,
        "muted_games": [],                   # game ids that should NOT flash
        "game_delays": {},                   # {"gameId": seconds} per-game overrides
    }
    for name in main.NUMERIC_ENV_FIELDS:
        s[name] = getattr(base, name)
    return s


def _clean_settings(body: dict, strict: bool = True) -> dict:
    """Validate a (partial) settings dict. strict=True raises ValueError on bad
    values (API); strict=False silently drops them (settings file)."""
    out: dict = {}

    def bad(msg: str) -> None:
        if strict:
            raise ValueError(msg)

    for key, (_, cast) in main.NUMERIC_ENV_FIELDS.items():
        if key not in body or body[key] in (None, ""):
            continue
        try:
            number = cast(str(body[key]))
        except (TypeError, ValueError):
            bad(f"Invalid value for {key}: {body[key]!r}")
            continue
        if number < 0:
            bad(f"{key} must not be negative.")
            continue
        if number < main.MINIMUMS.get(key, 0):
            bad(f"{key} must be at least {main.MINIMUMS[key]}.")
            continue
        out[key] = number

    if "primary_team" in body:
        team = str(body["primary_team"] or "").strip().upper()
        if team in main.TEAM_STYLES:
            out["primary_team"] = team
        else:
            bad(f"Unknown team: {team or '(none)'}")

    if "bulb_ips" in body:
        out["bulb_ips"] = list(main.parse_bulb_ips(body["bulb_ips"]))

    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])

    if "muted_games" in body:
        try:
            out["muted_games"] = sorted({int(g) for g in (body["muted_games"] or [])})
        except (TypeError, ValueError):
            bad("muted_games must be a list of integers.")

    if "game_delays" in body:
        delays: dict[str, float] = {}
        raw = body["game_delays"] or {}
        if isinstance(raw, dict):
            for gid, value in raw.items():
                try:
                    delays[str(int(gid))] = max(0.0, float(value))
                except (TypeError, ValueError):
                    bad(f"Invalid delay for game {gid}: {value!r}")
            out["game_delays"] = delays
        else:
            bad("game_delays must be an object.")

    return out


_settings_lock = threading.RLock()


def _load_settings() -> dict:
    s = _default_settings()
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        s.update(_clean_settings(raw, strict=False))
        # An old file that still has the previous default (35s) was never customised:
        # move it to the new default instead of leaving the old number behind.
        if raw.get("settings_version", 1) < 2 and s["primary_delay_seconds"] == 35.0:
            s["primary_delay_seconds"] = main.DEFAULT_PRIMARY_DELAY
    except (OSError, ValueError, AttributeError):
        pass
    if not s["bulb_ips"]:  # first run: seed from the BULB_IP environment variable
        s["bulb_ips"] = list(main.parse_bulb_ips(os.getenv("BULB_IP", "")))
    return s


_settings: dict = _load_settings()


def _save_settings() -> None:
    """Atomic write so a crash can never leave a half-written settings file."""
    with _settings_lock:
        data = json.dumps(_settings, indent=2)
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SETTINGS_PATH), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, SETTINGS_PATH)
    except OSError as exc:
        _append_log(f"ERROR: could not save settings: {exc}")


def _settings_copy() -> dict:
    with _settings_lock:
        return json.loads(json.dumps(_settings))


def _live_payload(s: dict) -> dict:
    """The subset of settings the running monitor understands."""
    payload = {name: s[name] for name in main.NUMERIC_ENV_FIELDS}
    payload.update({k: s[k] for k in ("primary_team", "bulb_ips", "muted_games", "game_delays")})
    return payload


# ══════════════════════════════════════════════════════════════════════════════
# Log helpers
# ══════════════════════════════════════════════════════════════════════════════

def _append_log(line: str) -> None:
    global _log_seq
    with _log_cond:
        _log_seq += 1
        _log_buf.append((_log_seq, line.replace("\r", "").replace("\n", " ")))
        _log_cond.notify_all()


def _read_stdout(proc: subprocess.Popen) -> None:
    try:
        for raw in iter(proc.stdout.readline, b""):
            _append_log(raw.decode(errors="replace").rstrip())
    except Exception:
        pass
    finally:
        code = proc.wait()
        _append_log(f"── monitor process exited (code {code}) ──")


# ══════════════════════════════════════════════════════════════════════════════
# Monitor process management
# ══════════════════════════════════════════════════════════════════════════════

def _proc_is_alive() -> bool:
    return _proc is not None and _proc.poll() is None


def _spawn_env(s: dict) -> dict[str, str]:
    env = os.environ.copy()
    for name, (var, _) in main.NUMERIC_ENV_FIELDS.items():
        env[var] = str(s[name])
    env.update({
        "BULB_IP": ",".join(s["bulb_ips"]),
        "PRIMARY_TEAM": s["primary_team"],
        "GAME_DELAYS": json.dumps(s["game_delays"]),
        "MUTED_GAMES": ",".join(str(g) for g in s["muted_games"]),
        "DAEMON": "1",
        "PYTHONUNBUFFERED": "1",
    })
    env.pop("GAME_IDS", None)
    return env


def _start_proc_locked(s: dict) -> str | None:
    """Caller holds _proc_lock. Returns an error string or None."""
    global _proc, _last_start
    if not s["bulb_ips"]:
        return "Add at least one bulb IP first."
    try:
        _proc = subprocess.Popen(
            [sys.executable, MAIN_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=_spawn_env(s), cwd=HERE,
        )
    except Exception as exc:
        _proc = None
        return f"Failed to start monitor: {exc}"
    _last_start = time.monotonic()
    threading.Thread(target=_read_stdout, args=(_proc,), daemon=True).start()
    return None


def _push_config_locked(s: dict) -> None:
    """Caller holds _proc_lock: send the current settings to the running monitor."""
    if not _proc_is_alive() or _proc.stdin is None:
        return
    try:
        line = json.dumps({"type": "config", "settings": _live_payload(s)}) + "\n"
        _proc.stdin.write(line.encode())
        _proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass


def _stop_proc_locked() -> None:
    global _proc
    if not _proc_is_alive():
        _proc = None
        return
    try:
        _proc.terminate()          # SIGTERM → main.py restores the bulb gracefully
        _proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        _proc.kill()
    except Exception:
        pass
    _proc = None


def _reconcile_locked(s: dict, push: bool = True) -> str | None:
    """Make reality match the settings: run when enabled, stop when not."""
    if not s["enabled"] or not s["bulb_ips"]:
        _stop_proc_locked()
        return None
    if _proc_is_alive():
        if push:
            _push_config_locked(s)
        return None
    return _start_proc_locked(s)


def _supervisor() -> None:
    """Keeps the monitor running: starts it on boot and restarts it if it dies."""
    while True:
        time.sleep(3)
        s = _settings_copy()
        with _proc_lock:
            if (s["enabled"] and s["bulb_ips"] and not _proc_is_alive()
                    and time.monotonic() - _last_start >= RESTART_DELAY):
                _start_proc_locked(s)


@atexit.register
def _cleanup() -> None:
    with _proc_lock:
        _stop_proc_locked()


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory(HERE, "web.html")


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify({"settings": _settings_copy(), "defaults": _default_settings()})

    body = request.get_json(silent=True) or {}
    try:
        patch = _clean_settings(body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with _settings_lock:
        _settings.update(patch)
    _save_settings()
    s = _settings_copy()
    with _proc_lock:
        error = _reconcile_locked(s)
    return jsonify({"ok": error is None, "error": error, "settings": s})


@app.route("/api/start", methods=["POST"])
def api_start():
    s = _settings_copy()
    if not s["bulb_ips"]:
        return jsonify({"error": "Add at least one bulb IP first."}), 400
    with _settings_lock:
        _settings["enabled"] = True
    _save_settings()
    with _proc_lock:
        error = _reconcile_locked(_settings_copy())
    if error:
        return jsonify({"error": error}), 500
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with _settings_lock:
        _settings["enabled"] = False
    _save_settings()
    with _proc_lock:
        _stop_proc_locked()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    s = _settings_copy()
    with _proc_lock:
        alive = _proc_is_alive()
        pid = _proc.pid if alive else None
    state = "monitoring" if alive else ("paused" if not s["enabled"] else "idle")
    return jsonify({"state": state, "pid": pid, "enabled": s["enabled"],
                    "has_bulbs": bool(s["bulb_ips"])})


@app.route("/api/games")
def api_games():
    global _games_cache, _games_cache_ts
    force = request.args.get("refresh") == "1"

    with _games_lock:
        now = time.monotonic()
        if not force and _games_cache is not None and (now - _games_cache_ts) < _GAMES_CACHE_TTL:
            return jsonify(_games_cache)

        today = datetime.now(EASTERN_TZ).date().isoformat()
        try:
            payload = _fetch_json(f"{NHL_API_BASE}/schedule/{today}")
        except Exception as exc:
            return jsonify({"error": str(exc)}), 502

        games: list[dict] = []
        for day in payload.get("gameWeek", []):
            if day.get("date") != today:
                continue
            for g in day.get("games", []):
                away, home = g.get("awayTeam", {}), g.get("homeTeam", {})
                games.append({
                    "id": g["id"],
                    "away_abbrev": away.get("abbrev", "???"),
                    "away_name": main._display_team_name(away),
                    "home_abbrev": home.get("abbrev", "???"),
                    "home_name": main._display_team_name(home),
                    "start_time_utc": g.get("startTimeUTC", "1970-01-01T00:00:00Z"),
                    "game_state": g.get("gameState", ""),
                })
        games.sort(key=lambda g: g["start_time_utc"])
        _games_cache, _games_cache_ts = games, now
        return jsonify(games)


@app.route("/api/game-state/<int:game_id>")
def api_game_state(game_id: int):
    """Live game state: period, clock, score, intermission flag."""
    try:
        data = _fetch_json(f"{NHL_API_BASE}/gamecenter/{game_id}/landing")
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    clock = data.get("clock", {})
    period = data.get("periodDescriptor", {})
    away, home = data.get("awayTeam", {}), data.get("homeTeam", {})
    return jsonify({
        "ok": True,
        "state": data.get("gameState", ""),
        "period": period.get("number"),
        "period_type": period.get("periodType", "REG"),
        "clock": clock.get("timeRemaining"),
        "in_intermission": clock.get("inIntermission", False),
        "away_abbrev": away.get("abbrev", ""),
        "away_score": away.get("score", 0),
        "home_abbrev": home.get("abbrev", ""),
        "home_score": home.get("score", 0),
    })


@app.route("/api/team-colors")
def api_team_colors():
    return jsonify({
        abbrev: {
            "name": main.TEAM_NAMES.get(abbrev, abbrev),
            "a": st.a, "b": st.b,
        }
        for abbrev, st in main.TEAM_STYLES.items()
    })


# ── log streaming ────────────────────────────────────────────────────────────

@app.route("/api/log-history")
def api_log_history():
    with _log_cond:
        entries = [line for _, line in _log_buf]
        last = _log_seq
    return jsonify({"entries": entries, "last": last})


@app.route("/api/clear-log", methods=["POST"])
def api_clear_log():
    with _log_cond:
        _log_buf.clear()
    return jsonify({"ok": True})


@app.route("/api/log")
def api_log():
    """SSE stream of log lines with seq > ?since (or the Last-Event-ID header).
    Each client keeps its own cursor, so multiple tabs/devices all see every line."""
    raw = request.headers.get("Last-Event-ID") or request.args.get("since", "")
    try:
        cursor = int(raw)
    except ValueError:
        with _log_cond:
            cursor = _log_seq

    def generate():
        nonlocal cursor
        while True:
            with _log_cond:
                if not any(seq > cursor for seq, _ in _log_buf):
                    _log_cond.wait(timeout=15)
                items = [(seq, line) for seq, line in _log_buf if seq > cursor]
            if not items:
                yield ": heartbeat\n\n"
                continue
            for seq, line in items:
                cursor = seq
                yield f"id: {seq}\ndata: {line}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── bulb tools ───────────────────────────────────────────────────────────────

@app.route("/api/test-bulb")
def api_test_bulb():
    ip = request.args.get("ip", "").strip()
    if not ip:
        return jsonify({"ok": False, "model": None, "error": "No IP provided"})
    try:
        model = asyncio.run(main.probe_bulb(ip))
        return jsonify({"ok": True, "model": model, "error": None})
    except Exception as exc:
        return jsonify({"ok": False, "model": None, "error": str(exc) or type(exc).__name__})


@app.route("/api/simulate-goal", methods=["POST"])
def api_simulate_goal():
    """Flash a team's two colours on the saved bulbs, using the saved flash settings."""
    body = request.get_json(silent=True) or {}
    team = str(body.get("team") or "").strip().upper()
    s = _settings_copy()

    if not s["bulb_ips"]:
        return jsonify({"ok": False, "error": "Add a bulb IP first."}), 400
    if team not in main.TEAM_STYLES:
        return jsonify({"ok": False, "error": f"Unknown team: {team or '(none)'}"}), 400

    if not _sim_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "A simulation is already running"}), 409
    try:
        config = main.AppConfig(
            bulb_ips=tuple(s["bulb_ips"]),
            flash_duration=min(60.0, float(s["flash_duration"])),
            flash_interval=float(s["flash_interval"]),
            flash_transition_ms=int(s["flash_transition_ms"]),
            restore_transition_ms=int(s["restore_transition_ms"]),
        )

        async def _simulate() -> None:
            bulb = main.BulbController(config)
            try:
                snaps = await bulb.capture_state()
                try:
                    await bulb.flash_team(team, snaps)
                finally:
                    await bulb.restore_state(snaps)
            finally:
                await bulb.shutdown()

        asyncio.run(asyncio.wait_for(_simulate(), timeout=config.flash_duration + 30))
        return jsonify({"ok": True, "error": None})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc) or type(exc).__name__})
    finally:
        _sim_lock.release()


# ── entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("SERVER_PORT", "5000"))
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    threading.Thread(target=_supervisor, name="supervisor", daemon=True).start()
    print(f"NHL Goal Light GUI → http://localhost:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
