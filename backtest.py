"""
Golden Trend 알림봇 규칙 백테스트 (업비트 원화마켓 + 바이낸스 USDT 마켓, 4시간봉)

규칙: 50봉 SMA가 200봉 SMA 위로 올라간 봉 종가에 매수 → 아래로 내려간 봉 종가에 매도 (알림봇과 동일)
- 수수료: 업비트 0.05%, 바이낸스 0.1% (매수·매도 각각) + 슬리피지 0.05%
- 기간: 최근 GT_BT_DAYS일 (기본 365일)
- 마지막까지 보유 중인 포지션은 마지막 종가에 청산한 것으로 계산

출력
- 콘솔: 거래소별 요약 + 코인별 상위/하위
- 텔레그램: 요약 메시지 + 코인별 결과 CSV 파일
"""
import os
import sys
import csv
import math
import time
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

DAYS = int(os.environ.get("GT_BT_DAYS", "365"))
FAST, SLOW = 50, 200
BARS = DAYS * 6 + SLOW + 5          # 4시간봉 = 하루 6개 + SMA 준비 구간
FEE = {"upbit": 0.0005, "binance": 0.001}
SLIP = 0.0005
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "golden-trend-backtest/1.0"}
STABLES = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "AEUR", "EURI", "USDE", "XUSD", "BFUSD", "PAXG", "USD1", "RLUSD"}


def log(*a):
    print(datetime.now(KST).strftime("%H:%M:%S"), *a, flush=True)


class RateLimit:
    def __init__(self, per_sec):
        self.gap, self.lock, self.next = 1.0 / per_sec, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        if t > now:
            time.sleep(t - now)


LIMITS = {"upbit": RateLimit(7), "binance": RateLimit(18)}


def get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=15)
            if r.status_code == 429:
                time.sleep(1.5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


# ───────── 데이터 ─────────
def upbit_markets():
    data = get("https://api.upbit.com/v1/market/all", {"isDetails": "true"})
    return [{"code": m["market"], "name": m["korean_name"]} for m in data if m["market"].startswith("KRW-")]


def upbit_candles(market, need):
    """마감된 4시간봉 종가 (오래된 → 최신). 200개씩 뒤로 거슬러 받음"""
    out, to = [], None
    while len(out) < need:
        LIMITS["upbit"].wait()
        params = {"market": market, "count": 200}
        if to:
            params["to"] = to
        data = get("https://api.upbit.com/v1/candles/minutes/240", params)
        if not data:
            break
        out.extend(data)
        to = data[-1]["candle_date_time_utc"]
        if len(data) < 200:
            break
    out = out[::-1]
    # 마지막은 진행 중 봉이므로 제외
    rows = out[:-1][-need:]
    return [(datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=timezone.utc), float(r["trade_price"])) for r in rows]


def binance_markets():
    info = get("https://data-api.binance.vision/api/v3/exchangeInfo", {"permissions": "SPOT"})
    out = []
    for s in info["symbols"]:
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
            continue
        base = s["baseAsset"]
        if base in STABLES or (base.endswith(("UP", "DOWN", "BULL", "BEAR")) and len(base) > 4):
            continue
        out.append({"code": s["symbol"], "name": base})
    return out


def binance_candles(symbol, need):
    out, end = [], None
    while len(out) < need + 1:
        LIMITS["binance"].wait()
        params = {"symbol": symbol, "interval": "4h", "limit": 1000}
        if end:
            params["endTime"] = end
        data = get("https://data-api.binance.vision/api/v3/klines", params)
        if not data:
            break
        out = data + out
        end = data[0][0] - 1
        if len(data) < 1000:
            break
    rows = out[:-1][-need:]
    return [(datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc), float(r[4])) for r in rows]


# ───────── 백테스트 ─────────
def sma_series(vals, n):
    out, s = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def backtest(candles, fee):
    closes = [c for _, c in candles]
    f, s = sma_series(closes, FAST), sma_series(closes, SLOW)
    trades, pos = [], None
    for i in range(SLOW, len(closes)):
        if f[i - 1] is None or s[i - 1] is None:
            continue
        up_now, up_prev = f[i] > s[i], f[i - 1] > s[i - 1]
        if pos is None and up_now and not up_prev:
            pos = (candles[i][0], closes[i] * (1 + fee + SLIP))
        elif pos is not None and (not up_now) and up_prev:
            exit_px = closes[i] * (1 - fee - SLIP)
            trades.append({"entry": pos[0], "exit": candles[i][0], "ret": exit_px / pos[1] - 1, "open": False})
            pos = None
    if pos is not None:
        exit_px = closes[-1] * (1 - fee - SLIP)
        trades.append({"entry": pos[0], "exit": candles[-1][0], "ret": exit_px / pos[1] - 1, "open": True})
    # 전략 누적 수익(복리) + 최대 낙폭 (거래 단위)
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in trades:
        eq *= 1 + t["ret"]
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    start_i = SLOW  # 보유 비교 구간은 신호 가능 구간과 맞춤
    hold = closes[-1] / closes[start_i] - 1 if len(closes) > start_i else None
    return {"trades": trades, "strategy": eq - 1, "mdd": mdd, "hold": hold, "bars": len(closes)}


def run_exchange(ex):
    markets = upbit_markets() if ex == "upbit" else binance_markets()
    fetch = upbit_candles if ex == "upbit" else binance_candles
    log(f"{ex}: {len(markets)}개 코인, 최근 {DAYS}일 4시간봉 수집")
    results, errors = [], 0

    def one(m):
        try:
            c = fetch(m["code"], BARS)
            if len(c) < SLOW + 30:
                return m, None, "short"
            return m, backtest(c, FEE[ex]), None
        except Exception as e:
            return m, None, str(e)[:80]

    with ThreadPoolExecutor(max_workers=4 if ex == "upbit" else 8) as pool:
        for m, bt, err in pool.map(one, markets):
            if bt is None:
                errors += 1
                continue
            results.append({**m, "ex": ex, **bt})
    log(f"{ex}: 분석 {len(results)}개, 제외 {errors}개")
    return results


def summarize(results, ex):
    trades = [t for r in results for t in r["trades"]]
    closed = [t for t in trades if not t["open"]]
    rets = [t["ret"] for t in closed]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    strat = [r["strategy"] for r in results]
    hold = [r["hold"] for r in results if r["hold"] is not None]
    beat = sum(1 for r in results if r["hold"] is not None and r["strategy"] > r["hold"])
    hold_days = [((t["exit"] - t["entry"]).total_seconds() / 86400) for t in closed]
    return {
        "ex": ex,
        "coins": len(results),
        "trades": len(closed),
        "open": len(trades) - len(closed),
        "win_rate": len(wins) / len(rets) * 100 if rets else 0,
        "avg_ret": statistics.mean(rets) * 100 if rets else 0,
        "median_ret": statistics.median(rets) * 100 if rets else 0,
        "avg_win": statistics.mean(wins) * 100 if wins else 0,
        "avg_loss": statistics.mean(losses) * 100 if losses else 0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "avg_hold_days": statistics.mean(hold_days) if hold_days else 0,
        "strategy_avg": statistics.mean(strat) * 100 if strat else 0,
        "strategy_median": statistics.median(strat) * 100 if strat else 0,
        "hold_avg": statistics.mean(hold) * 100 if hold else 0,
        "hold_median": statistics.median(hold) * 100 if hold else 0,
        "beat_hold_pct": beat / len(results) * 100 if results else 0,
        "mdd_avg": statistics.mean(r["mdd"] for r in results) * 100 if results else 0,
        "best": sorted(results, key=lambda r: -r["strategy"])[:5],
        "worst": sorted(results, key=lambda r: r["strategy"])[:5],
    }


def pct(x):
    return f"{x:+.1f}%"


def tg(text, parse="HTML"):
    if not TG_TOKEN:
        return
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": text, "parse_mode": parse, "disable_web_page_preview": True}, timeout=20)


def tg_file(path, caption):
    if not TG_TOKEN:
        return
    with open(path, "rb") as f:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                      data={"chat_id": TG_CHAT, "caption": caption}, files={"document": f}, timeout=60)


def main():
    exs = [e for e in os.environ.get("GT_EXCHANGES", "upbit,binance").split(",") if e]
    allres, sums = [], []
    for ex in exs:
        res = run_exchange(ex)
        allres += res
        sums.append(summarize(res, ex))

    label = {"upbit": "업비트", "binance": "바이낸스"}
    lines = [f"📊 <b>Golden Trend 백테스트</b> (4시간봉 · 최근 {DAYS}일 · 수수료 포함)"]
    for s in sums:
        lines += ["",
                  f"<b>{label[s['ex']]}</b> {s['coins']}개 코인 · 거래 {s['trades']}회 (보유 중 {s['open']})",
                  f"승률 {s['win_rate']:.0f}% · 거래당 평균 {pct(s['avg_ret'])} (중앙값 {pct(s['median_ret'])})",
                  f"이긴 거래 평균 {pct(s['avg_win'])} · 진 거래 평균 {pct(s['avg_loss'])} · 손익비 {s['profit_factor']:.2f}",
                  f"평균 보유 {s['avg_hold_days']:.1f}일 · 코인당 최대낙폭 평균 {pct(s['mdd_avg'])}",
                  f"코인당 전략 수익 평균 {pct(s['strategy_avg'])} (중앙값 {pct(s['strategy_median'])})",
                  f"같은 기간 그냥 보유 평균 {pct(s['hold_avg'])} (중앙값 {pct(s['hold_median'])})",
                  f"전략이 보유를 이긴 코인 {s['beat_hold_pct']:.0f}%",
                  "상위: " + ", ".join(f"{r['code'].replace('KRW-', '').replace('USDT', '')} {pct(r['strategy'] * 100)}" for r in s["best"]),
                  "하위: " + ", ".join(f"{r['code'].replace('KRW-', '').replace('USDT', '')} {pct(r['strategy'] * 100)}" for r in s["worst"])]
    lines += ["", "<i>과거 결과이며 미래 수익을 보장하지 않아요. 투자 권유가 아닙니다.</i>"]
    text = "\n".join(lines)
    print("\n" + text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))

    # CSV
    path = f"backtest_{DAYS}d.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["거래소", "코드", "이름", "거래수", "승률%", "전략수익%", "보유수익%", "최대낙폭%", "보유중"])
        for r in sorted(allres, key=lambda r: -r["strategy"]):
            cl = [t for t in r["trades"] if not t["open"]]
            wr = sum(1 for t in cl if t["ret"] > 0) / len(cl) * 100 if cl else ""
            w.writerow([label[r["ex"]], r["code"], r["name"], len(cl), f"{wr:.0f}" if cl else "",
                        f"{r['strategy'] * 100:.1f}", f"{r['hold'] * 100:.1f}" if r["hold"] is not None else "",
                        f"{r['mdd'] * 100:.1f}", "O" if any(t["open"] for t in r["trades"]) else ""])
    # 요약 JSON (로그용)
    print("SUMMARY_JSON " + str({k: (v if not isinstance(v, list) else [r["code"] for r in v]) for s in sums for k, v in s.items()}))
    tg(text)
    tg_file(path, f"코인별 백테스트 결과 ({DAYS}일)")


if __name__ == "__main__":
    main()
