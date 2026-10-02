"""평가 기준서 v1.0의 채점 규칙 구현.

각 항목 함수는 Res(점수 0~10 또는 None=N/A, 데이터값, 근거, 추정여부)를 돌려준다.
무료 데이터로 확인할 수 없는 항목은 N/A로 두고 분모에서 제외한다(기준서 2-2, 10장).
수동 입력(종목 상세 화면)이 있으면 자동 점수보다 우선한다.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind
from .bundle import Bundle
from .framework import (BONUS_CAP, BONUSES, ITEM_MAP, ITEMS, PENALTIES, PENALTY_CAP,
                        PERSPECTIVES, PROFILES, grade_of)

EOK = 1e8  # 1억 원


@dataclass
class Res:
    score: float | None
    value: str = ""
    basis: str = ""
    est: bool = False
    src: str = ""


def NA(reason):
    return Res(None, "", reason)


def f(x, d=1):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "-"
    return f"{x:,.{d}f}"


def is_num(x):
    return x is not None and not (isinstance(x, float) and np.isnan(x))


FIN_WORDS = ("금융", "은행", "보험", "증권")


class Ctx:
    """종목 1개의 채점 컨텍스트."""

    def __init__(self, tk, b: Bundle, uni):
        self.tk = tk
        self.b = b
        self.u = uni
        self.info = b.info.loc[tk]
        self.c = b.close[tk].dropna()
        idx = self.c.index
        self.o = b.open[tk].reindex(idx)
        self.h = b.high[tk].reindex(idx)
        self.l = b.low[tk].reindex(idx)
        self.v = b.volume[tk].reindex(idx).fillna(0)
        self.val = b.value[tk].reindex(idx).fillna(0)
        self.market = self.info.get("market", "KOSPI")
        ic = b.index_close.get(self.market)
        self.ix = ic.reindex(idx).ffill() if ic is not None else None
        self.fin = b.fin.get(tk)
        self.sector = self.info.get("sector") or "기타"
        self.is_fin = any(w in str(self.sector) for w in FIN_WORDS)
        self.mcap = self.info.get("mcap")
        self.n = len(self.c)
        self.cache = {}

    def ma(self, n):
        k = ("ma", n)
        if k not in self.cache:
            self.cache[k] = ind.sma(self.c, n)
        return self.cache[k]

    def get(self, key):
        v = self.info.get(key)
        return v if is_num(v) else None


# ───────────────────────── 단기 ─────────────────────────

def S01(t: Ctx):
    if t.n < 130:
        return NA("가격 이력 130일 미만")
    c = t.c.iloc[-1]
    m5, m20, m60, m120 = (t.ma(n).iloc[-1] for n in (5, 20, 60, 120))
    m20s = t.ma(20)
    rising = m20s.iloc[-1] > m20s.iloc[-6]
    full = (t.c > t.ma(5)) & (t.ma(5) > t.ma(20)) & (t.ma(20) > t.ma(60)) & (t.ma(60) > t.ma(120))
    held10 = bool(full.iloc[-10:].all())
    val = f"종가 {f(c,0)} / 5일 {f(m5,0)} / 20일 {f(m20,0)} / 60일 {f(m60,0)} / 120일 {f(m120,0)}"
    if c > m5 > m20 > m60 > m120 and rising and held10:
        return Res(10, val, "완전 정배열 10일 이상 유지, 20일선 상승")
    if c > m20 > m60 > m120:
        return Res(8, val, "종가>20>60>120일선 정배열")
    if c < m5 < m20 < m60 < m120:
        return Res(0, val, "완전 역배열")
    if c > m20 and m20 > m60:
        return Res(6, val, "20일선 위, 20>60 (120일선 미정렬, 전환 초기)")
    if c > m20:
        return Res(5, val, "20일선 위이나 20<60 (6점·4점 사이)")
    if m60 < c < m20:
        return Res(4, val, "20일선 아래·60일선 위 (상승 추세 내 조정)")
    if c < m60 and m20 < m60:
        return Res(2, val, "60일선 아래, 20<60")
    return Res(3, val, "60일선 아래이나 20>60 (혼조)")


def S02(t: Ctx):
    if t.n < 20:
        return NA("가격 이력 부족")
    d = t.c.iloc[-1] / t.ma(20).iloc[-1] * 100
    if 100 <= d < 105: s = 10
    elif 105 <= d < 110 or 96 <= d < 100: s = 8
    elif 110 <= d < 115 or 93 <= d < 96: s = 6
    elif 115 <= d < 120 or 90 <= d < 93: s = 4
    elif 120 <= d <= 130 or d < 90: s = 2
    else: s = 0
    return Res(s, f"이격도 {f(d)}", "종가 ÷ 20일선 × 100")


def _b5(x):
    if x >= 15: return 5
    if x >= 5: return 4
    if x > 0: return 3
    if x >= -5: return 2
    if x >= -15: return 1
    return 0


def S03(t: Ctx):
    if t.ix is None or t.n < 64:
        return NA("지수 또는 가격 이력 부족")
    r1, r3 = ind.ret(t.c, 21), ind.ret(t.c, 63)
    i1, i3 = ind.ret(t.ix, 21), ind.ret(t.ix, 63)
    if None in (r1, r3, i1, i3):
        return NA("수익률 계산 불가")
    e1, e3 = r1 - i1, r3 - i3
    pct = t.u["rs_pct"].get(t.tk)
    b1, b3 = _b5(e1), _b5(e3)
    val = f"1개월 초과 {f(e1)}%p / 3개월 초과 {f(e3)}%p"
    if (b1 == 5 and b3 == 5) or (pct is not None and pct >= 0.9 and e1 > 0 and e3 > 0):
        return Res(10, val, "1·3개월 초과수익 모두 +15%p 이상 또는 시장 상위 10%")
    if b1 >= 4 and b3 >= 4:
        return Res(8, val, "둘 다 +5%p 이상")
    if (b1 >= 3 and b3 >= 2) or (b3 >= 3 and b1 >= 2):
        return Res(6, val, "하나는 플러스, 다른 하나는 −5%p 이내")
    if b1 == 2 and b3 == 2:
        return Res(4, val, "둘 다 0 ~ −5%p")
    if b1 == 1 and b3 == 1:
        return Res(2, val, "둘 다 −5 ~ −15%p")
    if b1 == 0 and b3 == 0:
        return Res(0, val, "둘 다 −15%p 이하")
    return Res(min(b1, b3) * 2 + 1, val, "구간 혼재 → 사이 홀수 점수(보수적)")


def S04(t: Ctx):
    if t.n < 40:
        return NA("가격 이력 부족")
    r = ind.rsi(t.c)
    x, x3 = r.iloc[-1], r.iloc[-4]
    up = x > x3
    val = f"RSI {f(x)} (3일 전 {f(x3)})"
    if 55 <= x < 65:
        return Res(10 if up else 8, val, "55~65" + (" 상승 중" if up else " (상승 아님)"))
    if 50 <= x < 55 or 65 <= x < 70:
        return Res(8, val, "50~55 또는 65~70")
    if 45 <= x < 50:
        return Res(6 if up else 4, val, "45~50" + (" 3일 전보다 상승" if up else " 상승 아님"))
    if 70 <= x < 75 or 40 <= x < 45:
        return Res(4, val, "70~75 또는 40~45")
    if 75 <= x <= 80 or 30 <= x < 40:
        return Res(2, val, "75~80 또는 30~40")
    if x > 80:
        return Res(0, val, "80 초과 과열")
    return Res(0 if not up else 2, val, "30 미만" + (" 하락 지속" if not up else " 반등 시도") + " (다이버전스는 수동 확인)")


def S05(t: Ctx):
    if t.n < 60:
        return NA("가격 이력 부족")
    m, s, h = ind.macd(t.c)
    mv, sv = m.iloc[-1], s.iloc[-1]
    hh = h.iloc[-4:].values
    above = mv > sv
    cross = (m > s) & (m.shift(1) <= s.shift(1))
    recent_gc = bool(cross.iloc[-10:].any())
    val = f"MACD {f(mv,2)} / 시그널 {f(sv,2)} / 히스토그램 {f(hh[-1],2)}"
    if above:
        if mv > 0 and sv > 0 and hh[3] > hh[2] > hh[1] > hh[0]:
            return Res(10, val, "MACD>시그널, 0선 위, 히스토그램 3일 연속 확대")
        if hh[3] < hh[2]:
            return Res(4, val, "MACD>시그널이나 히스토그램 축소")
        if mv > 0 and recent_gc:
            return Res(8, val, "0선 위 골든크로스 후 10거래일 이내")
        if mv < 0:
            return Res(6, val, "0선 아래 골든크로스 구간(바닥 전환)")
        return Res(6, val, "0선 위 MACD>시그널 유지")
    if mv > 0:
        return Res(2, val, "0선 위 데드크로스")
    if hh[3] < hh[2]:
        return Res(0, val, "0선 아래 데드크로스, 히스토그램 확대")
    return Res(1, val, "0선 아래 데드크로스, 히스토그램 축소")


def S06(t: Ctx):
    if t.n < 145:
        return NA("가격 이력 부족")
    mid, up, lo = ind.bollinger(t.c)
    width = (up - lo) / mid
    w120 = width.iloc[-120:]
    pct_now = (w120 < width.iloc[-1]).mean()
    pct_min5 = (w120 < width.iloc[-6:-1].min()).mean()
    c = t.c.iloc[-1]
    vavg = t.v.iloc[-21:-1].mean()
    vratio = t.v.iloc[-1] / vavg if vavg > 0 else 0
    box_hi = t.h.iloc[-43:-3].max()
    val = f"밴드폭 백분위 {f(pct_now*100,0)}% / 40일 박스 고점 {f(box_hi,0)} / 당일 거래량 {f(vratio,1)}배"
    if pct_min5 <= 0.2 and c > up.iloc[-1] and vratio >= 2:
        return Res(10, val, "수축(하위 20%) 후 상단밴드 돌파 + 거래량 2배")
    if (t.c.iloc[-3:] > box_hi).all():
        return Res(8, val, "40일 박스권 고점 돌파 후 3일 유지")
    if c < lo.iloc[-1] and vratio >= 1.5:
        return Res(0, val, "거래량 증가 동반 하단밴드 이탈")
    if pct_now <= 0.4 and width.iloc[-1] < width.iloc[-21] and c > mid.iloc[-1]:
        return Res(6, val, "밴드 수축 중, 종가 중심선 위")
    if c < mid.iloc[-1] and c <= lo.iloc[-1] + 0.15 * (up.iloc[-1] - lo.iloc[-1]):
        return Res(2, val, "중심선 아래, 하단밴드 근접")
    return Res(4, val, "뚜렷한 패턴 없음")


def S07(t: Ctx):
    if t.n < 25:
        return NA("가격 이력 부족")
    v5, v20 = t.v.iloc[-5:].mean(), t.v.iloc[-20:].mean()
    if v20 <= 0:
        return NA("거래량 없음")
    ratio = v5 / v20
    chg = t.c.diff().iloc[-10:]
    vol10 = t.v.iloc[-10:]
    upv, dnv = vol10[chg > 0].sum(), vol10[chg < 0].sum()
    r5 = ind.ret(t.c, 5) or 0
    rising = r5 > 0
    above20 = t.c.iloc[-1] > t.ma(20).iloc[-1]
    val = f"5일/20일 거래량 {f(ratio,2)}배, 5일 수익률 {f(r5)}%, 상승일/하락일 거래량 {f(upv/1e3,0)}k/{f(dnv/1e3,0)}k"
    if r5 < 0 and ratio >= 2:
        s, why = 0, "주가 하락과 함께 거래량 2배 이상"
    elif ratio >= 2 and rising and upv > dnv:
        s, why = 10, "5일/20일 2.0 이상, 상승일 거래량 우위"
    elif ratio >= 1.5 and rising:
        s, why = 8, "1.5~2.0 상승 동반"
    elif ratio >= 1.0 and rising:
        s, why = 6, "1.0~1.5 상승 동반"
    elif ratio < 0.8 and -8 <= r5 < 0 and above20:
        s, why = 6, "거래량 줄어드는 건전한 눌림"
    elif dnv > upv * 1.2 and ratio >= 1.0:
        s, why = 2, "하락일 거래량 증가(분산 징후)"
    else:
        s, why = 4, "정체·중립"
    # 최근 5일 거래량 급증일의 장대음봉·긴 윗꼬리 → −2
    vavg = t.v.iloc[-25:-5].mean()
    for i in range(-5, 0):
        o, h, c, v = t.o.iloc[i], t.h.iloc[i], t.c.iloc[i], t.v.iloc[i]
        if vavg > 0 and v >= 2 * vavg and o > 0:
            body = abs(c - o)
            big_bear = c < o and (o - c) / o >= 0.03
            long_wick = (h - max(o, c)) >= max(2 * body, 0.03 * c)
            if big_bear or long_wick:
                s = max(0, s - 2)
                why += " / 급증일 장대음봉·윗꼬리 −2"
                break
    return Res(s, val, why)


def S08(t: Ctx):
    if t.n < 20:
        return NA("가격 이력 부족")
    a = t.val.iloc[-20:].mean() / EOK
    s = 10 if a >= 500 else 8 if a >= 200 else 6 if a >= 100 else 4 if a >= 50 else 2 if a >= 20 else 0
    return Res(s, f"20일 평균 거래대금 {f(a,0)}억", "구간표 적용")


def _pct_cap(x, t):
    return x / t.mcap * 100 if t.mcap else None


def S09(t: Ctx):
    f20, f5, i20 = t.get("f_net20"), t.get("f_net5"), t.get("i_net20")
    if f20 is None or f5 is None or not t.mcap:
        return NA("외국인 수급 데이터 없음")
    p20, p5 = _pct_cap(f20, t), _pct_cap(f5, t)
    val = f"20일 {f(p20,2)}% / 5일 {f(p5,2)}% (시총 대비)"
    if p20 >= 1.0 and p5 > 0: s, why = 10, "20일 1.0% 이상 + 5일 순매수"
    elif 0.3 <= p20 and p5 > 0: s, why = 8, "20일 0.3~1.0% + 5일 순매수"
    elif abs(p20) <= 0.1: s, why = 4, "±0.1% 이내 중립"
    elif p20 > 0: s, why = 6, "20일 순매수 (5일 순매도 또는 소규모)"
    elif p20 > -0.3 and p5 > 0: s, why = 6, "20일 소폭 순매도, 5일 순매수 전환"
    elif p20 > -0.3: s, why = 3, "20일 소폭 순매도 지속"
    elif p20 > -1.0: s, why = 2, "20일 −0.3 ~ −1.0% 순매도"
    elif p5 < 0: s, why = 0, "20일 −1.0% 이상 순매도, 5일도 매도"
    else: s, why = 1, "20일 대규모 순매도, 5일 매수 전환"
    if i20 is not None and f20 > 0 and i20 > 0 and s < 10:
        s, why = s + 1, why + " / 외국인·기관 쌍끌이 +1"
    return Res(s, val, why)


def S10(t: Ctx):
    i20, f20, pen = t.get("i_net20"), t.get("f_net20"), t.get("pension_net20")
    if i20 is None or not t.mcap:
        return NA("기관 수급 데이터 없음")
    p = _pct_cap(i20, t)
    val = f"기관 20일 {f(p,2)}% (시총 대비)" + (f", 연기금 {f(pen/EOK,0)}억" if pen is not None else "")
    if p >= 0.7 and pen is not None and pen > 0: s, why = 10, "0.7% 이상 + 연기금 순매수"
    elif p >= 0.2: s, why = 8, "0.2~0.7% 순매수"
    elif abs(p) <= 0.05: s, why = 4, "중립(±0.05%)"
    elif p > 0: s, why = 6, "0~0.2% 소폭 순매수"
    elif p > -0.2: s, why = 3, "소폭 순매도"
    elif p > -0.7: s, why = 2, "−0.2 ~ −0.7% 순매도"
    else: s, why = 0, "−0.7% 이상 순매도"
    if f20 is not None and f20 > 0 and i20 > 0 and s < 10:
        s, why = s + 1, why + " / 쌍끌이 +1"
    return Res(s, val, why)


def S13(t: Ctx):
    if t.n < 240:
        return NA("52주 이력 부족")
    hi = t.h.iloc[-252:].max()
    p = t.c.iloc[-1] / hi * 100
    new_high = t.h.iloc[-5:].max() >= hi
    vr = t.v.iloc[-5:].mean() / max(t.v.iloc[-25:-5].mean(), 1)
    val = f"52주 최고가 대비 {f(p)}% (최고 {f(hi,0)})"
    if new_high and vr > 1.2:
        return Res(10, val, "5일 내 거래량 증가와 함께 신고가 갱신")
    s = 8 if p >= 95 else 6 if p >= 85 else 4 if p >= 70 else 2 if p >= 50 else 0
    return Res(s, val, "구간표 적용")


def S14(t: Ctx):
    q = (t.fin or {}).get("quarters") or []
    if len(q) < 5:
        return NA("분기 실적(DART) 없음, 컨센서스 미제공")
    cur, prev = q[-1], q[-5]
    op, op0 = cur.get("op"), prev.get("op")
    if op is None or op0 is None:
        return NA("영업이익 데이터 없음")
    val = f"{cur['label']} 영업이익 {f(op/EOK,0)}억 (전년 {f(op0/EOK,0)}억)"
    src = "컨센서스 없음 → 영업이익 YoY 대체 규칙"
    if op <= 0:
        return Res(0, val, "적자", src=src)
    if op0 <= 0:
        return Res(8, val, "흑자 전환", src=src)
    g = (op / op0 - 1) * 100
    s = 8 if g >= 50 else 6 if g >= 20 else 4 if g >= 0 else 2
    return Res(s, val + f", YoY {f(g)}%", src, src=src)


def S17(t: Ctx):
    sec = t.u["sector"].get(t.sector)
    if not sec or sec["n"] < 3:
        return NA("업종 분류 없음 또는 구성 종목 부족")
    rank_pct = sec["rank_pct"]  # 0=1위
    leader = t.tk in sec["leaders"]
    val = f"{t.sector} 1개월 {f(sec['ret'])}%, 업종 순위 상위 {f(rank_pct*100,0)}%" + (" · 대장주" if leader else "")
    if rank_pct <= 0.1 and leader: s, why = 10, "업종 상위 10% + 대장주"
    elif rank_pct <= 0.2: s, why = 8, "상위 20%"
    elif rank_pct <= 0.4: s, why = 6, "상위 40%"
    elif rank_pct < 0.7: s, why = 4, "중위권"
    elif rank_pct < 0.9 or sec["ret"] >= 0: s, why = 2, "하위 30%"
    else: s, why = 0, "하위 10% 하락 지속"
    if not leader and s > 0:
        s, why = s - 1, why + " / 대장주 아님 −1"
    return Res(s, val, why)


def S18(t: Ctx):
    env = t.u["env"].get(t.market)
    if not env:
        return NA("시장 지수 데이터 없음")
    return Res(env["score"], env["value"], env["basis"])


def S19(t: Ctx):
    if t.n < 130:
        return NA("가격 이력 부족")
    c = t.c.iloc[-1]
    est = False
    target = None
    for n in (60, 120, 252):
        hh = t.h.iloc[-n:].max()
        if hh > c * 1.03:
            target = hh
            tgt_basis = f"직전 {n}일 고점"
            break
    if target is None:
        a = ind.atr(t.h, t.l, t.c).iloc[-1]
        target = c + 3 * a
        tgt_basis = "저항선 없음(신고가권) → 종가 + 3×ATR 추정"
        est = True
    swing = t.l.iloc[-20:].min() * 0.99
    stop = max(swing, c * 0.92)
    if stop >= c:
        stop = c * 0.92
    rr = (target - c) / (c - stop)
    s = 10 if rr >= 3 else 8 if rr >= 2.5 else 6 if rr >= 2 else 4 if rr >= 1.5 else 2 if rr >= 1 else 0
    val = f"손익비 {f(rr,2)} (목표 {f(target,0)}, 손절 {f(stop,0)})"
    t.cache["target"], t.cache["stop"] = float(target), float(stop)
    return Res(s, val, f"목표가: {tgt_basis} / 손절가: 20일 스윙 저점 또는 −8% 중 높은 값", est)


# ───────────────────────── 중기 ─────────────────────────

def _fin_q(t):
    return (t.fin or {}).get("quarters") or []


def M01(t: Ctx):
    q = _fin_q(t)
    if len(q) < 5 or q[-1].get("rev") in (None, 0) or not q[-5].get("rev"):
        return NA("분기 매출(DART) 없음")
    g = (q[-1]["rev"] / q[-5]["rev"] - 1) * 100
    ttm = None
    if len(q) >= 8 and all(x.get("rev") for x in q[-8:]):
        ttm = (sum(x["rev"] for x in q[-4:]) / sum(x["rev"] for x in q[-8:-4]) - 1) * 100
    val = f"{q[-1]['label']} 매출 YoY {f(g)}%" + (f", TTM {f(ttm)}%" if ttm is not None else "")
    if g >= 30 and ttm is not None and ttm >= 20: s = 10
    elif g >= 20: s = 8
    elif g >= 10: s = 6
    elif g >= 0: s = 4
    elif g >= -10: s = 2
    else: s = 0
    return Res(s, val, "분기·TTM 매출 YoY 구간표")


def _yoy(a, b):
    if a is None or b is None or b <= 0:
        return None
    return (a / b - 1) * 100


def M02(t: Ctx):
    q = _fin_q(t)
    if len(q) < 6:
        return NA("분기 영업이익(DART) 없음")
    op, op0 = q[-1].get("op"), q[-5].get("op")
    pop, pop0 = q[-2].get("op"), q[-6].get("op")
    if op is None or op0 is None:
        return NA("영업이익 데이터 없음")
    rev = q[-1].get("rev") or 0
    g, pg = _yoy(op, op0), _yoy(pop, pop0)
    val = f"{q[-1]['label']} 영업이익 {f(op/EOK,0)}억 (전년 {f(op0/EOK,0)}억)" + (f", YoY {f(g)}%" if g is not None else "")
    if op <= 0:
        if op0 <= 0 and op > op0:
            return Res(4, val, "적자 폭 축소")
        return Res(0, val, "적자 전환 또는 적자 지속·확대")
    if op0 <= 0:
        meaningful = rev > 0 and op / rev >= 0.02
        return Res(8 if meaningful else 6, val, "흑자 전환" + ("(의미 있는 규모)" if meaningful else "(소규모)"))
    if g >= 50 and pg is not None and pg > 0: s, why = 10, "YoY 50% 이상, 2개 분기 연속 증가"
    elif g >= 30: s, why = 8, "30% 이상"
    elif g >= 10: s, why = 6, "10~30%"
    elif g >= 0: s, why = 4, "0~10%"
    elif g > -30: s, why = 2, "0 ~ −30%"
    else: s, why = 0, "−30% 이상 감소"
    return Res(s, val, why)


def M03(t: Ctx):
    q = _fin_q(t)
    if len(q) < 7:
        return NA("분기 실적(DART) 부족")

    def opm(x):
        return x["op"] / x["rev"] * 100 if x.get("rev") and x.get("op") is not None else None

    deltas = []
    for k in (1, 2, 3):
        a, b = opm(q[-k]), opm(q[-k - 4])
        deltas.append(None if a is None or b is None else a - b)
    d = deltas[0]
    if d is None:
        return NA("영업이익률 계산 불가")
    cont = all(x is not None and x > 0 for x in deltas)
    val = f"영업이익률 {f(opm(q[-1]))}% (전년 동기 {f(opm(q[-5]))}%), {f(d)}%p"
    if d >= 3 and cont: s = 10
    elif d >= 1.5: s = 8
    elif d >= 0.5: s = 6
    elif d > -0.5: s = 4
    elif d > -2: s = 2
    else: s = 0
    return Res(s, val, "YoY 영업이익률 변화" + (", 3개 분기 연속 개선" if cont else ""))


def M04(t: Ctx):
    q = _fin_q(t)
    if len(q) < 4 or any(x.get("ni") is None for x in q[-4:]):
        return NA("컨센서스 없음, 분기 순이익(DART) 부족")
    ttm = sum(x["ni"] for x in q[-4:])
    fwd = q[-1]["ni"] * 4
    if fwd <= 0:
        return Res(0, f"최근 분기 연율화 순이익 {f(fwd/EOK,0)}억", "적자 예상(연율화)", True)
    if ttm <= 0:
        return Res(6, f"TTM 적자 → 연율화 흑자", "흑자 전환 추정", True)
    g = (fwd / ttm - 1) * 100
    s = 10 if g >= 40 else 8 if g >= 25 else 6 if g >= 10 else 4 if g >= 0 else 2 if g >= -15 else 0
    return Res(s, f"연율화 순이익 / TTM − 1 = {f(g)}%", "컨센서스 없음 → 최근 분기 연율화 보수 추정", True)


def M07(t: Ctx):
    per = t.get("per")
    med = t.u["sector_per"].get(t.sector)
    if per is None or per <= 0:
        eps = t.get("eps")
        if eps is not None and eps <= 0:
            return Res(0, "PER 산출 불가(적자)", "적자로 PER 산출 불가")
        return NA("PER 데이터 없음")
    if not med:
        return NA("업종 평균 PER 없음")
    r = per / med
    s = 10 if r <= 0.6 else 8 if r <= 0.8 else 6 if r <= 1.0 else 4 if r <= 1.2 else 2 if r <= 1.5 else 0
    return Res(s, f"PER {f(per)} / 업종 중앙값 {f(med)} = {f(r,2)}", "선행 PER 대신 후행 PER 사용", True)


def _coe(t):
    m = t.mcap or 0
    return 0.08 if m >= 5e12 else 0.10 if m < 1e12 else 0.09


def M09(t: Ctx):
    pbr, eps, bps = t.get("pbr"), t.get("eps"), t.get("bps")
    if pbr is None or eps is None or not bps:
        return NA("PBR·ROE 데이터 없음")
    roe = eps / bps
    if roe <= 0:
        return Res(0, f"ROE {f(roe*100)}%", "ROE 마이너스")
    coe = _coe(t)
    fair = roe / coe
    gap = pbr / fair
    s = 10 if gap <= 0.5 else 8 if gap <= 0.7 else 6 if gap <= 0.9 else 4 if gap <= 1.1 else 2 if gap <= 1.5 else 0
    return Res(s, f"PBR {f(pbr,2)} / 적정 PBR {f(fair,2)} (ROE {f(roe*100)}%, COE {coe*100:.0f}%) = {f(gap,2)}",
               "선행 ROE 대신 후행 ROE(EPS÷BPS) 사용", True)


def M13(t: Ctx):
    if t.n < 145 or t.ix is None:
        return NA("가격 이력 부족")
    c = t.c.iloc[-1]
    m = t.ma(120)
    m120, slope = m.iloc[-1], (m.iloc[-1] / m.iloc[-21] - 1) * 100
    r6, i6 = ind.ret(t.c, 126), ind.ret(t.ix, 126)
    ex = (r6 - i6) if r6 is not None and i6 is not None else 0
    dist = (c / m120 - 1) * 100
    val = f"120일선 대비 {f(dist)}%, 기울기 {f(slope)}%, 6개월 초과수익 {f(ex)}%p"
    rising, falling = slope > 1, slope < -1
    if dist <= -20 and ex <= -20: return Res(0, val, "120일선 −20% 이하, 6개월 초과 −20%p 이하")
    if abs(dist) <= 3: return Res(4, val, "120일선 ±3% 근접")
    if c > m120 and rising and ex >= 20: return Res(10, val, "120일선 위·상승, 초과수익 +20%p 이상")
    if c > m120 and rising and ex >= 5: return Res(8, val, "120일선 위·상승, 초과수익 +5~20%p")
    if c > m120 and not falling: return Res(6, val, "120일선 위, 횡보 또는 약한 초과수익")
    if c > m120: return Res(4, val, "120일선 위이나 120일선 하락")
    if falling: return Res(2, val, "120일선 아래, 120일선 하락")
    return Res(3, val, "120일선 아래, 120일선 횡보")


def M14(t: Ctx):
    now, m3, m6 = t.get("foreign_now"), t.get("foreign_3m"), t.get("foreign_6m")
    if now is None or m3 is None:
        return NA("외국인 지분율 데이터 없음")
    d3 = now - m3
    up6 = m6 is not None and now > m6
    val = f"외국인 지분율 {f(now,2)}% (3개월 {f(d3,2)}%p" + (f", 6개월 {f(now-m6,2)}%p)" if m6 is not None else ")")
    if d3 >= 2 and up6: s = 10
    elif d3 >= 1: s = 8
    elif d3 >= 0.3: s = 6
    elif d3 > -0.3: s = 4
    elif d3 > -1: s = 2
    else: s = 0
    return Res(s, val, "3개월 지분율 변화 구간표")


def M15(t: Ctx):
    i60 = t.get("i_net60")
    if i60 is None or not t.mcap:
        return NA("기관 수급 데이터 없음")
    p = _pct_cap(i60, t)
    s = 10 if p >= 1.5 else 8 if p >= 0.5 else 6 if p >= 0 else 4 if p >= -0.2 else 2 if p > -1 else 0
    return Res(s, f"기관 3개월 순매수 {f(p,2)}% (시총 대비)", "구간표 적용")


# ───────────────────────── 장기 ─────────────────────────

def _annual(t):
    a = (t.fin or {}).get("annual") or {}
    return [a[y] | {"year": y} for y in sorted(a)]


def L01(t: Ctx):
    ys = [x for x in _annual(t) if x.get("ni") is not None and x.get("equity")]
    if len(ys) < 3:
        return NA("연간 재무(DART) 3년 미만")
    ys = ys[-5:]
    roes = [x["ni"] / x["equity"] * 100 for x in ys]
    avg, mn = float(np.mean(roes)), min(roes)
    losses = sum(1 for x in ys if x["ni"] < 0)
    val = f"{len(ys)}년 평균 ROE {f(avg)}%, 최저 {f(mn)}%"
    if losses >= 2 or avg < 3: s = 0
    elif losses == 1 or avg < 7: s = 2
    elif avg < 10: s = 4
    elif avg < 15: s = 6
    elif avg < 20: s = 8 if mn >= 8 else 6
    else: s = 10 if mn >= 12 else 8 if mn >= 8 else 6
    why = "ROE 평균·최저 구간표"
    last = ys[-1]
    if not t.is_fin and last.get("liab") and last["liab"] / last["equity"] > 2 and s > 0:
        s, why = max(0, s - 2), why + " / 부채비율 200% 초과 레버리지 착시 −2"
    return Res(s, val, why, len(ys) < 5)


def _cagr(a, b, n):
    if a is None or b is None or a <= 0 or b <= 0 or n <= 0:
        return None
    return ((a / b) ** (1 / n) - 1) * 100


def L03(t: Ctx):
    ys = [x for x in _annual(t) if x.get("rev")]
    if len(ys) < 4:
        return NA("연간 매출(DART) 4년 미만")
    ys = ys[-6:]
    n = ys[-1]["year"] - ys[0]["year"]
    g = _cagr(ys[-1]["rev"], ys[0]["rev"], n)
    if g is None:
        return NA("CAGR 계산 불가")
    s = 10 if g >= 20 else 8 if g >= 12 else 6 if g >= 7 else 4 if g >= 3 else 2 if g >= 0 else 0
    return Res(s, f"매출 {n}년 CAGR {f(g)}% ({ys[0]['year']}→{ys[-1]['year']})", "구간표 적용", n < 5)


def L04(t: Ctx):
    ys = [x for x in _annual(t) if x.get("ni") is not None]
    if len(ys) < 4:
        return NA("연간 순이익(DART) 4년 미만")
    ys = ys[-6:]
    n = ys[-1]["year"] - ys[0]["year"]
    if ys[-1]["ni"] <= 0:
        return Res(0, "최근 연도 적자", "적자", True)
    g = _cagr(ys[-1]["ni"], ys[0]["ni"], n)
    if g is None:
        return Res(2, "시작 연도 적자 → CAGR 불가", "보수적 판단", True)
    s = 10 if g >= 25 else 8 if g >= 15 else 6 if g >= 8 else 4 if g >= 3 else 2 if g >= 0 else 0
    return Res(s, f"순이익 {n}년 CAGR {f(g)}%", "지배주주 EPS 대신 순이익 CAGR 사용", True)


def L05(t: Ctx):
    ys = [x for x in _annual(t) if x.get("rev") and x.get("op") is not None][-5:]
    sec = t.u["sector_opm"].get(t.sector)
    if len(ys) < 3 or not sec:
        return NA("연간 영업이익(DART) 또는 업종 평균 부족")
    opms = [x["op"] / x["rev"] * 100 for x in ys]
    avg, sd = float(np.mean(opms)), float(np.std(opms))
    losses = sum(1 for x in ys if x["op"] < 0)
    r = avg / sec if sec > 0 else None
    val = f"{len(ys)}년 평균 영업이익률 {f(avg)}% (업종 {f(sec)}%), 표준편차 {f(sd)}%p"
    if losses >= 2: return Res(0, val, "영업적자 2회 이상")
    if r is None: return NA("업종 평균 이익률 음수")
    if r >= 2 and sd <= 3: s = 10
    elif r >= 1.5: s = 8 if sd <= 5 else 6
    elif r >= 1.0: s = 6
    elif r >= 0.85: s = 4
    else: s = 2
    return Res(s, val, "업종 평균 대비 배수·안정성")


def L08(t: Ctx):
    if t.is_fin:
        return NA("금융업 → 자본비율 대체(수동 입력)")
    ys = _annual(t)
    bs = (t.fin or {}).get("latest_bs") or (ys[-1] if ys else None)
    if not bs or not bs.get("equity") or bs.get("liab") is None:
        return NA("재무상태표(DART) 없음")
    dr = bs["liab"] / bs["equity"] * 100
    s = 10 if dr < 50 else 8 if dr < 100 else 6 if dr < 150 else 4 if dr < 200 else 2 if dr < 300 else 0
    return Res(s, f"부채비율 {f(dr,0)}%", "이자보상배율 미제공 → 부채비율만으로 추정", True)


def L10(t: Ctx):
    dh = t.b.dps_hist
    dps, div, eps = t.get("dps"), t.get("div"), t.get("eps")
    if dh is None or t.tk not in dh.index:
        return NA("배당 이력 없음")
    row = dh.loc[t.tk].dropna().sort_index()
    vals = list(row.values)
    paid = [v > 0 for v in vals]
    years = len(vals)
    consec = 0
    for p in reversed(paid):
        if p: consec += 1
        else: break
    cut = len(vals) >= 2 and vals[-1] < vals[-2] * 0.95 and vals[-2] > 0
    grow = len(vals) >= 2 and vals[-1] >= vals[0] > 0
    val = f"DPS 이력 {', '.join(f(v,0) for v in vals)} / 배당수익률 {f(div,2)}%"
    if not any(paid):
        roe = (eps / t.get("bps") * 100) if eps is not None and t.get("bps") else None
        s, why = (4, "무배당이나 재투자 ROE 양호") if roe is not None and roe >= 12 else (0, "무배당, 재투자 성과 낮음")
    elif cut:
        s, why = 2, "최근 배당 삭감"
    elif consec >= years and grow:
        s, why = 8, f"{years}개 시점 연속 배당, DPS 유지·증가 (10년 연속 여부 미확인 → 최대 8)"
    elif consec >= years:
        s, why = 6, "연속 배당이나 금액 변동"
    else:
        s, why = 4, "간헐 배당"
    if div is not None and div >= 4 and s < 10:
        s, why = s + 1, why + " / 배당수익률 4% 이상 +1"
    return Res(s, val, why)


def L11(t: Ctx):
    ys = [x for x in _annual(t) if x.get("ni")][-3:]
    dh = t.b.dps_hist
    shares = t.get("shares")
    if len(ys) < 2 or dh is None or t.tk not in dh.index or not shares:
        return NA("배당총액·순이익 데이터 부족")
    row = dh.loc[t.tk]
    ratios = []
    for y in ys:
        d = row.get(y["year"]) if y["year"] in row.index else None
        if d is None or not is_num(d) or y["ni"] <= 0:
            continue
        ratios.append(d * shares / y["ni"] * 100)
    if not ratios:
        return NA("연도별 DPS 매칭 불가")
    r = float(np.mean(ratios))
    s = 10 if r >= 50 else 8 if r >= 35 else 6 if r >= 25 else 4 if r >= 15 else 2 if r >= 5 else 0
    return Res(s, f"배당성향 평균 {f(r,0)}% ({len(ratios)}년)", "자사주 소각 미반영(배당만), 현재 주식수 기준 근사", True)


def L15(t: Ctx):
    bps, eps, pbr = t.get("bps"), t.get("eps"), t.get("pbr")
    ys = [x for x in _annual(t) if x.get("ni") is not None and x.get("equity")][-3:]
    if not bps or bps <= 0:
        return NA("BPS 없음")
    roe = float(np.mean([x["ni"] / x["equity"] for x in ys])) if len(ys) >= 2 else (eps / bps if eps is not None else None)
    if roe is None:
        return NA("ROE 없음")
    coe, g = _coe(t), 0.01
    fair = bps + (roe - coe) * bps / (coe - g)
    c = t.c.iloc[-1]
    if fair <= 0:
        return Res(0, f"RIM 적정가 ≤ 0 (ROE {f(roe*100)}%)", "잔여이익모델", True)
    m = (fair - c) / fair * 100
    s = 10 if m >= 40 else 8 if m >= 30 else 6 if m >= 20 else 4 if m >= 10 else 2 if m >= 0 else 0
    return Res(s, f"RIM 적정가 {f(fair,0)} vs 현재가 {f(c,0)} → 안전마진 {f(m,0)}%",
               f"RIM: BPS {f(bps,0)}, ROE {f(roe*100)}%, COE {coe*100:.0f}%, g 1% (단일 방법)", True)


def L16(t: Ctx):
    ph = t.b.pbr_hist
    pbr = t.get("pbr")
    if ph is None or t.tk not in ph.index or pbr is None or pbr <= 0:
        return NA("PBR 이력 없음")
    hist = ph.loc[t.tk].dropna()
    hist = hist[hist > 0]
    if len(hist) < 12:
        return NA("PBR 이력 3년 미만")
    pct = (hist < pbr).mean() * 100
    roe_ok = (t.get("eps") or 0) > 0
    if pct <= 10: s = 10 if roe_ok else 8
    elif pct <= 25: s = 8
    elif pct <= 45: s = 6
    elif pct <= 65: s = 4
    elif pct <= 85: s = 2
    else: s = 0
    return Res(s, f"현재 PBR {f(pbr,2)}, 과거 {len(hist)}개 분기 중 하위 {f(pct,0)}% 위치", "PBR 밴드(분기 표본)")


def L20(t: Ctx):
    now, past = t.get("shares"), t.get("shares_5y")
    if not now or not past:
        return NA("5년 전 상장주식수 없음")
    g = (now / past - 1) * 100
    s = 10 if g < -0.5 else 8 if g <= 2 else 6 if g <= 5 else 4 if g <= 15 else 2 if g <= 30 else 0
    return Res(s, f"5년 주식수 변화 {f(g)}%", "상장주식수 비교 (액면분할·병합 시 수동 확인)")


# 사용자 요청으로 조사·채점에서 뺀 항목 (N/A 처리, 분모에서 제외)
DISABLED = {"S11": "공매도·대차잔고 조사 제외 (사용자 결정)"}

RULES = {k: v for k, v in globals().items() if k[:1] in "SML" and k[1:].isdigit() and callable(v)}


# ───────────────────────── 유니버스 공통 계산 ─────────────────────────

def build_universe(b: Bundle, tickers):
    u = {"sector": {}, "sector_per": {}, "sector_opm": {}, "env": {}, "rs_pct": {}}
    info = b.info.loc[tickers]
    close = b.close[tickers]
    # 1개월 수익률
    r1 = (close.iloc[-1] / close.iloc[-22] - 1) * 100 if len(close) > 22 else pd.Series(dtype=float)
    r3 = (close.iloc[-1] / close.iloc[-64] - 1) * 100 if len(close) > 64 else pd.Series(dtype=float)
    rs = (r1.rank(pct=True) + r3.rank(pct=True)) / 2
    u["rs_pct"] = rs.dropna().to_dict()
    # 업종 강도
    df = pd.DataFrame({"sector": info["sector"].fillna("기타"), "mcap": info["mcap"], "r1": r1,
                       "val": b.value[tickers].iloc[-20:].mean()})
    df = df.dropna(subset=["r1"])
    sec_rows = []
    for sec, g in df.groupby("sector"):
        w = g["mcap"].fillna(0)
        ret = float((g["r1"] * w).sum() / w.sum()) if w.sum() > 0 else float(g["r1"].mean())
        leaders = set(g.sort_values("mcap", ascending=False).index[:3]) | set(g.sort_values("val", ascending=False).index[:3])
        sec_rows.append((sec, ret, len(g), leaders))
    sec_rows.sort(key=lambda x: -x[1])
    N = len(sec_rows)
    for i, (sec, ret, n, leaders) in enumerate(sec_rows):
        u["sector"][sec] = {"ret": ret, "n": n, "leaders": leaders, "rank_pct": i / max(N - 1, 1)}
    # 업종 PER 중앙값 (흑자 기업)
    per = info[["sector", "per"]].copy()
    per = per[per["per"] > 0]
    u["sector_per"] = per.groupby("sector")["per"].median().to_dict()
    # 업종 평균 영업이익률 (DART 연간)
    opm = {}
    for tk in tickers:
        a = (b.fin.get(tk) or {}).get("annual") or {}
        vals = [x["op"] / x["rev"] * 100 for x in a.values() if x.get("rev") and x.get("op") is not None]
        if vals:
            opm.setdefault(info.at[tk, "sector"], []).append(float(np.mean(vals[-5:])))
    u["sector_opm"] = {k: float(np.median(v)) for k, v in opm.items() if len(v) >= 3}
    # 시장 환경 (S18)
    for mk, ix in b.index_close.items():
        ix = ix.dropna()
        if len(ix) < 80:
            continue
        m60 = ind.sma(ix, 60)
        conds, notes = [], []
        c1 = ix.iloc[-1] > m60.iloc[-1] and m60.iloc[-1] > m60.iloc[-6]
        conds.append(c1); notes.append(f"① 지수>60일선·상승 {'O' if c1 else 'X'}")
        mf = b.market_foreign20.get(mk)
        if mf is not None:
            conds.append(mf > 0); notes.append(f"② 외국인 20일 {f(mf/1e8,0)}억 {'O' if mf > 0 else 'X'}")
        else:
            notes.append("② 외국인 데이터 없음")
        notes.append("③ VKOSPI 미제공(제외)")
        if b.fx is not None and len(b.fx.dropna()) > 21:
            fx = b.fx.dropna()
            c4 = fx.iloc[-1] < fx.iloc[-21]
            conds.append(c4); notes.append(f"④ 환율 20일 {'하락 O' if c4 else '상승 X'} ({f(fx.iloc[-1],1)})")
        else:
            notes.append("④ 환율 데이터 없음")
        r20 = (ix.iloc[-1] / ix.iloc[-21] - 1) * 100
        p = sum(conds) / len(conds)
        if r20 <= -10:
            s, basis = 0, f"지수 20일 {f(r20)}% 급락 국면"
        else:
            s = 10 if p >= 1 else 8 if p >= 0.75 else 6 if p >= 0.5 else 4 if p >= 0.25 else 2
            basis = f"확인 가능한 {len(conds)}개 조건 중 {sum(conds)}개 충족(비율 환산)"
        u["env"][mk] = {"score": s, "value": " / ".join(notes), "basis": basis, "index": float(ix.iloc[-1]),
                        "date": ix.index[-1].strftime("%Y%m%d"),
                        "r20": r20, "conds": notes}
    return u


# ───────────────────────── 제외 필터 ─────────────────────────

def knockouts(t: Ctx, st):
    out = []
    name = str(t.info.get("name", ""))
    if t.tk in t.b.admin:
        out.append("K1 관리종목·거래정지")
    if t.n and t.v.iloc[-1] == 0 and t.o.iloc[-1] == 0:
        out.append("K1 매매거래정지(당일 거래 없음)")
    if t.n >= 20:
        a = t.val.iloc[-20:].mean() / EOK
        if a < st["k7_value_eok"]:
            out.append(f"K7 20일 평균 거래대금 {f(a,1)}억 < {st['k7_value_eok']}억")
    if t.mcap is not None and t.mcap / EOK < st["k8_mcap_eok"]:
        out.append(f"K8 시가총액 {f(t.mcap/EOK,0)}억 < {st['k8_mcap_eok']}억")
    if "스팩" in name or "SPAC" in name.upper():
        out.append("K9 합병 전 SPAC")
    bs = (t.fin or {}).get("latest_bs")
    if bs and bs.get("capital") and bs.get("equity") is not None:
        erosion = (bs["capital"] - bs["equity"]) / bs["capital"] * 100
        if erosion >= 50:
            out.append(f"K3 자본잠식률 {f(erosion,0)}%")
    ann = _annual(t)
    if len(ann) >= 3 and all((x.get("op") is not None and x["op"] < 0) for x in ann[-3:]):
        out.append("K10 3년 연속 영업손실(관리종목 요건 근접, 기술특례 여부 수동 확인)")
    return out


# ───────────────────────── 종합 ─────────────────────────

def score_ticker(t: Ctx, st, manual):
    items = {}
    for iid, meta in ITEM_MAP.items():
        m = manual.get(iid)
        if m is not None and m.get("score") is not None:
            r = Res(float(m["score"]), m.get("value") or "수동 입력", m.get("note") or "사용자 입력", False, "수동")
        else:
            fn = RULES.get(iid)
            if iid in DISABLED:
                r = NA(DISABLED[iid])
            elif fn is None:
                r = NA("무료 자동 데이터 없음 (상세 화면에서 수동 입력 가능)")
            else:
                try:
                    r = fn(t)
                except Exception as e:  # 데이터 이상으로 한 항목이 실패해도 전체는 계속
                    r = NA(f"계산 오류: {e}")
        if r.score is not None:
            r.score = float(max(0, min(10, r.score)))
            if r.est and r.src != "수동":
                r.score = min(r.score, 6.0)
        items[iid] = r

    persp, cov = {}, {}
    for p in PERSPECTIVES:
        num = den = 0
        for iid, meta in ITEM_MAP.items():
            if meta["persp"] != p:
                continue
            r = items[iid]
            if r.score is not None:
                num += r.score * meta["weight"]
                den += meta["weight"]
        persp[p] = num / den * 10 if den else None
        cov[p] = den / 100

    prof = PROFILES.get(st["profile"], PROFILES["C"])
    wsum = sum(prof[p] for p in PERSPECTIVES if persp[p] is not None)
    base = sum(persp[p] * prof[p] for p in PERSPECTIVES if persp[p] is not None) / wsum if wsum else 0
    coverage = sum(cov[p] * prof[p] for p in PERSPECTIVES)

    # 가점·감점: 수동 입력 + 자동 근사(D4)
    adj = []
    for k, (label, pts) in {**PENALTIES, **BONUSES}.items():
        m = manual.get(k)
        if m and m.get("score"):
            adj.append((k, label, pts, "수동"))
    if "D4" not in manual and t.n >= 126:
        lo6 = t.l.iloc[-126:].min()
        if lo6 > 0 and t.h.iloc[-126:].max() / lo6 >= 4:
            adj.append(("D4", PENALTIES["D4"][0] + " (가격 기준 자동 판정)", PENALTIES["D4"][1], "자동"))
    pen = max(PENALTY_CAP, sum(a[2] for a in adj if a[2] < 0))
    bon = min(BONUS_CAP, sum(a[2] for a in adj if a[2] > 0))
    final = max(0.0, min(100.0, base + bon + pen))

    # 등급 게이트
    ko = knockouts(t, st)
    core = [items[i] for i, m in ITEM_MAP.items() if m["core"]]
    gate = []
    if final >= 90:
        reasons = []
        if ko: reasons.append("제외 필터 해당")
        lows = [persp[p] for p in PERSPECTIVES if persp[p] is not None and prof[p] >= 0.10]
        if lows and min(lows) < 70: reasons.append(f"최저 관점 점수 {min(lows):.1f} < 70")
        if any(r.score is not None and r.score <= 2 for r in core): reasons.append("핵심(★) 항목 2점 이하")
        if coverage < st["gate_s_cov"]: reasons.append(f"커버리지 {coverage*100:.0f}% < {st['gate_s_cov']*100:.0f}%")
        if reasons:
            final = 89.0
            gate.append("S 제한: " + ", ".join(reasons))
    if final >= 80:
        reasons = []
        if ko: reasons.append("제외 필터 해당")
        if any(r.score is not None and r.score == 0 for r in core): reasons.append("핵심(★) 항목 0점")
        if coverage < st["gate_a_cov"]: reasons.append(f"커버리지 {coverage*100:.0f}% < {st['gate_a_cov']*100:.0f}%")
        if reasons:
            final = 79.0
            gate.append("A 제한: " + ", ".join(reasons))
    grade, glabel = grade_of(final)
    if ko:
        grade, glabel = "X", "제외 필터"
    reliability = "높음" if coverage >= 0.85 else "보통" if coverage >= 0.70 else "낮음"
    core_vals = [r.score for r in core if r.score is not None]
    return {
        "items": items, "persp": persp, "cov": cov, "base": base, "bonus": bon, "penalty": pen,
        "adj": adj, "final": final, "grade": grade, "grade_label": glabel, "gate": gate,
        "knockout": ko, "coverage": coverage, "reliability": reliability,
        "core_avg": float(np.mean(core_vals)) if core_vals else 0.0,
        "target": t.cache.get("target"), "stop": t.cache.get("stop"),
    }


def score_universe(b: Bundle, st, holdings=(), exclusions=(), manual=None, progress=None):
    manual = manual or {}
    holdings, exclusions = set(holdings), set(exclusions)
    tickers = [tk for tk in b.info.index if tk in b.close.columns and b.close[tk].notna().sum() >= 20]
    u = build_universe(b, tickers)
    out = []
    for i, tk in enumerate(tickers):
        if tk in exclusions and tk not in holdings:
            continue
        t = Ctx(tk, b, u)
        res = score_ticker(t, st, manual.get(tk, {}))
        res.update({"ticker": tk, "name": t.info.get("name", tk), "market": t.market, "sector": t.sector,
                    "close": float(t.c.iloc[-1]), "mcap": t.mcap, "chg": float(ind.ret(t.c, 1) or 0)})
        out.append(res)
        if progress and i % 200 == 0:
            progress(i, len(tickers))
    out.sort(key=lambda r: (r["grade"] != "X", r["final"], r["core_avg"]), reverse=True)
    return out, u
