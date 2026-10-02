"""화면 확인용 가상 데이터. 실제 종목·시세가 아니다 (종목명 앞에 '데모'가 붙는다)."""
import numpy as np
import pandas as pd

from .bundle import Bundle

SECTORS = ["전기전자", "화학", "운송장비", "의약품", "서비스업", "금융업", "유통업", "기계", "철강금속", "음식료품"]


def make_bundle(n=300, days=300, seed=7):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=days)
    tickers = [f"D{i:05d}" for i in range(n)]  # 실제 종목코드와 겹치지 않게
    drift = rng.normal(0.0004, 0.0012, n)
    vol = rng.uniform(0.012, 0.035, n)
    rets = rng.normal(drift, vol, (days, n))
    start = rng.uniform(2000, 200000, n).round(-1)
    close = pd.DataFrame(start * np.exp(np.cumsum(rets, axis=0)), index=dates, columns=tickers).round(0)
    spread = np.abs(rng.normal(0, 0.01, (days, n)))
    high = close * (1 + spread)
    low = close * (1 - spread)
    opn = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.005, (days, n)))
    shares = rng.uniform(5e6, 3e8, n)
    base_vol = shares * rng.uniform(0.001, 0.01, n)
    volume = pd.DataFrame(base_vol * rng.lognormal(0, 0.4, (days, n)), index=dates, columns=tickers).round(0)
    value = volume * close
    mcap = close.iloc[-1].values * shares
    market = np.where(rng.random(n) < 0.45, "KOSPI", "KOSDAQ")
    sector = rng.choice(SECTORS, n)
    eps = rng.normal(0.07, 0.08, n) * start
    bps = start * rng.uniform(0.4, 1.5, n)
    per = np.where(eps > 0, close.iloc[-1].values / np.where(eps == 0, 1, eps), 0)
    info = pd.DataFrame({
        "name": [f"데모{s}{i:03d}" for i, s in zip(range(n), sector)], "market": market, "sector": sector,
        "mcap": mcap, "shares": shares, "per": per, "pbr": close.iloc[-1].values / bps, "eps": eps, "bps": bps,
        "div": rng.uniform(0, 5, n), "dps": rng.uniform(0, 3000, n).round(-1),
        "f_net5": mcap * rng.normal(0, 0.002, n), "f_net20": mcap * rng.normal(0, 0.006, n),
        "i_net20": mcap * rng.normal(0, 0.004, n), "i_net60": mcap * rng.normal(0, 0.008, n),
        "pension_net20": mcap * rng.normal(0, 0.001, n),
        "foreign_now": rng.uniform(1, 50, n), "short_ratio": rng.exponential(0.6, n),
        "short_bal": shares * rng.uniform(0, 0.01, n), "shares_5y": shares * rng.uniform(0.8, 1.05, n),
    }, index=tickers)
    info["foreign_3m"] = info["foreign_now"] - rng.normal(0, 1.2, n)
    info["foreign_6m"] = info["foreign_now"] - rng.normal(0, 2, n)
    info["short_bal_20"] = info["short_bal"] * rng.uniform(0.7, 1.3, n)
    idx = {}
    for mk in ("KOSPI", "KOSDAQ"):
        cols = [t for t, m in zip(tickers, market) if m == mk]
        idx[mk] = (close[cols].pct_change().mean(axis=1).fillna(0) + 1).cumprod() * (2600 if mk == "KOSPI" else 850)
    fin = {}
    y0 = dates[-1].year
    for i, tk in enumerate(tickers[: int(n * 0.8)]):
        rev0 = mcap[i] * rng.uniform(0.1, 0.6) / 4
        g = rng.normal(0.02, 0.05)
        opm = rng.uniform(-0.02, 0.2)
        qs = []
        for k in range(10):
            y, q = y0 - 2 + (k // 4), k % 4 + 1
            rev = rev0 * (1 + g) ** k * rng.uniform(0.9, 1.1)
            op = rev * (opm + rng.normal(0, 0.02))
            qs.append({"label": f"{y}Q{q}", "rev": rev, "op": op, "ni": op * 0.75})
        eq = mcap[i] * rng.uniform(0.3, 1.2)
        annual = {}
        for y in range(y0 - 6, y0):
            rv = rev0 * 4 * (1 + g) ** (4 * (y - y0 + 2)) * rng.uniform(0.9, 1.1)
            op = rv * (opm + rng.normal(0, 0.02))
            annual[y] = {"rev": rv, "op": op, "ni": op * 0.75, "equity": eq * (1 + 0.05 * (y - y0)),
                         "liab": eq * rng.uniform(0.2, 2.5), "capital": eq * 0.1}
        fin[tk] = {"quarters": qs[:7], "annual": annual, "latest_bs": annual[y0 - 1]}
    dps_hist = pd.DataFrame({y: info["dps"] * rng.uniform(0.7, 1.05, n) for y in range(y0 - 5, y0)}, index=tickers)
    pbr_hist = pd.DataFrame({f"s{k}": info["pbr"] * rng.uniform(0.6, 1.5, n) for k in range(20)}, index=tickers)
    fx = pd.Series(1350 + np.cumsum(rng.normal(0, 3, 60)), index=dates[-60:])
    return Bundle(base_date=dates[-1].strftime("%Y%m%d"), close=close, open=opn, high=high, low=low,
                  volume=volume, value=value, info=info, index_close=idx,
                  market_foreign20={"KOSPI": 3.2e11, "KOSDAQ": -5e10}, fx=fx, dps_hist=dps_hist,
                  pbr_hist=pbr_hist, fin=fin, sources={"가격": "데모(가상) 데이터"},
                  warnings=["데모 데이터입니다. 실제 시세가 아닙니다."], demo=True)
