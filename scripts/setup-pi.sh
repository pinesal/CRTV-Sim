#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_ROOT"

python3 -m venv .venv
.venv/bin/python -m pip install --no-deps -e .

PROFILE=config/production.pi.local.toml
if [ ! -f "$PROFILE" ]; then
  cp config/production.pi.example.toml "$PROFILE"
  printf '%s\n' "Created $PROFILE. Review storage roots before continuing."
fi

.venv/bin/python -m retrotv --config "$PROFILE" init-db
printf '%s\n' "CRTV Sim Phase 1 Raspberry Pi setup is complete."

