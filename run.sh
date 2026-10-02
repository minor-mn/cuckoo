#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

if [ ! -f "$SCRIPT_DIR/config.toml" ]; then
  echo "Missing $SCRIPT_DIR/config.toml; copy config.toml.example to config.toml and set the OAuth 1.0a credentials in [auth]." >&2
  exit 1
fi

exec /usr/bin/env python3 "$SCRIPT_DIR/monitor.py" --config "$SCRIPT_DIR/config.toml" "$@"
