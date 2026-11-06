# 累计实验预算

个人账号可批准一轮实验的任务数、CPU秒和NPU秒，以及允许的托管模板和有效期。每次新任务在作业创建的同一SQLite事务里按CPU/NPU数量乘任务时限全额计费；失败、取消、未知状态均不自动退款。相同幂等请求只返回原任务，不重复扣费。不是实际运行时间计量，也不约束模型费用、HTTP调用、初始化/清理和未知状态下持续保留的物理租约。

普通个人凭据审批预算后，另交付仅含token的预算凭据给Agent。预算token不能创建预算、关闭预算或再签发凭据；中央自动把其任务绑定到该预算，拒绝指定其他预算，也限制其读取和取消到本预算任务。CLI/MCP使用同一profile，固定请求MCP仍一次一个任务、不接受命令覆盖；多个会话共用同一中央预算。把普通个人密码或宽权限token同时交给Agent，会允许它使用个人账号原有权限，不能称为受单预算约束。同一Linux UID下若允许Agent任意读文件，它仍可能读取其他个人profile；需限制harness工具并按真实UID/HOME/XDG及目录权限隔离，不能把文件0600当作对同UID的隔离。

关闭/到期只拒绝新的计费提交，既已批准的排队任务仍可执行，原幂等回执仍可读取。取消已有任务须显式取消并等待Node确认；不自动撤销原许可或释放未知资源。预算和计费记录随中央快照保存，但旧快照可能缺少后续计费。恢复会将所有预算置为restored_frozen并撤销旧预算token，防止额度随旧快照恢复。历史结果不删除；新的执行须单独批准新预算，不能重开原预算。存在历史记录即拒绝回滚到缺少支持的旧代码。

当前仅支持普通应用账号审批，不允许服务管理员凭据直接委托。输入上传仍按本人存储额度及原权限管理；预算不替代存储配额、Node资源硬限制、设备资格、环境锁、队列公平性或组织身份。默认单预算上限64任务、1,000,000 CPU秒、86,400 NPU秒、7天；每用户最多保留1,000预算，最新100条可列表，其他按ID读取。管理员可通过scheduler.experiment_budget配置较低或明确更高的有限上限；不自动迁移现有配置。

## 个人审批与委托

使用当前发布包安装的新个人客户端（manifest声明experiment-budget-client-v1），先以普通个人profile创建预算。示例spec.json：

```json
{
  "name": "算子编译错误排查",
  "idempotency_key": "investigation-001",
  "limits": {"jobs": 4, "cpu_seconds": 2400, "npu_seconds": 0},
  "templates": ["ir_v1", "build_v1"],
  "lifetime_seconds": 86400
}
```

```bash
labctl --profile /本人/private/client.json experiment-budget create --spec spec.json
labctl --profile /本人/private/client.json experiment-budget show experiment-替换为返回编号
# 输出目录须为本人0700，文件须不存在；不会把token打印到终端。
labctl --profile /本人/private/client.json experiment-budget token experiment-替换为返回编号 \
  --output /本人/private/agent-budget.json
labctl --profile /本人/private/agent-budget.json submit --workflow request.json --key candidate-001
```

每个预算最多签发16个历史委托token，撤销/过期不自动释放此签发次数；token最长24小时且不超过预算期限。签发也受控制盘余量和审计积压保护。

预算profile只含该token和服务器/信任位置，不含个人密码或普通token。中央从凭据自动绑定experiment_id；请求省略该字段也会计费，试图指定其他预算会被拒绝。普通个人profile可以在工作流JSON顶层明确experiment_id进行同样计费。与其他预算无关的个人正常任务保持原规则。

给支持MCP的任意harness使用新客户端：

```bash
python3 /本人/client/mcp_bridge.py --profile /本人/private/agent-budget.json \
  --session-dir /本人/private/conversation-001 --environment worker-python --workflow request.json
```

使用此固定MCP入口时，请在请求JSON中省略experiment_id，由预算profile在服务器绑定，不交给模型选择；普通个人CLI/网页的显式预算字段属于另一种提交方式。多个会话共享同一预算，单会话仍只允许原固定任务；queue_status显示累计已计费/剩余额度。凭据轮换必须保持原账号和预算，扩大或切换绑定将使原MCP会话拒绝工作。当前不是任意自主多轮修复harness；候选生成和复现质量仍需独立能力包与验收。

```bash
labctl --profile /本人/private/client.json experiment-budget list
labctl --profile /本人/private/client.json experiment-budget close experiment-替换为返回编号
```

close不取消已计费任务。观察超时、API503/507或响应丢失时读取/重试原操作，不更换任务幂等键；预算创建本身也有幂等键，不以重试刷新有效期。签发时响应丢失可能留下尚未交付的同预算token，不扩大资源额度；不要用普通密码作为降级凭据。原始profile文件必须保持私有，跨机拷贝需另准备公开CA信任材料。

恢复后如仅需继续历史诊断，普通个人账号可用`experiment-budget token ID --read-only --output 新私有路径`批准只读预算profile。它只允许读取该预算的历史任务，不能提交、取消或恢复原预算额度；最长24小时，也共用16次历史签发上限。不要把恢复后的计数当作包含所有快照后活动的完整账单。

跨轮消费旧产物可使用普通个人账号批准的[指定来源授权](SOURCE_GRANTS.md)。预算token仍不能读取旧来源任务或批准授权；仅中央按批准文件复制到新工作流输入。授权另有有效期/撤销和恢复冻结，不增加预算额度，不能跨owner。
