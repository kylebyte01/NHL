"""
NHL Goal Light — Flask server + subprocess manager
===================================================

Run:  python server.py
Then open http://localhost:5000

Dependencies:  pip install -r requirements.txt
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import subprocess
import sys
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
NHL_API_BASE = main.NHL_API_BASE
EASTERN_TZ = ZoneInfo("America/New_York")

# static_folder=None: otherwise /static/<file> would expose the source tree
app = Flask(__name__, static_folder=None)

# ── subprocess state ─────────────────────────────────────────────────────────
_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()

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

# Minimum accepted values (stops a typo from hammering the NHL API / bulb)
MINIMUMS = {
    "poll_live_seconds": 0.1, "poll_critical_seconds": 0.1,
    "poll_pregame_seconds": 1.0, "poll_error_seconds": 1.0,
    "flash_interval": 0.05, "flash_duration": 1.0, "request_timeout": 1.0,
}


# ── logging helpers ──────────────────────────────────────────────────────────

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


def _proc_is_alive() -> bool:
    return _proc is not None and _proc.poll() is None


def _stop_proc() -> None:
    """Caller must hold _proc_lock."""
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


@atexit.register
def _cleanup() -> None:
    with _proc_lock:
        _stop_proc()


# ── request validation ───────────────────────────────────────────────────────

def _build_env(body: dict) -> dict[str, str]:
    """Validate numeric settings and translate them into environment variables."""
    env: dict[str, str] = {}
    for key, (var, cast) in main.NUMERIC_ENV_FIELDS.items():
        value = body.get(key)
        if value is None or value == "":
            continue
        try:
            number = cast(str(value))
        except (TypeError, ValueError):
            raise ValueError(f"Invalid value for {key}: {value!r}")
        if number < 0:
            raise ValueError(f"{key} must not be negative.")
        if number < MINIMUMS.get(key, 0):
            raise ValueError(f"{key} must be at least {MINIMUMS[key]}.")
        env[var] = str(number)
    return env


# ── routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(HERE, "web.html")


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
            "primary": list(p.primary),
            "secondary": list(p.secondary),
        }
        for abbrev, p in main.TEAM_COLORS.items()
    })


@app.route("/api/config-defaults")
def api_config_defaults():
    return jsonify({
        "primary_team": main.DEFAULT_PRIMARY_TEAM,
        "primary_delay_seconds": main.DEFAULT_PRIMARY_DELAY,
        "default_delay_seconds": main.DEFAULT_OTHER_DELAY,
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


# ── monitor control ──────────────────────────────────────────────────────────

@app.route("/api/start", methods=["POST"])
def api_start():
    global _proc
    body: dict = request.get_json(silent=True) or {}

    ips = main.parse_bulb_ips(body.get("bulb_ips") or body.get("bulb_ip"))
    if not ips:
        return jsonify({"error": "At least one bulb IP is required"}), 400

    try:
        game_ids = [int(g) for g in (body.get("game_ids") or [])]
    except (TypeError, ValueError):
        return jsonify({"error": "game_ids must be integers"}), 400
    if not game_ids:
        return jsonify({"error": "At least one game_id is required"}), 400

    primary = str(body.get("primary_team") or main.DEFAULT_PRIMARY_TEAM).strip().upper()
    if primary not in main.TEAM_COLORS:
        return jsonify({"error": f"Unknown primary team: {primary}"}), 400

    game_delays: dict[str, float] = {}
    for key, value in (body.get("game_delays") or {}).items():
        try:
            game_delays[str(int(key))] = max(0.0, float(value))
        except (TypeError, ValueError):
            return jsonify({"error": f"Invalid delay for game {key}: {value!r}"}), 400

    try:
        env_overrides = _build_env(body)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with _proc_lock:
        if _proc_is_alive():
            return jsonify({"error": "Already monitoring. Stop first."}), 409

        env = os.environ.copy()
        env.update(env_overrides)
        env["BULB_IP"] = ",".join(ips)
        env["GAME_IDS"] = ",".join(str(g) for g in game_ids)
        env["PRIMARY_TEAM"] = primary
        env["GAME_DELAYS"] = json.dumps(game_delays)
        env["PYTHONUNBUFFERED"] = "1"

        try:
            _proc = subprocess.Popen(
                [sys.executable, MAIN_SCRIPT],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, cwd=HERE,
            )
        except Exception as exc:
            return jsonify({"error": f"Failed to start subprocess: {exc}"}), 500

        threading.Thread(target=_read_stdout, args=(_proc,), daemon=True).start()
        return jsonify({"ok": True, "pid": _proc.pid})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with _proc_lock:
        was_running = _proc_is_alive()
        _stop_proc()
    return jsonify({"ok": True, "message": None if was_running else "No process running"})


@app.route("/api/status")
def api_status():
    with _proc_lock:
        alive = _proc_is_alive()
        pid = _proc.pid if alive else None
    return jsonify({"state": "monitoring" if alive else "idle", "pid": pid})


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
    body = request.get_json(silent=True) or {}
    ips = main.parse_bulb_ips(body.get("bulb_ips") or body.get("ip"))
    team = str(body.get("team") or "").strip().upper()

    if not ips:
        return jsonify({"ok": False, "error": "No bulb IP provided"}), 400
    if team not in main.TEAM_COLORS:
        return jsonify({"ok": False, "error": f"Unknown team: {team or '(none)'}"}), 400
    try:
        duration = min(60.0, max(1.0, float(body.get("flash_duration", 12))))
        interval = max(0.05, float(body.get("flash_interval", 0.45)))
        transition = max(0, int(float(body.get("flash_transition_ms", 0))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid flash settings"}), 400

    if not _sim_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": "A simulation is already running"}), 409
    try:
        config = main.AppConfig(
            bulb_ips=ips, flash_duration=duration,
            flash_interval=interval, flash_transition_ms=transition,
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

        asyncio.run(asyncio.wait_for(_simulate(), timeout=duration + 30))
        return jsonify({"ok": True, "error": None})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc) or type(exc).__name__})
    finally:
        _sim_lock.release()


# ── entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("SERVER_PORT", "5000"))
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    print(f"NHL Goal Light GUI → http://localhost:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)
