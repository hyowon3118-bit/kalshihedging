# -*- coding: utf-8 -*-
"""
midterm_intraday.py
2026.11.3 중간선거 x 반도체 개별주 포트폴리오: 1분 단위 진입·청산 시점별 헤지 손익 비교

[합의한 가정]
 A. 시간·영업일
   A1 상원 '사실상 확정 시각' = 투표 마감(11.3 18:00 ET) 이후 계약 체결가가 처음 0.95 이상이 된 1분봉
   A2 모든 시각은 미 동부(America/New_York). 서머타임 종료(11.1) 자동 반영
   A3 NYSE 2026 휴장일 목록으로 진입·청산일이 영업일인지 검증 (Kiplinger)
   A4 주식은 프리·애프터마켓 포함(4:00-20:00 ET), 그 밖의 시간은 마지막 가격 유지
       NQ=F는 일 18:00~금 17:00 ET(월-목 17-18시 휴장), Kalshi는 24시간
   A5 SOX 헤지는 SOX=F(CME E-mini PHLX 반도체 선물). 1분봉이 없으면 ^SOX(정규장만)로 대체하고 경고
 B. 구간
   B1 데이터 구간 11.2 15:00 ~ 11.4 17:00 ET
   B2 진입 후보: 11.2 16:00 / 11.3 09:30 / 11.3 16:00 / 11.3 18:00
   B3 청산 후보: 11.4 04:00 / 11.4 09:30 / 11.4 16:00
   B4 11.5 이후 제외
 C. 가격
   C1 확률 측정 = 1분봉 체결가 종가, 헤지 매수 = 1분봉 매도호가(yes_ask) 종가, 청산 매도 = 매수호가(yes_bid) 종가
   C2 주식·선물 = 1분봉 종가, 거래 없으면 직전 값 이월
 D. 규모·사이징
   D1 손익은 원금 대비 %로 출력
   D2 사이징 민감도: 계약당 원금의 1% / 2% / 3% 손실을 덮도록 (계약 수 = 커버 금액 / (1 - 진입 매도호가))
   D3 유동성 = 진입 직전 24시간 Kalshi 거래량 대비 필요 계약 수 비율 (전후 1시간 값은 참고로 출력)
   D4 선물 계약 수는 소수 허용 (원금 % 표시라 무관)
   D5 베타 = 진입 직전 1년(2025-10-27 ~ 2026-10-27) 일별 수익률, ^NDX / ^SOX 기준
 E. 비용·정산
   E1 Kalshi 테이커 수수료 = ceil(0.07 x C x P x (1-P)), 진입·청산 매매 모두 부과
   E2 정산 수수료 0 (가정)
   E3 청산 시각에 거래 중이면 매수호가에 매도(청산 수수료 부과),
      결과 확정으로 거래가 멈춘 뒤면 정산값(yes=1, no=0)으로 평가(정산 수수료 0)
   E5 SOX=F 1분봉이 SOX_MIN_BARS 미만이면 거래 없는 시간대(정규장 외) SOX 헤지는 '평가 불가'로 표시
   E4 선물·주식 거래비용 0

실행: pip3 install requests yfinance   ->   python3 midterm_intraday.py
"""

# ===== 0) 라이브러리 =====
import math
import sys
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

try:
    import requests
    import yfinance as yf
    import pandas as pd
except ImportError:
    sys.exit("[error] 패키지 설치 필요: pip3 install requests yfinance")

ET = ZoneInfo("America/New_York")
BASE = "https://api.elections.kalshi.com/trade-api/v2"


def log(msg):
    """진행 상황 로그 출력"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ===== 1) 설정값 =====
STOCKS = ["NVDA", "AVGO", "MU", "AMD", "TXN"]          # SOXQ 비중 상위 5종목(2026.10.23), 동일가중
NQ, SOXF, SOXIDX, NDX = "NQ=F", "SOX=F", "^SOX", "^NDX"
BETA_START, BETA_END = "2025-10-27", "2026-10-27"
COVERS = [0.01, 0.02, 0.03]                             # D2 사이징 민감도
FEE_RATE = 0.07
SAVE_CSV = False                                        # True면 1분봉 원자료를 같은 폴더에 CSV로 저장
SOX_MIN_BARS = 500                                      # E5 SOX=F 유동성 판단 기준 (데이터 구간 내 1분봉 수)
SOX_STALE_MIN = 30                                      # 청산·진입 시각 기준 마지막 체결이 30분 넘게 지났으면 평가 불가

KALSHI = {                                              # 확인된 마켓 티커 (이전 실행 로그)
    "senate_dem": "CONTROLS-2026-D",
    "txgov_dem": "GOVPARTYTX-26-D",
}

def t(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)

DATA_START, DATA_END = t(2026, 11, 2, 15), t(2026, 11, 4, 17)
ENTRIES = {
    "11.2 16:00 (D-1 종가)": t(2026, 11, 2, 16),
    "11.3 09:30 (선거일 개장)": t(2026, 11, 3, 9, 30),
    "11.3 16:00 (선거일 종가)": t(2026, 11, 3, 16),
    "11.3 18:00 (투표 마감)": t(2026, 11, 3, 18),
}
EXITS = {
    "11.4 04:00 (프리마켓 시작)": t(2026, 11, 4, 4),
    "11.4 09:30 (정규장 개장)": t(2026, 11, 4, 9, 30),
    "11.4 16:00 (종가)": t(2026, 11, 4, 16),
}
POLL_CLOSE = t(2026, 11, 3, 18)

# NYSE 2026 휴장일 (Kiplinger) — 진입·청산일 검증용
NYSE_HOLIDAYS_2026 = {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
                      date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
                      date(2026, 11, 26), date(2026, 12, 25)}


# ===== 2) 영업일 검증 =====
def check_trading_days():
    """진입·청산 시각의 날짜가 NYSE 영업일인지 확인"""
    for name, ts in {**ENTRIES, **EXITS}.items():
        d = ts.date()
        ok = d.weekday() < 5 and d not in NYSE_HOLIDAYS_2026
        log(f"  {name}: {ts.isoformat()} -> {'영업일' if ok else '휴장일!'}")
        if not ok:
            sys.exit("[error] 휴장일이 포함됨. ENTRIES/EXITS 수정 필요")


# ===== 3) 베타 (일별 1년) =====
def daily_betas():
    """포트폴리오(일별 동일가중) 대비 ^NDX, ^SOX 베타"""
    df = yf.download(STOCKS + [NDX, SOXIDX], start="2025-10-20", end="2026-10-28",
                     auto_adjust=False, progress=False)["Close"]
    r = df.loc[BETA_START:BETA_END].dropna().pct_change().dropna()
    port = r[STOCKS].mean(axis=1)
    out = {}
    for k, sym in {"nasdaq": NDX, "sox": SOXIDX}.items():
        out[k] = port.cov(r[sym]) / r[sym].var()
        log(f"  beta vs {sym} = {out[k]:.3f} (corr {port.corr(r[sym]):.3f}, 표본 {len(r)}일)")
    return out


# ===== 4) 1분봉 (yfinance) =====
def to_et(idx):
    """인덱스를 미 동부 시각으로 변환"""
    idx = pd.to_datetime(idx)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return idx.tz_convert(ET)


def minute_bars():
    """주식(프리·애프터 포함)·선물·지수 1분봉 종가. 인덱스 = 봉 종료 시각(시작+1분)"""
    tickers = STOCKS + [NQ, SOXF, SOXIDX]
    df = yf.download(tickers, start="2026-11-02", end="2026-11-05", interval="1m",
                     prepost=True, auto_adjust=False, progress=False)["Close"]
    df.index = to_et(df.index) + pd.Timedelta(minutes=1)
    df = df.loc[DATA_START:DATA_END]
    for s in tickers:
        if s not in df.columns:
            df[s] = float("nan")                         # 다운로드 실패 종목은 빈 열로 둠
        n = df[s].notna().sum()
        log(f"  {s}: 1분봉 {n}개, {df[s].first_valid_index() if n else '-'} ~ {df[s].last_valid_index() if n else '-'}")
    return df


def px_at(series, ts):
    """ts 시각 이하 마지막 유효 가격 (이월)"""
    s = series.dropna()
    s = s[s.index <= ts]
    return (float(s.iloc[-1]), s.index[-1]) if len(s) else (None, None)


# ===== 5) Kalshi 1분봉 =====
def api_get(path, params=None, retries=3):
    for k in range(retries):
        try:
            r = requests.get(BASE + path, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            log(f"  HTTP {r.status_code} {path} -> {r.text[:150]}")
        except requests.RequestException as e:
            log(f"  요청 오류 {e}")
        time.sleep(1 + k)
    return None


def f(x):
    """문자열 달러 값 -> float (None 허용)"""
    return None if x in (None, "") else float(x)


def kalshi_candles(ticker):
    """1분봉: 체결가 종가, 매도·매수호가 종가, 거래량. 6시간 단위로 나눠 조회"""
    series = ticker.split("-")[0]
    rows, cur = [], DATA_START - timedelta(hours=25)        # 첫 진입(11.2 16:00) 직전 24시간 거래량 계산용으로 하루 더 받음
    while cur < DATA_END + timedelta(hours=1):
        nxt = cur + timedelta(hours=6)
        params = {"start_ts": int(cur.timestamp()), "end_ts": int(nxt.timestamp()), "period_interval": 1}
        data = api_get(f"/series/{series}/markets/{ticker}/candlesticks", params)
        if not data or not data.get("candlesticks"):
            data = api_get(f"/historical/markets/{ticker}/candlesticks", params)
        for c in (data or {}).get("candlesticks", []):
            rows.append({
                "ts": datetime.fromtimestamp(c["end_period_ts"], tz=ET),
                "trade": f((c.get("price") or {}).get("close_dollars")),
                "ask": f((c.get("yes_ask") or {}).get("close_dollars")),
                "bid": f((c.get("yes_bid") or {}).get("close_dollars")),
                "vol": f(c.get("volume_fp")) or 0.0,
            })
        cur = nxt
    df = pd.DataFrame(rows).drop_duplicates("ts").set_index("ts").sort_index()
    log(f"  {ticker}: 1분봉 {len(df)}개 ({df.index.min()} ~ {df.index.max()}), 체결 있는 봉 {df['trade'].notna().sum()}개")
    return df


def kalshi_result(ticker):
    """마켓 정산 결과 조회 (yes/no/'')"""
    data = api_get(f"/markets/{ticker}") or {}
    m = data.get("market") or {}
    log(f"  {ticker}: status={m.get('status')}, result={m.get('result')}, close_time={m.get('close_time')}")
    return m.get("result")


def exit_value(df, ts, result):
    """청산 가치: 거래 중이면 매수호가(매도 체결), 거래 종료 후면 정산값. (가격, 정산여부) 반환"""
    last_trade_ts = df["trade"].dropna().index.max()
    bid, _ = px_at(df["bid"], ts)
    ask, _ = px_at(df["ask"], ts)
    halted = ts > last_trade_ts or (bid is not None and ask is not None and bid <= 0.0 and ask >= 1.0)
    if halted:
        if result not in ("yes", "no"):
            sys.exit("[error] 거래 종료 후인데 정산 결과가 없음")
        return (1.0 if result == "yes" else 0.0), True
    if bid is None or bid <= 0:
        bid, _ = px_at(df["trade"], ts)
    return bid, False


def first_cross(df, level=0.95, after=POLL_CLOSE):
    """투표 마감 이후 체결가가 처음 level 이상이 된 시각"""
    s = df.loc[df.index > after, "trade"].dropna()
    hit = s[s >= level]
    return hit.index[0] if len(hit) else None


def fee(c, p):
    return math.ceil(round(FEE_RATE * c * p * (1 - p) * 100, 6)) / 100


# ===== 6) 메인 =====
def main():
    log("1/6 영업일 검증")
    check_trading_days()

    log("2/6 베타 계산 (일별 1년)")
    beta = daily_betas()

    log("3/6 주식·선물 1분봉 다운로드")
    bars = minute_bars()
    sox_sym = SOXF if bars[SOXF].notna().sum() > 0 else SOXIDX
    if sox_sym == SOXIDX:
        log("  [warn] SOX=F 1분봉 없음 -> ^SOX(정규장만)로 대체")
    sox_thin = bars[sox_sym].notna().sum() < SOX_MIN_BARS
    if sox_thin:
        log(f"  [warn] {sox_sym} 1분봉 {bars[sox_sym].notna().sum()}개로 얇음 -> 마지막 체결이 {SOX_STALE_MIN}분 넘게 지난 시각은 SOX 헤지 평가 불가")

    log("4/6 Kalshi 1분봉 다운로드")
    kc = {k: kalshi_candles(tk) for k, tk in KALSHI.items()}
    kres = {k: kalshi_result(tk) for k, tk in KALSHI.items()}
    for k, df in kc.items():
        log(f"  {k} 마지막 체결 1분봉: {df['trade'].dropna().index.max()}")
        log(f"  {k} 사실상 확정(체결가 0.95 이상 첫 시각): {first_cross(df)}")
    if SAVE_CSV:
        bars.to_csv("intraday_bars.csv")
        for k, df in kc.items():
            df.to_csv(f"kalshi_{k}_1m.csv")
        log("  CSV 저장 완료")

    log("5/6 시점별 가격 스냅샷")
    snap_times = {**ENTRIES, **EXITS}
    for name, ts in snap_times.items():
        line = [f"{name}:"]
        for k, df in kc.items():
            tr, _ = px_at(df["trade"], ts)
            ak, _ = px_at(df["ask"], ts)
            bd, _ = px_at(df["bid"], ts)
            line.append(f"{k} 체결 {tr} / 매도 {ak} / 매수 {bd} (스프레드 {None if ak is None or bd is None else round(ak - bd, 3)})")
        log("  " + " | ".join(line))

    log("6/6 손익 계산 (원금 대비 %)")
    print("\n" + "=" * 110)
    for en, t0 in ENTRIES.items():
        # 진입 시 Kalshi 매도호가와 거래량 (유동성 기준 = 진입 직전 24시간, 전후 1시간은 참고)
        entry = {}
        for k, df in kc.items():
            ask, at = px_at(df["ask"], t0)
            vol = df.loc[(df.index > t0 - timedelta(hours=24)) & (df.index <= t0), "vol"].sum()
            vol1h = df.loc[(df.index > t0 - timedelta(hours=1)) & (df.index <= t0 + timedelta(hours=1)), "vol"].sum()
            if ask is None:
                sys.exit(f"[error] {k} {en} 매도호가 없음")
            entry[k] = (ask, at, vol)
            print(f"  [참고] {k} {en} 전후 1시간 거래량 {vol1h:,.0f}계약")
        print(f"\n[진입 {en}]")
        for k, (ask, at, vol) in entry.items():
            print(f"  {k}: 매도호가 {ask} (봉 {at}), 직전 24시간 거래량 {vol:,.0f}계약")

        for ex, t1 in EXITS.items():
            if t1 <= t0:
                continue
            # 주식·선물 구간 수익률
            rets = {}
            for s in STOCKS + [NQ, sox_sym]:
                p0, _ = px_at(bars[s], t0)
                p1, _ = px_at(bars[s], t1)
                rets[s] = None if (p0 is None or p1 is None) else p1 / p0 - 1
            if any(rets[s] is None for s in STOCKS + [NQ, sox_sym]):
                print(f"  -> 청산 {ex}: 가격 결측으로 건너뜀 {[s for s in rets if rets[s] is None]}")
                continue
            rp = sum(rets[s] for s in STOCKS) / len(STOCKS)
            long_pct = rp * 100
            nq_pct = (rp - beta["nasdaq"] * rets[NQ]) * 100
            sx_pct = (rp - beta["sox"] * rets[sox_sym]) * 100

            # SOX 헤지 평가 가능 여부 (E5): 진입·청산 시각 모두 최근 체결이 있어야 함
            _, s0 = px_at(bars[sox_sym], t0)
            _, s1 = px_at(bars[sox_sym], t1)
            stale = sox_thin and (t0 - s0 > timedelta(minutes=SOX_STALE_MIN) or t1 - s1 > timedelta(minutes=SOX_STALE_MIN))
            sx_txt = "평가 불가(체결 없음)" if stale else f"{sx_pct:+.2f}%"

            # 예측시장 헤지 (사이징 민감도)
            pm, how = [], []
            for k in entry:
                v, settled = exit_value(kc[k], t1, kres[k])
                how.append(f"{k} {'정산' if settled else '매도'} {v}")
            for cov in COVERS:
                pnl, share = 0.0, []
                for k, (ask, _, vol) in entry.items():
                    v, settled = exit_value(kc[k], t1, kres[k])
                    n = cov / (1 - ask)                      # 원금 1 기준 계약 수
                    exit_fee = 0.0 if settled else fee(n * 1e6, v) / 1e6
                    pnl += n * (v - ask) - fee(n * 1e6, ask) / 1e6 - exit_fee
                    share.append(f"{k} {n * 1e6 / vol * 100 if vol else float('nan'):.1f}%")
                pm.append((cov, (rp + pnl) * 100, pnl * 100, ", ".join(share)))

            print(f"  -> 청산 {ex}: 포트폴리오 {long_pct:+.2f}% | {NQ} {rets[NQ]*100:+.2f}% | {sox_sym} {rets[sox_sym]*100:+.2f}% | Kalshi 청산: {', '.join(how)}")
            print(f"       롱온리 {long_pct:+.2f}% | 나스닥100 숏(β={beta['nasdaq']:.2f}) {nq_pct:+.2f}% | SOX 숏(β={beta['sox']:.2f}) {sx_txt}")
            for cov, tot, cpnl, share in pm:
                print(f"       예측시장(계약당 {cov*100:.0f}% 커버) {tot:+.2f}%  [계약 손익 {cpnl:+.2f}%, 원금 $100만 기준 필요 계약/직전 24h 거래량: {share}]")

        # 반사실: 두 계약 모두 No로 정산되면 계약 손실 (프리미엄 + 진입 수수료)
        for cov in COVERS:
            loss = sum(cov / (1 - ask) * ask + fee(cov / (1 - ask) * 1e6, ask) / 1e6 for ask, _, _ in entry.values())
            print(f"     [반사실: 공화 둘 다 승리] 계약당 {cov*100:.0f}% 커버 시 계약 손실 -{loss*100:.2f}% (포트폴리오 손익은 별도 가정 필요)")
    print("=" * 110)
    log("완료. 위 결과를 그대로 복사해서 보내줘.")


if __name__ == "__main__":
    main()
