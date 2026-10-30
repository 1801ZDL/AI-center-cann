# NPU 接入与实机验收


2026-10-31：开户总协调器已可接受显式device_probes候选计划，逐张提交固定探针并沿原job观察；--adopt-probe只采用管理员已成功的新任务，不启动重试。成功采用会在中央事务中禁止旧授权继续提交，保留原失败与新旧任务关系。候选卡数可大于同时使用额度。操作、观察到期和finishing阶段的区别见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。此前该编排阶段676项回归及31项编排检查通过；NPU正向仍是合成观测，0新kernel，实际设备/ACL/数值/性能/仿真未验收。


## 追加无关用户后的设备资格兼容

中央的qualification_compatibility.py只处理一种变更：已批准且完成绑定/ACK的Node，按原计划追加一个新用户和各一个原生工作/缓存槽。它逐项证明所有旧UID/GID、旧槽及其他执行字段保持原值；镜像、Python/CANN环境、设备策略、物理域和资源策略不允许夹带改变。全局policy_revision仍递增，并继续进入受保护的设备访问context，没有删除版本校验。

批准时在jobs→identities写保留内保存原资格、原成功固定probe、旧完整profile及新profile摘要、原用户单调安全修订。只有已启用、允许NPU的个人用户可成为候选；新增用户、仍禁用的stage、失败/过期或来源不匹配的资格不会继承。正常stage批准与已激活用户扩Node授权均使用同一规则。

Node确认原ready交接并产生新鲜、健康心跳后，中央核对当前boot/驱动/Docker指纹、六个受保护运行文件、原用户权限修订及原资格是否仍相同，再核算旧context与新context。条件全部成立才关联到新配置；原job、attempt、receipt、recorded_at和expires_at不变，不提交新probe、不延长30分钟有效期。记录保留probe_context_sha256及兼容意图指针，连续追加用户会保留此前审计链。资格仍只证明设备文件访问，不代表ACL初始化、数值、性能或仿真通过。

首次有效观察发现权限、指纹、资格来源或有效期不匹配时，保存skipped历史，之后恢复原值或重复心跳不会重新继承，需显式新探针。等待其他同域Node的当前库存对账时保留pending；期间配置变更、离线维护/备份被阻断，正常心跳和中央重启可继续恢复。资格变更、完成审计和Node观察位于同一事务；响应丢失重放不续期、不重复写入。备份恢复仍使verified资格过期，已完成的兼容记录不会将其复活。

本地服务管理员可读取分页审计摘要，不能通过此入口重发许可或修改记录：

```bash
workerctl --controller-state /var/lib/ascend-lab-controller \
  node-qualification-compatibility worker-替换为实际NodeID --limit 20 --offset 0
```

输出是历史计划及carried/skipped计数；当前能否用卡仍以node-device-status及正常任务准入为准。完整旧profile只保存在中央私有数据库，不通过摘要输出。计划最多1024条、每计划最多4096个候选且记录预留不超过2MiB、总预留128MiB；超限要求管理员明确保留/归档后再推进，不自动删历史。这是应用元数据上限，不是整个控制盘/SQLite的内核硬限额。存在兼容记录后拒绝切换到缺少该模块的旧中央包。

本阶段设备正向来自合成可信协议，CPU Root安装演练只验证原资源/交付恢复和已安装审计CLI的空历史；真实NPU沿用资格仍待硬件验收。完整检查和证据范围见PROGRESS.md。

## 待预配用户的管理员固定探针（2026-10-31）

NPU用户激活要求本用户在所需Node/卡上的有效访问资格；此前禁用且无凭据的stage不能提交探针，形成了预激活死锁。新增**仅中央本地服务管理员**的`workerctl owner-device-probe --spec`，不向HTTP/MCP开放管理操作，也不临时启用账号。它为原stage授予一次固定npu_access_probe任务，仍使用中央队列、本人额度、物理卡账本、Node工作盘、有限暂存和受保护回执。

前置：中央已加载staged-owner-device-probe-v1；对应Node已显式登记NPU设备合同及npu_access_probe模板，原用户绑定/ACK完成并已观察到健康心跳；中央原生存储组、实际服务附加组、认证环境、工作/缓存/暂存和预算都须就绪。npu额度至少1，memory_mib不能超过stage额度。此入口不改驱动、权限、可见编号、Node模板或个人登录状态，不自动探测/重试。

服务UID所有0600请求示例：

```json
{
  "operation_id":"0123456789abcdef0123456789abcdef",
  "owner":"researcher_a",
  "stage_id":"替换为中央64位stage摘要",
  "node_id":"worker-0123456789ab",
  "plan_id":"替换为原Node提案64位摘要",
  "device_id":0,
  "memory_mib":256
}
```

```bash
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- \
  workerctl owner-device-probe --spec /var/lib/ascend-lab-controller/researcher-a-probe.json
```

请求只含明确身份/计划/卡/内存，不接受命令、输入、挂载、依赖或自选项目。固定申请1CPU、单卡、零SHM、120秒、单文件1MiB/总产物2MiB；正常归档元数据预留另计，不能因为是管理探针绕过本人存储预算。用户保持disabled/password不可用/token数0。返回中央job ID，管理员沿原任务观察/取消，不因观察超时重提。原node-device-probe只接受有效启用身份；待预配用户必须走本入口，不能把空身份回落成服务管理员。

同operation_id重试只恢复原授权及同一job；完成或已提交请求的重放为历史，不重新上卡。请求改变拒绝。资格过期或原授权失效后要重新验证，须显式新operation_id；旧任务与结果保留；失效且仍排队的原任务应由管理员明确取消，以释放其等待名额和按留存规则处理归档额度。对stage执行user-disable可使单调身份修订变化，从而撤销原探针授权；恢复验证须明确新operation_id。授权历史上限4096条，达到上限要求明确留存处理，不自动清空审计。原stage/profile、单调身份修订、Node全profile/原ready handoff、中央storage以及受保护host/runtime访问context全部冻结。领取、启动许可和资格结果提交时复核，身份改后恢复原值也失效。启动后失效不凭此释放卡；仍须正常终止/清退。成功结果不能跨失效授权授予资格；激活前还会再次验证该资格来自仍有效的原stage探针。

此检查范围仍是**设备文件访问**。即使通过，也不证明ACL初始化、运行时逻辑/物理编号、数值、性能或仿真通过。当前协议正向使用合成Node观测与受保护格式回执；真实安装CLI另有CPU额度拒绝路径，尚无本轮新增NPU硬件正向。现有共享主机的EBUSY没有新环境事实，不重复上卡、不改旧容器/驱动。


2026-10-30：旧协作式宿主复查通道支持长任务目录。宿主与容器客户端通过私有目录 FD 的 `/proc/self/fd/<fd>/npu-admission.sock` 寻址，实际 socket 仍位于该任务目录、权限 0600，并验证对端 UID、执行身份、token 及一次性回执；不创建公共 `/tmp` 端点。清理绑定原目录 FD 和 socket 身份，保留后来替换目录中的对象。真实 260 字节宿主路径的跨容器握手使用合成设备观测，只验证通信/身份/清理，不是硬件准入或 NPU 计算验收。新修复的检查范围见 PROGRESS.md，历史 Root 安装验收不自动覆盖此改动。

## 设备文件访问资格与调度

v0.27新增固定`npu_access_probe`，通过原中央API申请1CPU/默认256MiB/1张指定卡，最长120秒，仍受本人额度、整机预算、工作区、设备健康/占用与结束清退规则约束。参数只有node_id、device_id和可选memory_mib，不接受用户命令或挂载；不会初始化ACL或启动算子。资格失败或未知不会触发自动探针。

普通精度/性能/仿真任务只选择**本用户、该节点、该卡**具有有效设备访问资格的候选卡，未验证、已失败、环境变化、过期或节点版本不符时保持排队，不预留CPU/NPU/工作区、不产生执行尝试。CPU任务及固定探针可继续按原预算调度。任务启动时仍重复执行设备访问检查；任何托管工作流的受保护设备访问失败会把同条件的资格记为blocked。只有新的成功固定探针能恢复verified，普通程序的输出和退出0不能授予资格。

资格绑定执行UID/GID、物理域/节点/卡及可见编号合同、固定镜像、已声明环境指纹、节点修订、受保护runtime源码、Docker绑定、主机boot摘要及两个受保护驱动文件摘要（driver/version.info、driver/lib64/driver/libascend_hal.so）。驱动文件不存在也被记入指纹；这不是完整驱动/CANN兼容认证，未覆盖的文件变化或内核模块热重载仍需管理员维护时重新验收。成功资格有效30分钟，到期必须显式续验；失败不会因时间经过自行放行。主机信息不可核对时暂停普通设备工作流。升级后节点须匹配新的`npu-access-qualification-v1`及runtime清单。

中央只接受上传SHA核对、终止/清理确认后的Root保护回执；保存本人可追溯的源job/attempt和摘要。网页仅展示按执行身份×卡汇总的未验证、已验证、已失效和失败阻断数量，不公开他人的资格任务、UID或回执。资格记录在SQLite持久化；离线备份恢复会令已通过资格过期，避免从旧备份恢复已被后来撤销的放行状态；失败记录保留。

管理员在节点登记和CPU环境认证后显式开放固定探针模板，使用已有节点配置传递/空闲重启流程，再执行：

```bash
./workerctl --controller-state /中央私有状态目录 node-device-status worker-ID --user alice
./workerctl --controller-state /中央私有状态目录 node-device-probe worker-ID --user alice --device 0
```

模板未登记、卡未授权或本人额度不足会拒绝/等待，不修改驱动、用户组或旧容器。新中央和节点需一起升级；旧npu_probe仍是原固定诊断，不授予新资格。设备访问资格通过后，ACL初始化、运行时编号/未获批卡隔离、精度、性能、仿真以及取消后的清退仍须分别验收；网页不会将这些标为通过。

## 托管工作流的任务身份检查

精度、性能、仿真工作流在执行用户程序前，由受保护的环境探针以任务UID检查单张获批卡：非root、零effective/permitted capability、NoNewPrivs、配置的运行时可见编号、容器只曝光获批设备文件，并以O_RDWR/O_NONBLOCK/O_NOFOLLOW打开字符设备后立即关闭。CPU编译/IR和固定CPU环境认证不执行这项设备检查；独立npu_probe模板仍执行其原有固定设备诊断。

检查失败时环境探针返回78，Root引导器保存在`results/_lab/environment.json`的受保护回执包含`device_access`、`failure_stage=npu_device_access`、`locked=false`和`workload_started=false`。节点归档该回执并分类为设备访问失败；不会启动用户程序，也不自动改权限、改镜像或重试。中央结果表/CSV保留失败阶段；新的设备工作流只有同时具备获批卡、非root身份的受保护访问证据，才可通过成功结果合同。

这只是启动条件。打开设备成功不证明ACL初始化、运行时物理/逻辑编号、未获批卡的全部访问路径、精度或性能；节点结束后的设备清退和资源释放规则保持。旧协作式zdl执行入口不是这项受控容器保证的适用范围。旧节点需升级并匹配新的受保护runtime文件哈希；不能只升级中央就认为旧执行器也具备该检查。

2026-10-29，v0.10。中央/节点的设备合同和准入代码已接入，数值/性能/仿真实机验收尚未完成。不要将设备管理读数正常、文件已挂载或固定探针成功当成完整算子通过。

2026-10-30只读补查：官方针对部分A2等产品的案例说明，`-8020`也可能涉及普通容器/不同mount namespace持有设备，不能仅按瞬时利用率解释。该案例未直接覆盖本机A5，现阶段只作为排查线索。[官方案例](https://www.hiascend.com/document/caselibrary/detail/topic_0000002238354198)

本机`/proc/uda/namespace_node`存在，宿主UID1104读取被拒，自有Root只读诊断可读取。v0.22首次记录未识别布局；2026-10-30已增加有界聚合解析，接受数字及文本identify，公开结果只含计数、字节数和SHA，不导出原始行、持有者PID或标识。解析结果不用于空闲判定、资源释放或自动故障归因。管理员试点仍需按实际芯片/驱动校准命名空间注册与设备持有关系；当前共享服务器不停止既有容器、不改驱动或权限，也不把Root管理查询成功当作非root计算通过。

## 2026-10-30 补充诊断：尚未通过NPU运行时验收

以下v026身份/命名空间文件来自v0.25之后的独立诊断，本身不证明服务发布或设备计算成功；服务验收另见对应validation文件。

- 队列作业 `job-4a04686e9ab6` 在获批单卡上完成五组新建受限容器对照：纯数字UID1104、提供用户名/组名的UID1104及UID23901、Root引导后降权23901、零capability的UID0管理探针。五组均管理返回172/驱动错误-8020及ret4，设备原始open返回errno16/EBUSY；均未初始化ACL、未运行算子。它排除了“只要补用户名/组名或Root先启动就能解决”的简单修复，不能排除所有身份/权限因素。旧privileged zdl中Root查询成功不与这些新受限容器等价。见 `evidence/npu-fresh-identity-v026.json`。
- CPU队列作业 `job-400a998069ef` 的只读Root reader不暴露NPU设备，无网络、无capability。最多读取32KiB驱动表，本次实际2890字节/20组；两个原始udevid各20条映射，其中各1条ns_id<128、19条>=128。每个udevid仅1条与事先授权的zdl进程/namespace信息匹配，该匹配位于>=128组；不识别其他持有者，不将udevid直接命名为已校准的物理卡ID。原始表SHA与旧只读观察相同。见 `evidence/npu-namespace-diagnostic-v026.json`。
- 上述注册计数与EBUSY同时存在只是排查线索，不是独占关系或因果证明。没有停止潜在持有者做反事实验证，不能据此认定某个容器/用户导致故障。受限容器UID0也失败，因而不能把历史Root/非root差异简化为唯一原因。
- 聚合解析器4项检查通过：未知格式不产生设备结论、文本标识不导出、只匹配已知自有信息、低ns_id记录不误归到已知容器。未重跑或冒称新增服务301项检查。所有自建诊断容器已清理，队列资源预留为零；未重置设备、修改驱动、重配/重启旧容器或调用模型。

下一次真正计算应以新的环境事实为前提：管理员干净测试节点确认容器设备注册策略、非root设备打开和ACL初始化，再做单次Triton精度及后续性能/仿真。不会因同一失败重复出现而扩大为Root计算或privileged任务。

v0.16节点先绑定本地Docker socket/daemon身份，防止本地NPU账本对应到远端Docker执行。安装doctor仅校验已配置设备/驱动/组的静态合同，不执行npu-smi、ACL初始化或kernel，报告始终保留npu_initialization/npu_numerical=not_run。这个基础设施改进不修复已有EBUSY/507899，不把CPU/IR或Docker绑定验收算成NPU数值通过。

## 两条执行路径

现有个人 Docker 是协作式入口：宿主 supervisor 负责管理查询，容器任务只检查设备权限并领取本次一次性回执。Unix 通道校验对端 UID、job/attempt/令牌及 nonce；回执5秒有效，启动前再检查期限。查询失败、占用变化或过期均不启动命令。npu-admission.json 不含令牌或其它用户进程。此通道需要共享的本 Demo 路径和相同任务/宿主 UID；不适用于跨服务器或非可信任务隔离。

受控节点使用独立的管理员设备合同和 HTTP 租约，不使用个人通道：节点观察最多10秒缓存，中央要求15秒内读数；同 physical_id 的所有入口排他预留物理卡。容器创建前和创建后/start前分别查询，不能因中央分配过就跳过复查。任务非 root、零 capability、no-new-privileges、只读根、无网络，仅有获批卡/管理设备和只读驱动。计算或探针结束后，节点确认 PID 命名空间结束、设备健康且无占用、归档清理完成，再结束预留；查询不可用或存在占用时保留资源并等待对账，不重置设备或重跑 kernel。若从未发出 start intent，可依据这一事实释放预留。

## 管理员合同

下面只展示格式，设备、编号和路径必须按目标服务器验证；不要直接复制为已验证配置。visible_ids 是每个单卡容器里的运行时编号，不是自动推断的物理编号。当前一任务一张卡；跨型号/运行时及卡1映射仍需单独验收。

```json
{
  "device_ids": [0],
  "visible_ids": {"0": 0},
  "device_gid": 1001,
  "management_devices": ["/dev/davinci_manager", "/dev/hisi_hdc"],
  "driver_paths": ["/usr/local/Ascend/driver"],
  "query_tool": "/usr/local/sbin/npu-smi",
  "management_env": "/data/pri/cann/set_env.sh"
}
```

合同文件为管理员本人600权限，通过 workerctl enroll --npu-config 读取。只接受注册的管理设备/驱动目录，检查真实字符设备和设备组；驱动来源必须 root 拥有且不允许其它账号写入。不支持任意宿主路径、root设备组、客户端挂载或特权模式。管理软件路径属于节点；任务的 CANN/torch/Triton/BishengIR 来自固定镜像和 environment_setup，二者分别核对。

登记默认仍只允许 CPU 模板。通过 node-templates 显式加入 npu_probe 或五类工作流，并授予用户 NPU 配额、传回配置后重启节点。固定 npu_probe 只检查设备文件曝光、非 root/附加组/零 capability 与打开文件，不初始化 CANN、不调用算子；它不能证明 ACL 的可见/逻辑/物理映射正确。镜像要运行真实工作流时必须具有匹配框架与编译工具，现有基础 CANN 镜像没有因此自动获得 Triton/torch 支持。

## 当前证据与失败

- 管理通道真实跨容器往返：npu-admission-live-validation.json。宿主1104查询，zdl内1104:1001请求，返回新鲜占用判断；未请求设备计算或启动kernel。
- 受控首次设备探针 job-2ef6a341a2f6：中央分配物理0，Docker元数据只有获批卡与管理设备，驱动只读；raw open 返回 errno16/EBUSY。见 managed-npu-live-validation.json，结论是失败，不是设备访问验收通过。
- 现有Docker烟测 job-b08e34e17e1e：宿主管理回执成功，任务在 CANN9.1 下 aclInit 返回507899/Resource_Busy，尚未输出显式Triton launch。日志在该任务，未修改共享安装。
- 本机只读信息为 Driver25.7.rc1、Driver_Install_For_All=yes。不能将错误直接归因为未配置 install-for-all。root/非root管理查询差异已复核，但确切驱动/容器/运行时原因仍待验证。
- Demo的zdl-mxh-dev现明确登记已有CANN9.2-beta.2，与近期该环境实验选择一致；共享cann符号链接不变。新组合的请求在等待期未获空闲卡后取消，未证明其数值兼容性。
- v0.14只读管理对照 job-103ead0dd78e：宿主 UID1104 查询成功，既有 zdl 内 UID1104:GID1001 查询失败，容器 root 查询成功；三者 npu-smi/HAL/DSMI 文件 SHA 相同。仅使用驱动库和初始化 CANN9.2 两种环境均有此差异。非root容器自身的 strace 显示打开 /dev/davinci0 返回 EBUSY，管理设备可打开；未发现该卡 open 的 EACCES。证据 npu-management-diagnostic-v014.json 不含其他用户 PID/命令。这个对照缩小了调查范围，但不能证明具体驱动容器共享策略或 root 计算可用；没有初始化 ACL 或启动 kernel，也没有更改既有容器配置。

部分旧版本/型号文档有容器共享限制，设备/驱动/CANN组合应按目标平台验证；不能从旧文档直接认定本机根因。[官方容器运行说明](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/82RC1alpha001/softwareinst/instg/CANN%E7%A4%BE%E5%8C%BA%E7%89%88%208.2.RC1.alpha001%20%E8%BD%AF%E4%BB%B6%E5%AE%89%E8%A3%85%E6%8C%87%E5%8D%97%2001.pdf)

Device ID、容器逻辑ID与用户可见索引不是同一概念，需在正确运行时核对；只设置环境变量不提供硬隔离。[官方ID映射说明](https://www.hiascend.com/doc_center/source/zh/canncommercial/900/API/runtimeapi/aclcppdevg_03_2060.html)

## 推广门禁

在新的管理员测试服务器上，先验证固定镜像/驱动/CANN、容器设备注册、非root初始化和编号映射，再做单次精度、独占性能和仿真，并测试取消/卡死及重启后的设备清退。驱动/容器共享模式改变、Ascend Docker Runtime安装及旧容器迁移要另行安排维护窗口，当前主机不进行这些操作。失败时保留日志和原始产物，不通过 root任务、privileged、host IPC 或放宽健康/占用规则规避问题。
