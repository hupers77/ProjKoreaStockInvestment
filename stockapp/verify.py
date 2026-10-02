"""투자검증(백테스트): 과거 기준일의 점수와 그 이후 오늘까지의 실제 주가 흐름을 비교한다."""
from datetime import datetime

import pandas as pd

from . import db

# 프로파일별 기본 검증 기간 (기준일 = 오늘 - 기간)
DEFAULT_OFFSET = {"U": {"days": 10}, "A": {"months": 2}, "B": {"months": 4}, "C": {"months": 6}, "D": {"months": 9}}
OFFSET_LABEL = {"U": "10일 전", "A": "2개월 전", "B": "4개월 전", "C": "6개월 전", "D": "9개월 전"}

_cache = {}


def default_date(profile, today=None):
    today = pd.Timestamp(today or datetime.now().date())
    return (today - pd.DateOffset(**DEFAULT_OFFSET.get(profile, DEFAULT_OFFSET["C"]))).strftime("%Y%m%d")


def _after_frame(base, demo):
    """기준일 다음 거래일부터 저장된 마지막 거래일까지의 종가 (index=날짜, columns=티커)."""
    if demo:
        from .demo import full_close
        c = full_close()
        return c[c.index > pd.Timestamp(base)]
    with db.conn() as c:
        last = c.execute("SELECT MAX(date) FROM price_days").fetchone()[0]
    key = (base, last)
    if key not in _cache:
        with db.conn() as c:
            df = pd.read_sql_query("SELECT date, ticker, close FROM prices WHERE date > ? AND close > 0", c, params=(base,))
        if df.empty:
            frame = pd.DataFrame()
        else:
            df["date"] = pd.to_datetime(df["date"])
            frame = df.pivot(index="date", columns="ticker", values="close").sort_index()
        _cache.clear()
        _cache[key] = frame
    return _cache[key]


def after_stats(base, demo):
    """{티커: {last, max, min, last_date}}, 평가일(YYYYMMDD)."""
    f = _after_frame(base, demo)
    if f.empty:
        return {}, None
    eval_date = f.index[-1].strftime("%Y%m%d")
    last = f.ffill().iloc[-1]
    mx, mn = f.max(), f.min()
    out = {}
    for tk in f.columns:
        if pd.notna(last[tk]):
            out[tk] = {"last": float(last[tk]), "max": float(mx[tk]), "min": float(mn[tk])}
    return out, eval_date


def index_returns(base, eval_date, demo):
    """{시장: {base, last, ret}} — 기준일 대비 평가일 지수 수익률."""
    out = {}
    if not eval_date:
        return out
    if demo:
        from .demo import make_bundle
        series = {mk: {d.strftime("%Y%m%d"): float(v) for d, v in s.items()} for mk, s in make_bundle().index_close.items()}
    else:
        series = {mk: db.index_series(mk) for mk in ("KOSPI", "KOSDAQ")}
    for mk, s in series.items():
        b = [v for d, v in s.items() if d <= base]
        e = [v for d, v in s.items() if d <= eval_date]
        if b and e:
            out[mk] = {"base": b[-1], "last": e[-1], "ret": (e[-1] / b[-1] - 1) * 100}
    return out


def ret_of(r, a):
    """과거 점수 행 r과 이후 시세 a로 수익률·최대상승·최대하락(%)을 만든다."""
    c0 = r.get("close")
    if not a or not c0:
        return None, None, None
    return (a["last"] / c0 - 1) * 100, (a["max"] / c0 - 1) * 100, (a["min"] / c0 - 1) * 100


def summarize(rows):
    """등급별·상위 N 종목 평균 수익률과 상승 비율."""
    def stat(xs):
        xs = [x for x in xs if x is not None]
        if not xs:
            return {"n": 0, "avg": None, "med": None, "win": None}
        s = pd.Series(xs)
        return {"n": len(xs), "avg": float(s.mean()), "med": float(s.median()), "win": float((s > 0).mean() * 100)}

    grades = {g: stat([r["ret"] for r in rows if r["g"] == g]) for g in ("S", "A", "B", "C", "X")}
    allr = stat([r["ret"] for r in rows])
    ranked = [r for r in rows if r["g"] != "X"]
    ranked.sort(key=lambda r: r["f"] or 0, reverse=True)
    tops = [(n, stat([r["ret"] for r in ranked[:n]])) for n in (10, 30, 50, 100) if len(ranked) >= n]
    return grades, allr, tops
