# 三板斧期货工作流（Three-Axes Futures Workflow）

> 一套跑在期货仿真账户（虚拟资金、真实行情）上的「三板斧」**主观趋势跟踪**工作流：
> 用免费数据自动完成 **三周期共振选环境 → 入场扳机扫描 → 以损定量算仓位 → 持仓风险监控 → 节假日前压力测试 → 影子止损演练**，并把结果推送到飞书（群消息 / 云文档 / 多维表格看板）。
>
> **全程只读：绝不自动下单、平仓、撤单。所有方向与信号都是客观规则结果，不构成投资建议；是否交易、何时平仓一律由人手动决定。**

## 特性

- **免费数据**：行情全部来自 [AkShare](https://akshare.akfamily.xyz/)（新浪/腾讯/东财等公开源），无需付费数据接口。
- **规则确定、可复算**：月/周/日三周期 MA10 共振、20 日唐奇安通道、ATR、1.5 倍止损、吊灯跟踪止盈，全部是经典趋势跟踪惯例，无主观打分。
- **双向证据**：日报同时给出「支持看涨」与「支持看跌」的证据，不预设立场。
- **完整闭环**：方向研判 → 入场扳机 → 持仓离场预警 → 看板沉淀 → 群推送，配套 11 个定时任务提示词。
- **安全演练**：内置纯只读「影子止损守护」，破位只发「影子演练·未真实下单」，用于在不接报单的情况下验证止损逻辑。

## 工作流组成

| 脚本 | 职责 |
|---|---|
| `daily_report.py` | 盘后扫描约 51 个品种：方向研判（多/空双向证据）、三周期共振、入场扳机、持仓离场、价量四象限、ATR/止损/建议手数；产出飞书文档 XML 与 `snapshots/evidence_<date>.json` |
| `sync_dashboard.py` | 把 `evidence_<date>.json` 幂等同步到飞书多维表格「品种信号看板」（28 字段、多视图、跨日趋势仪表盘） |
| `monitor_positions.py` | 交易时段对全部持仓做风险分级（关注/警告/危险）、逼近涨跌停；`--brief` 输出收盘小结 |
| `holiday_exit_check.py` | 长假前最后交易日的持仓离场压力测试（封板 / 3×ATR 跳空 / 提保情景，绿/黄/红灯号） |
| `shadow_stop_guard.py` | 纯只读影子止损守护：1.5×ATR 登记、破位只发演练；`--selftest` 离线自测、`--once` 单轮干跑 |
| `guard/keepalive_shadow.sh` | 影子守护每小时保活（STOP 哨兵 + pid 身份校验） |
| `screen_three_axes.py` | 三板斧共振/信号命令行扫描，输出 Markdown 名单 |
| `monitor_gold.py` | 黄金单品种早期版监控（历史，已并入 monitor_positions） |
| `screen_stocks_three_axes.py` / `make_stock_html.py` | 股票/指数版三板斧扫描（实验） |

## 三板斧口径（详见 [METHODOLOGY](docs/METHODOLOGY.md)）

1. **选环境（共振）**：月/周/日收盘价同在各自 10 均线一侧，且周线、日线 MA10 同向，才算多/空共振。
2. **扳机（入场）**：仅在共振方向——
   - 突破：收盘突破近 20 日通道高/低点，且当日量 ≥ 1.2 倍 20 日均量；
   - 回踩：回踩日线 MA10（容差 0.5%）后重新收阳/收阴并站回/压回 MA10。
3. **以损定量（仓位）**：初始止损 = 开仓价 ∓ 1.5×ATR；手数取「权益 1% 风险预算」与「单品种名义敞口 ≤ 10%」双重约束的较小值。
4. **离场（反证位）**：破初始止损 1.5×ATR；吊灯跟踪止盈 = 近 22 日极值 ∓ 3×ATR；日线 MA10 拐头；跌出共振。

## 快速开始

```bash
# 0) 前置：Python >= 3.10、Node >= 18（含 npm）
# 1) 一键安装 Python 依赖 + 两个 CLI（panda / lark-cli）
bash install.sh
# 2) 配置你自己的资源 ID
cp .env.example .env   # 编辑 .env，填入飞书群/Base/表 ID 与仿真账户 ID
# 3) 两个 CLI 分别登录（浏览器授权，需本人操作一次）
panda login
lark-cli config init && lark-cli auth login --recommend
# 4) 验证
python3 monitor_positions.py
python3 shadow_stop_guard.py --selftest
# 5) 按 docs/CRON_TASKS.md 配置定时任务
```

完整步骤、所需权限、网络域名白名单、故障排查见 **[docs/SETUP.md](docs/SETUP.md)**。

## 文档导航

| 文档 | 内容 |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | 环境准备、CLI 安装与授权、网络域名白名单、调度器、验证、故障排查 |
| [docs/METHODOLOGY.md](docs/METHODOLOGY.md) | 三板斧方法论权威文档（共振/扳机/止损/仓位/监控/灯号/影子守护/证据分级/铁律） |
| [docs/CRON_TASKS.md](docs/CRON_TASKS.md) | 11 个定时任务的时间点 + 完整提示词 + 恢复指引 |
| [docs/WORKFLOW_CONTEXT.md](docs/WORKFLOW_CONTEXT.md) | 体系脉络：演进时间线、脚本对照、已验证教训、迁移说明 |

## 目录约定

脚本工作目录为仓库根目录，运行产物落在 `reports/`（XML、meta）、`snapshots/`（evidence JSON、regime CSV）、`guard/`（守护状态与日志），均已 gitignore。

## 安全与免责

- 本仓库**不含任何凭证**：`~/.panda/`、`*.token`、`credentials*.json`、`.env` 已在 `.gitignore` 排除。
- 程序化输出不保证盈利，历史规则不代表未来；期货有风险，入市需谨慎。
- 本项目仅用于学习与研究，**不构成任何投资建议**。
