"""스캔 실행: 데이터 수집 → 채점 → 저장."""
import json
import threading
import traceback

from . import db
from .scoring import score_universe

_running = threading.Lock()
LAST = {"bundle": None, "universe": None, "scan_id": None}  # 마지막 스캔 데이터 (수동 점수 즉시 재계산용)


def is_running():
    return _running.locked()


def run_scan(trigger="manual", demo=False):
    if not _running.acquire(blocking=False):
        return None
    try:
        st = db.settings()
        sst = db.scoring_settings(st)
        scan_id = db.scan_start(sst["profile"], trigger, demo)

        def log(msg):
            db.scan_update(scan_id, message=msg)
            print(f"[scan {scan_id}] {msg}", flush=True)

        try:
            if demo:
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
            env = {mk: {k: v for k, v in e.items() if k != "conds"} | {"conds": e["conds"]} for mk, e in u["env"].items()}
            db.scan_update(scan_id, status="done", finished_at=db.now(), base_date=b.base_date,
                           n_scored=len(results), message="완료",
                           env=json.dumps({"env": env, "sources": b.sources}, ensure_ascii=False, default=float),
                           warnings=json.dumps(b.warnings, ensure_ascii=False))
            db.prune_details(int(st.get("keep_detail_scans", 60)))
            return scan_id
        except Exception as e:
            traceback.print_exc()
            db.scan_update(scan_id, status="failed", finished_at=db.now(), message=str(e)[:1000])
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
    if b is None or ticker not in b.info.index or ticker not in b.close.columns:
        return False
    t = Ctx(ticker, b, u)
    res = score_ticker(t, db.scoring_settings(), db.manual_scores().get(ticker, {}))
    res.update({"ticker": ticker, "name": t.info.get("name", ticker), "market": t.market, "sector": t.sector,
                "close": float(t.c.iloc[-1]), "mcap": t.mcap, "chg": float(ind.ret(t.c, 1) or 0)})
    db.save_scores(sid, [res])
    return True
