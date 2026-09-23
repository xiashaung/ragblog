#!/usr/bin/env bash
# 生产启动（后台运行 + pid 文件 + 日志）。默认单进程 uvicorn：
# 一个进程同时托管博客(/)、控制台(/admin)、API(/api/v1) 与 /media，无需 nginx 配静态。
#
# 用法：
#   ./scripts/start.sh                 # 默认 :8000
#   PORT=8011 ./scripts/start.sh       # 换端口
#
# 注意：
#   - 不要加 --workers 多开：RAG 任务队列的 worker 在每个进程里都会启动一份，
#     多进程会重复消费同一队列；
#   - 前台跑法（交给 systemd 管）：
#       .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8000}"
HOST="${HOST:-0.0.0.0}"
mkdir -p logs

PID_FILE="logs/app.pid"
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "已在运行（pid $(cat "$PID_FILE")），先执行 ./scripts/stop.sh"
  exit 1
fi

[ -d .venv ] || { echo "缺少 .venv：先 python -m venv .venv && .venv/bin/pip install -e ." >&2; exit 1; }

echo "==> 启动 uvicorn ${HOST}:${PORT}（日志 logs/app.log）"
nohup .venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT" \
  >> logs/app.log 2>&1 &
echo $! > "$PID_FILE"

# 等健康检查通过（lifespan 要建表、加载模型配置、起 RAG 队列，给 30 秒余量）
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${PORT}/api/v1/health" > /dev/null; then
    echo "==> OK：pid $(cat "$PID_FILE")，健康检查通过"
    echo "    博客 /            → http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1):${PORT}/"
    echo "    控制台 /admin     → ...:${PORT}/admin/"
    exit 0
  fi
  sleep 1
done

echo "!! 健康检查超时，最近日志：" >&2
tail -n 30 logs/app.log >&2
exit 1
