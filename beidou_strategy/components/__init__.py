"""BF-09: 策略组件 — Entry/Filter/Exit/Sizing 分离。

从 beidou_core/engine.py 迁移具体算法实现至此。
引擎只保留编排逻辑，不包含具体因子公式。

组件目录:
- entries/: 入场信号组件
- filters/: 过滤器组件 (只输出 ACCEPT/VETO/DEGRADE)
- exits/: 退出策略组件
- sizing/: 仓位管理组件
"""
