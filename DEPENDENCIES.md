# 阶段依赖与封存产物交接

当前验证范围以PROGRESS.md为准；`evidence/validation-v032.json`仅记录早期阶段，不代表后续功能的当前验收。功能用于把CPU编译与后续精度/性能/仿真分开调度；不要求Agent持续在线等待，不把整段编译时间算作占卡时间。

## 提交方式

先提交普通`build_v1`或其他版本化任务，取得真实job ID；再提交引用它的后续工作流。允许链、分叉与汇合；每个新任务最多8个已有parent和32个导入文件。只允许引用已经存在的不可变任务，接口不能给旧任务补边，因此正常API无法形成环。祖先深度/遍历规模也有限制。当前不提供整图一次性原子提交、循环、自动重试计算或跨owner可写共享。

任务JSON顶层可增加：

```json
{
  "depends_on": ["job-0123456789ab"],
  "artifact_inputs": [
    {"job_id":"job-0123456789ab", "name":"results/package.whl", "target":"deps/package.whl"}
  ]
}
```

这些字段与既有template、environment、parameters并列。parameters.input_id仍是用户已封存的基础源码输入；服务端将其与已声明产物合成另一个封存输入，不修改基础输入。target必须短、相对且与基础文件及其他target无碰撞；source可以是已确认归档中的较长路径，但只允许results/ir/profile内的工作负载产物，不复制控制配置。可额外指定sha256以要求确切摘要，或显式`executable:true`保留可执行用途；默认导入为不可执行文件。

父任务必须属于本人且为托管版本化工作流。项目读权限不能用来导入其他owner的私有产物。各阶段有独立环境锁和资源要求；编译阶段保持NPU0，精度/性能/仿真阶段按既有规则要求NPU及设备资格。平台不自动在共享环境中安装wheel；用户消费阶段应使用自己的私有工作区/venv处理已导入包，并锁定所需ABI、工具和运行环境。

## 执行、完整性和恢复

依赖未就绪或交接中的任务维持queued，allocation为空、无attempt，不预留CPU、内存或NPU。默认success条件下，全部上游成功后才进行交接；任何上游失败、取消或超时，会把尚未启动的下游记为cancelled并附`failure_stage=dependency`及上游原因。需要分析失败产物时，须使用下文显式terminal条件。

传输以中央已经校验的生产者artifact-hashes.json为依据，冻结来源job/attempt、文件名、目标名、大小、SHA、生产者输入与环境摘要。每次最多复制1MiB，每块不超过既有输入分块上限，使用已有上传进度和持久写入合同；一次tick最多处理4个等待项并轮换。中断不触发编译重跑，不覆盖未知输入或接受新摘要；最后完整SHA校验并封存，之后才允许Node领取。

合成输入同样受个人/中央输入预算、字节/inode硬配额和保留策略约束，不为交接绕过限额。空间不足时保留进度等待，源归档/基础输入和已创建的交接输入在下游未结束前受回收保护。下游自己的归档额度仍在提交时预留；多阶段请求需要合理申报结果大小，不能依赖超额隐式排队。

完整性或声明错误会锁定为blocked，保留数据供核对。用户修复原数据或通过既有未封存输入reset-file接口重置损坏的未完成文件后，可请求`POST /api/jobs/JOB/retry-inputs`，body必须为空对象；它仅按原冻结来源继续，不接受路径、命令、parent或摘要覆盖。也可取消下游后用新幂等键提交新的正确任务。已启动任务不允许重新交接输入。

就绪任务的`input_provenance`、`dependency_state`和独立`dependency-inputs.json`归档保留交接证据；原request.json仍是提交时的基础输入声明，实际Node grant绑定的是合成封存输入。CLI工作流JSON和固定请求MCP可携带依赖字段；模型不能临时覆盖会话批准的parents或导入映射。

## 仍需验收的范围

需要验证：上游实际构建/IR与跨attempt交接、分叉/汇合、无NPU提前预留、失败传播、撤权/跨用户拒绝、摘要冲突与损坏、跨tick与重启恢复、容量不足、源数据回收保护、独立Root安装/HTTPS/MCP、停机备份恢复和网页展示。真正的NPU精度/性能/仿真、完整Triton/外部LLVM构建链、真正两物理机与systemd仍需相应资源和实机证据，不能用CPU交接通过来代替。


## 显式终态产物交接

失败、超时或执行中取消也可能留下可分析的IR、精度数据或流水文件。诊断任务可在每条artifact_inputs中明确增加`"when":"terminal"`：

```json
{
  "depends_on":["job-0123456789ab"],
  "artifact_inputs":[
    {"job_id":"job-0123456789ab","name":"results/accuracy-input/actual.bin","target":"actual.bin","when":"terminal"},
    {"job_id":"job-0123456789ab","name":"results/accuracy-input/reference.bin","target":"reference.bin","when":"terminal"}
  ]
}
```

同一parent的所有导入须使用相同条件；混合遗漏/terminal会被拒绝。未指定的默认条件为success，显式success规范化为原请求，不改变旧幂等行为。没有导入项的parent仍为成功屏障；任意其他success屏障失败，后继仍按原规则取消。terminal不是跳过失败检查的通配符，不接受always或未知条件，也不新增任意路径/跨owner导入。

终态条件只接受已认证受控Node的确切执行attempt；上游须明确为succeeded/failed/timed_out/cancelled，结束及执行清退确认齐全，实际分配的工作区/缓存和NPU释放也须有对应确认。未执行就取消、没有attempt的作业不能伪造产物来源，会锁定交接供核对。运行、needs_attention、输出待恢复或缺确认时后继继续排队，无计算预留；不会释放或重新运行上游。

还须有与job/attempt/state匹配的中央归档确认回执，其完整manifest SHA与artifact-hashes.json一致。新输入冻结`terminal_sources`，记录原Node、attempt、原状态、相关退出/清退事实和归档manifest摘要；每条新导入注明when及producer_state。分块传输、配额、源归档回收保护、SHA封存和同owner授权沿用原机制；模型不能修改已绑定MCP会话的边或来源。

封存发布前、Node领取和首次启动许可时都会重核冻结的终态事实。事实变化时保留原输入/许可等待核对，已取得的attempt不被替换；原已开始工作仍按既有监督协议跟踪。诊断后继使用自己的环境锁、资源和获批预算；不能据上游已结束而继承或免除NPU资格。比对仍有差异时可继续明确failed，不把诊断交接本身当成算子修复。

客户端CLI/固定MCP/网页工作流JSON都保留嵌套when，中央必须具备terminal-artifact-handoff-v1支持，可从/api/me的dependency_capabilities及服务就绪记录查询。个人客户端manifest声明terminal-artifact-handoff-client-v1，并附本说明。普通输入字段不变；旧中央不支持时应明确拒绝，不删除条件后降级提交。新控制器保留了终态边历史后，会拒绝激活/恢复缺少dependency_conditions.py的旧包，防止旧代码将已批准的失败诊断误取消。离线快照仍要求排空所有排队/活跃工作，没有放宽备份静默条件。

实际验证和限制见PROGRESS.md。控制CPU程序的超时演练不证明NPU卡死根因；历史NPU精度数据CPU重放也不替代新的设备或多物理机验收。

单预算凭据还须在自身授权范围内读到parent；仅同owner不代表绑定另一预算的token能访问它。恢复会冻结旧预算，新批准预算的绑定token不会自动取得旧预算源任务的访问权。需要由普通个人账号明确批准、导出并重新封存获准数据，或后续实现显式的源产物委托；不能为了自动诊断放宽预算或跨owner权限。
