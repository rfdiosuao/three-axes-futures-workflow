#!/usr/bin/env bash
# ============================================================
# 三板斧期货工作流 · 一键安装脚本（只读体系，不做任何交易）
# 作用：安装 Python 依赖 + panda CLI + lark-cli
# 注意：两个 CLI 的「登录授权」需本人在浏览器完成，脚本无法代替。
# 用法：bash install.sh
# 支持：Linux / macOS；Windows 请在 Git Bash / WSL 下运行
# ============================================================
set -e

echo "============================================================"
echo "  三板斧期货工作流 · 环境安装"
echo "============================================================"

# ---------- 0. 前置检查 ----------
command -v python3 >/dev/null 2>&1 || { echo "❌ 未找到 python3，请先安装 Python >= 3.10"; exit 1; }
command -v npm     >/dev/null 2>&1 || { echo "❌ 未找到 npm，请先安装 Node.js >= 18（含 npm）"; exit 1; }
PY_VER=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
echo "✓ Python $PY_VER ; Node $(node -v 2>/dev/null) ; npm $(npm -v 2>/dev/null)"

# ---------- 1. Python 依赖 ----------
echo ""
echo "【1/3】安装 Python 依赖（akshare/pandas/numpy/requests ...）"
if [ -d ".venv" ]; then
  echo "  检测到 .venv，安装到虚拟环境"
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -r requirements-lock.txt
else
  echo "  安装到当前 Python（如需隔离，可先建 .venv 后重跑）"
  pip install -r requirements-lock.txt
fi
python3 -c "import akshare,pandas,numpy,requests;print('  ✓ Python 依赖就绪，akshare',akshare.__version__)"

# ---------- 2. panda CLI ----------
echo ""
echo "【2/3】安装 panda CLI（PandaAI 期货仿真，npm 全局包）"
PANDA_PKG="@chongqingliangyunzhijing/contest-cli"
if command -v panda >/dev/null 2>&1; then
  echo "  已安装：$(panda --version 2>&1 | head -1)"
  read -r -p "  是否更新到最新？[y/N] " ans
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then npm install -g "$PANDA_PKG"; fi
else
  npm install -g "$PANDA_PKG"
  echo "  ✓ panda 已安装：$(panda --version 2>&1 | head -1)"
fi

# ---------- 3. lark-cli ----------
echo ""
echo "【3/3】安装 lark-cli（飞书官方 CLI）"
if command -v lark-cli >/dev/null 2>&1; then
  echo "  已安装：$(lark-cli --version 2>&1 | head -1)"
else
  npx --yes @larksuite/cli@latest install
  echo "  ✓ lark-cli 已安装"
fi

# ---------- 4. 登录提示（需本人操作）----------
echo ""
echo "============================================================"
echo "  代码/工具安装完成。接下来请本人完成一次性授权："
echo ""
echo "  ① panda（PandaAI 仿真）："
echo "       panda login        # 浏览器授权"
echo "       panda whoami       # 验证"
echo ""
echo "  ② lark-cli（飞书）："
echo "       lark-cli config init        # 填自建应用 App ID/Secret"
echo "       lark-cli auth login --recommend"
echo "       lark-cli auth status        # 验证"
echo ""
echo "  ③ 验证只读脚本："
echo "       python3 monitor_positions.py"
echo "       python3 shadow_stop_guard.py --selftest"
echo ""
echo "  详细说明见 docs/SETUP.md；定时任务见 docs/CRON_TASKS.md"
echo "============================================================"
