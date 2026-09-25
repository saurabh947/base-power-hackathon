#!/usr/bin/env bash
# Project-local Postgres + Redis for development. Touches no system services.
# Usage: scripts/dev/services.sh up|down|status
# Ports come from PGPORT (default 5433) and REDIS_PORT (default 6380), so parallel worktrees can't collide.
set -euo pipefail
export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEV="$ROOT/.dev"
PGPORT="${PGPORT:-5433}"
REDIS_PORT="${REDIS_PORT:-6380}"
PGBIN="$(dirname "$(command -v pg_ctl)")"
PGDATA="$DEV/pg-$PGPORT"
case "${1:-status}" in
  up)
    mkdir -p "$DEV"
    [ -d "$PGDATA" ] || "$PGBIN/initdb" -D "$PGDATA" -U postgres --auth=trust -E UTF8 >/dev/null
    "$PGBIN/pg_ctl" -D "$PGDATA" -l "$DEV/pg-$PGPORT.log" -w \
      -o "-p $PGPORT -c listen_addresses=localhost -c unix_socket_directories=" start >/dev/null 2>&1 || true
    "$PGBIN/psql" -h localhost -p "$PGPORT" -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='fleetsim'" | grep -q 1 \
      || "$PGBIN/createdb" -h localhost -p "$PGPORT" -U postgres fleetsim
    redis-cli -p "$REDIS_PORT" ping >/dev/null 2>&1 || redis-server --port "$REDIS_PORT" --save '' --appendonly no \
      --daemonize yes --dir "$DEV" --pidfile "$DEV/redis-$REDIS_PORT.pid" --logfile "$DEV/redis-$REDIS_PORT.log"
    echo "DATABASE_URL=postgresql://postgres@localhost:$PGPORT/fleetsim"
    echo "REDIS_URL=redis://localhost:$REDIS_PORT/0"
    ;;
  down)
    "$PGBIN/pg_ctl" -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
    redis-cli -p "$REDIS_PORT" shutdown nosave >/dev/null 2>&1 || true
    echo "stopped"
    ;;
  status)
    "$PGBIN/pg_isready" -h localhost -p "$PGPORT" || true
    redis-cli -p "$REDIS_PORT" ping 2>/dev/null || echo "redis :$REDIS_PORT down"
    ;;
  *) echo "usage: $0 up|down|status" >&2; exit 2 ;;
esac
