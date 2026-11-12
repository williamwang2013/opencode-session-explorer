# -*- coding: utf-8 -*-
"""opencode 会话浏览器 —— 本地只读服务

设计约束：
  * 对 opencode.db 永远只读，绝不写入。
  * 搜索增强走独立侧车索引库（index/session-index.db），与 opencode.db 分离。
  * 仅监听 127.0.0.1；对带 Origin 头的请求做同源校验，阻断网页跨站读取。
"""

import base64
import datetime
import gzip
import hmac
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

VERSION = "2.1"

# 包根目录（src/py 的上两级），web/ 与 i18n/ 都在下面
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(ROOT, "web")
I18N_DIR = os.path.join(ROOT, "i18n")

HOST = os.environ.get("SESSION_EXPLORER_HOST") or "127.0.0.1"


def _env_port():
    try:
        return int(os.environ.get("SESSION_EXPLORER_PORT") or 8717)
    except (TypeError, ValueError):
        return 8717


PORT = _env_port()


def _default_data_home():
    """数据目录。

    优先沿用旧版目录 ~/.config/opencode/session-explorer（里面有历史索引与令牌），
    否则放到系统数据目录下，避免写进 npm 包目录。
    """
    legacy = os.path.join(os.path.expanduser("~"), ".config", "opencode", "session-explorer")
    if os.path.isdir(os.path.join(legacy, "index")) or os.path.isfile(os.path.join(legacy, "config.json")):
        return legacy
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(xdg, "opencode-session-explorer")


DATA_HOME = os.environ.get("SESSION_EXPLORER_HOME") or _default_data_home()
try:
    os.makedirs(DATA_HOME, exist_ok=True)
except OSError:
    pass

# opencode 自身的数据目录（只读访问）
OPENCODE_DIR = os.environ.get("OPENCODE_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".local", "share", "opencode"
)
DB = os.path.join(OPENCODE_DIR, "opencode.db")
TOOL_OUTPUT_DIR = os.path.join(OPENCODE_DIR, "tool-output")
INDEX_DB = os.path.join(DATA_HOME, "index", "session-index.db")
REPORT_DIR = os.path.join(DATA_HOME, "reports")
CONFIG_PATH = os.path.join(DATA_HOME, "config.json")
INDEX_HTML = os.path.join(WEB_DIR, "index.html")
LOGIN_HTML = os.path.join(WEB_DIR, "login.html")
I18N_JS = os.path.join(WEB_DIR, "i18n.js")

# --------------------------------------------------------------------------
# 多语言：文案在包内 src/i18n/<lang>.json，服务端只取 server.* 前缀
# --------------------------------------------------------------------------
SUPPORTED_LANGS = ("en", "zh")


def _env_lang():
    raw = (os.environ.get("SESSION_EXPLORER_LANG") or "").strip().lower()
    if raw in SUPPORTED_LANGS:
        return raw
    if raw:
        return "zh" if raw.startswith("zh") else "en"
    loc = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or "").lower()
    return "zh" if loc.startswith("zh") else "en"


LANG = _env_lang()
_TRANSLATIONS = {}


def translations(lang=None):
    lang = lang if lang in SUPPORTED_LANGS else LANG
    if lang not in _TRANSLATIONS:
        try:
            with io.open(os.path.join(I18N_DIR, lang + ".json"), "r", encoding="utf-8") as f:
                _TRANSLATIONS[lang] = json.load(f)
        except (IOError, OSError, ValueError):
            _TRANSLATIONS[lang] = {}
    return _TRANSLATIONS[lang]


def t(key, **kw):
    """取服务端文案：当前语言 → 英文 → key 本身。"""
    for lang in (LANG, "en"):
        val = translations(lang).get(key)
        if isinstance(val, str):
            try:
                return val.format(**kw) if kw else val
            except (KeyError, IndexError):
                return val
    return key


def inject_i18n(html, lang):
    """把两种语言的字典与 i18n.js 注入页面，前端可即时切换语言。"""
    payload = json.dumps(
        {"lang": lang, "dict": {code: translations(code) for code in SUPPORTED_LANGS}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    tag = '<script>window.__SE_I18N=' + payload + ';</script>\n<script src="/i18n.js"></script>'
    html = re.sub(r'(<html[^>]*\blang=")[^"]*(")', lambda m: m.group(1) + lang + m.group(2), html, count=1)
    if "</head>" in html:
        return html.replace("</head>", tag + "\n</head>", 1)
    return tag + "\n" + html

TOOL_INLINE_LIMIT = 4000
INDEX_TEXT_LIMIT = 8000
# 侧车索引结构版本：变更时自动重建（3 = 支持 V1/V2 双表）
INDEX_SCHEMA = 3
DEFAULT_PAGE = 120
MAX_PAGE = 400
TOOL_FILE_LIMIT = 8 * 1024 * 1024

MARKER_TYPES = ("compaction", "agent", "subtask")
ALL_KINDS = ("text", "reasoning", "tool", "file", "marker")

TOOL_POINTER = re.compile(r"Full output saved to:\s*(.+?)(?:\s|$)", re.I)


def load_config():
    cfg = {"token": ""}
    try:
        with io.open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in cfg})
    except (IOError, ValueError, OSError):
        pass
    return cfg


def save_config(cfg):
    payload = {
        "token": cfg.get("token") or "",
        "_note": t("server.config_note", port=PORT),
    }
    with io.open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


CONFIG = load_config()

# 登录失败计数（内存内，进程重启即清空）
_login_fails = {}


# --------------------------------------------------------------------------
# 连接
# --------------------------------------------------------------------------
def uri_path(p):
    return p.replace("\\", "/")


def open_ro():
    conn = sqlite3.connect(
        "file:%s?mode=ro" % uri_path(DB), uri=True, check_same_thread=False, timeout=5
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


INDEX_DDL = """
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS part_text(
            rid          INTEGER PRIMARY KEY,
            part_id      TEXT UNIQUE,
            session_id   TEXT NOT NULL,
            message_id   TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            role         TEXT,
            kind         TEXT,
            tool         TEXT,
            title        TEXT,
            text         TEXT
        );
        CREATE INDEX IF NOT EXISTS pt_session_time ON part_text(session_id, time_created);
        CREATE INDEX IF NOT EXISTS pt_time         ON part_text(time_created);
        CREATE INDEX IF NOT EXISTS pt_role         ON part_text(role);
        CREATE VIRTUAL TABLE IF NOT EXISTS part_fts
            USING fts5(text, tool, content='', contentless_delete=1, tokenize='trigram');
"""

# 索引写入用锁（避免并发重复插入）；读取不再共享连接，各请求独立，互不阻塞。
# 必须是 RLock：sync_index 会先持锁、再调 open_index 做首次初始化，而初始化也要拿这把锁。
_index_write_lock = threading.RLock()
_index_ready = False
_last_sync_at = 0.0


def open_index():
    """返回一个新的侧车索引连接。调用方负责关闭，请配合 index_db() 使用。"""
    global _index_ready
    os.makedirs(os.path.dirname(INDEX_DB), exist_ok=True)
    conn = sqlite3.connect(INDEX_DB, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    if not _index_ready:
        with _index_write_lock:
            if not _index_ready:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(INDEX_DDL)
                if str(meta_get(conn, "schema", 0)) != str(INDEX_SCHEMA):
                    conn.executescript("DROP TABLE IF EXISTS part_fts; DELETE FROM part_text;")
                    conn.execute(
                        "CREATE VIRTUAL TABLE part_fts "
                        "USING fts5(text, tool, content='', contentless_delete=1, tokenize='trigram')"
                    )
                    meta_set(conn, "schema", INDEX_SCHEMA)
                    meta_set(conn, "last_ct", 0)
                    meta_set(conn, "last_ut", 0)
                conn.commit()
                _index_ready = True
    return conn


@contextmanager
def index_db():
    conn = open_index()
    try:
        yield conn
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(conn, key, value):
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# --------------------------------------------------------------------------
# 片段抽取
# --------------------------------------------------------------------------
def clean_text(v, limit=INDEX_TEXT_LIMIT):
    if not isinstance(v, str):
        return ""
    return v.replace("\x00", " ")[:limit]


def part_kind(data):
    t = data.get("type")
    return "marker" if t in MARKER_TYPES else t


def extract_part(data):
    t = data.get("type")
    if t == "text":
        return {"t": "text", "text": data.get("text") or ""}
    if t == "reasoning":
        return {"t": "reasoning", "text": data.get("text") or ""}
    if t == "file":
        return {
            "t": "file",
            "name": data.get("filename") or data.get("name") or "",
            "mime": data.get("mime") or "",
            "inline": bool(str(data.get("url") or "").startswith("data:")),
        }
    if t == "tool":
        st = data.get("state") or {}
        md = st.get("metadata") or {}
        out = ""
        if isinstance(md, dict):
            out = md.get("output") or ""
        if not out:
            out = st.get("output") or ""
        out_path = md.get("outputPath") if isinstance(md, dict) else None
        if not out_path and isinstance(out, str):
            m = TOOL_POINTER.search(out)
            if m:
                out_path = m.group(1).strip().strip('"')
        truncated = bool(isinstance(md, dict) and md.get("truncated"))
        if isinstance(out, str) and len(out) > TOOL_INLINE_LIMIT:
            out = out[:TOOL_INLINE_LIMIT]
            truncated = True
        return {
            "t": "tool",
            "tool": data.get("tool") or "tool",
            "status": st.get("status") or "",
            "title": st.get("title") or "",
            "input": st.get("input") if isinstance(st.get("input"), (dict, list)) else None,
            "output": out,
            "truncated": truncated,
            "full": bool(out_path),
        }
    if t == "compaction":
        return {"t": "marker", "label": "上下文压缩 compact"}
    if t == "agent":
        return {"t": "marker", "label": "子代理: " + str(data.get("name") or data.get("agent") or "")}
    if t == "subtask":
        return {"t": "marker", "label": "子任务 subtask"}
    return None


def index_text_of(kind, part):
    if kind in ("text", "reasoning"):
        return clean_text(part.get("text") or "")
    if kind == "tool":
        buf = [str(part.get("tool") or ""), str(part.get("title") or "")]
        if part.get("input") is not None:
            try:
                buf.append(json.dumps(part["input"], ensure_ascii=False))
            except (TypeError, ValueError):
                pass
        buf.append(str(part.get("output") or ""))
        return clean_text("\n".join(buf))
    if kind == "file":
        return clean_text("%s %s" % (part.get("name") or "", part.get("mime") or ""))
    if kind == "marker":
        return clean_text(part.get("label") or "")
    return ""


# --------------------------------------------------------------------------
# 侧车索引
# --------------------------------------------------------------------------
def _index_upsert(idx, part_id, session_id, message_id, time_created, role, kind, tool, title, text):
    old = idx.execute("SELECT rid FROM part_text WHERE part_id=?", (part_id,)).fetchone()
    if old:
        idx.execute("DELETE FROM part_fts WHERE rowid=?", (old["rid"],))
        idx.execute("DELETE FROM part_text WHERE rid=?", (old["rid"],))
    cur = idx.execute(
        "INSERT INTO part_text(part_id,session_id,message_id,time_created,role,kind,tool,title,text) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (part_id, session_id, message_id, time_created, role, kind, tool, title, text),
    )
    idx.execute("INSERT INTO part_fts(rowid, text, tool) VALUES(?,?,?)",
                (cur.lastrowid, text, tool))


def sync_index(rebuild=False):
    with _index_write_lock, index_db() as idx:
        if rebuild:
            idx.executescript("DELETE FROM part_fts; DELETE FROM part_text;")
            meta_set(idx, "last_ct", 0)
            meta_set(idx, "last_ut", 0)
            meta_set(idx, "v2_last_ct", 0)
            meta_set(idx, "v2_last_ut", 0)
            idx.commit()

        last_ct = int(meta_get(idx, "last_ct", 0) or 0)
        last_ut = int(meta_get(idx, "last_ut", 0) or 0)
        v2_ct = int(meta_get(idx, "v2_last_ct", 0) or 0)
        v2_ut = int(meta_get(idx, "v2_last_ut", 0) or 0)

        src = open_ro()
        try:
            rows = src.execute(
                "SELECT p.id, p.message_id, p.session_id, p.time_created, p.time_updated, "
                "       p.data AS pdata, m.data AS mdata "
                "FROM part p JOIN message m ON m.id = p.message_id "
                "WHERE p.time_created > ? OR p.time_updated > ? "
                "ORDER BY p.time_created, p.id LIMIT 60000",
                (last_ct, last_ut),
            ).fetchall()
            v2_rows = []
            if v2_available(src):
                v2_rows = src.execute(
                    "SELECT id, session_id, type, seq, time_created, time_updated, data "
                    "FROM session_message WHERE time_created > ? OR time_updated > ? "
                    "ORDER BY time_created, seq LIMIT 60000",
                    (v2_ct, v2_ut),
                ).fetchall()
        finally:
            src.close()

        indexed_now = idx.execute("SELECT count(*) c FROM part_text").fetchone()["c"]
        if not rows and not v2_rows:
            return {"added": 0, "indexed": indexed_now}

        max_ct, max_ut, added = last_ct, last_ut, 0
        for r in rows:
            try:
                pdata = json.loads(r["pdata"])
            except (TypeError, ValueError):
                continue
            try:
                role = json.loads(r["mdata"]).get("role")
            except (TypeError, ValueError):
                role = None
            kind = part_kind(pdata)
            if kind not in ALL_KINDS:
                continue
            part = extract_part(pdata) or {}
            text = index_text_of(kind, part)
            title = part.get("title") or part.get("label") or part.get("name") or ""
            tool = part.get("tool") or ""
            _index_upsert(idx, r["id"], r["session_id"], r["message_id"],
                          r["time_created"], role, kind, tool, title, text)
            added += 1
            max_ct = max(max_ct, r["time_created"])
            max_ut = max(max_ut, r["time_updated"])

        v2_max_ct, v2_max_ut = v2_ct, v2_ut
        for r in v2_rows:
            try:
                d = json.loads(r["data"])
            except (TypeError, ValueError):
                continue
            role = v2_msg_to_role(d, r["type"])
            created = (d.get("time") or {}).get("created") or r["time_created"]
            raw_parts = []
            if r["type"] == "user":
                if (d.get("text") or "").strip():
                    raw_parts.append({"type": "text", "text": d["text"]})
            elif r["type"] == "assistant":
                raw_parts = d.get("content") or []
            elif r["type"] in ("compaction", "system", "synthetic"):
                txt = d.get("summary") or d.get("text") or d.get("description") or ""
                if txt:
                    raw_parts = [{"type": "text", "text": str(txt)}]
            for i, p in enumerate(raw_parts):
                if not isinstance(p, dict):
                    continue
                kind = part_kind(p)
                if kind not in ALL_KINDS:
                    continue
                part = extract_part(p) or {}
                text = index_text_of(kind, part)
                if not text:
                    continue
                title = part.get("title") or part.get("label") or part.get("name") or ""
                tool = part.get("tool") or ""
                pid = p.get("id") or ("%s_v2p%d" % (r["id"], i))
                _index_upsert(idx, pid, r["session_id"], r["id"],
                              created, role, kind, tool, title, text)
                added += 1
            v2_max_ct = max(v2_max_ct, r["time_created"])
            v2_max_ut = max(v2_max_ut, r["time_updated"] or 0)

        meta_set(idx, "last_ct", max_ct)
        meta_set(idx, "last_ut", max_ut)
        meta_set(idx, "v2_last_ct", v2_max_ct)
        meta_set(idx, "v2_last_ut", v2_max_ut)
        meta_set(idx, "synced_at", int(time.time() * 1000))
        idx.commit()
        indexed_now = idx.execute("SELECT count(*) c FROM part_text").fetchone()["c"]
        return {"added": added, "indexed": indexed_now}


def sync_index_throttled(min_interval=10.0):
    """节流版同步：短时间内多次调用只真正同步一次，避免频繁抢写锁。"""
    global _last_sync_at
    now = time.time()
    if now - _last_sync_at < min_interval:
        return None
    _last_sync_at = now
    try:
        return sync_index()
    except sqlite3.Error:
        return None


def index_state():
    with index_db() as idx:
        return {
            "indexed": idx.execute("SELECT count(*) c FROM part_text").fetchone()["c"],
            "synced_at": int(meta_get(idx, "synced_at", 0) or 0),
            "path": INDEX_DB,
        }


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def to_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def ms_str(v):
    if not v:
        return ""
    return datetime.datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d %H:%M")


def day_of(v):
    return datetime.datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d")


def parse_model(v):
    if not v:
        return ""
    try:
        d = json.loads(v)
        if isinstance(d, dict):
            mid, prov = d.get("id") or "", d.get("providerID") or ""
            return (prov + "/" + mid).strip("/") or v
    except (TypeError, ValueError):
        pass
    return v


def first_arg(params, key, default=None):
    v = params.get(key)
    return v[0] if v else default


def csv_set(params, key, allowed=None):
    raw = first_arg(params, key)
    if not raw:
        return None
    items = {x.strip() for x in str(raw).split(",") if x.strip()}
    if allowed:
        items &= set(allowed)
    return items or None


def read_all_sessions(src):
    """读取全部会话（V1 + V2 合并，同名 id 以 V2 为准）。"""
    sql, args = unified_sessions(
        src,
        extra_cols=(),
        order_by="time_updated DESC",
    )
    return src.execute(sql, args).fetchall()


# --------------------------------------------------------------------------
# 兼容层：opencode V1 / V2 双表结构
# --------------------------------------------------------------------------
# opencode 2.x（桌面端 2.0.x）把存储换成了 V2 结构：
#   * 会话主表：session      -> session_v2
#   * 消息表：  message      -> session_message
#     （V2 把 part 内嵌进 message.data.content[]，不再有独立 part 表）
# 两套结构并存于同一个 opencode.db。本服务对外统一暴露 V1 语义，
# 由本适配层把 V2 数据「翻译」成 V1 形态，其余代码无需感知差异。

def has_table(src, name):
    return bool(src.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def table_cols(src, table):
    try:
        return {r["name"] for r in src.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.Error:
        return set()


def v2_available(src):
    """当前库是否存在可用的 V2 表。"""
    return has_table(src, "session_v2") and has_table(src, "session_message")


# 两张会话表共有的列（其余列在另一侧补 NULL）
SESSION_COLS = [
    "id", "project_id", "workspace_id", "parent_id", "slug", "directory", "path",
    "title", "version", "share_url", "summary_additions", "summary_deletions",
    "summary_files", "summary_diffs", "metadata", "cost", "tokens_input",
    "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write",
    "revert", "permission", "agent", "model", "time_created", "time_updated",
    "time_compacting", "time_archived",
]


def unified_sessions(src, extra_cols=(), where=None, args=(), order_by=None,
                     limit=None, offset=None):
    """在 session / session_v2 上做 UNION ALL，返回统一行集（带 _src 标记）。

    * 以 V2 为准：同名 id 只取 V2 行，旧表只补 V2 中不存在的会话。
    * extra_cols 走子查询（如 msgs/parts 计数），只对 V1 行计算，V2 行填 0/NULL。
    * src 传 None 时自行开一个只读连接做 schema 探测。
    * 返回 (sql, args) —— 因 UNION 两侧各要一份参数，args 会自动复制两份。
    """
    own = src is None
    if own:
        src = open_ro()
    try:
        cols = SESSION_COLS + [c for c in extra_cols]
        v2 = v2_available(src)
        w = ("(" + where + ")") if where else None

        def sel(table, is_v2):
            have = table_cols(src, table)
            out = []
            for c in SESSION_COLS:
                out.append("%s AS %s" % (c, c) if c in have else "NULL AS %s" % c)
            for c in extra_cols:
                if c == "msgs":
                    if is_v2:
                        out.append("(SELECT count(*) FROM session_message m "
                                   "WHERE m.session_id=%s.id) AS msgs" % table)
                    else:
                        out.append("(SELECT count(*) FROM message m "
                                   "WHERE m.session_id=%s.id) AS msgs" % table)
                elif c == "parts":
                    if is_v2:
                        out.append("0 AS parts")
                    else:
                        out.append("(SELECT count(*) FROM part p "
                                   "WHERE p.session_id=%s.id) AS parts" % table)
                else:
                    out.append("NULL AS %s" % c)
            return out

        if not v2:
            sql = "SELECT %s FROM session" % ", ".join(sel("session", False))
            if w:
                sql += " WHERE " + w
        else:
            sql = (
                "SELECT %s, 'v2' AS _src FROM session_v2%s "
                "UNION ALL "
                "SELECT %s, 'v1' AS _src FROM session "
                "WHERE id NOT IN (SELECT id FROM session_v2)%s"
                % (
                    ", ".join(sel("session_v2", True)),
                    (" WHERE " + w) if w else "",
                    ", ".join(sel("session", False)),
                    (" AND " + w) if w else "",
                )
            )
            sql = "SELECT * FROM (" + sql + ") t"
        if order_by:
            sql += " ORDER BY " + order_by
        if limit:
            sql += " LIMIT %d OFFSET %d" % (limit, offset or 0)
        return sql, (list(args) * 2 if v2 else list(args))
    finally:
        if own:
            try:
                src.close()
            except sqlite3.Error:
                pass


# --- V2 消息 -> V1 形态翻译 -------------------------------------------------

def v2_msg_to_role(data, mtype):
    """把 V2 的 session_message 行映射成 V1 的 message 语义。"""
    if mtype in ("user", "assistant", "system", "synthetic"):
        return {"user": "user", "assistant": "assistant",
                "system": "system", "synthetic": "user"}[mtype]
    return mtype


def v2_content_to_parts(mid, sid, content, created):
    """V2 的 assistant.content[] -> V1 的 part 行列表。"""
    parts = []
    for i, p in enumerate(content or []):
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        pid = p.get("id") or ("%s_v2p%d" % (mid, i))
        if t == "text":
            parts.append({"id": pid, "message_id": mid, "session_id": sid,
                          "time_created": created, "time_updated": created,
                          "type": "text", "text": p.get("text") or ""})
        elif t == "reasoning":
            parts.append({"id": pid, "message_id": mid, "session_id": sid,
                          "time_created": (p.get("time") or {}).get("created") or created,
                          "time_updated": (p.get("time") or {}).get("completed") or created,
                          "type": "reasoning", "text": p.get("text") or ""})
        elif t == "tool":
            st = p.get("state") or {}
            parts.append({"id": pid, "message_id": mid, "session_id": sid,
                          "time_created": (p.get("time") or {}).get("created") or created,
                          "time_updated": (p.get("time") or {}).get("completed") or created,
                          "type": "tool",
                          "tool": p.get("name") or "",
                          "callID": p.get("id") or "",
                          "state": st})
        elif t == "file":
            parts.append({"id": pid, "message_id": mid, "session_id": sid,
                          "time_created": created, "time_updated": created,
                          "type": "file",
                          "filename": p.get("filename") or p.get("name") or "",
                          "mime": p.get("mime") or "",
                          "url": p.get("url") or ""})
    return parts


# --------------------------------------------------------------------------
# 接口
# --------------------------------------------------------------------------
def api_health(params):
    src = open_ro()
    try:
        v1s = src.execute("SELECT count(*) c FROM session").fetchone()["c"]
        v1m = src.execute("SELECT count(*) c FROM message").fetchone()["c"]
        v2s = v2m = 0
        if v2_available(src):
            v2s = src.execute("SELECT count(*) c FROM session_v2").fetchone()["c"]
            v2m = src.execute("SELECT count(*) c FROM session_message").fetchone()["c"]
        merged = src.execute(
            "SELECT count(*) c FROM (%s)" % unified_sessions(src, where=None)[0]
        ).fetchone()["c"]
    finally:
        src.close()
    return {
        "ok": True,
        "version": VERSION,
        "db": DB,
        "sessions": merged,
        "messages": v1m + v2m,
        "storage": {"v1_sessions": v1s, "v1_messages": v1m,
                    "v2_sessions": v2s, "v2_messages": v2m,
                    "v2_available": bool(v2s)},
        "index": index_state(),
        "token_required": bool(CONFIG.get("token")),
        "lang": LANG,
        "langs": list(SUPPORTED_LANGS),
        "data_home": DATA_HOME,
    }


def api_sessions(params):
    q = (first_arg(params, "q", "") or "").strip()
    dirs = first_arg(params, "dir", "") or ""
    frm = to_int(first_arg(params, "from", 0))
    to = to_int(first_arg(params, "to", 0))
    sort = first_arg(params, "sort", "desc")
    main = first_arg(params, "main", "0") == "1"
    limit = min(max(to_int(first_arg(params, "limit", 500), 500), 1), 2000)
    offset = max(to_int(first_arg(params, "offset", 0)), 0)

    where, args = ["time_archived IS NULL"], []
    if q:
        where.append("(title LIKE ? OR directory LIKE ? OR id LIKE ?)")
        args += ["%" + q + "%"] * 3
    if dirs:
        where.append("directory = ?")
        args.append(dirs)
    if frm:
        where.append("time_created >= ?")
        args.append(frm)
    if to:
        where.append("time_created <= ?")
        args.append(to)
    if main:
        where.append("(parent_id IS NULL OR parent_id = '')")

    sql, uargs = unified_sessions(
        src=None,
        extra_cols=("msgs", "parts"),
        where=" AND ".join(where),
        args=tuple(args),
        order_by="time_updated " + ("ASC" if sort == "asc" else "DESC"),
        limit=limit,
        offset=offset,
    )

    src = open_ro()
    try:
        rows = [dict(r) for r in src.execute(sql, uargs)]
        titles = {r["id"]: r["title"] for r in read_all_sessions(src)}
        # 目录聚合也按合并后的集合统计
        agg_sql, agg_args = unified_sessions(
            src, where="time_archived IS NULL", args=()
        )
        dir_rows = src.execute(
            "SELECT directory, count(*) c FROM (%s) GROUP BY directory ORDER BY c DESC"
            % agg_sql
        ).fetchall()
    finally:
        src.close()

    for r in rows:
        r["model"] = parse_model(r.get("model"))
        r["parent_title"] = titles.get(r.get("parent_id") or "", "")
        r["source"] = r.pop("_src", "v1")
    return {
        "sessions": rows,
        "count": len(rows),
        "limit": limit,
        "offset": offset,
        "dirs": [{"directory": d["directory"], "count": d["c"]} for d in dir_rows],
    }


def api_session(params):
    sid = first_arg(params, "id", "")
    if not sid:
        return {"error": "missing id"}, 400
    src = open_ro()
    try:
        src_kind = session_source(src, sid)
        v2 = src_kind == "v2"
        tbl = "session_v2" if v2 else "session"
        s = src.execute("SELECT * FROM %s WHERE id=?" % tbl, (sid,)).fetchone()
        if not s:
            return {"error": "not found"}, 404
        meta = dict(s)
        meta["model"] = parse_model(meta.get("model"))
        meta["storage"] = src_kind
        if meta.get("parent_id"):
            pr = src.execute("SELECT title FROM %s WHERE id=?" % tbl,
                             (meta["parent_id"],)).fetchone()
            meta["parent_title"] = pr["title"] if pr else ""
        if v2:
            children = src.execute(
                "SELECT id,title,time_created,time_updated,agent,cost,"
                "tokens_input,tokens_output,tokens_reasoning,"
                "(SELECT count(*) FROM session_message m WHERE m.session_id=session_v2.id) msgs,"
                "0 parts "
                "FROM session_v2 WHERE parent_id=? ORDER BY time_created",
                (sid,),
            ).fetchall()
        else:
            children = src.execute(
                "SELECT id,title,time_created,time_updated,agent,cost,"
                "tokens_input,tokens_output,tokens_reasoning,"
                "(SELECT count(*) FROM message m WHERE m.session_id=session.id) msgs,"
                "(SELECT count(*) FROM part p WHERE p.session_id=session.id) parts "
                "FROM session WHERE parent_id=? ORDER BY time_created",
                (sid,),
            ).fetchall()
        meta["children"] = [dict(c) for c in children]
        meta["children_summary"] = {
            "count": len(children),
            "cost": round(sum(c["cost"] or 0 for c in children), 4),
            "msgs": sum(c["msgs"] for c in children),
            "parts": sum(c["parts"] for c in children),
        }

        if v2:
            msg_tbl = "session_message"
            counts = {
                "messages": src.execute(
                    "SELECT count(*) c FROM session_message WHERE session_id=?", (sid,)
                ).fetchone()["c"],
                "parts": 0,  # V2 无独立 part 表，内容内嵌
            }
            counts["by_role"] = {
                r["t"] or "?": r["c"]
                for r in src.execute(
                    "SELECT type t, count(*) c FROM session_message "
                    "WHERE session_id=? GROUP BY type",
                    (sid,),
                )
            }
        else:
            msg_tbl = "message"
            counts = {
                "messages": src.execute("SELECT count(*) c FROM message WHERE session_id=?", (sid,)).fetchone()["c"],
                "parts": src.execute("SELECT count(*) c FROM part WHERE session_id=?", (sid,)).fetchone()["c"],
            }
            counts["by_role"] = {
                r["role"] or "?": r["c"]
                for r in src.execute(
                    "SELECT json_extract(data,'$.role') role, count(*) c FROM message "
                    "WHERE session_id=? GROUP BY role",
                    (sid,),
                )
            }
    finally:
        src.close()

    # 片段类型统计走侧车索引，避免对大会话做全表 JSON 解析
    try:
        sync_index_throttled()
        with index_db() as idx:
            counts["by_kind"] = {
                r["kind"] or "?": r["c"]
                for r in idx.execute(
                    "SELECT kind, count(*) c FROM part_text WHERE session_id=? GROUP BY kind", (sid,)
                )
            }
    except sqlite3.Error:
        counts["by_kind"] = {}

    src = open_ro()
    try:
        meta["days"] = [
            dict(d)
            for d in src.execute(
                "SELECT strftime('%%Y-%%m-%%d', time_created/1000, 'unixepoch', 'localtime') d, "
                "count(*) msgs, min(time_created) t0, max(time_created) t1 "
                "FROM %s WHERE session_id=? GROUP BY d ORDER BY d" % msg_tbl,
                (sid,),
            )
        ]
        meta["todos"] = [
            dict(t)
            for t in src.execute(
                "SELECT content,status,priority,position FROM todo WHERE session_id=? ORDER BY position",
                (sid,),
            )
        ]
    finally:
        src.close()
    return {"session": meta, "counts": counts}, 200


def session_source(src, sid):
    """判断某个会话属于 V2 还是 V1 存储。"""
    if v2_available(src) and src.execute(
        "SELECT 1 FROM session_v2 WHERE id=?", (sid,)
    ).fetchone():
        return "v2"
    return "v1"


# V2 中不承载对话内容的行类型（列表/详情默认跳过）
V2_NON_CHAT = ("idle", "model-switched", "agent-switched", "location-switched")


class _RowDict(dict):
    """轻量行对象：既能当 dict 用（.get/k in），也能像 sqlite3.Row 一样下标取值。"""
    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return dict.__getitem__(self, k)



def v2_msg_rows(src, sid, roles, before_time, before_id, around_time, limit):
    """V2 消息读取：模拟 V1 的 message 行（id/time_created/data）。"""
    where, args = ["session_id = ?"], [sid]
    if roles:
        where.append("type IN (%s)" % ",".join("?" * len(roles)))
        args += sorted(roles)
    else:
        # 非对话行类型直接以字面量排除（常量，无注入风险）
        lit = ",".join("'%s'" % t for t in V2_NON_CHAT)
        where.append("type NOT IN (%s)" % lit)
    if around_time:
        where.append("time_created <= ?")
        args.append(around_time)
    elif before_time:
        where.append("(time_created < ? OR (time_created = ? AND id < ?))")
        args += [before_time, before_time, before_id or ""]
    sql = (
        "SELECT id, session_id, type, seq, time_created, data FROM session_message WHERE "
        + " AND ".join(where)
        + " ORDER BY time_created DESC, seq DESC LIMIT ?"
    )
    args.append(limit)
    rows = src.execute(sql, tuple(args)).fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (TypeError, ValueError):
            d = {}
        # 归一成 V1 message 语义：{"role":..., "time":{...}, "modelID":..., ...}
        role = v2_msg_to_role(d, r["type"])
        norm = {
            "role": role,
            "time": d.get("time") or {"created": r["time_created"]},
            "agent": d.get("agent") or "",
            "modelID": ((d.get("model") or {}) or {}).get("id") or
                       ((d.get("model") or {}) or {}).get("modelID") or "",
            "cost": d.get("cost") or 0,
            "tokens": d.get("tokens") or {},
            "_v2_type": r["type"],
            "_v2_seq": r["seq"],
        }
        if r["type"] == "user":
            # 用户文本直接挂在消息上（V1 里是 part，这里也补一个 text part）
            norm["_user_text"] = d.get("text") or ""
        if r["type"] == "assistant":
            norm["_content"] = d.get("content") or []
        out.append({
            "id": r["id"],
            "time_created": (d.get("time") or {}).get("created") or r["time_created"],
            "time_updated": (d.get("time") or {}).get("completed") or r["time_created"],
            "data": json.dumps(norm, ensure_ascii=False),
            "_norm": norm,
        })
    return out


def v2_parts_for(src, sid, msg_ids):
    """V2：从消息内嵌 content 还原出 part 列表（形如 V1 part 行）。"""
    if not msg_ids:
        return []
    qmarks = ",".join("?" * len(msg_ids))
    rows = src.execute(
        "SELECT id, session_id, type, time_created, data FROM session_message "
        "WHERE id IN (%s)" % qmarks, tuple(msg_ids)
    ).fetchall()
    parts = []
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (TypeError, ValueError):
            continue
        t = r["time_created"]
        created = (d.get("time") or {}).get("created") or t
        if r["type"] == "user":
            txt = d.get("text") or ""
            if txt:
                parts.append({"id": r["id"] + "_t0", "message_id": r["id"],
                              "session_id": sid, "time_created": created,
                              "data": json.dumps({"type": "text", "text": txt},
                                                 ensure_ascii=False)})
            for i, f in enumerate(d.get("files") or []):
                if isinstance(f, dict):
                    parts.append({"id": r["id"] + "_f%d" % i, "message_id": r["id"],
                                  "session_id": sid, "time_created": created,
                                  "data": json.dumps({
                                      "type": "file",
                                      "filename": f.get("filename") or f.get("name") or "",
                                      "mime": f.get("mime") or "",
                                      "url": f.get("url") or "",
                                  }, ensure_ascii=False)})
        elif r["type"] == "assistant":
            for p in v2_content_to_parts(r["id"], sid, d.get("content"), created):
                parts.append({"id": p["id"], "message_id": p["message_id"],
                              "session_id": p["session_id"],
                              "time_created": p["time_created"],
                              "data": json.dumps(
                                  {k: v for k, v in p.items()
                                   if k not in ("id", "message_id", "session_id",
                                                "time_created", "time_updated")},
                                  ensure_ascii=False, default=str)})
        elif r["type"] in ("compaction", "system", "synthetic"):
            txt = d.get("summary") or d.get("text") or d.get("description") or ""
            if txt:
                parts.append({"id": r["id"] + "_c", "message_id": r["id"],
                              "session_id": sid, "time_created": created,
                              "data": json.dumps(
                                  {"type": "text", "text": str(txt)},
                                  ensure_ascii=False)})
    return parts


def _msg_rows(src, sid, roles, before_time, before_id, around_time, limit):
    if session_source(src, sid) == "v2":
        return v2_msg_rows(src, sid, roles, before_time, before_id, around_time, limit)
    where, args = ["session_id = ?"], [sid]
    if roles:
        where.append("json_extract(data,'$.role') IN (%s)" % ",".join("?" * len(roles)))
        args += sorted(roles)
    if around_time:
        where.append("time_created <= ?")
        args.append(around_time)
    elif before_time:
        where.append("(time_created < ? OR (time_created = ? AND id < ?))")
        args += [before_time, before_time, before_id or ""]
    sql = (
        "SELECT id,time_created,time_updated,data FROM message WHERE "
        + " AND ".join(where)
        + " ORDER BY time_created DESC, id DESC LIMIT ?"
    )
    args.append(limit)
    return src.execute(sql, tuple(args)).fetchall()


def api_parts(params):
    sid = first_arg(params, "session_id", "")
    if not sid:
        return {"error": "missing session_id"}, 400
    limit = min(max(to_int(first_arg(params, "limit", DEFAULT_PAGE), DEFAULT_PAGE), 1), MAX_PAGE)
    before_time = to_int(first_arg(params, "before_time", 0))
    before_id = first_arg(params, "before_id", "") or ""
    around_time = to_int(first_arg(params, "around_time", 0))
    roles = csv_set(params, "roles", ("user", "assistant"))
    kinds = csv_set(params, "kinds", ALL_KINDS)

    src = open_ro()
    try:
        is_v2 = session_source(src, sid) == "v2"
        rows = _msg_rows(src, sid, roles, before_time, before_id, around_time, limit)
        if not rows:
            return {"messages": [], "has_more": False, "cursor": None}, 200
        ids = [r["id"] for r in rows]
        qmarks = ",".join("?" * len(ids))
        if is_v2:
            part_rows = v2_parts_for(src, sid, ids)
            parts = [_RowDict(id=p["id"], message_id=p["message_id"],
                              time_created=p["time_created"], data=p["data"])
                     for p in part_rows]
            part_total = {}
            for p in part_rows:
                part_total[p["message_id"]] = part_total.get(p["message_id"], 0) + 1
        else:
            parts = src.execute(
                "SELECT id,message_id,time_created,data FROM part WHERE message_id IN (%s) "
                "ORDER BY time_created, id" % qmarks,
                tuple(ids),
            ).fetchall()
            part_total = {
                r["message_id"]: r["c"]
                for r in src.execute(
                    "SELECT message_id, count(*) c FROM part WHERE message_id IN (%s) GROUP BY message_id" % qmarks,
                    tuple(ids),
                )
            }
    finally:
        src.close()

    grouped = {}
    for p in parts:
        try:
            data = json.loads(p["data"])
        except (TypeError, ValueError):
            continue
        kind = part_kind(data)
        if kinds and kind not in kinds:
            continue
        item = extract_part(data)
        if not item:
            continue
        item["id"] = p["id"]
        item["time"] = p["time_created"]
        grouped.setdefault(p["message_id"], []).append(item)

    out = []
    for r in rows:
        if isinstance(r, dict) and "_norm" in r:
            md = r["_norm"]
            mid, mtime = r["id"], r["time_created"]
        else:
            mid, mtime = r["id"], r["time_created"]
            try:
                md = json.loads(r["data"])
            except (TypeError, ValueError):
                md = {}
            # V1 的模型字段在 modelID / providerID
            md.setdefault("modelID", md.get("modelID") or "")
        out.append(
            {
                "id": mid,
                "time": mtime,
                "role": md.get("role") or "",
                "agent": md.get("agent") or "",
                "model": md.get("modelID") or "",
                "cost": md.get("cost") or 0,
                "tokens": md.get("tokens") or {},
                "part_total": part_total.get(mid, 0),
                "parts": grouped.get(mid, []),
            }
        )

    oldest = rows[-1]
    oldest_id = oldest["id"] if isinstance(oldest, dict) and "_norm" in oldest else oldest["id"]
    oldest_t = oldest["time_created"]
    has_more = False
    if len(rows) == limit:
        src = open_ro()
        try:
            if is_v2:
                has_more = bool(
                    src.execute(
                        "SELECT 1 FROM session_message WHERE session_id=? AND "
                        "(time_created < ? OR (time_created = ? AND seq < ?)) LIMIT 1",
                        (sid, oldest_t, oldest_t,
                         (oldest.get("_norm", {}) or {}).get("_v2_seq", 0)
                         if isinstance(oldest, dict) else 0),
                    ).fetchone()
                )
            else:
                has_more = bool(
                    src.execute(
                        "SELECT 1 FROM message WHERE session_id=? AND "
                        "(time_created < ? OR (time_created = ? AND id < ?)) LIMIT 1",
                        (sid, oldest_t, oldest_t, oldest_id),
                    ).fetchone()
                )
        finally:
            src.close()

    return {
        "messages": out,
        "has_more": has_more,
        "cursor": {"time": oldest_t, "id": oldest_id},
    }, 200


def _snippet(text, q, width=140):
    if not text:
        return ""
    i = text.lower().find(q.lower()) if q else -1
    if i < 0:
        return " ".join(text[: width * 2].split())
    start = max(0, i - width)
    end = min(len(text), i + len(q) + width)
    return ("…" if start > 0 else "") + " ".join(text[start:end].split()) + ("…" if end < len(text) else "")


def api_search(params):
    with index_db() as idx:
        return _api_search(params, idx)


def _api_search(params, idx):
    q = (first_arg(params, "q", "") or "").strip()
    if not q:
        return {"hits": [], "count": 0, "index": index_state()}, 200
    sid = first_arg(params, "session_id", "") or ""
    dirs = first_arg(params, "dir", "") or ""
    frm = to_int(first_arg(params, "from", 0))
    to = to_int(first_arg(params, "to", 0))
    roles = csv_set(params, "roles", ("user", "assistant"))
    kinds = csv_set(params, "kinds", ALL_KINDS)
    limit = min(max(to_int(first_arg(params, "limit", 60), 60), 1), 300)
    offset = max(to_int(first_arg(params, "offset", 0)), 0)

    sync_index()

    src = open_ro()
    try:
        smeta = {r["id"]: dict(r) for r in read_all_sessions(src)}
    finally:
        src.close()

    where, args = [], []
    if sid:
        where.append("t.session_id = ?")
        args.append(sid)
    if dirs:
        ids = [k for k, v in smeta.items() if v["directory"] == dirs]
        if not ids:
            return {"hits": [], "count": 0, "index": index_state()}, 200
        where.append("t.session_id IN (%s)" % ",".join("?" * len(ids)))
        args += ids
    if frm:
        where.append("t.time_created >= ?")
        args.append(frm)
    if to:
        where.append("t.time_created <= ?")
        args.append(to)
    if roles:
        where.append("t.role IN (%s)" % ",".join("?" * len(roles)))
        args += sorted(roles)
    if kinds:
        where.append("t.kind IN (%s)" % ",".join("?" * len(kinds)))
        args += sorted(kinds)
    tail = (" AND " + " AND ".join(where)) if where else ""

    rows, total = [], 0
    if len(q) >= 3:
        phrase = '"' + q.replace('"', '""') + '"'
        base = "FROM part_fts f JOIN part_text t ON t.rid = f.rowid WHERE part_fts MATCH ?" + tail
        try:
            total = idx.execute("SELECT count(*) c " + base, tuple([phrase] + args)).fetchone()["c"]
            rows = idx.execute(
                "SELECT t.* " + base + " ORDER BY bm25(part_fts) LIMIT ? OFFSET ?",
                tuple([phrase] + args + [limit, offset]),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        base = "FROM part_text t WHERE t.text LIKE ?" + tail
        like = "%" + q + "%"
        total = idx.execute("SELECT count(*) c " + base, tuple([like] + args)).fetchone()["c"]
        rows = idx.execute(
            "SELECT t.* " + base + " ORDER BY t.time_created DESC LIMIT ? OFFSET ?",
            tuple([like] + args + [limit, offset]),
        ).fetchall()

    hits = []
    for r in rows:
        s = smeta.get(r["session_id"], {})
        hits.append(
            {
                "session_id": r["session_id"],
                "session_title": s.get("title") or "",
                "directory": s.get("directory") or "",
                "parent_id": s.get("parent_id") or "",
                "message_id": r["message_id"],
                "part_id": r["part_id"],
                "time": r["time_created"],
                "role": r["role"],
                "kind": r["kind"],
                "tool": r["tool"],
                "title": r["title"],
                "snippet": _snippet(r["text"], q),
            }
        )
    return {"hits": hits, "count": total, "limit": limit, "offset": offset, "index": index_state()}, 200


def api_index(params):
    return sync_index(rebuild=first_arg(params, "rebuild", "0") == "1"), 200


def api_stats(params):
    frm = to_int(first_arg(params, "from", 0))
    to = to_int(first_arg(params, "to", 0))
    where, args = [], []
    if frm:
        where.append("time_created >= ?")
        args.append(frm)
    if to:
        where.append("time_created <= ?")
        args.append(to)
    cond = (" WHERE " + " AND ".join(where)) if where else ""

    src = open_ro()
    try:
        usql, uargs = unified_sessions(
            src,
            where=(" AND ".join(where) if where else None),
            args=tuple(args),
        )
        sessions = src.execute(
            "SELECT id,title,directory,model,cost,tokens_input,tokens_output,tokens_reasoning,"
            "tokens_cache_read,tokens_cache_write,time_created,_src FROM (%s)" % usql,
            uargs,
        ).fetchall()
        # 消息按天统计：V1/V2 两张消息表合并
        v1_days, v2_days = [], []
        if where:
            w = " AND ".join(where)
            v1_days = src.execute(
                "SELECT strftime('%Y-%m-%d', time_created/1000, 'unixepoch', 'localtime') d, "
                "count(*) msgs FROM message WHERE " + w + " GROUP BY d", tuple(args)
            ).fetchall()
        else:
            v1_days = src.execute(
                "SELECT strftime('%Y-%m-%d', time_created/1000, 'unixepoch', 'localtime') d, "
                "count(*) msgs FROM message GROUP BY d"
            ).fetchall()
        if v2_available(src):
            if where:
                v2_days = src.execute(
                    "SELECT strftime('%Y-%m-%d', time_created/1000, 'unixepoch', 'localtime') d, "
                    "count(*) msgs FROM session_message WHERE " + " AND ".join(where)
                    + " GROUP BY d", tuple(args)
                ).fetchall()
            else:
                v2_days = src.execute(
                    "SELECT strftime('%Y-%m-%d', time_created/1000, 'unixepoch', 'localtime') d, "
                    "count(*) msgs FROM session_message GROUP BY d"
                ).fetchall()
    finally:
        src.close()

    dm = {}
    for d in list(v1_days) + list(v2_days):
        dm[d["d"]] = dm.get(d["d"], 0) + d["msgs"]
    days = [{"d": k, "msgs": v} for k, v in sorted(dm.items())]

    per_day_msgs = {d["d"]: d["msgs"] for d in days}
    by_day, by_dir, by_model = {}, {}, {}
    total = {"sessions": len(sessions), "cost": 0.0, "tokens": 0, "messages": 0}

    for s in sessions:
        tok = sum(
            s[k] or 0
            for k in (
                "tokens_input",
                "tokens_output",
                "tokens_reasoning",
                "tokens_cache_read",
                "tokens_cache_write",
            )
        )
        cost = s["cost"] or 0
        for key, bucket in (
            (day_of(s["time_created"]), by_day),
            (s["directory"] or "-", by_dir),
            (parse_model(s["model"]) or "-", by_model),
        ):
            e = bucket.setdefault(key, {"key": key, "sessions": 0, "cost": 0.0, "tokens": 0, "messages": 0})
            e["sessions"] += 1
            e["cost"] += cost
            e["tokens"] += tok
        total["cost"] += cost
        total["tokens"] += tok

    # 消息按"消息发生日"统计：跨天的长会话也要落到每一天
    for day, n in per_day_msgs.items():
        e = by_day.setdefault(day, {"key": day, "sessions": 0, "cost": 0.0, "tokens": 0, "messages": 0})
        e["messages"] = n
    total["messages"] = sum(per_day_msgs.values())

    return {
        "totals": {
            "sessions": total["sessions"],
            "cost": round(total["cost"], 4),
            "tokens": total["tokens"],
            "messages": total["messages"],
        },
        "by_day": sorted(by_day.values(), key=lambda x: x["key"]),
        "by_dir": sorted(by_dir.values(), key=lambda x: -x["cost"]),
        "by_model": sorted(by_model.values(), key=lambda x: -x["cost"]),
    }, 200


def lookup_part(src, pid):
    """按 part id 取出该 part 的 data JSON 字符串。

    V1 直接查 part 表；V2 的 part id 是合成的（形如 <msg_id>_v2pN / _tN / _fN），
    需要回退到所属消息的 session_message 行重新还原。
    """
    row = src.execute("SELECT data FROM part WHERE id=?", (pid,)).fetchone()
    if row:
        return row["data"]
    # V2 合成 id：前缀即消息 id
    mid = None
    for suffix in ("_v2p", "_t", "_f", "_c"):
        i = pid.rfind(suffix)
        if i > 0:
            mid = pid[:i]
            break
    if not mid:
        return None
    m = src.execute(
        "SELECT id, session_id, type, time_created, data FROM session_message WHERE id=?",
        (mid,),
    ).fetchone()
    if not m:
        return None
    try:
        d = json.loads(m["data"])
    except (TypeError, ValueError):
        return None
    created = (d.get("time") or {}).get("created") or m["time_created"]
    if m["type"] == "user" and pid.endswith("_t0"):
        return json.dumps({"type": "text", "text": d.get("text") or ""},
                          ensure_ascii=False)
    if m["type"] == "user":
        for i, f in enumerate(d.get("files") or []):
            if pid == "%s_f%d" % (mid, i) and isinstance(f, dict):
                return json.dumps({
                    "type": "file",
                    "filename": f.get("filename") or f.get("name") or "",
                    "mime": f.get("mime") or "",
                    "url": f.get("url") or "",
                }, ensure_ascii=False)
        return None
    if m["type"] == "assistant":
        for p in v2_content_to_parts(mid, m["session_id"], d.get("content"), created):
            if p["id"] == pid:
                return json.dumps(
                    {k: v for k, v in p.items()
                     if k not in ("id", "message_id", "session_id",
                                  "time_created", "time_updated")},
                    ensure_ascii=False, default=str)
        return None
    if m["type"] in ("compaction", "system", "synthetic") and pid.endswith("_c"):
        txt = d.get("summary") or d.get("text") or d.get("description") or ""
        return json.dumps({"type": "text", "text": str(txt)}, ensure_ascii=False)
    return None


def _part_output(src, pid):
    raw = lookup_part(src, pid)
    if raw is None:
        return None, None, None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None, None, None
    st = data.get("state") or {}
    md = st.get("metadata") or {}
    out = (md.get("output") if isinstance(md, dict) else "") or st.get("output") or ""
    path = md.get("outputPath") if isinstance(md, dict) else None
    if not path and isinstance(out, str):
        m = TOOL_POINTER.search(out)
        if m:
            path = m.group(1).strip().strip('"')
    return data, out, path


def api_tool_output(params):
    pid = first_arg(params, "part_id", "")
    if not pid:
        return {"error": "missing part_id"}, 400
    src = open_ro()
    try:
        _data, _out, path = _part_output(src, pid)
    finally:
        src.close()
    if not path:
        return {"error": "no full output"}, 404
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(TOOL_OUTPUT_DIR)):
        return {"error": "forbidden"}, 403
    if not os.path.isfile(real):
        return {"error": "file missing", "path": real}, 404
    size = os.path.getsize(real)
    if size > TOOL_FILE_LIMIT:
        return {"error": "too large", "size": size}, 413
    with io.open(real, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return {"path": real, "size": size, "text": text}, 200


def api_file(params):
    pid = first_arg(params, "part_id", "")
    if not pid:
        return {"error": "missing part_id"}, 400
    src = open_ro()
    try:
        raw = lookup_part(src, pid)
    finally:
        src.close()
    if raw is None:
        return {"error": "not found"}, 404
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {"error": "bad part"}, 400
    url = str(data.get("url") or "")
    if not url.startswith("data:"):
        return {"error": "not inline"}, 404
    try:
        head, payload = url.split(",", 1)
        mime = head[5:].split(";")[0] or "application/octet-stream"
        raw = base64.b64decode(payload)
    except Exception:  # noqa: BLE001
        return {"error": "decode failed"}, 500
    return {"__raw__": raw, "__mime__": mime}, 200


def _render_session_v2(src, sid, s, fmt, include, roles, frm, to):
    """V2 会话的导出渲染（json / md / txt），输出格式与 V1 保持一致。"""
    where, args = ["session_id = ?"], [sid]
    if frm:
        where.append("time_created >= ?")
        args.append(frm)
    if to:
        where.append("time_created <= ?")
        args.append(to)
    if roles:
        where.append("type IN (%s)" % ",".join("?" * len(roles)))
        args += sorted(roles)
    else:
        where.append("type NOT IN (%s)" % ",".join("'%s'" % t for t in V2_NON_CHAT))
    rows = src.execute(
        "SELECT id,type,seq,time_created,data FROM session_message WHERE "
        + " AND ".join(where) + " ORDER BY time_created, seq", tuple(args)
    ).fetchall()
    if not rows:
        return ""

    # 复用 V1 的 part 抽取口径：先还原成 part dict，再走 extract_part
    msgs, grouped = [], {}
    for r in rows:
        try:
            d = json.loads(r["data"])
        except (TypeError, ValueError):
            d = {}
        role = v2_msg_to_role(d, r["type"])
        msgs.append({"id": r["id"], "time_created": r["time_created"], "role": role})
        raw_parts = []
        if r["type"] == "user":
            if (d.get("text") or "").strip():
                raw_parts.append({"type": "text", "text": d["text"]})
            for f in d.get("files") or []:
                if isinstance(f, dict):
                    raw_parts.append({"type": "file",
                                      "filename": f.get("filename") or f.get("name") or "",
                                      "mime": f.get("mime") or "",
                                      "url": f.get("url") or ""})
        elif r["type"] == "assistant":
            raw_parts = d.get("content") or []
        elif r["type"] in ("compaction", "system", "synthetic"):
            txt = d.get("summary") or d.get("text") or d.get("description") or ""
            if txt:
                raw_parts = [{"type": "text", "text": str(txt)}]
        for p in raw_parts:
            if not isinstance(p, dict):
                continue
            if part_kind(p) not in include:
                continue
            item = extract_part(p)
            if item:
                grouped.setdefault(r["id"], []).append(item)

    if fmt == "json":
        return json.dumps(
            {
                "session": {"id": sid, "title": s.get("title"),
                            "directory": s.get("directory"), "storage": "v2"},
                "messages": [
                    {"time": m["time_created"], "role": m["role"],
                     "parts": grouped.get(m["id"], [])}
                    for m in msgs
                ],
            },
            ensure_ascii=False, indent=1,
        )

    out = [
        "# %s" % (s.get("title") or t("server.untitled")),
        "",
        t("server.export_session_id", id=sid),
        t("server.export_directory", dir=(s.get("directory") or "")),
        t("server.export_model", model=parse_model(s.get("model"))),
        t("server.export_time", **{"from": ms_str(s.get("time_created")), "to": ms_str(s.get("time_updated"))}),
        "",
    ]
    for m in msgs:
        role = m["role"]
        label = t("server.role_user") if role == "user" else (t("server.role_assistant") if role == "assistant" else role)
        out.append("## %s · %s" % (label, ms_str(m["time_created"])))
        out.append("")
        for p in grouped.get(m["id"], []):
            if p["t"] in ("text", "reasoning"):
                if p["t"] == "reasoning" and fmt == "txt":
                    continue
                out.append(("> " if p["t"] == "reasoning" else "") + p["text"].strip())
                out.append("")
            elif p["t"] == "tool":
                if fmt == "txt":
                    out.append(t("server.tool_txt", name=p["tool"], title=p["title"]))
                    if p.get("output"):
                        out.append(p["output"][:2000])
                    out.append("")
                else:
                    out.append("<details><summary>" + t("server.tool_summary", name=p["tool"], title=p["title"]) + "</summary>")
                    out.append("")
                    if p.get("input") is not None:
                        out.append("```json")
                        out.append(json.dumps(p["input"], ensure_ascii=False, indent=1)[:2000])
                        out.append("```")
                    if p.get("output"):
                        out.append("```")
                        out.append(p["output"][:2000])
                        out.append("```")
                    out.append("</details>")
                    out.append("")
            elif p["t"] == "file":
                out.append(t("server.attachment", name=p.get("name")))
                out.append("")
            elif p["t"] == "marker":
                out.append("_%s_" % p.get("label"))
                out.append("")
    return "\n".join(out)


def render_session(src, sid, s, fmt, include, roles, frm, to):
    is_v2 = session_source(src, sid) == "v2"
    if is_v2:
        return _render_session_v2(src, sid, s, fmt, include, roles, frm, to)

    where, args = ["session_id = ?"], [sid]
    if frm:
        where.append("time_created >= ?")
        args.append(frm)
    if to:
        where.append("time_created <= ?")
        args.append(to)
    if roles:
        where.append("json_extract(data,'$.role') IN (%s)" % ",".join("?" * len(roles)))
        args += sorted(roles)
    msgs = src.execute(
        "SELECT id,time_created,data FROM message WHERE " + " AND ".join(where) + " ORDER BY time_created, id",
        tuple(args),
    ).fetchall()
    if not msgs:
        return ""

    ids = [m["id"] for m in msgs]
    grouped = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        q = ",".join("?" * len(chunk))
        for p in src.execute(
            "SELECT message_id,data FROM part WHERE message_id IN (%s) ORDER BY time_created, id" % q,
            tuple(chunk),
        ):
            try:
                data = json.loads(p["data"])
            except (TypeError, ValueError):
                continue
            if part_kind(data) not in include:
                continue
            item = extract_part(data)
            if item:
                grouped.setdefault(p["message_id"], []).append(item)

    if fmt == "json":
        return json.dumps(
            {
                "session": {"id": sid, "title": s.get("title"), "directory": s.get("directory")},
                "messages": [
                    {
                        "time": m["time_created"],
                        "role": json.loads(m["data"]).get("role"),
                        "parts": grouped.get(m["id"], []),
                    }
                    for m in msgs
                ],
            },
            ensure_ascii=False,
            indent=1,
        )

    out = [
        "# %s" % (s.get("title") or t("server.untitled")),
        "",
        t("server.export_session_id", id=sid),
        t("server.export_directory", dir=(s.get("directory") or "")),
        t("server.export_model", model=parse_model(s.get("model"))),
        t("server.export_time", **{"from": ms_str(s.get("time_created")), "to": ms_str(s.get("time_updated"))}),
        "",
    ]
    for m in msgs:
        role = json.loads(m["data"]).get("role")
        label = t("server.role_user") if role == "user" else (t("server.role_assistant") if role == "assistant" else role)
        out.append("## %s · %s" % (label, ms_str(m["time_created"])))
        out.append("")
        for p in grouped.get(m["id"], []):
            if p["t"] in ("text", "reasoning"):
                if p["t"] == "reasoning" and fmt == "txt":
                    continue
                out.append(("> " if p["t"] == "reasoning" else "") + p["text"].strip())
                out.append("")
            elif p["t"] == "tool":
                if fmt == "txt":
                    out.append(t("server.tool_txt", name=p["tool"], title=p["title"]))
                    if p.get("output"):
                        out.append(p["output"][:2000])
                    out.append("")
                else:
                    out.append("<details><summary>" + t("server.tool_summary", name=p["tool"], title=p["title"]) + "</summary>")
                    out.append("")
                    if p.get("input") is not None:
                        out.append("```json")
                        out.append(json.dumps(p["input"], ensure_ascii=False, indent=1)[:2000])
                        out.append("```")
                    if p.get("output"):
                        out.append("```")
                        out.append(p["output"][:2000])
                        out.append("```")
                    out.append("</details>")
                    out.append("")
            elif p["t"] == "file":
                out.append(t("server.attachment", name=p.get("name")))
                out.append("")
            elif p["t"] == "marker":
                out.append("_%s_" % p.get("label"))
                out.append("")
    return "\n".join(out)


NOISE_PREFIX = (
    "continue if you have next steps",
    "use the above message and context",
    "called the read tool",
    "called the write tool",
    "reading mcp resource",
    "the user has provided",
    "<command-name>",
    "<system-reminder>",
    "[request interrupted",
)


def is_noise(txt):
    low = txt.strip().lower()
    if len(low) < 6:
        return True
    return any(low.startswith(p) for p in NOISE_PREFIX)


def build_report(src, ids, smeta, frm, to):
    buckets = {}
    for sid in ids:
        if sid not in smeta:
            continue
        is_v2 = smeta[sid].get("storage") == "v2" if isinstance(smeta.get(sid), dict) else False
        if is_v2:
            where, args = ["session_id = ?", "type = 'user'"], [sid]
            if frm:
                where.append("time_created >= ?")
                args.append(frm)
            if to:
                where.append("time_created <= ?")
                args.append(to)
            rows = src.execute(
                "SELECT id,time_created,data FROM session_message WHERE "
                + " AND ".join(where) + " ORDER BY time_created", tuple(args)
            ).fetchall()
            for m in rows:
                try:
                    d = json.loads(m["data"])
                except (TypeError, ValueError):
                    continue
                txt = (d.get("text") or "").strip()
                if txt:
                    buckets.setdefault(day_of(m["time_created"]), []).append(
                        " ".join(txt.split()))
            continue

        where, args = ["session_id = ?", "json_extract(data,'$.role') = 'user'"], [sid]
        if frm:
            where.append("time_created >= ?")
            args.append(frm)
        if to:
            where.append("time_created <= ?")
            args.append(to)
        for m in src.execute(
            "SELECT id,time_created FROM message WHERE " + " AND ".join(where) + " ORDER BY time_created",
            tuple(args),
        ):
            texts = []
            for p in src.execute(
                "SELECT data FROM part WHERE message_id=? ORDER BY time_created, id", (m["id"],)
            ):
                try:
                    d = json.loads(p["data"])
                except (TypeError, ValueError):
                    continue
                if d.get("type") == "text" and (d.get("text") or "").strip():
                    texts.append(d["text"].strip())
            if texts:
                buckets.setdefault(day_of(m["time_created"]), []).append(" ".join(" ".join(texts).split()))

    lines = [
        "# " + t("server.report_title"),
        "",
        t("server.report_generated", at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M")),
        t("server.report_range", **{
            "from": ms_str(frm) or t("server.report_earliest"),
            "to": ms_str(to) or t("server.report_latest"),
        }),
        t("server.report_note"),
        "",
    ]
    n_days = n_tasks = 0
    for day in sorted(buckets):
        items, seen = [], set()
        for txt in buckets[day]:
            if is_noise(txt):
                continue
            key = re.sub(r"\s+", "", txt)[:80]
            if key in seen:
                continue
            seen.add(key)
            txt = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", txt)
            txt = re.sub(r"https?://\S+", "", txt)
            txt = re.sub(r"<[^>]{1,80}>", " ", txt)
            txt = re.sub(r"\s+", " ", txt).strip(" -—·:：")
            if not txt:
                continue
            if len(txt) > 400:
                txt = txt[:400] + "…"
            items.append(txt)
        if not items:
            continue
        lines.append(t("server.report_day", day=day))
        n_days += 1
        for i, txt in enumerate(items, 1):
            lines.append(t("server.report_task", n=i, text=txt))
            n_tasks += 1
        lines.append("")
    return "\n".join(lines), n_days, n_tasks


def api_export(params):
    fmt = (first_arg(params, "format", "md") or "md").lower()
    sid = first_arg(params, "session_id", "") or ""
    frm = to_int(first_arg(params, "from", 0))
    to = to_int(first_arg(params, "to", 0))
    include = csv_set(params, "include", ALL_KINDS) or set(ALL_KINDS)
    roles = csv_set(params, "roles", ("user", "assistant"))
    # 报告默认只看主会话，避免把子代理的内部提示词当成本人任务
    only_main = first_arg(params, "main", "1") != "0"

    src = open_ro()
    try:
        smeta = {r["id"]: dict(r) for r in read_all_sessions(src)}
        # 给每条会话标注存储来源，供 build_report / render_session 分流
        for i, m in smeta.items():
            m["storage"] = session_source(src, i)
        if sid:
            ids = [sid]
        else:
            # 按"消息活动时间"选会话：长会话的创建时间可能在区间之外，但消息落在区间内
            where, args = [], []
            if frm:
                where.append("time_created >= ?")
                args.append(frm)
            if to:
                where.append("time_created <= ?")
                args.append(to)
            if where:
                w = " AND ".join(where)
                found = {r["sid"] for r in src.execute(
                    "SELECT DISTINCT session_id sid FROM message WHERE " + w, tuple(args))}
                if v2_available(src):
                    found |= {r["sid"] for r in src.execute(
                        "SELECT DISTINCT session_id sid FROM session_message WHERE " + w,
                        tuple(args))}
                ids = list(found)
            else:
                ids = [r["id"] for r in read_all_sessions(src)]
            if only_main and fmt == "report":
                ids = [i for i in ids if not (smeta.get(i) or {}).get("parent_id")]
            ids = [i for i in ids if i in smeta]
            ids.sort(key=lambda i: smeta[i]["time_created"])
        if fmt == "report":
            body, n_days, n_tasks = build_report(src, ids, smeta, frm, to)
            if first_arg(params, "save", "0") == "1":
                os.makedirs(REPORT_DIR, exist_ok=True)
                name = t("server.report_filename", stamp=datetime.datetime.now().strftime("%Y%m%d_%H%M"))
                path = os.path.join(REPORT_DIR, name)
                with io.open(path, "w", encoding="utf-8") as f:
                    f.write(body)
                return {
                    "path": path,
                    "size": len(body.encode("utf-8")),
                    "days": n_days,
                    "tasks": n_tasks,
                    "preview": body[:800],
                }, 200
            return {
                "__text__": body,
                "__mime__": "text/markdown; charset=utf-8",
                "__filename__": t("server.report_filename", stamp=datetime.date.today().isoformat()),
            }, 200
        chunks = [
            render_session(src, sid_, smeta[sid_], fmt, include, roles, frm, to)
            for sid_ in ids
            if sid_ in smeta
        ]
        if fmt == "json":
            body = json.dumps([json.loads(c) for c in chunks if c], ensure_ascii=False, indent=1)
        else:
            body = "\n\n".join(c for c in chunks if c)
    finally:
        src.close()

    if fmt == "json":
        mime, ext = "application/json; charset=utf-8", "json"
    elif fmt == "txt":
        mime, ext = "text/plain; charset=utf-8", "txt"
    else:
        mime, ext = "text/markdown; charset=utf-8", "md"
    return {
        "__text__": body,
        "__mime__": mime,
        "__filename__": "opencode_export_%s.%s" % (datetime.date.today().isoformat(), ext),
    }, 200


class Handler(BaseHTTPRequestHandler):
    server_version = "SessionExplorer/" + VERSION

    def log_message(self, fmt, *args):
        if "--verbose" in sys.argv:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, body, mime="application/json; charset=utf-8", code=200, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = {"Cache-Control": "no-store", "Content-Type": mime}
        if extra:
            headers.update(extra)
        if "gzip" in (self.headers.get("Accept-Encoding") or "") and len(body) > 1024 and not mime.startswith("image/"):
            body = gzip.compress(body, 5)
            headers["Content-Encoding"] = "gzip"
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        if isinstance(obj, tuple):
            obj, code = obj
        if isinstance(obj, dict) and "__raw__" in obj:
            return self._send(obj["__raw__"], obj["__mime__"])
        if isinstance(obj, dict) and "__text__" in obj:
            extra = {}
            if obj.get("__filename__"):
                extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(obj["__filename__"])
            return self._send(obj["__text__"], obj["__mime__"], code, extra)
        self._send(json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":")), code=code)

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT)

    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for chunk in raw.split(";"):
            k, _, v = chunk.strip().partition("=")
            if k == "se_token":
                return unquote(v)
        return None

    def _auth_ok(self, params):
        token = CONFIG.get("token") or ""
        if not token:
            return True
        given = (
            (first_arg(params, "token", "") or "")
            or self.headers.get("X-Session-Token")
            or self._cookie_token()
            or ""
        )
        return hmac.compare_digest(given, token)

    def _wants_html(self):
        if urlparse(self.path).path not in ("/", "/index.html"):
            return False
        return "text/html" in (self.headers.get("Accept") or "")

    def _fail_key(self):
        return self.client_address[0] if self.client_address else "-"

    def _login_blocked(self):
        return _login_fails.get(self._fail_key(), 0) > time.time()

    def _login_fail(self):
        key = self._fail_key()
        n = _login_fails.get(key + "#n", 0) + 1
        if n >= 5:
            _login_fails[key] = time.time() + min(300, 10 * (n - 4))
            n = 0
        _login_fails[key + "#n"] = n

    def _redirect(self, location, extra=None):
        self.send_response(303)
        self.send_header("Location", location)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _lang(self):
        try:
            raw = (first_arg(parse_qs(urlparse(self.path).query), "lang", "") or "").strip().lower()
        except (ValueError, TypeError):
            raw = ""
        if raw in SUPPORTED_LANGS:
            return raw
        if raw:
            return "zh" if raw.startswith("zh") else "en"
        return LANG

    def _serve_login(self, error=""):
        lang = self._lang()
        try:
            with io.open(LOGIN_HTML, "r", encoding="utf-8") as f:
                page = f.read()
        except (IOError, OSError):
            page = (
                '<meta charset=utf-8><h1>__LOGIN_TITLE__</h1>'
                '<form method=post action=/login><input name=token type=password>'
                '<button>__LOGIN_SUBMIT__</button></form><p>__ERROR__</p>'
            )
        page = page.replace("__LOGIN_TITLE__", t("server.login_title"))
        page = page.replace("__LOGIN_SUBMIT__", t("server.login_submit"))
        page = inject_i18n(page.replace("__ERROR__", error), lang)
        return self._send(page, "text/html; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        params = parse_qs(u.query)

        if not self._origin_ok():
            return self.send_json({"error": "origin not allowed"}, 403)

        if u.path == "/login":
            if not (CONFIG.get("token") or ""):
                return self._redirect("/")
            return self._serve_login()
        if u.path == "/logout":
            return self._redirect(
                "/login", {"Set-Cookie": "se_token=; Path=/; Max-Age=0; SameSite=Strict; HttpOnly"}
            )
        if u.path == "/i18n.js":
            try:
                with io.open(I18N_JS, "rb") as f:
                    return self._send(f.read(), "application/javascript; charset=utf-8")
            except (IOError, OSError):
                return self.send_json({"error": "i18n.js not found"}, 404)

        if not self._auth_ok(params):
            if self._wants_html():
                return self._redirect("/login")
            return self.send_json({"error": "unauthorized", "login": "/login"}, 401)

        try:
            if u.path in ("/", "/index.html"):
                with io.open(INDEX_HTML, "r", encoding="utf-8") as f:
                    body = inject_i18n(f.read(), self._lang())
                # 带正确令牌访问首页时顺手下发 Cookie，免去手动登录
                extra = None
                qtok = first_arg(params, "token", "") or ""
                if qtok and hmac.compare_digest(qtok, CONFIG.get("token") or ""):
                    extra = {
                        "Set-Cookie": "se_token=%s; Path=/; SameSite=Strict; HttpOnly"
                        % quote(qtok, safe="")
                    }
                return self._send(body, "text/html; charset=utf-8", 200, extra)
            routes = {
                "/api/health": api_health,
                "/api/sessions": api_sessions,
                "/api/session": api_session,
                "/api/parts": api_parts,
                "/api/search": api_search,
                "/api/index": api_index,
                "/api/stats": api_stats,
                "/api/tool-output": api_tool_output,
                "/api/file": api_file,
                "/api/export": api_export,
            }
            fn = routes.get(u.path)
            if not fn:
                return self.send_json({"error": "not found"}, 404)
            return self.send_json(fn(params))
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            try:
                return self.send_json({"error": str(e)}, 500)
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self):
        u = urlparse(self.path)
        if not self._origin_ok():
            return self.send_json({"error": "origin not allowed"}, 403)
        if u.path != "/login":
            return self.send_json({"error": "not found"}, 404)

        token = CONFIG.get("token") or ""
        if not token:
            return self._redirect("/")
        if self._login_blocked():
            return self._serve_login("尝试次数过多，请稍后再试。")

        try:
            length = min(int(self.headers.get("Content-Length") or 0), 8192)
        except ValueError:
            length = 0
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        given = (parse_qs(body).get("token") or [""])[0]

        if given and hmac.compare_digest(given, token):
            _login_fails.pop(self._fail_key(), None)
            _login_fails.pop(self._fail_key() + "#n", None)
            cookie = "se_token=%s; Path=/; SameSite=Strict; HttpOnly" % quote(token, safe="")
            return self._redirect("/", {"Set-Cookie": cookie})
        self._login_fail()
        return self._serve_login("令牌不正确。")


def port_in_use():
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def handle_token_args(argv):
    """令牌管理：--token 生成 / --set-token xxx 指定 / --no-token 关闭 / --show-token 查看。"""
    cfg = load_config()
    if "--no-token" in argv:
        cfg["token"] = ""
        save_config(cfg)
        print("已关闭访问令牌校验（重启服务后生效）。")
        return True
    if "--show-token" in argv:
        tok = cfg.get("token") or ""
        print("当前令牌：%s" % (tok if tok else "(未启用)"))
        return True
    if "--set-token" in argv:
        i = argv.index("--set-token")
        if i + 1 >= len(argv):
            print("用法：python server.py --set-token <令牌>")
            return True
        cfg["token"] = argv[i + 1]
        save_config(cfg)
        print("已设置令牌：%s" % cfg["token"])
        print("登录地址：http://%s:%d/login" % (HOST, PORT))
        return True
    if "--token" in argv:
        cfg["token"] = secrets.token_urlsafe(18)
        save_config(cfg)
        print("已生成并启用访问令牌：")
        print("    %s" % cfg["token"])
        print("登录地址：http://%s:%d/login" % (HOST, PORT))
        print("（令牌也保存在 %s）" % CONFIG_PATH)
        return True
    return False


def main():
    if handle_token_args(sys.argv):
        return 0
    if not os.path.exists(DB):
        print("opencode.db not found: %s" % DB)
        return 1
    if "--index" in sys.argv:
        print("indexing…", sync_index(rebuild="--rebuild" in sys.argv))
        return 0
    if port_in_use():
        print("Session Explorer already running: http://%s:%d/" % (HOST, PORT))
        return 0
    # 启动时先把侧车索引初始化好，避免首个请求承担初始化开销
    try:
        with index_db():
            pass
    except sqlite3.Error as e:
        print("索引初始化失败（将按需重建）：%s" % e)
    try:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("port %d busy: %s" % (PORT, e))
        return 0
    print("Session Explorer v%s: http://%s:%d/  (db: %s)" % (VERSION, HOST, PORT, DB))
    if CONFIG.get("token"):
        print("访问令牌已启用，请先登录：http://%s:%d/login" % (HOST, PORT))
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
