import os, json, itertools
import numpy as np
import pandas as pd

SCR = os.path.dirname(os.path.abspath(__file__))
df = pd.read_pickle(os.path.join(SCR, "panel.pkl"))
df = df[df.grade != "X"].copy()          # 제외 필터 해당 종목은 앱에서도 투자 대상이 아님
H = (1, 3, 5, 10)
SITEMS = [f"S{i:02d}" for i in range(1, 21) if f"S{i:02d}" in df and df[f"S{i:02d}"].notna().mean() > 0.5]
SPLIT = os.environ.get("SPLIT", "20260701")
R = {}
out = []
P = lambda *a: out.append(" ".join(str(x) for x in a))


def stat(g, h=5):
    x, e = g[f"f{h}"].dropna(), g[f"x{h}"].dropna()
    if len(x) == 0:
        return dict(n=0)
    return dict(n=len(x), dates=g.loc[x.index, "date"].nunique(), avg=x.mean(), med=x.median(),
                win=(x > 0).mean() * 100, exc=e.mean(), mae=g.loc[x.index, "mae10"].mean())


def tbl(groups, h=5):
    rows = []
    for k, g in groups:
        s = stat(g, h)
        s["key"] = k
        rows.append(s)
    return pd.DataFrame(rows).set_index("key")


base = df[df.f10.notna()]
P("표본:", len(base), "종목·일, 날짜", base.date.nunique(), base.date.min(), "~", base.date.max(),
  "종목수/일 평균", round(base.groupby("date").size().mean()))
for h in H:
    P(f"전체 평균 {h}일 순수익(비용차감)", round(base[f"f{h}"].mean(), 3), "승률", round((base[f"f{h}"] > 0).mean() * 100, 1))

# 1) 일별 순위상관(IC): 점수 vs 이후 수익
def ic(col, h, d=base):
    s = d.groupby("date").apply(lambda g: g[col].rank().corr(g[f"f{h}"].rank()) if g[col].notna().sum() > 30 else np.nan).dropna()
    return s.mean(), s.mean() / s.std() * np.sqrt(len(s)) if len(s) > 2 else np.nan, (s > 0).mean() * 100, len(s)

P("\n## IC (일별 스피어만 평균, t값, 양(+)인 날 %, 날짜수)")
icrows = []
for col in ["final", "s_short", "d1", "d5"] + SITEMS:
    r = {"item": col}
    for h in H:
        m, t, pp, n = ic(col, h)
        r[f"ic{h}"], r[f"t{h}"], r[f"pos{h}"] = m, t, pp
    icrows.append(r)
ICT = pd.DataFrame(icrows).set_index("item")
P(ICT.round(3).to_string())

# 2) 점수 구간별
bins = [0, 50, 55, 60, 65, 70, 75, 80, 101]
base = base.assign(fb=pd.cut(base.final, bins, right=False))
P("\n## 점수 구간별 (5일)")
P(tbl(base.groupby("fb", observed=True), 5).round(2).to_string())
P("\n## 점수 구간별 (10일)")
P(tbl(base.groupby("fb", observed=True), 10).round(2).to_string())
base = base.assign(dec=base.groupby("date").final.transform(lambda s: pd.qcut(s.rank(method="first"), 10, labels=False) + 1))
for h in H:
    P(f"\n## 일별 점수 10분위 ({h}일)")
    P(tbl(base.groupby("dec"), h).round(2).to_string())

# 3) 점수 변화
P("\n## 5일 점수 변화 구간 (5일 수익)")
base = base.assign(d5b=pd.cut(base.d5, [-100, -10, -5, -2, 2, 5, 10, 100]))
P(tbl(base.groupby("d5b", observed=True), 5).round(2).to_string())
P("\n## 5일 점수 변화 구간 (10일 수익)")
P(tbl(base.groupby("d5b", observed=True), 10).round(2).to_string())

# 4) 항목 점수별 5일 성과
P("\n## 항목별 점수 → 5일 초과수익(exc) / 승률")
for it in SITEMS:
    t = tbl(base.groupby(it), 5)
    P(it, " | ".join(f"{int(k)}점:n{int(r.n)} exc{r.exc:+.2f} win{r.win:.0f}" for k, r in t.iterrows()))

# 5) 저점 진입 정의: 직전 20일 고점 대비 -10% 이상 하락했거나 이격도 <100 (S02 원값), 즉 '눌림'
base = base.assign(dip=(base.dd20 <= -8))
P("\n## 눌림(20일 고점 대비 -8% 이하) 여부 × 점수 구간 (5일)")
P(tbl(base.groupby(["dip", "fb"], observed=True), 5).round(2).to_string())

# 6) 조건 조합 탐색: 앞 기간(발견) → 뒤 기간(검증)
conds = {}
for it in SITEMS:
    vals = sorted(base[it].dropna().unique())
    for v in vals:
        if (base[it] >= v).mean() < 0.95 and (base[it] >= v).mean() > 0.03:
            conds[f"{it}>={int(v)}"] = base[it] >= v
        if (base[it] <= v).mean() < 0.95 and (base[it] <= v).mean() > 0.03:
            conds[f"{it}<={int(v)}"] = base[it] <= v
for th in (60, 65, 70, 75):
    conds[f"U>={th}"] = base.final >= th
for th in (-5, -2, 2, 5):
    conds[f"dU5{'>=' if th > 0 else '<='}{th}"] = (base.d5 >= th) if th > 0 else (base.d5 <= th)
for th in (-5, -8, -12, -15):
    conds[f"dd20<={th}"] = base.dd20 <= th
conds["r5<=-5"] = base.r5 <= -5
conds["r5>=5"] = base.r5 >= 5
names = list(conds)
early = (base.date < SPLIT).values
late = ~early
y = base["x5"].values
yr = base["f5"].values
res = []
for k in (1, 2):
    for combo in itertools.combinations(names, k):
        if k == 2 and combo[0].split(">")[0].split("<")[0] == combo[1].split(">")[0].split("<")[0]:
            continue
        m = np.logical_and.reduce([conds[c].values for c in combo])
        me, ml = m & early, m & late
        if me.sum() < 150 or ml.sum() < 100:
            continue
        de = base.date.values[me]
        if len(np.unique(de)) < 20:
            continue
        res.append(dict(rule=" & ".join(combo), n_e=int(me.sum()), exc_e=np.nanmean(y[me]), ret_e=np.nanmean(yr[me]),
                        win_e=np.nanmean(yr[me] > 0) * 100, n_l=int(ml.sum()), exc_l=np.nanmean(y[ml]),
                        ret_l=np.nanmean(yr[ml]), win_l=np.nanmean(yr[ml] > 0) * 100,
                        dates_l=len(np.unique(base.date.values[ml]))))
RS = pd.DataFrame(res)
RS.to_pickle(os.path.join(SCR, "rules.pkl"))
P("\n## 조건 탐색 (발견기간 초과수익 상위 25, 검증기간 결과 병기)", "발견<", SPLIT, "<=검증")
P(RS.sort_values("exc_e", ascending=False).head(25).round(2).to_string())
P("\n## 조건 탐색 (두 기간 모두 양(+) 초과수익, 최소값 기준 상위 25)")
RS["minexc"] = RS[["exc_e", "exc_l"]].min(axis=1)
P(RS.sort_values("minexc", ascending=False).head(25).round(2).to_string())
P("\n## 피해야 할 조건 (두 기간 모두 음(-), 최대값 기준 하위 15)")
RS["maxexc"] = RS[["exc_e", "exc_l"]].max(axis=1)
P(RS.sort_values("maxexc").head(15).round(2).to_string())

# 7) 시장 타이밍: 날짜별 평균 U 점수·A등급 수 vs 다음 5·10일 전체 평균 수익
day = base.groupby("date").agg(avgU=("final", "mean"), nA=("final", lambda s: (s >= 80).sum()),
                               n70=("final", lambda s: (s >= 70).sum()), f5=("f5", "mean"), f10=("f10", "mean"),
                               s18=("S18", "mean"))
day["dU5"] = day.avgU - day.avgU.shift(5)
P("\n## 날짜별 시장 상태 vs 전 종목 평균 이후 수익 (상관)")
P(day[["avgU", "n70", "dU5", "s18"]].rank().corrwith(day.f5.rank()).round(3).to_dict(), "(5일)")
P(day[["avgU", "n70", "dU5", "s18"]].rank().corrwith(day.f10.rank()).round(3).to_dict(), "(10일)")
day.to_pickle(os.path.join(SCR, "day.pkl"))
base.to_pickle(os.path.join(SCR, "base.pkl"))
open(os.path.join(SCR, "analysis.txt"), "w").write("\n".join(out))
print("\n".join(out))
