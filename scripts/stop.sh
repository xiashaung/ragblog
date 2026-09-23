#!/usr/bin/env bash
# 停止由 scripts/start.sh 启动的服务（读 logs/app.pid，先优雅 TERM 再 KILL）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PID_FILE="logs/app.pid"
if [ ! -f "$PID_FILE" ]; then
  echo "没有 ${PID_FILE}，服务未在运行（或不是本脚本启动的）"
  exit 0
fi

PID="$(cat "$PID_FILE")"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "pid $PID 已不存在，清理 pid 文件"
  rm -f "$PID_FILE"
  exit 0
fi

echo "==> 停止 pid ${PID}（SIGTERM，等 lifespan 关队列与连接池）"
kill "$PID"
for _ in $(seq 1 15); do
  kill -0 "$PID" 2>/dev/null || { rm -f "$PID_FILE"; echo "==> 已停止"; exit 0; }
  sleep 1
done

echo "!! 15 秒未退出，补发 SIGKILL" >&2
kill -9 "$PID" 2>/dev/null || true
rm -f "$PID_FILE"
echo "==> 已停止"
