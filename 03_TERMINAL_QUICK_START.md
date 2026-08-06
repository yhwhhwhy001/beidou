# 终端快速启动

## 1. 将本包复制到北斗仓库根目录

解压后，把包内文件复制到 `beidou/` 根目录；不要覆盖仓库业务代码，发生同名冲突时先停止并审查。

```bash
cd /path/to/beidou
python delivery/scripts/validate_package.py
sha256sum -c CHECKSUMS.sha256
bash delivery/scripts/init_execution_branch.sh
```

## 2. Claude Code / Cloud Code 首批执行

在仓库根目录启动工程代理，并提交以下文件作为上下文：

```text
00_EXECUTION_MASTER.md
01_AGENT_OPERATING_PROTOCOL.md
delivery.yaml
delivery/prompts/FIRST_BATCH_T00_T03.md
```

第一批只允许执行 `BD-T00、BD-T01、BD-T02、BD-T03`，`BD-T15` 作为贯穿门禁同步实施。完成后停止，使用独立上下文执行 Round 1 验收。

## 3. 证据采集

```bash
export BEIDOU_AGENT_ID=claude-code-session-01
python delivery/scripts/collect_evidence.py --task BD-T00 -- python -m compileall -q beidou_* apps
```

## 4. 禁止

- 不得执行 Mainnet；
- 不得使用真实资金；
- 不得跳过任务依赖；
- 不得因现有测试失败而删除或弱化测试；
- 不得一次性让一个上下文完成开发与最终验收。
