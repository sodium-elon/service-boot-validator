from __future__ import annotations

from pathlib import Path

import pytest

import boot_validator


def _write_fake_process(proc_root: Path, pid: int, comm: str, cmdline: str) -> None:
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    (process / "comm").write_text(f"{comm}\n")
    (process / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0")


def test_quarantines_stale_postgres_pid_and_socket_locks(tmp_path: Path) -> None:
    pgdata = tmp_path / "data" / "postgres"
    socket_dir = tmp_path / "data" / "postgres-socket"
    proc_root = tmp_path / "proc"
    quarantine_dir = tmp_path / "quarantine"
    pgdata.mkdir(parents=True)
    socket_dir.mkdir(parents=True)

    pid = 1536
    (pgdata / "postmaster.pid").write_text(f"{pid}\n{pgdata}\n")
    (socket_dir / ".s.PGSQL.5432.lock").write_text(f"{pid}\n{pgdata}\n")
    _write_fake_process(proc_root, pid, "python3", "/usr/bin/python3 notifier.py")

    moved = boot_validator.quarantine_stale_postgres_artifacts(
        pgdata=pgdata,
        socket_dir=socket_dir,
        proc_root=proc_root,
        quarantine_dir=quarantine_dir,
        port_open=False,
        timestamp=123,
    )

    assert sorted(path.name for path in moved) == [
        ".s.PGSQL.5432.lock.stale.123",
        "postmaster.pid.stale.123",
    ]
    assert not (pgdata / "postmaster.pid").exists()
    assert not (socket_dir / ".s.PGSQL.5432.lock").exists()


def test_keeps_postgres_locks_when_owner_is_real_postgres(tmp_path: Path) -> None:
    pgdata = tmp_path / "data" / "postgres"
    socket_dir = tmp_path / "data" / "postgres-socket"
    proc_root = tmp_path / "proc"
    pgdata.mkdir(parents=True)
    socket_dir.mkdir(parents=True)

    pid = 42
    pidfile = pgdata / "postmaster.pid"
    socket_lock = socket_dir / ".s.PGSQL.5432.lock"
    pidfile.write_text(f"{pid}\n{pgdata}\n")
    socket_lock.write_text(f"{pid}\n{pgdata}\n")
    _write_fake_process(proc_root, pid, "postgres", f"postgres -D {pgdata}")

    moved = boot_validator.quarantine_stale_postgres_artifacts(
        pgdata=pgdata,
        socket_dir=socket_dir,
        proc_root=proc_root,
        quarantine_dir=tmp_path / "quarantine",
        port_open=False,
        timestamp=123,
    )

    assert moved == []
    assert pidfile.exists()
    assert socket_lock.exists()


def test_never_touches_postgres_locks_while_port_is_open(tmp_path: Path) -> None:
    pgdata = tmp_path / "postgres"
    socket_dir = tmp_path / "socket"
    pgdata.mkdir()
    socket_dir.mkdir()
    pidfile = pgdata / "postmaster.pid"
    pidfile.write_text("999999\n")

    moved = boot_validator.quarantine_stale_postgres_artifacts(
        pgdata=pgdata,
        socket_dir=socket_dir,
        proc_root=tmp_path / "proc",
        quarantine_dir=tmp_path / "quarantine",
        port_open=True,
        timestamp=123,
    )

    assert moved == []
    assert pidfile.exists()


@pytest.mark.parametrize(
    ("status", "healthy"),
    [(200, True), (404, True), (499, True), (500, False), (502, False), (0, False)],
)
def test_http_health_rejects_transport_failures_and_server_errors(status: int, healthy: bool) -> None:
    assert boot_validator.http_status_is_healthy(status) is healthy


class FakeSystemctl:
    def __init__(self) -> None:
        self.active = False
        self.commands: list[tuple[str, ...]] = []

    def run(self, *args: str) -> tuple[int, str]:
        self.commands.append(args)
        if args[:2] == ("is-active", "demo.service"):
            return (0, "active") if self.active else (3, "inactive")
        if args[:2] == ("reset-failed", "demo.service"):
            return 0, ""
        if args[:2] == ("start", "demo.service"):
            self.active = True
            return 0, ""
        raise AssertionError(args)


def test_ensure_user_service_starts_an_inactive_unit() -> None:
    runner = FakeSystemctl()

    result = boot_validator.ensure_user_service(
        "demo.service", runner=runner, attempts=1, delay_seconds=0
    )

    assert result is True
    assert ("reset-failed", "demo.service") in runner.commands
    assert ("start", "demo.service") in runner.commands


def test_check_only_does_not_require_retired_garmin_mcp(monkeypatch) -> None:
    from types import SimpleNamespace

    commands = []

    class Runner:
        def run(self, *args):
            commands.append(args)
            return (4, "not-found") if "garmin-mcp.service" in args else (0, "active")

    monkeypatch.setattr(boot_validator, "UserSystemctl", Runner)
    monkeypatch.setattr(boot_validator.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(
        boot_validator, "_retry_probe",
        lambda probe: (probe.service != "garmin-mcp.service", "fixture"),
    )

    assert boot_validator.validate_services(repair=False, include_public=False) == 0
    assert all("garmin-mcp.service" not in command for command in commands)
    assert all(command[0] == "is-active" for command in commands)


def _mock_validation_io(monkeypatch, postgrest_statuses):
    from types import SimpleNamespace

    events = []
    statuses = iter(postgrest_statuses)
    last_status = postgrest_statuses[-1]

    class Runner:
        def run(self, *args):
            events.append(args)
            return 0, "active"

    def http_status(url):
        nonlocal last_status
        if url == "http://127.0.0.1:3110/":
            last_status = next(statuses, last_status)
            events.append(("postgrest", last_status))
            return last_status
        return 200

    monkeypatch.setattr(boot_validator, "UserSystemctl", Runner)
    monkeypatch.setattr(boot_validator.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(boot_validator, "tcp_open", lambda *a, **kw: True)
    monkeypatch.setattr(boot_validator, "http_status", http_status)
    monkeypatch.setattr(boot_validator, "quarantine_stale_postgres_artifacts", lambda **kw: [])
    monkeypatch.setattr(boot_validator.time, "sleep", lambda seconds: None)
    return events


def test_strm_waits_for_postgrest_readiness_before_generation(monkeypatch) -> None:
    events = _mock_validation_io(monkeypatch, [503, 200])

    assert boot_validator.validate_services(repair=True, include_public=False) == 0
    assert events.index(("postgrest", 200)) < events.index(("start", "strm-bridge.service"))
    assert events.index(("postgrest", 503)) < events.index(("postgrest", 200))


@pytest.mark.parametrize("failure", ["postgrest", "postgres", "missing-probe"])
def test_strm_is_not_started_when_dependency_readiness_fails(monkeypatch, failure) -> None:
    events = _mock_validation_io(monkeypatch, [503] if failure == "postgrest" else [200])
    if failure == "postgres":
        monkeypatch.setattr(boot_validator, "tcp_open", lambda *a, **kw: False)
    if failure == "missing-probe":
        monkeypatch.setattr(
            boot_validator, "PROBES",
            [probe for probe in boot_validator.PROBES if probe.service != "postgrest.service"],
        )

    assert boot_validator.validate_services(repair=True, include_public=False) == 1
    assert ("start", "strm-bridge.service") not in events
    assert ("reset-failed", "strm-bridge.service") not in events


def test_check_only_never_generates_strm_or_controls_services(monkeypatch) -> None:
    events = _mock_validation_io(monkeypatch, [200])

    assert boot_validator.validate_services(repair=False, include_public=False) == 0
    assert all(event[0] in {"is-active", "postgrest"} for event in events)
