# Meme Coin Monitor — 多链聪明钱监控

[English](README.en.md) | **简体中文**

基于 Python 的只读命令行工具，监控公开钱包交易、发现历史盈利交易者，并通过 Telegram 发送提醒。程序版本为 **1.2.0**，入口为 `holder_watch.py`。

## 功能与支持范围

| 功能 | 支持范围 |
| --- | --- |
| 观察钱包买入、卖出和代币互换 | Solana、Ethereum、Base、BNB Chain、Arbitrum |
| 按代币发现历史盈利交易者 | 上述五条链 |
| 交易者历史表现评分、预设名单刷新 | 上述五条链 |
| 大户持仓、价格和流动性规则提醒 | Solana SPL 代币 |
| Telegram 提醒、运行日志、JSONL 信号记录 | 所有监控模块 |

“聪明钱”是按历史交易表现筛选的观察对象，不代表经过身份验证，也不保证未来盈利。程序不需要助记词或私钥，不签名、不发送链上交易、不自动跟单。

## 快速开始

需要 Python **3.10 或更新版本**、网络连接；Telegram 提醒需要自己的机器人。

### Windows（PowerShell）

```powershell
git clone https://github.com/instl999/meme-coin-monitor.git
cd meme-coin-monitor
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe holder_watch.py setup
.\.venv\Scripts\python.exe holder_watch.py --once
.\.venv\Scripts\python.exe holder_watch.py
```

### Linux / macOS

```bash
git clone https://github.com/instl999/meme-coin-monitor.git
cd meme-coin-monitor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python holder_watch.py setup
.venv/bin/python holder_watch.py --once
.venv/bin/python holder_watch.py
```

首次检查一个观察钱包时，程序记录起点，不回放其历史交易。以后检测新增交易。按 `Ctrl+C` 停止；更改配置后重新启动。

`setup` 会引导配置 Telegram、链和 API 密钥、预设交易者以及某个代币的交易者。密钥写入 `.env`，观察名单写入 `config.json`，修改配置前会生成备份。

## API 与 Telegram 配置

也可以将 `.env.example` 复制为 `.env` 后自行填写。不要把密钥写入 `config.json` 或提交到 GitHub。

| 环境变量 | 用途 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | Telegram 提醒；不设置时仅输出到控制台和日志 |
| `HELIUS_API_KEY` | Solana 持仓查询、发现交易者、构建预设名单；默认公共 RPC 配置下自动使用 |
| `ANKR_API_KEY` | EVM 交易历史索引、发现和评分；BNB Chain 观察建议先配置并验证 |
| `ALCHEMY_API_KEY` | 可替代 Ankr 的历史索引来源，具体链和 API 权限需自行确认 |
| `SOLANA_RPC_URL` | 自定义 Solana RPC |
| `ETHEREUM_RPC_URL`、`BASE_RPC_URL`、`BSC_RPC_URL`、`ARBITRUM_RPC_URL` | 各 EVM 链自定义 RPC；普通节点不一定提供钱包历史索引 |

Ethereum、Base、Arbitrum 的交易观察实现支持公共 RPC 路径；公共节点可能限流或限制日志查询。Solana 公共节点也可能无法完成大户查询。供应商支持、权限、额度和价格会变化，请以供应商控制台为准。

通过 Telegram 的 `@BotFather` 创建机器人，向机器人发送一条消息，再运行：

```bash
python holder_watch.py telegram
python holder_watch.py --test-alert
```

机器人已绑定其他服务的 webhook 时，可按提示手动填 chat ID 或使用独立机器人。

## 默认配置与规则

本仓库保留原项目 `config.json`：监控 CYBERLEEK 的 Solana mint `ApZuxdpzMrbEYTGEzeY9afh5pj9d6qPRJCTgQYiipbKg`，包含历史观察钱包及交易者预设。它们是原项目样例配置，不是实时精选名单或投资推荐。使用前核对链和合约地址，并调整观察名单。

| 配置 | 当前随附值 / 作用 |
| --- | --- |
| `mint` | Solana 持仓监控代币；设为 `PASTE_MINT_HERE` 可仅运行钱包观察，须至少配置一个交易者 |
| `poll_seconds` / `traders.poll_seconds` | 持仓检查 30 秒 / 交易者检查 60 秒，最小 15 秒 |
| `top_n` | 观察前 10 个非排除持有人，范围 1–20 |
| `exclude_owners` / `auto_exclude_pools` | 手动排除钱包 / 自动识别并排除池子等地址 |
| `always_alert_owners` | 指定钱包任何持仓流出触发提醒 |
| `rules.holder_drop_pct` | 单个观察钱包在时间窗内减少至少 20% |
| `rules.combined_drop_pct` | 观察钱包合计持仓减少至少 10% |
| `rules.window_minutes` | 上述持仓规则的时间窗：60 分钟 |
| `rules.min_liquidity_usd` | 最深池流动性低于 100,000 美元 |
| `rules.stop_price_usd` / `rules.trailing_stop_pct` | 价格阈值 / 从持久化峰值回落比例；随附配置为 `null`（关闭） |
| `traders.tokens` | `any` 所有代币；`source` 发现来源代币；`major` 主流代币范围 |
| `traders.min_trade_usd` | 默认仅提醒估值至少 10 美元的交易，可为每个钱包单独配置 |
| `heartbeat` | 每日运行状态提醒；随附时区为 `America/New_York`，可自行修改 |
| `signal_log` | 默认 `signals.jsonl`，每条已提醒交易一行；设为 `null` 关闭 |

规则值设为 `null` 即关闭。`labels` 可自定义钱包显示名，`alert_cooldown_minutes` 控制各规则重复提醒间隔。配置在启动时校验，错误以退出码 2 返回。以 `_` 开头的字段用于说明。

## 常用命令

以下命令中的 `python` 应替换成你虚拟环境里的 Python 路径。

```bash
python holder_watch.py traders
python holder_watch.py traders add WALLET_ADDRESS --chain base --label "My watch" --tokens any --min-usd 50
python holder_watch.py traders remove WALLET_ADDRESS --chain base
python holder_watch.py discover TOKEN_ADDRESS --chain base
python holder_watch.py discover TOKEN_ADDRESS --chain solana --watch 3
python holder_watch.py scorecard WALLET_ADDRESS --chain ethereum --days 30
python holder_watch.py presets list
python holder_watch.py presets add base bsc --build
python holder_watch.py presets refresh base --days 30 --top 5 --add
python holder_watch.py presets renew
python holder_watch.py --list
python holder_watch.py --config /path/to/config.json -v
```

链名为 `solana`、`ethereum`、`base`、`bsc`、`arbitrum`。将 `WALLET_ADDRESS` / `TOKEN_ADDRESS` 替换为真实地址，EVM 地址要明确指定链。`--watch N` 会直接将排名前 N 的交易者保存到观察名单；`--json` 可输出发现结果。

预设筛选考虑历史盈利、多个代币上的表现、持仓时间和活跃程度，并过滤部分机器人和短线交易行为。发现结果使用已实现与未实现收益，受历史覆盖范围和估值影响。刷新和续期可能耗时较长并消耗 API 额度；`presets renew` 更新预设钱包，保留手动添加的钱包。

## 数据与项目结构

```text
holder_watch.py       命令行入口
watcher/              配置、RPC、解析、规则、发现、评分及提醒
tests/                离线测试与交易样例
deploy/               打包、Linux 安装器及 systemd 单元
config.json           随附观察配置
presets.json          随附历史交易者名单
.env.example          空密钥配置模板
README.en.md          英文使用说明
DEPLOY.md             原项目详细英文部署指南
CODEX_DEPLOY.md       原项目部署任务参考，仅为文档
```

运行会生成 `state.json`、`trader_state.json`、`signals.jsonl`、`logs/` 等本地文件；刷新名单生成 `presets.local.json`。这些文件、`.env`、配置备份、虚拟环境和构建产物均被 Git 忽略。状态文件用于重启后接续检查；同一组状态文件只能运行一个监控实例。

## 测试与 Linux 常驻部署

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python deploy/package.py
```

打包生成 `dist/holder-watch-1.2.0.tar.gz` 和 SHA-256 校验文件，不包含 `.env` 或运行数据。将包、校验文件和你自己的 `.env` 分别传到服务器；服务器需要 Python 3.10+、venv、systemd 和出站 HTTPS。

```bash
cd /tmp
sha256sum -c holder-watch-1.2.0.tar.gz.sha256
tar -xzf holder-watch-1.2.0.tar.gz
cd holder-watch-1.2.0
sudo ./deploy/install.sh --check
sudo ./deploy/install.sh --env-file /tmp/holder-watch.env
sudo systemctl status holder-watch
sudo journalctl -u holder-watch -n 50 --no-pager
```

安装到 `/opt/holder-watch`，以 `holderwatch` 用户运行，并默认启用每月预设续期定时器。`--replace-config` 会使用包内配置替换服务器配置并备份旧配置，只在需要时添加；`--no-auto-renew` 关闭自动续期。安装器会删除传入的 `.env` 临时副本，请预先保留自己的安全备份。完整选项见 [DEPLOY.md](DEPLOY.md)。macOS 和 Windows 本地运行不使用 systemd。

## 故障排查与边界

- HTTP 429 或大户数据不可用：配置有效 RPC / Helius 密钥，降低检查频率，检查额度。
- EVM 发现或评分缺少历史：配置 Ankr / Alchemy，确认支持对应链和历史接口。
- Telegram `chat not found`：先向机器人发消息，再运行 `telegram` 重新识别 chat ID。
- 没有交易提醒：确认首次基线已建立、观察名单、代币范围和最小金额设置正确。
- Windows 时区错误：在当前虚拟环境重新安装 `requirements.txt`（包含 `tzdata`）。

本工具采用轮询，不能保证逐笔实时捕获。Solana 持仓来源受 RPC 前 20 个代币账户限制；交易观察对 keeper/solver 代发、DCA、限价单和某些路由可能漏报。EVM 同代币同区块交易可能合并。交易分类与收益统计属于尽力解析；价格和流动性依赖第三方数据。提醒保留浏览器链接以便核验，请勿将结果作为唯一交易依据。

## 许可

原始压缩包未提供开源许可证。本次公开发布未擅自添加授权条款；使用或分发请先取得权利人的许可。
