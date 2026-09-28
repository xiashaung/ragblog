#!/usr/bin/env bash
# 服务器端一键部署：git pull → 同步 Python 依赖 → 数据库迁移 → 重建前端 → 重启 → 健康检查。
#
# 前提（首次部署手工做一遍）：
#   1. git clone git@git.demitasse.cn:xiashuang/fastapi-vue.git && cd fastapi
#   2. python -m venv .venv && .venv/bin/pip install -e .
#   3. cp .env.example .env 并按生产修改（重点：APP_DEBUG=false、APP_CORS_ORIGINS
#      填真实域名、APP_DATABASE_URL 指 PostgreSQL、APP_ADMIN_API_KEY 改掉默认值）
#   4. 需要 Node 时全局装 pnpm（corepack enable 即可）；服务器不装 Node 的话，
#      在本机跑 ./scripts/build.sh all user@host:/path/to/repo 把产物 rsync 过去
#
# 用法：
#   ./scripts/deploy.sh                    # 完整流程（含重启）
#   SKIP_FRONTEND=1 ./scripts/deploy.sh    # 跳过前端构建（产物已 rsync 到位时）
#   MIGRATION_REVISION=xxxx ./scripts/deploy.sh   # 新增迁移后更新目标版本号
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 迁移目标：钉住最新 head。曾经库里有个孤儿 head（g6h7i8j9k0l1 孤挂在
# c2a3b4d5e6f7，导致裸 `upgrade head` 报歧义）；现已把 g6h7 重挂到主线尾部
# 收敛回单 head，且它对存量库幂等（表存在即跳过）。继续钉版本号是显式
# 控制手段：新增迁移后把这里的版本号推进，部署时能看见它没跟上。
MIGRATION_REVISION="${MIGRATION_REVISION:-g6h7i8j9k0l1}"

echo "==> 1/5 拉取代码（分支 $(git rev-parse --abbrev-ref HEAD)）"
git pull --ff-only

echo "==> 2/5 同步 Python 依赖"
[ -d .venv ] || { echo "缺少 .venv：先 python -m venv .venv && .venv/bin/pip install -e ." >&2; exit 1; }
.venv/bin/pip install -e .

echo "==> 3/5 数据库迁移 → ${MIGRATION_REVISION}"
.venv/bin/alembic upgrade "$MIGRATION_REVISION"

echo "==> 4/5 前端构建"
if [ "${SKIP_FRONTEND:-0}" = "1" ]; then
  echo "    SKIP_FRONTEND=1，跳过（确认 web/dist 与 web-blog/dist 已是最新产物）"
elif command -v pnpm > /dev/null; then
  ./scripts/build.sh all
else
  echo "    未装 pnpm/Node：跳过构建，请用本机 ./scripts/build.sh all user@host:$(pwd) 推送产物" >&2
fi
for d in web/dist web-blog/dist; do
  [ -f "$d/index.html" ] || echo "    !! $d 缺 index.html，访问对应页面会 500/兜底缺失" >&2
done

echo "==> 5/5 重启服务"
./scripts/stop.sh
./scripts/start.sh

cat << 'TIP'

部署完成。速查：
  日志     tail -f logs/app.log
  单测     .venv/bin/python -m pytest tests -q
  首次引导  python -m app.bootstrap seed --username admin --password 'xxx'
TIP
