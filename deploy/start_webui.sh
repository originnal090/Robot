#!/bin/sh
set -eu

# Orange Pi Web UI launcher. Override any value in /etc/hcirobot/web.env or
# export HCIROBOT_* variables before running this script.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

ENV_FILE=${HCIROBOT_WEB_ENV_FILE:-/etc/hcirobot/web.env}
if [ -r "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090 -- the deployment environment path is configurable.
    . "$ENV_FILE"
    set +a
fi

CONFIG_PATH=${HCIROBOT_CONFIG:-$PROJECT_DIR/deploy/config/config.orangepi.toml}
LISTEN_HOST=${HCIROBOT_WEB_HOST:-0.0.0.0}
LISTEN_PORT=${HCIROBOT_WEB_PORT:-8090}
ENABLE_GAMEPAD=${HCIROBOT_GAMEPAD:-1}
PYTHON_BIN=${HCIROBOT_PYTHON:-$PROJECT_DIR/.venv/bin/python}
WEB_DEPS=${HCIROBOT_WEB_DEPS:-$PROJECT_DIR/.webdeps}
ASCEND_ENV=${HCIROBOT_ASCEND_ENV:-/usr/local/Ascend/ascend-toolkit/set_env.sh}
ACLLITE_DIR=${HCIROBOT_ACLLITE_DIR:-/usr/local/Ascend/thirdpart/aarch64/acllite}

if [ ! -x "$PYTHON_BIN" ]; then
    printf '%s\n' "Python environment not found: $PYTHON_BIN" >&2
    printf '%s\n' "Run: uv sync --extra web --extra gamepad" >&2
    exit 1
fi
if [ -r "$ASCEND_ENV" ]; then
    # shellcheck disable=SC1090 -- CANN installation location is configurable.
    set +u
    . "$ASCEND_ENV"
    set -u
fi
PYTHONPATH="$PROJECT_DIR/src:$WEB_DEPS:$ACLLITE_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
if [ ! -r "$CONFIG_PATH" ]; then
    printf '%s\n' "Configuration not readable: $CONFIG_PATH" >&2
    exit 1
fi

# Detector profile paths are project-relative in both the desktop and Web UIs.
cd "$PROJECT_DIR"

case "$LISTEN_PORT" in
    ''|*[!0-9]*) printf '%s\n' "HCIROBOT_WEB_PORT must be an integer" >&2; exit 1 ;;
esac
if [ "$LISTEN_PORT" -lt 1 ] || [ "$LISTEN_PORT" -gt 65535 ]; then
    printf '%s\n' "HCIROBOT_WEB_PORT must be between 1 and 65535" >&2
    exit 1
fi
case "$ENABLE_GAMEPAD" in
    0|1) ;;
    *) printf '%s\n' "HCIROBOT_GAMEPAD must be 0 or 1" >&2; exit 1 ;;
esac
case "$LISTEN_HOST" in
    127.0.0.1|localhost|::1) ;;
    *)
        if [ -z "${HCIROBOT_WEB_TOKEN:-}" ]; then
            printf '%s\n' "Set HCIROBOT_WEB_TOKEN in $ENV_FILE before LAN access" >&2
            exit 1
        fi
        ;;
esac

"$PYTHON_BIN" -m hcirobot --config "$CONFIG_PATH" --check-config

set -- -m hcirobot.web --config "$CONFIG_PATH" --host "$LISTEN_HOST" --port "$LISTEN_PORT"
if [ "$ENABLE_GAMEPAD" -eq 1 ]; then
    set -- "$@" --gamepad
fi
if [ -n "${HCIROBOT_WEB_CERTFILE:-}" ] || [ -n "${HCIROBOT_WEB_KEYFILE:-}" ]; then
    if [ -z "${HCIROBOT_WEB_CERTFILE:-}" ] || [ -z "${HCIROBOT_WEB_KEYFILE:-}" ]; then
        printf '%s\n' "HCIROBOT_WEB_CERTFILE and HCIROBOT_WEB_KEYFILE must be set together" >&2
        exit 1
    fi
    set -- "$@" --ssl-certfile "$HCIROBOT_WEB_CERTFILE" --ssl-keyfile "$HCIROBOT_WEB_KEYFILE"
fi

printf '%s\n' "HCIRobot Web UI: http${HCIROBOT_WEB_CERTFILE:+s}://$LISTEN_HOST:$LISTEN_PORT"
printf '%s\n' "Config: $CONFIG_PATH"
printf '%s\n' "Orange Pi gamepad: $ENABLE_GAMEPAD"
exec "$PYTHON_BIN" "$@"
