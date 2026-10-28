# 工作流上下文脉络（开源版）

> 本文记录「三板斧主观趋势期货工作流」的体系脉络：目标、演进、脚本对照、关键教训与迁移说明。
> 开源版已将所有个人资源 ID（飞书群 / Base / 表 / 账户）替换为占位符，具体 ID 请通过 `.env` 自行配置（见 [SETUP](SETUP.md)）。

## 一、体系是什么

一套跑在期货**仿真账户**（虚拟资金、真实行情）上的量化监控体系，方法论核心叫「三板斧」：

1. **选环境**：月/周/日三周期 MA10 共振（只做共振方向）；
2. **严格止损**：1.5×ATR 初始止损 + 吊灯跟踪止盈（近 22 日极值 ∓3×ATR）+ 日线 MA10 拐头；
3. **以损定量**：手数 = 权益 1% 风险与单品种 10% 敞口双重约束取小。

纪律普句：**扳机不响不进、破线必走、不追单、节后首日跳空不追、全程只读不下单、不构成投资建议。**

## 二、体系演进时间线

- **第一版**：三板斧筛选（`screen_three_axes.py`）、单品种监控（`monitor_gold.py`）、51 品种日报（`daily_report.py`）、全持仓风险分级（`monitor_positions.py`）、看板同步（`sync_dashboard.py`）；建立飞书群推送与多维表格索引。
- **方向与证据**：日报加入看涨/看跌双向证据与确定性方向研判；风险扫描与收盘小结切换为全持仓。
- **信号扫描**：加入入场/离场信号（突破 20 日通道放量 ≥1.2 倍 / 回踩 MA10 企稳）；创建长假离场检查（14:35 盘中压力测试）；建立「品种信号看板」（28 字段、多视图、跨日趋势仪表盘）。
- **影子守护**：上线纯只读影子止损守护（每 3 分钟自扫、1.5×ATR 登记、破位只发「影子演练」不真下单）+ 每小时保活。
- **迁移**：原承载平台出现免费额度/频率门控，定时任务被拦截，遂将整套工作流归档到 GitHub，作为可迁移的开源模板。

## 三、配置清单（需要你自己填的资源）

> 所有资源 ID 通过环境变量提供（复制 `.env.example` 为 `.env`）。**本仓库不含任何真实 ID。**

| 环境变量 | 含义 | 形态 |
|---|---|---|
| `LARK_CHAT_ID` | 推送日报/预警的飞书群 ID | `oc_xxxxxxxx` |
| `LARK_BASE_TOKEN` | 三板斧 Base 的 token | `bascnxxxxxxxx` |
| `LARK_DASHBOARD_TABLE_ID` | 品种信号看板表 ID | `tblxxxxxxxx` |
| `LARK_INDEX_TABLE_ID` | 每日报告索引表 ID | `tblxxxxxxxx` |
| `PANDA_ACCOUNT_ID` | 要监控的仿真账户 ID（防误监控其他账户） | 数字串 |
| `ACCOUNT_EQUITY` | 账户权益（用于以损定量） | 数值 |

飞书 Base / 表 / 看板 / 仪表盘的字段与视图定义，见 [CRON_TASKS](CRON_TASKS.md) 中对应任务提示词。

## 四、脚本与产物对照

| 文件 | 职责 |
|---|---|
| daily_report.py | 51 品种日报：方向研判、共振、扳机、持仓离场、ATR/止损/手数、XML 九章、evidence JSON |
| sync_dashboard.py | evidence → 看板表 28 字段幂等同步 |
| monitor_positions.py | 全持仓风险分级（INFO/WARN/DANGER）、`--brief` 收盘小结 |
| holiday_exit_check.py | 长假前最后交易日 14:35 离场压力测试（LEVEL=SKIP/HOLIDAY/NOHOLD/ERROR） |
| monitor_gold.py | 黄金单品种早期监控（历史，已并入 monitor_positions） |
| screen_three_axes.py | 三板斧共振/信号命令行扫描 |
| screen_stocks_three_axes.py / make_stock_html.py | 股票/指数版三板斧扫描（实验） |
| shadow_stop_guard.py | 纯只读影子止损守护（`--once` 干跑 / `--selftest` 自测 / 常驻） |
| guard/keepalive_shadow.sh | 守护每小时保活（STOP 哨兵 / pid 身份校验） |
| requirements.txt / requirements-lock.txt | Python 依赖（宽松 / 精确锁定） |

运行产物（不入库）：`reports/`（XML、latest_meta.json）、`snapshots/`（evidence JSON、regime CSV）、`guard/`（守护状态、日志）。

## 五、已验证的关键教训（迁移后必须保留）

1. **止盈线不能漏**：只给初始止损、不给吊灯止盈线，会出现「浮盈很大却没收到离场提醒」。
   → 任何持仓推送必须**同时给初始止损（1.5ATR）、吊灯跟踪止盈线（近 22 日极值 ∓3ATR）与日线 MA10**；
   对已大幅盈利的持仓，吊灯线若被开仓前的历史极值污染会失效，需补 MA10 / 移动止盈跟踪。
2. **守护进程保活要防 pid 复用**：进程退出后 pid 可能被系统重新分配（实测被浏览器占用），保活必须叠加 `/proc/<pid>/cmdline` 身份校验（`keepalive_shadow.sh` 已内置）。
3. **休市 ≠ 故障**：行情日不是当天 / 周末 / 节假日时静默，不发 ERROR；只有交易时段内取数失败才提示通道异常，用幂等键防重复。
4. **日报脚本前台运行**：后台任务偶发句柄丢失，timeout 建议 ≥260s。
5. **看板同步幂等**：同日同品种已存在则更新、否则新增，单批 ≤200，写后回读校对行数 = 证据条数。
6. **新平台先确认额度/频率限制**：避免重蹈「免费额度耗尽、定时任务停触发」。

## 六、数据源与工具链

- 行情：AkShare（新浪/腾讯/东财等公开源，免费）；
- 账户 / 持仓：`panda` CLI（期货仿真，凭证由 CLI 管理，Agent 只读）；
- 推送 / 文档 / 表格：`lark-cli`（飞书官方 CLI，群消息、云文档、多维表格）。

## 七、迁移说明

- 本仓库为自包含模板：clone 后 `bash install.sh` → 配置 `.env` → 两个 CLI 登录 → 按 [CRON_TASKS](CRON_TASKS.md) 启用定时任务。
- 安全边界：不含凭证、手机号、open_id；运行产物不入库；所有信号为客观规则结果，不构成投资建议。
