# V3 Fail-Closed 切片

## 目标

关闭四条已被当前运行证据证明的安全旁路：默认批准密钥、P0 transient 绕过、只按环境的写互锁、以及 G7 零样本/短窗口假阳性。

## 可观察合同

| ID | 合同 | 验收证据 |
|---|---|---|
| BD-V3-01-A | Testnet 缺少签名密钥时预检返回 P0 FAIL，且源码无默认密钥 | `tests/unit/test_v3_fail_closed.py::test_testnet_without_signing_key_is_a_p0_startup_blocker` |
| BD-V3-03-A | POST/PUT/PATCH/DELETE 必须同时满足环境能力与当前 authority | `tests/unit/test_v3_fail_closed.py::test_supervisor_write_interlock_requires_live_resume_authority` |
| BD-V3-03-B | `/ready` 不得在 blocker 或未授权状态返回 true | supervisor health callback review + runtime evidence |
| BD-V3-06-A | G7 无样本、未经过真实窗口、P0 或超出恢复预算时不得 eligible | `tests/unit/test_v3_fail_closed.py::test_g7_*` |
| BD-V3-05-A | 健康端口默认只绑定 loopback | `tests/unit/test_v3_fail_closed.py::test_health_server_defaults_to_loopback` |

## 非目标

- 不启动或重启当前 Testnet。
- 不撤单、平仓、修改保护、修改余额或访问 Mainnet。
- 不把本切片误报为 PostgreSQL、ExchangeAdapter、G5 或 Alpha 已完成。
