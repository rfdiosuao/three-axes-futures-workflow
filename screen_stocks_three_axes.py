#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
股票版「三板斧」扫描：月/周/日 MA10 三周期共振选环境 + 1.5*ATR14 止损 + 以损定量。
- 股票池默认中证500（可 --index 切换：000300 沪深300 / 000905 中证500 / 000852 中证1000）。
- 行情走 AkShare 新浪链路（前复权日线）；东财域名在本环境被代理拦截，故不依赖快照/行业接口。
- 共振、ATR、手数口径直接复用 repo_three_axes/report_core.py，与期货三板斧完全一致。
- 仅做客观、可复现的技术形态筛选，不预测涨跌、不构成投资建议、不自动交易。
"""
import argparse, json, os, sys, time
from datetime import datetime
import pandas as pd
import akshare as ak

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "repo_three_axes"))
from report_core import signal, atr14, number  # noqa: E402

CN = {"date": "日期", "open": "开盘价", "high": "最高价", "low": "最低价", "close": "收盘价"}


def sina_symbol(code: str) -> str:
    return ("sh" if code.startswith(("6", "688")) else "sz") + code


def fetch_hist(code: str, start: str, end: str, retries: int = 2):
    last = None
    for _ in range(retries + 1):
        try:
            df = ak.stock_zh_a_daily(symbol=sina_symbol(code), start_date=start,
                                     end_date=end, adjust="qfq")
            if df is not None and not df.empty:
                return df
        except Exception as e:  # noqa
            last = e
            time.sleep(0.4)
    raise RuntimeError(f"取数失败 {code}: {last}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="000905")
    ap.add_argument("--equity", type=float, default=100000.0, help="假设总资金，用于以损定量演示")
    ap.add_argument("--amount-min", type=float, default=50_000_000.0, help="近20日日均成交额下限(元)")
    ap.add_argument("--months", type=int, default=18)
    ap.add_argument("--limit", type=int, default=0, help=">0 时只取前N只(调试用)")
    ap.add_argument("--workers", type=int, default=8, help="并发取数线程数")
    args = ap.parse_args()

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - pd.DateOffset(months=args.months)).strftime("%Y%m%d")

    # 当期完整成分以中证指数公司官方接口为准；index_stock_cons 返回的是历次纳入记录(不全)
    try:
        rawc = ak.index_stock_cons_csindex(symbol=args.index)
        cons = rawc.rename(columns={"成分券代码": "code", "成分券名称": "name"})[["code", "name"]].copy()
        cons_src = "csindex(官方当期成分)"
    except Exception as e:  # noqa
        rawc = ak.index_stock_cons(symbol=args.index)
        cons = rawc.rename(columns={"品种代码": "code", "品种名称": "name"})[["code", "name"]].copy()
        cons_src = "index_stock_cons(历次纳入,可能不全)"
    cons["code"] = cons["code"].astype(str).str.extract(r"(\d{6})", expand=False)
    cons = cons[~cons["name"].str.contains("ST|退", case=False, na=False)]
    cons = cons.drop_duplicates(subset="code").dropna(subset="code"]).reset_index(drop=True)
    print(f"成分来源 {cons_src}：{len(cons)} 只", file=sys.stderr)
    if args.limit > 0:
        cons = cons.head(args.limit).reset_index(drop=True)

    def process_one(r):
        code, name = r["code"], r["name"]
        try:
            raw = fetch_hist(code, start, end)
            d = raw.rename(columns=CN)[["日期", "开盘价", "最高价", "最低价", "收盘价"]].copy()
            d["日期"] = pd.to_datetime(d["日期"])
            d = d.sort_values("日期").reset_index(drop=True)
            if len(d) < 15:
                return None, dict(code=code, name=name, reason="日线不足15根")
            sig = signal(d)
            side = sig["side"]
            close = float(d["收盘价"].iloc[-1])
            trade_date = d["日期"].iloc[-1].strftime("%Y-%m-%d")
            atr = atr14(d)
            sd = 1.5 * atr if atr else None
            amt20 = float(raw["amount"].tail(20).mean()) if "amount" in raw else None
            liquid = bool(amt20 and amt20 >= args.amount_min)
            stop_px = None
            stop_pct = None
            shares_eq = shares_1m = 0
            if sd and side in ("long", "short"):
                stop_px = close - sd if side == "long" else close + sd
                stop_pct = sd / close * 100

                def lot(equity):  # 1%风险与单票10%仓位取小，向下取整到100股
                    return int(min(equity * 0.01 / sd, equity * 0.10 / close) // 100 * 100)

                shares_eq = lot(args.equity)
                shares_1m = lot(1_000_000)
            row = dict(code=code, name=name, side=side, date=trade_date, close=round(close, 3),
                       atr=round(atr, 3) if atr else None,
                       stop_distance=round(sd, 3) if sd else None,
                       stop_px=round(stop_px, 3) if stop_px else None,
                       stop_pct=round(stop_pct, 2) if stop_pct else None,
                       amount20_yi=round(amt20 / 1e8, 2) if amt20 else None,
                       liquid=liquid,
                       shares_per_100k=shares_eq if side == "long" and liquid else 0,
                       shares_per_1m=shares_1m if side == "long" and liquid else 0)
            return row, None
        except Exception as e:  # noqa
            return None, dict(code=code, name=name, reason=str(e)[:120])

    from concurrent.futures import ThreadPoolExecutor, as_completed
    rows, issues, done, total = [], [], 0, len(cons)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(process_one, r) for _, r in cons.iterrows()]
        for f in as_completed(futures):
            row, issue = f.result()
            done += 1
            if row:
                rows.append(row)
            if issue:
                issues.append(issue)
            if done % 50 == 0 or done == total:
                print(f"  进度 {done}/{total}  多头累计 {sum(x['side']=='long' for x in rows)}",
                      file=sys.stderr)

    df = pd.DataFrame(rows)
    if df.empty:
        print(json.dumps({"error": "无有效结果", "issues": issues[:10]}, ensure_ascii=False))
        return
    trade_date = df["date"].mode().iloc[0]
    os.makedirs("snapshots", exist_ok=True)
    csv = f"snapshots/stock_regime_{trade_date}.csv"
    df.to_csv(csv, index=False, encoding="utf-8-sig")

    longs = df[(df.side == "long") & (df.liquid)].sort_values("amount20_yi", ascending=False)
    shorts = df[(df.side == "short") & (df.liquid)]
    meta = dict(index=args.index, date=trade_date, universe=total, scanned=len(df),
                long=int((df.side == 'long').sum()), long_liquid=len(longs),
                short=int((df.side == 'short').sum()), short_liquid=len(shorts),
                flat=int((df.side == 'flat').sum()), unknown=int((df.side == 'unknown').sum()),
                illiquid=int((~df.liquid).sum()), fail=len(issues),
                equity_demo=args.equity, amount_min=args.amount_min, csv=csv,
                longs=longs.head(40).to_dict("records"),
                issues_sample=issues[:10])
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
