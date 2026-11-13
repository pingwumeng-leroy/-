# -*- coding: utf-8 -*-
"""合胜宏 · 抖音内容工作台

老板使用路径：取素材 → 看分析报告 → 生成文案（可编辑）→ 上传口播 → 一键出片
运维：改 config/settings.json 或在页面「设置」里填自有 AI / 转写接口。
"""
import base64
import os
import secrets
import threading
import traceback

from flask import Flask, Response, jsonify, request, send_file, send_from_directory, render_template

from core import analyze, config, db, editor, generate, harvest, ingest, llm, providers

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1GB

ALLOWED_VIDEO = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv"}
ALLOWED_TEXT = {".txt", ".srt", ".vtt", ".ass", ".md"}


@app.before_request
def guard():
    """可选访问密码：默认关闭（内网用）；开启后页面与所有 API 走 HTTP Basic。"""
    if request.path == "/healthz":
        return None
    a = config.get_settings().get("auth", {})
    if not a.get("enabled") or not a.get("password"):
        return None
    expected = "Basic " + base64.b64encode(
        f"{a.get('username') or 'boss'}:{a['password']}".encode("utf-8")
    ).decode("ascii")
    if secrets.compare_digest(request.headers.get("Authorization", ""), expected):
        return None
    # realm 必须是纯 ASCII：HTTP header 按 latin-1 编码，中文会让 Werkzeug 抛
    # UnicodeEncodeError，401 响应生成失败，反代侧表现为 502。
    return Response("需要访问密码", 401, {
        "WWW-Authenticate": 'Basic realm="douyin-studio"',
        "Content-Type": "text/plain; charset=utf-8",
    })


@app.route("/healthz")
def healthz():
    return ok({"status": "up", "stats": db.stats()})


def ok(data=None, **kw):
    body = {"ok": True}
    if data is not None:
        body["data"] = data
    body.update(kw)
    return jsonify(body)


def err(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


def ext_of(name):
    return os.path.splitext(name or "")[1].lower()


@app.errorhandler(Exception)
def handle(exc):
    traceback.print_exc()
    return err(exc)


# ---------------- 页面 ----------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------- 总览 / 设置 ----------------
@app.route("/api/state")
def api_state():
    s = config.get_settings()
    return ok({
        "stats": db.stats(),
        "llm_enabled": llm.available(),
        "asr_enabled": bool(s["asr"].get("enabled") and s["asr"].get("api_key")),
        "templates": config.get_templates(),
        "company": s.get("company", {}),
        "sensitive_count": len(config.get_sensitive_words()),
        "provider": s.get("search", {}).get("provider", "mock"),
    })


def _mask_settings(s):
    """回传设置时隐藏密钥。

    GET 和 POST 都必须走这里：POST 若原样返回 save_settings() 的结果，
    明文密码 / API Key 会出现在响应体、浏览器开发者工具与可能被采集的日志里。
    """
    out = {k: dict(v) for k, v in s.items() if isinstance(v, dict)}
    for k in ("llm", "asr"):
        key = out.get(k, {}).get("api_key", "")
        if key:
            out[k]["api_key"] = key[:4] + "****" + key[-4:]
    if out.get("auth", {}).get("password"):
        out["auth"]["password"] = "********"
    tk = out.get("search", {}).get("tikhub", {})
    if isinstance(tk, dict) and tk.get("api_key"):
        masked = dict(tk)
        masked["api_key"] = masked["api_key"][:4] + "****" + masked["api_key"][-4:]
        out["search"]["tikhub"] = masked
    return out


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return ok(_mask_settings(config.get_settings()))
    data = request.get_json(force=True, silent=True) or {}
    # 脱敏值不覆盖已保存的密钥
    for k in ("llm", "asr"):
        if k in data and isinstance(data[k], dict):
            v = data[k].get("api_key", "")
            if "****" in str(v):
                data[k].pop("api_key", None)
    if isinstance(data.get("auth"), dict) and "****" in str(data["auth"].get("password", "")):
        data["auth"].pop("password", None)
    tk = (data.get("search") or {}).get("tikhub") if isinstance(data.get("search"), dict) else None
    if isinstance(tk, dict) and "****" in str(tk.get("api_key", "")):
        tk.pop("api_key", None)
    return ok(_mask_settings(config.save_settings(data)))


@app.route("/api/templates", methods=["GET", "POST"])
def api_templates():
    if request.method == "GET":
        return ok(config.get_templates())
    items = request.get_json(force=True, silent=True) or []
    return ok(config.save_templates(items))


@app.route("/api/admin/reset", methods=["POST"])
def api_reset():
    """清空全部业务数据与上传/成片文件，保留配置与剪辑模板。用于交付前清演示数据。"""
    d = request.get_json(force=True, silent=True) or {}
    if d.get("confirm") != "RESET":
        return err("缺少确认参数")
    running = db.one("SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running')")
    if running and running["n"]:
        return err("还有剪辑任务没结束，请等它跑完再清空")
    # seed_accounts 属于运维录入的配置资产，保留；其余业务数据清空
    for t in ("jobs", "scripts", "reports", "videos", "harvest_jobs", "search_cache"):
        db.execute(f"DELETE FROM {t}")
    try:
        db.execute("DELETE FROM sqlite_sequence")
    except Exception:
        pass
    removed = 0
    for folder in (config.UPLOADS, config.OUTPUTS, config.ASSETS):
        if not os.path.isdir(folder):
            continue
        # assets 下按任务分了子目录，必须递归清，否则字幕/卡片等中间产物会残留
        for root, _dirs, files in os.walk(folder):
            for fn in files:
                try:
                    os.remove(os.path.join(root, fn))
                    removed += 1
                except OSError:
                    pass
        for root, _dirs, _files in os.walk(folder, topdown=False):
            if root != folder:
                try:
                    os.rmdir(root)
                except OSError:
                    pass
    return ok({"deleted_files": removed, "stats": db.stats()})


@app.route("/api/sensitive", methods=["POST"])
def api_sensitive():
    text = (request.get_json(force=True, silent=True) or {}).get("text", "")
    return ok({"hits": generate.check_sensitive(text)})


# ---------------- 1. 素材 ----------------
@app.route("/api/videos")
def api_videos():
    return ok(db.list_videos())


@app.route("/api/videos/upload", methods=["POST"])
def api_video_upload():
    f = request.files.get("file")
    if not f or ext_of(f.filename) not in ALLOWED_VIDEO:
        return err("请上传 mp4/mov/mkv/avi/webm 视频文件")
    path = ingest.save_upload(f, "src")
    dur, w, h = ingest.probe(path)
    cover = ingest.extract_cover(path, os.path.splitext(path)[0] + ".jpg")
    title = os.path.splitext(os.path.basename(f.filename))[0]
    vid = db.add_video(title=title, source_type="upload", file_path=path,
                       cover_path=cover, duration=dur, status="new")
    return ok(db.get_video(vid))


@app.route("/api/videos/fetch", methods=["POST"])
def api_video_fetch():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return err("请填入抖音分享链接")
    cookie = (data.get("cookie") or config.get_settings()["fetch"].get("douyin_cookie") or "")
    try:
        path, meta = ingest.fetch_douyin(url, cookie)
        dur, w, h = ingest.probe(path)
        cover = ingest.extract_cover(path, os.path.splitext(path)[0] + ".jpg")
        vid = db.add_video(title=meta["title"][:100], author=meta["author"],
                           source_url=url, source_type="douyin", file_path=path,
                           cover_path=cover, duration=dur, likes=meta["likes"], status="new")
        return ok(db.get_video(vid))
    except Exception as exc:
        return err(exc)


@app.route("/api/videos/<int:vid>", methods=["DELETE"])
def api_video_delete(vid):
    v = db.get_video(vid)
    if v and v.get("file_path") and os.path.exists(v["file_path"]):
        try:
            os.remove(v["file_path"])
        except OSError:
            pass
    db.delete_video(vid)
    return ok()


# ---------------- 2. 内容提取 ----------------
@app.route("/api/videos/<int:vid>/transcript", methods=["POST"])
def api_transcript(vid):
    """支持 multipart（字幕文件）或 json（manual_text / mode）。"""
    srt_path = ""
    f = request.files.get("file")
    if f and ext_of(f.filename) in ALLOWED_TEXT:
        srt_path = ingest.save_upload(f, "srt")
    if request.is_json:
        data = request.get_json(force=True, silent=True) or {}
    else:
        data = request.form.to_dict()
    mode = data.get("mode", "auto")
    manual = data.get("manual_text", "")
    try:
        txt, source = ingest.extract_transcript(vid, mode=mode,
                                                manual_text=manual, srt_path=srt_path)
        db.update_video(vid, transcript=txt, transcript_source=source,
                        status="transcribed", error="")
        return ok({"transcript": txt, "source": source, "length": len(txt)})
    except Exception as exc:
        return err(exc)


@app.route("/api/videos/<int:vid>/analyze", methods=["POST"])
def api_analyze(vid):
    try:
        rid = analyze.analyze_video(vid)
        return ok(db.get_report(rid))
    except Exception as exc:
        return err(exc)


# ---------------- 报告库 ----------------
@app.route("/api/reports")
def api_reports():
    return ok(db.list_reports())


@app.route("/api/reports/<int:rid>", methods=["GET", "DELETE"])
def api_report(rid):
    if request.method == "DELETE":
        db.delete_report(rid)
        return ok()
    r = db.get_report(rid)
    if not r:
        return err("报告不存在", 404)
    return ok(r)


@app.route("/api/reports/manual", methods=["POST"])
def api_report_manual():
    """手动往经验库补一条报告（不依赖视频素材）。"""
    d = request.get_json(force=True, silent=True) or {}
    r = {k: d.get(k, "") for k in
         ("title", "topic", "city", "hook_type", "hook_text", "structure", "cta",
          "pain_points", "top_comments", "policy_refs", "reusable_pattern",
          "risk_notes", "summary")}
    r["engine"] = "manual"
    rid = db.add_report(r, video_id=None, engine="manual")
    return ok(db.get_report(rid))


# ---------------- 批量采集：一段文字 → N 份报告 ----------------
@app.route("/api/providers")
def api_providers():
    return ok(providers.list_providers())


@app.route("/api/providers/test", methods=["POST"])
def api_provider_test():
    """数据源自检：只发 1 次搜索请求，用来区分 Key / 余额 / 端点问题。

    读的是已落盘的设置，所以前端要先「保存设置」再来测。
    异常信息已在 providers 层脱敏，不含 Token。
    """
    d = request.get_json(force=True, silent=True) or {}
    name = (d.get("provider") or "").strip() or None
    kw = (d.get("keyword") or "成都注册公司").strip()
    p = providers.get_provider(name)
    if p.name == "mock":
        return ok({"ok": True, "provider": "mock", "items": 0, "cost_calls": 0,
                   "message": "演示数据源，不联网、不产生费用"})
    try:
        res = p.search(kw, cursor=0, sort_by="likes", publish_days=180, page_size=5)
    except providers.ProviderError as exc:
        # str(exc) 只含 HTTP 状态码和有限长度的中英文消息，不含 Authorization
        return ok({"ok": False, "provider": p.name, "kind": type(exc).__name__,
                   "error": str(exc)[:200], "cost_calls": 0})
    sample = [{"title": x["title"][:40], "likes": x["digg_count"]}
              for x in res.get("items", [])[:3]]
    return ok({"ok": True, "provider": p.name, "keyword": kw,
               "items": len(res.get("items", [])),
               "cost_calls": res.get("cost_calls", 1),
               "has_more": bool(res.get("has_more")),
               "sample": sample,
               "message": "连接正常" if res.get("items") else "连接正常，但该关键词没有返回视频"})


@app.route("/api/llm/test", methods=["POST"])
def api_llm_test():
    """大模型连通性自检：确认 base_url / api_key / model 真的被上游接受。

    用户指定的模型名未必是官方公开名，上线前用一次极小调用验证，
    避免整批采集跑完才发现 404 / model not found。读的是已落盘设置。
    """
    s = config.get_settings().get("llm", {})
    if not llm.available():
        return ok({"ok": False, "error": "未启用，或 API Key / Base URL 未填写"})
    try:
        txt = llm.chat([
            {"role": "system", "content": "只回复 OK 两个字符。"},
            {"role": "user", "content": "连通性测试"},
        ], temperature=0.1, max_tokens=16)
        return ok({"ok": True, "model": s.get("model", ""),
                   "reply": str(txt or "").strip()[:40],
                   "message": "模型可用"})
    except Exception as exc:
        return ok({"ok": False, "model": s.get("model", ""),
                   "error": str(exc)[:200],
                   "message": "调用失败：核对 Base URL、API Key 与模型名"})


@app.route("/api/harvest/start", methods=["POST"])
def api_harvest_start():
    d = request.get_json(force=True, silent=True) or {}
    text = (d.get("text") or "").strip()
    if not text:
        return err("请输入选题或关键词")
    jid = harvest.start_harvest(text, d)
    return ok(db.get_harvest_job(jid))


@app.route("/api/harvest")
def api_harvest_list():
    return ok(db.list_harvest_jobs())


@app.route("/api/harvest/<int:jid>")
def api_harvest_job(jid):
    j = db.get_harvest_job(jid)
    if not j:
        return err("任务不存在", 404)
    return ok(j)


@app.route("/api/harvest/<int:jid>/stop", methods=["POST"])
def api_harvest_stop(jid):
    if not db.get_harvest_job(jid):
        return err("任务不存在", 404)
    db.update_harvest_job(jid, status="stopped", finished_at=db.now())
    return ok(db.get_harvest_job(jid))


@app.route("/api/seeds", methods=["GET", "POST"])
def api_seeds():
    if request.method == "GET":
        return ok(db.list_seeds())
    d = request.get_json(force=True, silent=True) or {}
    batch = (d.get("batch") or "").strip()
    added = 0
    if batch:
        # 批量导入：每行「昵称|主页链接|sec_user_id|分类」
        for line in batch.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if not parts or not parts[0]:
                continue
            db.add_seed(nickname=parts[0],
                        homepage=parts[1] if len(parts) > 1 else "",
                        sec_user_id=parts[2] if len(parts) > 2 else "",
                        category=parts[3] if len(parts) > 3 else "")
            added += 1
    else:
        db.add_seed(**d)
        added = 1
    return ok({"added": added, "items": db.list_seeds()})


@app.route("/api/seeds/<int:sid>", methods=["DELETE"])
def api_seed_delete(sid):
    db.delete_seed(sid)
    return ok()


# ---------------- 3. 文案 ----------------
@app.route("/api/scripts/generate", methods=["POST"])
def api_script_generate():
    req = request.get_json(force=True, silent=True) or {}
    try:
        sid, res = generate.generate_script(req)
        return ok({"id": sid, **res})
    except Exception as exc:
        return err(exc)


@app.route("/api/scripts")
def api_scripts():
    return ok(db.list_scripts())


@app.route("/api/scripts/<int:sid>", methods=["GET", "PUT", "DELETE"])
def api_script(sid):
    if request.method == "DELETE":
        db.delete_script(sid)
        return ok()
    if request.method == "PUT":
        d = request.get_json(force=True, silent=True) or {}
        hits = generate.check_sensitive((d.get("title") or "") + (d.get("body") or ""))
        db.update_script(sid, title=d.get("title", ""), body=d.get("body", ""),
                         status=d.get("status", "draft"), sensitive_hits=hits)
        return ok(db.get_script(sid))
    s = db.get_script(sid)
    if not s:
        return err("文案不存在", 404)
    return ok(s)


# ---------------- 4. 口播上传 ----------------
@app.route("/api/clips/upload", methods=["POST"])
def api_clip_upload():
    f = request.files.get("file")
    sid = request.form.get("script_id", "")
    if not f or ext_of(f.filename) not in ALLOWED_VIDEO:
        return err("请上传口播视频文件")
    if not sid or not db.get_script(int(sid) if sid.isdigit() else 0):
        return err("请先选择一份定稿文案")
    path = ingest.save_upload(f, "talk")
    dur, w, h = ingest.probe(path)
    if dur <= 0:
        return err("无法读取视频时长，请检查文件")
    return ok({"path": path, "duration": dur, "width": w, "height": h,
               "filename": os.path.basename(f.filename)})


# ---------------- 5. 剪辑 ----------------
@app.route("/api/clips/render", methods=["POST"])
def api_clip_render():
    d = request.get_json(force=True, silent=True) or {}
    sid, path = d.get("script_id"), d.get("video_path")
    tid = d.get("template_id", "standard")
    if not db.get_script(int(sid) if str(sid).isdigit() else 0):
        return err("文案不存在")
    if not path or not os.path.exists(path):
        return err("口播视频不存在，请重新上传")
    jid = db.add_job(int(sid), path, tid)
    editor.start_render(jid)
    return ok(db.get_job(jid))


@app.route("/api/jobs")
def api_jobs():
    return ok(db.list_jobs())


@app.route("/api/jobs/<int:jid>")
def api_job(jid):
    j = db.get_job(jid)
    if not j:
        return err("任务不存在", 404)
    if j.get("output_path"):
        j["download"] = f"/api/outputs/{os.path.basename(j['output_path'])}"
    return ok(j)


@app.route("/api/outputs/<path:name>")
def api_output(name):
    p = os.path.join(config.OUTPUTS, os.path.basename(name))
    if not os.path.exists(p):
        return err("文件不存在", 404)
    return send_file(p, mimetype="video/mp4", as_attachment=False,
                     conditional=True)


@app.route("/api/outputs/<path:name>/download")
def api_output_download(name):
    p = os.path.join(config.OUTPUTS, os.path.basename(name))
    if not os.path.exists(p):
        return err("文件不存在", 404)
    return send_file(p, mimetype="video/mp4", as_attachment=True,
                     download_name=name)


@app.route("/media/<path:name>")
def api_media(name):
    """素材封面/预览。"""
    for base in (config.UPLOADS, config.OUTPUTS):
        p = os.path.join(base, os.path.basename(name))
        if os.path.exists(p):
            return send_file(p, conditional=True)
    return err("文件不存在", 404)


def bootstrap():
    db.init()
    config._ensure()
    a = config.get_settings().get("auth", {})
    print("[BOOT] 数据库就绪：", config.DB_PATH)
    print("[BOOT] 目录可写： data=%s config=%s" % (
        os.access(config.DATA, os.W_OK), os.access(config.CONFIG_DIR, os.W_OK)))
    print("[BOOT] LLM 已启用：", llm.available())
    print("[BOOT] 剪辑模板：", [t["name"] for t in config.get_templates()])
    print("[BOOT] 访问密码：", "已开启" if (a.get("enabled") and a.get("password"))
          else "未开启（内网模式）")


# 模块级初始化：python3 app.py / gunicorn / flask run / test_client 都能直接跑
bootstrap()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
