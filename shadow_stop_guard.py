#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
止损影子守护（SHADOW，纯只读）
================================
目的：在 PandaAI 期货【仿真】账户上验证「1.5×ATR 初始硬止损」自动值守的准确性。

铁律（本脚本不可违反）：
1. MODE 固定为 "shadow"。脚本【只调用】 panda 的只读命令：account / positions / quote，
   以及 AkShare 读取日 K。脚本中【不存在】 order / cancel / close-all / target-position /
   prepare_plan / execute_plan 等任何写操作函数或调用，因此技术上不可能下单、撤单、平仓。
2. 触及登记止损价时，只做两件事：写本地演练记录 + 发一条「影子演练·未真实下单」飞书。
3. 只监控指定的【仿真】账户（由环境变量 PANDA_ACCOUNT_ID 指定），不接实盘；只对已登记 1.5×ATR 初始止损的持仓生效；
   不自动开仓、不反手、不加仓。
4. 单日累计“本该止损”的浮动亏损达账户权益 2% 时，记录熔断演练并提示（影子不下单）。
5. 非交易时段、非交易日、无当日新成交价（ready/最新价缺失）时一律不触发、不告警。
6. 是否真实切换为自动平仓，必须由用户在影子验证后【再次书面授权】，届时另写执行器并评审。

用法：
  python3 shadow_stop_guard.py --selftest   # 离线自测（不连柜台、不发飞书）
  python3 shadow_stop_guard.py --once       # 单轮干跑（只读、只打印，不发飞书）
  python3 shadow_stop_guard.py              # 常驻守护（每 INTERVAL 秒一轮；发启停/事件飞书）
停止常驻：touch guard/STOP   （守护下一轮自检到后优雅退出），或 kill $(cat guard/shadow_guard.pid)
"""
import os, sys, json, time, math, subprocess, re, argparse
from datetime import datetime, timedelta

# 工作目录 = 脚本所在目录（clone 后即可运行）
BASE = os.path.dirname(os.path.abspath(__file__))
GUARD_DIR = os.path.join(BASE, "guard")
REGISTRY_F = os.path.join(GUARD_DIR, "stop_registry.json")
STATE_F = os.path.join(GUARD_DIR, "state.json")
TRIGGERS_F = os.path.join(GUARD_DIR, "shadow_triggers.jsonl")
LOG_F = os.path.join(GUARD_DIR, "shadow_guard.log")
PID_F = os.path.join(GUARD_DIR, "shadow_guard.pid")
STOP_SENTINEL = os.path.join(GUARD_DIR, "STOP")

# 飞书群与仿真账户：请通过环境变量提供你自己的资源 ID（见 docs/SETUP.md）
CHAT_ID = os.environ.get("LARK_CHAT_ID", "<YOUR_CHAT_ID>")
EXPECT_ACCOUNT = os.environ.get("PANDA_ACCOUNT_ID", "<YOUR_ACCOUNT_ID>")

MODE = "shadow"              # 固定影子模式；本脚本不提供 live 切换入口
INTERVAL = 180               # 交易时段轮询间隔（秒）
STOP_K = 1.5                 # 初始止损 = 开仓价 ∓ 1.5×ATR（与三板斧一致）
CIRCUIT_PCT = 0.02           # 单日累计理论止损浮亏达权益 2% -> 熔断演练
ERR_STREAK_ALERT = 3         # 交易时段连续 3 轮取数失败 -> 一条通道异常
FALLBACK_EQUITY = float(os.environ.get("ACCOUNT_EQUITY", "5000000"))
NL = chr(10)            # 消息换行（避免在字符串里写字面反斜杠 n）

os.makedirs(GUARD_DIR, exist_ok=True)

# --------------------------- 基础工具 ---------------------------
def now():
    return datetime.now()

def log(msg):
    line = f"[{now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_F, "a", encoding="utf-8") as f:
            f.write(line + NL)
    except Exception:
        pass

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def fnum(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default

def pick(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d.get(k)
    return default

def variety_code(contract):
    """c2611->C, TA701->TA, lc2701->LC, IF2611->IF（取字母部分大写）。"""
    m = re.match(r"^([A-Za-z]+)", str(contract).strip())
    return m.group(1).upper() if m else str(contract).upper()

# --------------------------- 柜台只读封装 ---------------------------
def panda(args, timeout=40):
    """只允许只读子命令。白名单兜底，防止误传写命令。"""
    allow = {"account", "positions", "quote", "whoami", "snapshot", "orders", "trades", "contract-spec", "varieties", "resolve"}
    if str(args[0]).lower() not in allow:
        raise RuntimeError(f"影子守护拒绝调用非只读命令: {args[0]}")
    try:
        r = subprocess.run(["panda"] + list(args) + ["--json"],
                           capture_output=True, text=True, timeout=timeout)
        j = json.loads(r.stdout)
        return j.get("data") if j.get("ok") else None
    except Exception:
        return None

def get_equity(acct):
    if isinstance(acct, dict):
        avail = fnum(acct.get("availableFunds"))
        margin = fnum(acct.get("margin"))
        if avail or margin:
            return avail + margin, "柜台(可用+保证金)"
        for k in ("totalEquity", "equity", "staticProfit"):
            if fnum(acct.get(k)) > 0:
                return fnum(acct[k]), f"柜台({k})"
    return FALLBACK_EQUITY, "默认"

def get_quote(code, contract_name=None):
    """依次用 实际合约 / 品种码 / 合约名称 取最新快照；返回 dict 或 None。"""
    cand = [code, variety_code(code)]
    if contract_name:
        cand.append(contract_name)
    seen = set()
    for c in cand:
        if not c or c in seen:
            continue
        seen.add(c)
        q = panda(["quote", c])
        if isinstance(q, dict) and fnum(q.get("latestPrice")) > 0:
            return q
    return None

def quote_is_fresh(q, today_str):
    """有最新价且行情日期=今天，才算有当日新成交价。"""
    if not isinstance(q, dict):
        return False
    last = fnum(q.get("latestPrice"))
    if last <= 0:
        return False
    qt = str(q.get("quoteTime", ""))
    # 形如 "2026-10-09 94926" / "2026-10-09 09:49:26"
    m = re.match(r"([0-9]{4}-[0-9]{2}-[0-9]{2})", qt)
    if m and m.group(1) != today_str:
        return False
    return True

# --------------------------- ATR（AkShare 只读，与日报同口径） ---------------------------
_ak = None
def ak():
    global _ak
    if _ak is None:
        import akshare as _akshare
        _ak = _akshare
    return _ak

def contract_atr(code):
    """返回 (atr14, 来源)。优先实际持仓合约 futures_zh_daily_sina（英文列，settle 优先，
    与 daily_report.contract_levels 同口径）；若该合约无数据/历史不足（如远月新挂），
    回退到同品种主连 futures_main_sina（中文列，与日报扫描同源）。都失败返回 (None, 来源)。"""
    import pandas as pd
    # 1) 具体合约
    try:
        h = ak().futures_zh_daily_sina(symbol=code)
        if h is not None and len(h) >= 16:
            h = h.sort_values("date").reset_index(drop=True)
            pc = h["settle"].where(h["settle"].notna(), h["close"]).shift(1)
            tr = pd.concat([h["high"] - h["low"],
                            (h["high"] - pc).abs(),
                            (h["low"] - pc).abs()], axis=1).max(axis=1)
            return float(tr.rolling(14).mean().iloc[-1]), f"具体合约{code}"
    except Exception:
        pass
    # 2) 回退主连（品种码 + '0'，如 TA701->TA0、c2611->C0）
    try:
        sym = variety_code(code) + "0"
        d = ak().futures_main_sina(symbol=sym)
        if d is not None and len(d) >= 16:
            d = d.sort_values("日期").reset_index(drop=True)
            pc = d["收盘价"].shift(1)
            tr = pd.concat([d["最高价"] - d["最低价"],
                            (d["最高价"] - pc).abs(),
                            (d["最低价"] - pc).abs()], axis=1).max(axis=1)
            return float(tr.rolling(14).mean().iloc[-1]), f"主连{sym}回退"
    except Exception:
        pass
    return None, "取不到"

# --------------------------- 交易时段 ---------------------------
def in_session(t=None):
    """日盘 09:00-11:30、13:30-15:00；夜盘 21:00-23:00（周一至周五）。节假日由“无当日新价”兜底。"""
    t = t or now()
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    return (9 * 60 <= hm <= 11 * 60 + 30) or (13 * 60 + 30 <= hm <= 15 * 60) or (21 * 60 <= hm <= 23 * 60)

# --------------------------- 纯判定逻辑（便于离线自测） ---------------------------
def stop_triggered(direction, last, stop):
    if direction == "long":
        return last <= stop
    return last >= stop

def float_pnl(direction, openpx, last, mult, qty):
    """触发时浮动盈亏（负=亏损）。"""
    diff = (last - openpx) if direction == "long" else (openpx - last)
    return diff * mult * qty

def planned_stop_loss(openpx, stop, mult, qty):
    """按登记止损价离场的计划亏损（正数=亏）。"""
    return abs(openpx - stop) * mult * qty

# --------------------------- 飞书（仅事件通知） ---------------------------
def send_feishu(markdown, idem):
    try:
        r = subprocess.run(["lark-cli", "im", "+messages-send", "--as", "user",
                            "--chat-id", CHAT_ID, "--markdown", markdown,
                            "--idempotency-key", idem],
                           capture_output=True, text=True, timeout=40)
        out = r.stdout or ""
        try:
            j = json.loads(out)
            return (j.get("data") or {}).get("message_id")
        except Exception:
            return None
    except Exception as e:
        log(f"飞书发送失败: {e}")
        return None

# --------------------------- 单轮扫描 ---------------------------
def run_once(notify):
    t = now()
    today = t.strftime("%Y-%m-%d")
    registry = load_json(REGISTRY_F, {})
    state = load_json(STATE_F, {})
    if state.get("day") != today:
        state.update(day=today, day_loss=0.0, circuit=False, err_streak=0)

    acct = panda(["account"])
    positions = panda(["positions"])
    if not isinstance(acct, dict) or not isinstance(positions, list):
        state["err_streak"] = int(state.get("err_streak", 0)) + 1
        save_json(STATE_F, state)
        log(f"取数失败（账户/持仓），连续 {state['err_streak']} 轮")
        if notify and in_session(t) and state["err_streak"] >= ERR_STREAK_ALERT:
            idem = f"shadow-err-{t.strftime('%m%d%H')}"
            if state.get("last_err_idem") != idem:
                send_feishu(f"【影子守护·通道异常】{NL}- 交易时段连续多轮取不到账户/持仓（可能 panda 登录态失效或网络异常）。{NL}"
                            f"- 影子模式为纯只读、不会下单；请人工核对通道，必要时由你本人重新 `panda login` 授权。{NL}"
                            f"- 本消息为通道健康提示，非买卖建议。", idem)
                state["last_err_idem"] = idem
                save_json(STATE_F, state)
        return {"ok": False, "err_streak": state["err_streak"]}
    state["err_streak"] = 0

    equity, equity_src = get_equity(acct)
    acct_id = str(acct.get("accountId", "")).strip()
    if acct_id and acct_id != EXPECT_ACCOUNT:
        log(f"⚠️ 当前账户 {acct_id} 非预期仿真账户 {EXPECT_ACCOUNT}，跳过以防误监控其他账户。")
        if notify:
            send_feishu(f"【影子守护·账户不符】当前账户为 {acct_id}，非预期仿真账户 {EXPECT_ACCOUNT}，本轮已跳过、未做任何操作。",
                        f"shadow-acct-{t.strftime('%m%d%H')}")
        return {"ok": True, "skipped": "account_mismatch"}

    held = {}
    events, triggers = [], []
    for p in positions:
        qty = fnum(pick(p, "position", "volume", "totalVolume", "total"))
        if qty <= 0:
            continue
        code = str(p.get("contractCode", "")).split(".")[0]
        if not code:
            continue
        direction = str(pick(p, "direction", "side", "posSide", default="long")).lower()
        if direction not in ("long", "short"):
            direction = "short" if str(pick(p, "directionText", default="")).startswith("空") else "long"
        name = pick(p, "contractName", "name", default=code)
        openpx = fnum(pick(p, "openPrice", "avgPrice", "costPrice"))
        mult = fnum(pick(p, "roundLot", "multiplier", "contractMultiplier"), 1.0) or 1.0
        held[f"{code}|{direction}"] = True
        key = f"{code}|{direction}"
        reg = registry.get(key)

        # 新持仓 -> 登记 1.5ATR 初始止损
        if reg is None:
            atr, atr_src = contract_atr(code)
            if not atr or openpx <= 0:
                log(f"新持仓 {code} 暂无法取 ATR/开仓价，下轮重试登记")
                continue
            stop = openpx - STOP_K * atr if direction == "long" else openpx + STOP_K * atr
            reg = dict(code=code, name=name, direction=direction, qty=qty, openpx=openpx,
                       mult=mult, atr=round(atr, 4), atr_src=atr_src, stop=round(stop, 4),
                       reg_time=t.strftime("%Y-%m-%d %H:%M:%S"), fired=False, closed=False)
            registry[key] = reg
            events.append(("register", key, reg))
            log(f"登记 {name}({code}) {direction} {qty}手 开仓{openpx} ATR{atr:.2f} 止损{stop:.2f}")
        else:
            # 仓位/均价变动（加减仓）-> 重新登记并留痕
            if float(reg.get("qty", 0)) != qty or abs(float(reg.get("openpx", 0)) - openpx) > max(1e-9, openpx * 1e-4):
                atr, atr_src = contract_atr(code)
                if not atr:
                    atr, atr_src = float(reg.get("atr", 0)), "沿用原ATR"
                stop = openpx - STOP_K * atr if direction == "long" else openpx + STOP_K * atr
                reg.update(qty=qty, openpx=openpx, mult=mult, atr=round(atr, 4), atr_src=atr_src,
                           stop=round(stop, 4),
                           reg_time=t.strftime("%Y-%m-%d %H:%M:%S"), fired=False)
                events.append(("change", key, dict(reg)))
                log(f"仓位变动 {name}({code}) -> {qty}手 开仓{openpx} 重登记止损{stop:.2f}")

        # 取最新价
        q = get_quote(code, name)
        fresh = quote_is_fresh(q, today)
        last = fnum(q.get("latestPrice")) if fresh else fnum(p.get("lastPrice"))
        reg["last"] = last
        reg["fresh"] = fresh

        if not fresh and in_session(t):
            log(f"{code} 无当日新成交价，本轮不判定（不触发）")
            continue

        # 止损判定
        if (not reg.get("fired")) and stop_triggered(direction, last, float(reg["stop"])):
            plan_loss = planned_stop_loss(openpx, float(reg["stop"]), mult, qty)
            live_pnl = float_pnl(direction, openpx, last, mult, qty)
            trig = dict(time=t.strftime("%Y-%m-%d %H:%M:%S"), code=code, name=name,
                        direction=direction, qty=qty, openpx=openpx, atr=reg["atr"],
                        stop=reg["stop"], last=last, mult=mult,
                        planned_loss=round(plan_loss, 2), live_pnl=round(live_pnl, 2),
                        equity=round(equity, 2), live_pnl_pct=round(live_pnl / equity * 100, 3))
            triggers.append(trig)
            try:
                with open(TRIGGERS_F, "a", encoding="utf-8") as f:
                    f.write(json.dumps(trig, ensure_ascii=False) + NL)
            except Exception:
                pass
            reg["fired"] = True
            reg["fired_time"] = trig["time"]
            state["day_loss"] = float(state.get("day_loss", 0)) + min(0, live_pnl)
            events.append(("trigger", key, trig))
            log(f"【影子触发】{name}({code}) {direction} 最新{last} 触及止损{reg['stop']} 浮亏{live_pnl:.0f}")
        elif reg.get("fired"):
            log(f"{code} 仍处止损外侧（已记录过，不重复告警）")

    # 已平仓持仓归档（只记日志，不刷屏）
    for key, reg in list(registry.items()):
        if not reg.get("closed") and key not in held:
            reg["closed"] = True
            reg["closed_time"] = t.strftime("%Y-%m-%d %H:%M:%S")
            log(f"持仓 {reg.get('name')}({reg.get('code')}) 已消失，监控解除（归档）")
    # 清理已关闭超过 1 天的登记，保持文件精简
    registry = {k: v for k, v in registry.items() if not v.get("closed")}

    # 熔断演练
    if (not state.get("circuit")) and abs(float(state.get("day_loss", 0))) >= equity * CIRCUIT_PCT:
        state["circuit"] = True
        events.append(("circuit", None, dict(day_loss=state["day_loss"], equity=equity)))
        log(f"【熔断演练】当日累计理论止损浮亏 {state['day_loss']:.0f} ≥ 权益 {CIRCUIT_PCT*100:.0f}%")

    save_json(REGISTRY_F, registry)
    save_json(STATE_F, state)

    # 事件通知（守护模式才发飞书；--once 干跑不发）
    if notify:
        for kind, key, data in events:
            if kind == "register":
                d = data
                send_feishu(
                    f"【影子守护·登记止损｜未下单】{NL}"
                    f"- {d['name']}（{d['code']}）{('多头' if d['direction']=='long' else '空头')} {d['qty']:g} 手{NL}"
                    f"- 开仓价 {d['openpx']:g}，ATR14 {d['atr']:g}（{d.get('atr_src','')}），登记初始止损（1.5ATR）**{d['stop']:g}**{NL}"
                    f"- 影子模式只读监控、不会下单；破位由你本人手动决定，不构成投资建议。",
                    f"shadow-reg-{d['code']}-{t.strftime('%m%d%H')}")
            elif kind == "change":
                d = data
                send_feishu(
                    f"【影子守护·仓位变动重登记｜未下单】{NL}"
                    f"- {d['name']}（{d['code']}）{('多头' if d['direction']=='long' else '空头')} 现为 {d['qty']:g} 手、开仓价 {d['openpx']:g}{NL}"
                    f"- 重新登记初始止损 **{d['stop']:g}**（ATR {d['atr']:g}）。影子只读、不下单。",
                    f"shadow-chg-{d['code']}-{t.strftime('%m%d%H')}")
            elif kind == "trigger":
                d = data
                arrow = "跌破" if d["direction"] == "long" else "涨破"
                send_feishu(
                    f"## 【影子演练·未真实下单】止损本该触发{NL}"
                    f"- {d['name']}（{d['code']}）{('多头' if d['direction']=='long' else '空头')} {d['qty']:g} 手{NL}"
                    f"- 开仓价 {d['openpx']:g}，登记止损 {d['stop']:g}，最新价 **{d['last']:g}**（已{arrow}止损）{NL}"
                    f"- 按止损离场计划亏损约 {d['planned_loss']:,.0f} 元；触发时浮动盈亏 {d['live_pnl']:,.0f} 元（占权益 {d['live_pnl_pct']}%）{NL}"
                    f"- 时间 {d['time']}{NL}"
                    f"- ⚠️ 影子模式【只记录、不下单】。真实自动平仓需你二次书面授权后才会启用；当前是否离场请你手动决定，不构成投资建议。",
                    f"shadow-trig-{d['code']}-{t.strftime('%m%d%H%M')}")
            elif kind == "circuit":
                d = data
                send_feishu(
                    f"【影子演练·熔断｜未下单】{NL}- 当日累计“本该止损”的浮动亏损约 {d['day_loss']:,.0f} 元，"
                    f"已达账户权益（{d['equity']:,.0f}）的 {CIRCUIT_PCT*100:.0f}%。{NL}"
                    f"- 若为真实模式此刻将停机并交由人工接管；影子模式仅记录、不下单。请人工核对总风险，不构成投资建议。",
                    f"shadow-circuit-{today}")

    active = [v for v in registry.values()]
    return {"ok": True, "equity": round(equity, 2), "equity_src": equity_src,
            "account": acct_id, "positions": len(held), "active_registry": len(active),
            "triggers": triggers, "in_session": in_session(t)}

# --------------------------- 离线自测 ---------------------------
def selftest():
    cases = []
    def ck(name, cond):
        cases.append((name, bool(cond)))
    ck("多头: 价在止损上方不触发", stop_triggered("long", 100, 98) is False)
    ck("多头: 价跌破止损触发", stop_triggered("long", 97.9, 98) is True)
    ck("空头: 价在止损下方不触发", stop_triggered("short", 100, 102) is False)
    ck("空头: 价涨破止损触发", stop_triggered("short", 102.1, 102) is True)
    ck("多头浮亏为负", float_pnl("long", 100, 97, 10, 2) == (97 - 100) * 10 * 2)
    ck("计划亏损=|开仓-止损|*乘数*手数", planned_stop_loss(100, 97, 10, 2) == 3 * 10 * 2)
    ck("非交易时段(周六)", in_session(datetime(2026, 10, 10, 10, 0)) is False)
    ck("日盘时段", in_session(datetime(2026, 10, 9, 10, 0)) is True)
    ck("午间休市非时段", in_session(datetime(2026, 10, 9, 12, 0)) is False)
    ck("夜盘时段", in_session(datetime(2026, 10, 9, 21, 30)) is True)
    ck("行情日期非今天=不新鲜", quote_is_fresh({"latestPrice": 100, "quoteTime": "2026-10-08 210000"}, "2026-10-09") is False)
    ck("行情日期今天=新鲜", quote_is_fresh({"latestPrice": 100, "quoteTime": "2026-10-09 94926"}, "2026-10-09") is True)
    ck("无最新价=不新鲜", quote_is_fresh({"latestPrice": 0, "quoteTime": "2026-10-09 94926"}, "2026-10-09") is False)
    ck("品种码 c2611->C", variety_code("c2611") == "C")
    ck("品种码 TA701->TA", variety_code("TA701") == "TA")
    # 写命令白名单防护
    try:
        panda(["order"])
        ck("写命令 order 被拒绝", False)
    except RuntimeError:
        ck("写命令 order 被拒绝", True)
    print("===== 影子守护离线自测 =====")
    allok = True
    for n, ok in cases:
        print(f"[{'PASS' if ok else 'FAIL'}] {n}")
        allok = allok and ok
    print("结果:", "全部通过" if allok else "存在失败")
    return 0 if allok else 1

# --------------------------- 常驻守护 ---------------------------
def daemon():
    assert MODE == "shadow", "本脚本仅支持 shadow 模式"
    if os.path.exists(STOP_SENTINEL):
        log("检测到 STOP 停用标志，守护拒绝启动；如需恢复，请先删除 guard/STOP 再启动。")
        try:
            if os.path.exists(PID_F):
                os.remove(PID_F)
        except Exception:
            pass
        return
    with open(PID_F, "w") as f:
        f.write(str(os.getpid()))
    log("=" * 60)
    log(f"止损影子守护启动 pid={os.getpid()} 间隔{INTERVAL}s 模式={MODE}（纯只读、不下单）")
    send_feishu(f"【止损影子守护已启动｜只读·不下单】{NL}"
                f"- 模式：SHADOW（演练），每 {INTERVAL} 秒扫描一次，仅监控仿真账户 {EXPECT_ACCOUNT}{NL}"
                f"- 新持仓自动按 1.5×ATR 登记初始止损；破位只发『本该止损』演练消息，不做任何委托{NL}"
                f"- 单日累计理论止损浮亏达权益 {CIRCUIT_PCT*100:.0f}% 记录熔断；非交易时段/无当日新价不动作{NL}"
                f"- 不自动开仓/反手/加仓；是否离场始终由你本人手动决定，不构成投资建议{NL}"
                f"- 停止：在 guard/ 目录放置 STOP 文件，或 kill 该守护进程。",
                f"shadow-start-{now().strftime('%m%d')}")
    try:
        while True:
            if os.path.exists(STOP_SENTINEL):
                log("检测到 STOP 哨兵，优雅退出（保留 STOP 标志，保活任务不会自动拉起；恢复需先删除 guard/STOP）。")
                send_feishu("【止损影子守护已停止】监控已按指令关闭（只读、全程未下单）。"
                            "如需恢复，删除 guard/STOP 后重新启动守护。",
                            f"shadow-stop-{now().strftime('%m%d%H')}")
                break
            try:
                if in_session():
                    summ = run_once(notify=True)
                    log(f"轮询完成: {json.dumps({k:v for k,v in summ.items() if k!='triggers'}, ensure_ascii=False)}"
                        + (f"；触发 {len(summ.get('triggers', []))}" if summ.get('ok') else ""))
                    time.sleep(INTERVAL)
                else:
                    # 非交易时段：轻睡，每 5 分钟醒来看一次 STOP/时段
                    time.sleep(300)
            except Exception as e:
                log(f"轮询异常(已捕获，继续): {e}")
                time.sleep(60)
    finally:
        try:
            if os.path.exists(PID_F):
                os.remove(PID_F)
        except Exception:
            pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="单轮干跑（只读、只打印，不发飞书）")
    ap.add_argument("--selftest", action="store_true", help="离线自测")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(selftest())
    if args.once:
        log("单次干跑（--once），不发送飞书。")
        print(json.dumps(run_once(notify=False), ensure_ascii=False, indent=2))
        return
    daemon()

if __name__ == "__main__":
    main()
