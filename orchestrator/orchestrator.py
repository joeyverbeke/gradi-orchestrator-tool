#!/usr/bin/env python3
"""
Tier-1 Gradi orchestrator.

Responsibilities:
  * load service metadata from services.toml
  * wait for required USB devices before launching dependent services
  * start dependencies before their downstream consumers
  * stream stdout/stderr to per-service log files
  * restart crashed services after a fixed delay
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import tomllib


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "orchestrator" / "services.toml"
LOG_DIR = REPO_ROOT / "orchestrator" / "logs"


class ServiceState:
    STOPPED = "stopped"
    WAITING_DEVICES = "waiting_devices"
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass
class ServiceConfig:
    name: str
    display_name: str
    working_dir: Path
    command: str
    testing_command: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)
    requires_devices: List[str] = field(default_factory=list)
    venv: Optional[Path] = None
    restart_delay_seconds: int = 5


class ServiceOrchestrator:
    def __init__(
        self,
        configs: Dict[str, ServiceConfig],
        start_order: List[str],
        stop_timeout: int,
        testing_mode: bool,
    ) -> None:
        self.configs = configs
        self.start_order = start_order
        self.stop_timeout = stop_timeout
        self.testing_mode = testing_mode
        self.states: Dict[str, str] = {name: ServiceState.STOPPED for name in configs}
        self.processes: Dict[str, asyncio.subprocess.Process] = {}
        self.stop_event = asyncio.Event()
        self.device_poll_seconds = 2
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.request_stop(s)))
            except NotImplementedError:
                # Windows (or WSL without signal support) falls back to default handlers.
                signal.signal(sig, lambda *_: asyncio.create_task(self.request_stop(sig)))

        tasks = [asyncio.create_task(self.supervise_service(name)) for name in self.start_order]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def request_stop(self, sig: signal.Signals) -> None:
        if self.stop_event.is_set():
            return
        print(f"\n[{timestamp()}] Received {sig.name}, stopping services...")
        self.stop_event.set()
        await self._stop_all_processes()

    async def supervise_service(self, name: str) -> None:
        cfg = self.configs[name]
        while not self.stop_event.is_set():
            deps_ready = await self._wait_for_dependencies(cfg)
            if not deps_ready:
                break

            devices_ready = await self._wait_for_devices(cfg)
            if not devices_ready:
                break

            try:
                process = await self._launch_service(cfg)
            except Exception as exc:  # pragma: no cover
                self._set_state(name, ServiceState.FAILED, f"launch error: {exc}")
                await asyncio.sleep(cfg.restart_delay_seconds)
                continue

            self.processes[name] = process
            self._set_state(name, ServiceState.RUNNING)

            return_code = await process.wait()
            self.processes.pop(name, None)

            if self.stop_event.is_set():
                self._set_state(name, ServiceState.STOPPED)
                break

            self._set_state(name, ServiceState.BACKOFF, f"exit code {return_code}")
            await asyncio.sleep(cfg.restart_delay_seconds)
            self._set_state(name, ServiceState.STOPPED)

    async def _wait_for_dependencies(self, cfg: ServiceConfig) -> bool:
        missing: List[str]
        while not self.stop_event.is_set():
            missing = [name for name in cfg.dependencies if self.states.get(name) != ServiceState.RUNNING]
            if not missing:
                return True
            await asyncio.sleep(1)
        return False

    async def _wait_for_devices(self, cfg: ServiceConfig) -> bool:
        if not cfg.requires_devices:
            return True

        while not self.stop_event.is_set():
            missing = [dev for dev in cfg.requires_devices if not Path(dev).exists()]
            if not missing:
                self._set_state(cfg.name, ServiceState.STARTING, "devices ready")
                return True

            self._set_state(
                cfg.name,
                ServiceState.WAITING_DEVICES,
                f"waiting for {', '.join(missing)}",
            )
            await asyncio.sleep(self.device_poll_seconds)

        return False

    async def _launch_service(self, cfg: ServiceConfig) -> asyncio.subprocess.Process:
        self._set_state(cfg.name, ServiceState.STARTING)
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        log_path = LOG_DIR / f"{cfg.name}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        activation = ""
        if cfg.venv:
            activate_path = cfg.venv / "bin" / "activate"
            activation = f"source {shlex.quote(str(activate_path))}"
        selected_command = cfg.testing_command if self.testing_mode and cfg.testing_command else cfg.command
        cmd = selected_command.strip()
        if activation:
            full_cmd = f"{activation} && {cmd}"
        else:
            full_cmd = cmd

        process = await asyncio.create_subprocess_shell(
            full_cmd,
            cwd=str(cfg.working_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            executable="/bin/bash",
        )

        asyncio.create_task(self._stream_output(cfg.name, process.stdout, log_path, "stdout"))
        asyncio.create_task(self._stream_output(cfg.name, process.stderr, log_path, "stderr"))
        return process

    async def _stream_output(
        self,
        service_name: str,
        stream: Optional[asyncio.StreamReader],
        log_path: Path,
        stream_name: str,
    ) -> None:
        if stream is None:
            return

        with log_path.open("a", buffering=1, encoding="utf-8") as log_file:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip("\n")
                log_line = f"{timestamp()} [{stream_name}] {text}\n"
                log_file.write(log_line)
                log_file.flush()
                prefix = f"[{service_name}:{stream_name}]"
                print(f"{prefix} {text}")

    async def _stop_all_processes(self) -> None:
        if not self.processes:
            return

        for name, process in list(self.processes.items()):
            if process.returncode is not None:
                continue
            self._set_state(name, ServiceState.STOPPING)
            process.terminate()

        await asyncio.sleep(self.stop_timeout)

        for name, process in list(self.processes.items()):
            if process.returncode is not None:
                continue
            print(f"[{timestamp()}] Forcing {name} to exit")
            process.kill()

    def _set_state(self, name: str, new_state: str, reason: Optional[str] = None) -> None:
        old_state = self.states.get(name)
        if old_state == new_state and not reason:
            return
        self.states[name] = new_state
        extra = f" ({reason})" if reason else ""
        print(f"[{timestamp()}] {name}: {old_state} -> {new_state}{extra}")


def timestamp() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def load_config(path: Path) -> tuple[Dict[str, ServiceConfig], List[str], int]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("rb") as fp:
        data = tomllib.load(fp)

    defaults = data.get("defaults", {})
    restart_delay = int(defaults.get("restart_delay_seconds", 5))
    stop_timeout = int(defaults.get("stop_timeout_seconds", 10))

    services_section = data.get("services")
    if not services_section:
        raise ValueError("No services defined in configuration.")

    configs: Dict[str, ServiceConfig] = {}
    for name, payload in services_section.items():
        working_dir = REPO_ROOT / payload["working_dir"]
        if not working_dir.exists():
            raise ValueError(f"Working directory missing for {name}: {working_dir}")

        venv_name = payload.get("venv")
        venv_path = (working_dir / venv_name) if venv_name else None
        cmd = payload.get("command")
        if not cmd:
            raise ValueError(f"Missing command for service {name}")

        dependencies = payload.get("dependencies", [])
        requires_devices = payload.get("requires_devices", [])

        configs[name] = ServiceConfig(
            name=name,
            display_name=payload.get("display_name", name),
            working_dir=working_dir,
            command=cmd,
            testing_command=payload.get("testing_command"),
            dependencies=dependencies,
            requires_devices=requires_devices,
            venv=venv_path,
            restart_delay_seconds=int(payload.get("restart_delay_seconds", restart_delay)),
        )

    start_order = topological_sort(configs)
    return configs, start_order, stop_timeout


def topological_sort(configs: Dict[str, ServiceConfig]) -> List[str]:
    order: List[str] = []
    visiting: Dict[str, int] = {}

    def visit(name: str) -> None:
        state = visiting.get(name, 0)
        if state == 1:
            raise ValueError(f"Cycle detected at {name}")
        if state == 2:
            return
        visiting[name] = 1
        cfg = configs[name]
        for dep in cfg.dependencies:
            if dep not in configs:
                raise ValueError(f"Service '{name}' depends on unknown service '{dep}'")
            visit(dep)
        visiting[name] = 2
        order.append(name)

    for name in configs:
        visit(name)
    return order


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gradi service orchestrator")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to services.toml",
    )
    parser.add_argument(
        "--services",
        nargs="+",
        help="subset of services to manage (dependencies auto-included)",
    )
    parser.add_argument(
        "--testing",
        action="store_true",
        help="Use testing overrides (disable fullscreen, switch Calibrate to English, etc.)",
    )
    return parser.parse_args()


def filter_services(
    selected: Optional[List[str]],
    configs: Dict[str, ServiceConfig],
) -> Dict[str, ServiceConfig]:
    if not selected:
        return configs

    missing = [name for name in selected if name not in configs]
    if missing:
        raise ValueError(f"Unknown services requested: {', '.join(missing)}")

    result: Dict[str, ServiceConfig] = {}

    def add_with_dependencies(name: str) -> None:
        if name in result:
            return
        cfg = configs[name]
        for dep in cfg.dependencies:
            add_with_dependencies(dep)
        result[name] = cfg

    for name in selected:
        add_with_dependencies(name)
    return result


async def async_main() -> int:
    args = parse_args()
    configs, start_order, stop_timeout = load_config(args.config)
    filtered_configs = filter_services(args.services, configs)
    filtered_order = [name for name in start_order if name in filtered_configs]
    orchestrator = ServiceOrchestrator(
        filtered_configs,
        filtered_order,
        stop_timeout,
        testing_mode=args.testing,
    )
    await orchestrator.run()
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        return 1
    except Exception as exc:  # pragma: no cover
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
