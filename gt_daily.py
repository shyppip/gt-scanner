"""
Golden Trend 전략 일일 점검 (트레이딩뷰 전략 테스터를 대신하는 계산)

규칙: GT1-S와 같음
- 50봉·200봉 단순이동평균, 확정된 봉에서 골든크로스 → 다음 봉 시가에 매수
- 데드크로스 → 다음 봉 시가에 청산, 손절·익절 없음, 롱만
- 수수료 0.05%(진입·청산 각각), 슬리피지 2틱
데이터: 바이낸스 현물 4시간봉 최근 약 3,000봉(약 500일)
"""
import time
from datetime import datetime, timezone, timedelta

import scanner as S

SYMBOLS = [("SOLUSDT", "솔라나", 0.01), ("BTCUSDT", "비트코인", 0.01), ("ETHUSDT", "이더리움", 0.01)]
FEE = 0.0005
SLIP_TICKS = 2
BARS = 3000
KST = timezone(timedelta(hours=9))


def klines(symbol, interval="4h", total=BARS):
    rows, end = [], None
    while len(rows) < total:
        p = {"symbol": symbol, "interval": interval, "limit": 1000}
        if end:
            p["endTime"] = end
        batch = S.bn_get("/api/v3/klines", p)
        if not batch:
            break
        rows = batch + rows
        end = int(batch[0][0]) - 1
        if len(batch) < 1000:
            break
    now_ms = time.time() * 1000
    rows = [r for r in rows if int(r[6]) < now_ms]          # 마감된 봉만
    seen, out = set(), []
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        out.append((int(r[0]), float(r[1]), float(r[4])))   # 시작시각, 시가, 종가
    return out[-total:]


def backtest(bars, tick):
    closes = [c for _, _, c in bars]
    opens = [o for _, o, _ in bars]
    n = len(bars)
    pos, entry, equity, peak, mdd = False, 0.0, 1.0, 1.0, 0.0
    trades, signals = [], []
    start = S.SLOW + 1
    for i in range(start, n):
        f0, s0 = S.sma(closes, S.FAST, i), S.sma(closes, S.SLOW, i)            # i-1번째 봉까지
        f1, s1 = S.sma(closes, S.FAST, i + 1), S.sma(closes, S.SLOW, i + 1)    # i번째 봉까지
        up = f0 <= s0 and f1 > s1
        down = f0 >= s0 and f1 < s1
        if (up or down):
            signals.append((bars[i][0], "매수" if up else "청산", closes[i]))
        if i + 1 >= n:
            continue
        nxt = opens[i + 1]
        if up and not pos:
            entry = nxt + SLIP_TICKS * tick
            pos = True
        elif down and pos:
            exitp = nxt - SLIP_TICKS * tick
            r = (exitp * (1 - FEE)) / (entry * (1 + FEE)) - 1
            trades.append(r)
            equity *= 1 + r
            pos = False
        # 평가손익 포함 낙폭
        cur = equity * ((closes[i] * (1 - FEE)) / (entry * (1 + FEE)) if pos else 1)
        peak = max(peak, cur)
        mdd = max(mdd, 1 - cur / peak)
    open_r = None
    if pos:
        open_r = (closes[-1] * (1 - FEE)) / (entry * (1 + FEE)) - 1
    wins = [t for t in trades if t > 0]
    loss = [t for t in trades if t <= 0]
    gross_w = sum(wins)
    gross_l = -sum(loss)
    final = equity * (1 + open_r if open_r is not None else 1)
    return {
        "net": final - 1,
        "trades": len(trades),
        "win": len(wins) / len(trades) if trades else None,
        "pf": (gross_w / gross_l) if gross_l > 0 else None,
        "mdd": mdd,
        "avg": sum(trades) / len(trades) if trades else None,
        "pos": pos,
        "open_r": open_r,
        "entry": entry if pos else None,
        "signals": signals[-3:],
        "bh": closes[-1] / closes[start] - 1,
        "from": bars[start][0],
        "to": bars[-1][0],
        "last": closes[-1],
        "gap": (S.sma(closes, S.FAST, n) / S.sma(closes, S.SLOW, n) - 1) * 100,
    }


def pct(x, d=1):
    return "-" if x is None else f"{x * 100:+.{d}f}%"


def report():
    lines = ["📒 <b>Golden Trend 전략 일일 점검</b> (4시간봉, 바이낸스 기준)"]
    for sym, name, tick in SYMBOLS:
        try:
            r = backtest(klines(sym), tick)
        except Exception as e:
            lines.append(f"• {name}: 데이터 오류 ({str(e)[:80]})")
            continue
        d0 = datetime.fromtimestamp(r["from"] / 1000, KST)
        state = (f"🟢 보유 중 (진입 ${r['entry']:,.2f}, 평가 {pct(r['open_r'])})" if r["pos"]
                 else f"⚪ 현금 대기 (50/200 간격 {r['gap']:+.2f}%)")
        lines.append("")
        lines.append(f"<b>{name}</b> {sym} · 현재 ${r['last']:,.2f}")
        lines.append(f"   상태: {state}")
        lines.append(f"   순수익 {pct(r['net'])} (그냥 보유 {pct(r['bh'])}) · 거래 {r['trades']}회 · 승률 {pct(r['win'], 0).lstrip('+')}")
        pf = "-" if r["pf"] is None else f"{r['pf']:.2f}"
        lines.append(f"   손익비 {pf} · 최대 낙폭 -{r['mdd'] * 100:.1f}% · 평균 거래 {pct(r['avg'])}")
        if r["signals"]:
            sig = " / ".join(f"{datetime.fromtimestamp(t / 1000, KST):%m/%d %H시} {k}" for t, k, _ in r["signals"])
            lines.append(f"   최근 신호: {sig}")
        lines.append(f"   <i>기간 {d0:%Y-%m-%d}~ · 수수료 0.05% · 슬리피지 2틱</i>")
    lines.append("")
    lines.append("<i>트레이딩뷰 전략 테스터 대신 같은 규칙으로 직접 계산한 값이라 기간·데이터 차이로 조금 다를 수 있어요. 과거 성과는 미래 수익을 보장하지 않아요.</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    S.tg_send(report())
