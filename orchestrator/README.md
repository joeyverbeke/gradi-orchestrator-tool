# Gradi Orchestrator (Tier 1 MVP)

This Python watchdog launches every Gradi art piece plus the shared vLLM and Kokoro
services, waits for the required USB devices to appear, and restarts anything that
crashes. Each service runs in its own subprocess with stdout/stderr streamed to
`orchestrator/logs/<service>.log` for quick inspection.

## Layout

```
orchestrator/
├── orchestrator.py         # main entry point
├── requirements.txt        # pip dependencies (pyudev, etc.)
├── services.toml           # declarative service manifest
└── logs/
    ├── orchestrator/       # supervisor logs (timestamped)
    └── <service>/          # per-service stdout/stderr logs (timestamped)

## Setup

From the repo root:

```bash
# one-time environment creation
uv venv orchestrator/.venv
source orchestrator/.venv/bin/activate
uv pip install -r orchestrator/requirements.txt
```

Activate `orchestrator/.venv` before running the watchdog so `pyudev` and other
dependencies are on `PYTHONPATH`.
```

## Running the orchestrator

From the repo root:

```bash
python3 orchestrator/orchestrator.py
```

Key behaviour:
- Loads `services.toml`, validates dependencies, and computes a start order so
  vLLM and Kokoro boot before `gradi-mediate`.
- Uses `pyudev` to watch `/dev/gradi-*` symlinks; services only launch once their
  devices exist and are restarted automatically if a device disconnects.
- Runs HTTP/TCP health probes (when configured) before marking dependencies as
  ready and restarts services whose probes fail repeatedly.
- Streams stdout/stderr to per-service log files and prints concise state
  transitions (starting, running, backoff, waiting for device, etc.).

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
| `health` | Optional probe settings (HTTP/TCP/process) used for readiness + monitoring. |
| `device_restart_policy` | `immediate` (default) stops the service as soon as the required device disappears; `on_reconnect` waits until the device returns before restarting (useful for Gradi Compress). |

Update the command lines to match your preferred CLI flags (e.g., change
languages, lat/lon, Kokoro voice) and re-run the orchestrator.

## Viewing logs

- Service logs:

  ```bash
  tail -f "$(ls -t orchestrator/logs/gradi-mediate/*.log | head -n1)"
  ```

- Supervisor logs:

  ```bash
  tail -f "$(ls -t orchestrator/logs/orchestrator/*.log | head -n1)"
  ```

Each launch writes to a new timestamped file under its directory, and every line
is tagged so you always know where messages originate.

## WSL autostart recipe

These are the exact steps we use to boot the orchestrator automatically whenever
WSL spins up:

1. **Enable systemd inside the distro**
   - Edit `/etc/wsl.conf` with sudo and ensure it contains:
     ```
     [boot]
     systemd=true
     ```
   - From Windows PowerShell/CMD run `wsl --shutdown` once so systemd actually
     starts the next time the distro launches.

2. **Add the launcher script** (already in this repo)
   - File: `orchestrator/run_watchdog.sh`
     ```bash
     #!/usr/bin/env bash
     set -euo pipefail
     cd /home/joey/gradi
     source orchestrator/.venv/bin/activate
     exec python3 orchestrator/orchestrator.py "$@"
     ```
   - Make sure it is executable: `chmod +x orchestrator/run_watchdog.sh`.

3. **Create the user service**
   - File: `~/.config/systemd/user/gradi-orchestrator.service`
     ```ini
     [Unit]
     Description=Gradi art orchestrator
     After=network.target

     [Service]
     WorkingDirectory=/home/joey/gradi
     ExecStart=/home/joey/gradi/orchestrator/run_watchdog.sh
     Restart=on-failure
     RestartSec=5
     Environment=PYTHONUNBUFFERED=1

     [Install]
     WantedBy=default.target
     ```

4. **Enable after systemd is running**
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now gradi-orchestrator.service
   ```

Logs live in `journalctl --user -u gradi-orchestrator`, and you can start/stop
the watchdog with `systemctl --user start|stop gradi-orchestrator`. The
`PYTHONUNBUFFERED` flag keeps stdout/stderr streaming immediately into the
journal, while the script guarantees the correct working directory and virtual
environment before launching the supervisor.

⚠️  User-level systemd sessions inherit a minimal `PATH`, so the service
commands in `services.toml` reference full paths (e.g.,
`/home/joey/.local/bin/uv`) or explicitly source `~/.nvm/nvm.sh` before calling
`npm`. Mirror that pattern whenever you add new tools so restarts behave the
same as an interactive shell.

## Next steps

- Enrich structured logging/metrics (JSON logs, restart counters, Prometheus
  text exporter) so remote operators get quick insight.
- Persist a compact status file the art handlers can read (“Mediation waiting
  for /dev/gradi-esp-mediate”) without reading full logs.
- Add optional control surface (REST/CLI) if we later need remote start/stop
  or log streaming without SSH access.
