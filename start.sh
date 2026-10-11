#!/usr/bin/env bash
# 启动转文字工坊：项目内环境、组件检查、本地服务。
set -euo pipefail
cd "$(dirname "$0")"
export PATH="$PWD/runtime/bin:$PATH"

if [ ! -x venv/bin/python ]; then
  selected_python="${PYTHON_BIN:-}"
  if [ -z "$selected_python" ]; then
    for candidate in python3.12 python3.11 python3.13 python3; do
      if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(not ((3, 10) <= sys.version_info < (3, 14)))' 2>/dev/null; then
        selected_python="$(command -v "$candidate")"
        break
      fi
    done
  fi
  if [ -z "$selected_python" ] || ! "$selected_python" -c 'import sys; sys.exit(not ((3, 10) <= sys.version_info < (3, 14)))'; then
    echo "需要 Python 3.10–3.13（推荐 3.12）。可通过 PYTHON_BIN=/完整路径/python3 ./start.sh 指定。" >&2
    exit 1
  fi
  echo "创建虚拟环境…"
  "$selected_python" -m venv venv
fi

requirements_digest="$(./venv/bin/python -c 'import hashlib; print(hashlib.sha256(open("requirements.txt", "rb").read()).hexdigest())')"
installed_digest="$(cat venv/.requirements.sha256 2>/dev/null || true)"
if [ "$requirements_digest" != "$installed_digest" ]; then
  echo "安装项目依赖…"
  ./venv/bin/python -m pip install -r requirements.txt
  printf '%s\n' "$requirements_digest" > venv/.requirements.sha256
fi

for tool in ffmpeg ffprobe; do
  if ! command -v "$tool" >/dev/null 2>&1 || ! "$tool" -version >/dev/null 2>&1; then
    echo "缺少可运行的 $tool。请安装 FFmpeg，或将适合当前系统的程序放入 runtime/bin/。" >&2
    exit 1
  fi
done

port="${PORT:-8000}"
if ! ./venv/bin/python -c 'import sys; value=sys.argv[1]; sys.exit(not (value.isdigit() and 1 <= int(value) <= 65535))' "$port"; then
  echo "PORT 必须是 1–65535 的整数。" >&2
  exit 1
fi
echo "服务启动：http://127.0.0.1:$port"
exec ./venv/bin/python -m uvicorn backend:app --host 127.0.0.1 --port "$port"
