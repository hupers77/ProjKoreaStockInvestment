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
