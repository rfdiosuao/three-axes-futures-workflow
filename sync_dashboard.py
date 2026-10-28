#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三板斧品种信号调研看板 → 飞书多维表格同步器（只读行情、只写看板，绝不下单）。
输入：daily_report.py 产出的 snapshots/evidence_<date>.json（每品种一条完整证据）。
输出：飞书 Base「三板斧品种信号看板」表，每品种每交易日一行（幂等：同日同品种存在则更新）。
证据分级（借鉴「活投研框架」）：行情数值=事实(可核验)；共振/扳机=历史规则统计(相关≠因果)；
不写任何研究假说/新闻/概率。慢锚(季节)=月/周 MA10，快变量(天气)=日线 MA10 与扳机。
"""
import os, json, glob, subprocess, argparse, sys

# 工作目录 = 脚本所在目录（clone 后即可运行）
BASE = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(BASE, "snapshots")
REP_DIR = os.path.join(BASE, "reports")
# 飞书多维表格资源：请通过环境变量提供你自己的 Base/表 ID（见 docs/SETUP.md）
BASE_TOKEN = os.environ.get("LARK_BASE_TOKEN", "<YOUR_BASE_TOKEN>")
TABLE_ID = os.environ.get("LARK_DASHBOARD_TABLE_ID", "<YOUR_TABLE_ID>")
EVIDENCE_GRADE = "行情数值=事实(可核验)；共振/扳机=历史规则统计(相关≠因果)；本表不写研究假说"
SIDE_MAP = {"long": "多头共振", "short": "空头共振", "flat": "不共振"}


def lark(args, json_path=None):
    cmd = ["lark-cli", "base"] + args + ["--as", "user", "--base-token", BASE_TOKEN,
                                         "--table-id", TABLE_ID, "--format", "json"]
    if json_path:
        cmd += ["--json", "@" + json_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=BASE)
    try:
        j = json.loads(r.stdout)
    except Exception:
        raise RuntimeError(f"lark-cli 非JSON输出: {r.stdout[:300]} {r.stderr[:300]}")
    if not j.get("ok"):
        raise RuntimeError(f"lark-cli 失败: {json.dumps(j.get('error', j), ensure_ascii=False)[:400]}")
    return j.get("data", {})


def latest_date():
    fs = sorted(glob.glob(os.path.join(SNAP_DIR, "evidence_*.json")))
    if not fs:
        sys.exit("无 evidence_*.json，请先运行 daily_report.py")
    return os.path.basename(fs[-1])[len("evidence_"):-len(".json")]


def num(v, nd=2):
    try:
        if v is None:
            return None
        return round(float(v), nd)
    except Exception:
        return None


def to_fields(r):
    """evidence 行 → 飞书字段 map。"""
    date = r["date"]
    f = {
        "记录": f"{date} {r['name']}",
        "交易日": f"{date} 12:00:00",
        "品种": r["name"],
        "代码": r["sym"],
        "板块": r["sector"],
        "共振方向": SIDE_MAP.get(r["side"], "不共振"),
        "季节慢锚(月周)": r["season"],
        "天气快变量(日线)": r["weather"],
        "状态变化": r["change"],
        "入场扳机": r["trig"],
        "价量象限": r["quad"],
        "MA10方向": r["ma_dir"],
        "放量确认": bool(r.get("vol_ok")),
        "现价": num(r["last"]),
        "涨跌幅%": num(r["chg"] * 100),
        "ATR14": num(r["atr"]),
        "日线MA10": num(r["ma10"]),
        "20日通道上沿": num(r["don_hi"]),
        "20日通道下沿": num(r["don_lo"]),
        "持仓量": num(r.get("hold"), 0),
        "持仓变动": num(r.get("hchg"), 0),
        "建议手数": int(r["lots"]) if r.get("lots") is not None else 0,
        "敞口封顶": bool(r.get("capped")),
        "反证·失效条件": r["invalid"],
        "分层结论": r["conclusion"],
        "证据等级": EVIDENCE_GRADE,
    }
    if r.get("stop_px"):
        f["参考止损1.5ATR"] = num(r["stop_px"])
    if r.get("margin"):
        f["每手保证金"] = num(r["margin"], 0)
    return f


def existing_map():
    """读全表，返回 {主字段值: record_id}。"""
    out, offset = {}, 0
    while True:
        d = lark(["+record-list", "--limit", "200", "--offset", str(offset)])
        fields = d.get("fields", [])
        rows = d.get("data", [])
        ids = d.get("record_id_list", [])
        try:
            ki = fields.index("记录")
        except ValueError:
            ki = 0
        for rid, row in zip(ids, rows):
            if isinstance(row, list) and ki < len(row):
                key = row[ki]
                if isinstance(key, list):  # 文本读回可能是片段数组
                    key = "".join(str(x.get("text", x)) if isinstance(x, dict) else str(x) for x in key)
                out[str(key)] = rid
        if not d.get("has_more"):
            break
        offset += len(rows)
        if not rows:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    args = ap.parse_args()
    date = args.date or latest_date()
    ev_path = os.path.join(SNAP_DIR, f"evidence_{date}.json")
    rows = json.load(open(ev_path, encoding="utf-8"))
    print(f"同步交易日 {date}，证据 {len(rows)} 条", flush=True)

    exist = existing_map()
    creates, updates = [], {}
    for r in rows:
        f = to_fields(r)
        key = f["记录"]
        if key in exist:
            updates[exist[key]] = f
        else:
            creates.append(f)
    print(f"新增 {len(creates)} 条，更新 {len(updates)} 条", flush=True)

    if creates:
        for i in range(0, len(creates), 200):
            p = os.path.join(REP_DIR, "_dash_create.json")
            json.dump({"create_records": creates[i:i + 200]}, open(p, "w", encoding="utf-8"), ensure_ascii=False)
            lark(["+record-batch-create"], p)
    if updates:
        for i in range(0, len(updates), 200):
            p = os.path.join(REP_DIR, "_dash_update.json")
            items = dict(list(updates.items())[i:i + 200])
            json.dump({"update_records": items}, open(p, "w", encoding="utf-8"), ensure_ascii=False)
            lark(["+record-batch-update"], p)

    # 回读确认当天行数
    after = existing_map()
    n = sum(1 for k in after if k.startswith(f"{date} "))
    print(json.dumps({"date": date, "evidence": len(rows), "created": len(creates),
                      "updated": len(updates), "rows_for_date": n}, ensure_ascii=False))
    if n != len(rows):
        sys.exit(f"回读数量不符：期望 {len(rows)}，实际 {n}")


if __name__ == "__main__":
    main()
