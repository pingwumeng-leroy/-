#!/usr/bin/env bash
# douyin-studio 服务器升级 + 自检
# 在服务器上执行（需要 root / sudo）：
#   sudo bash deploy_server.sh               # 拉代码 + 装依赖(含 faster-whisper) + 修权限 + 重启 + 自检
#   sudo bash deploy_server.sh --no-asr      # 跳过 faster-whisper，只跑轻量档
#   sudo bash deploy_server.sh --check-only  # 不改代码，只做健康检查与 DeepSeek 自检
#
# 脚本不会写入任何密码 / Token。config/settings.json 永远只由你在设置页填写。
set -euo pipefail

APP="${APP_DIR:-/opt/douyin-studio}"
WITH_ASR=1
CHECK_ONLY=0
for a in "$@"; do
  case "$a" in
    --no-asr)     WITH_ASR=0 ;;
    --check-only) CHECK_ONLY=1 ;;
    *) echo "未知参数：$a（可用：--no-asr / --check-only）" >&2; exit 2 ;;
  esac
done

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[提示] %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m[错误] %s\033[0m\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then die "请用 sudo 执行：sudo bash $0"; fi

SVC="$(systemctl show -p User --value douyin-studio 2>/dev/null || true)"
SVC="${SVC:-www-data}"
say "服务运行用户：$SVC ｜ 项目目录：$APP"

if [ "$CHECK_ONLY" -eq 0 ]; then
  # ---- 1 目录与仓库 ----
  say "1/5 检查代码目录"
  [ -d "$APP" ] || die "$APP 不存在。若目录曾被清空，先恢复代码：
  git clone https://github.com/pingwumeng-leroy/-.git /tmp/ds-src
  mkdir -p $APP && cp -a /tmp/ds-src/. $APP/"
  [ -d "$APP/.git" ] || die "$APP 缺 .git，不是 git 仓库；用上面的 clone + cp -a 方式恢复，不要直接 clone 到非空目录。"
  [ -f "$APP/app.py" ] || die "$APP/app.py 缺失，代码目录不完整。"

  # ---- 2 拉代码 ----
  say "2/5 拉取远端 main（升级后 HEAD 应为 78d00e2 或更新）"
  if ! git -C "$APP" pull --ff-only origin main; then
    die "pull 失败。若服务器上有本地改动，先备份再执行：git -C $APP status --short"
  fi
  echo "HEAD = $(git -C "$APP" rev-parse --short HEAD)"
  [ -f "$APP/core/transcript.py" ] || die "core/transcript.py 没拉到，说明拉的不是最新 main。"
  grep -q "use_asr" "$APP/core/harvest.py" || die "harvest.py 缺 use_asr，代码版本不对。"

  # ---- 3 依赖 ----
  say "3/5 安装依赖"
  PIP="$APP/.venv/bin/pip"
  [ -x "$PIP" ] || die "缺虚拟环境。先建：
  python3 -m venv $APP/.venv && $APP/.venv/bin/pip install -r $APP/requirements.txt"
  "$PIP" install -q -r "$APP/requirements.txt"
  if [ "$WITH_ASR" -eq 1 ]; then
    echo "安装 faster-whisper（完整档逐字稿需要）..."
    if "$PIP" install -q "faster-whisper>=1.0"; then
      echo "faster-whisper 已就绪。首次采集会再下载约 500MB 模型到 data/hf_cache（走 hf-mirror.com）。"
    else
      warn "faster-whisper 安装失败（不影响轻量档）。可稍后手动装：$PIP install faster-whisper"
    fi
  else
    warn "已跳过 faster-whisper；逐字稿开关打开时会自动回落轻量档。"
  fi

  # ---- 4 权限 ----
  say "4/5 修正 data / config 权限"
  mkdir -p "$APP/data" "$APP/config"
  chgrp -R "$SVC" "$APP/data" "$APP/config"
  chmod -R g+rwX "$APP/data" "$APP/config"
  chmod o+rx "$APP"
  [ -f "$APP/config/settings.json" ] \
    || warn "缺 config/settings.json：请在网页设置页重新填访问密码、TikHub Token、DeepSeek Key（仓库不含这些密钥）"

  # ---- 5 重启 ----
  say "5/5 重启服务"
  systemctl restart douyin-studio
  sleep 2
fi

# ---- 健康检查 ----
say "健康检查 /healthz"
code="000"
for _ in 1 2 3 4 5; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8080/healthz 2>/dev/null || true)"
  [ "$code" = "200" ] && break
  sleep 2
done
echo "HTTP $code"
if [ "$code" != "200" ]; then
  systemctl status douyin-studio --no-pager -n 30 || true
  die "后端未就绪，看上面服务日志。常见原因：config/ 不可写、密钥格式错误、端口 8080 被占。"
fi

# ---- DeepSeek 真实链路自检 ----
say "DeepSeek 十维分析链路自检（真实 json_mode + 4000 tokens，不采视频、不花 TikHub 钱）"
if [ -f "$APP/check_deepseek.py" ]; then
  if "$APP/.venv/bin/python" "$APP/check_deepseek.py"; then
    echo "自检通过：DeepSeek 十维报告可正常生成。"
  else
    warn "自检未通过。把上面的报错发我，或看 $APP/data/llm_json_error.txt。"
  fi
else
  warn "未找到 check_deepseek.py（在仓库根目录），跳过；可从 GitHub 单独下载后放到 $APP/ 再跑。"
fi

# ---- 交接提示 ----
say "部署完成，接下来在网页里验证逐字稿"
cat <<'EOF'
  1) 设置页 →「逐字稿（完整档）」选「开启 · 下载视频并本地转写」，模型选 small，保存
  2) 用【没采过的关键词】采 1 条（同一 aweme_id 会去重，旧报告不会追溯补稿）
  3) 任务日志应出现「逐字稿 NN 字 / N 段（本地转写）」
  4) 报告详情第四章应出现：前 3 秒原话、前 15 秒原话、语速、信息密度曲线
  5) 若日志出现「逐字稿生成失败，回落轻量档：…」或「AI 十维分析失败，回落规则引擎：…」，
     把冒号后面的原因发我，我按原因修（多数是 play_url 防盗链 / 模型没下下来）
  历史规则引擎报告仍是老版详情页，属正常，不追溯重算。
EOF
