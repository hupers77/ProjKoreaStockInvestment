"""재채점 결과 + 이후 수익률 → panel.pkl"""
import glob, os, sqlite3
import numpy as np
import pandas as pd

SCR = os.path.dirname(os.path.abspath(__file__))
COST = 0.30  # 왕복 거래비용 % (수수료+증권거래세+슬리피지 가정)
H = (1, 3, 5, 10)

df = pd.concat([pd.read_pickle(p) for p in sorted(glob.glob(os.path.join(SCR, "out/*.pkl")))], ignore_index=True)
con = sqlite3.connect(os.path.join(SCR, "data/stock.db"))
days = [r[0] for r in con.execute("SELECT date FROM price_days ORDER BY date")]
start = days[days.index(df.date.min()) - 1]
px = pd.read_sql_query("SELECT date,ticker,open,high,low,close FROM prices WHERE date>=?", con, params=(start,))
for c in ("open", "high", "low"):
    px[c] = px[c].where(px[c] > 0, px["close"])
px["close"] = px["close"].where(px["close"] > 0)
P = {c: px.pivot(index="date", columns="ticker", values=c).sort_index() for c in ("open", "high", "low", "close")}
idx = P["close"].index
pos = {d: i for i, d in enumerate(idx)}

# 진입: 신호일 다음 거래일 시가, 청산: h거래일째 종가 (h=1이면 다음날 시가→종가)
op1 = P["open"].shift(-1)
fwd = {}
for h in H:
    fwd[h] = (P["close"].shift(-h) / op1 - 1) * 100 - COST
# 10일 내 최저가(진입가 대비) = 최대 역행폭, 최고가 = 최대 순행폭
lowmin = pd.concat([P["low"].shift(-k) for k in range(1, 11)]).groupby(level=0).min()
himax = pd.concat([P["high"].shift(-k) for k in range(1, 11)]).groupby(level=0).max()
low5 = pd.concat([P["low"].shift(-k) for k in range(1, 6)]).groupby(level=0).min()
mae10 = (lowmin / op1 - 1) * 100
mfe10 = (himax / op1 - 1) * 100
mae5 = (low5 / op1 - 1) * 100
# 과거 맥락: 20일 고점 대비 하락률, 5일·20일 수익률
c = P["close"]
dd20 = (c / P["high"].rolling(20, min_periods=10).max() - 1) * 100
r5 = (c / c.shift(5) - 1) * 100
r20 = (c / c.shift(20) - 1) * 100


def stack(frame, name):
    s = frame.stack()
    s.name = name
    return s


extra = pd.concat([stack(fwd[h], f"f{h}") for h in H] +
                  [stack(mae10, "mae10"), stack(mfe10, "mfe10"), stack(mae5, "mae5"), stack(dd20, "dd20"),
                   stack(r5, "r5"), stack(r20, "r20")], axis=1)
extra.index.names = ["date", "ticker"]
df = df.merge(extra.reset_index(), on=["date", "ticker"], how="left")
# 점수 변화 (같은 종목의 1·5거래일 전 재채점 점수 대비)
df = df.sort_values(["ticker", "date"])
g = df.groupby("ticker")["final"]
df["d1"] = df["final"] - g.shift(1)
df["d5"] = df["final"] - g.shift(5)
df["ds5"] = df["s_short"] - df.groupby("ticker")["s_short"].shift(5)
# 당일 전 종목 평균 대비 초과수익 (시장 국면 효과 제거)
for h in H:
    df[f"x{h}"] = df[f"f{h}"] - df.groupby("date")[f"f{h}"].transform("mean")
df.to_pickle(os.path.join(SCR, "panel.pkl"))
print(df.shape, df.date.min(), df.date.max(), df.f10.notna().groupby(df.date).any().sum(), "dates with 10d fwd")
