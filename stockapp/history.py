"""저장된 데이터만으로 과거 날짜의 점수를 다시 계산한다 (API를 다시 부르지 않는다).

날짜 d의 채점 데이터는 이렇게 만든다.
- 주가·거래량·지수: DB에 저장된 그날까지의 실제 값
- 종목 정보·수급·외국인 지분율: 그날 스캔 때 보관한 확정치(snap)가 있으면 그것,
  없으면 날짜가 가장 가까운 저장 데이터(마지막 스캔·투자검증 기준일)의 값을 쓰고
  시가총액·PER·PBR·배당수익률만 주가 비율로 조정한다
- 재무·배당 이력·환율: 날짜가 가장 가까운 저장 데이터의 값
모든 프로파일 점수를 계산해 score_days(일자별 점수)에 저장한다.
"""
import glob
import gzip
import os
import pickle
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta
from multiprocessing import get_context

import pandas as pd

from . import db
from .bundle import Bundle

YEAR_DAYS = 365  # 기준이 바뀌면 오늘부터 이 기간(달력일)의 점수를 모두 다시 계산한다
_W = {}  # 작업 프로세스의 공용 데이터 (_init에서 채움)


def load_bundle(path):
    with gzip.open(path, "rb") as f:
        return pickle.load(f)


def stored_bundles():
    """디스크에 있는 수집 데이터 묶음 [(기준일, 경로)] — 마지막 스캔 + 투자검증 기준일들."""
    from . import scanner
    out = []
    info = scanner.saved_bundle_info()
    if info and info.get("base_date"):
        out.append((info["base_date"], scanner.BUNDLE_PATH))
    for p in glob.glob(os.path.join(scanner.BT_DIR, "*.pkl.gz")):
        out.append((os.path.basename(p)[:8], p))
    return sorted(out)


def _template(b):
    """가격 패널을 뺀 채점용 데이터 (작업 프로세스로 보내는 크기를 줄인다)."""
    return {"base": b.base_date, "info": b.info, "c0": b.close.iloc[-1], "fin": b.fin, "dps_hist": b.dps_hist,
            "pbr_hist": b.pbr_hist, "fx": b.fx, "mf": b.market_foreign20, "admin": b.admin, "sources": b.sources,
            "demo": b.demo, "n_hist": len(b.close)}


def _panels(days, n_hist, demo, n_demo=300):
    """days(YYYYMMDD) 채점에 필요한 가격 패널과 지수. 가장 이른 날 앞으로 n_hist 거래일까지 불러온다."""
    if demo:
        from .demo import _paths, make_bundle
        _, _, _, close, high, low, opn, volume, value, _ = _paths(n_demo, 7)
        panels = {"close": close, "high": high, "low": low, "open": opn, "volume": volume, "value": value}
        idx = make_bundle(n=n_demo).index_close
        return panels, idx
    from .collector import load_panel
    with db.conn() as c:
        all_days = [r[0] for r in c.execute("SELECT date FROM price_days ORDER BY date")]
    first = min(days)
    i0 = max(0, sum(1 for d in all_days if d < first) - n_hist)
    panels = load_panel(all_days[i0:])
    idx = {}
    for mk in ("KOSPI", "KOSDAQ"):
        ser = db.index_series(mk)
        if ser:
            idx[mk] = pd.Series(ser).set_axis(pd.to_datetime(list(ser.keys())))
    return panels, idx


def _snap(d):
    """그날 스캔 때 보관한 종목 정보·수급 확정치 (없으면 None)."""
    from .collector import _df_unpack
    for kind in ("live", "bt"):
        v, _ = db.cache_get(f"snap:{d}:{kind}")
        if v:
            fx = pd.Series({pd.Timestamp(k): x for k, x in v["fx"].items()}, dtype=float) if v.get("fx") else None
            return _df_unpack(v["info"]), v.get("mf") or {}, fx
    return None


def day_bundle(d, panels, idx, templates):
    """날짜 d(YYYYMMDD)의 채점 데이터 묶음. 그날 시세가 없으면 None."""
    ts = pd.Timestamp(d)
    if ts not in panels["close"].index:
        return None
    tpl = min(templates, key=lambda t: (abs((pd.Timestamp(t["base"]) - ts).days), t["base"] < d))
    n_hist = tpl["n_hist"]
    last = panels["close"].loc[ts]
    tks = [t for t in tpl["info"].index if t in last.index and pd.notna(last[t])]
    if not tks:
        return None
    win = {k: v.loc[:ts, tks].iloc[-n_hist:] for k, v in panels.items()}
    cd = win["close"].iloc[-1]
    info = tpl["info"].loc[tks].copy()
    mf, fx = tpl["mf"], tpl["fx"]
    snap = None if tpl["demo"] or tpl["base"] == d else _snap(d)
    if snap:
        s_info, mf, s_fx = snap
        info.update(s_info.reindex(tks))
        fx = s_fx if s_fx is not None else fx
    elif tpl["base"] != d:
        ratio = (cd / tpl["c0"].reindex(tks)).where(lambda x: x > 0)
        if "shares" in info:
            info["mcap"] = info["shares"] * cd
        for col in ("per", "pbr"):
            if col in info:
                info[col] = info[col] * ratio
        if "div" in info:
            info["div"] = info["div"] / ratio
    if fx is not None:
        fx = fx[fx.index <= ts]
    return Bundle(base_date=d, close=win["close"], open=win["open"], high=win["high"], low=win["low"],
                  volume=win["volume"], value=win["value"], info=info,
                  index_close={k: v[v.index <= ts].iloc[-n_hist:] for k, v in idx.items()},
                  market_foreign20=mf, fx=fx, dps_hist=tpl["dps_hist"], pbr_hist=tpl["pbr_hist"], fin=tpl["fin"],
                  admin=tpl["admin"] if tpl["base"] == d else set(), sources=tpl["sources"], warnings=[], demo=tpl["demo"])


def _init(w):
    _W.clear()
    _W.update(w)


def _score_day(d):
    """작업 프로세스: 날짜 d를 모든 프로파일로 채점해 score_days 행을 돌려준다."""
    from .scoring import score_universe_all
    b = day_bundle(d, _W["panels"], _W["idx"], _W["templates"])
    if b is None:
        return d, []
    res, _ = score_universe_all(b, _W["sts"], _W["hold"], _W["excl"], _W["manual"])
    return d, db.day_rows(d, res, "calc")


def workers():
    """채점에 쓸 프로세스 수 (CPU 1개는 웹 화면용으로 남긴다)."""
    return max(1, min(8, (os.cpu_count() or 2) - 1))


def score_days(days, log, demo=False, extra=None):
    """days(YYYYMMDD 목록)를 모든 프로파일로 다시 채점해 score_days에 저장한다 (있던 값은 덮어씀).
    extra: 디스크에 없는 데이터 묶음(방금 받은 것)을 날짜 선택 후보로 더한다. 저장한 날짜 수를 돌려준다."""
    if not days:
        return 0
    templates = [_template(b) for b in (extra or [])]
    have = {t["base"] for t in templates}
    for base, path in stored_bundles():
        if base in have:
            continue
        try:
            b = load_bundle(path)
        except Exception as e:
            log(f"저장 데이터 {base} 읽기 실패: {e}")
            continue
        if bool(b.demo) == demo:
            templates.append(_template(b))
    if not templates:
        raise RuntimeError("계산에 쓸 저장 데이터가 없습니다. 먼저 '지금 스캔'을 한 번 실행하세요.")
    n_hist = max(t["n_hist"] for t in templates)
    n_demo = len(templates[0]["info"]) if demo else 300
    log(f"저장된 시세 불러오는 중 ({len(days)}일)…")
    panels, idx = _panels(days, n_hist, demo, n_demo)
    st = db.settings()
    w = {"panels": panels, "idx": idx, "templates": templates, "sts": db.scoring_settings_all(st),
         "hold": [h["ticker"] for h in db.holdings()], "excl": [e["ticker"] for e in db.exclusions()],
         "manual": db.manual_scores()}
    days = sorted(days, reverse=True)  # 최근 날짜부터 (점수 추이의 최근 구간이 먼저 채워진다)
    done = 0

    def save(d, rows):
        nonlocal done
        done += 1
        if rows:
            db.save_day_rows(rows)
        log(f"과거 점수 계산 중 {done}/{len(days)} ({d[:4]}-{d[4:6]}-{d[6:]})")

    nw = workers()
    if nw > 1 and len(days) > 2:
        try:
            with ProcessPoolExecutor(max_workers=nw, mp_context=get_context("spawn"),
                                     initializer=_init, initargs=(w,)) as ex:
                for d, rows in ex.map(_score_day, days):
                    save(d, rows)
            return done
        except Exception as e:  # 병렬 실행이 안 되는 환경이면 한 프로세스로 이어서 계산
            log(f"병렬 계산 실패, 순차 계산으로 전환: {type(e).__name__} {e}")
            days = days[done:]
    _init(w)
    for d in days:
        save(*_score_day(d))
    return done


def year_days(demo=False, since_days=YEAR_DAYS):
    """최근 1년의 거래일 (저장된 시세 기준, YYYYMMDD 목록)."""
    since = (datetime.now() - timedelta(days=since_days)).strftime("%Y%m%d")
    if demo:
        from .demo import full_close
        return [d.strftime("%Y%m%d") for d in full_close().index if d.strftime("%Y%m%d") >= since]
    with db.conn() as c:
        return [r[0] for r in c.execute("SELECT date FROM price_days WHERE date >= ? ORDER BY date", (since,))]


def missing_days(demo=False):
    """최근 1년 거래일 중 점수가 아직 없는 날."""
    have = db.score_day_dates()
    return [d for d in year_days(demo) if d not in have]
