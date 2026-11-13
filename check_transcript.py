# -*- coding: utf-8 -*-
"""逐字稿链路诊断（只读：不改代码、不采集视频、不花 TikHub 的钱、不调大模型）。

    cd /opt/douyin-studio && sudo .venv/bin/python check_transcript.py
    # 脚本放到别处时：DS_ROOT=/opt/douyin-studio sudo -E .venv/bin/python ~/check_transcript.py

按「设置开关 → 采集任务参数 → 任务日志 → 报告 facts」逐段打印，
直接定位第四章为什么显示「轻量档…没有逐字稿」。把输出整段发我即可。
"""
import json
import os
import sqlite3
import sys

ROOT = os.environ.get("DS_ROOT") or os.path.dirname(os.path.abspath(__file__))
if not os.path.isdir(os.path.join(ROOT, "core")):
    print("找不到项目目录：%s（可用 DS_ROOT=/path 指定）" % ROOT)
    sys.exit(2)

SETTINGS = os.path.join(ROOT, "config", "settings.json")
DB = os.path.join(ROOT, "data", "studio.db")
KEY_LINES = ("完整档", "逐字稿", "play_url", "已入库", "AI 十维", "降级", "完成")


def head(t):
    print("\n" + "=" * 62 + "\n" + t + "\n" + "=" * 62)


def jload(s, fallback):
    try:
        return json.loads(s or "")
    except (ValueError, TypeError):
        return fallback


# ---------- 1 设置里的开关 ----------
head("1 / 设置页开关（config/settings.json）")
use_asr = False
saved_asr = "<从未保存过设置，按 False 处理>"
if not os.path.isfile(SETTINGS):
    print("✗ 缺 config/settings.json：请在网页设置页保存一次")
else:
    s = jload(open(SETTINGS, encoding="utf-8").read(), {})
    h = s.get("harvest") or {}
    saved_asr = h.get("use_asr", "<缺少该键>")
    use_asr = bool(h.get("use_asr", False))
    print("harvest.use_asr      = %s" % saved_asr)
    print("harvest.whisper_model= %s" % h.get("whisper_model", "<缺少该键>"))
    print("harvest.comment_pages= %s" % h.get("comment_pages", 3))
    print("llm.enabled / model  = %s / %s" % (
        (s.get("llm") or {}).get("enabled"), (s.get("llm") or {}).get("model")))
try:
    import faster_whisper  # noqa: F401
    print("faster-whisper       = 已安装 ✓")
except ImportError:
    print("faster-whisper       = 未安装 ✗（开启逐字稿会回落轻量档）")

# ---------- 2/3 数据库 ----------
if not os.path.isfile(DB):
    print("\n✗ 缺 data/studio.db，无法继续；确认项目目录是否正确")
    sys.exit(2)
conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
conn.row_factory = sqlite3.Row

head("2 / 最近 3 个采集任务（params + 日志里的逐字稿痕迹）")
rows = [dict(r) for r in conn.execute(
    "SELECT * FROM harvest_jobs ORDER BY id DESC LIMIT 3")]
if not rows:
    print("没有任何采集任务")
latest_params_asr = None
for r in rows:
    p = jload(r.get("params"), {})
    if latest_params_asr is None:
        latest_params_asr = bool(p.get("use_asr", False))
    print("\n任务 #%s [%s] %s" % (r.get("id"), r.get("status"), r.get("created_at") or ""))
    print("  params.use_asr      = %s" % p.get("use_asr", "<未写进 params>"))
    print("  params.whisper_model= %s" % p.get("whisper_model", "<未写进 params>"))
    print("  provider=%s  found=%s  imported=%s" % (
        r.get("provider"), r.get("found"), r.get("imported")))
    hits = [l for l in (r.get("log") or "").splitlines()
            if any(k in l for k in KEY_LINES)]
    if hits:
        for l in hits[-12:]:
            print("  | " + l)
    else:
        print("  | （日志里没有任何逐字稿 / 完整档相关行）")

head("3 / 最近 3 份报告（facts.transcript 是否落库）")
reps = [dict(r) for r in conn.execute(
    "SELECT * FROM reports ORDER BY id DESC LIMIT 3")]
if not reps:
    print("报告库为空")
for r in reps:
    R = jload(r.get("report_json"), {})
    f = R.get("facts") or {}
    tr = f.get("transcript") or {}
    ai = R.get("ai_analysis") or {}
    print("\n报告 #%s [%s] %s" % (r.get("id"), r.get("engine"), r.get("created_at") or ""))
    print("  标题            = %s" % str(r.get("title") or "")[:44])
    print("  facts.no_transcript = %s" % f.get("no_transcript", "<无该键＝旧规则报告>"))
    print("  facts.transcript    = %s" % (
        "有，has_transcript=%s，%s 字" % (
            tr.get("has_transcript"), tr.get("chars"))
        if tr else "无 ← 第四章显示「没有逐字稿」的原因"))
    print("  ai_analysis         = %s / chapters=%s" % (
        "有" if ai else "无（旧报告，回退老版详情页）", ai.get("chapters")))
conn.close()

# ---------- 4 结论 ----------
head("4 / 判定")
logs = "\n".join(r.get("log") or "" for r in rows)
tr_lines = [l for l in logs.splitlines() if "逐字稿" in l and "字 /" in l]
latest_has_tr = bool(
    (jload(reps[0].get("report_json"), {}).get("facts") or {}).get("transcript")
) if reps else False

if not use_asr:
    print("→ 原因 A：设置页「逐字稿（完整档）」没开启，或选了但没点保存。")
    print("  修法：设置页选「开启 · 下载视频并本地转写」→ 保存 → 用【没采过的关键词】重新采 1 条。")
elif rows and latest_params_asr is False:
    print("→ 原因 B：开关现在是开的，但最近这个任务是【开启之前】启动的（params.use_asr=False）。")
    print("  修法：重新采集；历史报告不追溯补稿，同一 aweme_id 也会去重。")
elif "无 play_url" in logs:
    print("→ 原因 C：TikHub 搜索结果没给 play_url，无法下载视频。")
    print("  修法：把本输出发我，我给 provider 加详情接口兜底取播放地址。")
elif "逐字稿生成失败" in logs:
    print("→ 原因 D：视频下载或本地转写失败，看上面日志冒号后面的具体报错。")
    print("  常见：play_url 防盗链/过期、faster-whisper 模型没下下来、ffmpeg 缺失。")
elif "逐字稿为空" in logs:
    print("→ 原因 D2：转写完成但结果为空（可能是纯音乐/无人声），已按设计回落轻量档。")
elif tr_lines and not latest_has_tr:
    print("→ 原因 E：日志说转写成功，但最新报告里没落 transcript，疑似代码问题，把本输出发我。")
elif tr_lines and latest_has_tr:
    print("→ 逐字稿其实已生成并落库。你现在打开的应是【更早的报告】，刷新报告列表点最新一条。")
else:
    print("→ 开关已开，但最近任务里看不到「逐字稿 NN 字」；多半是这些任务在开开关之前跑的。")
    print("  修法：用没采过的关键词重新采 1 条，再看第 2 段日志。")

print("\n提示：check_deepseek.py 输出里的 no_transcript 是【自检脚本故意不传逐字稿】，")
print("      只验证模型链路，不代表生产采集没有逐字稿，两者不要混为一谈。")
