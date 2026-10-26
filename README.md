# NHL Goal Light 🚨

Real-time NHL goal detector that flashes your **TP-Link Kasa smart bulb(s)** in the scoring team's colours, with a web GUI to configure everything.

## Features

- 🏒 **Live goal detection** — polls the NHL API every ~1 s (0.75 s in the final minute of a period)
- 💡 **Multi-bulb support** — one IP per line; all bulbs flash in parallel, and one dead bulb never blocks the others
- 🎨 **Team colour flashing** — strobes between each team's primary and secondary colours
- ⭐ **Primary team** — pick your team (default **Montreal**); its games are starred, listed first and pre-selected
- ⏱ **Per-team TV delay** — primary-team games use their own delay; every other game defaults to **45 s**; any single game can still be overridden
- ⚡ **Simulate Goal**, 🌐 **web GUI** (dark/light, live log, mobile drawer), 🔄 **exponential backoff**

## Quick start

```bash
pip install -r requirements.txt
python server.py          # then open http://localhost:5000
```

## TV delay

| Game                              | Delay                                   |
|-----------------------------------|-----------------------------------------|
| Involves the **primary team**     | `primary_delay_seconds` (default 35 s)  |
| Any other game                    | `default_delay_seconds` (default 45 s)  |
| Edited in the game row (yellow)   | that value, for that game only          |

Change the primary team and both delays under **Teams & Delay** in the sidebar (click *Apply*). *Reset delays* clears per-game overrides.
Delays are read when monitoring starts. Stop and start again to apply changes.

CLI equivalents: `PRIMARY_TEAM=MTL PRIMARY_DELAY_SECONDS=35 DEFAULT_DELAY_SECONDS=45 GAME_DELAYS='{"2025020123": 30}'`.

## Configuration reference

| Parameter                | Default | Description                                   |
|--------------------------|---------|-----------------------------------------------|
| `primary_team`           | MTL     | Team whose games use the primary delay        |
| `primary_delay_seconds`  | 35 s    | TV delay for primary-team games               |
| `default_delay_seconds`  | 45 s    | TV delay for all other games                  |
| `flash_duration`         | 12 s    | How long the bulb flashes after a goal        |
| `flash_interval`         | 0.45 s  | Strobe speed (lower = faster)                 |
| `flash_quiet_window`     | 1.5 s   | Post-flash wait before restoring (bursts)     |
| `flash_transition_ms`    | 120 ms  | Colour crossfade (0 = instant)                |
| `poll_live_seconds`      | 1.0 s   | Poll rate during live play                    |
| `poll_critical_seconds`  | 0.75 s  | Poll rate in the final minute of a period     |
| `poll_pregame_seconds`   | 10 s    | Poll rate before puck drop                    |
| `poll_error_seconds`     | 5 s     | Retry wait after API failure                  |
| `request_timeout`        | 10 s    | NHL API request timeout                       |
| `max_retries`            | 4       | Retries after the first failed request        |
| `backoff_base` / `_max`  | 1 / 30 s| Exponential backoff start / cap               |
| `pregame_buffer_seconds` | 300 s   | Wake-up lead time before puck drop            |
| `restore_transition_ms`  | 150 ms  | Fade back to the original colour              |

## License

MIT
