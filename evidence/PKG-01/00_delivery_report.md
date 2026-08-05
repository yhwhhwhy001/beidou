# PKG-01 交付报告 — Policy Registry、Schema、签名与原子激活

## 状态：PASS ✅

## 实现概要

### Policy Registry (`beidou_policy/registry.py`)
- PolicyPackage Schema、签名、校验、兼容矩阵、有效期和环境绑定
- 完整生命周期：PROPOSED → VALIDATED → SIGNED → SCHEDULED → ACTIVE → REVOKED
- 严格状态转换检查，非法转换抛出 ValueError
- 原子激活：并发读取者只能看到完整旧版本或完整新版本
- 硬安全策略分类（HARD_SAFETY, ADAPTIVE_BOUNDARY, MODEL_PARAMETER, PROTOCOL_CONSTANT）
- Fail-Closed：缺失关键配置、过期、签名错误时返回 UNKNOWN
- 签名验证：使用 cryptography 库验证 RSA+SHA256 签名
- Registry 的 deepcopy 保证读取隔离

## 测试结果

```
32 passed (policy tests)
```

- 5 生命周期测试
- 7 注册中心测试
- 1 签名验证测试

## 文件清单

| 文件 | 内容 |
|------|------|
| `beidou_policy/__init__.py` | 包初始化 |
| `beidou_policy/registry.py` | PolicyRegistry 完整实现 |
| `tests/unit/test_policy_registry.py` | 13 个单元测试 |
