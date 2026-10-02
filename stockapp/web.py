"""웹 서버 (Flask) + 매일 자동 스캔 스케줄러."""
import json
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

from . import db, scanner
from .framework import BONUSES, ITEM_MAP, ITEMS, KNOCKOUTS, PENALTIES, PERSPECTIVES, PROFILES

app = Flask(__name__)
sched = BackgroundScheduler(timezone="Asia/Seoul")

GRADE_INFO = {
    "S": ("S", "투자 선정", "90점 이상"),
    "A": ("A", "투자 가능", "80~89점"),
    "B": ("B", "관찰", "70~79점"),
    "C": ("C", "제외(점수 미달)", "70점 미만"),
    "X": ("제외", "제외 필터 해당", "K1~K10"),
}


def schedule():
    st = db.settings()
    for j in sched.get_jobs():
        j.remove()
    if st.get("scan_enabled") == "1":
        hh, mm = (st.get("scan_time") or "18:30").split(":")
        sched.add_job(lambda: scanner.run_scan("schedule"), "cron", day_of_week="mon-fri",
                      hour=int(hh), minute=int(mm), id="daily", misfire_grace_time=3600, coalesce=True)


def next_run():
    j = sched.get_job("daily")
    return j.next_run_time.strftime("%Y-%m-%d %H:%M") if j and j.next_run_time else "꺼짐"


@app.context_processor
def common():
    return {"profiles": PROFILES, "grade_info": GRADE_INFO, "running": scanner.is_running(),
            "next_run": next_run(), "now": datetime.now()}


def _fmt_date(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:]}" if d and len(d) == 8 else (d or "-")


app.jinja_env.filters["d8"] = _fmt_date


@app.template_filter("num")
def num(x, d=0):
    if x is None:
        return "-"
    try:
        return f"{float(x):,.{d}f}"
    except (TypeError, ValueError):
        return str(x)


# ───────── 화면 ─────────

@app.route("/")
def dashboard():
    scan = db.latest_scan()
    last = db.latest_scan(done_only=False)
    rows, prev_map, env, warnings = [], {}, {}, []
    if scan:
        rows = db.scores(scan["id"])
        p = db.prev_scan(scan["id"])
        if p:
            prev_map = {r["ticker"]: r["final"] for r in db.scores(p["id"])}
        meta = json.loads(scan["env"] or "{}")
        env = meta.get("env", {})
        warnings = json.loads(scan["warnings"] or "[]")
    holds = {h["ticker"]: h for h in db.holdings()}
    excl = {e["ticker"] for e in db.exclusions()}
    slim = []
    for r in rows:
        prev = prev_map.get(r["ticker"])
        slim.append({
            "t": r["ticker"], "n": r["name"], "m": r["market"], "sec": r["sector"], "c": r["close"], "chg": r["chg"],
            "cap": (r["mcap"] or 0) / 1e8, "s": r["s_short"], "mi": r["s_mid"], "l": r["s_long"], "f": r["final"],
            "g": r["grade"], "cov": r["coverage"], "d": None if prev is None else r["final"] - prev,
            "ko": r["knockout"], "gate": r["gate"], "h": r["ticker"] in holds,
        })
    counts = {g: sum(1 for r in rows if r["grade"] == g) for g in GRADE_INFO}
    hold_rows = []
    score_map = {r["ticker"]: r for r in rows}
    for tk, h in holds.items():
        r = score_map.get(tk)
        alerts = []
        if r:
            if r["grade"] in ("C", "X"):
                alerts.append("등급 C 이하" if r["grade"] == "C" else "제외 필터 해당")
            if r["stop"] and r["close"] <= r["stop"]:
                alerts.append("손절가 이하")
            pv = prev_map.get(tk)
            if pv is not None and r["final"] - pv <= -5:
                alerts.append(f"점수 {r['final'] - pv:+.1f} 급락")
        ret = ((r["close"] / h["buy_price"] - 1) * 100) if r and h.get("buy_price") else None
        hold_rows.append({"h": h, "r": r, "ret": ret, "alerts": alerts,
                          "d": (r["final"] - prev_map[tk]) if r and tk in prev_map else None})
    return render_template("dashboard.html", scan=scan, last=last, rows_json=json.dumps(slim, ensure_ascii=False),
                           counts=counts, env=env, warnings=warnings, hold_rows=hold_rows, n_excl=len(excl),
                           st=db.settings())


@app.route("/stock/<ticker>")
def stock(ticker):
    scan = db.latest_scan()
    if not scan:
        return redirect(url_for("dashboard"))
    r = db.score_detail(scan["id"], ticker)
    if not r:
        abort(404)
    meta = json.loads(scan["env"] or "{}")
    hist = db.score_history(ticker)
    manual = db.manual_scores().get(ticker, {})
    holding = next((h for h in db.holdings() if h["ticker"] == ticker), None)
    excluded = any(e["ticker"] == ticker for e in db.exclusions())
    groups = {p: [ITEM_MAP[i[0]] for i in ITEMS if i[1] == p] for p in PERSPECTIVES}
    with db.conn() as c:
        px = [dict(x) for x in c.execute(
            "SELECT date, close, high, low, volume FROM prices WHERE ticker=? ORDER BY date DESC LIMIT 260", (ticker,))][::-1]
    b = scanner.LAST["bundle"]
    if not px and b is not None and ticker in b.close.columns:  # 데모 등 DB에 시세가 없는 경우
        s = b.close[ticker].dropna().iloc[-260:]
        px = [{"date": d.strftime("%Y%m%d"), "close": float(v)} for d, v in s.items()]
    return render_template("stock.html", scan=scan, r=r, d=r["detail"], groups=groups, manual=manual, hist=hist,
                           holding=holding, excluded=excluded, sources=meta.get("sources", {}),
                           px_json=json.dumps(px), hist_json=json.dumps(hist), penalties=PENALTIES, bonuses=BONUSES,
                           prof=PROFILES.get(scan["profile"], PROFILES["C"]), can_rescore=scanner.LAST["scan_id"] == scan["id"])


@app.route("/manage")
def manage():
    scan = db.latest_scan()
    smap = {r["ticker"]: r for r in db.scores(scan["id"])} if scan else {}
    return render_template("manage.html", holdings=db.holdings(), exclusions=db.exclusions(), smap=smap,
                           names_json=json.dumps(db.stock_names(), ensure_ascii=False))


@app.route("/scans")
def scans():
    rows = db.scans(60)
    for r in rows:
        r["warn_list"] = json.loads(r["warnings"] or "[]")
    return render_template("scans.html", rows=rows)


@app.route("/criteria", methods=["GET", "POST"])
def criteria():
    st = db.settings()
    p = request.values.get("p") or st["profile"]
    if p not in db.PROFILE_KEYS:
        p = "C"
    if request.method == "POST":
        f = request.form
        d = {}
        for iid, meta in ITEM_MAP.items():
            v = f.get(f"w.{iid}", "").strip()
            if f.get("reset"):
                v = meta["weight"]
            try:
                d[f"p{p}.w.{iid}"] = max(0.0, min(50.0, float(v)))
            except ValueError:
                d[f"p{p}.w.{iid}"] = meta["weight"]
        db.save_raw_settings(d)
        return redirect(url_for("criteria", p=p, saved=1))
    groups = {g: [ITEM_MAP[i[0]] for i in ITEMS if i[1] == g] for g in PERSPECTIVES}
    from .scoring import DISABLED, RULES
    auto = set(RULES) - set(DISABLED)
    return render_template("criteria.html", groups=groups, auto=auto, disabled=DISABLED, knockouts=KNOCKOUTS,
                           penalties=PENALTIES, bonuses=BONUSES, p=p, active=st["profile"],
                           weights=db.profile_weights(p, st), saved=request.args.get("saved"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        f = request.form
        d = {k: f.get(k, "").strip() for k in ("scan_time", "profile", "krx_id", "dart_key")}
        d["scan_enabled"] = "1" if f.get("scan_enabled") else "0"
        if f.get("krx_pw"):
            d["krx_pw"] = f["krx_pw"]
        try:
            datetime.strptime(d["scan_time"], "%H:%M")
        except ValueError:
            d["scan_time"] = "18:30"
        db.save_settings(d)
        raw = {}
        for pk in db.PROFILE_KEYS:
            for k in db.CRITERIA_KEYS:
                v = f.get(f"p{pk}.{k}", "").strip()
                try:
                    raw[f"p{pk}.{k}"] = float(v)
                except ValueError:
                    pass
        db.save_raw_settings(raw)
        schedule()
        return redirect(url_for("settings", saved=1))
    st = db.settings()
    return render_template("settings.html", st=st, crit=db.profile_criteria(st), saved=request.args.get("saved"))


# ───────── API ─────────

def _name(ticker):
    for x in db.stock_names():
        if x["ticker"] == ticker:
            return x["name"]
    return ""


@app.post("/api/holding/toggle")
def api_toggle():
    j = request.get_json(force=True)
    tk = j["ticker"]
    held = any(h["ticker"] == tk for h in db.holdings())
    if held:
        db.del_holding(tk)
    else:
        db.set_holding(tk, j.get("name") or _name(tk))
    return jsonify(ok=True, held=not held)


@app.post("/api/holding")
def api_holding():
    f = request.form
    tk = f["ticker"].strip()
    if f.get("delete"):
        db.del_holding(tk)
    else:
        num_or_none = lambda v: float(v.replace(",", "")) if v and v.strip() else None
        db.set_holding(tk, f.get("name") or _name(tk), num_or_none(f.get("buy_price")), num_or_none(f.get("qty")),
                       f.get("buy_date") or None, f.get("memo", ""))
    return redirect(request.referrer or url_for("manage"))


@app.post("/api/exclusion")
def api_exclusion():
    f = request.form
    tk = f["ticker"].strip()
    if f.get("delete"):
        db.del_exclusion(tk)
    else:
        db.set_exclusion(tk, f.get("name") or _name(tk), f.get("reason", ""))
    return redirect(request.referrer or url_for("manage"))


@app.post("/api/manual")
def api_manual():
    j = request.get_json(force=True)
    tk = j["ticker"]
    for it in j["items"]:
        sc = it.get("score")
        db.set_manual(tk, it["item"], None if sc in (None, "") else float(sc), it.get("value", ""), it.get("note", ""))
    ok = scanner.rescore(tk)
    return jsonify(ok=True, rescored=ok)


@app.post("/api/scan")
def api_scan():
    demo = bool((request.get_json(silent=True) or {}).get("demo"))
    started = scanner.run_async("manual", demo)
    return jsonify(ok=started, message="스캔을 시작했습니다." if started else "이미 스캔이 진행 중입니다.")


@app.post("/api/export")
def api_export():
    """대시보드 순위표에서 현재 검색·정렬된 종목을 파일로 내려준다."""
    from . import export
    j = request.get_json(force=True)
    fmt = j.get("fmt", "csv")
    scan = db.latest_scan()
    if not scan or fmt not in ("csv", "xlsx", "md"):
        abort(400)
    scores = db.scores(scan["id"])
    p = db.prev_scan(scan["id"])
    prev_map = {r["ticker"]: r["final"] for r in db.scores(p["id"])} if p else {}
    held = {h["ticker"] for h in db.holdings()}
    rows = export.build_rows(scores, j.get("tickers") or [], prev_map, held)
    bd = scan["base_date"] or ""
    name = f"종목순위_{bd}"
    if fmt == "csv":
        data, mime = export.to_csv(rows), "text/csv; charset=utf-8"
    elif fmt == "xlsx":
        data, mime = export.to_xlsx(rows), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        title = f"종목 순위 ({bd[:4]}-{bd[4:6]}-{bd[6:]})"
        note = f"프로파일 {PROFILES.get(scan['profile'], {}).get('name', scan['profile'])} · 검색 조건: {j.get('desc') or '전체'} · {len(rows)}종목"
        data, mime = export.to_md(rows, title, note), "text/markdown; charset=utf-8"
    fn = f"{name}.{fmt}"
    return Response(data, mimetype=mime, headers={
        "Content-Disposition": f"attachment; filename=ranking_{bd}.{fmt}; filename*=UTF-8''{quote(fn)}"})


@app.get("/api/scan/status")
def api_scan_status():
    s = db.latest_scan(done_only=False)
    return jsonify(running=scanner.is_running(), scan=s and {k: s[k] for k in ("id", "status", "message", "base_date", "started_at")})


def create_app():
    db.init()
    schedule()
    if not sched.running:
        sched.start()
    return app
