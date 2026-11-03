#!/usr/bin/env bash
set -euo pipefail
studio_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
runtime="${PYTHON_BIN:-$studio_dir/.venv/bin/python}"
if [[ ! -x "$runtime" ]]; then
  echo '请先按 README 安装独立 Python 环境和 requirements.txt。' >&2
  exit 1
fi
export LANGSMITH_TRACING=false
export LANGCHAIN_TRACING_V2=false
cd -- "$studio_dir/.."
exec "$runtime" -m video_agent_studio.app
