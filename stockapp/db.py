"""SQLite 저장소: 가격 캐시, 보유·제외 종목, 수동 점수, 스캔 결과, 설정."""
import json
import os
import struct
import sqlite3
import threading
import zlib
from contextlib import contextmanager
from datetime import datetime

DATA_DIR = os.environ.get("STOCKAPP_DATA", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
DB_PATH = os.path.join(DATA_DIR, os.environ.get("STOCKAPP_DB", "stock.db"))
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices(date TEXT, ticker TEXT, open REAL, high REAL, low REAL, close REAL,
    volume REAL, value REAL, PRIMARY KEY(date, ticker));
CREATE TABLE IF NOT EXISTS price_days(date TEXT PRIMARY KEY, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS kv_cache(key TEXT PRIMARY KEY, value BLOB, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS holdings(ticker TEXT PRIMARY KEY, name TEXT, buy_price REAL, qty REAL,
    buy_date TEXT, memo TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS exclusions(ticker TEXT PRIMARY KEY, name TEXT, reason TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS manual_scores(ticker TEXT, item TEXT, score REAL, value TEXT, note TEXT,
    updated_at TEXT, PRIMARY KEY(ticker, item));
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
    base_date TEXT, profile TEXT, status TEXT, message TEXT, n_scored INTEGER, demo INTEGER DEFAULT 0,
    env TEXT, warnings TEXT, trigger TEXT);
CREATE TABLE IF NOT EXISTS scores(scan_id INTEGER, ticker TEXT, name TEXT, market TEXT, sector TEXT,
    close REAL, chg REAL, mcap REAL, s_short REAL, s_mid REAL, s_long REAL, base REAL, bonus REAL,
    penalty REAL, final REAL, grade TEXT, coverage REAL, core_avg REAL, knockout TEXT, gate TEXT,
    target REAL, stop REAL, detail BLOB, PRIMARY KEY(scan_id, ticker));
CREATE INDEX IF NOT EXISTS ix_scores_ticker ON scores(ticker, scan_id);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""

DEFAULTS = {
    "scan_time": "18:30",          # 평일 자동 스캔 시각 (한국 시간)
    "scan_enabled": "1",
    "profile": "C",
    "k7_value_eok": "10",          # K7 20일 평균 거래대금 하한(억)
    "k8_mcap_eok": "300",          # K8 시가총액 하한(억)
    "gate_s_cov": "0.80",
    "gate_a_cov": "0.70",
    "history_days": "300",         # 가격 캐시 거래일 수
    "krx_id": "",
    "krx_pw": "",
    "dart_key": "",
    "keep_detail_scans": "60",
}


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    with _lock:
        c = sqlite3.connect(DB_PATH, timeout=60)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        for k, v in DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
    repair_numeric_blobs()


# ── 설정 ──
def settings():
    with conn() as c:
        st = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM settings")}
    for k, v in DEFAULTS.items():
        st.setdefault(k, v)
    # 환경변수가 있으면 우선
    for k, env in (("krx_id", "KRX_ID"), ("krx_pw", "KRX_PW"), ("dart_key", "DART_API_KEY")):
        if os.environ.get(env):
            st[k] = os.environ[env]
    return st


def scoring_settings(st=None):
    st = st or settings()
    return {"profile": st["profile"], "k7_value_eok": float(st["k7_value_eok"]),
            "k8_mcap_eok": float(st["k8_mcap_eok"]), "gate_s_cov": float(st["gate_s_cov"]),
            "gate_a_cov": float(st["gate_a_cov"])}


def save_settings(d):
    with conn() as c:
        for k, v in d.items():
            if k in DEFAULTS:
                c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (k, str(v)))


# ── 캐시 ──
def cache_get(key):
    with conn() as c:
        r = c.execute("SELECT value, fetched_at FROM kv_cache WHERE key=?", (key,)).fetchone()
    if not r:
        return None, None
    return json.loads(zlib.decompress(r["value"])), r["fetched_at"]


def cache_put(key, value):
    blob = zlib.compress(json.dumps(value, ensure_ascii=False, default=float).encode())
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO kv_cache(key, value, fetched_at) VALUES(?, ?, ?)", (key, blob, now()))


# ── 보유·제외 ──
def holdings():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM holdings ORDER BY added_at")]


def exclusions():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM exclusions ORDER BY added_at")]


def set_holding(ticker, name="", buy_price=None, qty=None, buy_date=None, memo=""):
    with conn() as c:
        c.execute("""INSERT INTO holdings(ticker, name, buy_price, qty, buy_date, memo, added_at)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET name=excluded.name,
            buy_price=excluded.buy_price, qty=excluded.qty, buy_date=excluded.buy_date, memo=excluded.memo""",
                  (ticker, name, buy_price, qty, buy_date, memo, now()))


def del_holding(ticker):
    with conn() as c:
        c.execute("DELETE FROM holdings WHERE ticker=?", (ticker,))


def set_exclusion(ticker, name="", reason=""):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO exclusions(ticker, name, reason, added_at) VALUES(?,?,?,?)",
                  (ticker, name, reason, now()))


def del_exclusion(ticker):
    with conn() as c:
        c.execute("DELETE FROM exclusions WHERE ticker=?", (ticker,))


# ── 수동 점수 ──
def manual_scores():
    out = {}
    with conn() as c:
        for r in c.execute("SELECT * FROM manual_scores"):
            out.setdefault(r["ticker"], {})[r["item"]] = dict(r)
    return out


def set_manual(ticker, item, score, value="", note=""):
    with conn() as c:
        if score is None or score == "":
            c.execute("DELETE FROM manual_scores WHERE ticker=? AND item=?", (ticker, item))
        else:
            c.execute("INSERT OR REPLACE INTO manual_scores VALUES(?,?,?,?,?,?)",
                      (ticker, item, float(score), value, note, now()))


# ── 스캔 ──
def scan_start(profile, trigger, demo=False):
    with conn() as c:
        cur = c.execute("INSERT INTO scans(started_at, profile, status, message, demo, trigger) VALUES(?,?,?,?,?,?)",
                        (now(), profile, "running", "시작", int(demo), trigger))
        return cur.lastrowid


def scan_update(scan_id, **kw):
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    with conn() as c:
        c.execute(f"UPDATE scans SET {cols} WHERE id=?", (*kw.values(), scan_id))


def scans(limit=50):
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,))]


def scan(scan_id):
    with conn() as c:
        r = c.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
    return dict(r) if r else None


def latest_scan(done_only=True):
    with conn() as c:
        q = "SELECT * FROM scans WHERE status='done' ORDER BY id DESC LIMIT 1" if done_only else \
            "SELECT * FROM scans ORDER BY id DESC LIMIT 1"
        r = c.execute(q).fetchone()
    return dict(r) if r else None


def prev_scan(scan_id):
    with conn() as c:
        r = c.execute("SELECT * FROM scans WHERE status='done' AND id<? ORDER BY id DESC LIMIT 1", (scan_id,)).fetchone()
    return dict(r) if r else None


NUM_COLS = ("close", "chg", "mcap", "s_short", "s_mid", "s_long", "base", "bonus", "penalty", "final",
            "coverage", "core_avg", "target", "stop")


def _num(x):
    """numpy 정수·실수를 파이썬 float로 바꾼다. numpy int64를 그대로 넣으면 SQLite에 bytes로 저장된다."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else v  # NaN → None


def _from_blob(v):
    """예전 버전이 bytes로 저장한 숫자를 복원한다 (8바이트 little-endian int64)."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        if len(b) == 8:
            return float(struct.unpack("<q", b)[0])
        if len(b) == 4:
            return float(struct.unpack("<i", b)[0])
        return None
    return v


def _fix_row(d):
    for k in NUM_COLS:
        if k in d:
            d[k] = _from_blob(d[k])
    return d


def repair_numeric_blobs():
    """이미 저장된 scores 행 중 bytes로 들어간 숫자를 고친다 (재스캔 불필요)."""
    with conn() as c:
        for col in NUM_COLS:
            rows = c.execute(f"SELECT rowid, {col} FROM scores WHERE typeof({col})='blob'").fetchall()
            c.executemany(f"UPDATE scores SET {col}=? WHERE rowid=?", [(_from_blob(r[1]), r[0]) for r in rows])


def save_scores(scan_id, results):
    rows = []
    for r in results:
        detail = {
            "items": {k: [v.score, v.value, v.basis, v.est, v.src] for k, v in r["items"].items()},
            "persp": r["persp"], "cov": r["cov"], "adj": r["adj"], "gate": r["gate"],
            "knockout": r["knockout"], "reliability": r["reliability"], "grade_label": r["grade_label"],
        }
        n = _num
        rows.append((scan_id, str(r["ticker"]), str(r["name"]), r["market"], r["sector"], n(r["close"]), n(r["chg"]),
                     n(r["mcap"]), n(r["persp"]["단기"]), n(r["persp"]["중기"]), n(r["persp"]["장기"]), n(r["base"]),
                     n(r["bonus"]), n(r["penalty"]), n(r["final"]), r["grade"], n(r["coverage"]), n(r["core_avg"]),
                     " / ".join(r["knockout"]), " / ".join(r["gate"]), n(r["target"]), n(r["stop"]),
                     zlib.compress(json.dumps(detail, ensure_ascii=False, default=float).encode())))
    with conn() as c:
        c.executemany(f"INSERT OR REPLACE INTO scores VALUES({','.join('?' * 23)})", rows)


def scores(scan_id):
    with conn() as c:
        return [_fix_row(dict(r)) for r in c.execute(
            "SELECT * FROM scores WHERE scan_id=? ORDER BY (grade='X'), final DESC, core_avg DESC", (scan_id,))]


def score_detail(scan_id, ticker):
    with conn() as c:
        r = c.execute("SELECT * FROM scores WHERE scan_id=? AND ticker=?", (scan_id, ticker)).fetchone()
    if not r:
        return None
    d = _fix_row(dict(r))
    d["detail"] = json.loads(zlib.decompress(d["detail"])) if d["detail"] else None
    return d


def score_history(ticker, limit=60):
    with conn() as c:
        return [_fix_row(dict(r)) for r in c.execute(
            """SELECT s.base_date, s.id, sc.final, sc.grade, sc.s_short, sc.s_mid, sc.s_long, sc.close
               FROM scores sc JOIN scans s ON s.id=sc.scan_id
               WHERE sc.ticker=? AND s.status='done' ORDER BY s.id DESC LIMIT ?""", (ticker, limit))][::-1]


def prune_details(keep):
    with conn() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM scans WHERE status='done' ORDER BY id DESC")]
        old = ids[int(keep):]
        if old:
            c.execute(f"UPDATE scores SET detail=NULL WHERE scan_id IN ({','.join('?'*len(old))})", old)


def stock_names():
    """최근 스캔 기준 종목명 목록 (검색용)."""
    s = latest_scan()
    if not s:
        return []
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT ticker, name, market FROM scores WHERE scan_id=?", (s["id"],))]
