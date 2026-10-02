"""무료 시세·수급 데이터 수집 (pykrx = 한국거래소 정보데이터시스템, FinanceDataReader 보조).

한국거래소(data.krx.co.kr)는 무료 회원 로그인이 필요하다. 설정 화면의 KRX 아이디/비밀번호를 사용한다.
일별 전 종목 시세는 DB에 쌓아 두므로 첫 실행(약 300거래일 백필)만 오래 걸리고 이후엔 하루치만 받는다.
"""
import os
import time
from datetime import datetime, timedelta

import pandas as pd

from . import db
from .bundle import Bundle

PAUSE = 0.15


def _login(st):
    if st.get("krx_id") and st.get("krx_pw"):
        changed = os.environ.get("KRX_ID") != st["krx_id"] or os.environ.get("KRX_PW") != st["krx_pw"]
        os.environ["KRX_ID"], os.environ["KRX_PW"] = st["krx_id"], st["krx_pw"]
        if changed:
            try:
                from pykrx.website.comm import auth
                auth.set_auth_session(None)
            except Exception:
                pass


def _retry(fn, *a, tries=3, **kw):
    err = None
    for i in range(tries):
        try:
            out = fn(*a, **kw)
            time.sleep(PAUSE)
            return out
        except Exception as e:
            err = e
            time.sleep(1.5 * (i + 1))
    raise err


def _cached(key, fn, permanent=True):
    v, _ = db.cache_get(key)
    if v is not None and permanent:
        return v
    v = fn()
    db.cache_put(key, v)
    return v


def _frame_to_dict(df, cols):
    df = df.copy()
    df.index = df.index.astype(str)
    return {c: df[c].astype(float).to_dict() for c in cols if c in df.columns}


def _index_closes_pykrx(stock, start, base):
    out = {}
    for code, mk in (("1001", "KOSPI"), ("2001", "KOSDAQ")):
        # name_display=False: 지수 이름 목록 조회를 건너뛴다. 이 목록 조회가 실패하면
        # pykrx 내부에서 KeyError('지수명')가 나서 스캔 전체가 멈췄다.
        df = _retry(stock.get_index_ohlcv, start, base, code, name_display=False)
        if df is None or df.empty or "종가" not in df.columns:
            raise RuntimeError(f"{mk} 지수 시세가 비어 있음")
        out[mk] = df["종가"].astype(float)
    return out


def _index_closes_fdr(start, base):
    import FinanceDataReader as fdr
    out = {}
    s, e = f"{start[:4]}-{start[4:6]}-{start[6:]}", f"{base[:4]}-{base[4:6]}-{base[6:]}"
    for code, mk in (("KS11", "KOSPI"), ("KQ11", "KOSDAQ")):
        df = fdr.DataReader(code, s, e)
        if df is None or df.empty:
            raise RuntimeError(f"{mk} 지수 시세가 비어 있음")
        out[mk] = df["Close"].astype(float)
    return out


def trading_dates(stock, base, log=print, warnings=None):
    """거래일 목록과 KOSPI·KOSDAQ 지수 종가. pykrx가 실패하면 FinanceDataReader로 대체한다."""
    start = (datetime.strptime(base, "%Y%m%d") - timedelta(days=365 * 6)).strftime("%Y%m%d")
    errors = []
    for name, fn in (("pykrx", lambda: _index_closes_pykrx(stock, start, base)),
                     ("FinanceDataReader", lambda: _index_closes_fdr(start, base))):
        try:
            out = fn()
            out = {k: v[v > 0].sort_index() for k, v in out.items()}
            out = {k: v[~v.index.duplicated()] for k, v in out.items()}
            if errors and warnings is not None:
                warnings.append(f"KRX 지수 조회 실패로 {name} 지수 사용: {errors[0]}")
            dates = [d.strftime("%Y%m%d") for d in out["KOSPI"].index]
            lag = (datetime.strptime(base, "%Y%m%d") - datetime.strptime(dates[-1], "%Y%m%d")).days
            if lag > 6 and warnings is not None:
                warnings.append(f"{name} 지수 데이터가 {dates[-1]}까지만 있어 기준일이 {lag}일 늦습니다.")
            return dates, out
        except Exception as e:
            errors.append(f"{name}: {type(e).__name__} {e}")
            log(f"지수 조회 실패({name}), 다른 경로 시도…")
    raise RuntimeError("지수 시세를 가져오지 못했습니다 — " + " / ".join(errors))


def krx_check(stock, st):
    """로그인 상태와 실제 데이터 응답을 확인해 원인을 알려 주는 메시지를 만든다."""
    msgs = []
    if st.get("krx_id") and st.get("krx_pw"):
        try:
            from pykrx.website.comm import auth
            if auth.get_auth_session() is None:
                msgs.append("KRX 로그인에 실패했습니다. 설정의 아이디/비밀번호를 data.krx.co.kr에서 직접 로그인해 확인하세요.")
        except Exception as e:
            msgs.append(f"KRX 로그인 확인 중 오류: {type(e).__name__} {e}")
    else:
        msgs.append("설정 화면에 한국거래소(data.krx.co.kr) 무료 회원 아이디/비밀번호를 입력하세요.")
    return msgs


def sync_prices(stock, dates, log):
    with db.conn() as c:
        have = {r[0] for r in c.execute("SELECT date FROM price_days")}
    need = [d for d in dates if d not in have]
    fails = []
    for i, d in enumerate(need):
        if i % 10 == 0:
            log(f"일별 시세 저장 중 {i + 1}/{len(need)} ({d})")
        try:
            df = _retry(stock.get_market_ohlcv, d, market="ALL")
            if df is None or df.empty or "종가" not in df.columns:
                raise RuntimeError("응답이 비어 있음")
        except Exception as e:
            fails.append(f"{d}: {type(e).__name__} {e}")
            # 처음 5일이 연속으로 실패하면 접속 문제로 보고 중단
            if len(fails) >= 5 and len(fails) == i + 1:
                raise RuntimeError("; ".join(fails[:2]))
            continue
        rows = [(d, str(tk), float(r["시가"]), float(r["고가"]), float(r["저가"]), float(r["종가"]),
                 float(r["거래량"]), float(r.get("거래대금", 0) or 0)) for tk, r in df.iterrows()]
        with db.conn() as c:
            c.executemany("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?,?)", rows)
            c.execute("INSERT OR REPLACE INTO price_days VALUES(?, ?)", (d, db.now()))


def load_panel(dates):
    with db.conn() as c:
        q = f"SELECT * FROM prices WHERE date IN ({','.join('?' * len(dates))})"
        df = pd.read_sql_query(q, c, params=dates)
    df["date"] = pd.to_datetime(df["date"])
    panel = {}
    for col in ("open", "high", "low", "close", "volume", "value"):
        p = df.pivot(index="date", columns="ticker", values=col).sort_index()
        panel[col] = p
    # 거래정지일은 시가·고가·저가가 0 → 종가로 대체
    for col in ("open", "high", "low"):
        panel[col] = panel[col].where(panel[col] > 0, panel["close"])
    panel["close"] = panel["close"].where(panel["close"] > 0)
    return panel


def collect(st, log=print, with_dart=True):
    from pykrx import stock
    _login(st)
    warnings, sources = [], {}
    today = datetime.now().strftime("%Y%m%d")
    log("KRX 로그인 확인 중…")
    login_msgs = krx_check(stock, st)
    log("거래일 확인 중 (지수)…")
    all_dates, index_close = trading_dates(stock, today, log, warnings)
    base = all_dates[-1]
    n_hist = int(st.get("history_days", 300))
    dates = all_dates[-n_hist:]
    try:
        sync_prices(stock, dates, log)
    except Exception as e:
        raise RuntimeError(f"KRX 일별 시세 조회 실패: {type(e).__name__} {e}. " + " ".join(login_msgs))
    panel = load_panel(dates)
    sources["가격"] = f"KRX 일별 시세, 기준일 {base}"

    log("종목 정보·밸류에이션 수집 중…")
    frames = []
    for mk in ("KOSPI", "KOSDAQ"):
        try:
            cap = _retry(stock.get_market_cap, base, market=mk)
            fun = _retry(stock.get_market_fundamental, base, market=mk)
            if cap is None or cap.empty or "시가총액" not in cap.columns:
                raise RuntimeError("시가총액 응답이 비어 있음")
        except Exception as e:
            raise RuntimeError(f"{mk} 시가총액·PER 조회 실패: {type(e).__name__} {e}")
        tks = list(cap.index)
        try:
            sec = _retry(stock.get_market_sector_classifications, base, mk)
            if "종목코드" in sec.columns:
                sec = sec.set_index("종목코드")
        except Exception as e:
            warnings.append(f"{mk} 업종 분류 실패: {e}")
            sec = pd.DataFrame(columns=["종목명", "업종명"])
        df = pd.DataFrame(index=pd.Index([str(t) for t in tks], name="ticker"))
        df["market"] = mk
        df["name"] = sec["종목명"].reindex(df.index) if "종목명" in sec else None
        df["sector"] = sec["업종명"].reindex(df.index) if "업종명" in sec else None
        df["mcap"] = cap["시가총액"].reindex(df.index)
        df["shares"] = cap["상장주식수"].reindex(df.index)
        for k, col in (("bps", "BPS"), ("per", "PER"), ("pbr", "PBR"), ("eps", "EPS"), ("div", "DIV"), ("dps", "DPS")):
            df[k] = fun[col].reindex(df.index) if fun is not None and col in fun else None
        frames.append(df)
    info = pd.concat(frames)
    for tk in info.index[info["name"].isna()]:
        try:
            info.at[tk, "name"] = stock.get_market_ticker_name(tk)
        except Exception:
            info.at[tk, "name"] = tk
    sources["밸류에이션"] = f"KRX PER/PBR/배당, 기준일 {base}"

    log("외국인·기관 수급 수집 중…")
    spec = {"f_net5": ("외국인", 5), "f_net20": ("외국인", 20), "i_net20": ("기관합계", 20),
            "i_net60": ("기관합계", 60), "pension_net20": ("연기금", 20)}
    for col, (inv, n) in spec.items():
        s = pd.Series(dtype=float)
        for mk in ("KOSPI", "KOSDAQ"):
            try:
                df = _retry(stock.get_market_net_purchases_of_equities, dates[-n], base, mk, inv)
                s = pd.concat([s, df["순매수거래대금"].astype(float)])
            except Exception as e:
                warnings.append(f"{mk} {inv} {n}일 수급 실패: {e}")
        s.index = s.index.astype(str)
        info[col] = s.reindex(info.index).fillna(0) if len(s) else None
    mf = {}
    for mk in ("KOSPI", "KOSDAQ"):
        try:
            df = _retry(stock.get_market_trading_value_by_date, dates[-20], base, mk)
            mf[mk] = float(df["외국인합계"].sum())
        except Exception as e:
            warnings.append(f"{mk} 시장 외국인 수급 실패: {e}")
    sources["수급"] = f"KRX 투자자별 순매수, {dates[-20]}~{base}"

    log("외국인 지분율 수집 중…")
    for col, off in (("foreign_now", 1), ("foreign_3m", 64), ("foreign_6m", 127)):
        d = all_dates[-off]
        s = pd.Series(dtype=float)
        for mk in ("KOSPI", "KOSDAQ"):
            try:
                v = _cached(f"foreign:{d}:{mk}",
                            lambda: _frame_to_dict(_retry(stock.get_exhaustion_rates_of_foreign_investment, d, mk), ["지분율"]),
                            permanent=off > 1)
                s = pd.concat([s, pd.Series(v.get("지분율", {}))])
            except Exception as e:
                warnings.append(f"{mk} 외국인 지분율({d}) 실패: {e}")
        info[col] = s.reindex(info.index) if len(s) else None

    log("과거 밸류에이션·배당 이력 수집 중…")
    pbr_hist, dps_hist = {}, {}
    samples = []
    for y in range(int(base[:4]) - 5, int(base[:4]) + 1):
        for m in (3, 6, 9, 12):
            cand = [d for d in all_dates if d[:6] == f"{y}{m:02d}"]
            if cand and cand[-1] < base:
                samples.append(cand[-1])
    for d in samples:
        for mk in ("KOSPI", "KOSDAQ"):
            try:
                v = _cached(f"fund:{d}:{mk}", lambda: _frame_to_dict(_retry(stock.get_market_fundamental, d, market=mk), ["PBR", "DPS"]))
            except Exception as e:
                warnings.append(f"과거 밸류에이션({d}) 실패: {e}")
                continue
            for tk, x in v.get("PBR", {}).items():
                pbr_hist.setdefault(tk, {})[d] = x
            if d[4:6] == "06":
                for tk, x in v.get("DPS", {}).items():
                    dps_hist.setdefault(tk, {})[int(d[:4]) - 1] = x
    pbr_df = pd.DataFrame(pbr_hist).T if pbr_hist else None
    dps_df = pd.DataFrame(dps_hist).T if dps_hist else None
    last_fy = int(base[:4]) - 1
    if dps_df is not None and last_fy not in dps_df.columns:
        dps_df[last_fy] = info["dps"].reindex(dps_df.index)
    # 5년 전 상장주식수 (L20)
    d5 = [d for d in all_dates if d <= f"{int(base[:4]) - 5}{base[4:]}"]
    if d5:
        s = pd.Series(dtype=float)
        for mk in ("KOSPI", "KOSDAQ"):
            try:
                v = _cached(f"cap:{d5[-1]}:{mk}", lambda: _frame_to_dict(_retry(stock.get_market_cap, d5[-1], market=mk), ["상장주식수"]))
                s = pd.concat([s, pd.Series(v.get("상장주식수", {}))])
            except Exception as e:
                warnings.append(f"5년 전 주식수 실패: {e}")
        info["shares_5y"] = s.reindex(info.index) if len(s) else None

    admin = set()
    try:
        import FinanceDataReader as fdr
        adm = fdr.StockListing("KRX-ADMIN")
        admin = set(adm["Symbol"].astype(str))
        sources["관리종목"] = "KIND 관리종목 목록"
    except Exception as e:
        warnings.append(f"관리종목 목록 조회 실패(K1 일부 미확인): {e}")
    fx = None
    try:
        import FinanceDataReader as fdr
        fx = fdr.DataReader("USD/KRW", (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d"))["Close"]
    except Exception as e:
        warnings.append(f"환율 조회 실패(S18 ④ 제외): {e}")

    fin = {}
    if with_dart and st.get("dart_key"):
        try:
            from . import dart
            log("DART 재무제표 수집 중 (첫 실행은 오래 걸립니다)…")
            fin = dart.collect(st["dart_key"], list(info.index), base, log)
            sources["재무"] = "OpenDART 다중회사 주요계정"
        except Exception as e:
            warnings.append(f"DART 재무 수집 실패: {e}")
    elif with_dart:
        warnings.append("DART API 키가 없어 재무 항목(M01~M04, L01~L08 등)은 N/A 처리됨")

    tickers = [t for t in info.index if t in panel["close"].columns]
    info = info.loc[tickers]
    return Bundle(
        base_date=base, close=panel["close"], open=panel["open"], high=panel["high"], low=panel["low"],
        volume=panel["volume"], value=panel["value"], info=info,
        index_close={k: v.iloc[-n_hist:] for k, v in index_close.items()}, market_foreign20=mf, fx=fx,
        dps_hist=dps_df, pbr_hist=pbr_df, fin=fin, admin=admin, sources=sources, warnings=warnings)
