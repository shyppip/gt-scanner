"""
후보 원장 자동 채점 — 일일 미증시 보고 세션에서 실행

사용:
  python3 eval.py --ledger ledger.csv --prices prices.csv [--snapdir snapshots] [--summary s1.csv s2.csv ...] --out ledger_new.csv --report eval_report.md

- 각 후보의 기준가(baseline_price, 없으면 기준일 종가) 대비 D+1/D+5/D+20 거래일 종가 수익률, 같은 기간 SPY 수익률, 초과수익(%p)을 채운다.
- 기준일 = baseline_timestamp_et의 날짜(없으면 selection_date_kst 전날) 이하의 마지막 거래일.
- 가격은 prices.csv(일봉) → snapshots/YYYY-MM-DD.csv(전 종목 종가) 순으로 찾는다. 둘 다 없으면 status=no_data.
- theme_group(요약 CSV에서 ticker+날짜로 연결, 없으면 원장의 theme)별 적중률(초과수익>0 비율)·평균·중앙값·표본 수를 보고서로 낸다.
"""
import argparse
import csv
import glob
import os
import re
import statistics as st
from datetime import datetime, timedelta

H = (1, 5, 20)


def num(x):
    try:
        v = float(str(x).replace(",", "").replace("$", "").strip())
        return v if v > 0 else None
    except Exception:
        return None


def ymd(s):
    m = re.search(r"(20\d\d)[-/.](\d\d)[-/.](\d\d)", str(s or ""))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def load_prices(path, snapdir):
    px = {}
    if path and os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            v = num(r["close"])
            if v:
                px.setdefault(r["ticker"].upper(), {})[r["date"]] = v
    if snapdir and os.path.isdir(snapdir):
        for fn in sorted(glob.glob(os.path.join(snapdir, "*.csv"))):
            d = os.path.basename(fn)[:10]
            for r in csv.DictReader(open(fn, encoding="utf-8")):
                v = num(r.get("close"))
                t = (r.get("ticker") or "").upper()
                if v and t:
                    px.setdefault(t, {}).setdefault(d, v)
    return px


def load_groups(paths):
    g = {}
    for p in paths or []:
        try:
            rows = list(csv.DictReader(open(p, encoding="utf-8-sig")))
        except Exception:
            continue
        if not rows:
            continue
        cols = rows[0].keys()
        tc = next((c for c in cols if c.lower() in ("ticker", "symbol", "티커")), None)
        gc = next((c for c in cols if "theme_group" in c.lower()), None)
        dc = next((c for c in cols if "date" in c.lower()), None)
        if not (tc and gc):
            continue
        fd = ymd(os.path.basename(p).replace("_", "-")[-14:]) or ymd(re.sub(r"(\d{4})(\d\d)(\d\d)", r"\1-\2-\3", os.path.basename(p)))
        for r in rows:
            d = ymd(r.get(dc)) if dc else None
            for t in re.split(r"[ ,/;|]+", r[tc] or ""):
                t = t.strip().upper()
                if t and r[gc]:
                    g[(d or fd, t)] = r[gc].strip()
                    g.setdefault((None, t), r[gc].strip())
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--prices", default="prices.csv")
    ap.add_argument("--snapdir", default="snapshots")
    ap.add_argument("--summary", nargs="*")
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", default="eval_report.md")
    a = ap.parse_args()

    px = load_prices(a.prices, a.snapdir)
    if "SPY" not in px:
        raise SystemExit("SPY 가격이 없음 — prices.csv 확인")
    days = sorted(px["SPY"])
    groups = load_groups(a.summary)
    rows = list(csv.DictReader(open(a.ledger, encoding="utf-8-sig")))
    cols = list(rows[0].keys()) if rows else []
    for c in ["theme_group"] + [f"evaluation_{n}d_{k}" for n in H for k in ("date", "price", "return_pct", "spy_return_pct", "excess_return_pct", "status")]:
        if c not in cols:
            cols.append(c)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    stats = {}

    for r in rows:
        t = (r.get("ticker") or "").strip().upper()
        sel = ymd(r.get("selection_date_kst"))
        bd = ymd(r.get("baseline_timestamp_et"))
        if not bd and sel:
            bd = (datetime.strptime(sel, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        prior = [d for d in days if bd and d <= bd]
        g = r.get("theme_group") or groups.get((sel, t)) or groups.get((None, t)) or r.get("theme") or "미분류"
        r["theme_group"] = g
        if not prior:
            for n in H:
                r[f"evaluation_{n}d_status"] = "no_baseline_date"
            continue
        i0 = days.index(prior[-1])
        tp = px.get(t, {})
        base = num(r.get("baseline_price")) or tp.get(days[i0])
        sbase = num(r.get("spy_baseline_price")) or px["SPY"].get(days[i0])
        if base and not num(r.get("baseline_price")):
            r["baseline_price"] = round(base, 4)
            r["baseline_price_type"] = r.get("baseline_price_type") or f"close {days[i0]} (eval.py 보충)"
        if sbase and not num(r.get("spy_baseline_price")):
            r["spy_baseline_price"] = round(sbase, 4)
        for n in H:
            k = f"evaluation_{n}d_"
            if i0 + n >= len(days):
                r[k + "status"] = f"pending (D+{n} 미도래)"
                continue
            d = days[i0 + n]
            p, sp = tp.get(d), px["SPY"].get(d)
            if not (p and base and sp and sbase):
                r[k + "status"] = "no_data"
                r[k + "date"] = d
                continue
            rr, sr = (p / base - 1) * 100, (sp / sbase - 1) * 100
            r.update({k + "date": d, k + "price": round(p, 4), k + "return_pct": round(rr, 2), k + "spy_return_pct": round(sr, 2),
                      k + "excess_return_pct": round(rr - sr, 2), k + "status": "complete"})
            stats.setdefault((g, n), []).append(rr - sr)
            stats.setdefault(("전체", n), []).append(rr - sr)
        r["last_updated_kst"] = now

    with open(a.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    cnt = {}
    for r in rows:
        for n in H:
            s = (r.get(f"evaluation_{n}d_status") or "").split(" ")[0]
            cnt[(n, s)] = cnt.get((n, s), 0) + 1
    L = [f"# 후보 자동 채점 ({now}, 가격 기준 마지막 거래일 {days[-1]})", "",
         "상태: " + " · ".join(f"D+{n} " + ", ".join(f"{s} {c}" for (m, s), c in sorted(cnt.items()) if m == n) for n in H), "",
         "| 테마 그룹 | 기간 | 표본 | 적중률(SPY 초과>0) | 평균 초과(%p) | 중앙값(%p) |", "|---|---|---|---|---|---|"]
    order = sorted({g for g, _ in stats}, key=lambda g: (g != "전체", g))
    for g in order:
        for n in H:
            v = stats.get((g, n))
            if v:
                hit = sum(1 for x in v if x > 0) / len(v) * 100
                L.append(f"| {g} | D+{n} | {len(v)} | {hit:.0f}% | {st.mean(v):+.2f} | {st.median(v):+.2f} |")
    L += ["", "표본 5개 미만 그룹은 우연일 가능성이 큼 — 판단 근거로 쓰지 말 것."]
    open(a.report, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
