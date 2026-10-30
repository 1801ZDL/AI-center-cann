# Harness 与领域能力接入

2026-10-31：调度 MCP 增加 `preview_job`，在原固定会话范围内读取同一任务的当前可见节点和阻碍，不创建提交意图/任务/资源预留。预览与提交共享请求构造，不能借预览传任意命令、路径、输入或资源。它是 `readOnlyHint` 工具，但 harness 的其他 shell/MCP 权限仍需单独限制。当前验证为实际HTTP/CLI/MCP适配器调用与协议检查，未新增模型诊断或NPU计算验收。参见CLIENT.md与API.md。


2026-10-30 v0.22 开发中：独立个人客户端新增 `diagnostic_case.py --profile ... --job-id ...`，提供只读、单任务绑定的 `get_diagnostic_case` MCP；完整参数与边界见 CLIENT.md。真实已失败的 Triton 最终链接任务通过 CLI/stdio 取得材料、分块哈希检查、跨用户拒绝及提交/任务覆盖拒绝。一次隔离 OpenCode/用户指定 DeepSeek 261017 配置的所有工具均禁用，仅整理已定位的 `-lz` 日志；主助手核对草稿与真实错误及新镜像预检一致。这只证明已知故障摘要流程，不证明自动发现复杂根因、修复编译器或获得 NPU/性能优化能力，msagent/msprof 仍未安装验收。

个人 OpenCode 可按任务绑定一个诊断入口，例如（路径和任务 ID 需替换；不复制其他人的 profile）：

```json
{"mcp":{"ascend_diagnostics":{"type":"local","command":["/usr/bin/python3","/本人/ascend-lab-client/diagnostic_case.py","--profile","/本人/private/client.json","--job-id","job-0123456789ab"],"enabled":true,"timeout":60000}}}
```

这与原 `ascend_lab` 调度 MCP 分开：诊断入口只取材料，实验入口仍执行配额、环境和资源准入。harness 本身的 shell/其他 MCP 权限需要独立配置，安装诊断 MCP 不会自动约束其所有工具。模型建议仍需结合原始证据与受调度实验验收。

2026-10-29。当前通用入口为个人 CLI 与 stdio 调度 MCP。v0.7 已实际完成 OpenCode/DeepSeek 的固定 IR 工作流，以及无模型依赖的独立 MCP IR 提交/重连；其他 harness 产品自身和 msagent 领域工具尚未安装或实际验收。本文不启用公共夜间构建、仿真或上板。

msagent 官方明确提供可供 OpenCode、Codex 等外部 Agent 复用的 Skills 与 msprof-mcp。其接入指南给出了 stdio 方式、安装/复制路径以及 Python/系统库要求，因此没有必要要求全体用户改用同一种 harness。部署时应固定所选能力包及依赖版本，并在个人隔离环境安装。[官方外部接入指南](https://gitcode.com/Ascend/msagent/blob/master/docs/zh/user_guide/integration-guide.md)

建议一个个人 harness 接两类工具：

| 接口 | 职责 | 当前状态 |
|---|---|---|
| ascend_lab 调度 MCP | 中央提交/排队/等待/取消、按 ACL 获取日志和产物 | CPU 示例与固定输入 IR 工作流已实测，独立安装见 CLIENT.md / WORKFLOWS.md |
| 领域分析 MCP/Skill | 读取有权限的归档数据，形成假设、报告和下一轮实验请求 | msagent/msprof 尚待隔离安装与真实数据验收 |

msprof-mcp 的文档列出的输入包括 profiling trace、算子 CSV 和 profiler 数据库。应先用这些支持格式做只读验收；原始 Triton dump、编译器 MLIR、具体仿真工具的流水并不因存在 MCP 接口就自动兼容。领域分析使用任务 ID/受限产物目录，不允许任意遍历其他人的主目录。[官方工具与数据说明](https://gitcode.com/Ascend/msagent/blob/master/docs/zh/user_guide/integration-guide.md)

msagent 的 Operator 文档面向单算子性能分析和优化，并要求提供 kernel 与可执行算子目录。接入本地 Triton/AscendNPU-IR 仍需核验所选技能的语言、工具链和输出格式，再挑选一份已有归档验证，而不是直接启动历史实验。[官方 Operator 说明](https://gitcode.com/Ascend/msagent/blob/master/docs/zh/agent_guide/operator.md)

能力分三步开放：先读取失败日志/环境/IR 并给出有证据链接的分析；随后把复现、编译、精度或性能实验变成中央任务模板请求；最后才在个人隔离 worktree 中生成补丁和回归建议。分析脚本若会启动编译、仿真或设备进程，必须改用调度适配接口或放入中央托管的任务，不能把“配置了调度 MCP”当作它必然不会绕过调度。

完整 msagent 会话/内置 shell/工作流可以作为另一个 harness 使用，但同样需要本人 profile 和受限工具权限。移植 Skill 时带齐脚本与明确依赖；对端 harness 的工具名、目录约定、会话状态和权限规则需分别适配。复制技能说明不能保证执行权限或数值/性能结论一致。跨 harness 的统一合同是任务 API、环境指纹、产物及结构化结果；对话历史不作为任务状态数据库。

本轮未自动安装外部 Skills、msprof 或第三方插件，没有修改任何人的 harness 配置。新接入前仍需源码/依赖范围与真实产物适配验收；PUBLIC-001 公共夜间任务继续延期。
