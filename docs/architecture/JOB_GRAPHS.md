# 整体提交多阶段任务图

2026-11-09补充：读取同时检查SQL与JSON的图ID、owner、状态和重试时间；阶段必须同时绑定
原图ID、节点名、任务ID、owner和实验预算。SQL中非本人owner在解析JSON前返回404。
已发布图仍有待归档快照、绑定缺失或状态不一致时暂停分配，不据此重新提交或释放预留。
保存不允许改变图身份、节点集合、实验或将submitted退回preparing；显式手动恢复仍可在
自动32次失败之后继续原图，不重置原编号与计费。

恢复在原维护锁与停止态检查内，打开两个只读SQLite事务，逐图核对完整元数据、阶段
SQL身份/幂等/fingerprint、完整执行事实及全部原执行尝试。当前attempt必须对应原任务的
node与状态，不允许游离的active attempt。启动前退避与未启动取消保持合法；归档路径和
归档状态仍允许原恢复流程重定位。额外目标图和当前状态已丢失时的目标图也要独立验绑定。
已迁出正文沿原副本/批准/行摘要读取，同一图按副本分组、每个数据库只保留一份解析缓存；
下一次恢复重新验证，不跨操作缓存。缺失或篡改原副本会拒绝恢复。

核对上限为每库4096图、每图16阶段、每阶段32条执行尝试、单图JSON64KiB、单阶段正文
32MiB；本次调用累计计入SQL及展开后执行事实64MiB，每库约400万SQL指令。超限保守拒绝，
没有静默跳过或截断。大规模历史的分段可恢复核对仍待开发，不能据此宣称长期容量已解决。
这些保护不迁出或删除图/attempt记录，不退还额度，不启动服务或定时任务。

专项使用小型SQLite、替代副本校验和两个原JobStore/Node票据场景，没有执行器、真实Root、
容器、设备或中断注入验收。[当前状态](../STATUS.md)区分开发完成与真实验收。

`POST /api/job-graphs` 一次接受1至16个托管工作流阶段，JSON总计不超过32KiB。每个阶段仍使用普通五类工作流的模板、封存输入、环境锁、资源、项目和预算校验；不支持本地任意命令或固定资格探针。输入须事先上传封存。任务图内所有阶段使用同一实验预算，或都不指定预算。

请求的 `idempotency_key` 由用户固定；各阶段的任务编号和幂等键由中央生成。任何阶段校验、配额、归档预留或累计预算不足，整个提交事务回滚，不留下部分任务、预算扣费或任务目录。无环关系在提交前检查，局部节点引用在同一事务内解析。每阶段最多8个父任务、32项产物交接，沿用普通依赖的校验与权限。

```json
{
  "idempotency_key": "my-build-ir-001",
  "name": "Build then inspect IR",
  "nodes": [
    {"key": "build", "job": {"template": "build_v1", "environment": "worker-python", "parameters": {"input_id": "input-REPLACE_WITH_SEALED_ID", "argv": ["python", "build.py"], "lock": {"python": "3.10.12"}, "resources": {"cpu": 1, "memory_mib": 512, "shm_mib": 0, "npu": 0}, "artifacts": {"file_mib": 1, "total_mib": 4}}}},
    {"key": "inspect", "after": ["build"], "artifact_inputs": [{"node": "build", "name": "ir/output.mlir", "target": "upstream.mlir"}], "job": {"template": "ir_v1", "environment": "worker-python", "parameters": {"input_id": "input-REPLACE_WITH_SEALED_ID", "argv": ["python", "inspect.py"], "lock": {"python": "3.10.12"}, "resources": {"cpu": 1, "memory_mib": 512, "shm_mib": 0, "npu": 0}, "artifacts": {"file_mib": 1, "total_mib": 4}}}}
  ]
}
```

`after` 是本图节点名；图节点 `artifact_inputs` 用 `node` 指定本图父阶段，其他交接字段沿用普通接口。`job.depends_on` / `job.artifact_inputs` 可引用已有的、已授权任务。`when: terminal` 可显式把已确认失败/超时且清退后的产物交给诊断阶段；默认仍要求成功。不会自动修改原失败或用新任务替代未知执行。

提交事务先整体持久保存全部任务、归档预留和预算扣费，状态为 `preparing`；此时任何阶段都不能领取或执行。中央随后根据数据库中的固定入口快照准备归档，全部准备完成后一次发布为 `submitted`，各阶段才进入原队列和依赖交接。这里保证整体准入，不保证同时执行；每阶段继续正常等待资源。图state表示准入和归档准备状态，各阶段执行状态在nodes[].state。未指定阶段名称时，中央生成图名/节点名，方便沿原网页列表识别。入口快照不执行用户代码。

在提交确认后崩溃或文件盘暂时不可用时，全部原任务编号与预留保留；后台恢复同一任务图，不重新计费。归档写入只接受与原快照相同的既有文件，内容冲突不覆盖。自动准备失败最多32次，之后 `needs_attention`；本人可显式 `POST /api/job-graphs/GRAPH_ID/resume`，请求体只能为 `{}`。准备期间不能取消单个未准备完的阶段；应先恢复归档准备，再使用原有逐任务取消接口。未确认的文件盘故障需要管理员恢复，不能靠换幂等键清除。

重复提交原图返回原编号；相同键改内容返回409。节点数组顺序不影响规范化请求，节点键与依赖决定拓扑顺序。图读取只允许原owner，实验预算凭据还必须属于该图预算。任务详情仍执行原ACL；任务图不会扩大父任务或原归档的可见范围。

独立个人 CLI / MCP：将指定的流程请求保存为本人0600文件，在本人0700对话目录保存状态，然后运行：

```bash
python graph_client.py --profile /private/client.json --request /private/approved-graph.json --state /private/conversation/graph.json submit
python graph_client.py --profile /private/client.json --request /private/approved-graph.json --state /private/conversation/graph.json status
python graph_client.py --profile /private/client.json --request /private/approved-graph.json --state /private/conversation/graph.json stdio
```

MCP仅提供无参数 `submit_graph`、`graph_status`、`resume_graph`，绑定指定流程请求，不接受模型指定命令、路径、资源或替换幂等键。提交意图先于POST保存；丢失回包时再次提交完全相同的指定请求解析原结果，不生成替代请求。状态文件与指定文件/controller不匹配时拒绝覆盖。流程文件可以由用户或其Agent准备；绑定后本对话使用原始字节，文件固定与身份权限分别校验。与模型供应商和harness无关，不复制个人模型配置或密钥。此绑定不建立同一UID内部的进程隔离。

保留历史上限为4096个图，受原控制盘容量和审计余量约束，不自动删除证据。存在任务图历史后，管理维护拒绝切换到不支持该合同的旧中央代码。当前控制状态仍在时，恢复前还核对原图的幂等键、阶段编号和执行记录，旧快照不能忘记它们；完全丢失控制状态后的最新性仍需外部备份与隔离验收。真实跨机、NPU及长期容量验收仍需单独进行；实现和当前验收状态见PROGRESS.md。
