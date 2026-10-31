# 固定输入、环境锁与五类工作流

可用预算profile将多个固定工作流会话绑定到同一累计实验预算。中央自动绑定，普通个人JSON也可在顶层指定experiment_id；不进入parameters，不改变容器引导/环境锁合同。失败和取消不自动退款，任务幂等重试不再次扣费，见EXPERIMENT_BUDGET.md。

托管设备工作流在环境指纹检查之前先核对非root任务身份、单卡设备曝光及原始设备打开情况；失败返回78、归档受保护回执并保留`npu_device_access`阶段，用户程序不启动。结果合同也要求匹配实际分配卡的受保护访问证据，详细范围见 [NPU.md](NPU.md)。CPU构建/IR不打开NPU设备；设备文件可打开仍不等于ACL或数值验收。

v0.17：中央环境认证与资源预留前的版本匹配见 [ENVIRONMENTS.md](ENVIRONMENTS.md)。新安装默认启用；旧配置需在空闲节点认证并确认后显式启用。

2026-10-29，v0.7。build_v1 / ir_v1 已完成真实容器编译与 BishengIR 处理验收。accuracy_v1 / performance_v1 / simulation_v1 已有输入、资源与结果合同；当前受控节点不透传 NPU，它们会等待并且不分配设备，不代表设备工作流已验收。

## 提交与复现

使用本人 profile，通过独立安装的客户端上传源码和输入文件。上传目录默认排除隐藏目录/文件和 __pycache__，拒绝符号链接与特殊文件。清单包括相对路径、大小、内容 SHA256 和可执行位。中央只在声明的大小/哈希全部匹配后封存，封存后的内容不能覆盖；同用户相同清单重试复用输入 ID。

```bash
./labctl --profile /本人/client.json input-add /本人/算子输入目录
./labctl --profile /本人/client.json input input-替换为实际编号
./labctl --profile /本人/client.json submit --workflow /本人/ir-request.json
./labctl --profile /本人/client.json wait job-替换为实际编号
```

工作流请求示例：input_id、镜像/依赖等须替换成实际环境。执行目录是输入的私有可写副本 /work/source-tree，原 /inputs 挂载只读。argv 是容器内参数数组；它不能选择宿主路径、镜像、UID、特权标志或设备挂载。

```json
{
  "template": "ir_v1",
  "environment": "worker-python",
  "name": "离线 IR 处理",
  "parameters": {
    "input_id": "input-REPLACE_WITH_REAL_ID",
    "argv": ["python", "ir.py"],
    "resources": {"cpu": 1, "memory_mib": 512, "shm_mib": 0, "npu": 0},
    "timeout_seconds": 90,
    "lock": {"python": "3.10.12"},
    "result_file": "results/workload-result.json"
  },
  "idempotency_key": "my-ir-run-001"
}
```

Python-only 锁只是最小合同。用已确认的基线工作流生成完整的**已观测环境锁**，再放入下一次请求的 lock 字段：

```bash
./labctl --profile /本人/client.json environment-lock job-实际基线编号 --output /本人/toolchain-lock.json
```

完整观测包括实际 Python、镜像 ID、Triton/torch/torch-npu 包版本、发现的编译工具实际路径/版本输出/SHA256、CANN setup 脚本，以及已安装 Triton Ascend 后端 compiler.py（若能从包元数据定位）。`__absent__` 明确锁定某个包未安装。也可在 files 中显式加入其他容器内文件的绝对路径与哈希。CANN/LLVM 的其他库、构建参数、未包含的工具与依赖不因有一份锁就自动获得兼容性保证。

中央还固定本次引导器、环境探针、归档器和参数合同的代码哈希。节点版本不符时拒绝执行；具备相应能力的节点才领取新工作流。升级仍需要保留匹配版本或先排空任务，当前不提供自动迁移正在执行的程序。

环境探针在用户工作负载前、以任务 UID 运行；Root 引导器将结果保存到任务 UID 无法修改的 /receipt。不匹配则退出 78，不启动用户工作流入口。只读辅助容器在 PID 命名空间确认结束后导出受保护记录，位于 `results/_lab/environment.json`。这与用户程序自己写的日志/断言不同；用户输出不得占用任一类别的 _lab 保留目录。

## 网页提交同一份工作流请求

网页“托管作业 → 提交方式 → 版本化工作流”支持上述五类模板。输入仍由本人CLI的`input-add`上传并封存；浏览器载入或粘贴与CLI相同的完整请求JSON，不读取服务器源码目录、不上传所选JSON以外的本机文件。浏览器导入文件最大256KiB，实际序列化请求仍受中央32KiB接口上限约束；必须明确`environment=worker-python`及1–120字符的`idempotency_key`。普通命令表单的名称、项目、工作目录和资源不会覆盖JSON；请求核对区显示输入、资源、工具链锁、结果位置及所声明依赖/缓存等。

导入和“下载当前请求”不提交。先用“检查可运行节点”查询当前条件，确认后再显式“提交到队列”；预览无预留且不保证启动。中央仍检查封存输入权限、身份、环境及资源，前端核对不能替代该校验。相同请求/幂等键返回原任务；如果提交响应丢失或刷新页面，重新导入原文件并沿用原键，不换键重新计算。修改已提交内容但复用原键会被拒绝。新实验应使用新请求及新键。

浏览器保留中央规范归档和结果/CSV下载，不在刷新后自动保存含路径的请求，不替个人CLI配置原脚本同级归档。需要持续自动下载时仍用CLI/MCP的个人归档选项，见CLIENT.md。JSON与下载文件可能包含私人路径或命令，应由本人保存。仅查看凭据无法提交或预览；任务执行仍由受控Node决定，不把浏览器作为执行者。

## 输出合同

程序可读取 LAB_JOB_DIR、LAB_RESULTS_DIR、LAB_IR_DIR、LAB_PROFILE_DIR、LAB_RESULT_FILE；分别指向本任务的 /work、results、ir、profile 和指定结果 JSON。Triton cache/dump、临时目录与编译并发使用本次获批额度/目录。msprof、仿真工具或构建脚本仍需按其接口显式设置输出路径；平台不会假设工具的任意默认目录都已经被收集。

所有结果文件都需要 schema、kind、passed。退出为 0 仍须满足对应合同：

| 模板 / kind | 资源约束 | 必需的结果证据 |
|---|---|---|
| accuracy_v1 / accuracy | 显式 NPU 申请 | 非空 checks；每项 passed=true、有限非负 max_abs_error/atol/rtol |
| performance_v1 / performance | 显式 NPU 申请 | 3–10000 个有限正值 samples_us，以及非空 conditions；中央从原始样本计算均值/中位数/nearest-rank P95 |
| simulation_v1 / simulation | 显式 NPU 申请 | artifacts 指向 profile/ 下实际存在、非空的普通文件 |
| build_v1 / build | NPU=0 | artifacts 指向 results/ 下实际存在、非空的普通文件 |
| ir_v1 / ir | NPU=0 | artifacts 指向 ir/ 下实际存在、非空的普通文件 |

构建例子：

```json
{
  "schema": "ascend-lab-result/v1",
  "kind": "build",
  "passed": true,
  "artifacts": ["results/package.whl"]
}
```

这些数值/功能断言由工作负载提供。中央核对受保护环境、真实退出状态、结果格式、原始样本与所指产物，但不能据此独立证明任意算子算法正确或性能提高。完整精度参考、shape/dtype/seed、频率/健康/独占状态、AB/BA 基准等还需要在实际算子模板中补齐。仿真合同也不等于已验证任意流水格式可解析或工具正常退出。

## 归档与网页

原始日志、结果、环境和 IR/profile 由节点通过 HTTP 回传，并校验字节与 SHA256。归档只读取普通单链接文件，拒绝符号/硬链接和特殊文件。过长或不适合传输的路径改为 `ir|profile|results/_lab/files/<路径哈希>.<后缀>`，映射保存到 results/_lab/path-map.json。原结果中的长路径不改写，中央合同核对通过映射定位文件。`artifact-hashes.json` 保存校验过的节点文件清单。

网页“任务结果”提供类型筛选、分页、详情与 CSV；沿用任务/项目 ACL，CSV 对公式前缀转义。validated-result.json 是中央生成的记录，原结果 JSON、日志及长路径映射都可下载。网页沿用控制器上的规范归档；个人CLI/MCP现支持本地原脚本同级归档和归档偏好，详见CLIENT.md；浏览器不代替个人归档客户端。大体积对象存储仍待。

## Agent 与其他 harness

在用户已上传输入并准备请求后，可把一个请求绑定给 MCP 会话。模型只能提交该请求，不能覆盖命令、资源、源码、环境锁或模板；每个会话最多一个任务。客户端通过 `/api/jobs/validate` 预先确认本人权限与合同，不读取中央身份库。

```bash
python3 /独立客户端/mcp_bridge.py \
  --profile /本人/client.json --session-dir /本人/私有状态/会话001 \
  --environment worker-python --workflow /本人/ir-request.json
```

Demo 的授权 OpenCode 启动器同样支持：

```bash
./agentctl run --profile runtime/personas/demo_bob/client.json \
  --environment worker-python --workflow /本人/ir-request.json
```

真实 DeepSeek 调用 agent-9035725fc2b4 / job-552f8ae50a73 完成了已绑定的实际 BishengIR IR 工作流。独立安装的 MCP 也验证了进程重连复用同一实际 IR 任务，无 OpenCode/模型依赖。模型和技能依旧由个人 harness 管理；本 Demo OpenCode 演示器的 zdl 模型读取不属于通用客户端。

当前上限：128 个输入文件、64 MiB/文件、128 MiB/输入包；每用户默认 32 包/512 MiB。输入支持分块连续偏移重试，未封存文件可由本人通过 reset-file API 清除后重传；尚无自动 GC。输出每文件 64 MiB、总量 128 MiB，并限制文件数量以给受保护证据留出空间。大源码可显式作为归档文件输入，但展开、构建依赖、工作盘硬配额与大 wheel/完整 IR 仍须下一阶段设计验收，不能把小型编译成功当作完整 Triton/NPU-IR 包构建通过。
# 文件系统工作区要求（v0.12）

parameters可增加`"workspace":{"min_mib":10240,"min_inodes":100000,"durable":true}`。需要中央/节点filesystem-workspace-v1能力：按容量、inode和持久性选择独占文件系统槽位，无法满足则等待，不退回普通目录。要求持久的工作流不会分配tmpfs；tmpfs整槽大小必须计入任务内存预算。硬边界覆盖/work及其中cache/tmp/HOME，不扩大输入和归档额度，也不表示完整包构建已验收。管理员准备、合同及实际证据见WORKSPACE.md。
# 大产物申请（v0.13）

parameters.artifacts为`{"file_mib":1280,"total_mib":1280}`等有限额度，默认64/128MiB。中央先验证管理员上限和个人留存预算，再冻结到job/grant.artifact_limits。节点需要匹配的IO合同和大产物能力，导出/上传/个人下载全部按该额度校验。工作区与内存仍独立申请，不能用产物额度代替工作盘或构建预算。已有wheel传输演练不等于完整包构建，详见STORAGE.md。

## 私有持久缓存（v0.31）

parameters可显式增加`"cache":{"mode":"persistent","min_free_mib":1,"min_free_inodes":8}`，需要已登记缓存政策和认证环境的Node。中央只分配本人的缓存槽，忙碌/满额/失挂载时等待，不借用他人或退回工作区；未请求时保留原工作区缓存。Node核对实际/cache挂载后设置TRITON_CACHE_DIR=/cache/triton和CCACHE_DIR=/cache/ccache，且只在实际确认容器结束并清理工作区后释放租约。CLI工作流JSON和固定请求MCP可携带该字段，不允许模型覆盖会话已批准的请求。缓存内容可被本人修改，不替代环境锁或原始结果。具体安装、Root GC、恢复和当前验收状态见服务端CACHE.md。

## 阶段依赖与封存产物（v0.32）

诊断失败或超时任务时，artifact_inputs可逐项显式声明`"when":"terminal"`，其余条件仍默认success。中央确认原attempt已终止、清退及归档完整后，交接冻结的来源事实和文件摘要；等待阶段不预留计算资源。固定MCP保留批准的JSON，不能临时覆盖来源；诊断任务仍使用自己的预算/环境，设备任务仍须NPU资格。独立客户端附DEPENDENCIES.md，详细条件及恢复/权限边界见该文档。

工作流JSON顶层可增加`"depends_on":["job-0123456789ab"]`及`"artifact_inputs":[{"job_id":"job-0123456789ab","name":"results/package.whl","target":"deps/package.whl"}]`。父任务必须已存在、同owner且为托管版本化工作流；可先后提交形成链、分叉和汇合，不能给旧任务加边。基础parameters.input_id必须预先封存；中央合成新输入，记录生产者job/attempt与SHA并完成封存后才开始分配下游资源。CPU构建阶段npu0，设备阶段等待自己的NPU能力/资格，绝不提前借用生产者的卡。

上游失败会把未启动的后继记为cancelled，结果表明确“未执行”，不伪造一次精度失败。交接不足空间时保留进度；完整性错误停止交接，原绑定不变。`labctl --profile /本人/client.json retry-inputs job-ID`只对本人未启动任务按原摘要继续。当前离线备份仍要求排空所有等待和活跃任务；进程重启恢复交接不等于已支持进行中状态的离线快照。详细存储、回收保护和限制见服务端DEPENDENCIES.md。

## 托管原生离线编译示例（v0.34候选）

`examples/workflows/native_toolchain.py`通过普通`build_v1`托管工作流执行：每次建立新的CMake/Ninja目录，运行C++校验程序，并使用明确目标的公开`triton.compile`生成NPU ELF及各级IR，不加载或运行NPU kernel。日志、编译命令、实际工具摘要、缓存计数、ELF、元数据和阶段产物进入既有归档及结果表。

管理员先固定任务镜像、Python、环境脚本，并用受保护CPU探针认证实际包与文件。版本号相同可能是不同后端；锁应包含包内compiler.py/driver.py、实际triton-mlir-opt与BiShengIR工具、兼容审计文件的摘要。PATH里的同名工具不代表Triton实际调用的工具。

示例任务申请CPU=1、memory_mib=2048、npu=0；显式缓存请求`{"mode":"persistent","min_free_mib":8,"min_free_inodes":700}`，工作盘至少12MiB/256inode且持久。管理员还应按实际工程扩大工作/缓存硬配额。原生提供器实测一个环境约570inode，旧小IR例子的128inode配额不足。这里的缓存参数是该固定演练的最低要求，不是任意模型/算子的容量预测。

将脚本上传并封存为本人input_id后，使用`argv=["python","native_toolchain.py","--source-variant","0"]`提交。`--source-variant 1`在同一缓存namespace内改变C++源和Triton constexpr，验证源码缓存键；`--hold`仅用于有界重启演练。个人CLI或固定工作流MCP继续使用同一请求接口。不要把CPU构建耗时或缓存命中率记为NPU算子性能。

私有离线镜像对明确compile-only且未设置设备核心限制的路径省去硬件查询；显式NPU_DEVICE_LIMIT拒绝离线推断，并保留正常在线分支。CANN9.1不支持的关闭态metaop参数仅在实测工具不支持时省略，原wheel与共享安装不修改。具体补丁摘要保存在镜像`/opt/ascend-lab-offline-compat.json`并纳入环境锁；这不是设备、数值或在线运行时验收。


## 大 IR 解析复现

compiler_compatibility.py使用compiler-case.json schema=ascend-lab-compiler-input/v2时允许最多8 MiB输入与输出；仍仅调用固定版本工具进行parse/verify。普通ir_v1请求需要为完整输入、IR副本和输出申请足够的工作盘与产物额度，不因schema自动扩容。只读诊断提供完整SHA校验和有限预览，不能据此声称完整lowering、数值或设备通过。详见COMPILER_DIAGNOSTICS.md。

## 历史精度数据离线重放

`accuracy_data.py`使用明确的actual/reference二进制数据与政策，通过普通build_v1的固定只读输入运行。差异非零时任务failed，原数据、结构化比较和CSV仍保留；匹配仅证明对该参考的比较结果。独立accuracy-case CLI/MCP可核封存摘要并重算，见ACCURACY.md。执行Node须明确登记build_v1。新的设备精度任务继续使用accuracy_v1及NPU资源和资格；本配方不执行设备代码。
