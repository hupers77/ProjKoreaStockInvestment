"""OpenDART(무료 API 키) 재무 데이터 수집.

다중회사 주요계정(fnlttMultiAcnt)으로 100개 회사씩 묶어 조회하고 DB에 캐시한다.
현금흐름·차입금(M17·M21·L09)은 주요계정에 없어 단일회사 전체 재무제표(fnlttSinglAcntAll)를
사업보고서만 회사별로 받는다. 과거 보고서는 한 번만 받으면 되므로 첫 실행만 오래 걸린다.
"""
import io
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta

import requests

from . import db

BASE = "https://opendart.fss.or.kr/api"
REPORTS = [("11013", 1), ("11012", 2), ("11014", 3), ("11011", 4)]  # 1분기, 반기, 3분기, 사업보고서
ACCOUNTS = {
    "매출액": "rev", "수익(매출액)": "rev", "영업수익": "rev",
    "영업이익": "op", "영업이익(손실)": "op",
    "당기순이익": "ni", "당기순이익(손실)": "ni",
    "자본총계": "equity", "부채총계": "liab", "유동자산": "cur_assets", "유동부채": "cur_liab", "자본금": "capital",
}


def _num(s):
    if s is None:
        return None
    s = str(s).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def corp_codes(key):
    cached, at = db.cache_get("dart:corpcode")
    if cached and at and datetime.strptime(at, "%Y-%m-%d %H:%M:%S") > datetime.now() - timedelta(days=30):
        return cached
    r = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": key}, timeout=60)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    out = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            out[sc] = el.findtext("corp_code").strip()
    db.cache_put("dart:corpcode", out)
    return out


def _fetch_report(key, year, reprt, corp_list, log):
    """{stock_code: {field: {"cur": v, "add": v, "prev": v}}}"""
    out = {}
    for i in range(0, len(corp_list), 100):
        chunk = corp_list[i:i + 100]
        for attempt in range(3):
            try:
                r = requests.get(f"{BASE}/fnlttMultiAcnt.json", timeout=60, params={
                    "crtfc_key": key, "corp_code": ",".join(chunk), "bsns_year": str(year), "reprt_code": reprt})
                j = r.json()
                break
            except Exception as e:  # 네트워크 일시 오류 재시도
                if attempt == 2:
                    log(f"DART {year}/{reprt} 조회 실패: {e}")
                    j = {}
                time.sleep(2)
        st = j.get("status")
        if st == "020":
            raise RuntimeError("DART 일일 요청 한도 초과")
        if st in ("010", "011", "012", "901"):
            raise RuntimeError(f"DART API 키 오류: {j.get('message')}")
        if st != "000":
            continue
        tmp = {}
        for row in j.get("list", []):
            fld = ACCOUNTS.get((row.get("account_nm") or "").replace(" ", ""))
            if not fld:
                continue
            sc = row.get("stock_code", "").strip()
            fs = row.get("fs_div")
            d = tmp.setdefault(sc, {}).setdefault(fs, {})
            if fld in d:
                continue
            d[fld] = {"cur": _num(row.get("thstrm_amount")), "add": _num(row.get("thstrm_add_amount")),
                      "prev": _num(row.get("frmtrm_amount")), "prev2": _num(row.get("bfefrmtrm_amount"))}
        for sc, fsd in tmp.items():
            out[sc] = fsd.get("CFS") or fsd.get("OFS")  # 연결 우선
        time.sleep(0.3)
    return out


def _report_final(year, q, today):
    """보고서 제출 기한이 충분히 지나 내용이 바뀌지 않는다고 볼 수 있는지."""
    end_month = {1: 3, 2: 6, 3: 9, 4: 12}[q]
    due = datetime(year + (1 if q == 4 else 0), 4 if q == 4 else end_month, 1) + timedelta(days=150 if q == 4 else 120)
    return today > due


def collect(key, tickers, base_date, log=print, strict=False, cf_tickers=None, warnings=None):
    """strict(투자검증)면 기준일에 실제로 공시돼 있었을 보고서만 쓴다 (분기 47일, 사업보고서 90일 제출 기한).
    cf_tickers: 현금흐름표(회사별 조회)를 받을 종목. 제외 필터(K7·K8)에 걸리지 않는 종목만 넘겨 호출 수를 줄인다."""
    today = datetime.strptime(base_date, "%Y%m%d")
    codes = corp_codes(key)
    corp_list = sorted({codes[t] for t in tickers if t in codes})
    by_corp = {v: k for k, v in codes.items()}
    reports = {}
    y0 = today.year
    plan = [(y, rc, q) for y in (y0 - 2, y0 - 1, y0) for rc, q in REPORTS]
    plan += [(y, "11011", 4) for y in range(y0 - 6, y0 - 2)]
    for y, rc, q in plan:
        # 분기 종료 후 45일 전이면 아직 제출 전
        end = datetime(y, {1: 3, 2: 6, 3: 9, 4: 12}[q], 28)
        if today < end + timedelta(days=(90 if q == 4 else 47) if strict else 40):
            continue
        ck = f"dart:{y}:{rc}"
        cached, at = db.cache_get(ck)
        fresh = at and datetime.strptime(at, "%Y-%m-%d %H:%M:%S") > datetime.now() - timedelta(days=7)
        if cached is not None and (_report_final(y, q, today) or fresh):
            reports[(y, q)] = cached
            continue
        log(f"DART {y}년 {q}분기 보고서 조회 중…")
        data = _fetch_report(key, y, rc, corp_list, log)
        db.cache_put(ck, data)
        reports[(y, q)] = data
    fin = build_fin(reports, tickers)
    if cf_tickers:
        try:
            cf = collect_cf(key, codes, cf_tickers, today, log, warnings)
        except Exception as e:
            cf = {}
            if warnings is not None:
                warnings.append(f"DART 현금흐름표 수집 실패(M17·M21·L09 N/A): {e}")
        for tk, v in cf.items():
            fin.setdefault(tk, {"quarters": [], "annual": {}, "latest_bs": None})["cf"] = v
    return fin


# ── 현금흐름·차입금 (단일회사 전체 재무제표, 사업보고서) ──

class DartLimit(RuntimeError):
    pass


def cf_year(base_dt):
    """기준일에 공시돼 있다고 볼 수 있는 최근 사업보고서 연도. 제출 기한(3월 말)에 여유를 두어 4월 10일부터 쓴다."""
    return base_dt.year - 1 if (base_dt.month, base_dt.day) >= (4, 10) else base_dt.year - 2


def _norm(s):
    return re.sub(r"[\s()\[\]<>ㆍ·,.]", "", s or "")


DEBT_WORDS = ("차입금", "사채", "리스부채", "유동성장기부채")
DEBT_SKIP = ("상환", "이자", "충당", "할인", "할증", "전환권", "신주인수권", "미지급", "대여", "채권", "보증", "평가")


def parse_full(rows):
    """사업보고서 전체 재무제표 행 → {"y": {0: 당기, 1: 전기, 2: 전전기 흐름}, "bs": 당기말 잔액}.
    흐름: ocf 영업활동현금흐름, capex 유형·무형자산 취득(양수), dep 감가상각비·상각비, op 영업이익, ni 당기순이익,
    ni_owner 지배주주 순이익. 잔액: cash 현금성자산+단기금융상품, debt 차입금·사채·리스부채, cur_assets, cur_liab."""
    cols = (("thstrm_amount", 0), ("frmtrm_amount", 1), ("bfefrmtrm_amount", 2))
    y = {0: {}, 1: {}, 2: {}}
    bs, seen = {}, set()

    def put(key, row, absval=False, add=False):
        for col, off in cols:
            v = _num(row.get(col))
            if v is None:
                continue
            v = abs(v) if absval else v
            if add:
                y[off][key] = y[off].get(key, 0.0) + v
            elif key not in y[off]:
                y[off][key] = v

    for row in rows:
        sj, aid, nm = row.get("sj_div"), row.get("account_id") or "", _norm(row.get("account_nm"))
        if sj == "CF":
            if aid == "ifrs-full_CashFlowsFromUsedInOperatingActivities" or (nm.startswith("영업활동") and "현금흐름" in nm):
                put("ocf", row)
            elif aid == "ifrs-full_PurchaseOfPropertyPlantAndEquipment" or nm in ("유형자산의취득", "유형자산취득"):
                put("capex_ppe", row, absval=True)
            elif aid == "ifrs-full_PurchaseOfIntangibleAssets" or nm in ("무형자산의취득", "무형자산취득"):
                put("capex_int", row, absval=True)
            elif "상각비" in nm and not any(w in nm for w in ("환입", "손상", "대손")) and ("CF", nm) not in seen:
                seen.add(("CF", nm))
                put("dep", row, absval=True, add=True)
        elif sj in ("IS", "CIS"):
            if aid == "dart_OperatingIncomeLoss" or nm in ("영업이익", "영업이익손실", "영업손실", "영업손익"):
                put("op", row)
            elif aid == "ifrs-full_ProfitLossAttributableToOwnersOfParent" or (
                    "지배기업" in nm and "소유주" in nm and "포괄" not in nm and "비지배" not in nm):
                put("ni_owner", row)
            elif aid == "ifrs-full_ProfitLoss" or nm in ("당기순이익", "당기순이익손실", "당기순손실", "당기순손익",
                                                        "연결당기순이익", "연결당기순이익손실"):
                put("ni", row)
        elif sj == "BS":
            v = _num(row.get("thstrm_amount"))
            if v is None:
                continue
            if aid == "ifrs-full_CashAndCashEquivalents" or nm == "현금및현금성자산":
                bs.setdefault("cash0", v)
            elif nm == "단기금융상품":
                bs.setdefault("stfin", v)
            elif aid == "ifrs-full_CurrentAssets" or nm == "유동자산":
                bs.setdefault("cur_assets", v)
            elif aid == "ifrs-full_CurrentLiabilities" or nm == "유동부채":
                bs.setdefault("cur_liab", v)
            elif any(w in nm for w in DEBT_WORDS) and not any(w in nm for w in DEBT_SKIP) and ("BS", nm) not in seen:
                seen.add(("BS", nm))  # 같은 이름이 상세 행으로 반복돼도 한 번만 더한다
                bs["debt"] = bs.get("debt", 0.0) + v
    for d in y.values():
        if "capex_ppe" in d or "capex_int" in d:
            d["capex"] = d.pop("capex_ppe", 0.0) + d.pop("capex_int", 0.0)
    if "cash0" in bs:
        bs["cash"] = bs.pop("cash0") + bs.pop("stfin", 0.0)
    bs.pop("stfin", None)
    if bs.get("cash") is not None and "debt" not in bs:
        bs["debt"] = 0.0  # 현금은 있는데 차입금 계정이 하나도 없으면 무차입으로 본다
    return {"y": {k: v for k, v in y.items() if v}, "bs": bs}


def _get_full(key, corp, year, fs):
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE}/fnlttSinglAcntAll.json", timeout=30, params={
                "crtfc_key": key, "corp_code": corp, "bsns_year": str(year), "reprt_code": "11011", "fs_div": fs})
            j = r.json()
            break
        except Exception:
            if attempt == 2:
                return None
            time.sleep(2)
    st = j.get("status")
    if st == "020":
        raise DartLimit("DART 일일 요청 한도 초과")
    if st in ("010", "011", "012", "901"):
        raise RuntimeError(f"DART API 키 오류: {j.get('message')}")
    if st == "013":
        return []
    return j.get("list") if st == "000" else None


def full_report(key, corp, year, final):
    """회사 1곳의 사업보고서 1개 (연결 우선, 없으면 별도). 받은 보고서는 다시 받지 않는다.
    아직 제출 전이라 비어 있던 최근 보고서만 7일 뒤 다시 확인한다. 반환 None = 조회 실패(캐시 안 함)."""
    ck = f"dartfull:{corp}:{year}"
    cached, at = db.cache_get(ck)
    if cached is not None:
        stale = (not cached and not final and at and
                 datetime.strptime(at, "%Y-%m-%d %H:%M:%S") < datetime.now() - timedelta(days=7))
        if not stale:
            return cached
    pref, _ = db.cache_get(f"dartfs:{corp}")
    order = [pref, "OFS" if pref == "CFS" else "CFS"] if pref else ["CFS", "OFS"]
    out = {}
    for fs in order:
        rows = _get_full(key, corp, year, fs)
        time.sleep(0.05)
        if rows is None:
            return None
        if rows:
            out = parse_full(rows)
            out["fs"] = fs
            if fs != pref:
                db.cache_put(f"dartfs:{corp}", fs)
            break
    db.cache_put(ck, out)
    return out


def collect_cf(key, codes, tickers, base_dt, log=print, warnings=None):
    """{티커: {"years": {연도: 흐름}, "bs": 잔액, "bs_year": 연도, "fs": "CFS"|"OFS"}}. 최근 5개 연도까지 모은다."""
    y0 = cf_year(base_dt)
    out, todo = {}, [(tk, codes[tk]) for tk in tickers if tk in codes]
    calls = 0
    for i, (tk, corp) in enumerate(todo):
        if i % 100 == 0:
            log(f"DART 현금흐름표 확인 중 {i + 1}/{len(todo)}…")
        years, bs, bs_year, fs, y, tries = {}, None, None, None, y0, 0
        try:
            while tries < 4 and len(years) < 5 and y >= y0 - 6:
                cached, _ = db.cache_get(f"dartfull:{corp}:{y}")
                rep = full_report(key, corp, y, final=y < y0)
                calls += cached is None
                tries += 1
                if rep is None:
                    break
                if not rep:
                    if not years and y == y0:  # 최신 사업보고서가 아직 없으면 한 해 전 것부터
                        y -= 1
                        continue
                    break
                if bs is None:
                    bs, bs_year, fs = rep.get("bs") or {}, y, rep.get("fs")
                for off, d in (rep.get("y") or {}).items():
                    yy = y - int(off)
                    if d and yy not in years:  # 최신 보고서의 (재작성된) 값 우선
                        years[yy] = d
                y = min(years) - 1 if years else y - 1
        except DartLimit as e:
            if warnings is not None:
                warnings.append(f"{e}: 현금흐름표는 {i}/{len(todo)}개 회사까지만 받았습니다. 내일 스캔에서 이어 받습니다.")
            break
        if years or bs:
            out[tk] = {"years": years, "bs": bs or {}, "bs_year": bs_year, "fs": fs}
    if calls:
        log(f"DART 사업보고서(현금흐름표) {calls}건 새로 받음")
    return out


def build_fin(reports, tickers):
    fin = {}
    for tk in tickers:
        cum, bs_latest, annual = {}, None, {}
        for (y, q), data in sorted(reports.items()):
            d = data.get(tk)
            if not d:
                continue
            rec = {}
            for fld in ("rev", "op", "ni"):
                v = d.get(fld)
                if not v:
                    continue
                rec[fld] = v["add"] if (q in (2, 3) and v.get("add") is not None) else v["cur"]
            cum[(y, q)] = rec
            bs = {k: (d.get(k) or {}).get("cur") for k in ("equity", "liab", "cur_assets", "cur_liab", "capital")}
            if bs.get("equity") is not None:
                bs_latest = bs
            if q == 4:
                annual[y] = {**{k: rec.get(k) for k in ("rev", "op", "ni")}, **bs}
                # 전기 금액으로 이전 연도 보충
                for back, key in ((1, "prev"), (2, "prev2")):
                    yy = y - back
                    if yy not in annual:
                        annual[yy] = {k: (d.get(k) or {}).get(key) for k in
                                      ("rev", "op", "ni", "equity", "liab", "cur_assets", "cur_liab", "capital")}
        quarters = []
        for (y, q) in sorted(cum):
            cur = cum[(y, q)]
            prev = cum.get((y, q - 1)) if q > 1 else {}
            if q > 1 and prev is None:
                continue
            row = {"label": f"{y}Q{q}"}
            for fld in ("rev", "op", "ni"):
                a = cur.get(fld)
                b = prev.get(fld) if q > 1 else 0
                row[fld] = None if a is None or b is None else a - b
            quarters.append(row)
        # 최근부터 끊김 없이 이어지는 분기만 유지 (q[-5] = 전년 동기가 되도록)
        run = []
        for row in reversed(quarters):
            y, q = int(row["label"][:4]), int(row["label"][-1])
            if run:
                py, pq = int(run[-1]["label"][:4]), int(run[-1]["label"][-1])
                exp = (py, pq - 1) if pq > 1 else (py - 1, 4)
                if (y, q) != exp:
                    break
            run.append(row)
        quarters = run[::-1]
        annual = {y: v for y, v in annual.items() if any(x is not None for x in v.values())}
        if quarters or annual:
            fin[tk] = {"quarters": quarters, "annual": annual, "latest_bs": bs_latest}
    return fin
