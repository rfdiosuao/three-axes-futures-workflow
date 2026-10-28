#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从股票三板斧扫描 CSV 渲染自包含静态 HTML 看板（无外部依赖）。"""
import glob, json, os, sys
import pandas as pd

# 仅用于聚类展示的人工粗略归类（非官方行业分类）
THEME = {
    "长飞光纤": "算力·光通信", "源杰科技": "算力·光芯片", "兴森科技": "算力·PCB/载板",
    "太辰光": "算力·光通信", "光库科技": "算力·光器件", "长芯博创": "算力·光通信",
    "仕佳光子": "算力·光芯片", "星宸科技": "算力·AI芯片", "睿创微纳": "算力·红外/芯片",
    "深信服": "算力·云计算/安全", "超颖电子": "算力·PCB",
    "康龙化成": "医药·CXO", "凯莱英": "医药·CXO", "普洛药业": "医药·原料药",
    "迪哲医药": "医药·创新药", "华大智造": "医药·基因设备",
    "新宙邦": "新能源·锂电材料",
    "山煤国际": "价值·煤炭", "雅戈尔": "价值·服装/投资", "隆鑫通用": "价值·通用机械",
    "韵达股份": "价值·快递物流", "申能股份": "价值·电力公用", "宁沪高速": "价值·公路",
    "青岛港": "价值·港口",
}


def bar(pct, color):
    return (f'<div class="bar"><i style="width:{pct:.1f}%;background:{color}"></i>'
            f'<span>{pct:.1f}%</span></div>')


def main(csv=None):
    csv = csv or sorted(glob.glob("snapshots/stock_regime_*.csv"))[-1]
    df = pd.read_csv(csv, dtype={"code": str})
    date = df["date"].mode().iloc[0]
    n = len(df)
    nlong = int((df.side == "long").sum()); nshort = int((df.side == "short").sum())
    nflat = int((df.side == "flat").sum()); nunk = int((df.side == "unknown").sum())
    L = df[(df.side == "long") & (df.liquid == True)].sort_values("amount20_yi", ascending=False).reset_index(drop=True)

    rows = []
    for i, r in L.iterrows():
        sp = max(0, min(100, r.stop_pct / 10 * 100))  # 止损距条：10%满格
        ap = max(0, min(100, r.amount20_yi / 60 * 100))  # 成交额条：60亿满格
        lot = "—" if r.shares_per_1m == 0 else f"{int(r.shares_per_1m):,}"
        rows.append(f"""<tr>
<td class="muted">{i+1}</td><td class="code">{r.code}</td><td class="name">{r['name']}</td>
<td class="theme">{THEME.get(r['name'],'—')}</td><td class="num">{r.close:.2f}</td>
<td class="num">{r.stop_px:.2f}</td>
<td><div class="bar sm"><i style="width:{sp:.1f}%;background:#e07a5f"></i><span>{r.stop_pct:.2f}%</span></div></td>
<td><div class="bar sm"><i style="width:{ap:.1f}%;background:#3d6b9c"></i><span>{r.amount20_yi:.1f}</span></div></td>
<td class="num">{lot}</td></tr>""")

    # 聚类计数
    grp = L["name"].map(lambda x: THEME.get(x, "其他").split("·")[0]).value_counts()
    chips = "".join(f'<span class="chip {("c1" if g=="算力" else "c2" if g=="医药" else "c3")}">{g} {int(c)} 只</span>'
                    for g, c in grp.items())

    html = f"""<!DOCTYPE html>
<html lang="zh-CN" style="margin:0;padding:0;">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>三板斧股票多头共振扫描·中证500·{date}</title>
<style>
 body{{margin:0;background:#f5f6f8;color:#1f2733;font:15px/1.7 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}}
 .wrap{{max-width:1080px;margin:0 auto;padding:28px 22px 60px;}}
 h1{{font-size:24px;margin:0 0 4px;}} .sub{{color:#6b7480;font-size:13px;margin-bottom:20px;}}
 .card{{background:#fff;border:1px solid #e6e9ee;border-radius:12px;padding:18px 20px;margin:16px 0;}}
 h2{{font-size:17px;margin:0 0 12px;border-left:4px solid #3d6b9c;padding-left:9px;}}
 .temp{{display:flex;align-items:center;gap:10px;margin:8px 0;}}
 .temp .lab{{width:130px;font-size:14px;}} .bar{{flex:1;background:#eef1f5;border-radius:6px;position:relative;height:24px;}}
 .bar i{{display:block;height:100%;border-radius:6px;}} .bar span{{position:absolute;right:8px;top:0;font-size:12px;line-height:24px;color:#33404f;}}
 .bar.sm{{height:18px;min-width:120px;}} .bar.sm span{{line-height:18px;font-size:11px;color:#445;}}
 table{{border-collapse:collapse;width:100%;font-size:13.5px;}}
 th,td{{padding:7px 8px;border-bottom:1px solid #eef1f5;text-align:left;white-space:nowrap;}}
 th{{font-weight:600;background:#fafbfc;color:#56606e;position:sticky;top:0;}}
 td.num{{text-align:right;font-variant-numeric:tabular-nums;}} td.code{{color:#3d6b9c;font-weight:600;}}
 td.muted{{color:#9aa3af;}} td.theme{{color:#56606e;font-size:12.5px;}}
 .chip{{display:inline-block;border-radius:20px;padding:3px 12px;margin:0 8px 8px 0;font-size:13px;border:1px solid;}}
 .chip.c1{{background:#fdeee9;border-color:#f0c3b6;color:#b5482f;}} .chip.c2{{background:#eef4ec;border-color:#c4ddbd;color:#3f7a43;}}
 .chip.c3{{background:#eef2f7;border-color:#c7d4e2;color:#3d5a80;}}
 ul{{margin:6px 0;padding-left:22px;}} li{{margin:5px 0;}}
 .note{{background:#fff8ec;border:1px solid #f0dfb8;border-radius:10px;padding:12px 16px;font-size:13.5px;color:#6b5320;}}
 .disc{{color:#8a929e;font-size:12.5px;margin-top:10px;}}
 .kpi{{display:flex;flex-wrap:wrap;gap:12px;margin:6px 0 2px;}}
 .kpi div{{flex:1;min-width:120px;background:#f7f9fb;border-radius:10px;padding:10px 14px;}}
 .kpi b{{font-size:22px;display:block;}} .kpi .t{{font-size:12px;color:#6b7480;}}
</style></head>
<body><div class="wrap">
<h1>三板斧 · 股票多头共振扫描（中证500）</h1>
<div class="sub">数据日 {date} 收盘 · 股票池：中证500官方当期成分 500 只（成功取数 {n} 只，1 只 CDR 无行情）· 行情：AkShare 新浪链路前复权日线 · 月/周/日 MA10 共振 + 1.5×ATR14 止损 + 以损定量</div>

<div class="card">
 <h2>一、市场温度（三板斧第一斧：先看环境）</h2>
 <div class="kpi">
  <div><b style="color:#c0392b">{nlong}</b><span class="t">多头共振（{nlong/n*100:.1f}%）</span></div>
  <div><b style="color:#1e8449">{nshort}</b><span class="t">空头共振（{nshort/n*100:.1f}%）</span></div>
  <div><b style="color:#6b7480">{nflat}</b><span class="t">不共振/横盘（{nflat/n*100:.1f}%）</span></div>
  <div><b style="color:#9aa3af">{nunk}</b><span class="t">数据不足</span></div>
 </div>
 <div class="temp"><span class="lab">多头共振</span>{bar(nlong/n*100,'#c0392b')}</div>
 <div class="temp"><span class="lab">空头共振</span>{bar(nshort/n*100,'#1e8449')}</div>
 <div class="temp"><span class="lab">不共振</span>{bar(nflat/n*100,'#9aa3af')}</div>
 <div class="note">多头共振占比仅 {nlong/n*100:.1f}%、空头达 {nshort} 只——当前中盘环境偏弱。三板斧第一斧在弱市里的默认动作是<b>少做、轻仓、防守，等共振扩散</b>；“共振”是当前趋势状态，不是上涨概率。</div>
</div>

<div class="card">
 <h2>二、{len(L)} 只多头共振的结构（方向为人工粗略归类，非官方行业）</h2>
 <div>{chips}</div>
 <div class="disc">弱市中资金集中在<b>算力/光通信硬件链</b>与<b>医药</b>两条线，另有少量低波动价值/公用事业股；该聚类仅帮助理解结构，不代表行业推荐。</div>
</div>

<div class="card">
 <h2>三、多头共振观察名单（按近20日日均成交额排序）</h2>
 <table>
  <thead><tr><th>#</th><th>代码</th><th>名称</th><th>方向(粗分)</th><th>收盘</th><th>参考止损价</th><th>止损距</th><th>20日均额(亿)</th><th>每100万股数</th></tr></thead>
  <tbody>{''.join(rows)}</tbody></table>
 <div class="disc">参考止损价 = {date} 收盘 − 1.5×ATR14（多头）；实盘以你的实际买入价重算。“每100万股数”= 在总资金100万、单笔最大亏损≤1%、单票仓位≤10%、A股100股整数倍下的参考股数，按自有资金等比缩放；显示“—”表示该口径下连1手（100股）都买不起或不满足预算。</div>
</div>

<div class="card">
 <h2>四、三板斧搬到股票上怎么用（纪律口径）</h2>
 <ul>
  <li><b>选环境（第一斧）：</b>月线、周线、日线收盘价同在各自 MA10 上方，且周线、日线 MA10 都向上（共5项严格条件）才算多头共振；等于均线或走平不算。T 日收盘确认信号，<b>T+1 开盘</b>再介入，不盘中追价。</li>
  <li><b>严格止损（第二斧）：</b>买入后把止损放在“买入价 − 1.5×ATR14”，触及即走；止损距多在 2.7%–9.6%，高波动光通信股止损更宽、单笔金额风险更大，新手优先看止损距小（约 3%–6%）的标的。</li>
  <li><b>以损定量（第三斧）：</b>先定“这笔最多亏总资金的 1%”，再用止损距离反推能买多少股，同时单票仓位不超过 10%，二者取小并按 100 股取整；预算不足宁可不买。</li>
  <li><b>股票与期货的差异：</b>股票无杠杆、T+1、普通账户不能做空——所以这里<b>只给多头名单</b>，128 只空头仅用于判断环境温度，不构成做空建议；涨跌停或跳空可能让实际止损价差于参考价。</li>
  <li><b>弱市纪律：</b>当前仅 {nlong/n*100:.1f}% 个股多头共振，宜控制总仓位、单赛道（如算力链）不要重复扎堆，多只高相关股票算同一笔风险。</li>
 </ul>
</div>

<div class="card">
 <h2>五、边界与免责</h2>
 <div class="disc">本名单是基于公开价量、按固定规则机械筛选的<b>技术形态观察结果</b>，不含财务、估值、业绩、公告或行业景气验证，不预测涨跌，<b>不构成任何买卖建议或收益承诺</b>；行情数据可能存在缺失或错误，使用前请独立复核。是否买入、买哪只、买多少、何时卖出，由你本人决定并自行承担结果。程序不连接券商、不自动下单。</div>
</div>

</div></body></html>"""
    out = f"reports/stock_three_axes_{date}.html"
    os.makedirs("reports", exist_ok=True)
    open(out, "w", encoding="utf-8").write(html)
    print(json.dumps({"out": out, "date": date, "long": nlong, "short": nshort,
                      "flat": nflat, "unknown": nunk, "n": n}, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
