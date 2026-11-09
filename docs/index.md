# 文档导航

先读[当前能力与缺口](STATUS.md)。[进展记录](development/PROGRESS.md)保存历史证据，
不作为当前版本全量验收的替代。

| 目标 | 文档 |
|---|---|
| 个人提交、归档、Agent 接入 | [客户端](guides/CLIENT.md)、[工作流](guides/WORKFLOWS.md)、[环境锁](guides/ENVIRONMENTS.md)、[Harness / MCP](guides/HARNESS.md) |
| 部署及扩展 | [服务器指南](guides/SERVER.md)、[部署](operations/DEPLOYMENT.md)、[多服务器路线](architecture/SCALING.md)、[账号接入](operations/OWNER_PROVISION.md) |
| 调度与依赖 | [任务图](architecture/JOB_GRAPHS.md)、[依赖](architecture/DEPENDENCIES.md)、[启动竞争](architecture/COOPERATIVE_START.md) |
| 本轮运维能力 | [资源压力、维护、通知和交互调试](operations/CORE_OPERATIONS.md)、[有限留存选择](operations/MAINTENANCE_POLICY.md)、[执行窗口与预算](operations/MAINTENANCE_EXECUTION.md) |
| 历史与存储 | [存储](operations/STORAGE.md)、[冷存读取回收](operations/COLD_RETENTION.md)、[历史容量](operations/HISTORY_CAPACITY.md)、[控制历史轮转](operations/CONTROL_ROTATION.md)、[结果正文迁出](operations/RESULT_HISTORY.md)、[恢复](operations/RECOVERY.md) |
| NPU 使用边界 | [设备准入](operations/NPU.md)、[条件观测](operations/NPU_CONDITIONS.md)、[测量窗口](operations/NPU_MEASUREMENT.md) |
| 接口 | [HTTP / MCP](reference/API.md) |
| 历史与开发 | [进展](development/PROGRESS.md)、[遗留项](development/BACKLOG.md)、[贡献约定](../CONTRIBUTING.md) |

目录分工：`guides/` 面向使用者，`architecture/` 解释设计，`reference/` 记录接口，
`operations/` 面向管理员，`development/` 保存开发进展与历史验收。
领域 Agent、夜间 PR 分析和网页操作扩展暂缓，按[当前状态](STATUS.md)的优先级推进。

新增[失效授权正文与预算恢复保护](operations/GRANT_HISTORY.md)，完整长期历史轮转仍待。
