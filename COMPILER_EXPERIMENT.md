# 有预算的小IR候选实验

个人客户端`compiler_experiment.py`为支持stdio MCP的harness提供固定编译实验接口。它复用现有中央任务、输入封存、预算、环境锁和归档，不增加中央Shell、Node权限或NPU能力。它与只读`compiler_diagnostic.py`分开；旧诊断入口仍不提交任务。

普通个人账号先批准一个只含ir_v1、NPU秒数0的有限预算，将已确认结束的原始失败编译任务放在该预算内，再交付仅绑定该预算的委托profile。原任务须是已封存known driver、受保护工具SHA匹配的ir_v1，完整且未脱敏的小IR不超过8192字节，stderr未截断，正常编译失败且执行/工作区清退确认；未知、超时、大IR预览或跨预算来源不接受。

```bash
python3 /本人/client/compiler_experiment.py init \
  --profile /本人/private/agent-budget.json \
  --source-job job-原始失败编号 \
  --session-dir /本人/private/compiler-conversation

python3 /本人/client/compiler_experiment.py mcp \
  --session-dir /本人/private/compiler-conversation
```

会话目录必须新建且私有；重连使用原目录，不重新init。安装客户端不包含个人凭据、模型配置或OpenCode。任何能使用此stdio协议的harness均可接入；模型费用、对话隔离和harness其他工具的权限仍由用户另行配置。OpenCode等harness应仅允许本MCP前缀，禁用Shell、任意文件读写、其他MCP和外部Skill。MCP接口不会约束模型进程之外的同UID代码。

工具按顺序使用：get_case读取绑定失败与原文；experiment_status读取中央预算及本会话候选；prepare_candidate以稳定candidate_key、原文SHA和一处old/new字面替换创建封存输入；submit_candidate提交固定CPU实验；wait_candidate跟踪原任务；get_candidate_result读取真实归档；需要时cancel_candidate显式取消并等待确认。

每会话最多两个候选。每次只替换一个非空单行字面片段，old/new各不超过256字节且只匹配一次，候选仍不超过8192字节。该限制不证明语义等价；模型可以提出错误候选，原失败和原封存输入始终保留。模型不能选择命令、编译器、镜像、资源、时限或任意路径；只运行原已核对driver和工具锁，固定1CPU/512MiB/NPU0、60秒、只读输入和有界工作/归档申报。中央仍执行实际Node能力、配额、硬限制及预算准入。

准备只上传输入，不启动实验；提交前先持久化原请求和幂等键。上传/提交响应丢失时重试原candidate_key，不更换内容或创建替代会话。任何候选执行状态未结束或原提交回执未知时，不允许用其他key替代。相同来源和候选摘要使用同一个中央幂等键，重复调用不重复计费。预算关闭/到期或耗尽由中央拒绝新任务；输入存储仍受原owner配额，预算不替代存储限制。

会话固定owner、预算和中央地址，凭据换成更宽身份或其他预算会拒绝。中央恢复冻结预算并撤销旧token后，不能继续消费；需要普通个人账号明确批准只读预算profile并替换原profile，才能读取保留结果，不能由此恢复提交额度。不要降级使用个人密码或管理员profile。

get_candidate_result只声明指定工具的parse/verify结果，始终不认证语义等价、算子精度或NPU。模型总结不能替代归档和主助手审阅；对源仓库应用修改、完整lowering、数值和性能验收仍需独立流程。能力边界、实际模型/Node/恢复验证及失败记录以[实施进度](PROGRESS.md)为准。

2026-11-02实际验收：既有DeepSeek配置的一次OpenCode调用，通过六次限定MCP工具读取自编小IR返回类型错误、提出单处替换、封存、排队、等待并读取实际成功报告。原失败及候选各一个attempt，累计申报2任务/120CPU秒/0NPU秒；另一实际数值UID使用单独安装的便携客户端复用同一job，不多计费。恢复后旧消费token失效，新批准的只读profile在原会话中继续读原结果且不能提交。完整1260项回归、最终同机隔离Root安装42及网页12通过；没有证明任意编译器bug修复、完整lowering、语义等价或NPU执行。初次安装清单失败与一条排除的错误PASS文案均记录在PROGRESS.md。
