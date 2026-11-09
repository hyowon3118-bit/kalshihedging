"""
2026.9 FOMC(9.16 발표) Kalshi 계약별 확률 경로: D-30 ~ D-0 일별
- 이벤트: KXFEDDECISION-26SEP (인상/동결/인하 등 결과별 마켓 전부)
- 확률 = 해당 시각까지의 마지막 체결가(체결 없으면 직전 값 이월, Diercks et al. 2026 p.13-14 방식)
- 시각 = 매 영업일 16:00 ET (D-0만 마지막 체결 시각 13:59 ET)
- 거래량 = 해당 시각 직전 24시간
- 라이브러리: requests, pandas (API 키 불필요, 공개 엔드포인트)
"""
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# ---------- 설정 ----------
BASE = "https://api.elections.kalshi.com/trade-api/v2"
EVENT = "KXFEDDECISION-26SEP"
SERIES = "KXFEDDECISION"
ET = ZoneInfo("America/New_York")
DECISION_DAY = datetime(2026, 9, 16, tzinfo=ET)
D0_TIME = datetime(2026, 9, 16, 13, 59, tzinfo=ET)          # Kalshi FOMC 계약 마지막 체결 시각
START = DECISION_DAY - timedelta(days=32)                    # D-30 직전 24시간 거래량용 여유
NYSE_HOLIDAYS_2026 = {datetime(2026, 9, 7).date()}           # 구간 내 휴장일: 노동절 9.7 (Kiplinger 2026 휴장일 목록)


def log(msg):
    """진행 상황 로그"""
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def api_get(path, params=None):
    """Kalshi 공개 API GET (429 시 재시도)"""
    for i in range(5):
        r = requests.get(BASE + path, params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(1 + i)
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    sys.exit(f"[error] 재시도 초과: {path}")


def f(x):
    """문자열 달러 값 -> float"""
    return None if x in (None, "") else float(x)


# ---------- 1. 이벤트의 마켓 목록 ----------
log(f"1/3 이벤트 {EVENT} 마켓 목록 조회")
ev = api_get(f"/events/{EVENT}")
if not ev:
    sys.exit("[error] 이벤트 없음")
markets = ev.get("markets") or []
for m in markets:
    log(f"  {m['ticker']}: {m.get('yes_sub_title')} (결과 {m.get('result')})")


# ---------- 2. 마켓별 1시간봉 수집 ----------
def candles(ticker):
    """1시간봉: 체결가 종가, 매도호가 종가, 거래량 (7일 단위로 나눠 조회)"""
    rows, cur = [], START
    while cur < D0_TIME + timedelta(hours=1):
        nxt = min(cur + timedelta(days=7), D0_TIME + timedelta(hours=1))
        params = {"start_ts": int(cur.timestamp()), "end_ts": int(nxt.timestamp()), "period_interval": 60}
        data = api_get(f"/series/{SERIES}/markets/{ticker}/candlesticks", params)
        if not data or not data.get("candlesticks"):
            data = api_get(f"/historical/markets/{ticker}/candlesticks", params)
        for c in (data or {}).get("candlesticks", []):
            rows.append({
                "ts": datetime.fromtimestamp(c["end_period_ts"], tz=ET),
                "trade": f((c.get("price") or {}).get("close_dollars")),
                "ask": f((c.get("yes_ask") or {}).get("close_dollars")),
                "vol": f(c.get("volume_fp")) or 0.0,
            })
        cur = nxt
    if not rows:
        return None
    return pd.DataFrame(rows).drop_duplicates("ts").set_index("ts").sort_index()


log("2/3 마켓별 1시간봉 수집")
data = {}
for m in markets:
    df = candles(m["ticker"])
    if df is None:
        log(f"  {m['ticker']}: 데이터 없음")
        continue
    data[m["ticker"]] = (m.get("yes_sub_title"), df)
    log(f"  {m['ticker']}: {len(df)}개 봉 ({df.index.min():%m-%d %H:%M} ~ {df.index.max():%m-%d %H:%M})")


# ---------- 3. 영업일별 확률표 ----------
log("3/3 D-30 ~ D-0 영업일별 확률 계산")
points = []                                                   # (라벨, 시각)
d = DECISION_DAY - timedelta(days=30)
while d.date() < DECISION_DAY.date():
    if d.weekday() < 5 and d.date() not in NYSE_HOLIDAYS_2026:
        points.append((f"D-{(DECISION_DAY.date() - d.date()).days}", d.replace(hour=16, minute=0)))
    d += timedelta(days=1)
points.append(("D-0", D0_TIME))

out = []
for label, ts in points:
    row = {"시점": label, "시각(ET)": f"{ts:%m-%d %H:%M}"}
    for tk, (name, df) in data.items():
        upto = df.loc[:ts]
        tr = upto["trade"].dropna()
        ak = upto["ask"].dropna()
        vol24 = df.loc[(df.index > ts - timedelta(hours=24)) & (df.index <= ts), "vol"].sum()
        row[f"{name} 체결"] = None if tr.empty else tr.iloc[-1]
        row[f"{name} 매도호가"] = None if ak.empty else ak.iloc[-1]
        row[f"{name} 24h거래량"] = int(vol24)
    out.append(row)

table = pd.DataFrame(out).set_index("시점")
pd.set_option("display.width", 250, "display.max_columns", 50)
print("\n" + table.to_string())
log("완료")
