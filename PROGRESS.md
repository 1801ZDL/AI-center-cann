# 实施进度

## 固定探针与开户前的静态容量准入（2026-10-31）

resources.require_managed_capacity共用一CPU/零SHM的静态可运行检查：Node与物理域CPU须有交集，负载内存+128MiB托管开销须同时不超过两侧预算；不扣减当前占用，不把draining当静态不可能。512MiB Node上384+128恰好可用，385+128在新授权/任务前拒绝。用户最大额度可大于单个Node，按实际探针内存判断。

owner_provision._nodes使用同一只读事务快照查目标profile和domain；每Node至少容纳256+128MiB CPU任务，显式device_probes按选定memory_mib验证，在主journal/身份/原生资源分配前拒不可能配置。管理员预激活_current在授权前及之后原授权验证时复核。JobStore.submit在原幂等返回之后、任务/归档预留/目录创建之前，于提交事务内调用Nodes.probe_submission：设备及环境固定探针须目标已登记启用、当前owner已映射、模板已授权，设备也须在合同内，并再次检查静态CPU/内存。

满载但足够容量仍正常排队；原活任务/未知租约不释放。旧幂等请求观察原job；配置在授权与入队间缩小时不写job或归档预留，保留原authorization/job=None意图，恢复合法容量可沿同operation继续。开户预检只是当前配置快照，不承诺后续配置、健康、超时、磁盘、环境一定满足。普通未绑定工作流的可运行节点集合预检仍待评估。

**最终757项/125.228秒/无跳过及JS语法通过；77非文档生产源码与最终Root/full/focused冻结一致。** focused job-57d111334f59为18/2.151秒。新18项覆盖Node/物理域边界、共同CPU不足、满载排队、普通设备/环境probe同规则、未登记/未映射/未授权模板/越界卡、CPU和NPU开户前拒绝、静态drain与实际准入分离、旧幂等/原staged job不替换、配置竞争下原授权保留、65536MiB+128上界及非法参数。合成设备观测不是设备打开或kernel。

最终Root job-3f881608a2f5：在自有隔离服务中通过真实管理员API将domain内存降到256MiB，已安装Root开户CLI明确因256+128不能放下而在主journal/身份前拒绝；原policy恢复后原49个SIGKILL、drain/abort、UID23801运行期间激活交付UID23802与实际个人IR排队/单attempt、第二用户单CLI/peer拒读/Node2 UID23812 IR等均通过。host独立核对capacity-original-domain.json、domain事件含256MiB及最终policy相同（修订递增保留审计），并核两IR时间顺序/单attempt和最终token/禁用状态。七文件盘loop/helper/角色/卷释放，backing files留private；/opt和/personal角色tmpfs不留host，未把host记录核查说成当前nativequota读取。

最终全量job-0c2873c72a86。基线repro job-8d5cd54ed6d5正确显示385+128被旧512MiB Node接收而永久排队。focused job-33550bd5188c的18项只有fixture异常类型错误：未知owner接口是AccessError，fixture误写JobError；改查identity DB确认不存在后最终18通过。既有environment probe目标测试改为真实已登记的另一Node，保留“不能被本Node领取”原含义；未登记目标现在按新规则提前拒绝。完整证据本地evidence/probe-capacity-validation.json，精选reports/public-source-check.json.probe_capacity；失败保留，未放宽预算。

Main空闲0nonterminal/有效hold时重载，configSHA/CPU254,255/2048MiB/SHM1024MiB/1槽及19Node profile SHA不变；旧Node未升级、fresh inventory无豁免。0NPU kernel/模型调用，protected6仍原v036SHA，未动共享驱动/宿主账号或既有zdl。

下一优先：普通工作流的可运行节点集合预检与面向用户的可解释准入，再完善领域harness/msagent诊断能力的实际接入。实际PID1/宿主真实用户/两物理机、NPU访问资格继承/ACL/数值/性能/仿真、远程交付/fencing、一般资源合同与控制盘硬边界仍待。PUBLIC-001延期，整体目标未完成。下方为历史阶段。


## 追加无关用户后的资格兼容审计（2026-10-31）

新增qualification_compatibility.py，中央stage批准与active-owner扩Node授权均在jobs→identities写保留内冻结原资格、成功固定probe来源、旧完整profile/新profile SHA、当前个人权限单调修订。只证明单个新用户及各一个原生work/cache槽的精确追加；旧UID/GID/槽及所有非追加字段必须相同。全局policy_revision继续递增并进入原受保护context，六个运行文件/设备协议无需改版；不是删除版本校验。

原ready交接与新鲜健康Node心跳到位后，比对当前boot/驱动/Docker及protected runtime、旧context、原资格和权限修订，再关联新context。原job/attempt/receipt/recorded_at/expires_at不变、不发新probe、不续30分钟有效期。连续追加保留原probe_context及此前兼容指针。新增用户/禁用stage/失败/过期/来源变化的资格不继承；首次不匹配记录skipped，后来恢复原值也不会再自动继承。等同域其他Node的当前库存时保留pending，并阻配置变更/离线维护/备份；原心跳和中央重启可恢复。资格、Node观察与完成审计在同一事务，重复HB不续期，备份恢复仍使verified过期。存在审计后拒缺少模块的旧中央包。

新管理员只读CLI workerctl node-qualification-compatibility NODE --limit/--offset返回历史计划计数，不输出旧profile/路径，不代表当前卡资格；当前状态仍用node-device-status。最多1024计划/4096候选每计划、每记录预留2MiB、总128MiB，超限明确处理历史，不自动删数据；不冒充SQLite/控制盘内核硬配额。

**最终739项/119.284秒/无跳过及JS语法通过；77个非文档生产源码与最终Root/全量/最终focused冻结完全一致。** focused job-307f6ad5c663为24/3.877秒。24项包括精确追加与设备/用户隔离、旧expiry/receipt不变、连续两次真实中央批准及保留历史、普通accuracy任务取得新context/旧卡grant且无新probe、active-owner扩Node、权限改后恢复、驱动变化后恢复、缺观测、被替换资格、expiry、policy实际API改后恢复仍需新probe、两受控事务异常回滚与重复HB、身份写锁、重开JobStore恢复、源意图篡改、审计预算回滚、恢复备份不复活、同域peer库存等待后正常确认、本地摘要CLI。设备观测和原生盘描述为合成可信协议，未执行设备打开或kernel。

最终Root job-2a550b58921b succeeded：原49 SIGKILL、忙队列排空/中止恢复、UID23801运行期间激活交付UID23802及个人实际IR排队/单attempt、第二新用户一次CLI、peer拒读、Node2 UID23812实际IR等均通过。已安装审计CLI读CPU节点空历史通过；host独立确认qualification_compatibility=0/device_qualifications=0，不能据此说Root/NPU资格继承正向通过。七文件盘loop/helper/角色/卷释放，backing files留private；/opt和/personal仍为角色tmpfs，host仅核持久DB/Node metadata，不宣称当前挂载quota或留存Root历史。

最终全量job-22adfa68b212。初始repro job-73b5d5b4dce3因fixture旧Alice默认NPU0在正常配额门拒绝；显式设置合法额度后job-f85eab8828c1正确复现无关追加导致stale。初步focused job-33f7bc0d4954为1/0.148秒；job-b519f1a5ffc6的19项有两个fixture错误：新计划rev4仍用旧硬编码回执rev2、错误引用JobStore.path；按原计划取回执版本与真实runtime DB路径修正。job-b45b01c2ef98为22/3.695秒，最终另补peer等待和CLI两项至24。失败留存，不放松准入。完整证据本地evidence/qualification-compatibility-validation.json，精选reports/public-source-check.json.qualification_compatibility。

Main空闲0nonterminal/有效hold时重载，原configSHA/CPU254,255/2048MiB/SHM1024MiB/1槽及19Node profile SHA不变；新表为空，旧Node未升级、fresh inventory无豁免。0 NPU kernel/模型调用、protected6仍原v036哈希；未改驱动/宿主账号/既有zdl。

下一优先：固定预激活探针内存须在stage前验证Node/物理域静态运行边界（含128MiB开销），不要把临时占用当成不可能运行。真实NPU资格继承及ACL/数值/性能/仿真、实际PID1/宿主真实多用户/两物理机、远程交付/fencing、一般资源合同及控制盘硬边界、组织身份/msagent仍待。PUBLIC-001继续延期，整体目标未完成。下方为历史阶段。


## 已知任务并行期间的身份激活与个人排队（2026-10-31）

最后激活不再复用结构交接的全Node空闲门。其他用户的已知reserved/running/cancelling任务可继续，原attempt、分配和资源账本不变；满载只让新用户任务排队。结构变更/绑定/ACK仍要求静默；同域未知执行（即使相关Node已禁用）、有效库存hold/过期轮次、待完成交接、draining、环境/存储或目标用户槽位异常仍阻断。其他物理域的其他用户未知任务不自动隔离本域。本用户自身排队/活动预激活任务会阻止最后改变身份，避免遗留授权失效任务。

固定预激活探针授权显式使用identity_change=False：只核当前可安全排队的合同，不改变身份，可与已知工作并存或为另一候选卡显式新建操作，仍禁用用户、无凭据、经原资源/归档账本。原完整候选计划仍串行、同操作仍同job，未自动增加探针或放弃原租约。NPU正向仅合成可信协议。

新增活中央库存轮次核对：本地Svc管理命令无进程内controller_instance时，读受保护server-ready，核活PID/启动身份、同服务UID、能力和32位轮次，再把预期轮次交给Inventory.reason。所有启用同域Node须已确认当前轮次，命令对象的已有冲突轮次、缺失/旧报告、检查期间轮次切换均拒绝；不改JobStore轮次、不伪造Node已对账、不豁免fresh inventory。

**最终715项/127.602秒/无跳过及JS语法通过；76个非文档生产源码与最终Root及全量冻结一致。** 新19项涵盖真实账本的旧任务保留/新IR排队授予、reserved/cancelling、目标与其他用户槽位、同域未知/禁用peer、独立域未知、结构交接仍静默、pending/drain/hold、全域库存轮次及管理轮次切换、预激活probe排队/自己的queued和reserved阻激活、显式另一候选排队与已有NPU资格并行激活。单位检查中的设备观测是合成，不是NPU硬件计算。

最终Root job-772c811b1692，原49个SIGKILL恢复点仍通过。旧用户UID23801的实际IR在运行时，新用户完成激活及UID23802个人交付；该个人沿交付profile认证、封存输入并提交实际IR，在满载原Node queued/0attempt，随后复用同CPU且各单attempt成功。独立host DB核：old job-938f4c7c6c38运行21:05:52.811955→21:06:25.039528 UTC，激活21:05:53.424601 UTC；new job-715c628a4f87在旧任务结束前提交、21:06:25.783685启动，两份锁定环境IR结果passed。个人/任务UID由角色内实际脚本验证，host核对持久任务/激活/身份/Node记录，不宣称host留存/opt Root历史或当前挂载quota。原排空、第二新用户单CLI、peer拒读、Node2 UID23812 IR及历史禁用重放仍过。七文件盘loop/helper/角色/卷释放，后备文件留私有。

最终全量job-50b6c1bf6358；测试源摘要在其仍queued时冻结，最终19项全通过。失败保留：repro job-b5e4b424f0f6是旧fixture占位spool不能签发真实grant；修正完整合同后job-814650bb29e4正确复现旧全Node空闲误阻。focused job-29a0fc5b44f6三个fixture问题为peer缺name、cpu_demo默认128MiB产物超过stage预算，以及重复claim被误当新分配；改合法登记、有限IR合同和exclude_attempts后job-6090efdb1cc2为12/2.295秒，增peer后job-a08ad32ddd01为14/2.384秒。Root job-2637e9c3eb6a揭示新库存检查未绑定本地Svc轮次，误等到finish_intent前；补活轮次后最终Root通过。19项job-cfc7b73bb043只有一个断言错误：缺cap时先由已有cap门拒绝，fixture却强求后置库存错误；按适用门断言后最终全量通过。未放松生产准入。完整证据本地evidence/owner-busy-admission-validation.json，精选reports/public-source-check.json.busy_owner_activation。

Main在0排队/活动/未知/有效库存hold时重载，configSHA/CPU254,255/2048MiB/SHM1024MiB/1槽及19个Node profile SHA不变；旧Node未升级、库存轮次无豁免。protected六文件仍原v036哈希。0 NPU kernel/模型调用，既有zdl启动时间和资源设置不变。

下一优先：无关用户追加后的既有设备资格兼容证明；探针内存在stage前校验Node/物理域运行边界。实际PID1/宿主多用户/两物理机、NPU访问ACL数值性能仿真、远程交付/fencing、一般资源合同/控制盘硬边界、组织身份/msagent仍待。PUBLIC-001继续延期，整体目标未完成。下方为各阶段历史。


## 忙队列维护排空与原进程恢复（2026-10-31）

新增owner_maintenance_drain.py及owner-maintenance-drain-v1。进程模式开户held先发布draining门：拒新提交，既有排队任务仍可claim/start，心跳/结果/归档/清理继续；事务内再次检查提交门，防止维护转换时越过准入。未知任务及inventory hold继续保留资源。缺失/维护门使已预留启动返回wait，同一attempt和分配不变。Node清理、ACK/GC、缓存租约和中央全部静默后记drained，再关闭执行准入、停止明确选择的本地进程Node及中央，最后重查中央，才进入stage。

首次冻结本地prefix/state/release/配置SHA及原运行状态；同Node/物理域/中央authority和辅助程序须匹配，拒用process模式操纵受管服务。最初未运行的服务不为了排空隐式启动。held中止不碰原Node；drained中止恢复原先运行的中央/Node。晚到工作退回排空时，先恢复进程再提交held；恢复中断或部分失败继续保留drained，确保再次中止仍记得恢复Node。systemd仍要求管理员先排空、初始inactive；实际PID1/受管忙排空未验收。恢复不扩大原停止范围，不删除原任务/目录/未知租约。

**最终696项/125.211秒/无跳过及JS语法通过；76个非文档生产源码与最终Root和全量冻结完全一致。** 新20项检查包含两个owner既有排队工作、拒新提交、事务竞争、缺失/维护门等待、未知资源保留、启动门优先、原进程快照/中止恢复、恢复崩溃与部分失败的持久顺序。后三项是受控单元异常，未宣称额外真机故障注入。

最终Root job-6c411e7fc099：49个SIGKILL恢复点通过；旧owner的两个真实IR任务在忙队列中止及完成排空路径各单attempt成功，停止后中止恢复原中央/Node。原第二新用户单次安装CLI开户、个人UID23802/23803认证与peer拒读、Svc23820、等待Node期间旧UID23801 IR、第二Node UID23812单attempt IR仍通过。独立host读取两任务/三个ready+onlineSeen交接/最终禁用及token计数/两个Node映射；Root /opt历史仅角色tmpfs内验证，不能当host留存历史或当前挂载quota。七文件盘loop/helper/角色/卷释放，后备文件留私有。

最终全量job-0dc9473b3bcc。初步focused job-f77217f8ff61为17/2.041秒；早期全量job-54973c7822d6为693/127.175秒、Root job-0fff89c656dc为49点，均在最后恢复顺序修复前，保留原范围。最终696另含3项恢复顺序检查且Root重新验收。完整证据本地evidence/owner-drain-validation.json，公开摘要reports/public-source-check.json.owner_maintenance_drain。

Main在无排队/活动/未知任务及有效库存保留时重载，已加载新capability，原configSHA/CPU254,255/2048MiB/SHM1024MiB/1槽和19个Node profile SHA不变；旧Node未升级，fresh inventory无豁免。库存表保留一条held=0历史记录，不是有效hold。0 NPU kernel/模型调用；protected六文件SHA与原基线一致，未改共享设备/驱动/账号/既有zdl。

下一优先：最终激活的全Node空闲条件与其他用户已知工作解耦；无关owner追加后的既有资格兼容证明；固定探针内存提前验证Node/物理域边界。真实设备/ACL/数值/性能/仿真、真实PID1/两OS用户/两物理机、远程交付/fencing、一般资源合同/控制盘硬边界、组织身份及msagent仍待。PUBLIC-001继续延期，整体目标未完成。下方为各阶段历史验收。



## 显式候选卡开户编排、观察与已成功任务采用（2026-10-31）

开户请求可选device_probes，每个所需Node显式列候选物理卡与有限内存；未选择不自动上卡。候选数量至少覆盖npu并发额度，可以多于额度，合计最多64个检查。预留全部固定探针再加一次最小托管任务的归档预算，Node/卡未登记、重复/布尔参数或预算不足在stage之前拒绝。Root逐个持久保存原意图、原job绑定和完成快照，固定Svc advance只提交一次并查询原job；失败、取消、未知、过期、权限变化和证据替换都停为attention，不自动重试。

新增owner_probe_adoption.py：Root --adopt-probe只接受管理员另行显式验证且当前仍有效的同owner/stage/Node/plan/卡/内存新任务。原任务仍queued/active/needs_attention时拒绝，保留原租约；原intent尚未提交时也在同一jobs→identities事务内禁止旧授权晚到提交，并追加CAS父流程采用链。中央提交记录是响应丢失后的权威，Root只缓存已提交记录；历史重复不倒退，旧失败/意图/快照保留。每卡最多8次采用、全局4096条应用限额，包含采用记录拒降级缺少模块。finishing阶段不改变前置计划，独立显式资格续验仍由最后激活检查当前来源。

servicectl owner-provision --wait-seconds 1..900观察同一请求；预算不打断管理步骤，到期不取消原job，attention立即返回。当前仍要求开始前排空；忙队列的无损维护门转换尚未实现，不将watch当作自动drain。

**最终676项/117.613秒/无跳过及JS语法通过；75非文档生产源码与最终Root及全量冻结一致。** 31项新协议检查覆盖逐卡/双卡、单并发额度多候选并真正选择空闲卡1的合法任务grant、原意图五个连续异常边界、Svc丢响应、job冲突/快照破坏、失败/取消/未知/过期与同ID恢复、显式采用/响应丢失/孤立意图禁止晚到提交/旧任务未终止拒绝/保持历史快照/不兼容降级、观察预算与历史回执。Root文件IO在非Root单测中适配，设备观测与回执均为合成，不是实际NPU执行。

最终Root job-6915ee7dad6a实际安装CLI验证：CPU节点的自动NPU计划在stage前拒绝；负等待参数无副作用；等待1秒返回pending而保留原请求；第二用户普通单CLI+wait完成CPU开户。原44个SIGKILL、Svc23820/个人23802与23803隔离、第二Node执行UID23812实际IR仍通过；七个文件盘loop/helper/角色/卷释放，后备文件保留私有。最终全量job-e47b5402877e，原Root75/full676另有早期冻结job-9db4525295e3/job-dfc8bc6960b8（118.512秒）；最终只规范化一个EOF空行并重核相同包源码。

早先focused job-0aa3e18144a5 20通过；job-42d80cbaf767 23项有一个fixture错误（目标Node未登记accuracy_v1，合法任务未领取）；job-6ed3e1a97168 31项有一个fixture断言错误（篡改回包也污染只读采用状态，先被更早门禁拒绝）。补齐fixture模板并限制篡改到目标probe回包后job-22badb71a08b 31通过。失败保留，不弱化准入。完整证据本地evidence/owner-probe-flow-validation.json，公开摘要reports/public-source-check.json.owner_probe_flow。

本轮0 NPU kernel/模型调用；protected六文件哈希不变，未改共享驱动/账号/既有zdl。真实硬件/ACL/数值/性能/仿真、真实PID1/两OS用户/两物理机、无损维护排空、普通用户并发下的预激活、Node无关用户追加的资格稳定性、一般资源合同与控制DB硬边界、组织身份/msagent仍待。PUBLIC-001继续延期，整体目标未完成。



## 管理员固定预激活NPU访问资格（2026-10-31）

新增owner_device_probe.py与本地服务管理员CLI workerctl owner-device-probe --spec。原disabled/token0阶段不能提交用户任务，但NPU激活要求先有本用户设备访问资格；现在为明确stage/原ready Node计划/物理卡授予一次固定npu_access_probe。它仍经过正常CPU/RAM/NPU/工作区/暂存/归档账本，固定1CPU/单卡/零SHM/120秒/1MiB文件与2MiB总产物；不临时启用身份，不生成凭据或执行用户命令。中央总协调可接受NPU额度，等待管理员显式探针，尚未自动编排探针或验收NPU一键流程。

授权冻结stage、profile、单调身份修订、Node原profile/ready交接、中央storage和受保护host/runtime context。提交/领取/启动许可/结果授予/最后激活均复核，改后恢复原值也失效；claim/start/complete按jobs→identities顺序持有身份写保留直到jobs提交。普通node-device-probe修复空身份回落成服务管理员的风险。原操作重试仍同一job，已提交/完成回执仅历史，不自动重试；新验收须明确新operation_id。授权历史4096条应用上限，包含记录后拒不兼容降级，整个控制盘/DB硬边界仍未完成。

**最终645项/119.488秒/无跳过及JS语法通过；74非文档源码与最终Root及全量快照一致。** 17项新增协议检查覆盖正/负资格、禁用用户无凭据、成功后再撤权阻激活、领取/启动/结果的权限变化、上下文变化、同operation恢复/冲突、固定模板限制、旧入口身份拒绝和身份锁。正向使用合成可信Node观测/回执，不是硬件计算。Root job-3e5d81b12916实际安装CLI拒CPU额度探针及普通入口缺失active身份；原同机真实Svc/Node/个人UID开户、第二用户单命令、配置隔离、第二Node IR及44个中断点仍通过，七个自有文件盘loop/helper/卷释放，后备文件保留私有。

最终全量job-1853b8ddfc01，针对性job-8492c1623ea3。早先642/121.438s及Root job-8423ff2d0b34在普通CLI修复前，保留其冻结范围。首次针对性job-4dc7893ca4c6有6个错误：fixture暂存合同不完整、两个排队任务超过fixture归档额度，以及参数异常类型；修正合同/fixture预算及统一参数错误后通过，没有放松资源门禁。一次首提交请求把资源字段错误嵌套被HTTP400拒绝，未创建job，已修正。完整证据仅本地evidence/owner-device-probe-validation.json，公开摘要reports/public-source-check.json.owner_device_probe。

本轮0 NPU kernel/模型调用、未改全局设备/驱动/账号或既有zdl。已有EBUSY没有新环境事实，不重跑相同探针。尚待：NPU探针显式选卡自动编排和真机设备/ACL/映射/数值/性能/仿真、实际PID1/两OS用户/两物理机、远程个人交付、一般资源变更、控制DB硬边界、组织身份/msagent。PUBLIC-001继续延期，整体目标未完成。



## 中央资源开户总协调与个人隔离（2026-10-31）

新增owner_provision.py及安装Root CLI owner-provision/owner-provision-node。固定原请求串联中央credential-free stage、原生GID配额、实际服务进程组加载、保护Node请求、原提案独立批准、Node绑定/认证ACK/健康心跳、最后激活和个人UID交付。初始重复GID/映射UID、错误Node/中央包在副作用前拒绝；批准使用原profile CAS，提交/claim在数据库事务内重查准入门。

资源维护先暂停准入并要求排空，在服务确认新组后开放旧用户；等待新Node时旧用户实际UID23801的IR任务单attempt成功。中央在等待Node和纯凭据阶段可沿原请求重启。中止只允许held，完成重放历史不复活账号、不重发凭据。元数据每记录12MiB、Root历史128操作/4096文件/128MiB及Svc参数4096文件/128MiB；整个控制盘/DB硬限制仍待。

最终**627项/127.415秒/无跳过及JS语法通过，73个非文档生产源码与最终Root包一致**。自有隔离Root连续44个中断恢复点（含初始无身份中止），另一个新用户使用普通安装CLI一次完成开户；实际个人UID23803认证成功，UID23802读取其私有配置被拒绝。首个新用户使用程序交付凭据在第二Node执行UID23812完成实际BishengIR单attempt任务，原Node/缓存/偏好及唯一原token保持，后来禁用后的历史重放不复活。七个文件后备盘的loop/helper/卷/角色已释放，原文件作为私有证据保留。

最终Root job-265951c1c22a；全量job-f7b3afe0b2ef。早先Root3/4通过但未覆盖第二用户普通CLI场景，保留原范围；失败保留：首次发现并修复finish pending恢复校验，随后receipt清理分支漏测试中断点；一次focused和一次625项全量因fixture误用atomic_json(mode=...)产生同3个错误，修正后当前627全通过。公开摘要reports/public-source-check.json.owner_provision；完整日志与独立host身份/Node元数据核验仅本地evidence/owner-provision-validation.json。

只验证同机进程模式，未运行新NPU/模型、创建宿主账号或改变旧zdl/全局服务。既有Main节点未自动升级，不豁免fresh inventory。跨主机仍管理员保护交付；真实PID1、两名宿主用户/两物理机、NPU预激活与精度性能仿真、一般资源合同变化、控制DB硬边界、组织身份和msagent案例仍待。PUBLIC-001继续延期，整体目标保持未完成。

以下为此前冻结阶段进度，不能用其“尚未实现”覆盖上方最新范围。

## Node资源准备、提案与确认的参与步骤

新增node_owner_provision.py及Root本地`servicectl node-owner-provision --prefix ... --spec ... [--complete|--abort]`。同一operation_id绑定原请求，默认准备原生work/cache槽并导出固定绑定提案；中央使用已有stage批准或active扩容授权后，complete推进原本地绑定及authenticated commit/ready ACK。Node不持有中央管理员身份，不创建宿主账号或远程Root通道、不发凭据、不启动服务。bound回执仍须等待正常Node启动/新鲜心跳及中央身份确认；完成后重放是历史，不复活被禁用用户。

原生计划先于副作用持久化；准备步骤跳过已完成资源，避免重放work撞到cache共用journal。Root内部作用域仅当前精确Node状态目录，普通验证、quota/startup恢复、Worker、备份/代码切换不能绕过；初次journal在独占状态锁下发布，关闭Worker进入窗口。提案写出后父阶段丢失可采用原文件，缺失/改变不能重生成。同ID换请求、换安装或物理机、无关子事务均拒绝。ACK阶段允许仅node_key轮换，其他配置摘要不变，仍用原计划/ACK。中止仅准备且未导出提案，先完成已开始的原生子事务，已分配资源保留、不创建未开始的另一盘。

模块单条记录12MiB、历史256文件/128MiB并预留后续空间，原子写入遗留Root文件保留且有界；不是整个控制盘/DB的内核配额。包含参与历史后拒降级到缺少协议的发布包。

最终**607项/109.872秒/无跳过及JS语法通过，72个非文档生产源码与Root包一致**。Root45次SIGKILL=协调器12+原生14+绑定10+ACK9，另两个quota中断后中止：work保留/cache未分配，以及cache在途恢复且work保留。原提案完整SHA保持；未批准ACK404、stage/token0保持，后续批准同计划恢复；commit response之后实际轮换Node密钥，旧key401、新key沿原ACK继续。新operation条件、提案丢失和未知历史文件均拒绝。原Node映射/槽/环境与缓存inode/IR SHA/namespace保留；随后真实健康心跳、激活/个人UID交付、单attempt实际IR、既有第二Node扩容/原凭据复用和禁用后历史重放仍通过，七个自有盘全部清理。

原始报告仅本地evidence/node-provision-validation.json；公开摘要为reports/public-source-check.json.node_owner_provision。保留两失败：首次真实缓存子事务恢复发现并修复work重放冲突；另一次fixture跨同源两个bind mount执行rename触发EXDEV，调整为同挂载内移出历史目录。早先606项及两份Root正向在节点密钥恢复改动前，只保留其原冻结范围，不重标当前验收。

完整目标继续未完成。下一步是中央stage/GID/配额、服务生命周期、Node请求/提案/回执及最后个人交付的总协调；跨主机只通过受保护的参与协议，不能把同机prefix调用当远程自动化。一般资源变更、控制DB增长、真实用户/PID1/两物理机、NPU数值/性能/仿真、组织身份及msagent案例仍待；PUBLIC-001继续延期。

## 已激活用户扩展第二个Node

新增owner_node_grant.py及服务UID本地`workerctl owner-expand-plan/apply`。经典owner-approve继续只接受禁用且无凭据的stage；扩容独立绑定已提交激活、单调权限版本、当前身份/凭据/成员/配置/中央存储指纹及完整不可变Node计划。权限触发器也覆盖直接SQL和改后恢复，不依赖字节恢复后仍相同的profile摘要。批准、ACK、首次健康心跳持有身份写保留锁直到资源数据库提交；批准后的槽位立即进入Node间冲突/容量预留。首次健康心跳前始终暂停新claim。

权限改变时不继续原grant；对同一Node计划明确`--reauthorize`仅替换未完成授权、使旧grant superseded，保留原资源投影和ACK，不重新绑定本地资源或发凭据。完成后重放仅返回历史结果，不反向恢复被禁用身份；包含授权记录的数据库拒绝降级到缺少协议的包。原个人客户端无需重装或更改profile，owner逻辑身份与每台Node的执行UID分离。

**590项/111.201秒/无跳过及JS语法通过，71个非文档生产源码与最终Root包一致。** 新12项覆盖enabled/disabled、凭据更新撤销、配额/成员/环境及直接SQL失效、同一计划重授权、首次健康心跳、槽位冲突、事务锁及降级。最终同机隔离Root：实际服务UID23820、个人UID23802，第二Node映射任务UID23812；一个既有token、不新增，原Node登记投影、旧缓存inode/SHA及个人偏好保持不变。通过10个本地绑定和9个ACK SIGKILL、两次权限改后恢复拒绝再显式授权；实际安装CLI及个人客户端完成第二Node排队BishengIR 19+23=42、幂等同任务/单attempt/产物读取。七个自有盘全部释放；旧激活/交付/中央重启及启动适配链仍按原范围通过。

本地私有evidence/owner-expansion-validation.json保留完整报告及六次失败定位：两次unit fixture字段/Node接口或能力标记问题，Root新增盘名白名单、两次预期详细错误但实际传输仅HTTP409、以及重复测试已持久ACK intent的中断点顺序。修正fixture后通过；不把这些失败改写成通过。公开摘要为reports/public-source-check.json.owner_expansion。

尚未完成整体目标：完整资源开户协调、一般UID/路径/容量变更协议、远程个人交付、真实宿主账号/实际PID1/第二物理机、NPU资格/精度/性能/仿真及msagent领域案例仍待。当前只验证同一物理机自有隔离Root环境，未改变共享驱动、旧容器或其他人的目录；PUBLIC-001夜间PR归因继续延期。

## 最后激活与个人UID交付的一次请求

新增owner_delivery.py、owner_finish.py及Root本地`owner-delivery-plan/apply/abort`、`owner-finish --spec ... [--plan|--abort]`。固定Root私有请求明确operation_id、原stage/Node计划和目标UID/GID/parent/name/同盘Root spool；中断保留原operation_id，新尝试显式换ID，避免将旧中止记录重放当成重新激活。**身份、中央配额、Node工作/缓存槽与ACK/健康心跳仍须先完成，owner-finish不是完整资源开户自动化。** 不创建宿主账号、不远程登录、不读取个人模型/skill/MCP配置。

服务身份子程序调用原激活事务，Root先持久原请求，再保存原始交付计划；计划/journal/回执只有绑定、身份和摘要，无密码/token。交付核对当前已提交且启用的身份、Basic/Bearer、范围/到期、源文件/目录身份及安装/config。Root私有spool完整落盘并可续写自身正确文件前缀，FD相对renameat2 NOREPLACE发布新目录，持久handoff授权后才转交0600文件与0700目录给个人UID；原始相对CA可交付，外部路径/私钥不接管。已有目录、链接/硬链接、父目录重建、跨挂载及过期凭据拒绝，不覆盖个人文件。用户接管后恢复不再重拷旧配置，归档偏好可保留。

中止仅处理原操作：未提交激活走已有spent-ID中止，已提交激活不自动撤销/禁用；交付及资源不回滚，未知/已交付/Root私有未完成文件不自动删除。Root回执明确保留原状态，完成重放是历史。pending阻断离线备份、代码切换和资源维护；纯凭据阶段不改变执行合同，中央可正常恢复启动/停止，其他resource/unit/startup journal仍阻断。已实际演练pending期间重启中央再恢复，避免激活等待live controller与禁止启动互相锁死。子程序输出丢弃，不用全进程FSIZE限制误伤超过256KiB的合法SQLite。

最终 **578项/103.558秒/无跳过** 及JS语法通过，70个非文档生产源码与最后Root包一致。实际Root/SVC23820/个人UID23802：原激活7、交付8、完成协调6个SIGKILL边界与同请求恢复；未提交完成中止后换operation_id沿用同一目标；一份最终激活token。实际部分Root文件续写、已有目标不覆盖、stale parent/跨文件系统拒绝、handoff前推进隔离管理员进程时钟导致过期拒绝并显式中止、300000字节身份DB填充通过。用户接管后实际UID修改archive_root，继续恢复保留；源用户禁用/令牌撤销后拒绝新交付，旧完成记录不重新启用或覆盖。随后个人UID用程序交付的client完成真实API登录/幂等提交/单attempt BishengIR 19+23=42及读取产物，peer UID读取凭据拒绝，无手工凭据复制。既有本地配额/缓存/Node ACK及中央/Node启动适配流程继续通过。

独立核对保留的master/交付回执、Svc原client SHA、激活DB记录与唯一已撤销token摘要一致；最终账户disabled1。三次成功Root各四盘均释放，所有自有角色/helper/volume缺失，原盘保留。私有完整evidence/owner-finish-validation.json、公开精选reports/public-source-check.json.owner_finish。两次测试失败保留：首focused夹具漏config.json导致2错误；后577回归的两个pidfd夹具漏合法role元数据导致2错误，已修夹具。此前Root/577绿色不重标为最后source范围。

旧zdl启动时间/Memory/SHM不变；没有当前宿主systemd写入、宿主账户、NPU/model或其他工程修改。实际PID1/两宿主用户/两物理机/远程HOME/NPU仍未验收，原新服务启动证据使用固定manager适配器。本阶段不替代完整资源开户协调、已激活用户扩新Node和合同变更重授权；整体goal仍未完成。

## 一次启动意图、真实进程就绪与准入交接

新增unit_start.py及本地Root命令`unit-start-plan/apply/abort`。绑定安装/配置/已加载unit/管理器/全部启用Node定义，持久journal和Root只读gate，发送前持久issued边界；只调用一次`StartUnit(name,"fail")`，不调用Restart/Stop/enable。响应不明或协调器崩溃后仅核对原请求；即使issued之后、RPC之前中断，也不猜测重发。原unit停止/无job且状态静默后可显式abort并生成新计划，不能在服务仍运行时清理中止记录。

先释放状态EX锁再启动，服务取得SH运行锁。入口严格核MainPID/INVOCATION_ID/UID/GID/cgroup，以O_EXCL持久消费nonce后才执行业务。安装服务通过固定prefix环境读取Root gate/journal；任一存在时提交返回HTTP503，中央本地调度、Node领取和Node本地执行暂停，身份查询、心跳和执行清单对账继续。中央须匹配本nonce的实际就绪进程、计划全部Node新鲜且属于本controller_instance的清单对账、无inventory hold；Node须有本nonce/PID/boot/starttick/NodeID的新鲜真实ACK。Root回执先持久，gate/journal最后清理；完成重放仅为历史，不能当作当前健康证明。管理器挂载依赖读回同时修正为显式路径加已知工作目录/PrivateTmp隐式路径，仍拒绝任意额外路径。

最终 **567项/95.628秒/无跳过** 及JS语法通过，68个非文档生产源码与最后Root包一致。固定管理器协议适配器真实启动中央UID23820和Root Node，各仅一次StartUnit。中央8个SIGKILL边界含“issued但未发送”和“已接受但无响应”，证明前者不重发可abort、后者恢复同一进程；持续HTTP503直到最后发布，身份API可用，live abort/改invocation拒绝。Node另5个边界，以实际安装Worker向中央发送认证心跳，nonce/PID/NodeID匹配，同一进程完成恢复并正常停止。原Node绑定/ACK、激活中止/重新激活、个人UID23802实际IR单attempt及归档仍通过。

独立读取保留的中央plan/claim/Root receipt及Node plan/claim/heartbeat逐项一致；所有自有角色/辅助容器/卷缺失、四盘loop均释放，原盘保留，旧zdl启动时间/Memory/SHM不变。完整私有证据evidence/unit-start-validation.json，公开精选reports/public-source-check.json.unit_start。两个失败Root均保留：第一次测试适配器在导入startup子类前替换Bus导致TypeError；第二次测试目录受umask变成0700，实际服务UID无法读回unit。仅修测试导入顺序、目录权限及可信Root测试的凭据读取方式，未放宽生产校验；两次loop释放/原盘保留。最初564项不是最终源码范围；首成功Root只含中央启动，最后Root增加实际Node启动，不相互重标。

本轮没有调用当前宿主systemd、创建宿主账户、运行NPU/model或修改他人容器。**固定适配器不能证明真实PID1的加载/启动/自动重启，两物理机和NPU验收仍缺。** 完整目标继续未完成；下一步为完整开户协调/私有交付、已激活用户扩新Node与资源合同重授权，及管理员试点的真实系统服务验收。

## systemd 生效配置读取、加载计划与中断恢复

新增 unit_manager.py 及本地 `unit-manager-check/plan/apply/abort` 管理命令。固定本地 system bus、清理继承的总线/进程环境、有界子进程输出与超时、Root PID1/总线身份绑定；严格核对属性及嵌套D-Bus类型、实际unit来源/别名/生成来源/附加配置、命令/身份/组/环境/可写路径与沙箱参数。不会导出原始环境属性。systemd启动入口还核对INVOCATION_ID、实际MainPID/UID/GID/存储组与cgroup；进程模式保持兼容。

Root在停止并排空时计划，持久journal后执行管理器Reload及选定unit的LoadUnit，再确认停止状态和生效配置。调用不确定时沿原计划恢复，管理器身份变化拒绝静默续接；显式abort只清理操作记录，不回滚已经发生的重载，也不发送总线写操作。普通启动、备份和代码切换不能绕过pending；完成回执重放明确仅为历史记录。这里没有StartUnit/RestartUnit/enable调用，启动和心跳协调器仍需补齐。Reload具有管理器级的daemon-reload语义。

最终 **554项/81.133秒/无跳过** 和JS语法通过，67个非文档生产源码与最终Root包一致。自有Root角色实际安装文件/journal，固定协议适配器连续六次SIGKILL恢复，身份变化拒绝、无写abort、新计划、来源拒绝、共享服务锁及pending门禁通过；后续已有Node本地绑定/持久ACK、激活/中止与重新激活、独立个人UID真实IR任务继续通过。四个自有loop/辅助容器/卷已清理，原盘和日志保留。最初553项和首Root通过范围早于最后的嵌套类型加固，未重标为最终结果。

宿主仅执行只读system bus身份/版本和公开安全属性查询，实际systemd255接口可读；**未在宿主调用Reload/LoadUnit/StartUnit**。适配器正向结果不等于真实PID1加载/启动验收；无新NPU/model、宿主账户/驱动/其他容器修改，原zdl启动时间与资源配置不变。完整私有证据为evidence/unit-manager-validation.json，精选公开范围见reports/public-source-check.json.unit_manager。

已补充当前资源下的推广优先级：保持个人API/MCP稳定，完善服务接管及一键开户、可执行资源合同、可复现实验与可替换领域Agent；先两名真实用户/单管理员测试机，再真实两机与约20人混合负载。公共夜间PR归因仍延期，完整目标保持未完成。

最后更新：2026-10-31。项目处于候选验证阶段，**尚未达到团队 NPU 生产运行条件**。

## 未提交激活：显式中止、已中止ID与重新激活

新增中央本地 `user-activation-status` / `user-activation-abort-plan` / `user-activation-abort-apply`。状态仅返回ID/阶段，不输出密码/token。账号必须仍是原stage定义、禁用且密码不可用、token0、无激活提交记录；已提交账号走禁用/撤销，不能中止回退。计划绑定原激活ID、stage摘要、私有目录和文件的身份/权限/SHA，不包含明文凭据；Node离线、意图过期和资源上下文改变可显式中止。

应用先持久journal，匹配删除本次client及有归属标记的本次CA，再提交永久已中止ID账本/审计、保存回执，最后清原intent和journal。原有/旧意图未标记归属的CA保留，未知文件拒绝删除。八个连续SIGKILL边界可沿原计划恢复；期间激活和备份/代码切换阻断，不兼容降级拒绝。旧意图恢复也不能启用，旧完成计划重放保留后来新建的凭据。不删除用户、配额、UID槽或缓存，重新激活仍受原资源/环境就绪合同约束。

实际同机安装中，服务UID23820真实生成client后中断、模拟过期、经状态和计划CLI中止，在同一计划八个边界逐次SIGKILL并恢复。此时禁用/token0/已中止ID1；再正常激活产生不同ID，旧Basic密码和Bearer均HTTP401，旧中止重放保留新凭据。随后个人UID23802上传输入、幂等提交单次真实BishengIR编译19+23=42、归档及清理继续通过，旧用户配置未变。最后账号禁用、新token唯一且撤销，独立核对SQLite已中止/激活/回执及原始ext4上的任务身份、结果和IR SHA。

同时修正已安装管理入口的状态目录覆盖约束，等号及缩写形式也拒绝；本地管理员命令不能借此误入另一安装状态。完整 **541项/87.928秒/无跳过**、JS语法通过，新增16项针对性检查；66个非文档生产源码摘要与最终Root包一致。此前540项和首个成功Root为补管理参数约束前范围。一个Root父因Docker辅助容器启动45秒超时而失败，未进入中央或中止流程；按确切镜像/标签/挂载及Created/PID0/未启动状态核对后，仅删除该自有对象，原盘和失败记录保留。不是把观察超时当作已执行成功或重复运行未知任务。后续Root均成功，自有4个loop、角色、卷及任务对象已清理。

空闲重载本Demo后声明owner-activation-abort-v1，原配置、CPU/RAM/SHM/槽位及既定4GiB管理归档预算保持。没有创建宿主账号、改宿主systemd或全局驱动、运行NPU kernel/模型；领域Agent、真实PID1/两机/NPU、资源合同变更重授权和完整一键协调仍待，整体目标保持未完成。

## 既有systemd unit：离线预览、CAS与中断恢复

新增Root管理命令 `unit-refresh-plan` / `unit-refresh-apply`。在停止且静默的选定安装上，核对安装/配置摘要、控制目录身份、unit目录与原文件元数据，预览当前生成文本。只接受本项目完整生成文本或仅缺少新存储组的历史生成文本；手工修改、未知组、链接和常见unit/前缀/type-wide drop-in拒绝。不会调用systemctl、启动/启用服务或修改无关unit。

应用先写Root持久journal，再保存unit和模板原始字节，原子发布两个文件和回执，最后清journal。六个连续SIGKILL边界在同一计划上恢复，未完成时正常验证/启动、备份、代码切换及其他配额维护阻断；配额恢复选项也不能绕过。Root私有历史目录保存原件与回执，重复操作核对记录，不覆盖后续修改。Node内容无需变化时保留inode，首次/重复应用状态有区分。输出明确manager未接触、仍需daemon-reload与服务启动；备份不是自动回滚许可。

最终隔离Root安装中，中央已有旧组23831的unit，新增GID23832后计划只补该组；共享服务锁、前缀drop-in、自定义内容和旧inode计划拒绝。6中断点恢复后两份原始备份字节一致，新unit与当前生成文本/回执SHA一致，无关unit未改。Node无需变化的unit同样计划/应用/重放且inode保持。随后原stage/GID、Node本地绑定与双ACK、个人UID23802激活/私有凭据、真实排队IR和归档清理继续通过。实际生成unit又经过本机systemd-analyze校验；此为文件更新与进程模式演练，未使用真实PID1加载/重启。

完整 **525项/80.944秒/无跳过** 与JS语法通过，新增10项unit针对性检查；65个非文档生产源码摘要与最终Root包一致。首轮524项和首Root为补路径/模式类型边界、Node无需变化案例之前的范围。两次针对性fixture失败保留：最初测试合同缺容量字段导致5处错误，后来新用例插入位置使1处变量未定义；只修测试，不放宽产品门禁。两个Root父均成功，4个loop/角色/卷/任务容器已清理，原盘和日志保留。源码、原始记录与精选公开摘要保持各自范围，旧Root54不重标。

下一步仍需实际manager生效配置读取/受控重载与重启、未提交激活intent中止重规划、完整一键协调、已激活用户扩新Node、NPU预激活资格和真实宿主多用户/两机。当前没有修改宿主systemd、旧zdl、全局驱动或其他工程；未运行新的NPU kernel或模型。

## 新用户激活、私有凭据与真实个人UID

新增中央本地 `accessctl user-activate --spec FILE`，明确owner、原stage摘要及每个必需Node的原交接计划，无远程提权接口。中央冻结Node账本并锁定身份事务，核对当前ready回执/profile、30秒心跳、环境、UID工作/缓存槽、有限暂存、物理域和原生配额。最低工作流的暂存字节/inode复用调度准入；NPU额度还需当前本用户设备资格。管理进程及实际运行的同UID中央服务都须具备新存储组，CLI临时补组不能掩盖旧systemd进程。

凭据intent和个人client文件在启用前持久保存，单身份事务提交启用位、密码哈希、一个个人token摘要和激活记录，随后发布私有交付回执。命令不输出密码/token，默认token24小时、仅jobs读/提交/取消。7个连续SIGKILL边界在同一激活ID上恢复，不重复发放；之后的禁用/撤销优先。未完成时备份/代码切换拒绝，含激活记录的降级拒绝；已完成记录离线恢复有路径迁移。未提交intent过期或合同改变的显式中止重规划仍待，当前保留证据并拒绝。

最终Root采用真实安装中央UID23820和Root Node，真实stage/GID/本地绑定/HTTP双ACK后，可信管理员将0600凭据交给0700个人目录的UID23802。该UID自行调用API、上传输入并幂等提交同一IR任务；UID23801无法读取个人凭据，另一个应用账号读任务返回404，个人token调用Node接口返回401。任务实际UID23802、零capability/NoNewPriv1，BishengIR编译19+23=42，单次attempt、归档及清理完成；最后禁用账号，激活重放保持禁用。独立只读原始ext4确认身份和IR SHA，身份库、私有回执及唯一token摘要另行一致核验。

当前完整 **515项/82.367秒/无跳过**、JS语法通过，18项激活针对性检查；64个非文档生产源码摘要与最终Root包一致。此前511项为补实际中央进程组检查前范围，513项为补最小用户额度/恢复路径检查前范围；各自保留原记录。5次失败保留：API字段误用、暂存字节不足、暂存inode不足、直接Worker测试漏生产umask导致GC拒绝、Worker多行状态触发父脚本JSON解析失败。两项容量问题补入激活门禁，下一新fixture使用1024MiB/262144inode盘；没有放宽原准入。最后两个失败的内层任务已成功，父失败仍保留，不能重标全成功。

四个自有盘/loop、角色、卷和任务容器均清理，原盘和失败日志保留。空闲重载本Demo后含owner-activation-v1，配置及CPU/RAM/SHM/槽位/既定4GiB管理账号保留预算不变。个人文件实际交付仍由可信管理员完成；这是同机数值UID模拟，环境由可信fixture管理员登记，未创建宿主账号，未验收新保护环境探针、NPU、systemd PID1或两台物理机。整体仍未完成，下一步见BACKLOG.md。

## 新用户预配：中央批准与持久ACK交接

新增中央 `workerctl owner-approve` / `owner-status` 与Node `servicectl node-owner-ack`，使用本节点凭据调用专用commit/ready接口。中央核对真实禁用/无凭据身份、GID硬配额、旧Node投影和修订及其他Node的分配，原子提交匹配登记；Node落盘两阶段ACK后才解除启动阻断。重复批准、请求和丢回复重试返回同一确认，不启用账号或发令牌。交接中暂停新领取、拒绝冲突配置/不兼容降级；撤销节点凭据后仍保留其待交接存储。配置继续修改须等更新后的健康心跳被中央观察。

最终同机联合演练采用实际安装的非Root中央UID23820与Root Node、真实中央stage定义和GID追加，四个独立文件后备ext4盘：工作/缓存各128MiB、中央载荷256MiB、Node暂存128MiB。Node本地绑定原10个连续SIGKILL链保留，中央ACK另有同一计划9个连续SIGKILL边界及原计划恢复；HTTP请求真实经过节点认证。最终本地/登记投影、中央提交/ready ACK和Node本地回执独立逐项一致；阻断文件已清，实际Worker健康心跳标记online。旧IR缓存暖命中和新UID独立IR编译仍通过。新身份保持disabled、不可用密码标记且令牌数0，尚未交付个人凭据。

完整 **497项/83.050秒/无跳过**、JavaScript语法通过，63个生产源码摘要与最终Root冻结包一致。此前495项是补交接撤权保留约束之前的范围，保留原记录。最初针对性测试有一处fixture引用不存在的JobStore.config；修fixture后9项通过，最终另补2项撤权/轮换场景。4次Root测试配置失败分别为隔离导入路径、组配额挂载参数、ext4有效容量/元数据余量和暂存盘最小容量；均在中央批准前失败，原日志/盘及清理证据保留，没有放宽产品校验。

所有自有角色、卷和4个loop均清理。空闲时仅重载本Demo，当前实际就绪信息包含owner-handoff-v1；配置、CPU/RAM/SHM/槽位和既定4GiB管理账号归档预算未改。环境由可信fixture管理员登记，不能称本轮重新完成中央保护探针认证；没有运行新用户队列任务或启用账号。完整开户还差最终激活/私有凭据发布、systemd组更新和实际多机/用户验收，见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。

## 新用户预配：Node 本地绑定阶段

新增 `node-owner-plan` / `node-owner-apply`。在停止且静默的已安装Root Node，将一个新用户映射与预先准备的工作/缓存UID槽一起绑定；更新全部旧缓存的策略摘要及新槽控制记录，保留原数据、UID/GID、命名空间key和环境。计划绑定配置/版本/控制目录/原生源、旧缓存状态及用量；中断只接受原计划。完成后仍等待中央确认，启动、维护、直接Worker运行、另一原生追加及代码回滚被阻断。

自有两个128MiB ext4 UID quota盘和真实安装代码完成：安装CLI、活跃租约/服务共享锁拒绝、同一计划在10个连续持久化边界SIGKILL并继续恢复。旧UID23801的真实BishengIR冷编译答案42，迁移后新租约命中同一文件、inode和SHA，调用次数仍1；新UID23802在独立命名空间另编译成功。两者均零capability/NoNewPriv1。旧配置映射、槽位和环境保持；独立控制记录核对旧key集合保留、新旧key不相交、租约均释放和交接计划一致。

演练使用fixture stage摘要，未连接中央，不证明中央授权或完整开户。真实前一代码9d025e0在绑定前回滚/再升级可保留缓存，新工具拒绝修改旧安装；进入待确认状态后回滚被拒绝。首次失败是外层umask收紧公共测试挂载目录，降权IR进程在绑定迁移前无法读入口；失败状态和日志保留，两个loop、角色和卷已清。修正目录权限并显式关闭拒绝启动时的状态锁后，Root及全量486项/75.161秒/无跳过、JS语法检查通过；8项新单元覆盖计划和启动边界。

随后仅增强Root验收脚本，直接核对暖命中前的完整旧缓存元数据，并验证控制generation变化后旧计划在写日志/修改配置前被拒绝；再次Root演练通过。生产61个文件与已通过486项回归的源码未变、与最终Root冻结包逐项一致；未因此重跑无变化的全量回归。原始报告及公开摘要分别保留完整和裁剪范围。

个人Main在验证通过后仅将管理账号的应用归档上限从2GiB显式扩至4GiB，以保留累计验证档案。事前观察物理磁盘可用约9.3TiB；所有逻辑用户额度行、CPU/RAM/SHM/槽位及其他容器未改，没有删除历史或原盘。空闲时重载本Demo加载当前源码和此容量配置；此前2GiB预算拒绝与正常结算后接纳的证据仍保留。这是运维容量变化，不是修改受测Root硬配额或放宽失败验收。

## 新用户预配：中央存储组阶段

新增已安装中央的 `payload-owner-plan` / `payload-owner-apply`。操作绑定无凭据的禁用身份及定义摘要、发布版本、控制目录身份、配置基线、原 owner 用量和配额修订；只追加一个空白 GID，不接管已有数据或兼容限额，不改旧 owner 分配。实际硬额度须覆盖身份声明的输入/归档预算，并检查静态总量与物理空闲字节/inode。控制盘恢复日志先于原生配额变更，普通启动/维护及另一类配额调整在未完成时被阻断。

自有256MiB ext4组配额盘上的实际中央安装和CLI验证通过：7个命名边界的SIGKILL与原计划恢复、旧用户哨兵数据及限额保持、过期计划/变更的身份定义/未登记既有限额拒绝。UID23820、零capability/NoNewPriv1固定可信写入进程在新增GID写满后返回EDQUOT122，另一GID仍可写入；带新数据回执重放及进程模式服务重启取得新补充组通过。真实前一公开提交60c2002重建旧包，代码回滚/再升级保留配置和数据；新管理工具拒绝修改缺少恢复保护的旧安装。

首轮演练因降权固定写入进程读不到Root私有测试源码，在开户/追加操作前失败；失败及清理证据保留。改用实际安装代码后通过；随后补回执重放的重启/组更新提示和实际身份定义变更拒绝，最终独立Root案例通过。没有把旧Root54或原生Node槽追加案例重标为本轮全量安装验收。

当前完整回归 **478项/78.009秒/无跳过**、JavaScript语法通过；9项新增单元涵盖计划/定义/数据拒绝/互斥及历史安装保护。最终Root案例冻结包的60个生产源码摘要与本轮受测源码一致，自有角色和卷另行确认已不存在。个人Main没有重载或修改预算；首次全量提交在Root父任务尚预留归档时被409拒绝，父任务结算后相同请求/幂等键正常接纳，未提高额度或删除历史数据。

**尚未完成统一开户。** 账号保持待预配，未交付凭据，Node工作/缓存绑定和最终激活仍待做。进程模式新组已测；真实systemd PID1和既有unit补充组更新未测、尚未自动化，返回明确更新标记。操作见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。

## 新用户预配：中央身份阶段

新增 `accessctl user-stage --spec` 和 `user-status`。中央一次提交禁用身份、固定定义摘要和不可用密码标记，不生成密码、令牌或个人客户端配置。同一定义可重复提交；不同定义、额度或环境修改被拒绝。项目和 Node 映射可提前登记，用户仍不能登录。普通启用或续发凭据无法跳过这个状态；完整资源就绪后的激活尚未实现。

新增12项检查覆盖：真实HTTP的Basic/Bearer拒绝、注入既有令牌仍拒绝、并发登记只创建一个身份、安装包内CLI/私有请求/链接路径拒绝、Node预登记不授予登录、旧禁用账号行为、发令牌与禁用竞争、恢复时重算定义摘要及拒绝不认识该状态的旧代码。全量 **469项/79.426秒/无跳过**、JavaScript语法通过；59个生产源码摘要已记录。测试运行在本项目的隔离状态目录，未创建宿主账号、重载Main或执行新的Root安装/NPU/模型调用。

后续仍需中央owner GID硬配额追加、Node工作/缓存策略的有修订绑定及各层就绪回执，最后才启用账号并发个人凭据。现有 `user-add` 保留旧测试账号行为，不是这套统一开户流程。详情见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。历史原生槽追加及Root54保留其冻结源码验收范围。

## 新用户预配：原生存储追加阶段

已实现已安装 Node 的 `workspace-add-plan` / `workspace-add-apply`：只向已登记的 ext4 UID quota 文件系统追加未使用 UID 的槽位，不修改原用户额度、数据或 Node 配置。计划绑定安装/配置/文件系统身份；控制盘和配额盘持久意图支持原计划恢复，未完成时阻断 Node 启动与维护。代码包携带 OWNER_PROVISION.md。

自有256MiB ext4和实际安装代码完成：CLI计划/应用、服务共享锁拒绝、7个SIGKILL边界及恢复、旧UID数据和限额保持、新UID内核EDQUOT、完成后带数据重放、过期计划/已有路径/既有限额拒绝。还从真实前一公开提交34d33c4重建旧包，验证代码回滚/再升级，原数据/配置保持；新管理代码拒绝跨版本修改缺少恢复保护的旧Node。该测试Node采用测试注册信息，未连接中央。

最初一次测试因角色镜像无Docker CLI，在安装前失败并完整清理；失败保留。修正为只读系统CLI和固定daemon绑定后通过，后续增加安装CLI和真实历史包兼容范围分别留证据。最终原生存储/兼容案例及全量回归均通过；457项/77.634秒/无跳过、JavaScript语法通过，58个生产代码摘要同时与Root案例冻结包匹配。没有重新运行历史Root54或大编译。

**完整开户尚未完成。** 原生存储步骤不创建/启用账号，不改中央GID配额、Node用户映射、缓存绑定或交付个人凭据；返回明确标记后续绑定仍必需。中央待预配身份已有本页顶部的新进展，后续继续owner存储与Node绑定，不能把槽位追加成功当成用户已可提交执行。边界见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。

## 最新旧命令产物合同修复

旧非模板命令现在保留有效的产物限额，并一致用于幂等指纹、归档预留、日志/源码快照和输出收集。独立CLI新增成对参数 `--artifact-file-mib` / `--artifact-total-mib`；受控工作流仍在JSON声明。超额输出保留原件，工作负载目录不进入归档；成功命令改记失败，日志与说明仍能下载。旧协作式执行不因此获得文件系统硬配额。

9项针对性回归的修复前运行失败，修复后全部通过；随后补充实际2000/2001文件数量边界用例，覆盖单文件/总量、参数结构、幂等冲突、默认兼容、日志截断、源码快照以及合法129MiB归档。全量 **450项/75.213秒/无跳过**、JavaScript语法通过。

升级演练先用独立安装的CLI在仍加载旧代码的中央复现“声明8/16MiB却保存64/128MiB”，保留成功的单次尝试。空闲时仅重启本Demo中央加载修复：旧默认请求仍返回原任务；旧被忽略的非默认声明重试返回409，原任务契约不改、不重复计算。新1/2MiB声明准确保存，512KiB输出成功；2MiB输出明确失败，原件保留且失败日志完整下载。错误参数返回HTTP400。配置、资源策略均未改变，终态无在途/未知/阻断；无NPU或模型调用。公开摘要见 reports/public-source-check.json，接口及边界见 STORAGE.md/API.md。

## 最新混合负载验收

此前冻结发布包在 UID quota 提供器上的完整 Triton 冷暖构建与 19 名其他逻辑用户的 57 项 IR/CMake 任务已通过，独立原盘核验也已通过；不是旧命令修复后重新编译一轮。公开摘要见 [reports/mixed-compiler-stage.json](reports/mixed-compiler-stage.json)。

- 独立安装的中央与 Node、20 个逻辑账号及不同任务 UID；工作盘 32GiB、缓存 8GiB、Node 暂存盘 4GiB、中央归档盘 8GiB，使用自有文件后备 ext4 及 UID/GID 字节和 inode 配额。
- 外层队列预算 2CPU/12GiB，完整编译申请 1CPU/8GiB，实际最多两个计算槽。62 个任务全部成功且各只有一个尝试：3 个前置检查、57 个其他用户任务、2 个完整构建。
- 冷构建约 4038.445 秒，531 次 cache miss、0 次 hit；全新源码暖构建约 158.107 秒，531 次直接命中、0 次 miss。外部 LLVM/BishengIR 沿用固定镜像内容，未在本轮重建。
- 两份 wheel 各 1,253,270,190 字节、5,416 项 RECORD 通过；私有安装、新 C++ 扩展导入和离线 IR 常量折叠通过。
- 19 名其他逻辑用户的 IR 与 CMake 答案均为 42；小编译冷任务各 2miss，暖任务各 2hit/0miss。35 项任务与冷编译实际重叠；20 个私有缓存 namespace、跨用户私有任务不可见检查通过。
- 独立安装的个人客户端完整下载并核对两份 wheel、RECORD 和受保护环境回执。
- 清理后，另一个排队任务通过只读 SQLite 和持有的普通镜像文件 FD 执行独立核验：62 个单次成功任务、无 inventory hold、57 份产物的 UID/GID 归属、20 个空工作槽、空暂存目录、两份原盘 wheel 的 RECORD/新扩展/Node 摘要回执均一致。
- 自有角色容器、卷和辅助容器均移除，4 个 loop 关联释放，cleanup_errors 为空。Main 已恢复原 2CPU/2048MiB/1槽；原有 zdl 容器的启动时间、内存及 SHM 配置与基线一致。

这证明同一物理服务器上隔离角色和逻辑用户的 CPU/编译/归档链路，不证明真实宿主账号、20 人同时计算、两台物理机、NPU 计算或宿主掉电恢复。未创建宿主账号，未修改全局服务、驱动或共享编译环境。

## 当前代码检查

当前完整回归 **578 项/103.558 秒/无跳过**、网页 JavaScript 语法通过；此前567项为启动/心跳准入交接、554项为manager读回/加载恢复、541项为未提交激活中止、525项为unit离线更新、515项为最后激活、497项为中央/Node交接、486项为Node本地绑定、478项为中央存储组、469项为待预配身份、457项为原生槽追加、440项为独立原盘核验工具阶段。公开摘要 [reports/public-source-check.json](reports/public-source-check.json) 保存受测 70 个生产代码摘要。

新增 5 个核验工具回归使用真实子进程，覆盖有效元数据、单次短写截断、持续写达到内核文件限额、stderr 洪泛和文件系统警告拒绝。小记录输出在子进程写入时就受 2MiB 上限约束，避免 capture_output 读完后才检查大小；不影响已冻结的生产代码或构建工作负载。

Worker 的历史并发修复已有修复前确定失败与修复后证据：先取样的未清理心跳晚于正常完成回报到达，曾误触发资源域隔离。现在完成回报等待旧心跳发送完，后续清理态心跳仍可在慢 ACK 期间继续；真正未知租约的隔离规则未放宽。本轮完整混合案例未再触发 hold。

v0.36 历史安装验收为 432 项单元检查、54 项自有 Root 容器安装检查，范围见 [reports/v036-public.json](reports/v036-public.json)。后续长目录 socket 修复已有原失败深层克隆的 434 项回归及真实跨容器合成握手；当前混合案例覆盖新增 Worker 的实际使用。它们分别保留证据，不重标历史 Root54 为新版本全量安装验收。

## 已有实现

- 程序化中央调度、持久资源账本、物理服务器资源域、幂等提交和用户/项目授权。
- Node 主动领取、受限非 root 任务容器、执行尝试身份、未知状态保留资源和恢复对账。
- 封存输入、工具链摘要锁、精度/性能/仿真/编译/IR 五类任务合同、结构化结果与个人归档。
- 独立工作盘、中央输入/归档组配额、Node 暂存盘、私有持久缓存、阶段依赖与产物交接。
- 中央/Node/个人客户端分角色安装；停机备份、新目录恢复和兼容代码切换/回滚。
- 历史真实 OpenCode → MCP → 中央 → 执行器的 CPU/IR 任务通过；领域诊断能力仍需已知案例验收。

## 保留的失败与发现

混合验收前四次失败均保留，未改成成功：单文件/输入总额度不一致、Root600 合同未交给服务 UID、IR 产物声明类别错误，以及真实 Worker 心跳顺序问题。第四轮虽有 38 个成功小编译，IR 合同仍失败，完整编译明确取消；本轮通过前先验证真实 IR 合同和冻结源码配置，配置使用不同缓存 namespace。

新回归首次提交被个人归档预算拒绝；当时原父任务仍预留默认产物额度。父任务终态结算后，相同请求正常接纳，无需提高限额或删除旧证据。排查还发现旧非模板 command 入口会校验 parameters.artifacts，却未在规范化结果中保留它，后续仍按默认额度预留；已修复并验证，见本页顶部。该问题不影响托管工作流的显式产物合同；现已完成本页顶部的修复与升级验证。

## 未通过的生产门槛

1. 新 owner 自动预配、Node 工作/缓存配额变更、缓存保留/新盘迁移、控制数据库及审计增长硬边界。
2. 非 root NPU 编号/隔离、ACL 初始化、真实精度、性能、仿真及清退；已有设备失败保留，不用管理查询成功替代计算验收。
3. 两名真实 Linux 用户、两台物理服务器、真实 systemd PID1、旧中央 fencing、组织身份及宿主掉电/磁盘故障。
4. 领域 Agent 的已知错误/性能/流水案例验收；完整外部 LLVM/NPU-IR 构建。

## 后续公共项目

夜间 PR 回归与归因（PUBLIC-001）暂未启用。后续采用独立机器人身份、低优先级预算和固定环境；资源不足等待，先产出复现实验及修改/revert 建议。

## 同步约定

每次有可复核的代码变更、验收结果或失败定位时更新本文件并提交。运行状态、模型配置、对话、原始实验盘和私有部署凭据不上传。推送前重新检查远端 HEAD，使用普通快进更新；完成一个阶段不等于完成整个项目。
