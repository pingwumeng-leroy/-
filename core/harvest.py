# -*- coding: utf-8 -*-
"""批量采集编排：老板输入一段文字 → N 份结构化分析报告入库。

链路：关键词展开 → 数据源搜索 → 归一化/去重/点赞筛选 → 逐条分析 → reports 表。
设计原则：
1. 逐条入库，任何一条失败都不丢已完成的报告；
2. 数据源异常降级到演示数据，任务判 done(部分) 而不是 failed；
3. 报告只落 reports 表，下游 generate.py 的召回逻辑零改动。
"""
import json
import os
import re
import shutil
import tempfile
import threading
import traceback

from . import analyze, config, db, ingest, llm, providers, transcript

BUSINESS_TERMS = [name for name, _ in analyze.TOPIC_RULES]
CITY_HINTS = ["成都", "四川", "高新区", "天府新区"]


def expand_keywords(text, max_words=5):
    """把老板的一段话展开成若干个可直接喂给搜索接口的关键词。

    TikHub 的 keyword 只收一个搜索词，不收长句子，所以这一层不能省。
    """
    text = (text or "").strip()
    if not text:
        return ["成都注册公司流程"]

    # 配了大模型就让 AI 展开；失败一律回退到规则，不让任务卡住
    if llm.available():
        try:
            res = llm.chat_json([
                {"role": "system", "content": "你是抖音搜索词规划师。把用户的一段话拆成 3~5 个"
                 "中文搜索关键词，要求是真实用户会在抖音搜的短词，不要长句，不要解释。"
                 '严格输出 JSON：{"words":["词1","词2","词3"]}'},
                {"role": "user", "content": text},
            ], temperature=0.3)
            words = [str(w).strip() for w in (res.get("words") or []) if str(w).strip()]
            if words:
                return words[:max_words]
        except Exception:
            pass

    city = next((c for c in CITY_HINTS if c in text), "")
    hits = [name for name, kws in analyze.TOPIC_RULES
            if any(k in text for k in kws)]
    base = hits[0] if hits else re.sub(r"[，。！？,.!?\s]+", "", text)[:12]
    base = base or "注册公司"

    words = []
    cleaned = re.sub(r"[，。！？,.!?]+", " ", text).strip()
    if 2 <= len(cleaned) <= 18:
        words.append(cleaned)
    if city and city not in base:
        words.append(city + base)
    words.append(base)
    words += [base + "流程", base + "需要什么材料", base + "要多久"]
    return list(dict.fromkeys(words))[:max_words]


def _percentile_threshold(items, count):
    """按候选池分位数自动算点赞阈值，避免写死数值误杀小号爆款。"""
    pool = items[:max(count * 2, 10)]
    vals = sorted(x["digg_count"] for x in pool)
    if not vals:
        return 0
    return vals[int(len(vals) * 0.25)]


def _build_transcript(item, comments, tr=None):
    """有逐字稿用逐字稿，否则用视频文案 + 前 5 条评论构造可分析文本。

    逐字稿由 settings.harvest.use_asr 开启：play_url 下载 → ffmpeg 抽音频 →
    本地 faster-whisper 转写（core.transcript），失败自动回落轻量档。
    """
    if tr and tr.get("full"):
        parts = [str(tr["full"])[:3000]]
    else:
        parts = [item.get("desc") or item.get("title") or ""]
    if comments:
        parts.append("评论区常问：" + "；".join((c.get("text") or "") for c in comments[:5]))
    return "\n".join(p for p in parts if p).strip()


def _log(jid, pct, lines, msg):
    lines.append(msg)
    db.update_harvest_job(jid, progress=int(pct), log="\n".join(lines[-40:]))


def run_harvest(jid):
    job = db.get_harvest_job(jid)
    if not job:
        return
    params = json.loads(job.get("params") or "{}")
    count = int(params.get("count") or 15)
    # 0 是合法值（时间范围＝不限），不能用 or 兜底把它改成 180
    _pd = params.get("publish_days")
    publish_days = int(_pd) if _pd is not None else 180
    max_calls = int(params.get("max_calls") or 60)
    pages = int(params.get("pages_per_keyword") or 2)
    hv = config.get_settings().get("harvest", {})
    comment_pages = int(params.get("comment_pages") or hv.get("comment_pages", 3))
    use_fallback = bool(params.get("use_seed_fallback", True))
    use_asr = bool(params.get("use_asr", hv.get("use_asr", False)))
    whisper_model = str(params.get("whisper_model") or hv.get("whisper_model") or "small")

    provider = providers.get_provider(job.get("provider"))
    fallback = providers.MockProvider()
    lines = ["数据源：" + provider.label]
    if use_asr:
        lines.append("完整档：启用逐字稿（本地 faster-whisper / %s）" % whisper_model)
    db.update_harvest_job(jid, status="running", progress=2, log="\n".join(lines))

    try:
        kws = expand_keywords(job.get("input_text") or "")
        db.update_harvest_job(jid, keywords=json.dumps(kws, ensure_ascii=False))
        _log(jid, 6, lines, "关键词展开：" + " / ".join(kws))

        collected, cost_calls, degraded = {}, 0, False
        for kw in kws:
            # 每个关键词独立维护翻页令牌。TikHub 的 cursor / search_id / backtrace
            # 必须用上一次响应带回的值；硬编码 page*8 只会重复拿到第一页。
            cursor, search_id, backtrace = 0, "", ""
            for page in range(pages):
                if cost_calls >= max_calls:
                    _log(jid, 40, lines, "已达到单次调用预算上限，停止搜索")
                    break
                if degraded and use_fallback:
                    # 已确认真实源不可用，后续不再逐条白试，避免无效调用刷屏
                    res = fallback.search(kw, cursor=cursor, sort_by="likes",
                                          publish_days=publish_days)
                else:
                    try:
                        res = provider.search(kw, cursor=cursor, sort_by="likes",
                                              publish_days=publish_days,
                                              search_id=search_id, backtrace=backtrace)
                        cost_calls += res.get("cost_calls", 1)
                    except providers.ProviderError as exc:
                        degraded = True
                        _log(jid, 30, lines, "「%s」%s：%s → 降级到演示数据" %
                             (kw, type(exc).__name__, str(exc)[:120]))
                        if not use_fallback:
                            raise
                        res = fallback.search(kw, cursor=cursor, sort_by="likes",
                                              publish_days=publish_days)
                for it in res.get("items", []):
                    if it.get("aweme_id"):
                        collected[it["aweme_id"]] = it
                _log(jid, 6 + min(30, cost_calls * 3), lines,
                     "「%s」第 %d 页 → %d 条，累计候选 %d" %
                     (kw, page + 1, len(res.get("items", [])), len(collected)))
                cursor = res.get("cursor") or cursor + len(res.get("items", []))
                search_id = res.get("search_id") or ""
                backtrace = res.get("backtrace") or ""
                if not res.get("has_more"):
                    break
            if cost_calls >= max_calls:
                break

        db.update_harvest_job(jid, found=len(collected), cost_calls=cost_calls)
        if not collected:
            _log(jid, 100, lines, "没有拿到候选视频，任务结束")
            db.update_harvest_job(jid, status="done", progress=100,
                                 log="\n".join(lines[-40:]), finished_at=db.now())
            return

        # 相关性优先：同选题的排前面，跨选题的高赞只作补充。
        # 真实搜索本身保证相关，这一层是防止供应商返回噪声 / 降级数据混入。
        by_likes = sorted(collected.values(), key=lambda x: x["digg_count"], reverse=True)
        target_topic = analyze.guess_topic((job.get("input_text") or "") + " " + " ".join(kws))

        def _topic_of(x):
            return analyze.guess_topic((x.get("title") or "") + " " + (x.get("desc") or ""))

        related = [x for x in by_likes if _topic_of(x) == target_topic]
        others = [x for x in by_likes if _topic_of(x) != target_topic]
        items = related + others
        thr = _percentile_threshold(by_likes, count) if params.get("likes_mode") == "percentile" else 0
        # 同选题的视频不因点赞稍低被阈值砍掉，阈值只用来挡跨选题的补充噪声
        items = [x for x in items if x["digg_count"] >= thr or _topic_of(x) == target_topic]
        related_kept = sum(1 for x in items if _topic_of(x) == target_topic)
        existing = {r["aweme_id"] for r in
                    db.q("SELECT aweme_id FROM reports WHERE aweme_id IS NOT NULL AND aweme_id != ''")}
        fresh = [x for x in items if x["aweme_id"] not in existing]
        skipped = len(items) - len(fresh)
        target = fresh[:count]
        _log(jid, 48, lines, "候选 %d 条（同选题「%s」%d 条），点赞阈值 %d，去重跳过 %d 条，本次分析 %d 条" %
             (len(items), target_topic, related_kept, thr, skipped, len(target)))

        imported, errors = 0, 0
        for i, item in enumerate(target):
            if db.get_harvest_job(jid).get("status") == "stopped":
                _log(jid, 100, lines, "任务被手动停止，已入库报告保留")
                break
            try:
                # 评论翻页同样计费：每页一次调用，必须受 max_calls 预算约束
                budget_left = max(0, max_calls - cost_calls)
                cpages = min(comment_pages, budget_left)
                if cpages <= 0:
                    comments = []
                    _log(jid, 50, lines, "调用预算已用完，跳过评论采集")
                else:
                    try:
                        cres = provider.comments(item["aweme_id"], max_pages=cpages)
                        comments = cres.get("items", [])
                        cost_calls += cres.get("cost_calls", 0)
                    except providers.ProviderError:
                        comments = fallback.comments(item["aweme_id"]).get("items", [])
                # 完整档：只对本次要入库的视频下载 + 本地转写。
                # 视频/音频/SRT 全放临时目录，用完即删，只把文字带进报告。
                tr = None
                if use_asr:
                    play_url = item.get("play_url") or ""
                    if not play_url:
                        _log(jid, 50, lines, "无 play_url，跳过逐字稿（回落轻量档）")
                    else:
                        tmpdir = tempfile.mkdtemp(prefix="ds_asr_", dir=config.DATA)
                        try:
                            vp = os.path.join(tmpdir, "video.mp4")
                            ingest.download_play_url(play_url, vp)
                            srt = transcript.transcribe_video(vp, tmpdir, whisper_model)
                            tr = transcript.load_transcript(srt)
                            if tr and tr.get("full"):
                                _log(jid, 50, lines, "逐字稿 %d 字 / %d 段（本地转写）" %
                                     (len(tr["full"]), len(tr["segments"])))
                            else:
                                tr = None
                                _log(jid, 50, lines, "逐字稿为空，回落轻量档")
                        except Exception as exc:
                            tr = None
                            _log(jid, 50, lines,
                                 "逐字稿生成失败，回落轻量档：%s" % str(exc)[:100])
                        finally:
                            shutil.rmtree(tmpdir, ignore_errors=True)

                # 用 .get() 取字段：第三方 provider 或手工导入的 item 可能缺列，
                # 不应因为一个可选字段让整条视频的分析全部失败。
                title = item.get("title") or item.get("desc") or "未命名视频"
                likes = item.get("digg_count") or 0
                transcript_text = _build_transcript(item, comments, tr)
                meta = {"likes": likes, "author": item.get("author") or "",
                        "publish_time": item.get("publish_time") or 0,
                        "duration": item.get("duration") or 0}
                engine = "rule"
                if llm.available():
                    try:
                        report = analyze.deep_analyze(item, comments, tr)
                        engine = "llm"
                    except Exception as exc:
                        _log(jid, 50, lines, "AI 十维分析失败，回落规则引擎：%s" % str(exc)[:100])
                        report = analyze.rule_analyze(title, transcript_text, meta)
                else:
                    report = analyze.rule_analyze(title, transcript_text, meta)
                if comments and engine != "llm":
                    report["top_comments"] = [c["text"] for c in comments[:5]]
                src = provider.name + ("(fallback)" if degraded else "")
                db.add_harvest_report(report, item, jid, src, engine=engine)
                imported += 1
            except Exception as exc:
                errors += 1
                _log(jid, 50 + int(45 * (i + 1) / max(len(target), 1)), lines,
                     "第 %d 条分析失败（已跳过）：%s" % (i + 1, str(exc)[:100]))
                continue
            _log(jid, 50 + int(45 * (i + 1) / max(len(target), 1)), lines,
                 "已入库 %d/%d：%s（%d 赞，评论 %d 条，逐字稿 %s，%s）" %
                 (imported, len(target), title[:26], likes, len(comments),
                  ("%d 字" % len(tr["full"])) if tr else "无",
                  "AI 十维" if engine == "llm" else "规则引擎"))

        db.update_harvest_job(
            jid, status="done", progress=100, imported=imported,
            cost_calls=cost_calls, error=("降级使用演示数据" if degraded else ""),
            log="\n".join(lines[-40:]), finished_at=db.now())
        _log(jid, 100, lines, "完成：入库 %d 份报告，失败 %d 条，调用 %d 次" %
             (imported, errors, cost_calls))
    except Exception as exc:
        traceback.print_exc()
        lines.append("任务异常：%s" % str(exc)[:200])
        db.update_harvest_job(jid, status="failed", error=str(exc)[:300],
                              log="\n".join(lines[-40:]), finished_at=db.now())


def start_harvest(text, params=None):
    """建任务并起后台线程，返回 job id。"""
    s = config.get_settings().get("harvest", {})
    _pd = (params or {}).get("publish_days")
    p = {
        "count": int((params or {}).get("count") or s.get("default_count", 15)),
        # 0 = 时间范围「不限」，保留原值，别被 or 兜底成 180
        "publish_days": int(_pd) if _pd is not None else 180,
        "max_calls": int((params or {}).get("max_calls") or s.get("max_calls", 60)),
        "comment_pages": int((params or {}).get("comment_pages") or s.get("comment_pages", 3)),
        "use_asr": bool((params or {}).get("use_asr", s.get("use_asr", False))),
        "whisper_model": (params or {}).get("whisper_model") or s.get("whisper_model", "small"),
        "pages_per_keyword": int((params or {}).get("pages_per_keyword") or 2),
        "likes_mode": (params or {}).get("likes_mode") or "percentile",
        "use_seed_fallback": bool((params or {}).get("use_seed_fallback", True)),
    }
    provider = (params or {}).get("provider") or config.get_settings().get("search", {}).get("provider") or "mock"
    jid = db.add_harvest_job((text or "").strip(), p, provider)
    threading.Thread(target=run_harvest, args=(jid,), daemon=True).start()
    return jid
