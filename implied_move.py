# -*- coding: utf-8 -*-
"""
implied_move.py  (중간선거 헤지 사이징 근거: 옵션 내재 예상 변동폭)

[방법]
 - 예상 변동폭(%) = ATM 스트래들 가격(콜 중간가 + 풋 중간가) / 기초자산 가격  (MarketBeat 'implied move' 공식)
 - 만기: 청산일(11.4) 이후 가장 가까운 만기
 - 시점: D-1 = 11.2 15:59 ET, D-0 = 11.3 15:59 ET (진입 16:00 직전 1분봉)
 - 기초자산: QQQ (사례 2-2), SMH (사례 2-1 반도체 5종목의 대용치. 상위 종목이 겹치고 옵션 유동성이 가장 큼)
 - 커버 적용: 계약 2개(상원·텍사스)가 둘 다 맞으면 합쳐서 예상 변동폭을 덮도록 계약당 커버 = 예상 변동폭 / 2

[데이터]
 - Databento OPRA.PILLAR: definition(만기·행사가), cbbo-1m(1분 단위 최우선 호가)
 - yfinance: 기초자산 1분봉 종가

[필요 키] DATABENTO_API_KEY (환경변수)
실행: python3 implied_move.py
"""
import os
import sys
from datetime import datetime, date
from zoneinfo import ZoneInfo

import databento as db
import pandas as pd
import yfinance as yf

# ---------- 설정 ----------
ET = ZoneInfo("America/New_York")
UNDERLYINGS = ["QQQ", "SMH"]
POINTS = {"D-1 (11.2 15:59)": datetime(2026, 11, 2, 15, 59, tzinfo=ET),
          "D-0 (11.3 15:59)": datetime(2026, 11, 3, 15, 59, tzinfo=ET)}
EXIT_DATE = date(2026, 11, 4)
MAX_COST_USD = 5.0                       # 요청 비용이 이보다 크면 중단 (크레딧 보호)
DATASET = "OPRA.PILLAR"


def log(msg):
    """진행 상황 로그"""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def check_cost(client, **kw):
    """요청 전 예상 비용 확인, 한도 초과 시 중단"""
    cost = client.metadata.get_cost(**kw)
    log(f"    예상 비용 ${cost:.4f}")
    if cost > MAX_COST_USD:
        sys.exit(f"[중단] 예상 비용 ${cost:.2f} > 한도 ${MAX_COST_USD}. MAX_COST_USD 조정 후 재실행")


def spot_at(sym, ts):
    """yfinance 1분봉에서 ts 시각 봉 종가 (인덱스 = 봉 시작 시각)"""
    df = yf.download(sym, start=ts.date().isoformat(), end=(ts.date() + pd.Timedelta(days=1)).isoformat(),
                     interval="1m", auto_adjust=False, progress=False)["Close"]
    s = df[sym] if isinstance(df, pd.DataFrame) else df
    s.index = s.index.tz_convert(ET)
    s = s.loc[:ts]
    if s.empty:
        sys.exit(f"[error] {sym} {ts} 1분봉 없음")
    return float(s.iloc[-1]), s.index[-1]


def atm_pair(client, sym, ts, spot):
    """그날 옵션 정의에서 청산일 이후 가장 가까운 만기의 ATM 콜·풋 raw_symbol 선택"""
    kw = dict(dataset=DATASET, schema="definition", stype_in="parent", symbols=[f"{sym}.OPT"],
              start=ts.date().isoformat(), end=(ts.date() + pd.Timedelta(days=1)).isoformat())
    check_cost(client, **kw)
    # 인덱스(ts_recv)가 중복되면 행사가 선택 시 Series 비교 오류가 나므로 인덱스 초기화
    d = client.timeseries.get_range(**kw).to_df().drop_duplicates("raw_symbol", keep="last").reset_index(drop=True)
    # 만기일 = OCC 심볼의 YYMMDD (expiration 타임스탬프는 UTC 자정 기준이라 ET로 바꾸면 하루 당겨짐)
    d["exp"] = pd.to_datetime(d["raw_symbol"].str[6:12], format="%y%m%d").dt.date
    d = d[d["exp"] >= EXIT_DATE]
    exp = d["exp"].min()
    d = d[d["exp"] == exp]
    k = float(d.loc[(d["strike_price"] - spot).abs().idxmin(), "strike_price"])  # 기초자산 가격에 가장 가까운 행사가
    c = d[(d["strike_price"] == k) & (d["instrument_class"] == "C")]["raw_symbol"].iloc[0]
    p = d[(d["strike_price"] == k) & (d["instrument_class"] == "P")]["raw_symbol"].iloc[0]
    log(f"    만기 {exp}, 행사가 {k} -> 콜 '{c}', 풋 '{p}'")
    return exp, k, c, p


def mid_at(client, raw, ts):
    """cbbo-1m에서 ts 이전 마지막 최우선 매수·매도호가 중간가"""
    kw = dict(dataset=DATASET, schema="cbbo-1m", stype_in="raw_symbol", symbols=[raw],
              start=(ts - pd.Timedelta(minutes=30)).isoformat(), end=(ts + pd.Timedelta(minutes=1)).isoformat())
    q = client.timeseries.get_range(**kw).to_df()
    q.index = q.index.tz_convert(ET)
    q = q.loc[:ts].dropna(subset=["bid_px_00", "ask_px_00"])
    if q.empty:
        sys.exit(f"[error] {raw} {ts} 호가 없음")
    r = q.iloc[-1]
    return (r["bid_px_00"] + r["ask_px_00"]) / 2, r["bid_px_00"], r["ask_px_00"], q.index[-1]


# ---------- 실행 ----------
if not os.environ.get("DATABENTO_API_KEY"):
    sys.exit("[error] DATABENTO_API_KEY 환경변수 필요")
client = db.Historical()

rows = []
for i, sym in enumerate(UNDERLYINGS, 1):
    log(f"{i}/{len(UNDERLYINGS)} {sym}")
    for label, ts in POINTS.items():
        log(f"  {label}")
        spot, st = spot_at(sym, ts)                                      # 1) 기초자산 가격
        log(f"    기초자산 {spot:.2f} (봉 {st:%m-%d %H:%M})")
        exp, k, c, p = atm_pair(client, sym, ts, spot)                   # 2) ATM 콜·풋 선택
        cm, cb, ca, ct = mid_at(client, c, ts)                           # 3) 콜·풋 중간가
        pm, pb, pa, pt = mid_at(client, p, ts)
        log(f"    콜 {cb}/{ca} (봉 {ct:%H:%M}), 풋 {pb}/{pa} (봉 {pt:%H:%M})")
        move = (cm + pm) / spot * 100                                    # 4) 예상 변동폭
        rows.append({"기초자산": sym, "시점": label, "만기": exp, "행사가": k, "기초자산가격": round(spot, 2),
                     "스트래들": round(cm + pm, 2), "예상변동폭(%)": round(move, 2),
                     "계약당 커버(%)": round(move / 2, 2)})

print("\n" + pd.DataFrame(rows).to_string(index=False))
log("완료. 출력 전체를 복사해서 보내줘 (API 키 줄 제외).")
