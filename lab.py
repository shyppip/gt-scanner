"""
Golden Trend 개선 실험실 — 데이터 1개(기본 BTCUSDT 4시간봉)로 규칙 변형을 비교

현재 기준(GT1-S): SMA20이 SMA200 위면 보유, 아래면 청산 (봉 마감 후 다음 봉 시가 체결)
지난 실험에서 버린 것: ADX·거래량 필터(효과 없음), ATR 추적손절·단기선 이탈 청산(수익 깎음),
  롱숏(낙폭 45%), 변동성 비중 조절(수익만 줄어듦), 점수제 비중(잦은 매매로 손해), 종가>200선(잦은 매매)
4차 결과: EMA 20/200이 SMA보다 낙폭이 작음 → 주변 길이도 좋은지 확인 중

각 규칙은 "봉마다 비중"(-1 ~ 1)을 정함. 봉 i 마감에 정한 비중은 i+1 시가부터 적용.
비중이 바뀐 만큼 편도 비용(0.1%)을 뗌. 앞 절반 / 뒤 절반을 따로 보여 과최적화 여부 확인.

실행: python3 lab.py [SYMBOL] [YEARS]   예) python3 lab.py BTCUSDT 3
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import requests

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
YEARS = float(sys.argv[2]) if len(sys.argv) > 2 else 3
WARMUP = 260
SIDE_COST = 0.001                 # 편도 0.1% (수수료+슬리피지)
KST = timezone(timedelta(hours=9))
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ───────── 데이터 ─────────
def klines(symbol, years, interval="4h"):
    per_day = {"15m": 96, "1h": 24, "4h": 6, "1d": 1}[interval]
    need = int(365 * years * per_day) + WARMUP + 20
    out, end = [], None
    while len(out) < need:
        p = {"symbol": symbol, "interval": interval, "limit": 1000}
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


# ───────── 기준 규칙: EMA 20/200 (현재 GT1-S) ─────────
def cross_state(f, s):
    return [None if a is None or b is None else (1.0 if a > b else 0.0) for a, b in zip(f, s)]


def run(bars, pos, a, b, bpy):
    """비중 방식 백테스트. pos[i] = 봉 i 마감에 정한 비중 → i+1 시가부터 적용"""
    eq, peak, mdd, cur, entries = 1.0, 1.0, 0.0, 0.0, 0
    for i in range(a, b - 2):
        tgt = pos[i] if pos[i] is not None else 0.0
        if abs(tgt - cur) > 1e-9:
            eq *= 1 - SIDE_COST * abs(tgt - cur)
            entries += cur == 0 and tgt != 0
            cur = tgt
        eq *= 1 + cur * (bars[i + 2]["o"] / bars[i + 1]["o"] - 1)
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
    yrs = (b - 2 - a) / bpy
    cagr = eq ** (1 / yrs) - 1 if eq > 0 else -1
    return {"n": entries, "win": None, "pf": None, "total": (eq - 1) * 100, "mdd": mdd * 100,
            "calmar": cagr / mdd if mdd else 0, "hold": (bars[b - 1]["o"] / bars[a + 1]["o"] - 1) * 100}


# ───────── 볼벤v1: 스퀴즈 모멘텀 (볼린저 20,2 / 켈트너 20,1.5 TR / MACD 12,26,9) ─────────
def squeeze_signals(bars, bb_len=20, bb_mult=2.0, kc_len=20, kc_mult=1.5):
    c = [b["c"] for b in bars]
    basis = sma(c, bb_len)
    dev = [None] * len(c)
    for i in range(bb_len - 1, len(c)):
        w = c[i - bb_len + 1:i + 1]
        m = sum(w) / bb_len
        dev[i] = (sum((x - m) ** 2 for x in w) / bb_len) ** 0.5      # Pine ta.stdev (모집단)
    tr = [bars[0]["h"] - bars[0]["l"]] + [max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - bars[i - 1]["c"]),
                                              abs(bars[i]["l"] - bars[i - 1]["c"])) for i in range(1, len(bars))]
    kc_mid, kc_rng = ema(c, kc_len), ema(tr, kc_len)                  # Pine ta.kc(useTrueRange=true)
    macd = [None if a is None or b is None else a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    sig = [None] * len(c)
    start = next(i for i, v in enumerate(macd) if v is not None)
    s9 = ema(macd[start:], 9)
    for k, v in enumerate(s9):
        sig[start + k] = v
    hist = [None if m is None or s is None else m - s for m, s in zip(macd, sig)]
    on = [None] * len(c)
    for i in range(len(c)):
        if basis[i] is None or kc_mid[i] is None:
            continue
        bu, bl = basis[i] + bb_mult * dev[i], basis[i] - bb_mult * dev[i]
        ku, kl = kc_mid[i] + kc_mult * kc_rng[i], kc_mid[i] - kc_mult * kc_rng[i]
        on[i] = bu < ku and bl > kl
    out = [0] * len(c)
    for i in range(1, len(c)):
        if on[i - 1] and on[i] is False and hist[i] is not None:
            out[i] = 1 if hist[i] > 0 else (-1 if hist[i] < 0 else 0)
    return out


def run_squeeze(bars, sigs, a, b, bpy, tp=2.0, sl=1.0, long_only=False):
    """트레이딩뷰 방식 재현: 신호 봉 마감 → 다음 봉 시가 진입, 익절/손절 주문은 그다음 봉부터 유효.
    한 봉에서 익절·손절이 둘 다 닿으면 시가가 고가에 가까우면 고가 먼저, 아니면 저가 먼저(트레이딩뷰 가정).
    반대 신호가 나오면 다음 봉 시가에 뒤집기. 매 거래 자산 100% 복리, 편도 비용 SIDE_COST."""
    eq, peak, mdd = 1.0, 1.0, 0.0
    side, entry, armed_from = 0, 0.0, None
    trades, pending = [], 0

    def close_at(px):
        nonlocal eq, side
        r = side * (px / entry - 1) - 2 * SIDE_COST
        trades.append((side, r))
        eq *= 1 + r
        side = 0

    for i in range(a, b):
        bar = bars[i]
        if pending:                                   # 직전 봉 신호 → 이번 봉 시가 체결
            if side and side != pending:
                close_at(bar["o"])
            if side == 0:
                side, entry, armed_from = pending, bar["o"], i + 1
            pending = 0
        if side and i >= armed_from:                  # 익절·손절 확인
            up, dn = (entry * (1 + tp / 100), entry * (1 - sl / 100)) if side > 0 else (entry * (1 + sl / 100), entry * (1 - tp / 100))
            hit = None
            if bar["o"] >= up:
                hit = bar["o"]
            elif bar["o"] <= dn:
                hit = bar["o"]
            else:
                hi_first = (bar["h"] - bar["o"]) <= (bar["o"] - bar["l"])
                for lvl in ((up, dn) if hi_first else (dn, up)):
                    if (lvl == up and bar["h"] >= up) or (lvl == dn and bar["l"] <= dn):
                        hit = lvl
                        break
            if hit is not None:
                close_at(hit)
        s = sigs[i]
        if s and not (long_only and s < 0) and s != side and i < b - 1:
            pending = s
        cur = eq * (1 + side * (bar["c"] / entry - 1)) if side else eq
        peak = max(peak, cur)
        mdd = max(mdd, 1 - cur / peak)
    if side:
        close_at(bars[b - 1]["c"])
    w = [r for _, r in trades if r > 0]
    l = [-r for _, r in trades if r <= 0]
    yrs = (b - a) / bpy
    cagr = eq ** (1 / yrs) - 1 if eq > 0 else -1
    return {"n": len(trades), "win": len(w) / len(trades) * 100 if trades else 0,
            "pf": sum(w) / sum(l) if l else float("inf"), "total": (eq - 1) * 100, "mdd": mdd * 100,
            "calmar": cagr / mdd if mdd else 0, "hold": (bars[b - 1]["c"] / bars[a]["o"] - 1) * 100}


def table(rows, title):
    out = [f"\n===== {title} =====",
           f"{'규칙':<22}{'거래':>5}{'승률':>6}{'손익비':>6}{'누적':>9}{'MDD':>6}{'수익/낙폭':>8}"]
    for name, r in rows:
        win = f"{r['win']:>5.0f}%" if r["win"] is not None else f"{'-':>6}"
        pf = ("∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}") if r["pf"] is not None else "-"
        out.append(f"{name:<22}{r['n']:>5}{win}{pf:>6}{r['total']:>+8.0f}%{r['mdd']:>5.0f}%{r['calmar']:>8.2f}")
    out.append(f"{'(그냥 보유)':<22}{'':>5}{'':>6}{'':>6}{rows[0][1]['hold']:>+8.0f}%")
    return "\n".join(out)


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": text[:3900], "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)


VARIANTS = [   # (이름, 익절%, 손절%, 롱만)
    ("볼벤v1 원본 2%/1% 롱숏", 2.0, 1.0, False),
    ("볼벤v1 2%/1% 롱만", 2.0, 1.0, True),
    ("볼벤v1 4%/2% 롱숏", 4.0, 2.0, False),
    ("볼벤v1 6%/3% 롱숏", 6.0, 3.0, False),
]


if __name__ == "__main__":
    d = lambda bars, i: datetime.fromtimestamp(bars[i]["t"] / 1000, tz=KST).strftime("%Y-%m-%d")
    report = []
    for interval, yrs in (("4h", YEARS), ("1h", YEARS), ("15m", min(YEARS, 1))):
        bars = klines(SYMBOL, yrs, interval)
        bpy = 365 * {"15m": 96, "1h": 24, "4h": 6}[interval]
        c = [b["c"] for b in bars]
        base = cross_state(ema(c, 20), ema(c, 200))
        sigs = squeeze_signals(bars)
        first = WARMUP
        mid = (first + len(bars)) // 2
        print(f"\n######## {SYMBOL} {interval} {len(bars)}개 · {d(bars, first)} ~ {d(bars, len(bars) - 1)} (앞/뒤 경계 {d(bars, mid)}) · 편도비용 {SIDE_COST * 100:.1f}%")
        print(f"스퀴즈 해제 신호: 롱 {sum(1 for s in sigs[first:] if s > 0)}번 / 숏 {sum(1 for s in sigs[first:] if s < 0)}번")
        for title, a, b in (("전체 기간", first, len(bars)), ("앞 절반", first, mid), ("뒤 절반", mid, len(bars))):
            rows = [("EMA 20/200 (현재 GT1-S)", run(bars, base, a, b, bpy))]
            rows += [(n, run_squeeze(bars, sigs, a, b, bpy, tp, sl, lo)) for n, tp, sl, lo in VARIANTS]
            t = table(rows, f"{interval} {title}")
            print(t)
            if title == "전체 기간":
                report.append(t)
    tg_send(f"<b>🧪 볼벤v1 백테스트 — {SYMBOL}</b>\n<pre>{''.join(report)}</pre>")
