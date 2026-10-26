"""
Golden Trend 개선 실험실 — 데이터 1개(기본 BTCUSDT 4시간봉)로 규칙 변형을 비교

기본 규칙(v1): SMA50이 SMA200 위로 올라간 봉이 마감되면 다음 봉 시가 매수, 아래로 내려가면 다음 봉 시가 매도
비교하는 보완안
  adx   : 추세 강도 필터 — ADX(14)가 기준 이상일 때만 진입 (횡보장 거르기)
  vol   : 거래량 필터 — 진입 봉 거래량이 20봉 평균의 1.5배 이상일 때만
  atr   : 청산 보완 — 보유 중 최고 종가에서 ATR(14)×배수 만큼 빠지면 데드크로스 전이라도 청산
  reent : 청산된 뒤 50선이 여전히 200선 위면 다시 진입 허용 (기본은 새 골든크로스까지 대기)
  exit_fast : 종가가 단기선 아래로 N봉 연속이면 청산 (데드크로스보다 빠른 청산)
  fast/slow : 이동평균 길이 바꿔 보기 (20/100, 30/150)
앞 절반 / 뒤 절반 기간을 따로 보여서, 앞에서 좋은 조합이 뒤에서도 좋은지(과최적화 여부) 확인

실행: python3 lab.py [SYMBOL] [YEARS]   예) python3 lab.py BTCUSDT 3
"""
import os
import sys
import time
import statistics
from datetime import datetime, timezone, timedelta

import requests

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
YEARS = float(sys.argv[2]) if len(sys.argv) > 2 else 3
FAST, SLOW = 50, 200
COST = 0.002                     # 왕복 비용 0.2% (수수료+슬리피지)
KST = timezone(timedelta(hours=9))
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ───────── 데이터 ─────────
def klines(symbol, years):
    need = int(365 * years * 6) + SLOW + 20
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
    return [{"t": r[0], "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[5])} for r in out]


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


def wilder(vals, n):
    """Wilder 평활(RMA). 앞쪽 None은 건너뜀"""
    out, acc, k = [None] * len(vals), None, 0
    for i, v in enumerate(vals):
        if v is None:
            continue
        k += 1
        if acc is None:
            acc = v
        else:
            acc = (acc * (n - 1) + v) / n
        if k >= n:
            out[i] = acc
    return out


def atr_adx(bars, n=14):
    tr, pdm, mdm = [None], [None], [None]
    for i in range(1, len(bars)):
        a, b = bars[i], bars[i - 1]
        tr.append(max(a["h"] - a["l"], abs(a["h"] - b["c"]), abs(a["l"] - b["c"])))
        up, dn = a["h"] - b["h"], b["l"] - a["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
    atr, spdm, smdm = wilder(tr, n), wilder(pdm, n), wilder(mdm, n)
    dx = [None] * len(bars)
    for i in range(len(bars)):
        if atr[i] and atr[i] > 0 and spdm[i] is not None:
            pdi, mdi = 100 * spdm[i] / atr[i], 100 * smdm[i] / atr[i]
            dx[i] = 100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > 0 else 0.0
    return atr, wilder(dx, n)


# ───────── 백테스트 ─────────
def run(bars, ind, start, end, adx_min=0, vol_x=0, atr_mult=0, reentry=False, fast=FAST, slow=SLOW, exit_fast=0):
    """bars[start:end] 구간에서 거래. 거래 목록과 성과를 돌려줌
    exit_fast=N : 종가가 단기선 아래로 N봉 연속 내려가면 청산 (데드크로스보다 빠른 청산)"""
    f, s = ind["sma"][fast], ind["sma"][slow]
    adx, atr, vavg = ind["adx"], ind["atr"], ind["vavg"]
    below = 0
    trades, pos, peak, armed = [], None, None, True     # armed: 새 골든크로스 이후 진입 가능 상태
    equity, eq_peak, mdd = 1.0, 1.0, 0.0
    for i in range(start, end - 1):
        if f[i] is None or s[i] is None or f[i - 1] is None or s[i - 1] is None:
            continue
        up, was_up = f[i] > s[i], f[i - 1] > s[i - 1]
        if up and not was_up:
            armed = True
        if pos is None:
            ok = up and armed
            if ok and adx_min and (adx[i] is None or adx[i] < adx_min):
                ok = False
            if ok and vol_x and (vavg[i] is None or bars[i]["v"] < vol_x * vavg[i]):
                ok = False
            if ok:
                pos, peak = (i + 1, bars[i + 1]["o"]), bars[i + 1]["o"]
                if not reentry:
                    armed = False
        else:
            peak = max(peak, bars[i]["c"])
            below = below + 1 if bars[i]["c"] < f[i] else 0
            stop = (atr_mult and atr[i] and bars[i]["c"] < peak - atr_mult * atr[i]) or (exit_fast and below >= exit_fast)
            if not up or stop:
                px = bars[i + 1]["o"]
                ret = px / pos[1] - 1 - COST
                trades.append({"in": pos[0], "out": i + 1, "ret": ret, "why": "stop" if stop and up else "cross"})
                equity *= 1 + ret
                pos = None
        cur = equity if pos is None else equity * (bars[i]["c"] / pos[1])
        eq_peak = max(eq_peak, cur)
        mdd = max(mdd, 1 - cur / eq_peak)
    if pos is not None:
        ret = bars[end - 1]["c"] / pos[1] - 1 - COST
        trades.append({"in": pos[0], "out": end - 1, "ret": ret, "why": "open"})
        equity *= 1 + ret
    hold = bars[end - 1]["c"] / bars[start]["o"] - 1
    return trades, equity - 1, hold, mdd


def summary(trades, total, hold, mdd):
    if not trades:
        return {"n": 0, "win": 0, "avg": 0, "pf": 0, "total": total, "hold": hold, "mdd": mdd, "stop": 0}
    w = [t["ret"] for t in trades if t["ret"] > 0]
    l = [-t["ret"] for t in trades if t["ret"] <= 0]
    return {"n": len(trades), "win": len(w) / len(trades) * 100, "avg": statistics.mean(t["ret"] for t in trades) * 100,
            "pf": (sum(w) / sum(l)) if l else float("inf"), "total": total * 100, "hold": hold * 100, "mdd": mdd * 100,
            "stop": sum(1 for t in trades if t["why"] == "stop")}


VARIANTS = [
    # 1차: ADX·거래량 필터는 효과 없음. 2차: 빠른 청산·추적손절은 수익을 깎음. 남은 건 "이동평균 길이" 하나
    ("10/50",    {"fast": 10, "slow": 50}),
    ("15/75",    {"fast": 15, "slow": 75}),
    ("20/100",   {"fast": 20, "slow": 100}),
    ("25/125",   {"fast": 25, "slow": 125}),
    ("30/150",   {"fast": 30, "slow": 150}),
    ("40/200",   {"fast": 40, "slow": 200}),
    ("50/200 v1", {}),
    ("20/200",   {"fast": 20, "slow": 200}),
    ("20/150",   {"fast": 20, "slow": 150}),
    ("30/100",   {"fast": 30, "slow": 100}),
]


def table(rows, title):
    out = [f"\n===== {title} =====",
           f"{'규칙':<24}{'거래':>5}{'승률':>7}{'평균':>8}{'손익비':>7}{'누적':>9}{'MDD':>7}{'손절':>5}"]
    for name, r in rows:
        pf = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        out.append(f"{name:<24}{r['n']:>5}{r['win']:>6.0f}%{r['avg']:>+7.1f}%{pf:>7}{r['total']:>+8.0f}%{r['mdd']:>6.0f}%{r['stop']:>5}")
    out.append(f"{'(그냥 보유)':<24}{'':>5}{'':>7}{'':>8}{'':>7}{rows[0][1]['hold']:>+8.0f}%")
    return "\n".join(out)


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT:
        return
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": text[:3900], "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=20)


if __name__ == "__main__":
    bars = klines(SYMBOL, YEARS)
    closes = [b["c"] for b in bars]
    atr, adx = atr_adx(bars)
    lens = {n for _, kw in VARIANTS for n in (kw.get("fast", FAST), kw.get("slow", SLOW))}
    ind = {"sma": {n: sma(closes, n) for n in lens}, "atr": atr, "adx": adx, "vavg": sma([b["v"] for b in bars], 20)}
    first = SLOW + 1
    mid = (first + len(bars)) // 2
    d = lambda i: datetime.fromtimestamp(bars[i]["t"] / 1000, tz=KST).strftime("%Y-%m-%d")
    print(f"{SYMBOL} 4시간봉 {len(bars)}개 · 거래 구간 {d(first)} ~ {d(len(bars) - 1)} · 왕복비용 {COST * 100:.1f}%")
    print(f"앞 절반 {d(first)}~{d(mid)} / 뒤 절반 {d(mid)}~{d(len(bars) - 1)}")
    sections = []
    for title, a, b in (("전체 기간", first, len(bars)), ("앞 절반", first, mid), ("뒤 절반", mid, len(bars))):
        rows = [(name, summary(*run(bars, ind, a, b, **kw))) for name, kw in VARIANTS]
        sections.append(table(rows, title))
    print("\n".join(sections))
    # 텔레그램엔 전체 기간 표만
    tg_send(f"<b>🧪 Golden Trend 실험실 — {SYMBOL} 4h {YEARS:g}년</b>\n<pre>{sections[0]}</pre>")
