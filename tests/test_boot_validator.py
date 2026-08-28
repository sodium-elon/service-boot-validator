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
