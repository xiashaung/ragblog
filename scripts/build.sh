#!/usr/bin/env bash
# 前端构建脚本：产物由 FastAPI 单进程托管（博客挂 /，控制台挂 /admin）。
#
# 用法：
#   ./scripts/build.sh [all|admin|blog]                    # 只在本机构建
#   ./scripts/build.sh all user@host:/path/to/repo         # 构建后把产物 rsync 到服务器
#
# 说明：
#   - admin 产物输出到仓库根的 web/dist（web-vben 的 vite outDir 已指到那里）；
#   - blog 产物输出到 web-blog/dist；两者都被 .gitignore 忽略（dist/），
#     所以「服务器没有 Node/pnpm」时，用本机构建 + rsync 模式推送产物；
#   - base 约定：web-vben vite base=/admin/，web-blog vite base=/，
#     与后端 app/main.py 的挂载路径一一对应，改动必须两侧同步。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${2:-}"

build_admin() {
  echo "==> 构建管理控制台 web-vben（产物：web/dist，挂 /admin）"
  cd "$ROOT/web-vben"
  pnpm install --frozen-lockfile
  pnpm build
}

build_blog() {
  echo "==> 构建博客前台 web-blog（产物：web-blog/dist，挂 /）"
  cd "$ROOT/web-blog"
  pnpm install --frozen-lockfile
  pnpm build
}

case "${1:-all}" in
  admin) build_admin ;;
  blog)  build_blog ;;
  all)   build_admin; build_blog ;;
  *) echo "用法: $0 [all|admin|blog] [user@host:/repo/path]" >&2; exit 1 ;;
esac

if [ -n "$TARGET" ]; then
  HOST="${TARGET%%:*}"
  RPATH="${TARGET#*:}"
  echo "==> rsync 产物到 ${HOST}:${RPATH}"
  if [[ "$1" != "blog" ]]; then
    rsync -avz --delete "$ROOT/web/dist/" "$HOST:$RPATH/web/dist/"
  fi
  if [[ "$1" != "admin" ]]; then
    rsync -avz --delete "$ROOT/web-blog/dist/" "$HOST:$RPATH/web-blog/dist/"
  fi
fi

echo "==> 完成"
