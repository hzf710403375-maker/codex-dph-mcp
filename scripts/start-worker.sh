#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export CODEX_DPH_DSH_CONFIG_HOME="${CODEX_DPH_DSH_CONFIG_HOME:-$HOME/.dsh}"
export CODEX_DPH_DSH_SETTINGS_PROFILE="${CODEX_DPH_DSH_SETTINGS_PROFILE:-desktop}"
export DSH_HOME="${DSH_HOME:-$task_root/.runtime/worker-dsh}"
unset DEEPSEEK_API_KEY
exec "$task_root/.venv/bin/codex-dph" worker
