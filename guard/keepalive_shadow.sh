#!/usr/bin/env bash
# ============================================================
# 影子止损守护 · 每小时保活脚本（由定时任务每小时调用一次）
# 规则：
#   - guard/STOP 存在 = 用户主动停用 -> 不拉起、静默退出
#   - 守护进程存活（pid 且 kill -0 成功）-> 静默退出
#   - 进程不在且无 STOP 标志 -> setsid 后台拉起，并记录结果
# 本脚本只做“进程保活”，不做任何交易、不下单、不撤单。
# ============================================================
# 工作目录 = 本脚本所在目录的上一级（clone 后即可运行，无需硬编码路径）
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDF="$BASE/guard/shadow_guard.pid"
STOPF="$BASE/guard/STOP"
LOG="$BASE/guard/keepalive.log"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

# 判断守护是否“真正”在运行：pid 存活 且 /proc/<pid>/cmdline 确实是 shadow_stop_guard.py。
# 只做 kill -0 会被 PID 复用骗过——守护退出后该 pid 可能被系统重新分配给别的进程（实测被 chrome 占用），
# 从而误判 alive、不再拉起。故必须叠加 cmdline 身份校验。
guard_alive() {
  local p="$1"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null || return 1
  tr '\0' ' ' < "/proc/$p/cmdline" 2>/dev/null | grep -q "shadow_stop_guard.py"
}

if [ -f "$STOPF" ]; then
  echo "[$(ts)] 存在 STOP 主动停用标志，跳过保活不拉起。" >> "$LOG"
  exit 0
fi

if [ -f "$PIDF" ] && guard_alive "$(cat "$PIDF")"; then
  echo "[$(ts)] alive pid=$(cat "$PIDF")" >> "$LOG"
  exit 0
fi

echo "[$(ts)] 守护未运行（或记录 pid 已被其他进程复用），开始拉起..." >> "$LOG"
cd "$BASE" || { echo "[$(ts)] 进入工作目录失败" >> "$LOG"; exit 1; }
nohup setsid python3 shadow_stop_guard.py </dev/null >>guard/daemon.out 2>&1 &
disown 2>/dev/null || true
sleep 8
if [ -f "$PIDF" ] && guard_alive "$(cat "$PIDF")"; then
  echo "[$(ts)] 已拉起 pid=$(cat "$PIDF")" >> "$LOG"
else
  echo "[$(ts)] RESTART FAILED 拉起失败或身份校验未通过，需人工检查" >> "$LOG"
  exit 1
fi
