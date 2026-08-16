# TASK-M00-F07 证据：launchd 治理（P0-02 仓库侧）

## 问题

- 实装 plist（~/Library/LaunchAgents）KeepAlive=true + ThrottleInterval=10，与监督器终态退出码 5（LOCKED）/6（FAILED）冲突 → LOCKED 状态变成 10s 一次的崩溃-重启循环（P0-02）
- 实装与模板漂移：30 vs 2 symbols、KeepAlive 相反、实装用 shell eval .zshrc 注入凭据、日志路径 /tmp vs .beidou/
- 仓库侧修复（实装 plist 变更需用户授权，另行执行）

## 修改

1. 新增 `deploy/beidou_launchd_wrapper.sh`：受治理 wrapper —— 终态退出码 5/6 映射为 0（launchd 不重启），其余非零退出透传（崩溃由 launchd 条件重启）；凭据从 ~/beidou/.env 注入
2. 重写 `deploy/com.beidou.autopilot.plist`：经 wrapper 启动；KeepAlive={SuccessfulExit:false}（崩溃条件重启、终态不重启）；无 shell/eval、无内嵌密钥；ThrottleInterval=30、ExitTimeOut=30
3. `beidou_launcher/preflight.py` 新增 `preflight.launchd_plist_drift` 检查（P2 非阻断 WARN）：比对实装与模板的 KeepAlive/ThrottleInterval/shell-eval 注入
4. 既有测试 `test_launcher.py::test_launchagent_template_is_direct_and_fail_closed` 按新设计更新（保留全部不变量：无 shell/eval、无密钥、显式参数、symbols 非 DEFAULT/ALL；KeepAlive 断言从 `is False` 升级为「不得为 True 且必须是 {SuccessfulExit: false}」—— 对旧 bug 更强约束）

## 测试证据

| 命令 | 结果 |
|---|---|
| `pytest tests/unit/test_launchd_plist_governance.py` | 6 passed（漂移检测×3 + wrapper 退出码映射×2 + 模板语义×1） |
| `pytest tests/unit/test_launcher.py::test_launchagent_template_is_direct_and_fail_closed` | passed |
| `plutil -lint deploy/com.beidou.autopilot.plist` | OK |
| 全量 `pytest tests/` | 2526 passed |

## 待用户授权

- 实装 ~/Library/LaunchAgents/com.beidou.autopilot.plist 更新为新模板（含 wrapper + 30 symbols + 代理环境变量）—— 系统级变更需显式授权后执行
- doctor 输出将出现 `preflight.launchd_plist_drift` WARN（实装仍是旧配置）—— 预期行为，提示漂移存在
