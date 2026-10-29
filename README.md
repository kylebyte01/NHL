# NHL Goal Light 🚨

Real-time NHL goal detector that flashes your **TP-Link Kasa smart bulb(s)** in the scoring team's two colours, alternating evenly, with a mobile-friendly web GUI.

## Features

- ▶️ **Zero-touch** — starts on its own and tracks **every game, every day**. Untick a game to mute it.
- ⚡ **Instant settings** — every change in the GUI is saved and applied immediately. No Apply button, no stop/start.
- 🏒 **Live goal detection** — polls the NHL API every ~1 s (0.75 s in the final minute of a period)
- 💡 **Multi-bulb support** — one IP per line; all bulbs flash in parallel
- 🎨 **Official team colours** — every team flashes its two brand colours, alternating with exactly equal time
- 🖌 **Edit any team's colours in the GUI** — saved permanently, applied to the next goal
- ⭐ **Primary team** — pick your team (default **Montreal**); its games are starred, listed first, and use their own delay
- ⏱ **TV delay** — primary team games 60 s, all other games 45 s, per-game overrides
- 📱 **Works on phones** — the page scrolls normally, settings live in a slide-out drawer

## Quick start

```bash
pip install -r requirements.txt
python server.py          # then open http://localhost:5000
```

Add your bulb IP(s) in the sidebar. That's it — monitoring starts automatically and settings are kept in `settings.json`
(you can also seed the bulb with `BULB_IP=192.168.1.x`). Keep `server.py` running (e.g. a Raspberry Pi, always-on PC, or Replit) and it
handles every game day by itself. **Pause / Resume** is there if you want the lights off.

## TV delay

| Game                              | Delay                                   |
|-----------------------------------|-----------------------------------------|
| Involves the **primary team**     | `primary_delay_seconds` (default 60 s)  |
| Any other game                    | `default_delay_seconds` (default 45 s)  |
| Edited in the game row (yellow)   | that value, for that game only          |

Edits apply to the next goal immediately. *Reset delays* clears per-game overrides.

## Team colours

Each team flashes its two official brand colours, **alternating with exactly equal time**. A bulb can't show black (that would just be "off"), so where a
team's second colour is black the other official colour is used instead, usually white. Dark colours such as navy are lifted to a visible brightness, and white/silver
use a proper white colour temperature rather than a dim white.

| Team | Colours | Team | Colours | Team | Colours |
|------|---------|------|---------|------|---------|
| ANA | orange · gold | MIN | green · red | SEA | ice blue · red |
| BOS | gold · white | MTL | red · blue | STL | blue · yellow |
| BUF | royal blue · gold | NSH | gold · navy | TBL | blue · white |
| CGY | red · yellow | NJD | red · white | TOR | blue · white |
| CAR | red · white | NYI | blue · orange | UTA | mountain blue · white |
| CHI | red · white | NYR | blue · red | VAN | blue · green |
| COL | burgundy · blue | OTT | red · gold | VGK | gold · red |
| CBJ | navy · red | PHI | orange · white | WSH | red · navy |
| DAL | green · white | PIT | gold · white | WPG | blue · silver |
| DET | red · white | SJS | teal · white | LAK | silver · white |
| EDM | navy · orange | FLA | red · navy | | |

### Changing a team's colours

In **Team Colours & Test**, pick a team and change either colour with the colour picker. You'll see a live preview, and the change is **saved permanently**
(in `settings.json`) and used for the very next goal, with no restart. Customised teams are marked ✎ in the team lists. **Reset team** restores that team's official
colours and **Reset all colours** clears every override. **Test on bulb** flashes the team's current colours on your bulb. Bulbs can't show black, so choose
visible colours (very dark picks are brightened automatically).

Use **Test on bulb** to preview any team on your bulb. Utah is now the **Utah Mammoth** (UTA).

**Even timing:** colour changes sit on a fixed timeline. Each colour's command latency is measured separately (bulbs can answer one colour slower than the other) and
the command is sent that much early. If your Wi-Fi/bulb can't keep up with the *Colour time* you set (very fast values), it is raised automatically to what the bulb can do
so both colours still get equal time. After a stall it never "catches up" with a burst of commands. The log shows `Flash timing on <ip>: A 60ms, B 380ms, beat 0.45s`.

**No default-colour flicker:** flashes use the bulb's native command with `ignore_default` set, so the bulb never flashes its stored default state between colours, and the
light is never switched off or dimmed during a flash.

## Configuration reference

All of these are editable live in the sidebar.

| Parameter                | Default | Description                                   |
|--------------------------|---------|-----------------------------------------------|
| `primary_team`           | MTL     | Team whose games use the primary delay        |
| `primary_delay_seconds`  | 60 s    | TV delay for primary-team games               |
| `default_delay_seconds`  | 45 s    | TV delay for all other games                  |
| `flash_duration`         | 12 s    | How long the bulb flashes after a goal        |
| `flash_interval`         | 0.45 s  | Time each colour is held (raised automatically if Wi-Fi is slow) |
| `flash_quiet_window`     | 1.5 s   | Post-flash wait before restoring (bursts)     |
| `flash_transition_ms`    | 0 ms    | Colour crossfade (0 = instant)                |
| `poll_live_seconds`      | 1.0 s   | Poll rate during live play                    |
| `poll_critical_seconds`  | 0.75 s  | Poll rate in the final minute of a period     |
| `poll_pregame_seconds`   | 10 s    | Poll rate before puck drop                    |
| `poll_error_seconds`     | 5 s     | Retry wait after API failure                  |
| `request_timeout`        | 10 s    | NHL API request timeout                       |
| `max_retries`            | 4       | Retries after the first failed request        |
| `backoff_base` / `_max`  | 1 / 30 s| Exponential backoff start / cap               |
| `pregame_buffer_seconds` | 300 s   | Wake-up lead time before puck drop            |
| `restore_transition_ms`  | 150 ms  | Fade back to the original colour              |

## Command line

`python main.py` still works standalone (interactive game picker), or `GAME_IDS=...`; `DAEMON=1 BULB_IP=... python main.py` runs the auto mode without the GUI.

## License

MIT
