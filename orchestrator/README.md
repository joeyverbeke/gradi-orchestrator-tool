# Gradi Orchestrator (Tier 1 MVP)

This Python watchdog launches every Gradi art piece plus the shared vLLM and Kokoro
services, waits for the required USB devices to appear, and restarts anything that
crashes. Each service runs in its own subprocess with stdout/stderr streamed to
`orchestrator/logs/<service>.log` for quick inspection.

## Layout

```
orchestrator/
├── orchestrator.py     # main entry point
├── services.toml       # declarative service manifest
└── logs/               # created automatically, per-service logs live here
```

## Running the orchestrator

From the repo root:

```bash
python3 orchestrator/orchestrator.py
```

Key behaviour:
- Loads `services.toml`, validates dependencies, and computes a start order so
  vLLM and Kokoro boot before `gradi-mediate`.
- Blocks per service until every required `/dev/gradi-*` device exists before
  launching (covers the WSL USB handoff delay at boot).
- Restarts any process that exits unexpectedly after a 5 s delay.
- Prints state transitions to stdout and mirrors the same lines into each log file.

### Useful CLI options

- `--services vllm gradi-mediate` limits supervision to the listed services
  (dependencies such as Kokoro are auto-included).
- `--config /path/to/services.toml` lets you experiment with other manifests.
- `--testing` switches Gradi Calibrate to English prompts and launches Gradi
  Compress without kiosk/fullscreen mode (handy for local smoke tests).

Stop the orchestrator with `Ctrl+C` (SIGINT) or `systemctl --user stop` when
running under systemd; it sends SIGTERM to each child, waits 10 s, then SIGKILLs
anything still alive.

## Editing the manifest

Each entry under `[services.<name>]` supports:

| Field | Description |
| --- | --- |
| `display_name` | Human label (used only for clarity in the file). |
| `working_dir` | Relative path to run the command from. |
| `command` | Shell command executed via `bash -lc`. Multi-line strings are OK. |
| `testing_command` | Optional command override used only when `--testing` is passed. |
| `venv` | Relative path to the venv folder (its `bin/activate` is sourced automatically). |
| `dependencies` | Other service names that must be running first. |
| `requires_devices` | List of `/dev/...` nodes that must exist before launch. |
| `restart_delay_seconds` | Optional override for the 5 s default backoff. |

Update the command lines to match your preferred CLI flags (e.g., change
languages, lat/lon, Kokoro voice) and re-run the orchestrator.

## Viewing logs

Tail a specific service log in another shell:

```bash
tail -f orchestrator/logs/gradi-mediate.log
```

Stdout/stderr lines are timestamped and tagged with `stdout`/`stderr` to make it
clear where messages originate.

## Example systemd user service

To launch the orchestrator automatically after login:

1. Create `~/.config/systemd/user/gradi-orchestrator.service`:

   ```ini
   [Unit]
   Description=Gradi Orchestrator
   After=default.target

   [Service]
   WorkingDirectory=/home/joey/gradi
   ExecStart=/usr/bin/python3 /home/joey/gradi/orchestrator/orchestrator.py
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=default.target
   ```

2. Reload and enable:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now gradi-orchestrator.service
   ```

The orchestrator continues waiting for `/dev/gradi-*` devices even if this
service starts before the Windows USB bridge finishes attaching hardware.

## Next steps (Tiers 2 & 3)

- Replace the simple `/dev` polling with `pyudev` watchers so reconnects are
  detected immediately.
- Add structured health checks (ports, heartbeats) and exponential backoff.
- Cascade dependency restarts (e.g., restart `gradi-mediate` when vLLM dies).
- Expose a control socket / dashboard for remote status queries and manual
  start/stop commands.
