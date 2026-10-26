"""
NHL Goal Light Controller
━━━━━━━━━━━━━━━━━━━━━━━━

Monitors NHL games in real time and flashes one or more TP-Link Kasa smart
bulbs in the scoring team's colours whenever a goal is scored.

TV delay
--------
Every game gets a delay (seconds) before the flash fires, so the light lines
up with your stream:

  * games involving the PRIMARY team (default MTL)  -> PRIMARY_DELAY_SECONDS (35)
  * every other game                                 -> DEFAULT_DELAY_SECONDS (45)
  * an explicit per-game override (GAME_DELAYS json) -> wins over both
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import signal
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

DEFAULT_PRIMARY_TEAM = "MTL"
DEFAULT_PRIMARY_DELAY = 35.0
DEFAULT_OTHER_DELAY = 45.0


# ── team data ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TeamPalette:
    primary: tuple[int, int, int]    # HSV (h 0-360, s 0-100, v 0-100)
    secondary: tuple[int, int, int]


TEAM_COLORS: dict[str, TeamPalette] = {
    "ANA": TeamPalette((18, 99, 99), (196, 6, 68)),     # Orange, Silver
    "BOS": TeamPalette((42, 92, 99), (0, 0, 7)),        # Gold, Black
    "BUF": TeamPalette((219, 100, 53), (42, 92, 99)),   # Navy, Gold
    "CGY": TeamPalette((352, 100, 82), (42, 92, 99)),   # Red, Gold
    "CAR": TeamPalette((353, 92, 81), (0, 0, 7)),       # Red, Black
    "CHI": TeamPalette((350, 95, 81), (0, 0, 7)),       # Red, Black
    "COL": TeamPalette((341, 66, 44), (206, 76, 57)),   # Burgundy, Blue
    "CBJ": TeamPalette((213, 100, 33), (353, 92, 81)),  # Navy, Red
    "DAL": TeamPalette((161, 100, 41), (0, 0, 7)),      # Green, Black
    "DET": TeamPalette((353, 92, 81), (0, 0, 100)),     # Red, White
    "EDM": TeamPalette((18, 99, 99), (215, 94, 26)),    # Orange, Navy
    "FLA": TeamPalette((215, 94, 26), (350, 92, 78)),   # Navy, Red
    "LAK": TeamPalette((0, 0, 7), (196, 6, 68)),        # Black, Silver
    "MIN": TeamPalette((157, 70, 28), (350, 85, 65)),   # Green, Red
    "MTL": TeamPalette((354, 83, 69), (223, 82, 54)),   # Red, Blue
    "NSH": TeamPalette((41, 89, 100), (215, 94, 26)),   # Gold, Navy
    "NJD": TeamPalette((353, 92, 81), (0, 0, 7)),       # Red, Black
    "NYI": TeamPalette((208, 100, 61), (18, 99, 99)),   # Blue, Orange
    "NYR": TeamPalette((220, 100, 66), (353, 92, 81)),  # Blue, Red
    "OTT": TeamPalette((353, 84, 77), (37, 52, 72)),    # Red, Gold
    "PHI": TeamPalette((17, 99, 97), (0, 0, 7)),        # Orange, Black
    "PIT": TeamPalette((0, 0, 7), (42, 92, 99)),        # Black, Gold
    "SJS": TeamPalette((184, 100, 46), (29, 100, 92)),  # Teal, Orange
    "SEA": TeamPalette((207, 100, 16), (180, 29, 85)),  # Deep Navy, Ice Blue
    "STL": TeamPalette((219, 100, 53), (42, 92, 99)),   # Blue, Gold
    "TBL": TeamPalette((217, 100, 41), (0, 0, 100)),    # Blue, White
    "TOR": TeamPalette((219, 100, 36), (0, 0, 100)),    # Blue, White
    "UTA": TeamPalette((216, 99, 31), (197, 44, 73)),   # Navy, Teal
    "VAN": TeamPalette((219, 100, 36), (156, 100, 53)), # Blue, Green
    "VGK": TeamPalette((41, 50, 71), (206, 29, 28)),    # Muted Gold, Steel Grey
    "WSH": TeamPalette((350, 85, 77), (215, 94, 26)),   # Red, Navy
    "WPG": TeamPalette((215, 94, 26), (210, 43, 38)),   # Navy, Aviator Blue
}
DEFAULT_PALETTE = TeamPalette((0, 100, 100), (0, 0, 100))

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
    "UTA": "Utah Hockey Club", "VAN": "Vancouver Canucks", "VGK": "Vegas Golden Knights",
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


@dataclass(frozen=True)
class AppConfig:
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
    flash_transition_ms: int = 120
    primary_team: str = DEFAULT_PRIMARY_TEAM
    primary_delay_seconds: float = DEFAULT_PRIMARY_DELAY
    default_delay_seconds: float = DEFAULT_OTHER_DELAY
    game_delays: dict = field(default_factory=dict)   # {game_id: seconds}

    def delay_for(self, game: GameInfo) -> float:
        """TV delay (s) for a game: explicit override > primary team > default."""
        override = self.game_delays.get(game.game_id)
        if override is not None:
            return max(0.0, float(override))
        if self.is_primary_game(game):
            return max(0.0, self.primary_delay_seconds)
        return max(0.0, self.default_delay_seconds)

    def is_primary_game(self, game: GameInfo) -> bool:
        return self.primary_team in (game.away_abbrev, game.home_abbrev)

    @classmethod
    def from_env(cls) -> "AppConfig":
        ips = parse_bulb_ips(os.getenv("BULB_IP", ""))
        if not ips:
            raise SystemExit(
                "ERROR: Set the BULB_IP environment variable to your bulb's IP address "
                "(comma-separate several), e.g.  export BULB_IP=192.168.1.x"
            )
        kwargs = {name: _env(var, cast, getattr(cls, name))
                  for name, (var, cast) in NUMERIC_ENV_FIELDS.items()}
        primary = (os.getenv("PRIMARY_TEAM", "") or DEFAULT_PRIMARY_TEAM).strip().upper()
        if primary not in TEAM_COLORS:
            log.warning("Unknown PRIMARY_TEAM %r — falling back to %s.", primary, DEFAULT_PRIMARY_TEAM)
            primary = DEFAULT_PRIMARY_TEAM
        return cls(
            bulb_ips=ips,
            primary_team=primary,
            game_delays=_parse_game_delays(os.getenv("GAME_DELAYS", "")),
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


def _parse_game_delays(raw: str) -> dict[int, float]:
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("GAME_DELAYS is not valid JSON — ignoring.")
        return {}
    out: dict[int, float] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            try:
                out[int(key)] = max(0.0, float(value))
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
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._config.request_timeout),
            headers={"User-Agent": "nhl-goal-light/2.0"},
        )
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
                async with self._session.get(url) as resp:
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

    async def _turn_off_instant(self) -> None:
        try:
            await self._bulb.turn_off(transition=0)
        except TypeError:
            await self._bulb.turn_off()

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
        async with self._lock:
            try:
                await self._connect()
                if not snap.is_on:
                    await self._bulb.turn_off()
                    return
                if snap.color_temp:
                    await self._set_color_temp(snap.color_temp, snap.brightness,
                                               cfg.restore_transition_ms)
                elif snap.supports_color:
                    await self._set_hsv(snap.hue, snap.saturation, snap.brightness,
                                        cfg.restore_transition_ms)
                else:
                    await self._set_brightness(snap.brightness, cfg.restore_transition_ms)
                await self._bulb.turn_on()
            except Exception as exc:
                log.error("Failed to restore bulb %s (%s) — will reconnect next time.", self.ip, exc)
                await self._reset()

    async def flash(self, palette: TeamPalette, snap: BulbSnapshot) -> None:
        cfg = self._config
        async with self._lock:
            try:
                await self._connect()
                await self._bulb.turn_on()
                loop = asyncio.get_running_loop()
                tick = loop.time()
                end = tick + cfg.flash_duration
                use_primary = True
                while loop.time() < end:
                    h, s, v = palette.primary if use_primary else palette.secondary
                    if v == 0:
                        await self._turn_off_instant()
                    elif snap.supports_color:
                        await self._set_hsv(h, s, _clamp(v, 1, 100), cfg.flash_transition_ms)
                    else:
                        await self._set_brightness(_clamp(v, 1, 100), cfg.flash_transition_ms)
                    use_primary = not use_primary
                    # fixed cadence: network latency doesn't stretch the strobe
                    tick += cfg.flash_interval
                    await asyncio.sleep(max(0.0, tick - loop.time()))
                await self._bulb.turn_on()
            except Exception as exc:
                log.error("Flash failed on bulb %s (%s) — will reconnect next time.", self.ip, exc)
                await self._reset()

    async def close(self) -> None:
        async with self._lock:
            await self._reset()


class BulbController:
    """Drives one or more bulbs in parallel; one dead bulb never blocks the rest."""

    def __init__(self, config: AppConfig) -> None:
        self._bulbs = [KasaBulb(ip, config) for ip in config.bulb_ips]

    async def capture_state(
        self, fallback: Optional[dict[str, BulbSnapshot]] = None
    ) -> dict[str, BulbSnapshot]:
        results = await asyncio.gather(*(b.capture() for b in self._bulbs),
                                       return_exceptions=True)
        snaps: dict[str, BulbSnapshot] = {}
        errors: list[str] = []
        for bulb, result in zip(self._bulbs, results):
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
        await asyncio.gather(*(b.restore(snaps[b.ip]) for b in self._bulbs if b.ip in snaps))

    async def flash_team(self, team: str, snaps: dict[str, BulbSnapshot]) -> None:
        palette = TEAM_COLORS.get(team, DEFAULT_PALETTE)
        await asyncio.gather(*(b.flash(palette, snaps[b.ip])
                               for b in self._bulbs if b.ip in snaps))

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
        except Exception as exc:
            log.error("Could not capture bulb state (%s) — using start-up snapshot.", exc)
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
) -> None:
    await _interruptible_sleep(delay, shutdown)
    if shutdown.is_set():
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
    delay = config.delay_for(game)
    if game.game_id in config.game_delays:
        kind = "custom"
    elif config.is_primary_game(game):
        kind = f"{config.primary_team} game"
    else:
        kind = "default"
    log.info("Tracking %s [%s] — TV delay %.0fs (%s)",
             tag, _format_local_time(game.start_time_utc), delay, kind)

    pending: set[asyncio.Task] = set()

    def schedule(team: str, count: int) -> None:
        # Delayed goals run as their own tasks so polling is never blocked.
        for _ in range(count):
            task = asyncio.create_task(
                _deliver_goal(GoalEvent(game.game_id, team), delay, tag, goal_queue, shutdown))
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

    while not shutdown.is_set():
        try:
            status = await nhl_client.fetch_game_status(game.game_id)
        except Exception as exc:
            log.error("[%s] Failed to fetch game status: %s", tag, exc)
            await _interruptible_sleep(config.poll_error_seconds, shutdown)
            continue

        if status is None:
            await _interruptible_sleep(config.poll_error_seconds, shutdown)
            continue

        if status.away_score is None or status.home_score is None:
            if status.game_state in FINAL_STATES:
                log.info("[%s] Game ended (no score payload).", tag)
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

    async with NhlApiClient(config) as nhl_client:
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
                log.error("%s", exc)
                return

            shutdown = asyncio.Event()
            install_signal_handlers(shutdown)

            goal_queue: asyncio.Queue[Optional[GoalEvent]] = asyncio.Queue()
            worker = asyncio.create_task(flash_worker(goal_queue, bulb, snapshots, config))
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
