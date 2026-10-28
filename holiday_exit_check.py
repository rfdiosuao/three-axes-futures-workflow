#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
长假持仓离场检查（免费数据：AkShare 交易日历/单合约日K + PandaAI CLI）。

设计：每个交易日收盘后由定时任务调用一次，脚本先用交易日历判断“今天是否长假前
最后一个交易日”（下一交易日与今天相隔 >= GAP_DAYS 个自然日；普通周末仅 2 天，不触发），
只有长假前最后一个交易日才输出报告，其余时间打印 LEVEL=SKIP 静默退出——从而自动覆盖
春节/国庆/五一/清明/端午/中秋等所有节假日，无需每年维护日期。

报告对账户【全部持仓】做过节压力测试并给绿/黄/红灯号：
  - 最坏情景一：节后开盘直接封不利方向涨跌停（用柜台涨跌停价）；
  - 最坏情景二：节后反向跳空 3×ATR；
  - 保证金情景：假设保证金统一上调 MARGIN_BUMP（以交易所通知为准），测算可用资金；
  - 趋势状态：日线 MA10 是否破位、吊灯跟踪止盈线（近 CHAN_N 日极值 ∓ CHAN_K×ATR）是否触发；
  - 浮亏分级沿用 monitor_positions（相对开仓价，达风险预算 50/80/100%）。
灯号仅为客观风险提示，不是买卖指令；是否减仓/离场一律由用户手动决定，脚本全程只读、不下单。

输出：第一行 LEVEL=<HOLIDAY|SKIP|NOHOLD|ERROR>，其后为 Markdown（HOLIDAY/NOHOLD/ERROR 需推送）。
用法：
  python3 holiday_exit_check.py                 # 以今天为基准日，仅长假前最后交易日出报告
  python3 holiday_exit_check.py --date 2026-09-30  # 指定基准日（验证用）
  python3 holiday_exit_check.py --force         # 强制出报告（不管是否节前，验证用）
"""
import sys, os, json, datetime
import akshare as ak
import pandas as pd
from monitor_positions import (panda, pick, base_code, fnum, assess_position,
                               RISK_PCT, FALLBACK_EQUITY)

# 工作目录 = 脚本所在目录（clone 后即可运行）
BASE = os.path.dirname(os.path.abspath(__file__))
REP_DIR = os.path.join(BASE, "reports")
os.makedirs(REP_DIR, exist_ok=True)

GAP_DAYS = 3          # 基准日收盘后到下一交易日之间的休市自然日 >=3 才认定为长假（普通周末=2，不触发）
STOP_K = 1.5          # 初始止损 ATR 倍数
CHAN_K = 3.0          # 吊灯跟踪止盈 ATR 倍数
CHAN_N = 22           # 吊灯极值回看根数
MARGIN_BUMP = 0.20    # 节前保证金上调情景（假设 +20%，以交易所通知为准）
RED_WORST_EQ = 0.03   # 单品种封涨跌停亏损 >=权益3% → 红
RED_ATR3_EQ = 0.02    # 单品种跳 3ATR 亏损 >=权益2% → 红
YEL_WORST_EQ = 0.015
YEL_ATR3_EQ = 0.010
WD = ["周一","周二","周三","周四","周五","周六","周日"]


def holiday_gap(base):
    """返回 (last_trade_day, next_trade_day, gap_days)；日历取不到返回 None。
    gap_days = base 收盘后到下一交易日之间的【休市自然日数】（不含基准日、不含下一交易日）：
    普通周五→周一为 2 天（周六、周日），不触发；春节/国庆等长假为 >=3 天才触发。
    注意不能用 (next_td-base).days，那个日期差在普通周末恰好=3，会误判成长假。"""
    cal = ak.tool_trade_date_hist_sina()
    days = sorted(set(pd.to_datetime(cal["trade_date"]).dt.date))
    past = [d for d in days if d <= base]
    future = [d for d in days if d > base]
    if not past or not future:
        return None
    last_td, next_td = past[-1], future[0]
    # next_td 是 base 之后首个交易日，中间夹的自然日全部休市，休市天数=日期差-1
    gap = (next_td - base).days - 1
    return last_td, next_td, gap


def contract_levels(code):
    """具体合约日K（英文列）：ATR14、近 CHAN_N 日高/低、收盘、日线MA10及前值。"""
    try:
        h = ak.futures_zh_daily_sina(symbol=code)
        if len(h) < max(CHAN_N, 12) + 1:
            return None
        h = h.sort_values("date").reset_index(drop=True)
        pc = h["settle"].where(h["settle"].notna(), h["close"]).shift(1)
        tr = pd.concat([h["high"]-h["low"], (h["high"]-pc).abs(),
                        (h["low"]-pc).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        ma = h["close"].rolling(10).mean()
        return dict(atr=atr, hi=float(h["high"].iloc[-CHAN_N:].max()),
                    lo=float(h["low"].iloc[-CHAN_N:].min()),
                    close=float(h["close"].iloc[-1]),
                    ma10=float(ma.iloc[-1]), ma10_prev=float(ma.iloc[-2]))
    except Exception:
        return None


def evaluate(p, q, r, lv, equity):
    """返回单持仓过节压力测试与灯号 dict。"""
    last, mult, vol, is_short = r["last"], r["mult"], r["vol"], r["is_short"]
    open_px = r["open_px"]
    out = dict(light="🟢", reasons=[], trend="", init=None, chan=None,
               worst_limit=None, worst_eq=0.0, atr3=None, atr3_eq=0.0)

    # 趋势 / 离场位
    if lv:
        atr = lv["atr"]
        if not is_short:
            out["init"] = open_px - STOP_K*atr if open_px else None
            out["chan"] = lv["hi"] - CHAN_K*atr
            broken_chan = last <= out["chan"] if last else False
            broken_ma = (last <= lv["ma10"] and lv["ma10"] <= lv["ma10_prev"]) if last else False
            side = "多"
        else:
            out["init"] = open_px + STOP_K*atr if open_px else None
            out["chan"] = lv["lo"] + CHAN_K*atr
            broken_chan = last >= out["chan"] if last else False
            broken_ma = (last >= lv["ma10"] and lv["ma10"] >= lv["ma10_prev"]) if last else False
            side = "空"
        if broken_chan:
            out["trend"] = f"已触发吊灯离场（吊灯线 {out['chan']:g}），趋势走坏"
        elif broken_ma:
            out["trend"] = f"已跌破/站上日线MA10（{lv['ma10']:g}）且均线拐头，环境转弱"
        else:
            out["trend"] = f"趋势仍在（MA10 {lv['ma10']:g}、吊灯线 {out['chan']:g} 未破）"
        out["atr3"] = 3.0*atr*mult*vol      # 反向跳空 3×ATR 的亏损额
    else:
        side = "空" if is_short else "多"
        out["trend"] = "取不到该合约日K，趋势位以交易终端/monitor_positions 为准"

    # 最坏情景一：封不利涨跌停
    if isinstance(q, dict) and q.get("ready"):
        up, dn = fnum(q.get("limitUp")), fnum(q.get("limitDown"))
        if last and up > dn:
            worst_px = dn if not is_short else up
            out["worst_limit"] = abs(last - worst_px)*mult*vol
    # 兜底：无涨跌停价时用 ATR 估算
    if not out["worst_limit"] and lv and last:
        out["worst_limit"] = 3.0*lv["atr"]*mult*vol
    out["worst_eq"] = (out["worst_limit"]/equity) if out["worst_limit"] else 0.0
    if out["atr3"] is None and lv:
        out["atr3"] = 3.0*lv["atr"]*mult*vol
    out["atr3_eq"] = (out["atr3"]/equity) if out["atr3"] else 0.0

    # 灯号判定
    red, yel = [], []
    if "走坏" in out["trend"] or "转弱" in out["trend"]:
        red.append("趋势已破位（吊灯/MA10）")
    if r["level"] == "DANGER":
        red.append("浮亏已达风险预算 100%（危险）")
    elif r["level"] == "WARN":
        red.append("浮亏达风险预算 80%（警告）")
    if out["worst_eq"] >= RED_WORST_EQ:
        red.append(f"封涨跌停最坏亏损达权益 {out['worst_eq']*100:.1f}%")
    if out["atr3_eq"] >= RED_ATR3_EQ:
        red.append(f"跳3×ATR亏损达权益 {out['atr3_eq']*100:.1f}%")
    if r["level"] == "INFO":
        yel.append("浮亏达风险预算 50%（关注）")
    if YEL_WORST_EQ <= out["worst_eq"] < RED_WORST_EQ:
        yel.append(f"封涨跌停最坏亏损占权益 {out['worst_eq']*100:.1f}%")
    if YEL_ATR3_EQ <= out["atr3_eq"] < RED_ATR3_EQ:
        yel.append(f"跳3×ATR亏损占权益 {out['atr3_eq']*100:.1f}%")

    if red:
        out["light"] = "🔴"; out["reasons"] = red
        out["advice"] = "不建议持仓过节，节前应减仓或离场（手动执行）"
    elif yel:
        out["light"] = "🟡"; out["reasons"] = yel
        out["advice"] = "建议降低仓位、轻仓过节，预留足够保证金"
    else:
        out["light"] = "🟢"; out["reasons"] = ["趋势未破、浮亏可控、极端跳空压力可承受"]
        out["advice"] = "可持仓，但仍建议轻仓并留足保证金"
    out["side"] = side
    return out


def outlook_lines():
    """复用最新三板斧日报 meta，给出节后做多/做空方向与入场时机（不重复扫描，口径与日报一致）。"""
    lines = ["- **节后方向与做多/做空时机**（环境取自最近一次三板斧日报，收盘口径）："]
    try:
        m = json.load(open(os.path.join(REP_DIR, "latest_meta.json"), encoding="utf-8"))
        lines.append(f"    - 方向信号：**{m.get('direction','均衡分化')}**（市场温度 {m.get('temperature','-')}，数据日 {m.get('date','-')}）。")
        ln = "、".join(m.get("long_names") or []) or "无"
        sn = "、".join(m.get("short_names") or []) or "无"
        lines.append(f"    - 顺势做多候选（多头共振）：{ln}")
        lines.append(f"    - 顺势做空候选（空头共振）：{sn}")
        bol = "、".join(m.get("breakout_long") or []) or "无"
        pol = "、".join(m.get("pullback_long") or []) or "无"
        bos = "、".join(m.get("breakout_short") or []) or "无"
        pos = "、".join(m.get("pullback_short") or []) or "无"
        lines.append(f"    - 已出做多扳机：突破20日通道放量 [{bol}]；回踩MA10企稳 [{pol}]")
        lines.append(f"    - 已出做空扳机：跌破20日通道放量 [{bos}]；反弹受压MA10 [{pos}]")
        t = "、".join(m.get("top") or []) or "-"; b = "、".join(m.get("bottom") or []) or "-"
        lines.append(f"    - 当日领涨：{t}；领跌：{b}")
    except Exception:
        lines.append("    - 暂无最新日报方向数据，以当晚 20:00 三板斧日报的方向与扳机为准。")
    lines.append("    - 节后操作：只做共振方向；长假后首日若大幅跳空不追单，等盘中企稳或次日收盘确认；"
                 "扳机（突破20日通道放量 / 回踩MA10企稳）不响不进场；进场仍按单笔风险1%定手数、初始止损1.5ATR。")
    return lines


def money(x):
    return f"{x:,.0f}"


def main():
    args = sys.argv[1:]
    force = "--force" in args
    base = None
    if "--date" in args:
        base = datetime.datetime.strptime(args[args.index("--date")+1], "%Y-%m-%d").date()
    else:
        base = datetime.date.today()
    now = datetime.datetime.now()

    # 1) 判断是否长假前最后交易日
    try:
        gap_info = holiday_gap(base)
    except Exception as e:
        gap_info = None
        cal_err = str(e)[:80]
    if not force:
        if gap_info is None:
            print("LEVEL=ERROR")
            print("### ⚠️ 节前离场检查：取不到交易日历（AkShare 通道异常），请人工核对节假日安排")
            return
        last_td, next_td, gap = gap_info
        if not (last_td == base and gap >= GAP_DAYS):
            print("LEVEL=SKIP")
            print(f"今天 {base} 不是长假前最后交易日（下一交易日 {next_td}），静默不推送。")
            return
    else:
        gap = gap_info[2] if gap_info else 0
        next_td = gap_info[1] if gap_info else None

    # 2) 拉账户与持仓
    acct = panda(["account"]); positions = panda(["positions"])
    if not isinstance(positions, list):
        print("LEVEL=ERROR")
        print(f"### ⚠️ 节前离场检查：无法获取账户持仓（PandaAI 通道异常或登录失效），请人工核对。时间 {now:%Y-%m-%d %H:%M}")
        return
    holds = [p for p in positions if fnum(pick(p, "position", "volume", "totalVolume", "total", default=0)) > 0]

    equity = FALLBACK_EQUITY; equity_src = "默认"
    avail = margin_acct = risk_rate = 0.0
    if isinstance(acct, dict):
        avail = fnum(acct.get("availableFunds")); margin_acct = fnum(acct.get("margin"))
        risk_rate = fnum(acct.get("riskRate"))
        if avail or margin_acct:
            equity = avail + margin_acct; equity_src = "柜台"
    risk_budget = equity * RISK_PCT

    L = []
    L.append(f"### 🛂 长假持仓离场检查 · {base}（{WD[base.weekday()]}）")
    if next_td:
        L.append(f"- **休市安排**：下一交易日 **{next_td}**，预计连休 **{gap} 天**。假期国内期货休市，"
                 "但境外黄金/有色/原油/农产品/股指照常交易，节后开盘可能**跳空**，挂单止损或被穿透成交。")
    if not holds:
        L.append("- **当前无持仓**：空仓过节不存在离场问题，也不承担跳空风险。")
        L.extend(outlook_lines())
        L.append("- 本检查为客观风险提示，不构成投资建议；Agent 全程只读、不会自动下单。")
        print("LEVEL=NOHOLD"); print(chr(10).join(L)); return

    # 3) 逐持仓评估
    items = []; total_mv = total_margin = total_worst = total_atr3 = 0.0
    for p in holds:
        raw = pick(p, "contractCode", "symbol", "instrumentId", default="")
        short, prefix = base_code(raw)
        q = panda(["quote", short])
        if not (isinstance(q, dict) and q.get("ready")):
            q = panda(["quote", str(raw).upper()])
        r = assess_position(p, q, risk_budget)
        lv = contract_levels(short)
        ev = evaluate(p, q, r, lv, equity)
        name = r["name"]
        items.append((name, short, r, ev, lv))
        total_mv += r["market_value"]; total_margin += r["margin"]
        total_worst += ev["worst_eq"]*equity; total_atr3 += ev["atr3_eq"]*equity

    items.sort(key=lambda x: ({"🔴":0,"🟡":1,"🟢":2}[x[3]["light"]], -(x[3]["worst_eq"])))
    n_red = sum(1 for _,_,_,ev,_ in items if ev["light"]=="🔴")
    n_yel = sum(1 for _,_,_,ev,_ in items if ev["light"]=="🟡")
    n_grn = len(items)-n_red-n_yel

    avail_after_bump = avail - MARGIN_BUMP*total_margin
    L.append(f"- **账户**：权益 {money(equity)} 元（{equity_src}）、可用 {money(avail)}、保证金占用 {money(total_margin)}"
             + (f"、风险度 {risk_rate*100:.1f}%" if risk_rate else "")
             + f"；合计名义敞口 {money(total_mv)}（占权益 {total_mv/equity*100:.0f}%）。")
    L.append("- **组合极端压力测试（情景假设，非行情预测）**：")
    L.append(f"    - 若节后所有持仓开盘均封在不利方向涨跌停：合计约再亏 **{money(total_worst)} 元＝权益 {total_worst/equity*100:.1f}%**；")
    L.append(f"    - 若节后平均反向跳空 3×ATR：合计约亏 **{money(total_atr3)} 元＝权益 {total_atr3/equity*100:.1f}%**；")
    bump_warn = "，⚠️可用资金或转负、存在追保风险" if avail_after_bump < 0 else ""
    L.append(f"    - 若节前保证金统一上调 {MARGIN_BUMP*100:.0f}%（情景，**以交易所/期货公司通知为准**）：新增占用约 "
             f"{money(MARGIN_BUMP*total_margin)}，可用资金降至约 {money(avail_after_bump)}{bump_warn}。")
    L.append(f"- **逐持仓结论（{n_red} 红 / {n_yel} 黄 / {n_grn} 绿）**：")
    for name, short, r, ev, lv in items:
        loss_txt = (f"浮亏 {money(abs(r['pnl_open']))}＝预算 {r['ratio']*100:.0f}%"
                    if r["pnl_open"] < 0 else f"浮盈 {money(r['pnl_open'])}")
        L.append(f"    - {ev['light']} **{name} {short} {r['side_text']} {r['vol']} 手**：现价 {r['last']:g}，{loss_txt}。{ev['trend']}。")
        L.append(f"        - 最坏封板再亏 {money(ev['worst_limit'] or 0)}（权益 {ev['worst_eq']*100:.1f}%）；"
                 f"跳3×ATR再亏 {money(ev['atr3'] or 0)}（权益 {ev['atr3_eq']*100:.1f}%）。")
        L.append(f"        - 结论：{ev['advice']}（{'、'.join(ev['reasons'])}）。")

    L.extend(outlook_lines())

    L.append("- **总体建议**：")
    if n_red:
        L.append(f"    - 有 **{n_red}** 个品种亮红灯，按客观规则**不建议持仓过节**，应在节前最后交易日手动减仓/离场；"
                 "要留也只留顺势、浮盈、极端跳空压力可承受的轻仓。")
    elif n_yel:
        L.append(f"    - 无红灯但有 {n_yel} 个黄灯，建议主动降仓、留足保证金，不要在节前加仓或赌开盘方向。")
    else:
        L.append("    - 全部绿灯、组合极端压力可承受，可轻仓过节；仍须预留保证金并接受跳空不确定性。")
    L.append("    - 长假前一晚通常无夜盘；具体提保比例、涨跌停板调整与休市安排以交易所/期货公司通知为准。")
    L.append("- 本检查为客观压力测试与信号提示，**不构成投资建议**；是否减仓/平仓由你本人手动决定，Agent 全程只读、不下单、不撤单。")

    md = chr(10).join(L)
    print("LEVEL=HOLIDAY")
    print(md)
    open(os.path.join(REP_DIR, f"holiday_check_{base}.md"), "w", encoding="utf-8").write(md)


if __name__ == "__main__":
    main()
