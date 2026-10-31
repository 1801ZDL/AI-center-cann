# 仿真流水的离线分析与个人 MCP

`simulation_data.py`是标准库CPU分析载荷，复用build_v1的封存输入、环境锁、非root任务和归档合同，不启动新仿真或设备程序。`simulation_diagnostic.py`先授权绑定任务，再下载完整原始字节、校验SHA与输入封存并独立重算，向个人harness交付有限摘要。个人客户端不需要安装完整msagent、Perfetto或设备库。

支持msprof simulator的schemaVersion=1、profilingType=op的每核Chrome trace和配套instr_exe.csv，最多4核。ts/dur按明确的us解释，displayTimeUnit仅记录为显示设置；不能据ns显示标签把时长再缩放。[Perfetto格式说明](https://perfetto.dev/docs/getting-started/other-formats)。支持X指令事件，M元数据及s/t/f流和i/I即时事件计数但不计入占用；其他阶段明确拒绝。只接受有PC的完整指令记录，不从不配套的trace-view或PipeUtilization推导零占用。

输入使用固定名称simulation-input.json、capture.log、core0.trace.json/core0.instructions.csv等。manifest示例（实际SHA、core和origin均须替换）：

```json
{
  "schema":"ascend-lab-simulation-input/v1",
  "name":"历史流水分析",
  "timestamp_unit":"us",
  "origin":{"id":"capture-a","kind":"historical_simulator_output","elf_sha256":"填写64位小写SHA256","claimed_exit_code":null,"scope":"原始采集来源及已知限制"},
  "log":{"file":"capture.log","sha256":"填写原日志SHA256"},
  "cores":[{"id":"core0.veccore0","trace":{"file":"core0.trace.json","sha256":"填写完整trace的SHA256"},"instructions":{"file":"core0.instructions.csv","sha256":"填写配套CSV的SHA256"}}]
}
```

单trace最多32MiB、100000个事件，CSV最多1MiB/10000行，日志64KiB，全部原始数据最多120MiB；manifest16KiB、报告128KiB。每核顺序分析。输入仍受中央默认128MiB/包和个人预算限制，工作区需容纳输入副本和归档原件；四核历史示例应申请至少256MiB持久工作区，512MiB任务内存，32MiB单产物和128MiB总归档额度，实际准入仍看管理员策略和余量。不是完整任意规模仿真数据管理。

把simulation_data.py及数据封存为输入，提交worker-python/build_v1，argv固定为`["python","-I","-B","simulation_data.py"]`、NPU=0、input_mode=readonly，锁定实际CPU镜像/Python/环境，设置有限时限。生成results/simulation-analysis.json、CSV及results/simulation-input/完整原件。build任务passed仅表示这次离线解析和报告生成成功，capture_state与完整性另列；不能把build成功视为原采集正常退出、精度通过或性能提升。设备采集继续走simulation_v1及NPU资格和资源预留。

分析按CSV中的指令名和PC关联pipe，核对CSV call_count与全部X事件数；缺项如实标UNMAPPED/计数差异。相同PC/指令有歧义pipe会拒绝。PID可能被流水线和SIMT warp复用，只报告歧义而不据PID分类。时长使用时间区间并集，保留零时长事件数；不同pipe、内联源码位置和固定指令组可重叠，不能相加成总利用率，不能把关联源码并集解释为可删除的关键路径成本。报告只给前12指令/前8源码位置；源码路径是数据，不读取这些文件。

日志出现Child process killed by signal或Running task failed时明确标abnormal；即使同时有Profiling data parse finished也不宣称采集正常结束。没有失败字样仍为unverified；调用者填写exit0不认证完整性，和异常日志矛盾单独标出。ELF身份和历史来源为调用者声明，本次受保护环境回执只对应CPU分析。

安装轻量客户端后：

```bash
./labctl --profile /本人/private/client.json simulation-case job-实际报告任务ID
/usr/bin/python3 /本人/ascend-lab-client/simulation_diagnostic.py mcp \
  --profile /本人/private/client.json --job-id job-实际报告任务ID
```

stdio只提供无参数get_diagnostic_case；不能覆盖任务/路径、提交实验、执行源码、启动采集或释放原任务。重算前核完整字节与封存manifest，报告变更或配套源不符拒绝；原始流水不传模型。标准库CLI/MCP使用同一授权归档接口，harness无需专用插件。失败上游到分析的显式when=terminal交接沿DEPENDENCIES.md，仍受源任务及实验预算读取范围限制。

与固定msagent材料的关系：已核对的5a9b2a1a8a38f35cc63109929850aa4a46db090e的diagnose-pipeline材料列出指令CSV/源码CSV/trace等证据类型。这里针对本机实际格式编写有界适配器，没有复制或执行其整套Skill，未套用其启发式百分比阈值自动判断瓶颈。[上游材料](https://gitcode.com/Ascend/msagent/blob/5a9b2a1a8a38f35cc63109929850aa4a46db090e/skills/operator/ops-performance-tuning/references/diagnose/diagnose-pipeline.md)。最终实际验证范围见PROGRESS.md。
