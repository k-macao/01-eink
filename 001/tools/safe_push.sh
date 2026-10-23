#!/usr/bin/env bash
# ============================================================
# 🐙 章鱼AI · 安全提交推送（GitHub Actions / 本地 cron 共用）
#
# 解决的问题（2026-10-04 事故复盘）：
#   工作流从「检出那一刻的 main」出发跑 2~15 分钟，再把生成物提交推回 main。
#   这段时间里 main 只要被别人推动一次（另一个工作流、手动重跑、一个 PR 合并），
#   `git push origin HEAD` 就会被拒：
#       ! [rejected]        HEAD -> main (fetch first)
#       error: failed to push some refs to 'https://github.com/k-macao/02'
#   已实测到的三次：① 与 PR 合并只差 5 秒（run 37169109384）
#                  ② 与「手动抓取推送」并行（run 37144889286）
#                  ③ 重跑一天前的旧 run，本地落后 26 个提交（run 37054889086 #2）
#   推送被拒不等于「白跑一次」：日报 HTML 与**预测留痕 JSON**都没进库，
#   留痕丢一次，命中率统计就少一条样本（详见 自动更新推送被拒-原因诊断.md）。
#
# 做法：被拒后把「本次提交」重放到远端最新提交之上，再推；最多 --attempts 次。
#   · 非生成物（代码 / 文档 / README）：一律保留远端版本，绝不回退别人的改动；
#   · 生成物 HTML：以本次为准（本次是更新的一轮运行）；
#   · 留痕 JSON：交给 tools/merge_generated.py 按键做并集，两边条目都不丢；
#   · 自校验文件（output/market_db/*.json 带 sha256）：整份取本次并告警；
#   · 不是「远端更新」类的失败（权限 / 保护分支 / 钩子拒绝）：立即失败，不做无谓重试。
#
# 用法：
#   bash tools/safe_push.sh --message "📰 自动更新 2026-10-04 09:50" \
#        --branch main -- output/latest.html output/news_history.json
#
#   --branch    目标分支（默认 $GITHUB_REF_NAME，其次当前分支，最后 main）
#   --message   提交信息（必填）
#   --attempts  最多推送尝试次数（默认 5）
#   --delay     重试基础等待秒数，线性递增 + 抖动（默认 3；设 0 关闭等待）
#   --user/--email  提交身份（默认 octopus-bot / bot@octopus.ai）
#   --          其后全部是 git pathspec（只有这些路径会被提交）
#
# 退出码：0 推送成功 / 无新内容 / 远端已含相同内容；1 推送最终失败；2 用法错误。
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MERGE_TOOL="${OCTOPUS_MERGE_TOOL:-$SCRIPT_DIR/merge_generated.py}"
PYTHON_BIN="${PYTHON:-python3}"

BRANCH="${GITHUB_REF_NAME:-}"
MESSAGE=""
ATTEMPTS=5
BASE_DELAY=3
PUSH_USER="${GIT_PUSH_USER:-octopus-bot}"
PUSH_EMAIL="${GIT_PUSH_EMAIL:-bot@octopus.ai}"
PATHS=()

log()  { echo "[$(TZ='Asia/Shanghai' date '+%H:%M:%S')] 📤 $*"; }
warn() { echo "::warning::$*"; }
die()  { echo "::error::$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --branch)        BRANCH="${2:-}";     shift 2 ;;
        --message|-m)    MESSAGE="${2:-}";    shift 2 ;;
        --attempts)      ATTEMPTS="${2:-5}";  shift 2 ;;
        --delay)         BASE_DELAY="${2:-3}"; shift 2 ;;
        --user)          PUSH_USER="${2:-}";  shift 2 ;;
        --email)         PUSH_EMAIL="${2:-}"; shift 2 ;;
        -h|--help)       sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
        --)              shift; while [ $# -gt 0 ]; do PATHS+=("$1"); shift; done ;;
        *)               PATHS+=("$1"); shift ;;
    esac
done

[ -n "$MESSAGE" ] || { echo "❌ 缺少 --message" >&2; exit 2; }
[ "${#PATHS[@]}" -gt 0 ] || { echo "❌ 缺少要提交的 pathspec（-- 之后列出）" >&2; exit 2; }

git rev-parse --git-dir >/dev/null 2>&1 || { echo "❌ 当前目录不在 git 仓库内" >&2; exit 2; }
cd "$(git rev-parse --show-toplevel)"
if [ -z "$BRANCH" ]; then
    BRANCH="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo main)"
fi

git config user.name  "$PUSH_USER"
git config user.email "$PUSH_EMAIL"

# ------------------------------------------------------------
# 1) 暂存 + 提交（pathspec 逐个添加：某个通配符没匹配到不该让整步失败）
# ------------------------------------------------------------
for spec in "${PATHS[@]}"; do
    git add -A -- "$spec" >/dev/null 2>&1 || true
done

if git diff --cached --quiet; then
    log "无新内容，跳过提交。"
    exit 0
fi

git commit --quiet -m "$MESSAGE"
OUR_COMMIT="$(git rev-parse HEAD)"
log "已提交 $(git rev-parse --short "$OUR_COMMIT")：$MESSAGE"

# ------------------------------------------------------------
# 2) 把「本次提交」的改动重放到远端最新提交之上
#    返回 0 = 重放完成（HEAD 已在远端 tip 之上）；1 = 重放失败；
#    2 = 远端 tip 就是我们自己（推送被拒另有原因，不该重试）
# ------------------------------------------------------------
replay_onto_remote() {
    local tip base status relpath policy tmp_local tmp_out
    local -a changed=()

    if ! git fetch --quiet origin "+refs/heads/${BRANCH}:refs/remotes/origin/${BRANCH}" 2>/dev/null; then
        git fetch --quiet origin "$BRANCH" || { warn "拉取远端 ${BRANCH} 失败"; return 1; }
    fi
    tip="$(git rev-parse FETCH_HEAD)"
    if [ "$tip" = "$(git rev-parse HEAD)" ]; then
        return 2
    fi
    # 本次提交的父提交；万一本次是仓库第一个提交（没有父），用空树当基准
    base="$(git rev-parse --verify --quiet "${OUR_COMMIT}^" || git hash-object -w -t tree /dev/null)"
    log "远端 ${BRANCH} 已前进到 $(git rev-parse --short "$tip")，把本次改动重放上去…"

    # 本次提交只动了这些文件；重放时只碰它们，其它一律用远端版本（绝不回退别人的改动）
    git reset --hard --quiet "$tip"
    while IFS= read -r -d '' status && IFS= read -r -d '' relpath; do
        changed+=("$relpath")
        case "$status" in
            D*)
                git rm --quiet --ignore-unmatch -- "$relpath" >/dev/null 2>&1 || true
                continue
                ;;
        esac
        policy=local
        case "$relpath" in
            *.json|*.jsonl)
                if [ -f "$relpath" ]; then
                    policy="$("$PYTHON_BIN" "$MERGE_TOOL" policy "$relpath" 2>/dev/null || echo local)"
                fi
                ;;
        esac
        if [ "$policy" != "local" ]; then
            tmp_local="$(mktemp)"; tmp_out="$(mktemp)"
            if git show "${OUR_COMMIT}:${relpath}" > "$tmp_local" 2>/dev/null \
               && "$PYTHON_BIN" "$MERGE_TOOL" merge "$relpath" "$relpath" "$tmp_local" -o "$tmp_out" \
               && [ -s "$tmp_out" ]; then
                cat "$tmp_out" > "$relpath"
            else
                warn "${relpath} 合并失败 → 退回「以本次为准」"
                git checkout "$OUR_COMMIT" -- "$relpath" 2>/dev/null || true
            fi
            rm -f "$tmp_local" "$tmp_out"
        else
            git checkout "$OUR_COMMIT" -- "$relpath" 2>/dev/null || true
        fi
    done < <(git diff --name-status -z --no-renames "$base" "$OUR_COMMIT")

    if [ "${#changed[@]}" -gt 0 ]; then
        for relpath in "${changed[@]}"; do
            git add -A -- "$relpath" >/dev/null 2>&1 || true
        done
    fi

    if git diff --cached --quiet; then
        log "远端已包含相同内容，本次无需再提交。"
        return 0
    fi
    git commit --quiet -m "$MESSAGE"
    OUR_COMMIT="$(git rev-parse HEAD)"
    log "重放完成 → $(git rev-parse --short "$OUR_COMMIT")（父提交 $(git rev-parse --short "$tip")）"
    return 0
}

# ------------------------------------------------------------
# 3) 推送：被拒 → 重放 → 再推
# ------------------------------------------------------------
attempt=0
while :; do
    attempt=$((attempt + 1))
    push_log="$(mktemp)"
    set +e
    git push origin "HEAD:refs/heads/${BRANCH}" > "$push_log" 2>&1
    push_rc=$?
    set -e
    cat "$push_log"

    if [ "$push_rc" -eq 0 ]; then
        rm -f "$push_log"
        if [ "$attempt" -eq 1 ]; then
            log "✅ 推送成功 → origin/${BRANCH}"
        else
            log "✅ 推送成功（第 ${attempt} 次尝试）→ origin/${BRANCH}"
        fi
        exit 0
    fi

    # 只重试「远端更新导致的非快进被拒」；权限 / 保护分支 / 钩子拒绝立即失败
    if ! grep -qE '\(fetch first\)|\(stale info\)|non-fast-forward|remote contains work that you do not have' "$push_log"; then
        rm -f "$push_log"
        die "推送失败，且不是「远端已前进」类型（见上方 git 输出），不做重试。"
    fi
    rm -f "$push_log"

    if [ "$attempt" -ge "$ATTEMPTS" ]; then
        die "连续 ${ATTEMPTS} 次推送被拒（远端一直在动），放弃；本次生成物未入库。工作流的「🧯 推送失败兜底」步骤会把它们存成 Artifact（保留 14 天），可下载后人工补提交，或直接重跑本工作流。"
    fi

    warn "第 ${attempt} 次推送被拒：远端 ${BRANCH} 在本次运行期间被推动，准备重放"
    set +e
    replay_onto_remote
    replay_rc=$?
    set -e
    if [ "$replay_rc" -eq 2 ]; then
        die "远端 ${BRANCH} 的最新提交就是本地 HEAD，推送却被拒——多半是权限 / 保护分支 / 钩子问题，不做重试。"
    fi
    if [ "$replay_rc" -ne 0 ]; then
        die "重放到远端最新提交失败，放弃推送（本次生成物未入库）。"
    fi

    if [ "$BASE_DELAY" -gt 0 ] 2>/dev/null; then
        sleep $(( BASE_DELAY * attempt + RANDOM % 3 ))
    fi
done
