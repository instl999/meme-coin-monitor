# Meme Coin Monitor · 多链聪明钱监控

[简体中文使用说明](README.zh-CN.md) | [English documentation](README.en.md)

只读监控 **Solana、Ethereum、Base、BNB Chain 和 Arbitrum** 上的公开钱包交易，发现历史盈利交易者，并通过 Telegram 发送提醒。支持 Solana 大户持仓、价格与流动性规则，以及 JSONL 信号记录。

A read-only Python CLI for public wallet trades on **Solana, Ethereum, Base, BNB Chain and Arbitrum**, historical trader discovery, and Telegram alerts. Includes Solana holder, price and liquidity rules, plus JSONL signal records.

**Python 3.10+ · Version 1.2.0 · CLI / 命令行工具**

## 快速开始 / Quick start

```bash
python -m venv .venv
# Linux / macOS
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python holder_watch.py setup
.venv/bin/python holder_watch.py
```

```powershell
# Windows PowerShell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe holder_watch.py setup
.\.venv\Scripts\python.exe holder_watch.py
```

首次检查建立基线，不回放历史交易。密钥保存在 `.env`；不要上传它。随附代币和观察钱包为历史样例，使用前请核对并调整。

The first check establishes a baseline without replaying old trades. Keep credentials in `.env` and never upload it. The shipped token and wallet lists are historical samples; review and customize them before use.

## 文档 / Documentation

| 文档 / Document | 内容 / Contents |
| --- | --- |
| [中文说明](README.zh-CN.md) | Windows/Linux 安装、API 配置、命令、规则、部署与排错 |
| [English guide](README.en.md) | Installation, API keys, commands, rules, deployment and troubleshooting |
| [Deployment reference](DEPLOY.md) | Original detailed Linux/systemd deployment procedure |

程序不需要助记词或私钥，不签名、不交易、不自动跟单。“聪明钱”仅为历史表现筛选，不保证未来收益。原项目未附开源许可证。

No seed phrases or private keys, transaction signing, trading or automatic copy trading. Historical performance does not guarantee future returns. The supplied project has no open-source license.
