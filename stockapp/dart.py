"""OpenDART(무료 API 키) 재무 데이터 수집.

다중회사 주요계정(fnlttMultiAcnt)으로 100개 회사씩 묶어 조회하고 DB에 캐시한다.
과거 보고서는 한 번만 받으면 되므로 첫 실행만 오래 걸린다.
"""
import io
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


def collect(key, tickers, base_date, log=print, strict=False):
    """strict(투자검증)면 기준일에 실제로 공시돼 있었을 보고서만 쓴다 (분기 45일, 사업보고서 90일 제출 기한)."""
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
    return build_fin(reports, tickers)


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
