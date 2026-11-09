# -*- coding: utf-8 -*-
"""
midterm_hedge_compare.py  (yfinance 버전)
2026.11.3 중간선거 x 반도체 개별주 포트폴리오: 진입 시점(D-7 / D-1 / D-0)별 헤지 손익 비교

비교 대상
  (1) 롱온리
  (2) 나스닥100 선물 숏  : NQ=F (CME E-mini Nasdaq-100 연속물), 베타 헤지
  (3) SOX 숏             : ^SOX (PHLX Semiconductor 지수; 야후에 SOX 선물이 없어 지수로 대용), 베타 헤지
  (4) 예측시장 헤지      : Kalshi "상원 민주당" Yes + "텍사스 주지사 민주당(Hinojosa)" Yes

주가 데이터
  - Yahoo Finance (yfinance), auto_adjust=False 의 Close(배당 미조정 종가)
  - 베타: 진입 직전 1년(2025-10-27 ~ 2026-10-27) 일별 수익률, ^NDX / ^SOX 지수 기준
    (NQ=F 연속물은 롤오버일 가격 점프가 있어 베타 추정에는 지수를 사용)

Kalshi 가격 정의 (선행연구 근거)
  - 체결(trade) 데이터 기준, 기준시각 직전 마지막 체결가
    (Diercks, Katz & Wright 2026, p.13-14: 체결 데이터 사용, 거래 없으면 직전 체결가 이월,
     꼬리 구간 호가 스프레드가 넓어 호가 중간값 대신 체결가 사용)
  - 기준시각 = 해당일 미 동부 16:00 (주식 종가 시각과 일치)

실행: pip3 install requests yfinance   ->   python3 midterm_hedge_compare.py
"""

# ===== 0) 라이브러리 =====
import math
import sys
import time
from datetime import datetime
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
PRINCIPAL = 1_000_000            # 포트폴리오 원금 ($)
COVER_PER_CONTRACT = 0.03        # 사전 가정: 각 계약(상원/주지사)이 원금의 3% 손실을 덮도록 사이징
FEE_RATE = 0.07                  # Kalshi 수수료 = ceil(0.07 x C x P x (1-P)) (센트 단위 올림)

STOCKS = ["NVDA", "AVGO", "MU", "AMD", "TXN"]   # SOXQ 비중 상위 5종목 (2026.10.23 기준), 동일가중
HEDGE_PNL = {"nasdaq": "NQ=F", "sox": "^SOX"}    # 헤지 손익 계산용 상품
HEDGE_BETA = {"nasdaq": "^NDX", "sox": "^SOX"}   # 베타 추정용 지수
BETA_START, BETA_END = "2025-10-27", "2026-10-27"
EXIT_DAY = "2026-11-04"

EVENTS = {                       # Kalshi 이벤트 티커 -> 민주당 쪽 마켓 자동 탐색
    "senate_dem": "CONTROLS-2026",
    "txgov_dem": "GOVPARTYTX-26",
}
MANUAL_TICKERS = {               # 자동 탐색 실패 시 마켓 티커 직접 입력
    "senate_dem": None,
    "txgov_dem": None,
}

# 진입 시점: (라벨, Kalshi 기준시각, 주식 진입일 종가)
ENTRIES = [
    ("D-7 (10.27)", datetime(2026, 10, 27, 16, 0, tzinfo=ET), "2026-10-27"),
    ("D-1 (11.2)", datetime(2026, 11, 2, 16, 0, tzinfo=ET), "2026-11-02"),
    ("D-0 (11.3 장마감, 개표 전)", datetime(2026, 11, 3, 16, 0, tzinfo=ET), "2026-11-03"),
]
EXIT_TIME = datetime(2026, 11, 4, 16, 0, tzinfo=ET)
CARRY_FORWARD_DAYS = 7           # 직전 24시간 체결이 없으면 최대 7일 전까지 이월 탐색


# ===== 2) 주가 다운로드 (yfinance) =====
def load_prices():
    """종목·지수·선물 일별 종가 다운로드 후 결측 확인"""
    tickers = STOCKS + sorted(set(HEDGE_PNL.values()) | set(HEDGE_BETA.values()))
    log(f"  yfinance 다운로드: {tickers}")
    df = yf.download(tickers, start="2025-10-01", end="2026-11-07",
                     auto_adjust=False, progress=False)["Close"]
    df.index = pd.to_datetime(df.index).tz_localize(None)
    log(f"  받은 기간 {df.index.min().date()} ~ {df.index.max().date()}, {len(df)}행")
    need = [d for _, _, d in ENTRIES] + [EXIT_DAY]
    for d in need:
        if pd.Timestamp(d) not in df.index:
            sys.exit(f"[error] {d} 종가가 없음 (휴장 또는 데이터 누락)")
        row = df.loc[d]
        if row.isna().any():
            log(f"  [warn] {d} 결측: {list(row[row.isna()].index)}")
    return df


def beta(df, index_sym):
    """포트폴리오(일별 동일가중) 대비 베타, 진입 직전 1년 일별 수익률"""
    sub = df.loc[BETA_START:BETA_END, STOCKS + [index_sym]].dropna()
    r = sub.pct_change().dropna()
    port = r[STOCKS].mean(axis=1)
    b = port.cov(r[index_sym]) / r[index_sym].var()
    c = port.corr(r[index_sym])
    return b, c, len(r)


def ret(df, sym, a, b):
    """a일 종가 -> b일 종가 수익률"""
    return df.loc[b, sym] / df.loc[a, sym] - 1


# ===== 3) Kalshi API 함수 =====
def api_get(path, params=None, retries=3):
    """GET 요청 (재시도 포함)"""
    for t in range(retries):
        try:
            r = requests.get(BASE + path, params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            log(f"  HTTP {r.status_code} {path} {params} -> {r.text[:200]}")
        except requests.RequestException as e:
            log(f"  요청 오류 {e}")
        time.sleep(1 + t)
    return None


def find_dem_market(event_ticker):
    """이벤트 안에서 민주당 쪽 마켓 탐색 (현재 경로 -> historical 경로)"""
    log(f"  이벤트 {event_ticker} 마켓 목록 조회")
    data = api_get(f"/events/{event_ticker}")
    markets = (data or {}).get("markets") or []
    if not markets:
        log("    현재 경로에 없음 -> historical 경로 조회")
        data = api_get("/historical/markets", {"event_ticker": event_ticker, "limit": 100})
        markets = (data or {}).get("markets") or []
    for m in markets:
        log(f"    - {m.get('ticker')} | {m.get('yes_sub_title')} | result={m.get('result')} | volume_fp={m.get('volume_fp')}")
    for m in markets:
        label = f"{m.get('yes_sub_title', '')} {m.get('ticker', '')}".lower()
        if "democrat" in label or label.endswith("-d") or "hinojosa" in label:
            return m
    return None


def fetch_trades(ticker, min_ts, max_ts):
    """구간 내 전체 체결 (현재 경로, 없으면 historical 경로), 페이지네이션 처리"""
    for path in ("/markets/trades", "/historical/trades"):
        out, cursor = [], ""
        while True:
            params = {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = api_get(path, params)
            if not data:
                break
            out += data.get("trades", [])
            cursor = data.get("cursor") or ""
            if not cursor:
                break
        if out:
            log(f"    {path}: {len(out)}건")
            return out
    return []


def price_at(ticker, when):
    """기준시각 직전 마지막 체결가, 체결 시각, 직전 24시간 거래량(계약 수)"""
    end = int(when.timestamp())
    for back in range(1, CARRY_FORWARD_DAYS + 1):
        trades = fetch_trades(ticker, end - 86400 * back, end)
        if trades:
            trades.sort(key=lambda t: t["created_time"])
            last = trades[-1]
            vol24 = sum(float(t["count_fp"]) for t in trades
                        if datetime.fromisoformat(t["created_time"].replace("Z", "+00:00")).timestamp() >= end - 86400)
            return float(last["yes_price_dollars"]), last["created_time"], vol24
        log(f"    직전 {back}일 체결 없음 -> 이월 탐색")
    return None, None, 0.0


def fee(contracts, p):
    """Kalshi 거래 수수료 (센트 단위 올림)"""
    return math.ceil(round(FEE_RATE * contracts * p * (1 - p) * 100, 6)) / 100


# ===== 4) 메인 =====
def main():
    log("1/5 주가 다운로드")
    df = load_prices()

    log("2/5 포트폴리오 베타 계산 (진입 직전 1년)")
    betas = {}
    for k, sym in HEDGE_BETA.items():
        b, c, n = beta(df, sym)
        betas[k] = b
        log(f"  {k}: beta vs {sym} = {b:.3f} (corr {c:.3f}, 표본 {n}일)")

    log("3/5 Kalshi 마켓 탐색")
    markets = {}
    for key, ev in EVENTS.items():
        if MANUAL_TICKERS[key]:
            markets[key] = {"ticker": MANUAL_TICKERS[key], "result": "?"}
            continue
        m = find_dem_market(ev)
        if not m:
            sys.exit(f"[error] {ev} 민주당 마켓을 못 찾음 -> MANUAL_TICKERS에 직접 입력해줘")
        markets[key] = m
        log(f"  선택: {key} = {m['ticker']} (result={m.get('result')})")

    log("4/5 Kalshi 체결가 수집")
    exit_px = {}
    for key, m in markets.items():
        p, ts, _ = price_at(m["ticker"], EXIT_TIME)
        exit_px[key] = p
        log(f"  [청산 참고] {key} 11.4 16:00 ET 직전 체결가 {p} ({ts})")
    rows = []
    for label, when, sday in ENTRIES:
        log(f"  진입 {label} = {when.isoformat()}")
        info = {}
        for key, m in markets.items():
            p, ts, vol24 = price_at(m["ticker"], when)
            if p is None:
                sys.exit(f"[error] {m['ticker']} {label} 체결가 없음")
            info[key] = (p, ts, vol24)
            log(f"    {key}: 체결가 {p:.4f} ({ts}), 직전 24h 거래량 {vol24:,.0f}계약")
        rows.append((label, sday, info))

    log("5/5 손익 계산")
    print("\n" + "=" * 100)
    for label, sday, info in rows:
        # 주식·헤지 구간 수익률 (진입일 종가 -> 11.4 종가)
        rp = sum(ret(df, s, sday, EXIT_DAY) for s in STOCKS) / len(STOCKS)
        rN = ret(df, HEDGE_PNL["nasdaq"], sday, EXIT_DAY)
        rS = ret(df, HEDGE_PNL["sox"], sday, EXIT_DAY)
        long_pnl = PRINCIPAL * rp
        nq_pnl = long_pnl - betas["nasdaq"] * PRINCIPAL * rN
        sx_pnl = long_pnl - betas["sox"] * PRINCIPAL * rS

        # 예측시장 헤지 손익
        pm_pnl, detail = 0.0, []
        for key, (p, ts, vol24) in info.items():
            res = markets[key].get("result")
            payoff = 1.0 if res == "yes" else (0.0 if res == "no" else exit_px[key])  # 정산값, 미정산이면 11.4 체결가
            n_c = round(PRINCIPAL * COVER_PER_CONTRACT / (1 - p), 2)
            cost, f = n_c * p, fee(n_c, p)
            pnl = n_c * payoff - cost - f
            pm_pnl += pnl
            share = n_c / vol24 * 100 if vol24 else float("nan")
            detail.append(f"{key}: {n_c:,.0f}계약 @{p:.3f} -> {payoff:.2f}, 비용 {cost:,.0f}, 수수료 {f:,.0f}, "
                          f"손익 {pnl:,.0f} | 24h 거래량 대비 {share:.1f}%")

        print(f"\n[진입 {label}]  주식 구간 {sday} 종가 -> {EXIT_DAY} 종가")
        print(f"  포트폴리오 {rp*100:+.2f}% | NQ=F {rN*100:+.2f}% | ^SOX {rS*100:+.2f}%")
        for s in STOCKS:
            print(f"    {s}: {ret(df, s, sday, EXIT_DAY)*100:+.2f}%")
        print(f"  롱온리                       {long_pnl:>12,.0f}")
        print(f"  나스닥100 선물 숏 (β={betas['nasdaq']:.2f})   {nq_pnl:>12,.0f}")
        print(f"  SOX 숏 (β={betas['sox']:.2f})              {sx_pnl:>12,.0f}")
        print(f"  예측시장 헤지                {long_pnl + pm_pnl:>12,.0f}  (계약 손익 {pm_pnl:,.0f})")
        for d in detail:
            print("    - " + d)
    print("=" * 100)
    log("완료. 위 결과를 그대로 복사해서 보내줘.")


if __name__ == "__main__":
    main()
