#!/bin/bash
# ============================================================
# 🐙 章鱼AI · 手动推送
# 清理 output/ 目录历史 HTML 报告 → 重新抓取最新数据 → 生成日报 →
# 当天内容检验 → 推送到微信
#
# 用法:
#   ./output/manual_push.sh              # 当天检验通过才推送（推荐）
#   ./output/manual_push.sh --force      # 内容非当天也强制推送（谨慎）
#   ./output/manual_push.sh --no-push    # 只重新生成日报，不推送
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="$REPO_DIR/output"

if ! cd "$REPO_DIR"; then
    echo "❌ 错误：无法切换到仓库目录 $REPO_DIR"
    exit 1
fi

log() {
    echo "[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S CST')] 🐙 $*"
}

# 解析参数，决定是否清理 + 是否推送
ARGS=()
MODE="手动推送（当天检验通过才推送）"
CLEAN=true  # 默认清理；--push-only / --list 由 pipeline 内部按需跳过
case "${1:-}" in
    --force)
        ARGS+=(--force-push)
        MODE="手动强制推送（内容非当天也推送，谨慎）"
        ;;
    --no-push)
        ARGS+=(--no-push)
        MODE="手动重新生成日报（不推送）"
        ;;
    "")
        ;;
    *)
        log "未知参数：$1（支持 --force / --no-push）"
        exit 1
        ;;
esac

log "====== $MODE ======"

# 0. 清理 output/ 目录下所有历史 HTML 报告（含 daily_report_*.html + latest.html）
#    避免历史残留文件（含旧版本特征）被误推或被 latest.html 引用
if [ "$CLEAN" = true ]; then
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
fi

log "执行: python3 output/pipeline.py --manual ${ARGS[*]:-}"

# 用进程替换避免子 shell + 退出码误判问题
while IFS= read -r line || [[ -n "$line" ]]; do
    log "$line"
done < <(python3 "$SCRIPT_DIR/pipeline.py" --manual "${ARGS[@]}" 2>&1)

log "手动推送任务结束"
