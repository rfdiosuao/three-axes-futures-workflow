"""Pure calculations. Missing evidence is never silently replaced by a fact."""
import math
import pandas as pd


def number(value, positive=False):
    try:
        value = float(value)
        return value if math.isfinite(value) and (not positive or value > 0) else None
    except (ValueError, TypeError):
        return None


def prepare_history(frame):
    required = ["日期", "开盘价", "最高价", "最低价", "收盘价"]
    if not set(required).issubset(frame.columns) or frame.empty:
        raise ValueError("缺少有效 OHLC 日线字段")
    d = frame.copy()
    d["日期"] = pd.to_datetime(d["日期"], errors="coerce")
    if d["日期"].isna().any() or d["日期"].duplicated().any():
        raise ValueError("日期无效或重复")
    d = d.sort_values("日期").reset_index(drop=True)
    for col in required[1:]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
        if d[col].map(lambda v: number(v, positive=True) is None).any():
            raise ValueError(f"{col} 存在非正或非有限值")
    if ((d["最高价"] < d[["开盘价", "收盘价", "最低价"]].max(axis=1)) |
        (d["最低价"] > d[["开盘价", "收盘价", "最高价"]].min(axis=1))).any():
        raise ValueError("OHLC 高低关系异常")
    return d


def signal(d):
    d = prepare_history(d)
    close = d.set_index("日期")["收盘价"]
    result = {"includes_partial_periods": True}
    for name, series in (("monthly", close.resample("ME").last().dropna()),
                         ("weekly", close.resample("W-FRI").last().dropna()),
                         ("daily", close)):
        ma = series.rolling(10).mean()
        if len(series) < (10 if name == "monthly" else 11):
            result[name] = None
        else:
            result[name] = {"close": float(series.iloc[-1]), "ma": float(ma.iloc[-1]),
                            "ma_prev": number(ma.iloc[-2])}
    result["side"] = "unknown"
    if any(result[k] is None for k in ("monthly", "weekly", "daily")):
        return result
    m, w, day = (result[k] for k in ("monthly", "weekly", "daily"))
    long = m["close"] > m["ma"] and all(x["close"] > x["ma"] > x["ma_prev"] for x in (w, day))
    short = m["close"] < m["ma"] and all(x["close"] < x["ma"] < x["ma_prev"] for x in (w, day))
    result["side"] = "long" if long else "short" if short else "flat"
    return result


def atr14(d):
    # Require 15 closes so every one of the last 14 TRs has a previous close.
    if len(d) < 15:
        return None
    pc = d["收盘价"].shift(1)
    tr = pd.concat([d["最高价"]-d["最低价"], (d["最高价"]-pc).abs(),
                    (d["最低价"]-pc).abs()], axis=1).max(axis=1)
    return number(tr.tail(14).mean(), positive=True)


def position_size(price, stop_distance, multiplier, equity, risk_pct=.01, exposure_pct=.10):
    values = [number(x, positive=True) for x in (price, stop_distance, multiplier, equity, risk_pct, exposure_pct)]
    if any(x is None for x in values):
        return dict(lots=None, risk_lots=None, exposure_lots=None, binding="unknown",
                    risk_per_lot=None, notional_per_lot=None)
    price, sd, mult, equity, risk_pct, cap = values
    risk, exposure = math.floor(equity*risk_pct/(sd*mult)), math.floor(equity*cap/(price*mult))
    return dict(lots=min(risk, exposure), risk_lots=risk, exposure_lots=exposure,
                binding="risk" if risk < exposure else "exposure" if exposure < risk else "both",
                risk_per_lot=sd*mult, notional_per_lot=price*mult)


def build_dataset(histories, rules, date, watch, sectors, equity):
    target = pd.Timestamp(date).normalize()
    data = {"date": target.date().isoformat(), "rows": [], "issues": [], "expected": len(watch),
            "equity": equity, "mode": "raw", "independently_verified": False}
    for sym, name in watch.items():
        try:
            if sym not in histories:
                raise ValueError("未取得日线")
            d = histories[sym].copy()
            d['日期'] = pd.to_datetime(d['日期'], errors='coerce')
            if d['日期'].isna().any(): raise ValueError('日期无法解析')
            # Only this declared 18-month window contributes to current and prior MA10/ATR.
            d = prepare_history(d[(d['日期'] <= target) & (d['日期'] >= target-pd.DateOffset(months=18))])
            if d.empty or d["日期"].iloc[-1].normalize() != target:
                raise ValueError("目标交易日无数据，未混入旧行情")
            last = float(d["收盘价"].iloc[-1]); now = signal(d)
            prev = signal(d.iloc[:-1])["side"] if len(d) > 1 else "unknown"
            settlement = number(d["动态结算价"].iloc[-2], True) if "动态结算价" in d and len(d)>1 else None
            hold = number(d["持仓量"].iloc[-1]) if "持仓量" in d else None
            hold_prev = number(d["持仓量"].iloc[-2]) if "持仓量" in d and len(d)>1 else None
            if hold is not None and hold < 0: hold = None
            if hold_prev is not None and hold_prev < 0: hold_prev = None
            hchg = hold-hold_prev if hold is not None and hold_prev is not None else None
            chg = last/settlement-1 if settlement else None
            atr = atr14(d); sd = 1.5*atr if atr else None
            ri = rules.get(sym[:-1].upper(), {})
            mult = number(ri.get("合约乘数"), True)
            rate = number(ri.get("交易保证金比例"), True)
            size = position_size(last, sd, mult, equity)
            quadrant = "数据不足"
            if chg is not None and hchg is not None:
                quadrant = "持仓不变" if hchg == 0 else "价格持平" if chg == 0 else (
                    ("价涨" if chg > 0 else "价跌") + ("仓增" if hchg > 0 else "仓减"))
            notes = []
            if settlement is None: notes.append("前结算价缺失，不参与涨跌统计")
            if hchg is None: notes.append("持仓变化未知")
            if mult is None: notes.append("合约乘数缺失，停止仓位测算")
            if rate is None or rate>100: notes.append("保证金比例缺失或无效，不输出保证金估算")
            if now["side"] == "unknown": notes.append("历史周期不足，不作共振判断")
            data["rows"].append(dict(symbol=sym,name=name,sector=sectors.get(sym[:-1],"其他"),
                date=data["date"],last=last,chg=chg,atr=atr,side=now["side"],side_prev=prev,
                evidence=now,hold_change=hchg,quadrant=quadrant,mult=mult,margin_rate=rate,
                margin=last*mult*rate/100 if mult and rate and rate <= 100 else None,
                stop_distance=sd,stop_px=last-sd if sd and now["side"]=="long" else
                last+sd if sd and now["side"]=="short" else None,notes=notes,**size))
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            data["issues"].append({"symbol": sym, "reason": str(exc)})
    return data
