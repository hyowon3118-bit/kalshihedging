# -*- coding: utf-8 -*-
"""
fomc_nfp_backtest.py  (2년물 현물 직접 보유 버전, 회귀 없음)
2026.9 FOMC(25bp 인상) / 2026.10 NFP(11.6 발표) x 미 국채 2년물 롱: 진입·청산 시점별 헤지 손익 비교

[가정]
 B1  포지션: 미 국채 2년물 현물 롱, 기존 보유
     = 표면이자율이 진입일 FRED DGS2와 같은 만기 2년 액면가 채권(연 2회 이자)으로 가정
       (DGS2는 고정만기 국채 수익률 = 액면가 수익률 곡선 기준)
 B2  비교: 롱온리 / 2년물 선물(ZT) 숏 (DV01 1:1) / 예측시장
 B3  일중 금리 변화: 2년물 선물(ZT) 가격 변화를 수정듀레이션으로 환산
       -> 현물-선물 베이시스 0 가정이므로 선물 헤지 손익은 정의상 0
     일별 검증: 발표일 FRED DGS2 변화(bp)와 비교
 B4  FOMC 계약: Kalshi KXFEDDECISION-26SEP 중 '25bp 인상' Yes
 B5  NFP 계약: Kalshi KXPAYROLLS-26OCT 중 컨센서스(59,000)보다 높은 행사가에서
     진입 직전 24시간 거래량 상위 2개 Yes (진입 시점마다 선택, 사후 정보 미사용)
 B12 유동성: 필요 계약 수 / 진입 직전 24시간 거래량 (±1시간 값은 참고로 출력)
 B6  진입: D-1 = 전 영업일 16:00 ET / D-0 = 발표 직전(FOMC 9.16 13:59, NFP 11.6 08:29 ET)
 B7  청산: 발표 1시간 후(FOMC 15:00, NFP 09:30) / 당일 16:00 ET
 B8  Kalshi: 매수=매도호가, 청산=거래 중이면 매수호가(수수료 부과), 확정 후면 정산값(수수료 0)
 B9  사이징: 계약당 2년물 금리 5/10/15bp 상승 손실(= 커버bp x DV01)을 덮도록
 B10 비용: Kalshi 테이커 수수료 0.07xPx(1-P), 정산 수수료 0, 선물 비용 0 / NYSE 휴장일 검증
 B11 분봉: Databento 1분봉(ZTZ6, API 키 필요) 우선, 없으면 yfinance ZT=F(FOMC 5분봉, NFP 1분봉)

[필요 키 (환경변수)]
  FRED_API_KEY       : FRED (fredapi)
  DATABENTO_API_KEY  : Databento (없으면 yfinance로 자동 전환)

실행: pip3 install requests yfinance fredapi databento
      export FRED_API_KEY=...  ;  export DATABENTO_API_KEY=...   (선택)
      python3 fomc_nfp_backtest.py
"""

# ===== 0) 라이브러리 =====
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

try:
    import requests
    import yfinance as yf
    import pandas as pd
    from fredapi import Fred
except ImportError:
    sys.exit("[error] 패키지 설치 필요: pip3 install requests yfinance fredapi databento")

ET = ZoneInfo("America/New_York")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def log(msg):
    """진행 상황 로그"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def t(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


# ===== 1) 설정값 =====
FUT_YF, FUT_DB = "ZT=F", "ZTZ6"            # 2년물 선물: yfinance 연속물 / Databento 2026년 12월물
COVERS_BP = [5, 10, 15]                     # B9
FEE_RATE = 0.07
NFP_CONSENSUS = 59_000                      # B5 (CNBC)
PRINCIPAL_FOR_LIQ = 1_000_000               # 유동성 비율 계산용 원금 ($)

EVENTS = {
    "FOMC 2026.9": {
        "release": t(2026, 9, 16, 14, 0),
        "kalshi_event": "KXFEDDECISION-26SEP",
        "entries": {"D-1 (9.15 16:00)": t(2026, 9, 15, 16), "D-0 (9.16 13:59)": t(2026, 9, 16, 13, 59)},
        "exits": {"발표+1h (9.16 15:00)": t(2026, 9, 16, 15), "종가 (9.16 16:00)": t(2026, 9, 16, 16)},
        "yf_interval": "5m",
    },
    "NFP 2026.10": {
        "release": t(2026, 11, 6, 8, 30),
        "kalshi_event": "KXPAYROLLS-26OCT",
        "entries": {"D-1 (11.5 16:00)": t(2026, 11, 5, 16), "D-0 (11.6 08:29)": t(2026, 11, 6, 8, 29)},
        "exits": {"발표+1h (11.6 09:30)": t(2026, 11, 6, 9, 30), "종가 (11.6 16:00)": t(2026, 11, 6, 16)},
        "yf_interval": "1m",
    },
}

NYSE_HOLIDAYS_2026 = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
                      date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
                      date(2026, 11, 26), date(2026, 12, 25)}


# ===== 2) 영업일 검증 =====
def check_days():
    for ev, cfg in EVENTS.items():
        for name, ts in {**cfg["entries"], **cfg["exits"]}.items():
            d = ts.date()
            ok = d.weekday() < 5 and d not in NYSE_HOLIDAYS_2026
            log(f"  {ev} {name}: {ts.isoformat()} -> {'영업일' if ok else '휴장일!'}")
            if not ok:
                sys.exit("[error] 휴장일 포함")


# ===== 3) 2년물 액면가 채권의 수정듀레이션 (B1) =====
def par_mod_duration(y, years=2, freq=2):
    """표면이자율 = 수익률 y(소수)인 액면가 채권의 수정듀레이션 (연 freq회 이자)"""
    c, n, r = y / freq, years * freq, y / freq
    times = [(k + 1) / freq for k in range(n)]
    cfs = [c] * (n - 1) + [1 + c]
    pv = [cf / (1 + r) ** (k + 1) for k, cf in enumerate(cfs)]
    mac = sum(tk * v for tk, v in zip(times, pv)) / sum(pv)
    return mac / (1 + r)


def fred_inputs(fred, ev, cfg):
    """진입일 DGS2(듀레이션 계산용) + 발표일 DGS2 변화(검증용) + FOMC 목표금리 상단"""
    d0 = min(cfg["entries"].values()).date()
    d = cfg["release"].date()
    s = fred.get_series("DGS2", observation_start=d0 - timedelta(days=10), observation_end=d + timedelta(days=1)).dropna()
    s.index = pd.to_datetime(s.index).normalize()
    y_entry = s[s.index <= pd.Timestamp(d0)].iloc[-1]
    prev = s[s.index < pd.Timestamp(d)].iloc[-1]
    cur = s.loc[pd.Timestamp(d)] if pd.Timestamp(d) in s.index else None
    D = par_mod_duration(y_entry / 100)
    log(f"  [FRED] 진입 기준 DGS2 {y_entry:.2f}% -> 2년 액면가 채권 수정듀레이션 {D:.3f}, DV01 ${D*100:.0f}/원금$100만/bp")
    log(f"  [FRED] 발표일 DGS2 {prev:.2f}% -> {cur}% (변화 {None if cur is None else round((cur - prev) * 100, 1)}bp)")
    if ev.startswith("FOMC"):
        u = fred.get_series("DFEDTARU", observation_start=d - timedelta(days=3), observation_end=d + timedelta(days=3)).dropna()
        log(f"  [FRED] DFEDTARU: {dict((k.date().isoformat(), v) for k, v in u.items())}")
    return D, (None if cur is None else (cur - prev) * 100)


# ===== 4) 2년물 선물 분봉: Databento 우선, 없으면 yfinance =====
def to_et(idx):
    idx = pd.to_datetime(idx)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return idx.tz_convert(ET)


def zt_databento(start, end):
    """Databento GLBX.MDP3 ZTZ6 1분봉. 인덱스=봉 종료 시각"""
    import databento as db
    client = db.Historical(os.environ["DATABENTO_API_KEY"])
    log(f"  Databento GLBX.MDP3 {FUT_DB} {start} ~ {end}")
    df = client.timeseries.get_range(dataset="GLBX.MDP3", schema="ohlcv-1m", symbols=FUT_DB, stype_in="raw_symbol",
                                     start=start.astimezone(ZoneInfo("UTC")).isoformat(),
                                     end=end.astimezone(ZoneInfo("UTC")).isoformat()).to_df()
    s = df["close"].astype(float)
    s.index = to_et(s.index) + pd.Timedelta(minutes=1)
    log(f"    ZT: {len(s)}개 봉 (1분)")
    return s


def zt_yf(start, end, interval):
    """yfinance ZT=F 분봉. 인덱스=봉 종료 시각"""
    step = int(interval.rstrip("m"))
    df = yf.download(FUT_YF, start=start.date().isoformat(), end=(end.date() + timedelta(days=1)).isoformat(),
                     interval=interval, prepost=True, auto_adjust=False, progress=False)["Close"]
    s = df[FUT_YF] if isinstance(df, pd.DataFrame) else df
    s.index = to_et(s.index) + pd.Timedelta(minutes=step)
    s = s.loc[start:end]
    log(f"    ZT=F: {s.notna().sum()}개 봉 ({interval})")
    return s


def px_at(s, ts):
    s = s.dropna()
    s = s[s.index <= ts]
    return (float(s.iloc[-1]), s.index[-1]) if len(s) else (None, None)


# ===== 5) Kalshi =====
def api_get(path, params=None, retries=3):
    for k in range(retries):
        try:
            r = requests.get(KALSHI + path, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            log(f"  HTTP {r.status_code} {path} -> {r.text[:150]}")
        except requests.RequestException as e:
            log(f"  요청 오류 {e}")
        time.sleep(1 + k)
    return None


def event_markets(event_ticker):
    """이벤트 마켓 목록 (현재 -> historical)"""
    data = api_get(f"/events/{event_ticker}")
    ms = (data or {}).get("markets") or []
    series = ((data or {}).get("event") or {}).get("series_ticker") or event_ticker.split("-")[0]
    if not ms:
        data = api_get("/historical/markets", {"event_ticker": event_ticker, "limit": 200})
        ms = (data or {}).get("markets") or []
    for m in ms:
        log(f"    - {m.get('ticker')} | {m.get('yes_sub_title')} | result={m.get('result')} | strike={m.get('floor_strike')}")
    return ms, series


def strike_of(m):
    if m.get("floor_strike") is not None:
        return float(m["floor_strike"])
    nums = re.findall(r"-?[\d,]+", m.get("yes_sub_title", ""))
    return float(nums[0].replace(",", "")) if nums else None


def pick_contracts(ev, ms):
    """B4/B5 규칙으로 헤지 계약 선택"""
    if ev.startswith("FOMC"):
        for m in ms:
            lab = (m.get("yes_sub_title") or "").lower()
            if "hike" in lab and "25" in lab and ">" not in lab:
                return [m]
        sys.exit("[error] FOMC '25bp 인상' 마켓 못 찾음")
    # B5(수정): 컨센서스보다 높은 행사가 전부를 후보로 두고, 진입 시점마다 직전 24시간 거래량 상위 2개 선택
    cands = sorted([(strike_of(m), m) for m in ms if strike_of(m) is not None and strike_of(m) > NFP_CONSENSUS],
                   key=lambda x: x[0])
    if len(cands) < 2:
        sys.exit("[error] NFP 컨센서스 위 행사가 2개 미만")
    return [m for _, m in cands]


def vol_before(df, ts, hours=24):
    """진입 직전 hours시간 거래량 (사후 정보 미사용)"""
    return df.loc[(df.index > ts - timedelta(hours=hours)) & (df.index <= ts), "vol"].sum()


def candles(series, ticker, start, end):
    """Kalshi 1분봉 (6시간 단위, 현재 -> historical 경로)"""
    rows, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(hours=6), end)
        p = {"start_ts": int(cur.timestamp()), "end_ts": int(nxt.timestamp()), "period_interval": 1}
        data = api_get(f"/series/{series}/markets/{ticker}/candlesticks", p)
        if not data or not data.get("candlesticks"):
            data = api_get(f"/historical/markets/{ticker}/candlesticks", p)
        for c in (data or {}).get("candlesticks", []):
            g = lambda d, k: None if not d or d.get(k) in (None, "") else float(d.get(k))
            rows.append({"ts": datetime.fromtimestamp(c["end_period_ts"], tz=ET),
                         "trade": g(c.get("price"), "close_dollars"), "ask": g(c.get("yes_ask"), "close_dollars"),
                         "bid": g(c.get("yes_bid"), "close_dollars"), "vol": float(c.get("volume_fp") or 0)})
        cur = nxt
    if not rows:
        sys.exit(f"[error] {ticker} 1분봉 없음")
    df = pd.DataFrame(rows).drop_duplicates("ts").set_index("ts").sort_index()
    log(f"    {ticker}: {len(df)}개 봉, 마지막 체결 {df['trade'].dropna().index.max()}")
    return df


def exit_value(df, ts, result):
    """청산 가치: 거래 중이면 매수호가, 거래 종료 후면 정산값"""
    last = df["trade"].dropna().index.max()
    bid, _ = px_at(df["bid"], ts)
    ask, _ = px_at(df["ask"], ts)
    if ts > last or (bid is not None and ask is not None and bid <= 0 and ask >= 1):
        return (1.0 if result == "yes" else 0.0), True
    if bid is None or bid <= 0:
        bid, _ = px_at(df["trade"], ts)
    return bid, False


def fee(c, p):
    return math.ceil(round(FEE_RATE * c * p * (1 - p) * 100, 6)) / 100


# ===== 6) 메인 =====
def main():
    log("1/5 영업일 검증")
    check_days()

    if not os.environ.get("FRED_API_KEY"):
        sys.exit("[error] FRED_API_KEY 환경변수 필요")
    fred = Fred(api_key=os.environ["FRED_API_KEY"])
    use_db = bool(os.environ.get("DATABENTO_API_KEY"))
    log(f"  분봉 출처: {'Databento 1분봉' if use_db else 'yfinance (FOMC 5분봉, NFP 1분봉)'}")

    for ev, cfg in EVENTS.items():
        print("\n" + "=" * 110 + f"\n[{ev}] 발표 {cfg['release'].isoformat()}\n" + "=" * 110)
        first_entry, last_exit = min(cfg["entries"].values()), max(cfg["exits"].values())

        log(f"2/5 {ev} FRED: 듀레이션, 결과 검증")
        D, dgs2_chg = fred_inputs(fred, ev, cfg)

        log(f"3/5 {ev} 2년물 선물 분봉")
        bstart, bend = first_entry - timedelta(hours=1), last_exit + timedelta(hours=1)
        zt = zt_databento(bstart, bend) if use_db else zt_yf(bstart, bend, cfg["yf_interval"])

        log(f"4/5 {ev} Kalshi 후보 계약 및 1분봉")
        ms, series = event_markets(cfg["kalshi_event"])
        picks = pick_contracts(ev, ms)
        kc, kres = {}, {}
        for m in picks:
            tk = m["ticker"]
            log(f"  후보: {tk} ({m.get('yes_sub_title')}), result={m.get('result')}")
            kc[tk] = candles(series, tk, bstart - timedelta(hours=25), bend)   # 직전 24시간 거래량 계산용으로 하루 더 받음
            kres[tk] = m.get("result")
            s = kc[tk]["trade"].dropna()
            hit = s[(s.index > cfg["release"]) & (s >= 0.95)]
            log(f"    발표 후 체결가 0.95 이상 첫 시각: {hit.index[0] if len(hit) else '-'}")

        log(f"5/5 {ev} 손익 (단위: 원금 대비 bp)")
        dv01 = D / 1e4                                      # 원금 1당 금리 1bp 손익
        for en, t0 in cfg["entries"].items():
            # 진입 시점별 계약 선택: NFP는 직전 24시간 거래량 상위 2개, FOMC는 후보 그대로
            v24 = {tk: vol_before(df, t0) for tk, df in kc.items()}
            if ev.startswith("NFP"):
                print(f"\n[진입 {en}] NFP 후보 직전 24h 거래량: " + ", ".join(f"{tk.split('-')[-1]} {v:,.0f}" for tk, v in v24.items()))
                chosen = sorted(v24, key=v24.get, reverse=True)[:2]
            else:
                chosen = list(kc)
            entry = {}
            for tk in chosen:
                df = kc[tk]
                ask, at = px_at(df["ask"], t0)
                tr, _ = px_at(df["trade"], t0)
                vol1h = df.loc[(df.index > t0 - timedelta(hours=1)) & (df.index <= t0 + timedelta(hours=1)), "vol"].sum()
                if ask is None:
                    sys.exit(f"[error] {tk} {en} 매도호가 없음")
                entry[tk] = (ask, v24[tk])
                print(f"[진입 {en}] 선택 {tk}: 체결 {tr} / 매도호가 {ask} (봉 {at}), 직전 24h 거래량 {v24[tk]:,.0f}계약 (참고 ±1h {vol1h:,.0f})")

            for ex, t1 in cfg["exits"].items():
                f0, f0t = px_at(zt, t0)
                f1, f1t = px_at(zt, t1)
                if None in (f0, f1):
                    print(f"  -> 청산 {ex}: 선물 가격 결측으로 건너뜀")
                    continue
                rf = f1 / f0 - 1
                dy_bp = -rf / D * 1e4                       # 선물 가격 변화 -> 금리 변화(bp) 환산 (B3)
                long_bp = -D * dy_bp                        # 2년물 현물 손익 = -DV01 x 금리 변화 (원금 대비 bp)
                fut_bp = long_bp - rf * 1e4                 # DV01 1:1 선물 숏 (베이시스 0 가정 -> 0)
                how = []
                for tk in entry:
                    v, st = exit_value(kc[tk], t1, kres[tk])
                    how.append(f"{tk} {'정산' if st else '매도'} {v}")
                print(f"  -> 청산 {ex}: ZT {f0}->{f1} (봉 {f0t.strftime('%m-%d %H:%M')}->{f1t.strftime('%m-%d %H:%M')}) "
                      f"= 2년물 금리 {dy_bp:+.1f}bp | Kalshi: {', '.join(how)}")
                print(f"       롱온리 {long_bp:+.1f}bp | 2년물 선물 숏 {fut_bp:+.1f}bp")
                for cov in COVERS_BP:
                    pnl, share = 0.0, []
                    for tk, (ask, vol) in entry.items():
                        v, st = exit_value(kc[tk], t1, kres[tk])
                        n = cov * dv01 / (1 - ask)              # 원금 1 기준 계약 수
                        ef = 0.0 if st else fee(n * 1e6, v) / 1e6
                        pnl += n * (v - ask) - fee(n * 1e6, ask) / 1e6 - ef
                        share.append(f"{tk} {n * PRINCIPAL_FOR_LIQ / vol * 100 if vol else float('nan'):.1f}%")
                    print(f"       예측시장({cov}bp 커버/계약) {long_bp + pnl*1e4:+.1f}bp "
                          f"[계약 {pnl*1e4:+.1f}bp, $100만 기준 필요계약/직전 24h 거래량: {', '.join(share)}]")
            for cov in COVERS_BP:
                loss = sum(cov * dv01 / (1 - a) * a + fee(cov * dv01 / (1 - a) * 1e6, a) / 1e6 for a, _ in entry.values())
                print(f"     [반사실: 헤지한 결과가 안 나왔을 때] {cov}bp 커버 시 계약 손실 -{loss*1e4:.1f}bp")
        if dgs2_chg is not None:
            print(f"\n  [검증] 발표일 FRED DGS2 일별 변화 {dgs2_chg:+.1f}bp (위 일중 환산값과 방향·크기 비교)")
    print("\n" + "=" * 110)
    log("완료. 출력 전체를 복사해서 보내줘.")


if __name__ == "__main__":
    main()
