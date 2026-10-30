# 节点环境认证与分配（v0.17）

中央在预留 CPU/内存/NPU 前匹配已认证的不可变镜像、Python、CPU架构、包版本及所选工具/文件 SHA256。任务版本不匹配或事实尚未采集时保持排队，不创建执行尝试。启动前的受保护环境复查仍保留；认证不能证明任意包组合、驱动 ABI 或算子精度可用。

新中央默认 `scheduler.require_environment_contract=true`，旧安装升级保持原配置，不能对在途任务强行切换。所有入口使用同一检查。Agent 只提交结构化要求，不能自行批准节点清单、放宽版本或提供镜像/挂载/Root命令。CPU固定示例不要求版本清单；五类版本化工作流要求认证。

## 管理员引导

1. 按 SERVER.md 登记不可变本地镜像和受控节点；配置专用工作区。节点需有 `environment-admission-v1` 能力，模板允许 `environment_probe`。镜像不存在时拒绝领取新任务，不自动拉取。
2. 在中央身份下提交固定探测：

   ```bash
   ./workerctl node-probe worker-xxxxxxxxxxxx --user 映射的任务用户 \
     --tool bishengir-compile --tool cc --package triton-ascend
   ```

   此任务固定 1 CPU / 256MiB / 120秒 / NPU=0，无用户命令和输入。它排队后在相同不可变镜像及环境脚本下以非Root身份采集 Python、架构、包版本、固定工具与指定不可变文件SHA。Root引导保存只读证据，用户工作负载不启动。NPU节点只读挂载登记的驱动路径，不给探测任务设备。默认也采集 triton、triton-ascend、torch、torch-npu 及常用工具；找不到记录null，未查询不等于不存在。
3. 确认成功且节点没有在途/未知任务，执行：

   ```bash
   ./workerctl node-environment worker-xxxxxxxxxxxx --from-job job-xxxxxxxxxxxx \
     --expected-revision 当前修订号
   ```

   中央核对探测来源节点、成功状态、修订号、隔离证据及当前心跳中的镜像/脚本/解释器。认证保存来源 job/attempt、规范化JSON回执SHA和已选事实，增加修订号并失效旧心跳。停止空闲节点、传送更新的私有节点配置并重启；中央机上的命令不会自动SSH部署。节点配置包含 node_key，不能放到公开仓库或交给任务Agent。
4. 节点重新确认清单指纹后，五类工作流可分配。心跳提交使用配置比较更新，认证/策略更改期间已开始校验的旧心跳不能覆盖新修订的失效状态。`GET /api/nodes` 的授权结果包含环境摘要及可复用 `lock`。普通任务启动时仍复查锁，执行尝试冻结清单指纹/修订号。未知事实要求重新探测认证，不能猜测匹配。

镜像/环境脚本/解释器不同于认证值时节点拒绝声明该环境。被选中且落在登记的宿主驱动只读挂载中的工具/文件，每次心跳及启动重新检查SHA。没有覆盖全部动态链接库、环境变量、传递依赖或管理员对宿主文件的并发修改；真实NPU运行时验收仍必须单独完成。更新镜像时应排空并重新登记/认证，不提供在途镜像迁移。

设备工作流在严格策略下还需 `lock.npu_name`，值取新鲜管理观测中的精确设备名称；UNKNOWN不可作为目标。实际分配与启动重新检查健康、占用和名称。名称不是完整硬件/固件ABI合同，也不证明非Root ACL初始化通过。

## 安装与验证边界

`servicectl doctor` 区分安装前提 `install_ready`、受控CPU任务前提 `task_dispatch_prerequisites` 和本地已配置认证的 `versioned_workflows_ready`。最后一项仍不替代中央实际确认、工具链/NPU真机验收或 `production_ready`（保持false）。新安装可先通过探测建立清单，避免要求用户任务先运行才能登记环境的循环依赖。

当前实测与失败边界见 `evidence/environment-live-validation.json` 和当前Root安装证据。v0.17常驻入口使用同一镜像；v0.18另以两个临时镜像分别选择实际GCC11和BiSheng/Clang15，完成版本认证、错误节点零attempt等待、实际C构建与中央重启验证，见heterogeneous-live-validation.json。临时镜像与节点已清理/停用，原镜像不改。这是CPU编译器选择演练，不是重建两套Triton/NPU-IR，也不是两台物理服务器验收。
# 大型 tmpfs 工作区的固定探针

固定 CPU 探针默认仍为 1 CPU / 256MiB / NPU=0。若节点只有大型 tmpfs 槽位，整个槽位容量必须计入任务内存预留，可显式使用 `workerctl node-probe 节点ID --memory-mib 32768` 对 32GiB 槽位探测。memory_mib 只接受 256–65536 的整数，仍受个人、节点、物理域预算约束；不能覆盖探针命令、CPU、挂载或申请 NPU。

该参数需要节点声明 environment-probe-memory-v1；旧节点保持排队且零次启动，已有默认探针不受影响。请求中显式固定预留值，不在重试时随节点配置重新计算。磁盘型 ext4 槽位无需因容量大而提高内存；不应将 tmpfs 作为磁盘持久性的证明。工具不存在的探针事实仍为 None，不能把它省略后当成工作负载已具备该工具；完整编译另须验证工具可运行、动态依赖和配置阶段。

# v0.34 非默认Python认证与旧记录

环境认证同时将保护探针观察到的Python可执行路径写入中央节点登记及交付的私有Node配置，和镜像、setup及清单一起发布。同为Python3.11的不同安装路径不视为同一个解释器。旧清单若与登记的Python/镜像/setup不一致，新工作流在分配前等待并提示管理员重新认证；不会自动猜测路径或中断已存在的执行尝试。

管理员使用当前节点的成功固定CPU环境探针，再在空闲节点上执行`workerctl node-environment NODE --from-job JOB --expected-revision REV`，交付生成的私有配置并重启该节点。既有的探针来源、非root保护回执、当前心跳、修订号和无在途任务要求继续生效。此检查也覆盖持久缓存namespace生成，避免普通任务能领取而缓存任务因缺失解释器登记反复返回协议错误。

