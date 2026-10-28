#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三板斧期市日报生成器（免费数据：AkShare 新浪主力连续 + futures_rule + PandaAI CLI）。
借鉴专业复盘骨架（总览→结构→榜单→关注→提示→免责），但全部内容按“三板斧”体系
（①选环境：月/周/日 MA10 三周期共振；②严格止损：1.5×ATR；③以损定量：1%风险+10%敞口）
用客观数据重算，不编造宏观/地缘新闻。
产出：reports/daily_report_<交易日>.xml（飞书文档）、reports/meta_<日>.json、
      snapshots/regime_<日>.csv；stdout 打印 meta JSON。
只读、不下单；报告不构成投资建议。
"""
import os, json, math, subprocess, datetime
import akshare as ak
import pandas as pd

# 工作目录 = 脚本所在目录（clone 后即可运行，无需硬编码路径）
BASE = os.path.dirname(os.path.abspath(__file__))
REP_DIR = os.path.join(BASE, "reports"); SNAP_DIR = os.path.join(BASE, "snapshots")
os.makedirs(REP_DIR, exist_ok=True); os.makedirs(SNAP_DIR, exist_ok=True)
EQUITY = float(os.environ.get("ACCOUNT_EQUITY", "5000000")); RISK_PCT = 0.01; STOP_K = 1.5
CONTRACT = "AU2612"
# —— 入场/离场信号参数（经典趋势跟踪惯例，可按回测调整）——
DON_N = 20          # 唐奇安通道回看根数：收盘破近20日高/低点=突破
VOL_MULT = 1.2      # 突破放量确认：当日量 ≥ 20日均量的 1.2 倍
PB_TOL = 0.005      # 回踩 MA10 容差 0.5%
CHAN_K = 3.0        # 吊灯跟踪止盈 ATR 倍数
CHAN_N = 22         # 吊灯极值回看根数

WATCH = {
 "AU0":"黄金","AG0":"白银","CU0":"铜","AL0":"铝","ZN0":"锌","NI0":"镍","SN0":"锡",
 "RB0":"螺纹钢","HC0":"热卷","SS0":"不锈钢","FU0":"燃料油","BU0":"沥青","RU0":"橡胶",
 "SC0":"原油","LU0":"低硫燃油","NR0":"20号胶","BC0":"国际铜",
 "M0":"豆粕","Y0":"豆油","P0":"棕榈油","A0":"豆一","C0":"玉米","CS0":"玉米淀粉",
 "JD0":"鸡蛋","I0":"铁矿石","J0":"焦炭","JM0":"焦煤","L0":"塑料","PP0":"聚丙烯",
 "V0":"PVC","EG0":"乙二醇","EB0":"苯乙烯","PG0":"液化气","LH0":"生猪",
 "SR0":"白糖","CF0":"棉花","TA0":"PTA","MA0":"甲醇","OI0":"菜油","RM0":"菜粕",
 "FG0":"玻璃","SA0":"纯碱","AP0":"苹果","UR0":"尿素","PF0":"短纤",
 "IF0":"沪深300","IH0":"上证50","IC0":"中证500","IM0":"中证1000",
 "SI0":"工业硅","LC0":"碳酸锂",
}
SECTOR = {
 "AU":"贵金属","AG":"贵金属",
 "CU":"有色","AL":"有色","ZN":"有色","NI":"有色","SN":"有色","SS":"有色","BC":"有色","SI":"有色","LC":"有色",
 "RB":"黑色","HC":"黑色","I":"黑色","J":"黑色","JM":"黑色",
 "FU":"能化","BU":"能化","RU":"能化","NR":"能化","SC":"能化","LU":"能化","L":"能化","PP":"能化",
 "V":"能化","EG":"能化","EB":"能化","PG":"能化","TA":"能化","MA":"能化","PF":"能化","UR":"能化",
 "FG":"建材","SA":"建材",
 "M":"农产品","Y":"农产品","P":"农产品","A":"农产品","C":"农产品","CS":"农产品","JD":"农产品",
 "LH":"农产品","OI":"农产品","RM":"农产品","SR":"农产品","CF":"农产品","AP":"农产品",
 "IF":"股指","IH":"股指","IC":"股指","IM":"股指",
}
WD = ["周一","周二","周三","周四","周五","周六","周日"]

def esc(s):
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def resample(df, rule):
    d = df.copy(); d["日期"] = pd.to_datetime(d["日期"]); d = d.set_index("日期")
    return pd.DataFrame({
        "open": d["开盘价"].resample(rule).first(), "high": d["最高价"].resample(rule).max(),
        "low": d["最低价"].resample(rule).min(), "close": d["收盘价"].resample(rule).last()}).dropna()

def cycle_detail(df):
    """返回月/周/日三周期各自『收盘价在 MA10 哪一侧 + MA10 是否上行』。
    慢锚(季节)=月线+周线，快变量(天气)=日线；供信号调研看板做证据下钻。"""
    wk, mo, dy = resample(df,"W-FRI"), resample(df,"ME"), resample(df,"D")
    def pos(x):
        ma = x["close"].rolling(10).mean()
        return (bool(x["close"].iloc[-1] > ma.iloc[-1]), bool(ma.iloc[-1] > ma.iloc[-2]))
    m_above,_ = pos(mo); w_above,w_up = pos(wk); d_above,d_up = pos(dy)
    return dict(m_above=m_above,w_above=w_above,w_up=w_up,d_above=d_above,d_up=d_up)

def side(df):
    c = cycle_detail(df)
    if c["m_above"] and c["w_above"] and c["w_up"] and c["d_above"] and c["d_up"]: return "long"
    if (not c["m_above"]) and (not c["w_above"]) and (not c["w_up"]) and (not c["d_above"]) and (not c["d_up"]): return "short"
    return "flat"

def atr14(df):
    d = df.sort_values("日期").reset_index(drop=True); pc = d["收盘价"].shift(1)
    tr = pd.concat([d["最高价"]-d["最低价"],(d["最高价"]-pc).abs(),(d["最低价"]-pc).abs()],axis=1).max(axis=1)
    return float(tr.rolling(14).mean().iloc[-1])

def contract_levels(code):
    """拉具体持仓合约日K（futures_zh_daily_sina，英文列），算 ATR14、近 CHAN_N 日最高/最低、
    收盘、日线MA10及前值，用于初始止损与吊灯跟踪止盈；失败返回 None。"""
    try:
        h = ak.futures_zh_daily_sina(symbol=code)
        if len(h) < max(CHAN_N, 12) + 1: return None
        h = h.sort_values("date").reset_index(drop=True)
        pc = h["settle"].where(h["settle"].notna(), h["close"]).shift(1)
        tr = pd.concat([h["high"]-h["low"],(h["high"]-pc).abs(),(h["low"]-pc).abs()],axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        ma = h["close"].rolling(10).mean()
        return dict(atr=atr, hi=float(h["high"].iloc[-CHAN_N:].max()), lo=float(h["low"].iloc[-CHAN_N:].min()),
                    close=float(h["close"].iloc[-1]), ma10=float(ma.iloc[-1]), ma10_prev=float(ma.iloc[-2]))
    except Exception:
        return None

def panda(args):
    try:
        r = subprocess.run(["panda"]+args+["--json"],capture_output=True,text=True,timeout=40)
        j = json.loads(r.stdout); return j.get("data") if j.get("ok") else None
    except Exception:
        return None

def tbl(headers, rows):
    h = "".join(f'<th background-color="light-gray"><p>{esc(x)}</p></th>' for x in headers)
    body = []
    for r in rows:
        body.append("<tr>"+"".join(f"<td><p>{esc(c)}</p></td>" for c in r)+"</tr>")
    return "<table><thead><tr>"+h+"</tr></thead><tbody>"+"".join(body)+"</tbody></table>"

def main():
    rule = ak.futures_rule(); rmap = {str(r["代码"]).strip(): r for _,r in rule.iterrows()}
    try:
        avail = set(ak.futures_display_main_sina()["symbol"].astype(str))
    except Exception:
        avail = set(WATCH)
    rows, snap = [], []
    for sym,name in WATCH.items():
        if sym not in avail: continue
        try:
            d = ak.futures_main_sina(symbol=sym)
            if len(d) < 210: continue
            d = d.sort_values("日期").reset_index(drop=True)
            last = float(d["收盘价"].iloc[-1]); code = sym[:-1]; sector = SECTOR.get(code,"其他")
            prev_settle = float(d["动态结算价"].iloc[-2]) if not pd.isna(d["动态结算价"].iloc[-2]) and float(d["动态结算价"].iloc[-2]) else float(d["收盘价"].iloc[-2])
            chg = last/prev_settle-1
            hold = float(d["持仓量"].iloc[-1]); hold_prev = float(d["持仓量"].iloc[-2]); hchg = hold-hold_prev
            atr = atr14(d)
            s_now = side(d); s_prev = side(d.iloc[:-1])
            ri = rmap.get(code,{}); mult = float(ri.get("合约乘数",1) or 1); mrate = float(ri.get("交易保证金比例",0) or 0)
            sd = STOP_K*atr; lots_risk = math.floor(EQUITY*RISK_PCT/(sd*mult)) if sd*mult>0 else 0
            lots_exp = math.floor(EQUITY*0.10/(last*mult)) if last*mult>0 else 0
            lots = min(lots_risk,lots_exp); capped = lots_exp < lots_risk
            stop_px = last-sd if s_now=="long" else last+sd if s_now=="short" else None
            margin = last*mult*mrate/100 if mrate else 0
            D = str(d["日期"].iloc[-1])[:10]
            # --- 入场扳机（日线收盘确认；仅在共振方向）---
            c0=float(d["收盘价"].iloc[-1]); o0=float(d["开盘价"].iloc[-1])
            h0=float(d["最高价"].iloc[-1]); l0=float(d["最低价"].iloc[-1])
            dma=d["收盘价"].rolling(10).mean(); ma10=float(dma.iloc[-1]); ma10p=float(dma.iloc[-2])
            don_hi=float(d["最高价"].iloc[-(DON_N+1):-1].max()); don_lo=float(d["最低价"].iloc[-(DON_N+1):-1].min())
            vcol="成交量" if "成交量" in d.columns else "持仓量"
            vnow=float(d[vcol].iloc[-1]); vma=float(d[vcol].iloc[-(DON_N+1):-1].mean())
            vol_ok = (vnow >= VOL_MULT*vma) if vma>0 else False
            bo_long = (s_now=="long"  and c0>don_hi and vol_ok)
            bo_short = (s_now=="short" and c0<don_lo and vol_ok)
            pb_long = (s_now=="long"  and not bo_long  and l0<=ma10*(1+PB_TOL) and c0>o0 and c0>ma10)
            pb_short = (s_now=="short" and not bo_short and h0>=ma10*(1-PB_TOL) and c0<o0 and c0<ma10)
            # —— 信号调研看板证据字段：慢锚(季节=月/周)、快变量(天气=日)、价量象限、扳机分类 ——
            cyc = cycle_detail(d)
            if cyc["m_above"] and cyc["w_above"] and cyc["w_up"]: season="月周偏多"
            elif (not cyc["m_above"]) and (not cyc["w_above"]) and (not cyc["w_up"]): season="月周偏空"
            else: season="月周分歧"
            if cyc["d_above"] and cyc["d_up"]: weather="日线偏多"
            elif (not cyc["d_above"]) and (not cyc["d_up"]): weather="日线偏空"
            else: weather="日线中性"
            ma_dir = "上行" if cyc["d_up"] else "下行"
            if chg>0: quad="价涨仓增·多头进攻" if hchg>0 else "价涨仓减·空头回补"
            elif chg<0: quad="价跌仓增·空头打压" if hchg>0 else "价跌仓减·多头止损"
            else: quad="平盘"
            trig = ("突破多" if bo_long else "回踩多" if pb_long else
                    "突破空" if bo_short else "回踩空" if pb_short else "等待")
            rows.append(dict(sym=sym,code=code,name=name,sector=sector,last=last,chg=chg,hold=hold,
                hchg=hchg,atr=atr,side=s_now,side_prev=s_prev,mult=mult,mrate=mrate,lots=lots,
                capped=capped,stop_px=stop_px,sd=sd,margin=margin,date=D,
                ma10=ma10,ma10_up=ma10>ma10p,don_hi=don_hi,don_lo=don_lo,vol_ok=vol_ok,
                bo_long=bo_long,bo_short=bo_short,pb_long=pb_long,pb_short=pb_short,
                m_above=cyc["m_above"],w_above=cyc["w_above"],w_up=cyc["w_up"],
                d_above=cyc["d_above"],d_up=cyc["d_up"],
                season=season,weather=weather,ma_dir=ma_dir,quad=quad,trig=trig))
            snap.append(dict(date=D,symbol=sym,name=name,sector=sector,regime=s_now,last=round(last,2),chg_pct=round(chg*100,2),atr=round(atr,2)))
        except Exception as e:
            print(f"[fail] {sym}: {str(e)[:60]}", flush=True)
    if not rows:
        print(json.dumps({"error":"no data"},ensure_ascii=False)); return
    D = rows[0]["date"]; wd = WD[datetime.datetime.strptime(D,"%Y-%m-%d").weekday()]
    longs = [r for r in rows if r["side"]=="long"]; shorts = [r for r in rows if r["side"]=="short"]
    flats = [r for r in rows if r["side"]=="flat"]
    up = [r for r in rows if r["chg"]>0]; dn = [r for r in rows if r["chg"]<0]; fl = [r for r in rows if r["chg"]==0]
    up_ratio = len(up)/max(1,len(up)+len(dn))
    if len(longs)>len(shorts) and up_ratio>=0.55: temp="偏多"
    elif len(shorts)>len(longs) and up_ratio<=0.45: temp="偏空"
    else: temp="均衡分化"
    new_long=[r for r in rows if r["side"]=="long" and r["side_prev"]!="long"]
    new_short=[r for r in rows if r["side"]=="short" and r["side_prev"]!="short"]
    exit_long=[r for r in rows if r["side_prev"]=="long" and r["side"]!="long"]
    exit_short=[r for r in rows if r["side_prev"]=="short" and r["side"]!="short"]
    # —— 看板用：状态变化、反证/失效位、分层结论（事实→规则信号→含义，主动给反证）——
    for r in rows:
        if r["side"]=="long" and r["side_prev"]!="long": r["change"]="新进入多头"
        elif r["side"]=="short" and r["side_prev"]!="short": r["change"]="新进入空头"
        elif r["side_prev"]=="long" and r["side"]!="long": r["change"]="跌出多头"
        elif r["side_prev"]=="short" and r["side"]!="short": r["change"]="跌出空头"
        else: r["change"]="持续"
        sp = f'{r["stop_px"]:.1f}' if r["stop_px"] else "—"
        if r["side"]=="long":
            r["invalid"]=f"收盘跌破日线MA10({r['ma10']:.1f})且拐头、触及1.5ATR止损{sp}，或跌出多头共振"
        elif r["side"]=="short":
            r["invalid"]=f"收盘站上日线MA10({r['ma10']:.1f})且拐头、触及1.5ATR止损{sp}，或跌出空头共振"
        else:
            r["invalid"]="三周期不共振，无持仓方向信号"
        lots_txt = "不足1手·不做" if r["lots"]<1 else f'{r["lots"]}手'+("(敞口封顶)" if r["capped"] else "")
        if r["side"]=="long":
            if r["trig"]=="突破多": c=f"季节(月/周)偏多、天气(日)共振，今日放量突破20日通道上沿{r['don_hi']:.1f}，做多扳机触发"
            elif r["trig"]=="回踩多": c=f"月周日多头共振，今日回踩日线MA10({r['ma10']:.1f})企稳收阳，回踩做多扳机触发"
            else: c="月周日多头共振(季节偏多)，今日扳机未响，候选·等待，不追高"
            if r["change"]=="新进入多头": c+="；今日新进入多头共振，趋势潜在启动"
            c+=f"；参考止损{sp}(1.5ATR)、建议{lots_txt}，破MA10/止损或跌出共振即反证失效"
        elif r["side"]=="short":
            if r["trig"]=="突破空": c=f"季节(月/周)偏空、天气(日)共振，今日放量跌破20日通道下沿{r['don_lo']:.1f}，做空扳机触发"
            elif r["trig"]=="回踩空": c=f"月周日空头共振，今日反抽日线MA10({r['ma10']:.1f})受阻收阴，回踩做空扳机触发"
            else: c="月周日空头共振(季节偏空)，今日扳机未响，候选·等待，不抄底"
            if r["change"]=="新进入空头": c+="；今日新进入空头共振，走弱信号"
            c+=f"；参考止损{sp}(1.5ATR)、建议{lots_txt}，站上MA10/止损或跌出共振即反证失效"
        else:
            c=f"月/周/日未同向共振(季节{r['season']}、天气{r['weather']})，长短方向不一致，按规则暂不做；价量象限：{r['quad']}"
        r["conclusion"]=c+"。客观规则信号、非买卖建议、需自行回测"
    # 入场扳机名单
    bo_l=[r for r in rows if r["bo_long"]]; bo_s=[r for r in rows if r["bo_short"]]
    pb_l=[r for r in rows if r["pb_long"]]; pb_s=[r for r in rows if r["pb_short"]]
    top=sorted(rows,key=lambda r:-r["chg"])[:5]; bot=sorted(rows,key=lambda r:r["chg"])[:5]
    # 板块汇总
    secs={}
    for r in rows: secs.setdefault(r["sector"],[]).append(r)
    sec_rows=[]
    for s,rs in secs.items():
        avg=sum(x["chg"] for x in rs)/len(rs)
        sec_rows.append((s,len(rs),sum(1 for x in rs if x["chg"]>0),sum(1 for x in rs if x["chg"]<0),
                         sum(1 for x in rs if x["side"]=="long"),sum(1 for x in rs if x["side"]=="short"),avg))
    sec_rows.sort(key=lambda x:-x[6])
    # 四象限
    q1=[r for r in rows if r["chg"]>0 and r["hchg"]>0]; q2=[r for r in rows if r["chg"]>0 and r["hchg"]<=0]
    q3=[r for r in rows if r["chg"]<0 and r["hchg"]>0]; q4=[r for r in rows if r["chg"]<0 and r["hchg"]<=0]
    def qlist(q):
        q=sorted(q,key=lambda r:-abs(r["chg"]))[:6]
        return "、".join(f'{x["name"]}({x["chg"]*100:+.2f}%,仓{x["hchg"]:+.0f})' for x in q) or "无显著品种"

    # ===== 今日方向研判（确定性、可复算；信号层面的客观倾向，非买卖指令）=====
    long_names = [r["name"] for r in sorted(longs, key=lambda r:-r["chg"])]
    short_names = [r["name"] for r in sorted(shorts, key=lambda r:r["chg"])]
    bull_sec = [s for (s,n,u,d2,lo,sh,a) in sec_rows if a > 0]
    bear_sec = [s for (s,n,u,d2,lo,sh,a) in sec_rows if a < 0]
    n_q1, n_q3 = len(q1), len(q3)
    net = len(longs) - len(shorts)

    def nm(lst, k=8):
        return "、".join(lst[:k]) if lst else "无"

    if temp == "偏多" and net >= 2:
        direction, d_emoji, d_bg, d_bd = "偏多", "🔴", "light-red", "red"
        d_word = "多头占优，信号方向偏多：顺势优先在多头共振品种里找机会，不逆势做空、不追高"
    elif temp == "偏空" and net <= -2:
        direction, d_emoji, d_bg, d_bd = "偏空", "🟢", "light-green", "green"
        d_word = "空头占优，信号方向偏空：以防守/顺势找空为主，不轻易抄底、不逆势做多"
    else:
        direction, d_emoji, d_bg, d_bd = "均衡分化", "🟡", "light-yellow", "yellow"
        d_word = "多空拉锯、无统一方向：控制仓位、观望为主，等待多空共振净差拉开再顺势"

    bull_evidence = [
        f"多头共振 {len(longs)} 个（月/周/日收盘价同在 MA10 上方且周线日线上行）：{nm(long_names)}",
        (f"今日新进入多头共振 {len(new_long)} 个：{nm([r['name'] for r in new_long])}（趋势潜在启动）"
         if new_long else "今日无新进入多头共振品种"),
        f"上涨 {len(up)} 家 vs 下跌 {len(dn)} 家，上涨占比 {up_ratio*100:.0f}%",
        (f"领涨板块：{nm(bull_sec,4)}；领涨品种：{nm([r['name'] for r in top[:3]],3)}"
         if bull_sec else "无板块整体收涨"),
        (f"价涨仓增（多头主动进攻）象限 {n_q1} 个：{qlist(q1)}" if n_q1
         else "无价涨仓增（多头主动进攻）品种"),
    ]
    bear_evidence = [
        f"空头共振 {len(shorts)} 个（三周期收盘价同在 MA10 下方且周线日线下行）：{nm(short_names)}",
        (f"今日新进入空头共振 {len(new_short)} 个：{nm([r['name'] for r in new_short])}（走弱信号）"
         if new_short else "今日无新进入空头共振品种"),
        f"下跌 {len(dn)} 家 vs 上涨 {len(up)} 家，下跌占比 {(1-up_ratio)*100:.0f}%",
        (f"领跌板块：{nm(bear_sec,4)}；领跌品种：{nm([r['name'] for r in bot[:3]],3)}"
         if bear_sec else "无板块整体收跌"),
        (f"价跌仓增（空头主动打压）象限 {n_q3} 个：{qlist(q3)}" if n_q3
         else "无价跌仓增（空头主动打压）品种"),
    ]

    # 持仓/账户
    acct=panda(["account"]); pos=panda(["positions"]); qt=panda(["quote",CONTRACT])
    gold=next((r for r in rows if r["sym"]=="AU0"),None)

    # 持仓离场信号：初始止损(1.5ATR) / 吊灯跟踪止盈(CHAN_K×ATR, CHAN_N日极值) / 日线MA10反转
    pos_exits=[]
    for p in (pos if isinstance(pos,list) else []):
        code=str(p.get("contractCode","")).split(".")[0]
        if not code: continue
        pdir=str(p.get("direction","")).lower()
        last=float(p.get("lastPrice",0) or 0); openpx=float(p.get("openPrice",0) or 0)
        item=dict(name=p.get("contractName",code), code=code, direction=pdir,
                  qty=p.get("position"), last=last, open=openpx)
        lv=contract_levels(code)
        if lv and last>0 and openpx>0:
            atr=lv["atr"]
            if pdir=="long":
                init=openpx-STOP_K*atr; chan=lv["hi"]-CHAN_K*atr
                if last<=init: st="🔴 跌破初始止损(1.5ATR)，应离场"
                elif last<=chan: st="🔴 触发吊灯止盈(3ATR)，趋势离场"
                elif last<=lv["ma10"] and lv["ma10"]<=lv["ma10_prev"]: st="🟠 跌破日线MA10且走平/下拐，环境转弱"
                else: st=f"🟢 持有中，吊灯线 {chan:g}（在其上方 {(last/chan-1)*100:+.1f}%）"
            else:
                init=openpx+STOP_K*atr; chan=lv["lo"]+CHAN_K*atr
                if last>=init: st="🔴 涨破初始止损(1.5ATR)，应离场"
                elif last>=chan: st="🔴 触发吊灯止盈(3ATR)，趋势离场"
                elif last>=lv["ma10"] and lv["ma10"]>=lv["ma10_prev"]: st="🟠 站上日线MA10且走平/上拐，环境转弱"
                else: st=f"🟢 持有中，吊灯线 {chan:g}（在其下方 {(chan/last-1)*100:+.1f}%）"
            item.update(atr=round(atr,2),init=round(init,2),chan=round(chan,2),ma10=round(lv["ma10"],2),status=st)
        else:
            item.update(status="⚠️ 取不到该合约日K，离场位以 monitor_positions.py 分级为准")
        pos_exits.append(item)

    def cand(r):
        hand = "不足1手·不做" if r["lots"]<1 else f'{r["lots"]}手'+("(敞口封顶)" if r["capped"] else "")
        sp = f'{r["stop_px"]:.2f}' if r["stop_px"] else "-"
        return (r["name"],f'{r["last"]:g}',f'{r["atr"]:.2f}',f'{sp}',hand,f'{r["margin"]:,.0f}')
    X=[]
    X.append(f"<title>三板斧期市日报 · {D}（{wd}）</title>")
    X.append(f'<callout emoji="📌" background-color="light-blue" border-color="blue"><p><b>一句话定性：</b>市场温度<b>{temp}</b>——扫描 {len(rows)} 个主流品种，上涨 {len(up)}、下跌 {len(dn)}、平盘 {len(fl)}（涨跌比 {len(up)}:{len(dn)}）；三板斧多头共振 {len(longs)} 个、空头共振 {len(shorts)} 个、不共振 {len(flats)} 个。本报告只做客观环境筛选与风险计算，不构成投资建议。</p></callout>')

    bull_li = "".join(f"<li>{esc(e)}</li>" for e in bull_evidence)
    bear_li = "".join(f"<li>{esc(e)}</li>" for e in bear_evidence)
    X.append(f'<callout emoji="{d_emoji}" background-color="{d_bg}" border-color="{d_bd}">'
             f'<p><b>今日方向研判：{direction}</b>——{esc(d_word)}。</p>'
             f'<p><b>支持看涨（多）的证据：</b></p><ul>{bull_li}</ul>'
             f'<p><b>支持看跌（空）的证据：</b></p><ul>{bear_li}</ul>'
             f'<p>判定口径：市场温度为{temp}、多头共振净差 {net:+d}（{len(longs)}-{len(shorts)}）；净差≥2 且温度偏多方定为“偏多”，净差≤-2 且温度偏空方定为“偏空”，否则为“均衡分化”。'
             f'该方向是三周期共振与价量统计的<b>客观倾向，不是买卖指令、不预测涨跌幅</b>，不构成投资建议；是否交易、交易什么、何时平仓均由你手动决定。</p></callout>')

    X.append("<h1 seq='auto'>一、盘面总览</h1>")
    X.append(f"<p>当日主力连续合约<b>上涨 {len(up)} 个、下跌 {len(dn)} 个、平盘 {len(fl)} 个</b>，上涨占比 {up_ratio*100:.0f}%。三板斧市场温度判定为<b>{temp}</b>（多头共振 {len(longs)} vs 空头共振 {len(shorts)}）。</p>")
    X.append(tbl(["板块","品种数","涨","跌","多头共振","空头共振","板块均涨跌"],
                 [(s,n,u,d2,lo,sh,f"{a*100:+.2f}%") for (s,n,u,d2,lo,sh,a) in sec_rows]))

    X.append("<h1 seq='auto'>二、三板斧多空环境（第一板斧·选环境）</h1>")
    X.append(f"<p><b>✅ 多头共振候选（{len(longs)}）：</b>月/周/日收盘价同在 10 均线之上且周线日线向上。</p>")
    X.append(tbl(["品种","现价","ATR(14)","参考止损位","建议手数","每手保证金约"],[cand(r) for r in longs]) or "<p>无</p>")
    X.append(f"<p><b>❇️ 空头共振候选（{len(shorts)}）：</b>月/周/日收盘价同在 10 均线之下且周线日线向下。</p>")
    X.append(tbl(["品种","现价","ATR(14)","参考止损位","建议手数","每手保证金约"],[cand(r) for r in shorts]) or "<p>无</p>")
    def names(q): return "、".join(x["name"] for x in q) or "无"
    X.append("<ul>"
      f"<li><b>今日新进入多头共振：</b>{esc(names(new_long))}</li>"
      f"<li><b>今日新进入空头共振：</b>{esc(names(new_short))}</li>"
      f"<li><b>跌出多头共振（转弱）：</b>{esc(names(exit_long))}</li>"
      f"<li><b>跌出空头共振（转强）：</b>{esc(names(exit_short))}</li>"
      f"<li><b>三周期不共振、暂不做（{len(flats)}）：</b>{esc(names(flats))}</li></ul>")

    X.append("<h1 seq='auto'>三、今日入场信号（扳机 · 日线收盘确认）</h1>")
    X.append(f"<p>只在三周期共振的方向给扳机，<b>共振只是环境、不是入场，必须等触发</b>。"
             f"<b>突破</b>：收盘价突破近 {DON_N} 日通道高/低点，且当日量≥{VOL_MULT:g} 倍 20 日均量（防假突破）；"
             f"<b>回踩</b>：回踩日线 MA10（容差 {PB_TOL*100:.1f}%）后重新收阳/收阴并站回/压回 MA10。"
             "以下为客观触发名单，<b>不是买入推荐</b>；下单前仍须过入场闸门（手数≥1、止损空间足够、不追涨跌停、避开数据事件）并手动执行。</p>")
    def erow(kind, r):
        if kind=="突破":
            lvl = r["don_hi"] if r["side"]=="long" else r["don_lo"]
        else:
            lvl = r["ma10"]
        hand = "不足1手·不做" if r["lots"]<1 else f'{r["lots"]}手'+("(敞口封顶)" if r["capped"] else "")
        sp = f'{r["stop_px"]:.2f}' if r["stop_px"] else "-"
        return (kind, r["name"], f'{r["last"]:g}', f'{lvl:g}', f'{r["atr"]:.2f}', sp, hand,
                "放量" if r["vol_ok"] else "-")
    long_rows=[erow("突破",r) for r in bo_l]+[erow("回踩",r) for r in pb_l]
    short_rows=[erow("突破",r) for r in bo_s]+[erow("回踩",r) for r in pb_s]
    X.append(f"<p><b>🔴 多头扳机（{len(long_rows)}）：</b></p>")
    X.append(tbl(["扳机","品种","现价","关键位(通道/MA10)","ATR(14)","参考止损","建议手数","量能"],long_rows)
             or "<p>今日无多头触发；多头共振候选挂条件单继续等，不追高。</p>")
    X.append(f"<p><b>🟢 空头扳机（{len(short_rows)}）</b></p>")
    X.append(tbl(["扳机","品种","现价","关键位(通道/MA10)","ATR(14)","参考止损","建议手数","量能"],short_rows)
             or "<p>今日无空头触发；空头共振候选继续等，不抄底。</p>")
    n_wait=len(longs)+len(shorts)-len(long_rows)-len(short_rows)
    X.append(f"<p>其余 {max(n_wait,0)} 个共振品种今日<b>未触发扳机</b>，状态为“候选·等待”，不提前进场。</p>")

    X.append("<h1 seq='auto'>四、价量结构四象限（客观）</h1>")
    X.append(tbl(["象限","含义","代表品种（涨跌幅, 持仓变动手）"],[
        ("价涨仓增","多头主动进攻·趋势信号最强",qlist(q1)),
        ("价涨仓减","空头回补·持续性较弱",qlist(q2)),
        ("价跌仓增","空头主动打压",qlist(q3)),
        ("价跌仓减","多头止损离场",qlist(q4))]))

    X.append("<h1 seq='auto'>五、品种涨跌榜</h1>")
    X.append(tbl(["涨幅前五","收盘","涨跌幅","持仓变动"],[(r["name"],f'{r["last"]:g}',f'{r["chg"]*100:+.2f}%',f'{r["hchg"]:+.0f}') for r in top]))
    X.append(tbl(["跌幅前五","收盘","涨跌幅","持仓变动"],[(r["name"],f'{r["last"]:g}',f'{r["chg"]*100:+.2f}%',f'{r["hchg"]:+.0f}') for r in bot]))

    X.append("<h1 seq='auto'>六、我的账户持仓与离场信号（第二、三板斧）</h1>")
    if isinstance(acct,dict):
        eq=float(acct.get("availableFunds",0) or 0)+float(acct.get("margin",0) or 0)
        X.append(f"<p>账户权益约 {eq:,.0f} 元，可用 {float(acct.get('availableFunds',0) or 0):,.0f} 元，"
                 f"保证金占用 {float(acct.get('margin',0) or 0):,.0f} 元，风险度 {float(acct.get('riskRate',0) or 0):.2%}。</p>")
    gp = pos if isinstance(pos,list) else []
    if gp:
        X.append("<p>当前有持仓。浮亏分级（50%/80%/100%=关注/警告/危险）以全持仓监控脚本 monitor_positions.py 为准；"
                 "下表给三条<b>离场通道</b>的客观价位：初始止损（开仓价±1.5ATR，保命）、吊灯跟踪止盈（近 "
                 f"{CHAN_N} 日极值∓{CHAN_K:g}ATR，让利润奔跑）、日线 MA10 反转。</p>")
        if pos_exits:
            X.append(tbl(["持仓","方向","手数","最新价","初始止损1.5ATR","吊灯线3ATR","日线MA10","离场状态"],
                [(p["name"], "多" if p["direction"]=="long" else "空", p.get("qty","-"),
                  f'{p["last"]:g}',
                  f'{p["init"]:g}' if "init" in p else "-",
                  f'{p["chan"]:g}' if "chan" in p else "-",
                  f'{p["ma10"]:g}' if "ma10" in p else "-",
                  p["status"]) for p in pos_exits]))
            X.append("<p>吊灯线每日只朝盈利方向移动、收盘价跌破/涨破即趋势离场；初始止损被触及无条件走（约 -1R）。"
                     "以上为客观价位，<b>是否平仓由你手动决定，Agent 不下单</b>。</p>")
    else:
        gtxt = "黄金当前三周期不共振，按第一板斧<b>暂不做</b>" if gold and gold["side"]=="flat" else ("黄金处于多头共振" if gold and gold["side"]=="long" else "黄金处于空头共振")
        X.append(f"<p>当前<b>无持仓、无挂单</b>。{gtxt}；单笔风险预算 {EQUITY*RISK_PCT:,.0f} 元（权益 1%），"
                 "总名义敞口上限 10%（50 万元）。候选品种手数见第二部分，开仓前仍须等待入场点并入场即设止损。</p>")
    if isinstance(qt,dict) and qt.get("ready"):
        X.append(f"<p>{CONTRACT} 最新 {qt.get('latestPrice')}（{float(qt.get('changeRate',0))*100:+.2f}%），"
                 f"涨跌停区间 {qt.get('limitDown')}~{qt.get('limitUp')}（查询时刻 {qt.get('quoteTime')}）。</p>")

    X.append("<h1 seq='auto'>七、明日关注（数据驱动）</h1>")
    X.append("<ul>"
      f"<li><b>入场扳机：</b>今日突破/回踩名单见第三部分；未触发的共振候选，明日继续盯 20 日通道与 MA10 回踩，触发前不提前进。</li>"
      f"<li><b>持仓离场：</b>有持仓按第六部分的初始止损/吊灯线/MA10 执行，破线必走；重点跟踪新进入/跌出共振品种（见第二部分）。</li>"
      f"<li><b>极端波动：</b>当日涨幅居前 {esc('、'.join(x['name'] for x in top[:3]))}；跌幅居前 {esc('、'.join(x['name'] for x in bot[:3]))}，追涨杀跌风险高。</li>"
      "<li><b>价量背离：</b>价涨仓减象限多为空头回补，不宜直接当作新趋势；价跌仓增象限代表空头占优。</li>"
      "<li><b>宏观事件：</b>本报告不臆测新闻与政策；当晚/次日重要数据、央行事件、库存公布请以 "
      '<a href="https://www.jin10.com">金十数据财经日历</a> 与交易所公告为准，事件落地前控制单边敞口。</li></ul>')

    X.append("<h1 seq='auto'>八、三板斧纪律提示</h1>")
    X.append('<callout emoji="🪓" background-color="light-yellow" border-color="yellow"><ul>'
      "<li>第一板斧：只在月/周/日三周期共振的方向找机会，不共振=不做，不看杂毛、不逆势。</li>"
      "<li>共振只是“环境对了”，<b>不是入场信号</b>；必须等第三部分的扳机——突破 20 日通道（放量）或回踩 MA10 企稳，扳机不响不进场、不追单。</li>"
      "<li>第二板斧：开仓必须入场即设止损（初始 1.5×ATR，实盘结合前低/前高/均线），不可后移、不可取消。</li>"
      "<li>离场三通道：碰初始止损无条件走；吊灯线（3×ATR）收盘破=趋势离场让利润奔跑；跌破/站上 MA10 且拐头、跌出共振=环境反转主动走，不扛单。</li>"
      "<li>第三板斧：先有止损点再定手数；单笔风险≤1%、总名义敞口≤10%；不足 1 手的品种直接不做。</li>"
      "<li>Agent 只做数据筛选与计算，<b>不自动下单/平仓</b>，交易决策与执行由你手动完成。</li></ul></callout>")

    X.append("<h1 seq='auto'>九、数据口径与免责声明</h1>")
    X.append("<p>数据来源：AkShare 聚合的新浪主力连续、交易所规则（futures_rule）与 PandaAI 仿真账户，"
             "于报告生成时刻拉取，可能存在延迟与主力连续口径差异；涨跌幅按收盘价/前结算价近似计算，"
             "持仓变动为主力连续口径，与单合约/资讯商口径或有微差。月/周/日均线为各自 10 周期均线，"
             "“右上角”以价格在 MA10 之上且 MA10 上行近似；止损为 1.5×ATR、仓位为 1% 风险与 10% 名义敞口双重约束。"
             f"入场信号：突破=收盘破近 {DON_N} 日唐奇安通道且量≥{VOL_MULT:g} 倍 20 日均量，回踩=回踩 MA10（容差 {PB_TOL*100:.1f}%）企稳；"
             f"离场信号：初始止损 1.5×ATR、吊灯止盈=近 {CHAN_N} 日极值∓{CHAN_K:g}×ATR(14)、日线 MA10 反转/跌出共振。"
             "信号均为客观规则、需自行历史回测后使用，不预测涨跌幅。</p>")
    X.append("<p>【免责声明】本报告基于公开数据客观整理，仅供学习参考，<b>不构成任何投资建议</b>。期货市场有风险，投资需谨慎，"
             "投资者应结合自身风险承受能力独立决策，据此操作盈亏自负。</p>")

    xml = chr(10).join(X)
    xml_path = os.path.join(REP_DIR, f"daily_report_{D}.xml")
    open(xml_path,"w",encoding="utf-8").write(xml)
    pd.DataFrame(snap).to_csv(os.path.join(SNAP_DIR,f"regime_{D}.csv"),index=False,encoding="utf-8-sig")
    # 每品种完整证据明细（供飞书信号调研看板同步），全部为客观行情数值与规则信号
    json.dump(rows, open(os.path.join(SNAP_DIR,f"evidence_{D}.json"),"w",encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)
    meta = {"date":D,"weekday":wd,"temperature":temp,"direction":direction,
            "scanned":len(rows),"up":len(up),"down":len(dn),"flat":len(fl),
            "long_n":len(longs),"short_n":len(shorts),
            "new_long":[r["name"] for r in new_long],"new_short":[r["name"] for r in new_short],
            "long_names":long_names,"short_names":short_names,
            "bull_evidence":bull_evidence,"bear_evidence":bear_evidence,
            "breakout_long":[r["name"] for r in bo_l],"breakout_short":[r["name"] for r in bo_s],
            "pullback_long":[r["name"] for r in pb_l],"pullback_short":[r["name"] for r in pb_s],
            "position_exits":[{k:p[k] for k in ("name","direction","qty","last","init","chan","ma10","status") if k in p}
                              for p in pos_exits],
            "top":[r["name"] for r in top[:3]],"bottom":[r["name"] for r in bot[:3]],"xml_path":xml_path}
    json.dump(meta,open(os.path.join(REP_DIR,"latest_meta.json"),"w",encoding="utf-8"),ensure_ascii=False,indent=2)
    print(json.dumps(meta,ensure_ascii=False))

if __name__ == "__main__":
    main()
