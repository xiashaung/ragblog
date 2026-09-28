#!/usr/bin/env bash
# 质量门：提交前 / CI 一键把「能自动判对错」的检查全跑一遍。
#
# 用法：
#   ./scripts/check.sh          # lint + 测试（门禁），mypy 仅提示不拦门
#   ./scripts/check.sh --lint   # 只跑 ruff
#   ./scripts/check.sh --test   # 只跑 pytest
#
# 门禁口径：
#   - ruff、pytest 是硬门——任一失败即非零退出，挡在提交/合并前；
#   - mypy 全量 strict 属历史欠账（存量未清），这里只做基线提示，
#     不让它拦门，免得每天被无关的类型报错淹没；清账后改成硬门即可。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 解释器：显式 PYTHON > 本地 .venv > PATH 上的 python3
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
  else
    PY="python3"
  fi
fi

run_lint() {
  echo "==> ruff check（lint）"
  "$PY" -m ruff check app tests
}

run_test() {
  echo "==> pytest（测试）"
  "$PY" -m pytest -q
}

run_types() {
  # mypy 没装就跳过；装了则跑一遍但忽略退出码（仅提示）
  if "$PY" -m mypy --version >/dev/null 2>&1; then
    echo "==> mypy（类型检查，非门禁，仅基线提示）"
    if ! "$PY" -m mypy app; then
      echo "   （mypy 未通过：已知历史欠账，不影响本次质量门结果）" >&2
    fi
  else
    echo "==> mypy 未安装，跳过类型检查"
  fi
}

case "${1:-all}" in
  --lint) run_lint ;;
  --test) run_test ;;
  ""|all)
    run_lint
    run_test
    run_types
    ;;
  *)
    echo "用法: $0 [--lint|--test]" >&2
    exit 1
    ;;
esac

echo "==> 质量门通过"
