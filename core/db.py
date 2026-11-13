# -*- coding: utf-8 -*-
"""SQLite 数据层：素材、分析报告、文案、剪辑任务。单文件，零运维。"""
import json
import os
import sqlite3
import threading
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL DEFAULT '',
    author TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    source_type TEXT DEFAULT 'upload',
    file_path TEXT DEFAULT '',
    cover_path TEXT DEFAULT '',
    duration REAL DEFAULT 0,
    likes INTEGER DEFAULT 0,
    transcript TEXT DEFAULT '',
    transcript_source TEXT DEFAULT '',
    status TEXT DEFAULT 'new',
    error TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER,
    title TEXT DEFAULT '',
    topic TEXT DEFAULT '',
    city TEXT DEFAULT '',
    hook_type TEXT DEFAULT '',
    hook_text TEXT DEFAULT '',
    structure TEXT DEFAULT '',
    cta TEXT DEFAULT '',
    pain_points TEXT DEFAULT '',
    top_comments TEXT DEFAULT '',
    policy_refs TEXT DEFAULT '',
    reusable_pattern TEXT DEFAULT '',
    risk_notes TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    report_json TEXT NOT NULL,
    engine TEXT DEFAULT 'rule',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_topic ON reports(topic);

CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT DEFAULT '',
    body TEXT DEFAULT '',
    requirement TEXT DEFAULT '{}',
    source_report_ids TEXT DEFAULT '[]',
    engine TEXT DEFAULT 'rule',
    status TEXT DEFAULT 'draft',
    sensitive_hits TEXT DEFAULT '[]',
    note TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    script_id INTEGER,
    video_path TEXT DEFAULT '',
    template_id TEXT DEFAULT 'standard',
    output_path TEXT DEFAULT '',
    status TEXT DEFAULT 'queued',
    progress INTEGER DEFAULT 0,
    log TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS harvest_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    input_text TEXT DEFAULT '',
    keywords TEXT DEFAULT '[]',
    provider TEXT DEFAULT 'mock',
    status TEXT DEFAULT 'queued',
    progress INTEGER DEFAULT 0,
    log TEXT DEFAULT '',
    params TEXT DEFAULT '{}',
    found INTEGER DEFAULT 0,
    imported INTEGER DEFAULT 0,
    cost_calls INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    created_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS seed_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nickname TEXT DEFAULT '',
    sec_user_id TEXT DEFAULT '',
    homepage TEXT DEFAULT '',
    category TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    note TEXT DEFAULT '',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS search_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    provider TEXT NOT NULL,
    payload TEXT DEFAULT '[]',
    fetched_at TEXT,
    UNIQUE(keyword, provider)
);
"""

# 兼容已有库：逐条 ALTER，列已存在就忽略
MIGRATIONS = [
    "ALTER TABLE reports ADD COLUMN aweme_id TEXT DEFAULT ''",
    "ALTER TABLE reports ADD COLUMN likes INTEGER DEFAULT 0",
    "ALTER TABLE reports ADD COLUMN author TEXT DEFAULT ''",
    "ALTER TABLE reports ADD COLUMN source_url TEXT DEFAULT ''",
    "ALTER TABLE reports ADD COLUMN source_provider TEXT DEFAULT ''",
    "ALTER TABLE reports ADD COLUMN harvest_job_id INTEGER DEFAULT 0",
    "ALTER TABLE reports ADD COLUMN comment_count INTEGER DEFAULT 0",
    "ALTER TABLE reports ADD COLUMN publish_time INTEGER DEFAULT 0",
]

_local = threading.local()


def conn():
    """每线程一个连接，WAL 支持后台剪辑线程并发写。"""
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(config.DATA, exist_ok=True)
        c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


def init():
    c = conn()
    c.executescript(SCHEMA)
    for sql in MIGRATIONS:
        try:
            c.execute(sql)
        except sqlite3.OperationalError:
            pass  # 列已存在
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_aweme "
                  "ON reports(aweme_id) WHERE aweme_id != ''")
    except sqlite3.OperationalError:
        pass
    c.commit()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def q(sql, params=()):
    return [dict(r) for r in conn().execute(sql, params).fetchall()]


def one(sql, params=()):
    r = conn().execute(sql, params).fetchone()
    return dict(r) if r else None


def execute(sql, params=()):
    c = conn()
    cur = c.execute(sql, params)
    c.commit()
    return cur.lastrowid


def dumps(v):
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return "" if v is None else str(v)


# ---------- videos ----------
def add_video(**kw):
    return execute(
        "INSERT INTO videos(title,author,source_url,source_type,file_path,cover_path,duration,"
        "likes,transcript,transcript_source,status,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kw.get("title", ""), kw.get("author", ""), kw.get("source_url", ""),
         kw.get("source_type", "upload"), kw.get("file_path", ""), kw.get("cover_path", ""),
         kw.get("duration", 0), kw.get("likes", 0), kw.get("transcript", ""),
         kw.get("transcript_source", ""), kw.get("status", "new"), kw.get("error", ""), now()))


def update_video(vid, **kw):
    allowed = {"title", "author", "file_path", "cover_path", "duration", "likes",
               "transcript", "transcript_source", "status", "error"}
    sets, vals = [], []
    for k, v in kw.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(vid)
    execute(f"UPDATE videos SET {','.join(sets)} WHERE id=?", vals)


def get_video(vid):
    return one("SELECT * FROM videos WHERE id=?", (vid,))


def list_videos(limit=100):
    return q("SELECT * FROM videos ORDER BY id DESC LIMIT ?", (limit,))


def delete_video(vid):
    execute("DELETE FROM videos WHERE id=?", (vid,))
    execute("DELETE FROM reports WHERE video_id=?", (vid,))


# ---------- reports ----------
def add_report(r, video_id=None, engine="rule"):
    cols = ("video_id", "title", "topic", "city", "hook_type", "hook_text", "structure",
            "cta", "pain_points", "top_comments", "policy_refs", "reusable_pattern",
            "risk_notes", "summary", "report_json", "engine", "created_at")
    vals = (video_id, r.get("title", ""), r.get("topic", ""), r.get("city", ""),
            r.get("hook_type", ""), r.get("hook_text", ""), dumps(r.get("structure", "")),
            r.get("cta", ""), dumps(r.get("pain_points", "")), dumps(r.get("top_comments", "")),
            dumps(r.get("policy_refs", "")), r.get("reusable_pattern", ""),
            r.get("risk_notes", ""), r.get("summary", ""),
            json.dumps(r, ensure_ascii=False), engine, now())
    ph = ",".join("?" for _ in cols)
    return execute(f"INSERT INTO reports({','.join(cols)}) VALUES({ph})", vals)


def get_report(rid):
    return one("SELECT * FROM reports WHERE id=?", (rid,))


def list_reports(limit=200):
    return q("SELECT * FROM reports ORDER BY id DESC LIMIT ?", (limit,))


def delete_report(rid):
    execute("DELETE FROM reports WHERE id=?", (rid,))


# ---------- scripts ----------
def add_script(title, body, requirement, report_ids, engine, hits):
    t = now()
    return execute(
        "INSERT INTO scripts(title,body,requirement,source_report_ids,engine,status,"
        "sensitive_hits,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (title, body, json.dumps(requirement, ensure_ascii=False),
         json.dumps(report_ids, ensure_ascii=False), engine, "draft",
         json.dumps(hits, ensure_ascii=False), "", t, t))


def update_script(sid, **kw):
    allowed = {"title", "body", "status", "note", "sensitive_hits"}
    sets, vals = [], []
    for k, v in kw.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(dumps(v) if k == "sensitive_hits" else v)
    sets.append("updated_at=?")
    vals.append(now())
    vals.append(sid)
    execute(f"UPDATE scripts SET {','.join(sets)} WHERE id=?", vals)


def get_script(sid):
    return one("SELECT * FROM scripts WHERE id=?", (sid,))


def list_scripts(limit=100):
    return q("SELECT * FROM scripts ORDER BY id DESC LIMIT ?", (limit,))


def delete_script(sid):
    execute("DELETE FROM scripts WHERE id=?", (sid,))


# ---------- jobs ----------
def add_job(script_id, video_path, template_id):
    return execute(
        "INSERT INTO jobs(script_id,video_path,template_id,status,progress,log,created_at) "
        "VALUES(?,?,?,'queued',0,'',?)",
        (script_id, video_path, template_id, now()))


def update_job(jid, **kw):
    allowed = {"output_path", "status", "progress", "log", "error", "finished_at"}
    sets, vals = [], []
    for k, v in kw.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(jid)
    execute(f"UPDATE jobs SET {','.join(sets)} WHERE id=?", vals)


def get_job(jid):
    return one("SELECT * FROM jobs WHERE id=?", (jid,))


def list_jobs(limit=50):
    return q("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))


# ---------- 批量采集任务 ----------
def add_harvest_job(input_text, params, provider):
    return execute(
        "INSERT INTO harvest_jobs(input_text,keywords,provider,status,progress,log,params,"
        "found,imported,cost_calls,error,created_at) "
        "VALUES(?,?,?,'queued',0,'',?,0,0,0,'',?)",
        (input_text, "[]", provider, json.dumps(params, ensure_ascii=False), now()))


def update_harvest_job(jid, **kw):
    allowed = {"status", "progress", "log", "keywords", "found", "imported",
               "cost_calls", "error", "finished_at", "params"}
    sets, vals = [], []
    for k, v in kw.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(jid)
    execute(f"UPDATE harvest_jobs SET {','.join(sets)} WHERE id=?", vals)


def get_harvest_job(jid):
    return one("SELECT * FROM harvest_jobs WHERE id=?", (jid,))


def list_harvest_jobs(limit=50):
    return q("SELECT * FROM harvest_jobs ORDER BY id DESC LIMIT ?", (limit,))


def delete_harvest_job(jid):
    """删除单条采集任务记录；reports 表里已入库的分析报告不受影响。"""
    execute("DELETE FROM harvest_jobs WHERE id=?", (jid,))


def clear_harvest_jobs():
    """清空已结束的采集任务记录，运行中的保留。返回 (删除数, 保留数)。

    不碰 reports / scripts / videos，也不重置自增序号：清空历史只影响第 2 步的
    任务列表，经验库里的报告全部保留。
    """
    total = (one("SELECT COUNT(*) n FROM harvest_jobs") or {}).get("n", 0)
    execute("DELETE FROM harvest_jobs WHERE status NOT IN ('queued','running')")
    kept = (one("SELECT COUNT(*) n FROM harvest_jobs") or {}).get("n", 0)
    return total - kept, kept


def add_harvest_report(report, item, job_id, source_provider, engine="rule"):
    """先按原有结构入库，再补写来源字段；保证 generate.py 无需任何改动。"""
    rid = add_report(report, video_id=None, engine=engine)
    execute(
        "UPDATE reports SET aweme_id=?, likes=?, author=?, source_url=?, "
        "source_provider=?, harvest_job_id=?, comment_count=?, publish_time=? WHERE id=?",
        (item.get("aweme_id", ""), item.get("digg_count", 0), item.get("author", ""),
         item.get("share_url", ""), source_provider, job_id,
         item.get("comment_count", 0), item.get("publish_time", 0), rid))
    return rid


# ---------- 同行种子账号 ----------
def list_seeds(limit=200):
    return q("SELECT * FROM seed_accounts ORDER BY id DESC LIMIT ?", (limit,))


def add_seed(**kw):
    return execute(
        "INSERT INTO seed_accounts(nickname,sec_user_id,homepage,category,enabled,note,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (kw.get("nickname", ""), kw.get("sec_user_id", ""), kw.get("homepage", ""),
         kw.get("category", ""), 1 if kw.get("enabled", True) else 0,
         kw.get("note", ""), now()))


def delete_seed(sid):
    execute("DELETE FROM seed_accounts WHERE id=?", (sid,))


# ---------- 搜索结果缓存（避免重复烧调用费） ----------
def cache_get(keyword, provider, ttl_hours=24):
    r = one("SELECT payload, fetched_at FROM search_cache WHERE keyword=? AND provider=?",
            (keyword, provider))
    if not r:
        return None
    try:
        t = datetime.strptime(r["fetched_at"], "%Y-%m-%d %H:%M:%S")
        if (datetime.now() - t).total_seconds() > ttl_hours * 3600:
            return None
        return json.loads(r["payload"])
    except Exception:
        return None


def cache_put(keyword, provider, payload):
    execute(
        "INSERT INTO search_cache(keyword,provider,payload,fetched_at) VALUES(?,?,?,?) "
        "ON CONFLICT(keyword,provider) DO UPDATE SET "
        "payload=excluded.payload, fetched_at=excluded.fetched_at",
        (keyword, provider, json.dumps(payload, ensure_ascii=False), now()))


def stats():
    return {
        "videos": one("SELECT COUNT(*) n FROM videos")["n"],
        "reports": one("SELECT COUNT(*) n FROM reports")["n"],
        "scripts": one("SELECT COUNT(*) n FROM scripts")["n"],
        "jobs": one("SELECT COUNT(*) n FROM jobs")["n"],
        "final_scripts": one("SELECT COUNT(*) n FROM scripts WHERE status='final'")["n"],
        "done_jobs": one("SELECT COUNT(*) n FROM jobs WHERE status='done'")["n"],
        "harvest_jobs": one("SELECT COUNT(*) n FROM harvest_jobs")["n"],
        "harvest_reports": one("SELECT COUNT(*) n FROM reports WHERE harvest_job_id > 0")["n"],
        "seeds": one("SELECT COUNT(*) n FROM seed_accounts")["n"],
    }
