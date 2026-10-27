# NHL Goal Light 🚨

Real-time NHL goal detector that flashes your **TP-Link Kasa smart bulb(s)** in the scoring team's colours **and rhythm**, with a mobile-friendly web GUI.

## Features

- ▶️ **Zero-touch** — starts on its own and tracks **every game, every day**. Untick a game to mute it.
- ⚡ **Instant settings** — every change in the GUI is saved and applied immediately. No Apply button, no stop/start.
- 🏒 **Live goal detection** — polls the NHL API every ~1 s (0.75 s in the final minute of a period)
- 💡 **Multi-bulb support** — one IP per line; all bulbs flash in parallel
- 🎨 **Every team is distinguishable** — official 2026-27 brand colours *plus* a per-team flash rhythm (see below)
- ⭐ **Primary team** — pick your team (default **Montreal**); its games are starred, listed first, and use their own delay
- ⏱ **TV delay** — primary team games 35 s, all other games 45 s, per-game overrides
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
| Involves the **primary team**     | `primary_delay_seconds` (default 35 s)  |
| Any other game                    | `default_delay_seconds` (default 45 s)  |
| Edited in the game row (yellow)   | that value, for that game only          |

Edits apply to the next goal immediately. *Reset delays* clears per-game overrides.

## Team colours & rhythms

Each team flashes its brand colours in its own rhythm (1 beat = the *Beat* setting, 0.45 s by default), so even teams that share colours
(five red/black teams, a dozen blue ones) are told apart. Colours are the official hex values, converted to bulb HSV with brightness lifted for dark
shades (navy, burgundy) so they don't just look black. Teams that look alike on a bulb are guaranteed to use different rhythm *families*.

| Rhythm       | Pattern (A, B = team colours · C = accent · W = white · D = dark) |
|--------------|-------------------------------------------------------------------|
| alt          | A B (even)                                                        |
| slow         | A A B B (even, slow)                                              |
| gallop       | A A B                                                             |
| rev-gallop   | A B B                                                             |
| double-tap   | A D A B B                                                         |
| white-tick   | A W B W                                                           |
| dark-gap     | A D B D (flicker)                                                 |
| heartbeat    | A B D D                                                           |
| tri-color    | A B C                                                             |
| a-heavy      | A A A B                                                           |
| b-heavy      | A B B B                                                           |

Use **Simulate Goal** to preview any team on your bulb. Utah is now the **Utah Mammoth** (UTA).

**Even timing:** every step sits on a fixed beat timeline. Each colour's command latency is measured separately (bulbs often answer some colours slower than others) and the command is sent that much early, so every colour change lands on the beat and every step lasts its full length. After a stall it never "catches up" with a burst of commands. The log prints `Flash timing on <ip>: A 60ms, B 380ms` after each flash, and warns if your bulb needs longer per command than one beat (then raise **Beat**).

**No default-colour flicker:** flashes use the bulb's native command with `ignore_default` set, so it never flashes its stored default state between steps, and "dark" steps switch the bulb fully off instead of dimming it.

## Configuration reference

All of these are editable live in the sidebar.

| Parameter                | Default | Description                                   |
|--------------------------|---------|-----------------------------------------------|
| `primary_team`           | MTL     | Team whose games use the primary delay        |
| `primary_delay_seconds`  | 35 s    | TV delay for primary-team games               |
| `default_delay_seconds`  | 45 s    | TV delay for all other games                  |
| `flash_duration`         | 12 s    | How long the bulb flashes after a goal        |
| `flash_interval`         | 0.45 s  | One beat of the rhythm (lower = faster)       |
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
