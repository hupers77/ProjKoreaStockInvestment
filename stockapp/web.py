"""웹 서버 (Flask) + 매일 자동 스캔 스케줄러."""
import json
import os
from datetime import datetime, timedelta

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
from urllib.parse import quote

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, url_for

from . import db, scanner
from .framework import BONUSES, ITEM_MAP, ITEMS, KNOCKOUTS, PENALTIES, PERSPECTIVES, PROFILES

app = Flask(__name__)
sched = BackgroundScheduler(timezone="Asia/Seoul")

# 상단 외부 링크와 후원 화면(/support) 주소. SPONSORS_URL 이 비어 있으면 GitHub Sponsors 는 준비 중으로 표시된다.
BLOG_URL = "http://blog.naver.com/hupers"
DOCS_URL = "https://github.com/hupers77/ProjKoreaStockInvestment#readme"
KAKAOPAY_URL = "https://qr.kakaopay.com/Ej9H7kdOT9c404930"
KAKAOPAY_AMOUNT = "5,000원"
SPONSORS_URL = ""

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
        hh, mm = (st.get("scan_time") or "20:30").split(":")
        sched.add_job(lambda: scanner.run_scan("schedule"), "cron", day_of_week="mon-fri",
                      hour=int(hh), minute=int(mm), id="daily", misfire_grace_time=3600, coalesce=True)


def next_run():
    j = sched.get_job("daily")
    return j.next_run_time.strftime("%Y-%m-%d %H:%M") if j and j.next_run_time else "꺼짐"


@app.context_processor
def common():
    return {"profiles": PROFILES, "grade_info": GRADE_INFO, "running": scanner.is_running(),
            "next_run": next_run(), "now": datetime.now(),
            "blog_url": BLOG_URL, "docs_url": DOCS_URL}


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

@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon-32.png")


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
    """종목 상세. ?scan=ID로 과거 기준일(투자검증 포함) 시점의 평가를 볼 수 있다."""
    sid = request.args.get("scan", type=int)
    scan = db.scan(sid) if sid else db.latest_scan()
    if not scan or scan["status"] != "done":
        return redirect(url_for("dashboard"))
    bt = scan.get("kind") == "bt"
    r = db.score_detail(scan["id"], ticker)
    if not r or not r["detail"]:
        abort(404)
    base = scan["base_date"]
    meta = json.loads(scan["env"] or "{}")
    since = (datetime.strptime(base, "%Y%m%d") - timedelta(days=365)).strftime("%Y%m%d")
    hist = db.score_history(ticker, since=since)  # 기준일 1년 전부터 오늘까지 있는 점수 모두
    manual = db.manual_scores().get(ticker, {})
    holding = next((h for h in db.holdings() if h["ticker"] == ticker), None)
    excluded = any(e["ticker"] == ticker for e in db.exclusions())
    groups = {p: [ITEM_MAP[i[0]] for i in ITEMS if i[1] == p] for p in PERSPECTIVES}
    if scan["demo"]:  # 데모는 DB에 시세가 없으므로 같은 가상 시세를 다시 만든다
        from .demo import full_close, full_value
        fc, fv = full_close(), full_value()
        s_ = fc[ticker].dropna() if ticker in fc.columns else fc.iloc[:, :0]
        v_ = fv[ticker] if ticker in fv.columns else pd.Series(dtype=float)
        rows = [{"date": d.strftime("%Y%m%d"), "close": float(v), "value": float(v_.get(d, 0) or 0)} for d, v in s_.items()]
        px = [x for x in rows if x["date"] <= base][-260:]
        px_after = [x for x in rows if x["date"] > base]
    else:
        with db.conn() as c:
            px = [dict(x) for x in c.execute(
                "SELECT date, close, value FROM prices WHERE ticker=? AND date<=? AND close>0 ORDER BY date DESC LIMIT 260",
                (ticker, base))][::-1]
            px_after = [dict(x) for x in c.execute(
                "SELECT date, close, value FROM prices WHERE ticker=? AND date>? AND close>0 ORDER BY date",
                (ticker, base))] if bt else []
    if not bt:
        px_after = []
    res = None
    if bt:
        from . import verify as vf
        live = db.latest_scan()
        nw = db.score_detail(live["id"], ticker) if live else None
        res = {"now": nw, "eval_date": px_after[-1]["date"] if px_after else None}
        if px_after and r["close"]:
            cl = [x["close"] for x in px_after]
            res.update(last=cl[-1], ret=(cl[-1] / r["close"] - 1) * 100, mx=(max(cl) / r["close"] - 1) * 100,
                       mn=(min(cl) / r["close"] - 1) * 100)
            im = vf.index_returns(base, res["eval_date"], scan["demo"]).get(r["market"])
            res["idx"] = im
            res["ex"] = res["ret"] - im["ret"] if im else None
    d4_auto = r["detail"].get("d4_auto")
    if d4_auto is None:  # 이전 스캔 기록에는 자동 판정 값이 따로 없다
        d4_auto = any(a[0] == "D4" and a[3] == "자동" for a in r["detail"].get("adj", []))
    return render_template("stock.html", scan=scan, r=r, d=r["detail"], groups=groups, manual=manual, hist=hist, d4_auto=d4_auto,
                           holding=holding, excluded=excluded, sources=meta.get("sources", {}), bt=bt, res=res,
                           options=db.ticker_scans(ticker), options_live_id=(db.latest_scan() or {}).get("id"), px_json=json.dumps(px), after_json=json.dumps(px_after),
                           hist_json=json.dumps(hist), penalties=PENALTIES, bonuses=BONUSES,
                           prof=PROFILES.get(scan["profile"], PROFILES["C"]), can_rescore=scanner.LAST["scan_id"] == scan["id"])


@app.route("/verify")
def verify():
    """투자검증: 과거 기준일의 점수와 그 뒤 오늘까지의 실제 수익률을 비교한다."""
    from . import verify as vf
    st = db.settings()
    prof = st["profile"] if st["profile"] in PROFILES else "C"
    raw = (request.args.get("date") or "").replace("-", "")
    date = raw if len(raw) == 8 and raw.isdigit() else vf.default_date(prof)
    today = datetime.now().strftime("%Y%m%d")
    bad_date = date >= today
    scan = None if bad_date else db.bt_scan(date)
    last = db.latest_scan(done_only=False, kind="bt")
    fail = last if last and last["status"] == "failed" and last.get("req_date") == date and \
        (not scan or last["id"] > scan["id"]) else None
    ctx = {"date": date, "default_date": vf.default_date(prof), "offset_label": vf.OFFSET_LABEL[prof], "prof": prof,
           "scan": scan, "fail": fail, "bad_date": bad_date, "has_data": scanner.bt_has_data(date) if not bad_date else None,
           "history": db.bt_scans()[:30], "rows_json": "[]", "hold_rows": [], "env": {}, "warnings": [],
           "counts": {}, "grades": {}, "allr": None, "tops": [], "idx": {}, "eval_date": None,
           "live": db.latest_scan(), "changed": False}
    if not scan:
        return render_template("verify.html", **ctx)
    rows = db.scores(scan["id"])
    after, eval_date = vf.after_stats(scan["base_date"], scan["demo"])
    idx = vf.index_returns(scan["base_date"], eval_date, scan["demo"])
    live = ctx["live"]
    now_map = {r["ticker"]: r for r in db.scores(live["id"])} if live else {}
    holds = {h["ticker"]: h for h in db.holdings()}
    slim = []
    for r in rows:
        ret, mx, mn = vf.ret_of(r, after.get(r["ticker"]))
        nw = now_map.get(r["ticker"])
        im = idx.get(r["market"])
        slim.append({
            "t": r["ticker"], "n": r["name"], "m": r["market"], "sec": r["sector"], "c": r["close"],
            "last": (after.get(r["ticker"]) or {}).get("last"), "ret": ret, "mx": mx, "mn": mn,
            "ex": None if ret is None or not im else ret - im["ret"],
            "f": r["final"], "g": r["grade"], "s": r["s_short"], "mi": r["s_mid"], "l": r["s_long"], "cov": r["coverage"],
            "nf": nw["final"] if nw else None, "ng": nw["grade"] if nw else None,
            "df": (nw["final"] - r["final"]) if nw else None, "h": r["ticker"] in holds, "ko": r["knockout"],
        })
    grades, allr, tops = vf.summarize(slim)
    smap = {x["t"]: x for x in slim}
    hold_rows = []
    for tk, h in holds.items():
        x = smap.get(tk)
        hold_rows.append({"h": h, "x": x})
    meta = json.loads(scan["env"] or "{}")
    changed_at = db.raw_setting("scoring_changed_at")
    ctx.update(rows_json=json.dumps(slim, ensure_ascii=False), hold_rows=hold_rows, env=meta.get("env", {}),
               warnings=json.loads(scan["warnings"] or "[]"), counts={g: grades[g]["n"] for g in grades},
               grades=grades, allr=allr, tops=tops, idx=idx, eval_date=eval_date,
               changed=bool(changed_at and changed_at > (scan.get("finished_at") or "")))
    for hr in hold_rows:
        x = hr["x"]
        hr["notes"] = []
        if x:
            if x["df"] is not None:
                hr["notes"].append(f"점수 {x['df']:+.1f}")
            if x["ng"] and x["ng"] != x["g"]:
                hr["notes"].append(f"등급 {GRADE_INFO[x['g']][0]} → {GRADE_INFO[x['ng']][0]}")
            if x["mn"] is not None and x["mn"] <= -15:
                hr["notes"].append(f"기간 중 최대 {x['mn']:.1f}% 하락")
    return render_template("verify.html", **ctx)


@app.post("/api/verify")
def api_verify():
    j = request.get_json(silent=True) or {}
    date = str(j.get("date") or "").replace("-", "")
    if len(date) != 8 or not date.isdigit() or date >= datetime.now().strftime("%Y%m%d"):
        return jsonify(ok=False, message="오늘 이전의 기준일을 고르세요.")
    mode = "calc" if j.get("mode") == "calc" else "fetch"
    if mode == "calc" and not scanner.bt_has_data(date):
        return jsonify(ok=False, message="이 기준일의 저장 데이터가 없습니다. '지금 스캔'으로 먼저 데이터를 받으세요.")
    started = scanner.run_backtest_async(date, mode)
    if not started:
        return jsonify(ok=False, message="이미 스캔이 진행 중입니다.")
    return jsonify(ok=True, message="검증 계산을 시작했습니다." if mode == "calc" else
                   "기준일 데이터를 받아 검증을 시작했습니다. 처음 받는 기간이면 오래 걸릴 수 있습니다.")


@app.route("/manage")
def manage():
    scan = db.latest_scan()
    smap = {r["ticker"]: r for r in db.scores(scan["id"])} if scan else {}
    return render_template("manage.html", holdings=db.holdings(), exclusions=db.exclusions(), smap=smap,
                           names_json=json.dumps(db.stock_names(), ensure_ascii=False))


def _elapsed(start, end):
    """스캔 소요 시간 '3분 12초' (진행 중이면 지금까지)."""
    try:
        t0 = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(end, "%Y-%m-%d %H:%M:%S") if end else datetime.now()
    except (TypeError, ValueError):
        return ""
    sec = max(0, int((t1 - t0).total_seconds()))
    h, m, s_ = sec // 3600, sec % 3600 // 60, sec % 60
    txt = f"{h}시간 {m}분" if h else f"{m}분 {s_}초" if m else f"{s_}초"
    return txt if end else txt + " (진행 중)"


@app.route("/scans")
def scans():
    per = 20
    total = db.scans_count()
    pages = max(1, (total + per - 1) // per)
    page = min(max(1, request.args.get("page", 1, type=int)), pages)
    rows = db.scans(per, (page - 1) * per)
    for i, r in enumerate(rows):
        r["no"] = total - (page - 1) * per - i  # 화면 번호 = 오래된 것부터 센 순번
        r["elapsed"] = _elapsed(r.get("started_at"), r.get("finished_at"))
    for r in rows:
        r["warn_list"] = json.loads(r["warnings"] or "[]")
    last = db.latest_scan()
    changed_at = db.raw_setting("scoring_changed_at")
    changed = bool(changed_at and last and changed_at > (last.get("finished_at") or ""))
    return render_template("scans.html", rows=rows, saved=scanner.saved_bundle_info(), changed=changed,
                           page=page, pages=pages, total=total)


@app.post("/api/scans/purge")
def api_scans_purge():
    if scanner.is_running():
        return jsonify(ok=False, message="스캔이 진행 중입니다. 끝난 뒤에 지우세요.")
    n = db.delete_old_scans(365)
    return jsonify(ok=True, message=f"1년 지난 스캔 기록 {n}건을 지웠습니다." if n else "1년 지난 스캔 기록이 없습니다.")


@app.post("/api/verify/delete")
def api_verify_delete():
    """투자검증 기준일의 결과와 받아 둔 데이터를 지운다. 공용 시세 캐시는 남긴다."""
    if scanner.is_running():
        return jsonify(ok=False, message="스캔이 진행 중입니다. 끝난 뒤에 지우세요.")
    date = ((request.get_json(silent=True) or {}).get("date") or "").replace("-", "")
    if len(date) != 8 or not date.isdigit():
        return jsonify(ok=False, message="기준일이 올바르지 않습니다.")
    bases = db.delete_bt(date)
    files = 0
    for b in set(bases) | {date}:
        path = scanner.bt_bundle_path(b)
        if os.path.exists(path):
            os.remove(path)
            files += 1
    if not bases and not files:
        return jsonify(ok=True, message=f"{_fmt_date(date)} 투자검증 데이터가 없습니다.")
    return jsonify(ok=True, message=f"{_fmt_date(date)} 투자검증 결과와 받아 둔 데이터를 지웠습니다.")


@app.route("/support")
def support():
    return render_template("support.html", kakaopay_url=KAKAOPAY_URL, kakaopay_amount=KAKAOPAY_AMOUNT,
                           sponsors_url=SPONSORS_URL)


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
        d["scoring_changed_at"] = db.now()
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
            d["scan_time"] = "20:30"
        db.save_settings(d)
        raw = {}
        for pk in db.PROFILE_KEYS:
            for k in db.CRITERIA_KEYS:
                v = f.get(f"p{pk}.{k}", "").strip()
                try:
                    raw[f"p{pk}.{k}"] = float(v)
                except ValueError:
                    pass
        raw["scoring_changed_at"] = db.now()
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
    j = request.get_json(silent=True) or {}
    if j.get("recalc"):
        if not scanner.saved_bundle_info():
            return jsonify(ok=False, nodata=True, message="재계산에 쓸 저장 데이터가 아직 없습니다.")
        started = scanner.run_async("recalc")
        return jsonify(ok=started, message="저장된 데이터로 재계산을 시작했습니다." if started else "이미 스캔이 진행 중입니다.")
    started = scanner.run_async("manual", bool(j.get("demo")))
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
    s = db.latest_scan(done_only=False, kind=None)
    return jsonify(running=scanner.is_running(), scan=s and {k: s[k] for k in ("id", "code", "status", "message", "base_date", "started_at")})


def create_app():
    db.init()
    schedule()
    if not sched.running:
        sched.start()
    return app
