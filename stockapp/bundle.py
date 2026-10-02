"""스캔 1회에 필요한 시장 데이터 묶음. 실제 수집(collector)과 데모(demo)가 같은 형태를 만든다."""
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class Bundle:
    base_date: str                       # YYYYMMDD (직전 거래일)
    # 가격 패널: index=날짜(DatetimeIndex), columns=티커
    close: pd.DataFrame
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    volume: pd.DataFrame
    value: pd.DataFrame                  # 거래대금(원)
    # 종목 스냅샷: index=티커
    # name, market, sector, mcap, shares, per, pbr, eps, bps, div, dps,
    # f_net5, f_net20, i_net20, i_net60, pension_net20 (원),
    # foreign_now, foreign_3m, foreign_6m (지분율 %),
    # shares_5y
    info: pd.DataFrame
    index_close: dict = field(default_factory=dict)      # {"KOSPI": Series, "KOSDAQ": Series}
    market_foreign20: dict = field(default_factory=dict)  # {"KOSPI": 원}
    fx: pd.Series | None = None                           # 원/달러 환율
    dps_hist: pd.DataFrame | None = None                  # index=티커, columns=연도 → DPS
    pbr_hist: pd.DataFrame | None = None                  # index=티커, columns=표본일 → PBR
    fin: dict = field(default_factory=dict)               # 티커 → DART 재무 dict
    admin: set = field(default_factory=set)               # 관리종목·거래정지 등 (알 수 있는 경우)
    sources: dict = field(default_factory=dict)           # 데이터 항목 → 출처·기준일 문자열
    warnings: list = field(default_factory=list)
    demo: bool = False
