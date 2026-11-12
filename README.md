# Palladium boot service validator

A boot-time validator and safe recovery tool for John's long-lived services.

## What it does

- Starts inactive or dependency-failed user services in dependency order.
- Repairs stale PostgreSQL PID/socket artifacts only when port 5432 is closed and no real PostgreSQL process owns them.
- Runs the `strm-bridge.service` oneshot only after PostgreSQL TCP and PostgREST HTTP readiness probes pass, not merely after their units become active. Failed or missing dependency probes leave generation unstarted and fail validation.
- Garmin retrieval belongs to the pinned Personal API CLI consumed by AlphaRunner/Sportscoach. The retired MCP service and port are not boot requirements or recovery targets.
- Verifies system Caddy, local TCP/HTTP endpoints, and production `anthood.net` routes.
- Returns nonzero and leaves exact failures in the user journal.

It deliberately does not validate the `3000` or Storybook Caddy routes because those are development slots, not boot services.

## Commands

```bash
# Full validation with safe local recovery
python3 boot_validator.py

# Observe only
python3 boot_validator.py --check-only

# Ignore public DNS/tunnel routes
python3 boot_validator.py --check-only --no-public

# Boot-run logs
journalctl --user -u service-boot-validator.service -b --no-pager
```

The timer runs once, 45 seconds after boot, giving ordinary services time to settle first.
