#!/usr/bin/env bash
# Install the shipped Python dependency set before adding test-only tools.
set -euo pipefail
component="${1:?coordinator or doc-extract is required}"
mode="${2:-runtime}"
case "$component" in coordinator|doc-extract) ;; *) exit 2 ;; esac
case "$mode" in runtime|dev) ;; *) exit 2 ;; esac
python -m pip install --require-hashes -r "$component/requirements.lock"
if [[ "$mode" == dev ]]; then
  constraints=$(mktemp)
  trap 'rm -f "$constraints"' EXIT
  python -m pip freeze > "$constraints"
  if [[ "$component" == coordinator ]]; then
    python -m pip install --constraint "$constraints" -r coordinator/requirements-dev.txt
  else
    python -m pip install --constraint "$constraints" 'pytest>=8,<10'
  fi
fi
python -m pip check
