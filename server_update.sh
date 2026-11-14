#!/usr/bin/env bash
# 在 douyin-studio 服务器上执行：更新代码 + 装依赖 + 重启 + 自检
# 用法：sudo bash server_update.sh            （含 faster-whisper）
#       sudo bash server_update.sh --no-asr   （跳过语音识别，只更新口播路线）
set -uo pipefail   # 故意不用 -e：单步失败要能看到原因，别整段中断

APP="${APP_DIR:-/opt/douyin-studio}"
WITH_ASR=1
[ "${1:-}" = "--no-asr" ] && WITH_ASR=0

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[提示] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[错误] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 sudo 执行"
[ -d "$APP/.git" ] || die "$APP 不是 git 仓库"
SVC="$(systemctl show -p User --value douyin-studio 2>/dev/null)"; SVC="${SVC:-www-data}"
say "目录 $APP ｜ 服务用户 $SVC"

# ---------- 1 备份当前 HEAD，便于回滚 ----------
say "1/6 记录当前版本"
OLD="$(git -C "$APP" rev-parse HEAD 2>/dev/null || echo none)"
echo "回滚命令：git -C $APP reset --hard $OLD"

# ---------- 2 处理本地改动 ----------
say "2/6 检查本地改动"
if [ -n "$(git -C "$APP" status --porcelain)" ]; then
  DIRTY="$(git -C "$APP" status --porcelain | wc -l)"
  warn "服务器上有 $DIRTY 个文件被改过，先备份成 patch 再丢弃"
  STAMP="$(date +%Y%m%d_%H%M%S)"
  git -C "$APP" diff > "/root/server_local_changes_$STAMP.patch" 2>/dev/null || true
  echo "改动已存到 /root/server_local_changes_$STAMP.patch"
  git -C "$APP" checkout -- . 2>/dev/null
  git -C "$APP" clean -fd -- app.py core static templates 2>/dev/null
fi

# ---------- 3 拉代码 ----------
say "3/6 拉取 origin/main"
if ! git -C "$APP" fetch --depth=1 origin main; then
  die "fetch 失败（网络或仓库权限）。常见原因：服务器访问 github.com 超时。
可以临时换镜像：git -C $APP remote set-url origin https://ghfast.top/https://github.com/pingwumeng-leroy/-.git 再跑一次"
fi
TARGET="$(git -C "$APP" rev-parse FETCH_HEAD)"
git -C "$APP" reset --hard "$TARGET" || die "reset 失败"
echo "HEAD = $(git -C "$APP" rev-parse --short HEAD)"

# 关键文件自检：确认拉到的确实带口播路线
grep -q "def build_poster_talk" "$APP/core/motionkit.py" \
  || die "core/motionkit.py 里没有 build_poster_talk，拉到的不是新版本"
grep -q "talkOverlay" "$APP/core/motionkit.py" || die "缺 talkOverlay，版本不对"
grep -q "uploads/text" "$APP/app.py" || die "app.py 缺字幕上传接口，版本不对"
grep -q "motionTalkMode" "$APP/templates/index.html" || die "页面缺人声来源开关，版本不对"

# ---------- 4 权限 ----------
say "4/6 修正权限"
chgrp -R "$SVC" "$APP/data" "$APP/config" 2>/dev/null
chmod -R g+rwX "$APP/data" "$APP/config" 2>/dev/null
chmod o+rx "$APP" 2>/dev/null

# ---------- 5 依赖 ----------
say "5/6 依赖"
PIP="$APP/.venv/bin/pip"
if [ -x "$PIP" ]; then
  "$PIP" install -q -r "$APP/requirements.txt" && echo "requirements 已同步"
  if [ "$WITH_ASR" -eq 1 ]; then
    echo "安装 faster-whisper（没有 SRT 时靠它自动对时）..."
    "$PIP" install -q "faster-whisper>=1.0" && echo "faster-whisper 就绪" \
      || warn "faster-whisper 装失败：不影响出片，但没字幕时只能按字数估算时间轴"
  fi
  # 确认口播路线依赖的两个外部命令
  command -v ffmpeg  >/dev/null || die "缺 ffmpeg，口播画面叠加无法工作"
  command -v ffprobe >/dev/null || die "缺 ffprobe"
else
  warn "没有 .venv，跳过依赖安装"
fi

# ---------- 6 重启 ----------
say "6/6 重启服务"
systemctl restart douyin-studio
code=000
for _ in 1 2 3 4 5 6; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8080/healthz 2>/dev/null || true)"
  [ "$code" = "200" ] && break
  sleep 2
done
if [ "$code" != "200" ]; then
  systemctl status douyin-studio --no-pager -n 40 || true
  die "后端没起来，回滚：git -C $APP reset --hard $OLD && systemctl restart douyin-studio"
fi
echo "healthz HTTP 200"

say "冒烟测试：字幕上传接口"
TF="$(mktemp /tmp/smoke_XXXX.srt)"
printf '1\n00:00:00,000 --> 00:00:01,500\n冒烟测试\n' > "$TF"
RESP="$(curl -sS -F "file=@$TF" http://127.0.0.1:8080/api/uploads/text || true)"
rm -f "$TF"
echo "$RESP" | grep -q '"ok":true' && echo "接口正常：$RESP" || warn "接口返回异常：$RESP"

say "完成"
cat <<EOF
  版本：$(git -C "$APP" rev-parse --short HEAD)
  回滚：git -C $APP reset --hard $OLD && systemctl restart douyin-studio

  网页里这样验证口播路线：
  1) 第 4 步上传你的口播原片
  2) 第 5 步选「MotionGit 模板」→ 人声来源保持「用第 4 步的口播原声」
  3) 有条件就上传同一条口播的 .srt（不上传则走语音识别）
  4) 出片后任务日志应出现：口播原声 NN.NNs（不合成配音、不变速）
  5) 随后的「对时依据」应为 provided-srt 或 asr；若是 estimate，说明没对上，时间轴不可靠
  6) 日志末尾应出现：口播画面已叠加：N 段
EOF
