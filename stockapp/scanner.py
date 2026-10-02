"""스캔 실행: 데이터 수집 → 채점 → 저장."""
import gzip
import json
import os
import pickle
import threading
import traceback

from . import db
from .scoring import score_universe

_running = threading.Lock()
LAST = {"bundle": None, "universe": None, "scan_id": None}  # 마지막 스캔 데이터 (수동 점수 즉시 재계산용)
BUNDLE_PATH = os.path.join(db.DATA_DIR, "last_bundle.pkl.gz")  # 재계산용으로 디스크에 남기는 마지막 수집 데이터
BUNDLE_INFO = os.path.join(db.DATA_DIR, "last_bundle.json")


def _save_bundle(b, scan_id):
    tmp = BUNDLE_PATH + ".tmp"
    with gzip.open(tmp, "wb", compresslevel=3) as f:
        pickle.dump(b, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, BUNDLE_PATH)
    with open(BUNDLE_INFO, "w", encoding="utf-8") as f:
        json.dump({"scan_id": scan_id, "base_date": b.base_date, "demo": b.demo, "saved_at": db.now()}, f)


def saved_bundle_info():
    """재계산에 쓸 저장 데이터 정보 (없으면 None)."""
    if not os.path.exists(BUNDLE_PATH):
        return None
    try:
        with open(BUNDLE_INFO, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_bundle():
    if LAST["bundle"] is not None:
        return LAST["bundle"]
    if not os.path.exists(BUNDLE_PATH):
        return None
    with gzip.open(BUNDLE_PATH, "rb") as f:
        return pickle.load(f)


def is_running():
    return _running.locked()


def run_scan(trigger="manual", demo=False):
    """trigger="recalc"이면 데이터를 새로 받지 않고 저장된 마지막 데이터를 현재 설정·가중치로 다시 채점한다."""
    if not _running.acquire(blocking=False):
        return None
    try:
        st = db.settings()
        sst = db.scoring_settings(st)
        recalc = trigger == "recalc"
        b = None
        if recalc:
            b = _load_bundle()
            if b is None:
                raise_msg = "재계산할 저장 데이터가 없습니다. 먼저 '지금 스캔'을 한 번 실행하세요."
                scan_id = db.scan_start(sst["profile"], trigger, False)
                db.scan_update(scan_id, status="failed", finished_at=db.now(), message=raise_msg)
                return scan_id
            demo = b.demo
        scan_id = db.scan_start(sst["profile"], trigger, demo)

        def log(msg):
            db.scan_update(scan_id, message=msg)
            print(f"[scan {scan_id}] {msg}", flush=True)

        try:
            if recalc:
                log(f"저장된 데이터({b.base_date})로 재계산 중…")
            elif demo:
                from .demo import make_bundle
                log("데모 데이터 생성 중…")
                b = make_bundle()
            else:
                from .collector import collect
                b = collect(st, log)
            log("점수 산정 중…")
            hold = [h["ticker"] for h in db.holdings()]
            excl = [e["ticker"] for e in db.exclusions()]
            results, u = score_universe(b, sst, hold, excl, db.manual_scores(),
                                        progress=lambda i, n: log(f"점수 산정 중 {i}/{n}"))
            db.save_scores(scan_id, results)
            LAST.update(bundle=b, universe=u, scan_id=scan_id)
            if not recalc:
                try:
                    _save_bundle(b, scan_id)
                except Exception as e:  # 저장 실패는 스캔 결과에 영향 없음
                    print(f"[scan {scan_id}] 재계산용 데이터 저장 실패: {e}", flush=True)
            env = {mk: {k: v for k, v in e.items() if k != "conds"} | {"conds": e["conds"]} for mk, e in u["env"].items()}
            db.scan_update(scan_id, status="done", finished_at=db.now(), base_date=b.base_date,
                           n_scored=len(results), message="완료 (저장 데이터로 재계산)" if recalc else "완료",
                           env=json.dumps({"env": env, "sources": b.sources}, ensure_ascii=False, default=float),
                           warnings=json.dumps(b.warnings, ensure_ascii=False))
            db.prune_details(int(st.get("keep_detail_scans", 60)))
            return scan_id
        except Exception as e:
            traceback.print_exc()
            msg = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
            db.scan_update(scan_id, status="failed", finished_at=db.now(), message=msg[:1000])
            return scan_id
    finally:
        _running.release()


BT_DIR = os.path.join(db.DATA_DIR, "bt")  # 투자검증 기준일별 수집 데이터 (검증 계산에 재사용)


def bt_bundle_path(base_date):
    return os.path.join(BT_DIR, f"{base_date}.pkl.gz")


def bt_has_data(date):
    """투자검증 기준일의 수집 데이터가 저장돼 있으면 실제 거래일을 돌려준다."""
    base = db.bt_base_date(date)
    if base and os.path.exists(bt_bundle_path(base)):
        return base
    if os.path.exists(bt_bundle_path(date)):
        return date
    return None


def run_backtest(req_date, mode="fetch"):
    """투자검증: req_date(YYYYMMDD) 시점의 데이터로 현재 평가 기준을 적용해 채점한다.
    mode="fetch"는 그 시점(1년 이전까지)의 데이터를 받아 저장한 뒤 계산하고,
    mode="calc"는 이미 받아 둔 데이터로 계산만 한다."""
    if not _running.acquire(blocking=False):
        return None
    try:
        st = db.settings()
        sst = db.scoring_settings(st)
        live = db.latest_scan()
        demo = bool(live and live.get("demo"))  # 데모로 쓰는 중이면 검증도 데모 시세로
        scan_id = db.scan_start(sst["profile"], "bt-" + mode, demo, kind="bt", req_date=req_date)

        def log(msg):
            db.scan_update(scan_id, message=msg)
            print(f"[검증 {scan_id}] {msg}", flush=True)

        try:
            if mode == "calc":
                base = bt_has_data(req_date)
                if not base:
                    raise RuntimeError("이 기준일의 저장 데이터가 없습니다. '지금 스캔'으로 먼저 데이터를 받으세요.")
                log(f"저장된 {base} 데이터로 계산 중…")
                with gzip.open(bt_bundle_path(base), "rb") as f:
                    b = pickle.load(f)
                db.scan_update(scan_id, demo=int(b.demo))
            else:
                if demo:
                    from .demo import make_bundle
                    log("데모 데이터 생성 중…")
                    b = make_bundle(base_date=req_date)
                else:
                    from .collector import collect
                    b = collect(st, log, base_date=req_date, on_base=lambda d: db.scan_update(scan_id, base_date=d))
                os.makedirs(BT_DIR, exist_ok=True)
                tmp = bt_bundle_path(b.base_date) + ".tmp"
                with gzip.open(tmp, "wb", compresslevel=3) as f:
                    pickle.dump(b, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp, bt_bundle_path(b.base_date))
            db.scan_update(scan_id, base_date=b.base_date)
            log("점수 산정 중…")
            hold = [h["ticker"] for h in db.holdings()]
            excl = [e["ticker"] for e in db.exclusions()]
            results, u = score_universe(b, sst, hold, excl, db.manual_scores(),
                                        progress=lambda i, n: log(f"점수 산정 중 {i}/{n}"))
            db.save_scores(scan_id, results)
            n_days = _score_after_days(b, sst, hold, excl, log)
            env = {mk: {k: v for k, v in e.items() if k != "conds"} | {"conds": e["conds"]} for mk, e in u["env"].items()}
            db.scan_update(scan_id, status="done", finished_at=db.now(), n_scored=len(results),
                           message=("완료 (검증 계산" if mode == "calc" else "완료 (") + f"이후 {n_days}개 날짜 점수 포함)",
                           env=json.dumps({"env": env, "sources": b.sources}, ensure_ascii=False, default=float),
                           warnings=json.dumps(b.warnings, ensure_ascii=False))
            return scan_id
        except Exception as e:
            traceback.print_exc()
            msg = str(e) if isinstance(e, RuntimeError) else f"{type(e).__name__}: {e}"
            db.scan_update(scan_id, status="failed", finished_at=db.now(), message=msg[:1000])
            return scan_id
    finally:
        _running.release()


AFTER_DAILY_MAX = 20  # 기준일 이후 거래일이 이보다 많으면 5거래일(주 1회) 간격으로 계산 (전 종목 1회 채점이 수십 초)


def _after_bundles(b):
    """기준일 이후 날짜별 채점용 데이터 묶음을 만든다 (데이터를 새로 받지 않음).
    주가·거래량·지수는 그날까지의 실제 값, 시가총액·PER·PBR·배당수익률은 주가 비율로 조정,
    수급·외국인 지분율·재무·환율은 기준일 값을 그대로 쓴다."""
    import pandas as pd
    from .bundle import Bundle
    n_hist = len(b.close)
    if b.demo:
        from .demo import _paths, make_bundle
        _, _, _, close, high, low, opn, volume, value, _ = _paths(len(b.info), 7)
        panels = {"close": close, "high": high, "low": low, "open": opn, "volume": volume, "value": value}
        idx_full = make_bundle(n=len(b.info)).index_close
    else:
        from .collector import load_panel
        with db.conn() as c:
            days = [r[0] for r in c.execute("SELECT date FROM price_days ORDER BY date")]
        before = [d for d in days if d <= b.base_date]
        after = [d for d in days if d > b.base_date]
        if not after:
            return []
        panels = load_panel(before[-n_hist:] + after)
        idx_full = {}
        for mk in ("KOSPI", "KOSDAQ"):
            ser = db.index_series(mk)
            if ser:
                idx_full[mk] = pd.Series(ser).set_axis(pd.to_datetime(list(ser.keys())))
    base_ts = pd.Timestamp(b.base_date)
    after = [d for d in panels["close"].index if d > base_ts]
    if len(after) > AFTER_DAILY_MAX:
        after = after[::-1][::5][::-1]  # 오늘(마지막 거래일)부터 5거래일 간격
    tks = [t for t in b.info.index if t in panels["close"].columns]
    c0 = b.close.iloc[-1].reindex(tks)
    for d in after:
        win = {k: v.loc[:d, tks].iloc[-n_hist:] for k, v in panels.items()}
        cd = win["close"].ffill().iloc[-1]
        ratio = (cd / c0).where(lambda x: x > 0)
        info = b.info.loc[tks].copy()
        if "shares" in info:
            info["mcap"] = info["shares"] * cd
        for col in ("per", "pbr"):
            if col in info:
                info[col] = info[col] * ratio
        if "div" in info:
            info["div"] = info["div"] / ratio
        yield Bundle(base_date=d.strftime("%Y%m%d"), close=win["close"], open=win["open"], high=win["high"],
                     low=win["low"], volume=win["volume"], value=win["value"], info=info,
                     index_close={k: v[v.index <= d].iloc[-n_hist:] for k, v in idx_full.items()},
                     market_foreign20=b.market_foreign20, fx=b.fx, dps_hist=b.dps_hist, pbr_hist=b.pbr_hist,
                     fin=b.fin, admin=set(), sources=b.sources, warnings=[], demo=b.demo), len(after)


def _score_after_days(b, sst, hold, excl, log):
    """기준일 다음 날부터 오늘까지 날짜별 점수를 만들어 점수 추이에 쓴다."""
    db.delete_bt_days(b.base_date)
    n = 0
    manual = db.manual_scores()
    for bd, total in _after_bundles(b):
        n += 1
        log(f"기준일 이후 점수 생성 중 {n}/{total} ({bd.base_date})")
        results, _ = score_universe(bd, sst, hold, excl, manual)
        sid = db.scan_start(sst["profile"], "bt-day", b.demo, kind="btd", req_date=b.base_date)
        db.save_scores(sid, results, with_detail=False)
        db.scan_update(sid, status="done", finished_at=db.now(), base_date=bd.base_date, n_scored=len(results),
                       message="투자검증 이후 날짜 점수")
    return n


def run_backtest_async(req_date, mode="fetch"):
    if is_running():
        return False
    threading.Thread(target=run_backtest, args=(req_date, mode), daemon=True).start()
    return True


def run_async(trigger="manual", demo=False):
    if is_running():
        return False
    threading.Thread(target=run_scan, args=(trigger, demo), daemon=True).start()
    return True


def rescore(ticker):
    """수동 점수 변경 후 마지막 스캔 데이터로 해당 종목만 다시 계산한다."""
    from .scoring import Ctx, ind, score_ticker
    b, u, sid = LAST["bundle"], LAST["universe"], LAST["scan_id"]
    latest = db.latest_scan()
    if b is None and latest:  # 프로그램 재시작 후에는 저장된 데이터를 불러온다
        info = saved_bundle_info() or {}
        if info.get("base_date") == latest.get("base_date"):
            from .scoring import build_universe
            b = _load_bundle()
            if b is not None:
                u = build_universe(b, [tk for tk in b.info.index if tk in b.close.columns and b.close[tk].notna().sum() >= 20])
                sid = latest["id"]
                LAST.update(bundle=b, universe=u, scan_id=sid)
    if b is None or ticker not in b.info.index or ticker not in b.close.columns:
        return False
    t = Ctx(ticker, b, u)
    res = score_ticker(t, db.scoring_settings(), db.manual_scores().get(ticker, {}))
    res.update({"ticker": ticker, "name": t.info.get("name", ticker), "market": t.market, "sector": t.sector,
                "close": float(t.c.iloc[-1]), "mcap": t.mcap, "chg": float(ind.ret(t.c, 1) or 0)})
    db.save_scores(sid, [res])
    return True
