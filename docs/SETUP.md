# 环境准备与安装指南（迁移必读 · SETUP）

> 把「三板斧主观量化期货工作流」迁移到任何新环境（豆包之外的机器 / 服务器 / 云主机）时，照本文从零搭起即可。
> 本体系是「**数据 + 两个 CLI + 一个调度器**」的组合，全程只读、不自动下单。

## 〇、一句话总览

```
Python 依赖（akshare/pandas/numpy/requests）
   + panda CLI      → 读 PandaAI 期货仿真账户/持仓/行情
   + lark-cli       → 发飞书群消息、建云文档、写多维表格
   + 一个调度器      → 按 11 个时间点定时触发（见 CRON_TASKS.md）
```

## 一、系统要求

| 项 | 要求 | 开发验证版本 |
|---|---|---|
| 操作系统 | Linux（Ubuntu/Debian/CentOS 等；也支持 macOS/Windows） | Linux x86_64 |
| Python | ≥ 3.10（建议 3.12） | 3.12.11（/opt/python3.12） |
| Node.js / npm | Node ≥ 18（装两个 CLI 用） | Node v22.23.2 / npm 10.9.8 |
| git | ≥ 2.x | 2.34.1 |
| 网络 | 能访问飞书、行情数据源（见第六节白名单） | — |

> 若新环境没有 Node，先装：Ubuntu 可 `curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt-get install -y nodejs`，或用 nvm。

## 二、需要安装的全部组件（清单）

| 组件 | 类型 | 作用 | 安装命令 |
|---|---|---|---|
| Python 依赖 | pip | 行情拉取、数据计算、信号 | `pip install -r requirements-lock.txt` |
| **panda CLI** | npm 全局包 | PandaAI 期货仿真：账户/持仓/行情/合约参数（只读） | `npm install -g @chongqingliangyunzhijing/contest-cli` |
| **lark-cli** | 飞书官方安装器 | 飞书群消息 / 云文档 / 多维表格 / 云盘 | `npx @larksuite/cli@latest install` |
| 调度器 | 系统 cron / 云函数 / GitHub Actions | 定时跑 11 个任务 | 见第八节 |

## 三、安装 Python 依赖

```bash
# 建议先建虚拟环境
python3 -m venv .venv && source .venv/bin/activate
# 精确版本（已验证可运行）
pip install -r requirements-lock.txt
# 或用宽松约束
pip install -r requirements.txt
```

验证：
```bash
python3 -c "import akshare, pandas, numpy, requests; print(akshare.__version__, pandas.__version__)"
```

## 四、安装并授权 panda CLI（PandaAI 期货仿真）

- **是什么**：PandaAI 期货仿真大赛命令行工具，npm 包名 `@chongqingliangyunzhijing/contest-cli`。
- **安装**：
  ```bash
  npm install -g @chongqingliangyunzhijing/contest-cli      # 最新；开发验证版 0.1.23
  panda --version
  ```
- **登录（浏览器授权，需本人操作一次）**：
  ```bash
  panda login        # 打开浏览器完成授权
  panda whoami       # 查看登录状态与绑定账户
  panda doctor       # 连通性与状态自检
  ```
- **凭证位置**：`~/.panda/credentials.json`（已在 `.gitignore` 排除，**禁止读取/提交/展示**）。
- **本体系只调用只读子命令**：`account` / `positions` / `quote` / `snapshot` / `contract-spec` / `varieties` / `orders` / `trades`。
  写命令（`order` / `cancel` / `close-all` / `target-position` / `rollover`）一律不调用。
- 目标仿真账户：由环境变量 `PANDA_ACCOUNT_ID` 指定（你自己的仿真账户 ID），起始资金按你的账户填写。

## 五、安装并授权 lark-cli（飞书官方 CLI）

- **是什么**：飞书/Lark 官方开源 CLI（MIT，仓库 https://github.com/larksuite/cli ），覆盖消息/文档/多维表格/云盘等 200+ 命令。
- **安装**：
  ```bash
  npx @larksuite/cli@latest install
  lark-cli --version
  ```
- **前置：需要一个飞书「自建应用」**（迁移时若没有，需在飞书开放平台创建）：
  1. 打开 https://open.feishu.cn → 开发者后台 → 创建企业自建应用；
  2. 拿到 **App ID** 和 **App Secret**；
  3. 在「权限管理」开通所需权限（见下），并把应用发布/安装到企业、加入目标群。
- **配置应用凭证 + 登录**：
  ```bash
  lark-cli config init            # 交互式填入 App ID / App Secret（AI 协助时用 config init --new）
  lark-cli auth login --recommend # OAuth 登录，自动选常用权限
  lark-cli auth status            # 验证登录与已授权 scope
  ```
- **需要的权限（scopes）**：
  - 消息：`im:message`、`im:message.send_as_user`（以用户身份发群消息）；
  - 云文档：`docx:document`（创建/读取/编辑）；
  - 多维表格：`bitable:app`（表/记录/视图/仪表盘读写）；
  - 云盘：`drive:drive`（上传/导出/导入）；
  - 电子表格：`sheets:spreadsheet`（如需）。
  用 `--recommend` 会自动勾选常用 scope；可用 `lark-cli auth check --scope <scope>` 逐项校验。
- 凭证存在 OS 原生 keychain / 本地配置，**禁止提交**。

## 六、网络接口 / 域名（防火墙白名单）

| 域名 / 端点 | 用途 | 谁在用 |
|---|---|---|
| `open.feishu.cn`（国内）/ `open.larksuite.com`（国际） | 飞书 OpenAPI | lark-cli |
| `hq.sinajs.cn` | 新浪期货/股票实时行情 | AkShare |
| `stock2.gtimg.cn`、`qt.gtimg.cn` | 腾讯期货行情（主力连续、日 K） | AkShare |
| `push2.eastmoney.com`、`datacenter-web.eastmoney.com` | 东方财富行情/数据 | AkShare |
| `finance.sina.com.cn`、`vip.stock.finance.sina.net` | 新浪期货历史日 K（futures_zh_daily_sina） | AkShare |
| `www.szse.cn`、`www.sse.com.cn`、各交易所官网 | 交易日历/合约参数 | AkShare |
| PandaAI 仿真后端（CLI 内置域名） | 仿真账户/行情 | panda CLI |
| `registry.npmjs.org` | 安装 npm 包 | npm/npx |

> 出网受限的内网环境，至少放行：飞书 OpenAPI 域名 + 一个行情源（新浪或腾讯）+ npm registry。

## 七、飞书资源（迁移后需重建或沿用）

- 以下资源的 ID 已在 `docs/WORKFLOW_CONTEXT.md` 完整列出：推送群、索引 Base/表、品种信号看板表/仪表盘、复盘台账、云盘文件夹。
- **若沿用原飞书组织**：资源 ID 不变，脚本可直接用。
- **若换了飞书组织/新建资源**：需要重新建 Base、表、看板，并把新的 base_token / table_id / chat_id 替换进各脚本与 `docs/WORKFLOW_CONTEXT.md`。
- 看板字段（28 字段）与索引表字段定义见 `docs/CRON_TASKS.md` 中对应任务提示词。

## 八、调度器（跑 11 个定时任务）

11 个任务的时间点（cron/rrule）与完整提示词见 `docs/CRON_TASKS.md`。可选承载方式：

- **系统 cron（最贴近原方案）**：在常驻机器上 `crontab -e`，按表达式调用，query 用文档里的提示词。
- **GitHub Actions**：用 `on.schedule.cron`（UTC 时间，注意 +8 换算）；需把 panda/lark 登录态或 App 凭证存为 Repository Secrets，且 Actions 是无状态短任务，`panda login` 这类浏览器授权需改走凭证注入。
- **云函数 / 无服务器调度**（阿里云函数计算、腾讯云 SCF 等）：按 cron 触发，注意登录态与依赖打包。
- **注意**：
  - 原豆包平台最小调度间隔为 3600s（1 小时），新平台若支持更短间隔可按需加密（如影子守护 3 分钟）。
  - panda / lark 登录态会过期，建议在常驻机器上运行，并在任务里保留「登录态失效 → 提示重新授权」的分支（各任务提示词已内置）。

## 九、安装后验证（最小冒烟）

```bash
# 1. Python 依赖
python3 -c "import akshare,pandas,numpy,requests; print('py deps ok')"
# 2. panda 通道
panda whoami
# 3. lark 通道
lark-cli auth status
# 4. 只读跑一次持仓脚本（不发飞书、不下单）
python3 monitor_positions.py
# 5. 影子守护离线自测（不连柜台）
python3 shadow_stop_guard.py --selftest
```

全部通过即环境就绪，再按 `docs/CRON_TASKS.md` 启用定时任务。

## 十、一键安装

```bash
bash install.sh        # 安装 Python 依赖 + panda CLI + lark-cli（登录步骤仍需本人浏览器完成）
```

## 十一、故障排查

| 现象 | 处理 |
|---|---|
| `panda` 取数返回空 / 未授权 | 重新 `panda login`；先确认是否休市（休市无数据属正常，不是故障） |
| `lark-cli` 报登录失效 / scope 缺失 | `lark-cli auth login --recommend`，或按报错补 scope 后重新登录 |
| 行情接口超时 / 403 | 数据源限流，退避重试；可换新浪/腾讯另一源 |
| npm/npx 安装慢 | 配置 npm 镜像：`npm config set registry https://registry.npmmirror.com` |
| 非交易时段无数据 | 正常现象，静默即可，不要发 ERROR |
| 换月/合约无 ATR | 脚本会回退到主连计算；仍失败则如实标注，不硬凑 |

## 十二、安全红线

- 本仓库不含任何凭证、手机号、open_id；`~/.panda/`、`*.token`、`credentials*.json` 已 gitignore。
- 任何开/平仓、撤单都由人本人手动确认，Agent 与脚本不连接报单自动交易。
- 所有信号为客观规则结果，不构成投资建议；历史规则不代表未来。
