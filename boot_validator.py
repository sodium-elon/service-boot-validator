#!/usr/bin/env python3
"""Validate and recover Palladium's long-lived services after boot."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

PGDATA = Path("/home/john/projects/postgres-hosting.workspace/data/postgres")
PGSOCKET = Path("/home/john/projects/postgres-hosting.workspace/data/postgres-socket")

# Dependency providers first; consumers afterwards.
USER_SERVICES = [
    "palladium.service",
    "valkey.service",
    "subscription-llm.service",
    "subscription-llm-images.service",
    "postgrest.service",
    "fli-mcp.service",
    "alpharunner.service",
    "iptvchannels.service",
    "saveatrip.service",
    "cloudbeaver.service",
    "experiments.service",
    "github-pr-notifier.service",
    "hermes-dashboard.service",
    "hermes-gateway.service",
    "hermes-gateway-sportscoach.service",
    "hermes-gateway-coder.service",
    "hermes-wake-bridge.service",
    "t3code.service",
    "afp-tc-mount.service",
]


@dataclass(frozen=True)
class Probe:
    name: str
    service: str
    url: str | None = None
    host: str = "127.0.0.1"
    port: int | None = None
    public: bool = False


PROBES = [
    Probe("PostgreSQL", "palladium.service", port=5432),
    Probe("Valkey", "valkey.service", port=6379),
    Probe("Saveatrip", "saveatrip.service", url="http://127.0.0.1:3040/"),
    Probe("AlphaRunner", "alpharunner.service", url="http://127.0.0.1:3050/"),
    Probe("IPTV Channels", "iptvchannels.service", url="http://127.0.0.1:3100/"),
    Probe("PostgREST", "postgrest.service", url="http://127.0.0.1:3110/"),
    Probe("CloudBeaver", "cloudbeaver.service", url="http://127.0.0.1:3130/"),
    Probe("GitHub PR notifier", "github-pr-notifier.service", url="http://127.0.0.1:3140/"),
    Probe("Experiments", "experiments.service", url="http://127.0.0.1:5000/"),
    Probe("FLI MCP", "fli-mcp.service", url="http://127.0.0.1:7878/mcp"),
    Probe("Image API", "subscription-llm-images.service", url="http://127.0.0.1:8787/"),
    Probe("Subscription LLM", "subscription-llm.service", url="http://127.0.0.1:8789/"),
    Probe("Hermes dashboard", "hermes-dashboard.service", url="http://127.0.0.1:9119/"),
    Probe("Public GitHub notifier", "github-pr-notifier.service", url="https://github-pr-notifier.anthood.net/", public=True),
    Probe("Public IPTV Channels", "iptvchannels.service", url="https://iptvchannels.anthood.net/", public=True),
    Probe("Public Saveatrip", "saveatrip.service", url="https://saveatrip.anthood.net/", public=True),
    Probe("Public AlphaRunner", "alpharunner.service", url="https://alpharunner.anthood.net/", public=True),
    Probe("Public CloudBeaver", "cloudbeaver.service", url="https://cloudbeaver.anthood.net/", public=True),
    Probe("Public Experiments", "experiments.service", url="https://experiments.anthood.net/", public=True),
]


class Runner(Protocol):
    def run(self, *args: str) -> tuple[int, str]: ...


class UserSystemctl:
    def run(self, *args: str) -> tuple[int, str]:
        completed = subprocess.run(
            ["systemctl", "--user", *args],
            text=True,
            capture_output=True,
            check=False,
        )
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        return completed.returncode, output


def http_status_is_healthy(status: int) -> bool:
    """Any real client response is alive; transport failures and 5xx are not."""
    return 100 <= status < 500


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text().splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return None


def _pid_is_postgres(pid: int | None, pgdata: Path, proc_root: Path) -> bool:
    if pid is None:
        return False
    process = proc_root / str(pid)
    try:
        comm = (process / "comm").read_text().strip()
        cmdline = (process / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return False
    return comm == "postgres" and str(pgdata) in cmdline


def quarantine_stale_postgres_artifacts(
    *,
    pgdata: Path,
    socket_dir: Path,
    proc_root: Path = Path("/proc"),
    quarantine_dir: Path = Path("/tmp"),
    port_open: bool,
    timestamp: int | None = None,
) -> list[Path]:
    """Move stale PostgreSQL lock/socket artifacts only when no real server owns them."""
    if port_open:
        return []

    artifacts = [
        pgdata / "postmaster.pid",
        socket_dir / ".s.PGSQL.5432.lock",
        socket_dir / ".s.PGSQL.5432",
    ]
    existing = [path for path in artifacts if path.exists() or path.is_socket()]
    if not existing:
        return []

    pids = [_read_pid(path) for path in existing if path.name.endswith(".pid") or path.name.endswith(".lock")]
    if any(_pid_is_postgres(pid, pgdata, proc_root) for pid in pids):
        return []

    quarantine_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time()) if timestamp is None else timestamp
    moved: list[Path] = []
    for source in existing:
        target = quarantine_dir / f"{source.name}.stale.{stamp}"
        suffix = 1
        while target.exists():
            target = quarantine_dir / f"{source.name}.stale.{stamp}.{suffix}"
            suffix += 1
        shutil.move(str(source), str(target))
        moved.append(target)
    return moved


def tcp_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_status(url: str, timeout: float = 8.0) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": "palladium-boot-validator/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError):
        return 0


def ensure_user_service(
    unit: str,
    *,
    runner: Runner,
    attempts: int = 5,
    delay_seconds: float = 2.0,
) -> bool:
    code, _ = runner.run("is-active", unit)
    if code == 0:
        return True

    runner.run("reset-failed", unit)
    start_code, _ = runner.run("start", unit)
    if start_code != 0:
        return False

    for _ in range(attempts):
        code, _ = runner.run("is-active", unit)
        if code == 0:
            return True
        if delay_seconds:
            time.sleep(delay_seconds)
    return False


def probe_healthy(probe: Probe) -> tuple[bool, str]:
    if probe.url:
        status = http_status(probe.url)
        return http_status_is_healthy(status), f"HTTP {status or 'transport-failure'}"
    if probe.port is not None:
        healthy = tcp_open(probe.host, probe.port)
        return healthy, "TCP open" if healthy else "TCP closed"
    return False, "invalid probe"


def _retry_probe(probe: Probe, attempts: int = 5, delay_seconds: float = 2.0) -> tuple[bool, str]:
    detail = "not run"
    for attempt in range(attempts):
        healthy, detail = probe_healthy(probe)
        if healthy:
            return True, detail
        if attempt + 1 < attempts and delay_seconds:
            time.sleep(delay_seconds)
    return False, detail


def validate_services(*, repair: bool, include_public: bool) -> int:
    runner = UserSystemctl()
    failures: list[str] = []

    if repair and not tcp_open("127.0.0.1", 5432):
        moved = quarantine_stale_postgres_artifacts(
            pgdata=PGDATA,
            socket_dir=PGSOCKET,
            port_open=False,
        )
        for path in moved:
            print(f"REPAIRED stale PostgreSQL artifact -> {path}")

    for unit in USER_SERVICES:
        if repair:
            healthy = ensure_user_service(unit, runner=runner)
        else:
            healthy = runner.run("is-active", unit)[0] == 0
        print(f"{'PASS' if healthy else 'FAIL'} service {unit}")
        if not healthy:
            failures.append(f"service:{unit}")

    # Caddy is a system service. This user-level validator can inspect it but must not sudo.
    caddy = subprocess.run(
        ["systemctl", "is-active", "--quiet", "caddy.service"], check=False
    ).returncode == 0
    print(f"{'PASS' if caddy else 'FAIL'} system service caddy.service")
    if not caddy:
        failures.append("service:caddy.service")

    # Active units can still return 503 while their databases/schema cache warm up.
    if repair and runner.run("is-active", "palladium.service")[0] == 0 and runner.run("is-active", "postgrest.service")[0] == 0:
        dependencies = {"palladium.service", "postgrest.service"}
        dependency_probes = [probe for probe in PROBES if probe.service in dependencies and not probe.public]
        ready = {probe.service for probe in dependency_probes} == dependencies and all(
            _retry_probe(probe)[0] for probe in dependency_probes
        )
        if ready:
            runner.run("reset-failed", "strm-bridge.service")
            code, output = runner.run("start", "strm-bridge.service")
            print(f"{'PASS' if code == 0 else 'FAIL'} oneshot strm-bridge.service" + (f" ({output})" if code and output else ""))
            if code != 0:
                failures.append("oneshot:strm-bridge.service")
        else:
            print("FAIL oneshot strm-bridge.service (dependency readiness failed; not started)")
            failures.append("oneshot:strm-bridge.service")

    for probe in PROBES:
        if probe.public and not include_public:
            continue
        healthy, detail = _retry_probe(probe)
        if not healthy and repair and not probe.public:
            # An active process with a dead port is not healthy. Restart only its owning unit.
            runner.run("restart", probe.service)
            healthy, detail = _retry_probe(probe)
        print(f"{'PASS' if healthy else 'FAIL'} probe {probe.name}: {detail}")
        if not healthy:
            failures.append(f"probe:{probe.name}")

    if failures:
        print("FAILED " + ", ".join(failures))
        return 1
    print("ALL MANAGED SERVICES HEALTHY")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-only", action="store_true", help="report failures without starting/restarting services")
    parser.add_argument("--no-public", action="store_true", help="skip public anthood.net probes")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return validate_services(repair=not args.check_only, include_public=not args.no_public)


if __name__ == "__main__":
    sys.exit(main())
