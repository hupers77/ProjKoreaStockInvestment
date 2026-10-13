"""U 프로파일 일별 재채점 (항목 상세 포함). 각 날짜 D는 D 이전의 가장 최근 수집 묶음(bt/last)을 기준으로
가격·지수는 D까지 실제값, 수급·재무는 그 묶음 시점 값(미래정보 없음)으로 채점한다."""
import glob, gzip, os, pickle, sys
from multiprocessing import Pool

APP = os.path.expanduser("~/ProjKoreaStockInvestment/ProjKoreaStockInvestment")
SCR = os.path.dirname(os.path.abspath(__file__))
os.environ["STOCKAPP_DATA"] = os.path.join(SCR, "data")
sys.path.insert(0, APP)

import pandas as pd
from stockapp import db
from stockapp.bundle import Bundle
from stockapp.collector import load_panel
from stockapp.scoring import score_universe

OUT = os.path.join(SCR, "out")
BUNDLES = sorted(glob.glob(os.path.join(SCR, "data/bt/*.pkl.gz"))) + [os.path.join(APP, "data/last_bundle.pkl.gz")]
G = {}


def load(p):
    with gzip.open(p, "rb") as f:
        return pickle.load(f)


def make_day(b, d, panels):
    n_hist = len(b.close)
    tks = [t for t in b.info.index if t in panels["close"].columns]
    c0 = b.close.iloc[-1].reindex(tks)
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
    return Bundle(base_date=d.strftime("%Y%m%d"), close=win["close"], open=win["open"], high=win["high"],
                  low=win["low"], volume=win["volume"], value=win["value"], info=info,
                  index_close={k: v[v.index <= d].iloc[-n_hist:] for k, v in G["idx"].items()},
                  market_foreign20=b.market_foreign20, fx=b.fx, dps_hist=b.dps_hist, pbr_hist=b.pbr_hist,
                  fin=b.fin, admin=set(), sources=b.sources, warnings=[], demo=False)


def init():
    with db.conn() as c:
        days = [r[0] for r in c.execute("SELECT date FROM price_days ORDER BY date")]
    G["panels"] = load_panel(days)
    G["idx"] = {}
    for mk in ("KOSPI", "KOSDAQ"):
        ser = db.index_series(mk)
        G["idx"][mk] = pd.Series(ser).set_axis(pd.to_datetime(list(ser.keys())))
    G["st"] = db.scoring_settings()
    G["bcache"] = {}


def job(args):
    bpath, dstr = args
    out = os.path.join(OUT, f"{dstr}.pkl")
    if os.path.exists(out):
        return dstr
    if bpath not in G["bcache"]:
        G["bcache"] = {bpath: load(bpath)}
    b = G["bcache"][bpath]
    d = pd.Timestamp(dstr)
    bd = b if b.base_date == dstr else make_day(b, d, G["panels"])
    res, _ = score_universe(bd, G["st"], (), (), {})
    rows = []
    for r in res:
        row = {"date": dstr, "ticker": r["ticker"], "name": r["name"], "market": r["market"], "sector": r["sector"],
               "close": r["close"], "mcap": r["mcap"], "final": r["final"], "grade": r["grade"],
               "s_short": r["persp"]["단기"], "s_mid": r["persp"]["중기"], "base": r["base"],
               "penalty": r["penalty"], "ko": " / ".join(r["knockout"]), "cov": r["coverage"],
               "bundle": b.base_date}
        for k, v in r["items"].items():
            if k[0] == "S" or k in ("M13",):
                row[k] = v.score
                row[k + "_v"] = v.value
        rows.append(row)
    pd.DataFrame(rows).to_pickle(out + ".tmp")
    os.replace(out + ".tmp", out)
    return dstr


def main():
    with db.conn() as c:
        days = [r[0] for r in c.execute("SELECT date FROM price_days ORDER BY date")]
    metas = sorted(((load(p).base_date if False else None), p) for p in BUNDLES)
    # 묶음 기준일: 파일명(bt) 또는 last_bundle.json
    import json
    bases = []
    for p in BUNDLES:
        if p.endswith("last_bundle.pkl.gz"):
            bases.append((json.load(open(os.path.join(APP, "data/last_bundle.json")))["base_date"], p))
        else:
            bases.append((os.path.basename(p)[:8], p))
    bases.sort()
    start = bases[0][0]
    only = sys.argv[1:] or None
    jobs = []
    for d in days:
        if d < start or (only and d not in only):
            continue
        bp = [p for bdate, p in bases if bdate <= d][-1]
        jobs.append((bp, d))
    # 같은 묶음끼리 모아서 한 프로세스가 연속 처리하도록 정렬
    jobs.sort()
    print(len(jobs), "days", flush=True)
    nproc = int(os.environ.get("NPROC", "6"))
    with Pool(nproc, initializer=init) as pool:
        for i, d in enumerate(pool.imap_unordered(job, jobs, chunksize=4)):
            print(i + 1, d, flush=True)


if __name__ == "__main__":
    main()
