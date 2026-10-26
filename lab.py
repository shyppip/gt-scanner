"""
Golden Trend 개선 실험실 — 데이터 1개(기본 BTCUSDT 4시간봉)로 규칙 변형을 비교

현재 기준(GT1-S): SMA20이 SMA200 위면 보유, 아래면 청산 (봉 마감 후 다음 봉 시가 체결)
지난 실험에서 버린 것: ADX·거래량 필터(효과 없음), ATR 추적손절·단기선 이탈 청산(수익 깎음)

각 규칙은 "봉마다 비중"(-1 ~ 1)을 정함. 봉 i 마감에 정한 비중은 i+1 시가부터 적용.
비중이 바뀐 만큼 편도 비용(0.1%)을 뗌. 앞 절반 / 뒤 절반을 따로 보여 과최적화 여부 확인.

실행: python3 lab.py [SYMBOL] [YEARS]   예) python3 lab.py BTCUSDT 3
"""
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
YEARS = float(sys.argv[2]) if len(sys.argv) > 2 else 3
WARMUP = 210
SIDE_COST = 0.001                 # 편도 0.1% (수수료+슬리피지)
BARS_PER_YEAR = 365 * 6
KST = timezone(timedelta(hours=9))
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ───────── 데이터 ─────────
def klines(symbol, years):
    need = int(365 * years * 6) + WARMUP + 20
    out, end = [], None
    while len(out) < need:
        p = {"symbol": symbol, "interval": "4h", "limit": 1000}
        if end:
            p["endTime"] = end
        r = requests.get("https://data-api.binance.vision/api/v3/klines", params=p, timeout=20)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        out = rows + out
        end = rows[0][0] - 1
        time.sleep(0.1)
        if len(rows) < 1000:
            break
    out = sorted({r[0]: r for r in out}.values(), key=lambda r: r[0])[:-1]   # 진행 중 봉 제외
    return [{"t": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4])} for r in out]


# ───────── 지표 ─────────
def sma(vals, n):
    out, s = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(vals, n):
    out, k, e = [None] * len(vals), 2 / (n + 1), None
    for i, v in enumerate(vals):
        e = v if e is None else e + k * (v - e)
        if i >= n - 1:
            out[i] = e
    return out


def realized_vol(closes, n=60):
    """최근 n봉 수익률 표준편차를 연율화"""
    out = [None] * len(closes)
    rets = [0.0] + [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    for i in range(n, len(closes)):
        w = rets[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / (n - 1) * BARS_PER_YEAR)
    return out


# ───────── 규칙: 봉마다 목표 비중 ─────────
def cross_state(f, s, short=False):
    return [None if a is None or b is None else (1.0 if a > b else (-1.0 if short else 0.0)) for a, b in zip(f, s)]


def vol_target(base, vol, target):
    return [None if b is None or v is None else b * min(1.0, target / v) for b, v in zip(base, vol)]


def score3(c, s20, s50, s200):
    """3가지 조건 점수 → 비중 0 / 33 / 67 / 100%: 20>200, 종가>50, 50선 상승"""
    out = [None] * len(c)
    for i in range(6, len(c)):
        if None in (s20[i], s200[i], s50[i], s50[i - 6]):
            continue
        out[i] = ((s20[i] > s200[i]) + (c[i] > s50[i]) + (s50[i] > s50[i - 6])) / 3
    return out


def build(bars):
    c = [b["c"] for b in bars]
    S = {n: sma(c, n) for n in (20, 50, 100, 200)}
    E = {n: ema(c, n) for n in (20, 50, 100, 200)}
    vol = realized_vol(c)
    base = cross_state(S[20], S[200])
    return [
        ("SMA 20/200 (현재)", base),
        ("EMA 20/200", cross_state(E[20], E[200])),
        ("EMA 50/200", cross_state(E[50], E[200])),
        ("EMA 20/100", cross_state(E[20], E[100])),
        ("종가>SMA200", cross_state(c, S[200])),
        ("롱숏 SMA 20/200", cross_state(S[20], S[200], short=True)),
        ("20/200 변동성 60%", vol_target(base, vol, 0.60)),
        ("20/200 변동성 40%", vol_target(base, vol, 0.40)),
        ("점수제 3조건", score3(c, S[20], S[50], S[200])),
    ]


# ───────── 백테스트 ─────────
def run(bars, pos, a, b):
    """bars[a:b] 구간. pos[i] = 봉 i 마감에 정한 비중 → i+1 시가부터 i+2 시가까지 적용"""
    eq, peak, mdd, cur, held, changes, entries = 1.0, 1.0, 0.0, 0.0, 0, 0, 0
    for i in range(a, b - 2):
        tgt = pos[i] if pos[i] is not None else 0.0
        if tgt != 0 and cur != 0 and tgt * cur > 0 and abs(tgt - cur) < 0.1:
            tgt = cur                       # 10%p 미만 비중 조정은 생략 (비용 절약)
        if abs(tgt - cur) > 1e-9:
            eq *= 1 - SIDE_COST * abs(tgt - cur)
            changes += 1
            if cur == 0 and tgt != 0:
                entries += 1
            cur = tgt
        eq *= 1 + cur * (bars[i + 2]["o"] / bars[i + 1]["o"] - 1)
        held += cur != 0
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    n = b - 2 - a
    yrs = n / BARS_PER_YEAR
    cagr = eq ** (1 / yrs) - 1 if yrs > 0 and eq > 0 else 0
    hold = bars[b - 1]["o"] / bars[a + 1]["o"] - 1
    return {"total": (eq - 1) * 100, "mdd": mdd * 100, "calmar": cagr / mdd if mdd else 0,
            "inmkt": held / n * 100, "entries": entries, "changes": changes, "hold": hold * 100}


def table(rows, title):
    out = [f"\n===== {title} =====",
           f"{'규칙':<20}{'누적':>8}{'MDD':>7}{'수익/낙폭':>9}{'참여':>6}{'진입':>5}{'조정':>5}"]
    for name, r in rows:
        out.append(f"{name:<20}{r['total']:>+7.0f}%{r['mdd']:>6.0f}%{r['calmar']:>9.2f}{r['inmkt']:>5.0f}%{r['entries']:>5}{r['changes']:>5}")
    out.append(f"{'(그냥 보유)':<20}{rows[0][1]['hold']:>+7.0f}%")
    return "\n".join(out)


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": text[:3900], "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)


if __name__ == "__main__":
    bars = klines(SYMBOL, YEARS)
    rules = build(bars)
    first = WARMUP
    mid = (first + len(bars)) // 2
    d = lambda i: datetime.fromtimestamp(bars[i]["t"] / 1000, tz=KST).strftime("%Y-%m-%d")
    print(f"{SYMBOL} 4시간봉 {len(bars)}개 · 거래 구간 {d(first)} ~ {d(len(bars) - 1)} · 편도비용 {SIDE_COST * 100:.1f}%")
    print(f"앞 절반 {d(first)}~{d(mid)} / 뒤 절반 {d(mid)}~{d(len(bars) - 1)}")
    print("수익/낙폭 = 연 수익률 ÷ 최대 낙폭 (클수록 효율적)")
    sections = []
    for title, a, b in (("전체 기간", first, len(bars)), ("앞 절반", first, mid), ("뒤 절반", mid, len(bars))):
        sections.append(table([(name, run(bars, pos, a, b)) for name, pos in rules], title))
    print("\n".join(sections))
    tg_send(f"<b>🧪 Golden Trend 실험실 — {SYMBOL} 4h {YEARS:g}년</b>\n<pre>{sections[0]}</pre>")
