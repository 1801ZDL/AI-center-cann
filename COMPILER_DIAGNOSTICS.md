# 编译诊断证据与受控验证

新增可独立分发的单任务编译证据读取器，固定读取编译报告、完整小IR和有界编译器stderr，按原服务端ACL授权，不执行修改、提交或设备操作。实际验证状态见PROGRESS.md。

`examples/workflows/compiler_compatibility.py` 是普通ir_v1工作负载。封存输入包括该脚本、input.mlir和compiler-case.json；配置只选择显式镜像中的triton-mlir-opt或bishengir-opt绝对路径及SHA。实际工具必须与请求中已认证的environment lock.files哈希相同；调用固定为parse/verify和输出IR，不接受插件或额外参数。失败仍归档报告/输入/诊断并退出非零；成功必须产生真实IR并满足原结构化结果合同。这里的解析通过不代表完整lowering、数值等价、性能或设备正确性。

个人端使用`labctl --profile PRIVATE compiler-case job-ID`，或以`python3 compiler_diagnostic.py mcp --profile PRIVATE --job-id job-ID`提供单任务get_diagnostic_case。原diagnose与性能报告接口保持原合同，固定性能镜像的四个源文件无需因此改变。

候选修改只能作为待审数据。原始source SHA、完整未脱敏源、已结束且清理确认的原attempt是精确补丁验证的前提；未知状态保持原租约，不重提。修改后的副本必须重新封存，并通过中央队列执行一个明确预算的新任务，原失败记录保留。模型输出不是执行授权，也不等于已验证修复。

## 版本化输入与请求

输入目录只需三个文件：当前客户端附带的compiler_compatibility.py、UTF-8 input.mlir（1..8192字节）和compiler-case.json：

```json
{
  "schema":"ascend-lab-compiler-input/v1",
  "tool":"triton-mlir-opt",
  "path":"/管理员固定镜像中的/triton-mlir-opt",
  "sha256":"实际二进制的64位SHA256"
}
```

工具仅限triton-mlir-opt或bishengir-opt，路径需为规范的/usr、/opt或/data镜像路径且basename匹配。工作负载不接受额外pass、插件或shell命令。工具文件的相同SHA还必须存在于请求环境锁files中；先按ENVIRONMENTS.md通过固定environment_probe查询并由管理员确认，不能用自行填写的版本号代替镜像资格。本机复用此前固定工具镜像，没有重装共享环境。

将目录经原input-add接口封存后，通过原submit --workflow提交：

```json
{
  "name":"编译兼容性复现",
  "template":"ir_v1",
  "environment":"worker-python",
  "idempotency_key":"本人本次复现唯一键",
  "parameters":{
    "input_id":"input-封存返回的ID",
    "argv":["python","-I","-B","compiler_compatibility.py"],
    "lock":{"python":"实际精确版本","image_sha256":"sha256:实际镜像ID","files":{"/实际工具路径":"实际工具SHA"}},
    "resources":{"cpu":1,"memory_mib":512,"shm_mib":0,"npu":0},
    "timeout_seconds":60,
    "input_mode":"readonly",
    "workspace":{"min_mib":8,"min_inodes":128,"durable":true},
    "artifacts":{"file_mib":1,"total_mib":2}
  }
}
```

示例中的占位符必须替换为真实封存ID和受保护证据导出的锁。实际调用是工具输入与-o输出，默认解析/验证；不因某个工具接受--canonicalize，就假定另一个同样接受。工作负载在归档中保存原小IR、驱动SHA、工具版本/文件SHA、实际argv、有界stdout/stderr和输出摘要。退出非零仍为失败；不是把捕获到错误当作工作流成功。

## 读取与候选边界

compiler-case先经过原任务ACL，再读取固定归档文件并核对完整长度、块SHA、源码SHA；IR必须与中央记录的封存input.mlir一致，驱动SHA必须与封存驱动一致。未结束任务的归档继续按原接口拒绝；可用原diagnose读取有界活动日志，不能据观察超时重提。

case另外给出workflow_binding：当前客户端是否认识该驱动版本，以及实际工具SHA是否同时匹配请求锁和受保护环境回执。旧驱动的报告可以保留为证据，不能自动进入当前已知配方的候选检查。完整小IR、字段和任务元数据都会脱敏；source_redacted=true时不能对脱敏后的文本做精确补丁。

review_minimal_rename仅检查一个由主助手事先建立依据的单处字面替换实验：原任务必须明确failed/terminal，退出/执行清理/工作区释放均确认；源码完整未脱敏且SHA一致，驱动为客户端已知版本，工具锁有受保护确认。候选必须绑定原SHA和目标工具，不接受命令、目标覆盖、任意diff、批量替换或数值/设备/全流水已通过的声明。函数仅返回候选文本和摘要，不提交或执行。它不替代主助手对错误阶段、迁移依据和目标版本的判断。

模型仅可得到已核对的最小证据。本机演练沿已有授权的DeepSeek配置调用一次，所有工具deny、MCP为空；输入为实际错误、IR、配对解析观测和已批准的单处替换依据。模型产出是待审草稿，不是未知bug的自主根因确认。主助手核对后，在新的输入副本中应用改动、重新封存并走中央队列。其他harness可使用相同只读MCP和队列API，不需要共享模型密钥或会话。

## 本机实际案例及限制

所测BishengIR2.0.0/LLVM19.1.7接受bufferization.to_memref，而Triton工具LLVM22.0.0git拒绝它；to_buffer的接受关系相反。LLVM上游有对应的[NFC重命名提交](https://github.com/llvm/llvm-project/commit/8f91b108df21bee8ee64e88ffa7bdee230389800)。本例使用带引号的generic operation，避免把新旧自定义打印语法的变化混为一谈。

受控演练保留四个单attempt任务：旧工具接受原IR；新工具明确失败；经主助手批准的候选在新工具通过；候选在旧工具仍失败。恢复后原失败证据可读、同一封存候选可再次验证。具体job、源码/工具SHA及最终检查数见PROGRESS.md。这说明修复必须绑定消费者版本，不能把新拼写推广到所有环境。

初次预探还发现所测triton-mlir-opt不接受--canonicalize，该次错误发生在参数解析阶段；后续改为不加pass的解析实验后才取得上述IR接受矩阵，旧错误单独保留。没有把它当成IR语法错误，也没有据工具的模糊建议擅自替换其他选项。

这是结合已有工程背景构造的最小IR复现，使用真实固定编译器；不是修复原始大算子、完整lowering、数值正确性、NPU故障或性能验收。msagent的[版本对齐参考](https://gitcode.com/Ascend/msagent/blob/5a9b2a1a8a38f35cc63109929850aa4a46db090e/skills/operator/ops-performance-tuning/references/compile/compile-triton.md)强调按目标release核对配套；其[msDebug技能](https://gitcode.com/Ascend/msagent/blob/5a9b2a1a8a38f35cc63109929850aa4a46db090e/skills/operator/msdebug-basic-usage/SKILL.md)明确不覆盖Triton JIT，本例没有执行该调试器流程或宣称安装完整msagent。


## 完整大 IR 与有限诊断预览

新的独立客户端声明`compiler-large-ir-v2`。同一配方可将compiler-case.json中的schema明确设为`ascend-lab-compiler-input/v2`，其余字段保持不变；输入和单个输出IR上限各为8 MiB，原v1仍为8192字节。任务仍须单独申请输入、产物、内存、工作盘和时限，schema不会扩大管理员或个人额度。工具调用仍只做固定parse/verify，不新增pass、插件、shell或设备访问。

驱动分别排空stdout/stderr，只保留各前8192字节并记录读到的字节数；输出文件有子进程文件大小限制，固定20秒编译时限覆盖等待和管道读取。超时会结束本次驱动创建的编译器进程组；任务及其后代的最终清退仍由原Node执行器确认。超时、输出截断、异常退出都不能当作正常编译错误的修复依据。

诊断端经原任务ACL读取完整归档，按最多256 KiB的块核对名称、偏移、总长度和块SHA，最终再核对报告与封存input.mlir的完整SHA。完整IR读取量有8 MiB上限；不扩大MCP返回额度。大于8192字节时返回`ascend-lab-compiler-diagnostic/v2`、`source_ir=null`、`source_complete=false`及`source_verified_bytes`，只附经过脱敏的首尾预览和最多四处报错附近的有限行。先对完整有界文本脱敏再取预览，避免传输块或截取边界暴露凭据。

完整字节经过校验，不等于完整源码已交付给模型；`review_minimal_rename`拒绝这种大IR预览。需要修改时，使用本人已认证的归档下载接口取得原文件，在新的本地副本中审查修改，再封存并申请新的预算内任务。只读编译MCP仍只有原绑定任务的空参数getter，不接受任意路径或执行请求。真实历史算子IR只作为私有输入，公开报告仅保留SHA、长度、消费者工具和结果，不公开源码或部署路径。

成功编译可能没有stderr。原归档器省略零字节文件；若报告声明stderr_bytes=0且归档无该条目，诊断明确返回compiler_stderr_archived=false、compiler_stderr_sha256=null及声明来源，不伪造已归档空文件的SHA。报告声明非零而文件缺失时仍拒绝；没有归档错误证据不能授权既有的单处重命名实验。
