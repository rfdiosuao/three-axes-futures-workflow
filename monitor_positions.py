#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全持仓第一层风险监控（免费数据：PandaAI CLI；行情口径与 monitor_gold.py 一致）。
覆盖 PandaAI 仿真账户里的【全部持仓品种】，逐个按《新手操作与风控手册》分级：
  单笔风险预算 = 账户权益 × 1%；
  累计浮亏（相对开仓价）达预算 50%=INFO / 80%=WARN / 100%=DANGER；
  价格逼近自身不利方向涨跌停（3% 以内）= DANGER。
输出：第一行 LEVEL=<NONE|INFO|WARN|DANGER|ERROR>，其后为 Markdown 组合简报。
用法：
  python3 monitor_positions.py          # 扫描模式：仅 WARN/DANGER/ERROR 需推送
  python3 monitor_positions.py --brief  # 收盘简报：强制输出组合小结（INFO 起）
本脚本全程只读，不下单、不撤单、不改单；任何平仓动作必须用户手动确认。
"""
import os, subprocess, json, sys, datetime

RISK_PCT = 0.01
NEAR_LIMIT = 0.03          # 距不利方向涨跌停 3% 以内视为逼近
EXPOSURE_PCT = 0.10        # 三板斧单品种名义敞口参考上限 = 权益的 10%
FALLBACK_EQUITY = float(os.environ.get("ACCOUNT_EQUITY", "5000000"))

# 常见期货品种代码前缀 -> 中文名（未命中则显示代码本身）
CN = {
    "AU": "黄金", "AG": "白银", "CU": "沪铜", "AL": "沪铝", "PB": "沪铅", "ZN": "沪锌",
    "NI": "沪镍", "SN": "沪锡", "BC": "国际铜", "SS": "不锈钢", "AO": "氧化铝", "SI": "工业硅",
    "LC": "碳酸锂", "RB": "螺纹钢", "HC": "热卷", "I": "铁矿石", "J": "焦炭", "JM": "焦煤",
    "SF": "硅铁", "SM": "锰硅", "ZC": "动力煤", "SC": "原油", "FU": "燃料油", "BU": "沥青",
    "LU": "低硫燃油", "RU": "橡胶", "NR": "20号胶", "SP": "纸浆", "BR": "丁二烯橡胶",
    "EC": "集运指数", "A": "豆一", "B": "豆二", "M": "豆粕", "Y": "豆油", "P": "棕榈油",
    "C": "玉米", "CS": "玉米淀粉", "JD": "鸡蛋", "L": "塑料", "V": "PVC", "PP": "聚丙烯",
    "EG": "乙二醇", "EB": "苯乙烯", "PG": "LPG", "LH": "生猪", "RM": "菜粕", "OI": "菜油",
    "SR": "白糖", "CF": "棉花", "CY": "棉纱", "TA": "PTA", "MA": "甲醇", "FG": "玻璃",
    "SA": "纯碱", "UR": "尿素", "PF": "短纤", "AP": "苹果", "CJ": "红枣", "PK": "花生",
    "PX": "PX", "SH": "烧碱", "PR": "瓶片", "QF": "铸造硅铁",
    "IF": "沪深300", "IH": "上证50", "IC": "中证500", "IM": "中证1000",
    "TS": "2年国债", "TF": "5年国债", "T": "10年国债", "TL": "30年国债",
}

LEVEL_RANK = {"NONE": 0, "INFO": 1, "WARN": 2, "DANGER": 3, "ERROR": 4}
LEVEL_EMOJI = {"DANGER": "🛑", "WARN": "🟠", "INFO": "🟡", "NONE": "🟢"}
LEVEL_HEAD = {
    "DANGER": "组合持仓风险·危险（有品种触及止损预算/逼近涨跌停）",
    "WARN": "组合持仓风险·警告（有品种浮亏达风险预算 80%）",
    "INFO": "组合持仓风险·关注（有品种浮亏达风险预算 50%）",
    "NONE": "组合持仓·正常",
}


def panda(args):
    try:
        r = subprocess.run(["panda"] + args + ["--json"], capture_output=True,
                           text=True, timeout=40)
        j = json.loads(r.stdout)
        return j.get("data") if j.get("ok") else None
    except Exception:
        return None


def pick(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return default


def base_code(code):
    """AU2612.SHF -> AU2612；返回(交易代码, 前缀)。"""
    code = str(code).strip().upper()
    short = code.split(".")[0]
    prefix = ""
    for ch in short:
        if ch.isalpha():
            prefix += ch
        else:
            break
    return short, prefix


def cname(prefix, short):
    return CN.get(prefix, short)


def fnum(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def assess_position(p, q, risk_budget):
    """返回单个持仓的风控 dict。"""
    raw_code = pick(p, "contractCode", "symbol", "instrumentId", default="")
    short, prefix = base_code(raw_code)
    name = cname(prefix, short)
    vol = fnum(pick(p, "position", "volume", "totalVolume", "total", default=0))
    side = str(pick(p, "direction", "side", "posSide", default="long")).lower()
    side_text = pick(p, "directionText", default=("多头" if side != "short" else "空头"))
    is_short = ("short" in side) or ("空" in str(side_text)) or side in ("sell", "2")
    sign = -1 if is_short else 1
    open_px = fnum(pick(p, "openPrice", "avgPrice", "costPrice", default=0))
    mult = fnum(pick("roundLot", "multiplier", "contractMultiplier", default=1)) or 1.0

    # 最新价：优先实时 quote，回退持仓自带 lastPrice
    q_ready = bool(isinstance(q, dict) and q.get("ready"))
    last = fnum(q.get("latestPrice")) if q_ready else fnum(pick(p, "lastPrice", default=0))
    if not last:
        last = fnum(pick(p, "lastPrice", default=0))

    # 累计浮动盈亏（相对开仓价，与黄金监控口径一致）
    pnl_open = sign * (last - open_px) * mult * vol if (last and open_px) else 0.0
    ratio = (abs(pnl_open) / risk_budget) if pnl_open < 0 else 0.0

    # 柜台盯市盈亏（相对上日结算价，仅展示）
    pnl_mtm = fnum(pick(p, "holdingPnl", "positionPnl", default=0))
    margin = fnum(pick(p, "margin", default=0))
    market_value = last * mult * vol if last else fnum(pick(p, "marketValue", default=0))

    # 涨跌停位置（0=跌停，1=涨停）与当日数据
    dist = None
    chg_pct = None
    hi = lo = pre = None
    trade_date = pick(p, "tradeDate", default="")
    if q_ready:
        up = fnum(q.get("limitUp"))
        dn = fnum(q.get("limitDown"))
        if up > dn:
            dist = (last - dn) / (up - dn)
        pre = fnum(q.get("preclose"))
        chg_pct = fnum(q.get("changeRate")) or ((last - pre) / pre if pre else 0)
        hi = fnum(q.get("high")) or last
        lo = fnum(q.get("low")) or last
        trade_date = str(q.get("tradeDate") or trade_date)

    # 分级
    level = "NONE"
    near_bad_limit = False
    if dist is not None:
        # 多头怕跌停(dist→0)，空头怕涨停(dist→1)
        near_bad_limit = (dist <= NEAR_LIMIT) if not is_short else (dist >= 1 - NEAR_LIMIT)
    if ratio >= 1.0 or near_bad_limit:
        level = "DANGER"
    elif ratio >= 0.8:
        level = "WARN"
    elif ratio >= 0.5:
        level = "INFO"

    return {
        "short": short, "name": name, "side_text": side_text, "is_short": is_short,
        "vol": int(vol), "open_px": open_px, "last": last, "mult": mult,
        "pnl_open": pnl_open, "ratio": ratio, "pnl_mtm": pnl_mtm, "margin": margin,
        "market_value": market_value, "dist": dist, "chg_pct": chg_pct,
        "hi": hi, "lo": lo, "pre": pre, "trade_date": trade_date,
        "q_ready": q_ready, "level": level, "near_bad_limit": near_bad_limit,
    }


def main():
    force_brief = "--brief" in sys.argv
    now = datetime.datetime.now()
    acct = panda(["account"])
    positions = panda(["positions"])

    if not isinstance(positions, list):
        print("LEVEL=ERROR")
        print(f"⚠️ 全持仓监控：无法获取账户持仓（PandaAI 通道异常、登录失效或当前休市）。"
              f"请人工打开 PandaAI 交易终端核对账户。时间 {now:%Y-%m-%d %H:%M}")
        return

    # 只保留有持仓量的条目
    holds = []
    for p in positions:
        try:
            if fnum(pick(p, "position", "volume", "totalVolume", "total", default=0)) > 0:
                holds.append(p)
        except Exception:
            continue

    if not holds:
        print("LEVEL=NONE")
        print("### 组合持仓·当前无持仓")
        print(f"时间 {now:%Y-%m-%d %H:%M}")
        print("- PandaAI 仿真账户当前无任何持仓，第一层持仓风控无触发项。")
        print("- 说明：无持仓品种的开仓信号/异常波动由盘后三板斧日报覆盖；本监控只对持仓做风险预警。")
        return

    # 账户权益 = 可用资金 + 占用保证金（与黄金监控一致）
    equity = FALLBACK_EQUITY
    equity_src = "默认"
    if isinstance(acct, dict):
        avail = fnum(acct.get("availableFunds"))
        margin = fnum(acct.get("margin"))
        if avail or margin:
            equity = avail + margin
            equity_src = "柜台"
    risk_budget = equity * RISK_PCT
    risk_rate = fnum(acct.get("riskRate")) if isinstance(acct, dict) else 0.0

    # 逐品种取行情并评估
    items, bad_quote = [], 0
    trade_dates = set()
    for p in holds:
        raw = pick(p, "contractCode", "symbol", "instrumentId", default="")
        short, _ = base_code(raw)
        q = panda(["quote", short])
        if not (isinstance(q, dict) and q.get("ready")):
            q = panda(["quote", str(raw).upper()])  # 再试带后缀
        r = assess_position(p, q, risk_budget)
        if not r["q_ready"]:
            bad_quote += 1
        if r["trade_date"]:
            trade_dates.add(str(r["trade_date"]))
        items.append(r)

    if not any(r["last"] for r in items):
        print("LEVEL=ERROR")
        print(f"⚠️ 全持仓监控：账户有 {len(holds)} 个持仓，但全部取不到有效行情"
              f"（PandaAI 行情通道异常、登录失效或当前休市）。请人工打开交易终端核对。"
              f"时间 {now:%Y-%m-%d %H:%M}")
        return

    # 排序：风险等级高的在前，同级按亏损额大的在前
    items.sort(key=lambda r: (LEVEL_RANK[r["level"]], abs(min(r["pnl_open"], 0))), reverse=True)
    top = max((r["level"] for r in items), key=lambda x: LEVEL_RANK[x])
    total_pnl_open = sum(r["pnl_open"] for r in items)
    total_pnl_mtm = sum(r["pnl_mtm"] for r in items)
    total_mv = sum(r["market_value"] for r in items)

    if force_brief and top == "NONE":
        out_level = "INFO"
    else:
        out_level = top

    td = "/".join(sorted(trade_dates)) if trade_dates else "n/a"
    stale = ""
    today_compact = now.strftime("%Y%m%d")
    if td and td != today_compact:
        stale = f"（休市/非交易时段，下列为最新有效收盘价，行情日 {td}）"

    print(f"LEVEL={out_level}")
    print(f"### {LEVEL_EMOJI[out_level]} {LEVEL_HEAD[top]}")
    print(f"时间 {now:%Y-%m-%d %H:%M}{stale}")
    print(f"- 账户权益 {equity:,.0f} 元（{equity_src}），单笔风险预算（1%）{risk_budget:,.0f} 元；"
          f"持仓 {len(items)} 个品种，合计名义敞口 {total_mv:,.0f} 元（占权益 {total_mv/equity*100:.0f}%）"
          + (f"，保证金风险度 {risk_rate*100:.1f}%" if risk_rate else ""))
    print(f"- 合计浮动盈亏：相对开仓价 {total_pnl_open:,.0f} 元；柜台盯市（相对上日结算）{total_pnl_mtm:,.0f} 元")

    for r in items:
        emoji = LEVEL_EMOJI[r["level"]]
        loss_txt = (f"浮亏 {abs(r['pnl_open']):,.0f} 元＝预算 {r['ratio']*100:.0f}%"
                    if r["pnl_open"] < 0 else f"浮盈 {r['pnl_open']:,.0f} 元")
        head = (f"- {emoji} **{r['name']} {r['short']}** {r['side_text']} {r['vol']} 手"
                f"（乘数 {r['mult']:g}）：开仓 {r['open_px']:g} → 现价 {r['last']:g}，{loss_txt}")
        detail = []
        if r["chg_pct"] is not None:
            detail.append(f"当日 {r['chg_pct']*100:+.2f}%")
        if r["dist"] is not None:
            # 距自身不利方向涨跌停
            bad = r["dist"] if not r["is_short"] else 1 - r["dist"]
            detail.append(f"距不利方向涨跌停 {bad*100:.0f}%")
        if r["market_value"]:
            detail.append(f"名义敞口 {r['market_value']/equity*100:.0f}% 权益"
                          + (" ⚠️超10%参考上限" if r["market_value"]/equity > EXPOSURE_PCT else ""))
        if r["pnl_mtm"]:
            detail.append(f"柜台盯市 {r['pnl_mtm']:,.0f}")
        if not r["q_ready"]:
            detail.append("⚠️实时行情缺失，用持仓昨价")
        print(head)
        if detail:
            print(f"    - " + "；".join(detail))
        if r["level"] == "DANGER":
            why = []
            if r["ratio"] >= 1.0:
                why.append("累计浮亏已达/超 100% 风险预算")
            if r["near_bad_limit"]:
                why.append("价格逼近不利方向涨跌停")
            print(f"    - 🛂 触发危险：{'、'.join(why)}")

    if top in ("WARN", "DANGER"):
        print("- 提醒：以上为客观风险预警，不构成买卖建议；是否平仓/离场请你手动确认后操作，Agent 全程只读、不会自动下单。")
    if bad_quote:
        print(f"- 注：{bad_quote} 个品种实时行情未取到，已用持仓内最新价估算，复市后以交易终端为准。")


if __name__ == "__main__":
    main()
