"""
NHL Goal Light Controller
━━━━━━━━━━━━━━━━━━━━━━━━

Monitors NHL games in real time and flashes one or more TP-Link Kasa smart
bulbs in the scoring team's two colours whenever a goal is scored.

Modes
-----
  * DAEMON=1   (what the web GUI uses) tracks EVERY game, every day, forever.
               Settings can be changed live over stdin — no restart needed.
  * GAME_IDS   tracks a fixed list of games, then exits.
  * (neither)  interactive picker in the terminal.

TV delay
--------
  * games involving the PRIMARY team (default MTL)  -> PRIMARY_DELAY_SECONDS (60)
  * every other game                                 -> DEFAULT_DELAY_SECONDS (45)
  * an explicit per-game override (GAME_DELAYS json) -> wins over both
"""
from __future__ import annotations

import asyncio
import colorsys
import json
import logging
import os
import random
import re
import signal
import sys
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import aiohttp
from kasa.iot import IotBulb

log = logging.getLogger(__name__)

NHL_API_BASE = "https://api-web.nhle.com/v1"
EASTERN_TZ = ZoneInfo("America/New_York")

FINAL_STATES = frozenset({"FINAL", "OFF"})
LIVE_STATES = frozenset({"LIVE", "CRIT"})
PREGAME_STATES = frozenset({"PRE", "FUT"})
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
STALE_SECONDS = 30.0
LIGHT_SERVICE = "smartlife.iot.smartbulb.lightingservice"
WHITE_KELVIN = 6500                # what "white" means for a flash step
SCHEDULE_REFRESH_SECONDS = 600     # daemon re-reads the schedule this often
GIVE_UP_AFTER = timedelta(hours=4)  # postponed / never-started games
MIN_STEP_GAP = 0.03                # never hammer a bulb faster than this

DEFAULT_PRIMARY_TEAM = "MTL"
DEFAULT_PRIMARY_DELAY = 60.0
DEFAULT_OTHER_DELAY = 45.0


# ── team styles: two colours, flashed evenly ─────────────────────────────────
#
# Every team flashes its two main brand colours, alternating with equal time.
# A bulb cannot show black (it would just be "off"), so a team whose second
# colour is black uses its other official colour instead (usually white).

BRIGHT_FLOOR = 60                 # dark brand colours (navy…) are lifted to this brightness


@dataclass(frozen=True)
class Shade:
    """One light state a bulb can be told to show."""
    kind: str                     # "hsv" | "white"
    h: int = 0
    s: int = 0
    v: int = 100
    kelvin: int = 4000            # only used when kind == "white"


def bulb_shade(hex_color: str) -> Shade:
    """Brand hex colour -> bulb light state.

    Hue and saturation stay true to the brand colour; brightness is lifted for dark
    shades (a dim LED just looks black).  White/silver use a white colour
    temperature rather than 'saturation 0', which some bulbs render as their warm
    default colour.
    """
    hx = hex_color.lstrip("#")
    r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    H, S, V = round(h * 360) % 360, round(s * 100), round(v * 100)
    if S < 12 or V < 15:                                    # white / silver / grey
        return Shade("white", v=100, kelvin=4000 if hx.upper() == "FFFFFF" else 6500)
    return Shade("hsv", H, S, max(V, BRIGHT_FLOOR))


@dataclass(frozen=True)
class TeamStyle:
    a: str                        # first colour (hex)
    b: str                        # second colour (hex)

    def palette(self) -> tuple[Shade, Shade]:
        return bulb_shade(self.a), bulb_shade(self.b)


# Official club brand colours, 2026-27 season.
TEAM_STYLES: dict[str, TeamStyle] = {
    "ANA": TeamStyle("#F47A38", "#B9975B"),   # orange, gold
    "BOS": TeamStyle("#FFB81C", "#FFFFFF"),   # gold, white
    "BUF": TeamStyle("#003087", "#FFB81C"),   # royal blue, gold
    "CGY": TeamStyle("#D2001C", "#FAAF19"),   # red, yellow
    "CAR": TeamStyle("#CE1126", "#FFFFFF"),   # red, white
    "CHI": TeamStyle("#CF0A2C", "#FFFFFF"),   # red, white
    "COL": TeamStyle("#6F263D", "#236192"),   # burgundy, blue
    "CBJ": TeamStyle("#002654", "#CE1126"),   # union blue, red
    "DAL": TeamStyle("#006847", "#FFFFFF"),   # victory green, white
    "DET": TeamStyle("#CE1126", "#FFFFFF"),   # red, white
    "EDM": TeamStyle("#041E42", "#FF4C00"),   # navy blue, orange
    "FLA": TeamStyle("#C8102E", "#041E42"),   # red, navy
    "LAK": TeamStyle("#A2AAAD", "#FFFFFF"),   # silver (cool), white (neutral)
    "MIN": TeamStyle("#154734", "#A6192E"),   # forest green, red
    "MTL": TeamStyle("#AF1E2D", "#192168"),   # red, blue
    "NSH": TeamStyle("#FFB81C", "#041E42"),   # gold, navy
    "NJD": TeamStyle("#CE1126", "#FFFFFF"),   # red, white
    "NYI": TeamStyle("#00539B", "#F47D30"),   # blue, orange
    "NYR": TeamStyle("#0038A8", "#CE1126"),   # blue, red
    "OTT": TeamStyle("#C52032", "#C69214"),   # red, gold
    "PHI": TeamStyle("#F74902", "#FFFFFF"),   # orange, white
    "PIT": TeamStyle("#FCB514", "#FFFFFF"),   # gold, white
    "SJS": TeamStyle("#006D75", "#FFFFFF"),   # pacific teal, white
    "SEA": TeamStyle("#99D9D9", "#E9072B"),   # ice blue, red alert
    "STL": TeamStyle("#002F87", "#FCB514"),   # blue, yellow
    "TBL": TeamStyle("#002868", "#FFFFFF"),   # blue, white
    "TOR": TeamStyle("#00205B", "#FFFFFF"),   # blue, white
    "UTA": TeamStyle("#6CACE4", "#FFFFFF"),   # mountain blue, salt white
    "VAN": TeamStyle("#00205B", "#00843D"),   # blue, green
    "VGK": TeamStyle("#B4975A", "#C8102E"),   # gold, red
    "WSH": TeamStyle("#C8102E", "#041E42"),   # red, navy
    "WPG": TeamStyle("#004C97", "#A2AAAD"),   # aviator blue, silver (cool white)
}
DEFAULT_STYLE = TeamStyle("#FF0000", "#FFFFFF")
TEAM_COLORS = TEAM_STYLES   # backwards-compatible alias

TEAM_NAMES: dict[str, str] = {
    "ANA": "Anaheim Ducks", "BOS": "Boston Bruins", "BUF": "Buffalo Sabres",
    "CGY": "Calgary Flames", "CAR": "Carolina Hurricanes", "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche", "CBJ": "Columbus Blue Jackets", "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings", "EDM": "Edmonton Oilers", "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings", "MIN": "Minnesota Wild", "MTL": "Montreal Canadiens",
    "NSH": "Nashville Predators", "NJD": "New Jersey Devils", "NYI": "New York Islanders",
    "NYR": "New York Rangers", "OTT": "Ottawa Senators", "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins", "SJS": "San Jose Sharks", "SEA": "Seattle Kraken",
    "STL": "St. Louis Blues", "TBL": "Tampa Bay Lightning", "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Mammoth", "VAN": "Vancouver Canucks", "VGK": "Vegas Golden Knights",
    "WSH": "Washington Capitals", "WPG": "Winnipeg Jets",
}


# ── data classes ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GameInfo:
    game_id: int
    away_abbrev: str
    home_abbrev: str
    away_name: str
    home_name: str
    start_time_utc: datetime
    game_state: str


@dataclass(frozen=True)
class GameStatus:
    away_abbrev: str
    home_abbrev: str
    game_state: str
    away_score: Optional[int]
    home_score: Optional[int]


@dataclass(frozen=True)
class BulbSnapshot:
    is_on: bool
    brightness: int
    hue: int
    saturation: int
    color_temp: Optional[int]
    color_mode: Optional[str]
    supports_color: bool
    supports_color_temp: bool


@dataclass(frozen=True)
class GoalEvent:
    game_id: int
    scoring_team: str


def _to_int(value: str) -> int:
    return int(float(value))


# config field -> (environment variable, cast).  The server uses this table too.
NUMERIC_ENV_FIELDS: dict[str, tuple[str, Callable[[str], float]]] = {
    "request_timeout":        ("NHL_REQUEST_TIMEOUT", float),
    "max_retries":            ("NHL_MAX_RETRIES", _to_int),
    "backoff_base":           ("NHL_BACKOFF_BASE", float),
    "backoff_max":            ("NHL_BACKOFF_MAX", float),
    "flash_duration":         ("FLASH_DURATION", float),
    "flash_interval":         ("FLASH_INTERVAL", float),
    "flash_quiet_window":     ("FLASH_QUIET_WINDOW", float),
    "pregame_buffer_seconds": ("PREGAME_BUFFER_SECONDS", _to_int),
    "poll_live_seconds":      ("POLL_LIVE_SECONDS", float),
    "poll_critical_seconds":  ("POLL_CRITICAL_SECONDS", float),
    "poll_pregame_seconds":   ("POLL_PREGAME_SECONDS", float),
    "poll_error_seconds":     ("POLL_ERROR_SECONDS", float),
    "restore_transition_ms":  ("RESTORE_TRANSITION_MS", _to_int),
    "flash_transition_ms":    ("FLASH_TRANSITION_MS", _to_int),
    "primary_delay_seconds":  ("PRIMARY_DELAY_SECONDS", float),
    "default_delay_seconds":  ("DEFAULT_DELAY_SECONDS", float),
}

# Smallest accepted values (stops a typo from hammering the NHL API / a bulb).
MINIMUMS: dict[str, float] = {
    "poll_live_seconds": 0.1, "poll_critical_seconds": 0.1,
    "poll_pregame_seconds": 1.0, "poll_error_seconds": 1.0,
    "flash_interval": 0.1, "flash_duration": 1.0, "request_timeout": 1.0,
}


@dataclass
class AppConfig:
    """Mutable on purpose: the daemon applies GUI changes to this one shared object,
    and every consumer reads the fields at the moment it needs them."""
    bulb_ips: tuple[str, ...] = ()
    request_timeout: float = 10.0
    max_retries: int = 4
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    flash_duration: float = 12.0
    flash_interval: float = 0.45
    flash_quiet_window: float = 1.5
    pregame_buffer_seconds: int = 300
    poll_live_seconds: float = 1.0
    poll_critical_seconds: float = 0.75
    poll_pregame_seconds: float = 10.0
    poll_error_seconds: float = 5.0
    restore_transition_ms: int = 150
    flash_transition_ms: int = 0
    primary_team: str = DEFAULT_PRIMARY_TEAM
    primary_delay_seconds: float = DEFAULT_PRIMARY_DELAY
    default_delay_seconds: float = DEFAULT_OTHER_DELAY
    game_delays: dict = field(default_factory=dict)      # {game_id: seconds}
    muted_games: set = field(default_factory=set)        # {game_id}
    team_colors: dict = field(default_factory=dict)      # {'EDM': ('#RRGGBB', '#RRGGBB')} user overrides

    def delay_for(self, game: GameInfo) -> float:
        """TV delay (s) for a game: explicit override > primary team > default."""
        override = self.game_delays.get(game.game_id)
        if override is not None:
            return max(0.0, float(override))
        if self.is_primary_game(game):
            return max(0.0, self.primary_delay_seconds)
        return max(0.0, self.default_delay_seconds)

    def style_for(self, team: str) -> TeamStyle:
        """The team's colours: the user's override if there is one, else the brand colours."""
        custom = self.team_colors.get(team)
        if custom:
            return TeamStyle(custom[0], custom[1])
        return TEAM_STYLES.get(team, DEFAULT_STYLE)

    def is_primary_game(self, game: GameInfo) -> bool:
        return self.primary_team in (game.away_abbrev, game.home_abbrev)

    def apply(self, data: dict) -> set[str]:
        """Apply a (partial) settings dict in place; returns the names that changed."""
        changed: set[str] = set()

        for name, (_, cast) in NUMERIC_ENV_FIELDS.items():
            if name not in data:
                continue
            try:
                value = cast(str(data[name]))
            except (TypeError, ValueError):
                continue
            if value < MINIMUMS.get(name, 0.0):
                continue
            if getattr(self, name) != value:
                setattr(self, name, value)
                changed.add(name)

        if "primary_team" in data:
            team = str(data["primary_team"] or "").strip().upper()
            if team in TEAM_STYLES and team != self.primary_team:
                self.primary_team = team
                changed.add("primary_team")

        if "bulb_ips" in data:
            ips = parse_bulb_ips(data["bulb_ips"])
            if ips and ips != self.bulb_ips:
                self.bulb_ips = ips
                changed.add("bulb_ips")

        if "game_delays" in data:
            delays = _clean_game_delays(data["game_delays"])
            if delays != self.game_delays:
                self.game_delays = delays
                changed.add("game_delays")

        if "muted_games" in data:
            muted = _clean_id_set(data["muted_games"])
            if muted != self.muted_games:
                self.muted_games = muted
                changed.add("muted_games")

        if "team_colors" in data:
            colors = _clean_team_colors(data["team_colors"])
            if colors != self.team_colors:
                self.team_colors = colors
                changed.add("team_colors")

        return changed

    @classmethod
    def from_env(cls) -> "AppConfig":
        ips = parse_bulb_ips(os.getenv("BULB_IP", ""))
        if not ips:
            raise SystemExit(
                "ERROR: Set the BULB_IP environment variable to your bulb's IP address "
                "(comma-separate several), e.g.  export BULB_IP=192.168.1.x"
            )
        defaults = cls()
        kwargs = {name: _env(var, cast, getattr(defaults, name))
                  for name, (var, cast) in NUMERIC_ENV_FIELDS.items()}
        primary = (os.getenv("PRIMARY_TEAM", "") or DEFAULT_PRIMARY_TEAM).strip().upper()
        if primary not in TEAM_STYLES:
            log.warning("Unknown PRIMARY_TEAM %r — falling back to %s.", primary, DEFAULT_PRIMARY_TEAM)
            primary = DEFAULT_PRIMARY_TEAM
        return cls(
            bulb_ips=ips,
            primary_team=primary,
            game_delays=_parse_game_delays(os.getenv("GAME_DELAYS", "")),
            muted_games=_clean_id_set((os.getenv("MUTED_GAMES", "") or "").split(",")),
            team_colors=_parse_team_colors(os.getenv("TEAM_COLOR_OVERRIDES", "")),
            **kwargs,
        )


class _RetryableError(Exception):
    def __init__(self, status: int, retry_after: Optional[float]) -> None:
        super().__init__(f"Retryable HTTP status: {status}")
        self.status = status
        self.retry_after = retry_after


# ── small helpers ────────────────────────────────────────────────────────────

def _env(name: str, cast: Callable[[str], float], default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw.strip())
    except ValueError:
        raise SystemExit(f"ERROR: invalid value for {name}: {raw!r}")


def parse_bulb_ips(raw) -> tuple[str, ...]:
    """Accept 'a, b', 'a\\nb' or ['a', 'b'] and return a de-duplicated tuple."""
    if raw is None:
        return ()
    if not isinstance(raw, str):
        raw = ",".join(str(x) for x in raw)
    out: list[str] = []
    for part in re.split(r"[,\s;]+", raw):
        if part and part not in out:
            out.append(part)
    return tuple(out)


def _clean_game_delays(data) -> dict[int, float]:
    out: dict[int, float] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            try:
                out[int(key)] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
    return out


def _parse_game_delays(raw: str) -> dict[int, float]:
    if not raw or not raw.strip():
        return {}
    try:
        return _clean_game_delays(json.loads(raw))
    except ValueError:
        log.warning("GAME_DELAYS is not valid JSON — ignoring.")
        return {}


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _clean_team_colors(data) -> dict[str, tuple[str, str]]:
    """{'EDM': ['#112233', '#445566']} or {'EDM': {'a':..,'b':..}} -> {'EDM': ('#112233','#445566')}.
    Unknown teams and malformed colours are dropped."""
    out: dict[str, tuple[str, str]] = {}
    if not isinstance(data, dict):
        return out
    for team, pair in data.items():
        team = str(team).strip().upper()
        if team not in TEAM_STYLES:
            continue
        if isinstance(pair, dict):
            pair = (pair.get("a"), pair.get("b"))
        try:
            a, b = pair
        except (TypeError, ValueError):
            continue
        if isinstance(a, str) and isinstance(b, str) and _HEX_RE.match(a) and _HEX_RE.match(b):
            out[team] = (a.upper(), b.upper())
    return out


def _parse_team_colors(raw: str) -> dict[str, tuple[str, str]]:
    if not raw or not raw.strip():
        return {}
    try:
        return _clean_team_colors(json.loads(raw))
    except ValueError:
        log.warning("TEAM_COLOR_OVERRIDES is not valid JSON — ignoring.")
        return {}


def _clean_id_set(values) -> set[int]:
    out: set[int] = set()
    for v in values or ():
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _format_local_time(dt_utc: datetime) -> str:
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    return dt_utc.astimezone(local_tz).strftime("%I:%M %p")


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _display_team_name(team: dict) -> str:
    place = team.get("placeName", {}).get("default", "")
    common = team.get("commonName", {}).get("default", "")
    return f"{place} {common}".strip()


def _extract_hsv(hsv_obj) -> tuple[int, int, int]:
    if hsv_obj is None:
        return 0, 0, 100
    try:
        return int(hsv_obj.hue), int(hsv_obj.saturation), int(hsv_obj.value)
    except AttributeError:
        pass
    try:
        if len(hsv_obj) >= 3:
            return int(hsv_obj[0]), int(hsv_obj[1]), int(hsv_obj[2])
    except Exception:
        pass
    return 0, 0, 100


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _poll_interval(game_state: str, config: AppConfig) -> float:
    if game_state == "CRIT":
        return config.poll_critical_seconds
    if game_state in LIVE_STATES:
        return config.poll_live_seconds
    if game_state in FINAL_STATES:
        return 0.0
    return config.poll_pregame_seconds


def _format_schedule_state(game_state: str, local_time: str) -> str:
    if game_state in LIVE_STATES:
        return " [LIVE]"
    if game_state in FINAL_STATES:
        return " [FINAL]"
    if game_state in PREGAME_STATES:
        return f" [Starts {local_time}]"
    return f" [{game_state or 'UNKNOWN'} at {local_time}]"


def _backoff(attempt: int, base: float, maximum: float) -> float:
    return random.uniform(0, min(maximum, base * (2 ** attempt)))


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_dt = parsedate_to_datetime(value)
        if retry_dt.tzinfo is None:
            retry_dt = retry_dt.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


async def _interruptible_sleep(seconds: float, shutdown: asyncio.Event) -> None:
    if seconds <= 0:
        return
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _close_device(device) -> None:
    disconnect = getattr(device, "disconnect", None)
    if disconnect is not None:
        try:
            await disconnect()
        except Exception:
            pass


# ── NHL API ──────────────────────────────────────────────────────────────────

class NhlApiClient:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "NhlApiClient":
        self._session = aiohttp.ClientSession(headers={"User-Agent": "nhl-goal-light/3.0"})
        return self

    async def __aexit__(self, *_) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_json(self, path: str) -> dict:
        if self._session is None:
            raise RuntimeError("NhlApiClient must be used as an async context manager.")

        cfg = self._config
        url = f"{NHL_API_BASE}{path}"
        attempts = max(1, cfg.max_retries + 1)      # first try + retries
        last_exc: Exception = RuntimeError("No attempts made.")

        for attempt in range(attempts):
            try:
                # timeout is read per request so GUI changes apply instantly
                timeout = aiohttp.ClientTimeout(total=cfg.request_timeout)
                async with self._session.get(url, timeout=timeout) as resp:
                    if resp.status == 404:
                        return {}
                    if resp.status in RETRYABLE_STATUS:
                        raise _RetryableError(
                            resp.status, _parse_retry_after(resp.headers.get("Retry-After")))
                    resp.raise_for_status()
                    return await resp.json(content_type=None)
            except _RetryableError as exc:
                last_exc = exc
                wait = (min(exc.retry_after, cfg.backoff_max) if exc.retry_after is not None
                        else _backoff(attempt, cfg.backoff_base, cfg.backoff_max))
                reason = f"HTTP {exc.status}"
            except aiohttp.ClientResponseError:
                raise                                   # non-retryable 4xx: fail fast
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                wait = _backoff(attempt, cfg.backoff_base, cfg.backoff_max)
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__

            if attempt + 1 >= attempts:
                break
            log.warning("NHL API %s on %s (attempt %d/%d) — retrying in %.1fs.",
                        reason, path, attempt + 1, attempts, wait)
            await asyncio.sleep(wait)

        log.error("Giving up on %s after %d attempts.", path, attempts)
        raise last_exc

    async def fetch_todays_games(self) -> list[GameInfo]:
        today = datetime.now(EASTERN_TZ).date().isoformat()
        payload = await self._get_json(f"/schedule/{today}")
        games: list[GameInfo] = []
        for day in payload.get("gameWeek", []):
            if day.get("date") != today:
                continue
            for g in day.get("games", []):
                away, home = g.get("awayTeam", {}), g.get("homeTeam", {})
                games.append(GameInfo(
                    game_id=g["id"],
                    away_abbrev=away.get("abbrev", "???"),
                    home_abbrev=home.get("abbrev", "???"),
                    away_name=_display_team_name(away),
                    home_name=_display_team_name(home),
                    start_time_utc=_parse_utc(g["startTimeUTC"]),
                    game_state=g.get("gameState", ""),
                ))
        games.sort(key=lambda game: game.start_time_utc)
        return games

    async def fetch_game_status(self, game_id: int) -> Optional[GameStatus]:
        payload = await self._get_json(f"/gamecenter/{game_id}/boxscore")
        if not payload:
            return None
        away, home = payload.get("awayTeam", {}), payload.get("homeTeam", {})
        return GameStatus(
            away_abbrev=away.get("abbrev", "???"),
            home_abbrev=home.get("abbrev", "???"),
            game_state=payload.get("gameState", ""),
            away_score=away.get("score"),
            home_score=home.get("score"),
        )


# ── bulbs ────────────────────────────────────────────────────────────────────

class KasaBulb:
    """One physical bulb. Every public method takes the bulb's own I/O lock."""

    def __init__(self, ip: str, config: AppConfig) -> None:
        self.ip = ip
        self._config = config
        self._bulb: Optional[IotBulb] = None
        self._light = None
        self._last_update = 0.0
        self._raw_ok: Optional[bool] = None   # None = untested
        self._lat: dict[tuple, float] = {}    # measured command time per colour, kept across flashes
        self._lock = asyncio.Lock()

    # -- connection management --------------------------------------------

    async def _connect(self, force: bool = False) -> None:
        now = time.monotonic()
        if (self._bulb is not None and not force
                and (now - self._last_update) <= STALE_SECONDS):
            return
        if self._bulb is None:
            bulb = IotBulb(self.ip)
            try:
                await bulb.update()
            except Exception:
                await _close_device(bulb)
                raise
            self._bulb = bulb
        else:
            await self._bulb.update()
        self._light = self._bulb.modules.get("Light")
        if self._light is None:
            await self._reset()
            raise RuntimeError(f"Bulb at {self.ip} has no Light module.")
        self._last_update = now

    async def _reset(self) -> None:
        bulb, self._bulb, self._light = self._bulb, None, None
        if bulb is not None:
            await _close_device(bulb)

    # -- low-level setters (older python-kasa versions lack `transition`) --

    async def _set_hsv(self, h: int, s: int, v: int, ms: int) -> None:
        try:
            await self._light.set_hsv(h, s, v, transition=ms)
        except TypeError:
            await self._light.set_hsv(h, s, v)

    async def _set_brightness(self, b: int, ms: int) -> None:
        try:
            await self._light.set_brightness(b, transition=ms)
        except TypeError:
            await self._light.set_brightness(b)

    async def _set_color_temp(self, ct: int, b: int, ms: int) -> None:
        try:
            await self._light.set_color_temp(ct, brightness=b, transition=ms)
        except TypeError:
            try:
                await self._light.set_color_temp(ct, brightness=b)
            except TypeError:
                await self._light.set_color_temp(ct)

    async def _transition(self, **state) -> bool:
        """Send the bulb's native transition_light_state command.

        ignore_default=1 stops the bulb applying its stored 'default' preset
        first (that preset showed up as a split-second flash of the default
        colour between steps).  Returns False if the raw command isn't usable
        here, so callers fall back to the normal python-kasa API."""
        if self._raw_ok is False:
            return False
        try:
            await self._bulb._query_helper(
                LIGHT_SERVICE, "transition_light_state",
                {"ignore_default": 1, "mode": "normal", **state})
            self._raw_ok = True
            return True
        except (asyncio.TimeoutError, OSError):
            raise                                   # network problem, not unsupported
        except Exception as exc:
            if self._raw_ok:                        # worked before -> genuine failure
                raise
            self._raw_ok = False
            log.warning("Bulb %s: native command unavailable (%s) — using the standard API.",
                        self.ip, exc)
            return False

    async def _show(self, shade: Shade, snap: BulbSnapshot, dim: bool = False) -> None:
        ms = self._config.flash_transition_ms
        v = _clamp(shade.v, 1, 100)
        if not snap.supports_color:                 # white-only bulb: alternate bright / dim
            v = 25 if dim else 100
            if not await self._transition(on_off=1, brightness=v, transition_period=ms):
                await self._set_brightness(v, ms)
            return
        if shade.kind == "white":
            ok = await self._transition(on_off=1, color_temp=shade.kelvin, hue=0,
                                        saturation=0, brightness=v, transition_period=ms)
            if not ok:
                await self._set_hsv(0, 0, v, ms)
            return
        ok = await self._transition(on_off=1, color_temp=0, hue=shade.h,
                                    saturation=shade.s, brightness=v, transition_period=ms)
        if not ok:
            await self._set_hsv(shade.h, shade.s, v, ms)

    # -- public API ---------------------------------------------------------

    async def capture(self) -> BulbSnapshot:
        async with self._lock:
            try:
                await self._connect(force=True)
                light = self._light
                hue, sat, value = _extract_hsv(getattr(light, "hsv", None))
                color_temp = getattr(light, "color_temp", None) or None
                color_mode = getattr(light, "color_mode", None)
                brightness = getattr(light, "brightness", value if value > 0 else 100)
                snap = BulbSnapshot(
                    is_on=bool(self._bulb.is_on),
                    brightness=_clamp(brightness, 1, 100),
                    hue=hue, saturation=sat,
                    color_temp=color_temp,
                    color_mode=str(color_mode) if color_mode is not None else None,
                    supports_color=hasattr(light, "set_hsv"),
                    supports_color_temp=bool(
                        hasattr(light, "set_color_temp")
                        and getattr(self._bulb, "is_variable_color_temp", False)),
                )
            except Exception:
                await self._reset()
                raise
        log.info("Captured %s: on=%s brightness=%s hue=%s sat=%s color=%s color_temp=%s",
                 self.ip, snap.is_on, snap.brightness, snap.hue, snap.saturation,
                 snap.supports_color, snap.color_temp)
        return snap

    async def restore(self, snap: BulbSnapshot) -> None:
        cfg = self._config
        ms = cfg.restore_transition_ms
        async with self._lock:
            try:
                await self._connect()
                if not snap.is_on:
                    if not await self._transition(on_off=0, transition_period=ms):
                        await self._bulb.turn_off()
                    return
                if snap.color_temp:
                    ok = await self._transition(on_off=1, color_temp=snap.color_temp,
                                                brightness=snap.brightness, transition_period=ms)
                    if not ok:
                        await self._set_color_temp(snap.color_temp, snap.brightness, ms)
                elif snap.supports_color:
                    ok = await self._transition(on_off=1, color_temp=0, hue=snap.hue,
                                                saturation=snap.saturation,
                                                brightness=snap.brightness, transition_period=ms)
                    if not ok:
                        await self._set_hsv(snap.hue, snap.saturation, snap.brightness, ms)
                else:
                    ok = await self._transition(on_off=1, brightness=snap.brightness,
                                                transition_period=ms)
                    if not ok:
                        await self._set_brightness(snap.brightness, ms)
                        await self._bulb.turn_on()
            except Exception as exc:
                log.error("Failed to restore bulb %s (%s) — will reconnect next time.", self.ip, exc)
                await self._reset()

    async def flash(self, style: TeamStyle, snap: BulbSnapshot) -> None:
        """Alternate the team's two colours for `flash_duration` seconds, evenly.

        Every colour change sits on a fixed timeline (one beat apart).  A command
        takes time to reach the bulb and bulbs can answer one colour slower than the
        other, so each colour's latency is measured separately and its command is
        sent that much early, which makes the visible change land on the beat.  If
        the bulb/Wi-Fi can't keep up with the configured beat, the beat is raised to
        what it can do so both colours still get exactly equal time.  After a stall
        we never "catch up" with a burst of commands.
        """
        cfg = self._config
        shades = style.palette()
        sig = [(sh.kind, sh.h, sh.s, sh.kelvin) for sh in shades]
        async with self._lock:
            try:
                await self._connect()
                loop = asyncio.get_running_loop()
                end = loop.time() + cfg.flash_duration
                lat = self._lat                     # command time per shade; persists between flashes

                def est(k: int) -> float:
                    if sig[k] in lat:
                        return lat[sig[k]]
                    return statistics.mean(lat.values()) if lat else 0.0

                def beat() -> float:
                    slowest = max((lat[x] for x in sig if x in lat), default=0.0)
                    return max(cfg.flash_interval, 1.3 * slowest)

                grid = loop.time()                  # when the next colour should become visible
                i = 0
                while grid < end:
                    k = i % 2
                    i += 1
                    wait = (grid - est(k) / 2) - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    t0 = loop.time()
                    await self._show(shades[k], snap, dim=(k == 1))
                    took = loop.time() - t0
                    prev = lat.get(sig[k])
                    # clamp outliers so one stalled reply can't distort later beats
                    lat[sig[k]] = took if prev is None else 0.6 * prev + 0.4 * min(took, 2 * prev + 0.05)
                    applied = t0 + lat[sig[k]] / 2  # when the bulb actually changed
                    grid = max(grid, applied) + beat()
                tail = grid - loop.time()           # let the last colour play its full beat
                if tail > 0:
                    await asyncio.sleep(min(tail, 5.0))
                used = beat()
                log.info("Flash timing on %s: A %.0fms, B %.0fms, beat %.2fs", self.ip,
                         est(0) * 1000, est(1) * 1000, used)
                if used > cfg.flash_interval + 0.01:
                    log.warning("Bulb %s needs ~%.0f ms per command (Wi-Fi latency) — beat raised from "
                                "%.2fs to %.2fs so both colours stay even.",
                                self.ip, max(est(0), est(1)) * 1000, cfg.flash_interval, used)
            except Exception as exc:
                log.error("Flash failed on bulb %s (%s) — will reconnect next time.", self.ip, exc)
                await self._reset()

    async def close(self) -> None:
        async with self._lock:
            await self._reset()


class BulbController:
    """Drives one or more bulbs in parallel; one dead bulb never blocks the rest."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._bulbs = [KasaBulb(ip, config) for ip in config.bulb_ips]

    def set_ips(self, ips: tuple[str, ...]) -> None:
        """Swap the bulb list live (call from the event-loop thread)."""
        existing = {b.ip: b for b in self._bulbs}
        self._bulbs = [existing.pop(ip, None) or KasaBulb(ip, self._config) for ip in ips]
        for old in existing.values():
            asyncio.get_running_loop().create_task(old.close())
        log.info("Bulbs now: %s", ", ".join(ips) or "(none)")

    async def capture_state(
        self, fallback: Optional[dict[str, BulbSnapshot]] = None
    ) -> dict[str, BulbSnapshot]:
        bulbs = list(self._bulbs)
        results = await asyncio.gather(*(b.capture() for b in bulbs), return_exceptions=True)
        snaps: dict[str, BulbSnapshot] = {}
        errors: list[str] = []
        for bulb, result in zip(bulbs, results):
            if isinstance(result, BaseException):
                if fallback and bulb.ip in fallback:
                    snaps[bulb.ip] = fallback[bulb.ip]
                errors.append(f"{bulb.ip}: {result}")
            else:
                snaps[bulb.ip] = result
        if not snaps:
            raise RuntimeError("Could not reach any bulb — " + "; ".join(errors))
        for err in errors:
            log.error("Bulb unreachable — %s", err)
        return snaps

    async def restore_state(self, snaps: dict[str, BulbSnapshot]) -> None:
        bulbs = [b for b in self._bulbs if b.ip in snaps]
        await asyncio.gather(*(b.restore(snaps[b.ip]) for b in bulbs))

    async def flash_team(self, team: str, snaps: dict[str, BulbSnapshot]) -> None:
        style = self._config.style_for(team)
        bulbs = [b for b in self._bulbs if b.ip in snaps]
        await asyncio.gather(*(b.flash(style, snaps[b.ip]) for b in bulbs))

    async def shutdown(self) -> None:
        await asyncio.gather(*(b.close() for b in self._bulbs), return_exceptions=True)


async def probe_bulb(ip: str, timeout: float = 8.0) -> str:
    """Connect to a bulb and return its model name (used by the GUI's Test button)."""
    bulb = IotBulb(ip)
    try:
        await asyncio.wait_for(bulb.update(), timeout=timeout)
        return str(bulb.model)
    finally:
        await _close_device(bulb)


# ── flash worker ─────────────────────────────────────────────────────────────

async def _safe_restore(bulb: BulbController, snaps: dict[str, BulbSnapshot]) -> None:
    try:
        await bulb.restore_state(snaps)
    except Exception:
        log.exception("Bulb restore failed.")


async def _flash_burst(
    queue: "asyncio.Queue[Optional[GoalEvent]]",
    bulb: BulbController,
    snaps: dict[str, BulbSnapshot],
    config: AppConfig,
    event: GoalEvent,
) -> bool:
    """Flash `event`, then keep flashing while more goals arrive within the quiet
    window. Returns True if the shutdown sentinel was seen."""
    while True:
        log.info("Flash: game=%s team=%s", event.game_id, event.scoring_team)
        await bulb.flash_team(event.scoring_team, snaps)
        queue.task_done()
        try:
            nxt = await asyncio.wait_for(queue.get(), timeout=config.flash_quiet_window)
        except asyncio.TimeoutError:
            return False
        if nxt is None:
            queue.task_done()
            return True
        event = nxt


async def flash_worker(
    queue: "asyncio.Queue[Optional[GoalEvent]]",
    bulb: BulbController,
    fallback: dict[str, BulbSnapshot],
    config: AppConfig,
) -> None:
    while True:
        event = await queue.get()
        if event is None:
            queue.task_done()
            return
        # Re-capture so we restore what the bulb looks like *now*, not at start-up.
        try:
            snaps = await bulb.capture_state(fallback)
            fallback = snaps
        except Exception as exc:
            if not fallback:
                log.error("No reachable bulb (%s) — skipping %s flash.", exc, event.scoring_team)
                queue.task_done()
                continue
            log.error("Could not capture bulb state (%s) — using last known snapshot.", exc)
            snaps = fallback
        try:
            stop = await _flash_burst(queue, bulb, snaps, config, event)
        finally:
            await _safe_restore(bulb, snaps)
        if stop:
            return


# ── game monitor ─────────────────────────────────────────────────────────────

async def _deliver_goal(
    event: GoalEvent,
    delay: float,
    tag: str,
    queue: "asyncio.Queue[Optional[GoalEvent]]",
    shutdown: asyncio.Event,
    config: AppConfig,
) -> None:
    await _interruptible_sleep(delay, shutdown)
    if shutdown.is_set():
        return
    if event.game_id in config.muted_games:          # muting applies instantly
        log.info("[%s] %s goal not flashed (game muted).", tag, event.scoring_team)
        return
    log.info("[%s] Releasing %s flash (after %.0fs TV delay).", tag, event.scoring_team, delay)
    await queue.put(event)


async def monitor_game(
    game: GameInfo,
    nhl_client: NhlApiClient,
    goal_queue: "asyncio.Queue[Optional[GoalEvent]]",
    shutdown: asyncio.Event,
    config: AppConfig,
) -> None:
    tag = f"{game.away_abbrev}@{game.home_abbrev}"
    if game.game_id in config.game_delays:
        kind = "custom"
    elif config.is_primary_game(game):
        kind = f"{config.primary_team} game"
    else:
        kind = "default"
    log.info("Tracking %s [%s] — TV delay %.0fs (%s)",
             tag, _format_local_time(game.start_time_utc), config.delay_for(game), kind)

    pending: set[asyncio.Task] = set()

    def schedule(team: str, count: int) -> None:
        # Delay is looked up at goal time (so GUI edits apply to the next goal), and
        # each goal waits in its own task so polling is never blocked.
        delay = config.delay_for(game)
        for _ in range(count):
            task = asyncio.create_task(_deliver_goal(
                GoalEvent(game.game_id, team), delay, tag, goal_queue, shutdown, config))
            pending.add(task)
            task.add_done_callback(pending.discard)

    cancelled = False
    try:
        await _track_game(game, nhl_client, shutdown, config, tag, schedule)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        leftovers = list(pending)
        if leftovers:
            if shutdown.is_set() or cancelled:
                for task in leftovers:
                    task.cancel()
            await asyncio.gather(*leftovers, return_exceptions=True)


async def _track_game(
    game: GameInfo,
    nhl_client: NhlApiClient,
    shutdown: asyncio.Event,
    config: AppConfig,
    tag: str,
    schedule: Callable[[str, int], None],
) -> None:
    wake = game.start_time_utc - timedelta(seconds=config.pregame_buffer_seconds)
    wait = (wake - datetime.now(timezone.utc)).total_seconds()
    if wait > 0:
        log.info("[%s] Sleeping %d min until %d min before puck drop.",
                 tag, int(wait / 60), config.pregame_buffer_seconds // 60)
        await _interruptible_sleep(wait, shutdown)

    last: Optional[GameStatus] = None
    waiting_logged = False

    def expired() -> bool:
        return datetime.now(timezone.utc) > game.start_time_utc + GIVE_UP_AFTER

    while not shutdown.is_set():
        try:
            status = await nhl_client.fetch_game_status(game.game_id)
        except Exception as exc:
            log.error("[%s] Failed to fetch game status: %s", tag, exc)
            await _interruptible_sleep(config.poll_error_seconds, shutdown)
            continue

        if status is None:
            if last is None and expired():
                log.info("[%s] No data %dh after start — giving up.", tag, GIVE_UP_AFTER.seconds // 3600)
                return
            await _interruptible_sleep(config.poll_error_seconds, shutdown)
            continue

        if status.away_score is None or status.home_score is None:
            if status.game_state in FINAL_STATES:
                log.info("[%s] Game ended (no score payload).", tag)
                return
            if last is None and expired():
                log.info("[%s] Never started (postponed?) — giving up.", tag)
                return
            if not waiting_logged and datetime.now(timezone.utc) >= game.start_time_utc:
                log.info("[%s] Waiting for score data...", tag)
                waiting_logged = True
            await _interruptible_sleep(_poll_interval(status.game_state, config), shutdown)
            continue

        a, h = status.away_score, status.home_score
        if last is None:
            log.info("[%s] Game started: %s %d – %s %d", tag,
                     status.away_abbrev, a, status.home_abbrev, h)
        else:
            for team, delta in (
                (status.away_abbrev, max(a - (last.away_score or 0), 0)),
                (status.home_abbrev, max(h - (last.home_score or 0), 0)),
            ):
                if delta:
                    log.info("[%s] GOAL x%d — %s! Score: %s %d – %s %d", tag, delta, team,
                             status.away_abbrev, a, status.home_abbrev, h)
                    schedule(team, delta)
        last = status

        if status.game_state in FINAL_STATES:
            log.info("[%s] Final: %s %d – %s %d", tag,
                     status.away_abbrev, a, status.home_abbrev, h)
            return

        await _interruptible_sleep(_poll_interval(status.game_state, config), shutdown)


# ── daemon: every game, every day, live-configurable ─────────────────────────

async def _daemon_loop(
    config: AppConfig,
    nhl_client: NhlApiClient,
    goal_queue: "asyncio.Queue[Optional[GoalEvent]]",
    shutdown: asyncio.Event,
) -> None:
    tracked: dict[int, asyncio.Task] = {}
    done_ids: set[int] = set()
    last_summary: Optional[tuple[int, int]] = None
    log.info("Auto mode: tracking every NHL game, every day. Settings apply instantly.")
    try:
        while not shutdown.is_set():
            try:
                games = await nhl_client.fetch_todays_games()
            except Exception as exc:
                log.error("Could not fetch schedule: %s — retrying in 30s.", exc)
                await _interruptible_sleep(30, shutdown)
                continue

            for game in games:
                if game.game_id in tracked or game.game_id in done_ids:
                    continue
                if game.game_state in FINAL_STATES:
                    done_ids.add(game.game_id)
                    continue
                tracked[game.game_id] = asyncio.create_task(
                    monitor_game(game, nhl_client, goal_queue, shutdown, config))

            for gid, task in list(tracked.items()):
                if task.done():
                    del tracked[gid]
                    done_ids.add(gid)
                    if not task.cancelled() and task.exception() is not None:
                        log.error("Monitor for game %s crashed: %r", gid, task.exception())

            summary = (len(tracked), len(done_ids))
            if summary != last_summary:
                log.info("Tracking %d game(s); %d finished today.", *summary)
                last_summary = summary

            await _interruptible_sleep(SCHEDULE_REFRESH_SECONDS, shutdown)
    finally:
        for task in tracked.values():
            task.cancel()
        await asyncio.gather(*tracked.values(), return_exceptions=True)


def start_command_listener(
    loop: asyncio.AbstractEventLoop,
    handler: Callable[[dict], None],
    shutdown: asyncio.Event,
) -> None:
    """Read JSON lines from stdin (sent by the web server) on a helper thread.
    EOF means the server went away, so we shut down instead of becoming an orphan."""
    def reader() -> None:
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                loop.call_soon_threadsafe(handler, msg)
        except Exception:
            pass
        finally:
            try:
                loop.call_soon_threadsafe(shutdown.set)
            except RuntimeError:
                pass

    threading.Thread(target=reader, name="stdin-commands", daemon=True).start()


# ── CLI / entry point ────────────────────────────────────────────────────────

def select_games(games: list[GameInfo], config: AppConfig) -> list[GameInfo]:
    if not games:
        print("No games scheduled for today.")
        return []

    print("\nToday's NHL Games")
    print("-" * 52)
    for idx, game in enumerate(games, 1):
        time_str = _format_local_time(game.start_time_utc)
        state_str = _format_schedule_state(game.game_state, time_str)
        star = "*" if config.is_primary_game(game) else " "
        print(f" {idx:>2}.{star}{game.away_name} ({game.away_abbrev}) @ "
              f"{game.home_name} ({game.home_abbrev}){state_str}  [delay {config.delay_for(game):.0f}s]")
    print(f"\n * = {config.primary_team} game (primary team)")

    while True:
        raw = input("\nTrack which games? (e.g. 1,3 | 'all' | 0 to exit): ").strip().lower()
        if raw in {"0", "q", "quit", "exit"}:
            return []
        if raw == "all":
            return list(games)
        try:
            indexes = sorted({int(p.strip()) for p in raw.split(",") if p.strip()})
        except ValueError:
            print("  Invalid input — enter numbers like 1,3 or 'all'.")
            continue
        selected = [games[i - 1] for i in indexes if 1 <= i <= len(games)]
        if not selected or len(selected) != len(indexes):
            print("  One or more selections are out of range — try again.")
            continue
        return selected


def install_signal_handlers(shutdown: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except (NotImplementedError, OSError):
            pass


async def run() -> None:
    config = AppConfig.from_env()
    daemon = os.getenv("DAEMON", "") == "1"

    async with NhlApiClient(config) as nhl_client:
        selected: list[GameInfo] = []
        if not daemon:
            log.info("Fetching today's games...")
            games = await nhl_client.fetch_todays_games()
            game_ids_env = os.getenv("GAME_IDS", "").strip()
            if game_ids_env:
                id_set = {s.strip() for s in game_ids_env.split(",") if s.strip()}
                selected = [g for g in games if str(g.game_id) in id_set]
                if not selected:
                    log.error("GAME_IDS=%r matched no games in today's schedule.", game_ids_env)
                    return
                log.info("Game IDs from env: %s", ", ".join(str(g.game_id) for g in selected))
            else:
                selected = select_games(games, config)
                if not selected:
                    print("No games selected. Exiting.")
                    return

        log.info("Primary team: %s (delay %.0fs) — other games: %.0fs",
                 config.primary_team, config.primary_delay_seconds, config.default_delay_seconds)

        bulb = BulbController(config)
        try:
            log.info("Capturing bulb state (%d bulb%s)...",
                     len(config.bulb_ips), "" if len(config.bulb_ips) == 1 else "s")
            try:
                snapshots = await bulb.capture_state()
            except Exception as exc:
                if not daemon:
                    log.error("%s", exc)
                    return
                log.error("%s — will keep trying whenever a goal is scored.", exc)
                snapshots = {}

            shutdown = asyncio.Event()
            install_signal_handlers(shutdown)
            goal_queue: asyncio.Queue[Optional[GoalEvent]] = asyncio.Queue()
            worker = asyncio.create_task(flash_worker(goal_queue, bulb, snapshots, config))

            try:
                if daemon:
                    def on_command(msg: dict) -> None:
                        if msg.get("type") != "config":
                            return
                        changed = config.apply(msg.get("settings") or {})
                        if "bulb_ips" in changed:
                            bulb.set_ips(config.bulb_ips)
                        if changed:
                            log.info("Settings updated: %s", ", ".join(sorted(changed)))

                    start_command_listener(asyncio.get_running_loop(), on_command, shutdown)
                    await _daemon_loop(config, nhl_client, goal_queue, shutdown)
                else:
                    monitors = [
                        asyncio.create_task(monitor_game(g, nhl_client, goal_queue, shutdown, config))
                        for g in selected
                    ]
                    log.info("Tracking %d game(s). Press Ctrl+C to stop.", len(selected))
                    try:
                        await asyncio.gather(*monitors)
                        if not shutdown.is_set():
                            await goal_queue.join()
                    finally:
                        shutdown.set()
                        for task in monitors:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*monitors, return_exceptions=True)
            finally:
                shutdown.set()
                await goal_queue.put(None)
                await worker
        finally:
            await bulb.shutdown()

    log.info("Done.")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logging.info("Interrupted by user.")


if __name__ == "__main__":
    main()
