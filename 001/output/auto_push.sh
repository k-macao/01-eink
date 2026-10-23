#!/bin/bash
# ============================================================
# 🐙 章鱼AI · 全自动推送（cron / 定时任务用）
# 清理 output/ 目录历史 HTML 报告 → 重新抓取全网数据 → 分析 → 生成 → 推送
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$REPO_DIR/output"

# 目录切换前置校验，失败给出明确提示
if ! cd "$REPO_DIR"; then
    echo "❌ 错误：无法切换到仓库目录 $REPO_DIR"
    exit 1
fi

# 日志函数：改用兼容性更广的上海时区（与澳门同属东八区）
log() {
    echo "[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S CST')] 🐙 $*"
}

# 0. 清理 output/ 目录下所有历史 HTML 报告
#    避免历史残留文件（含旧版本特征）被误推或被 latest.html 引用
log "清理历史 HTML 报告..."
DELETED=0
# shellcheck disable=SC2086
for f in $OUTPUT_DIR/daily_report_*.html; do
    [ -e "$f" ] || continue
    rm -f -- "$f" && DELETED=$((DELETED + 1))
done
if [ -f "$OUTPUT_DIR/latest.html" ]; then
    rm -f -- "$OUTPUT_DIR/latest.html" && DELETED=$((DELETED + 1))
fi
if [ "$DELETED" -gt 0 ]; then
    log "🧹 已清理 $DELETED 份历史 HTML 报告"
else
    log "无历史 HTML 报告需要清理"
fi

log "====== 自动推送任务启动 ======"

# 如果未显式传参，默认按时段分流：09:00 与 21:00 全流程推送微信，其余盘中时段静默更新（--no-push）
EXTRA_ARGS=("$@")
if [ $# -eq 0 ]; then
    CURRENT_HOUR=$(TZ='Asia/Shanghai' date +%H)
    if [ "$CURRENT_HOUR" != "09" ] && [ "$CURRENT_HOUR" != "21" ]; then
        log "当前时段 ($CURRENT_HOUR:00) 盘中更新：默认静默更新（--no-push），不推微信；早晚 09:00 / 21:00 正常推送"
        EXTRA_ARGS=("--no-push")
    else
        log "当前时段 ($CURRENT_HOUR:00)：全流程生成并推送微信"
    fi
fi

# 用进程替换替代管道，避免子shell + 退出码误判问题
# 同时处理最后一行无换行的边界情况
while IFS= read -r line || [[ -n "$line" ]]; do
    log "$line"
done < <(python3 "$SCRIPT_DIR/pipeline.py" "${EXTRA_ARGS[@]}" 2>&1)

log "任务结束"
