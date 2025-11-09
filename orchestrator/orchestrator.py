#!/usr/bin/env python3
"""
Tier-1 Gradi orchestrator.

Responsibilities:
  * load service metadata from services.toml
  * wait for required USB devices before launching dependent services
  * monitor /dev state with pyudev and restart services when devices drop
  * run health probes before marking dependencies ready and during runtime
  * stream stdout/stderr to per-service log files
  * restart crashed services after a fixed delay
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import shlex
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import pyudev
import tomllib
import urllib.request


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "orchestrator" / "services.toml"
LOG_DIR = REPO_ROOT / "orchestrator" / "logs"
SUPERVISOR_LOG_DIR = LOG_DIR / "orchestrator"


class ServiceState:
    STOPPED = "stopped"
    WAITING_DEVICES = "waiting_devices"
    STARTING = "starting"
    RUNNING = "running"
    BACKOFF = "backoff"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass
class HealthCheckConfig:
    probe_type: str
    url: Optional[str] = None
    host: str = "127.0.0.1"
    port: Optional[int] = None
    interval_seconds: int = 5
    timeout_seconds: int = 5
    failure_threshold: int = 3
    success_threshold: int = 1
    initial_delay_seconds: int = 5


class DeviceRegistry:
    """pyudev-backed watcher that signals when any tracked /dev node changes."""

    def __init__(self, loop: asyncio.AbstractEventLoop, device_paths: List[str]) -> None:
        self.loop = loop
        self.device_paths = sorted(set(device_paths))
        self._change_event = asyncio.Event()
        self._observer: Optional[pyudev.MonitorObserver] = None
        if self.device_paths:
            try:
                context = pyudev.Context()
                monitor = pyudev.Monitor.from_netlink(context)
                monitor.filter_by(subsystem="tty")
                self._observer = pyudev.MonitorObserver(
                    monitor,
                    callback=self._handle_event,
                    name="gradi-device-monitor",
                )
                self._observer.start()
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[{timestamp()}] Device monitor disabled: {exc}")
                self._observer = None
        # Kick things once so waiters immediately re-evaluate current state.
        self._change_event.set()

    def close(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer = None

    def _handle_event(self, action: str, device: pyudev.Device) -> None:  # pragma: no cover - handled via pyudev
        del action, device
        self.loop.call_soon_threadsafe(self._change_event.set)

    async def wait_for_change(self, stop_event: asyncio.Event, timeout: float) -> bool:
        """Wait until a udev change occurs or timeout elapses."""
        if not self.device_paths:
            await asyncio.sleep(timeout)
            return not stop_event.is_set()

        change_task = asyncio.create_task(self._change_event.wait())
        stop_task = asyncio.create_task(stop_event.wait())
        timeout_task = asyncio.create_task(asyncio.sleep(timeout))

        done, pending = await asyncio.wait(
            {change_task, stop_task, timeout_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if stop_task in done:
            return False
        if change_task in done:
            self._change_event.clear()
        return True


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
    health: Optional[HealthCheckConfig] = None
    device_restart_policy: str = "immediate"


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
        self.health_tasks: Dict[str, asyncio.Task] = {}
        self.device_tasks: Dict[str, asyncio.Task] = {}
        self.device_registry: Optional[DeviceRegistry] = None
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        SUPERVISOR_LOG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        self.supervisor_log = SUPERVISOR_LOG_DIR / f"orchestrator-{timestamp_str}.log"
        self.supervisor_log.touch(exist_ok=False)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self.request_stop(s)))
            except NotImplementedError:
                # Windows (or WSL without signal support) falls back to default handlers.
                signal.signal(sig, lambda *_: asyncio.create_task(self.request_stop(sig)))

        tracked_devices = sorted(
            {device for cfg in self.configs.values() for device in cfg.requires_devices}
        )
        if tracked_devices:
            self.device_registry = DeviceRegistry(loop, tracked_devices)

        tasks = [asyncio.create_task(self.supervise_service(name)) for name in self.start_order]
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if self.device_registry:
                self.device_registry.close()

    async def request_stop(self, sig: signal.Signals) -> None:
        if self.stop_event.is_set():
            return
        self._log_supervisor(f"Received {sig.name}, stopping services...")
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
            ready = await self._await_initial_readiness(cfg, process)
            if not ready:
                await self._gracefully_stop_process(name, process, "initial health/device check failed")
                self.processes.pop(name, None)
                self._set_state(name, ServiceState.BACKOFF, "initial readiness failed")
                await asyncio.sleep(cfg.restart_delay_seconds)
                self._set_state(name, ServiceState.STOPPED)
                continue

            if cfg.health:
                health_task = asyncio.create_task(self._monitor_health(cfg, process))
                self.health_tasks[name] = health_task

            if cfg.requires_devices:
                device_task = asyncio.create_task(self._monitor_device_disconnects(cfg, process))
                self.device_tasks[name] = device_task

            return_code = await process.wait()
            self.processes.pop(name, None)
            self._cancel_task(self.health_tasks.pop(name, None))
            self._cancel_task(self.device_tasks.pop(name, None))

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
            missing = self._missing_devices(cfg.requires_devices)
            if not missing:
                self._set_state(cfg.name, ServiceState.STARTING, "devices ready")
                return True

            self._set_state(
                cfg.name,
                ServiceState.WAITING_DEVICES,
                f"waiting for {', '.join(missing)}",
            )
            if not await self._wait_for_device_event():
                break

        return False

    async def _launch_service(self, cfg: ServiceConfig) -> asyncio.subprocess.Process:
        self._set_state(cfg.name, ServiceState.STARTING)
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        log_path = self._prepare_log_path(cfg.name)

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
            start_new_session=True,
        )

        asyncio.create_task(self._stream_output(cfg.name, process.stdout, log_path, "stdout"))
        asyncio.create_task(self._stream_output(cfg.name, process.stderr, log_path, "stderr"))
        return process

    def _prepare_log_path(self, service_name: str) -> Path:
        service_dir = LOG_DIR / service_name
        service_dir.mkdir(parents=True, exist_ok=True)
        timestamp_str = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        return service_dir / f"{service_name}-{timestamp_str}.log"

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

    async def _await_initial_readiness(
        self,
        cfg: ServiceConfig,
        process: asyncio.subprocess.Process,
    ) -> bool:
        if cfg.health:
            return await self._await_initial_health(cfg, process)

        self._set_state(cfg.name, ServiceState.RUNNING)
        return True

    async def _await_initial_health(
        self,
        cfg: ServiceConfig,
        process: asyncio.subprocess.Process,
    ) -> bool:
        assert cfg.health  # guarded by caller
        health = cfg.health
        successes = 0
        failures = 0
        await self._sleep_with_stop(health.initial_delay_seconds)
        while not self.stop_event.is_set():
            if process.returncode is not None:
                return False
            healthy = await self._perform_health_check(health)
            if healthy:
                successes += 1
                failures = 0
                if successes >= health.success_threshold:
                    self._set_state(cfg.name, ServiceState.RUNNING, "health probe passed")
                    return True
            else:
                successes = 0
                failures += 1
                if failures >= health.failure_threshold:
                    return False
            await self._sleep_with_stop(health.interval_seconds)
        return False

    async def _monitor_health(
        self,
        cfg: ServiceConfig,
        process: asyncio.subprocess.Process,
    ) -> None:
        assert cfg.health
        health = cfg.health
        failures = 0
        while not self.stop_event.is_set():
            if process.returncode is not None:
                return
            healthy = await self._perform_health_check(health)
            if healthy:
                failures = 0
            else:
                failures += 1
                if failures >= health.failure_threshold:
                    self._set_state(cfg.name, ServiceState.BACKOFF, "health probe failed")
                    process.terminate()
                    return
            await self._sleep_with_stop(health.interval_seconds)

    async def _monitor_device_disconnects(
        self,
        cfg: ServiceConfig,
        process: asyncio.subprocess.Process,
    ) -> None:
        if not cfg.requires_devices:
            return

        notified_missing = False
        policy = cfg.device_restart_policy or "immediate"

        while not self.stop_event.is_set():
            if process.returncode is not None:
                return
            missing = self._missing_devices(cfg.requires_devices)
            if not missing:
                notified_missing = False
                if not await self._wait_for_device_event():
                    return
                continue

            if not notified_missing:
                self._set_state(
                    cfg.name,
                    ServiceState.WAITING_DEVICES,
                    f"device lost: {', '.join(missing)}",
                )
                notified_missing = True

            if policy == "on_reconnect":
                self._log_supervisor(f"{cfg.name}: waiting for device to return before restart")
                await self._wait_for_device_return(cfg, process)
                return

            await self._gracefully_stop_process(cfg.name, process, "device lost")
            return

    async def _wait_for_device_return(
        self,
        cfg: ServiceConfig,
        process: asyncio.subprocess.Process,
    ) -> None:
        while not self.stop_event.is_set():
            if process.returncode is not None:
                return
            if not self._missing_devices(cfg.requires_devices):
                self._log_supervisor(f"{cfg.name}: device reattached, restarting service")
                await self._gracefully_stop_process(cfg.name, process, "device reattached")
                return
            if not await self._wait_for_device_event():
                return

    async def _perform_health_check(self, health: HealthCheckConfig) -> bool:
        probe_type = health.probe_type.lower()
        timeout = health.timeout_seconds
        if probe_type == "http":
            assert health.url, "HTTP health checks require a URL"

            def _http_request() -> bool:
                try:
                    with urllib.request.urlopen(health.url, timeout=timeout) as response:
                        return 200 <= response.status < 400
                except Exception:
                    return False

            return await asyncio.to_thread(_http_request)

        if probe_type == "tcp":
            assert health.port is not None, "TCP health checks require a port"
            host = health.host or "127.0.0.1"
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, health.port),
                    timeout=timeout,
                )
                writer.close()
                await writer.wait_closed()
                return True
            except Exception:
                return False

        # Default: process-alive probe
        return True

    async def _wait_for_device_event(self) -> bool:
        if self.stop_event.is_set():
            return False
        if self.device_registry:
            return await self.device_registry.wait_for_change(self.stop_event, self.device_poll_seconds)
        await self._sleep_with_stop(self.device_poll_seconds)
        return not self.stop_event.is_set()

    async def _sleep_with_stop(self, seconds: float) -> None:
        if seconds <= 0:
            return
        stop_task = asyncio.create_task(self.stop_event.wait())
        sleep_task = asyncio.create_task(asyncio.sleep(seconds))
        done, pending = await asyncio.wait(
            {stop_task, sleep_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _missing_devices(self, devices: List[str]) -> List[str]:
        return [dev for dev in devices if not Path(dev).exists()]

    async def _gracefully_stop_process(
        self,
        name: str,
        process: asyncio.subprocess.Process,
        reason: str,
    ) -> None:
        if process.returncode is not None:
            return
        self._set_state(name, ServiceState.STOPPING, reason)
        self._send_signal(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self.stop_timeout)
        except asyncio.TimeoutError:
            self._log_supervisor(f"Forcing {name} to exit (timeout)")
            self._send_signal(process, signal.SIGKILL)
            await process.wait()

    def _cancel_task(self, task: Optional[asyncio.Task]) -> None:
        if task is None:
            return
        task.cancel()

        def _consume(task_obj: asyncio.Task) -> None:
            with contextlib.suppress(asyncio.CancelledError):
                task_obj.exception()

        task.add_done_callback(_consume)

    def _log_supervisor(self, message: str, also_stdout: bool = True) -> None:
        timestamp_str = timestamp()
        line = f"[{timestamp_str}] {message}"
        if also_stdout:
            print(line)
        with self.supervisor_log.open("a", encoding="utf-8") as fp:
            fp.write(f"{line}\n")

    def _send_signal(self, process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        try:
            os.killpg(process.pid, sig.value if hasattr(sig, "value") else sig)
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                pass

    async def _stop_all_processes(self) -> None:
        for task in self.health_tasks.values():
            self._cancel_task(task)
        for task in self.device_tasks.values():
            self._cancel_task(task)
        self.health_tasks.clear()
        self.device_tasks.clear()

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
        message = f"{name}: {old_state} -> {new_state}{extra}"
        self._log_supervisor(message)


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
        health_section = payload.get("health")
        health = None
        if health_section:
            probe_type = health_section.get("type") or health_section.get("probe_type")
            if not probe_type:
                raise ValueError(f"Health check for service {name} is missing 'type'")
            health = HealthCheckConfig(
                probe_type=probe_type,
                url=health_section.get("url"),
                host=health_section.get("host", "127.0.0.1"),
                port=health_section.get("port"),
                interval_seconds=int(health_section.get("interval_seconds", 5)),
                timeout_seconds=int(health_section.get("timeout_seconds", 5)),
                failure_threshold=int(health_section.get("failure_threshold", 3)),
                success_threshold=int(health_section.get("success_threshold", 1)),
                initial_delay_seconds=int(health_section.get("initial_delay_seconds", 5)),
            )

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
            health=health,
            device_restart_policy=payload.get("device_restart_policy", "immediate"),
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
