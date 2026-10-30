# 用户预配：当前阶段与交付条件

当前增加中央总协调器，把**待预配身份、中央GID配额、服务生命周期、Node原生槽/独立批准/ACK、最后激活与个人UID交付**串成可恢复请求。已验证同机进程模式的一次命令开户；跨主机仍须管理员传递受保护请求/提案并启动远端Node，不是远程安装器。真实宿主账号、两物理机、PID1和NPU仍待验收。下文保留各独立步骤供诊断和恢复。

## 中央资源开户协调（2026-10-31）

这一步面向**已安装中央、已登记的基线Node和已认证环境**。管理员仍须先准备Linux UID/GID、专用中央存储盘、Node原生work/cache盘、个人parent和同盘Root spool；首个Node的登记/探针身份也须先按SERVER.md准备。协调器不创建OS账号、不格式化系统盘、不复制模型配置、不持有远端Root登录凭据。个人Agent只调用任务API/MCP，不能调用这个Root管理入口。

中央和Node安装包须包含匹配的owner_provision.py。现有运行中央须已加载owner-provision-v1；旧进程会被拒绝，应先排空、按普通升级流程重载。所选Node须已登记并启用，work/cache分别登记为ext4-userquota；新owner/UID不能已有映射。当前协调入口只接受CPU额度，npu必须为0；NPU预激活资格尚未接通。

Root所有、0600的请求示例；operation_id生成一次后随原请求重试，Node ID和路径按实际替换：

```json
{
  "operation_id":"0123456789abcdef0123456789abcdef",
  "owner":"researcher_a",
  "quota":{"cpu":2,"memory_mib":2048,"npu":0,"max_running":1,"max_queued":20,"input_storage_mib":512,"archive_storage_mib":2048},
  "storage":{"gid":23034,"bytes":3221225472,"inodes":131072},
  "service":{"mode":"process","unit_dir":null},
  "nodes":[{
    "node_id":"worker-0123456789ab","uid":23004,"gid":23004,
    "workspace":{"root":"/srv/ascend-work","bytes":4294967296,"inodes":131072},
    "cache":{"root":"/srv/ascend-cache","bytes":4294967296,"inodes":131072}
  }],
  "delivery":{"owner":"researcher_a","parent":"/home/researcher_a","name":"ascend-lab","uid":23004,"gid":23004,"spool":"/home/.ascend-lab-delivery"}
}
```

中央原生字节额度至少覆盖input_storage_mib与archive_storage_mib之和加65536字节元数据；实际盘剩余空间、GID冲突及Node映射也会在stage之前检查。执行UID是每Node本地映射，可以与个人客户端UID不同。delivery.parent须已属于指定个人UID/GID，name目标不存在，spool是同盘同挂载的Root0700目录。不会自动选另一盘或覆盖已有客户端配置。

同机、进程模式、所选Node已停止时，可显式指定本地安装路径：

```bash
/opt/ascend-lab-controller/current/servicectl owner-provision \
  --prefix /opt/ascend-lab-controller --spec /root/researcher-a.json \
  --local-node worker-0123456789ab=/opt/ascend-lab-node
```

该映射只表示同机已安装Node，不是SSH地址；可重复指定。它准备槽位/提案、中央独立批准、完成绑定/认证ACK，并以普通进程入口启动Node。不会自动停止正在运行的Node；共享锁冲突时保留原操作，管理员须按原流程排空/停止后恢复。中央只在配额变更窗口暂停准入，要求既有排队/活动任务和未知库存先处理；恢复服务并确认新组生效后开放旧用户任务，在等待新Node时仍可运行。新用户只有所有指定Node就绪后才激活。

跨主机时省略--local-node，中央返回state=nodes和node_requests中的request_file。由可信管理员通过保护传输交给对应Node的Root，保持Root所有0600；包内绑定Node、物理域、中央URL/CA和原stage，没有个人模型密钥。Node执行：

```bash
/opt/ascend-lab-node/current/servicectl owner-provision-node \
  --prefix /opt/ascend-lab-node --request /root/researcher-a-node.json
```

把返回proposal_path指向的**原提案**保护传回中央，由中央原请求加--proposal批准：

```bash
/opt/ascend-lab-controller/current/servicectl owner-provision \
  --prefix /opt/ascend-lab-controller --spec /root/researcher-a.json \
  --proposal /root/researcher-a-node-proposal.json
```

Node再以同一request加--complete完成绑定/ACK，按其既有服务管理方式启动并产生健康心跳；最后重放中央原请求完成个人交付。这个包/提案协议已在同机安装CLI验证；真实跨机传输和远端个人HOME交付尚未验收。中央不代替管理员传包或远程启动。

中断后保留原operation_id、请求、计划与提案，重复同一命令；普通verify/备份/代码切换在主journal存在时拒绝。Root主journal在prefix/owner-provision-change.json，公开准入门在prefix/owner-provision-admission.json，原计划/回执在prefix/owner-provision-history。固定服务UID子程序只执行stage、空闲检查、独立批准、就绪和进程组检查，不接收用户shell。批准会比较登记Node原profile摘要，不能悄悄采用并发变化。

--abort仅适用于held阶段，尚未产生持久身份/资源；之后要求完成或明确协调原状态，不自动回收配额、删目录或撤销用户。完成重放只返回historical_receipt_only，不重新启用后续被禁用的账号、不重新签发凭据、不覆盖个人偏好。主历史记录最多128个操作/4096文件/128MiB，单记录12MiB；服务参数另有4096文件/128MiB限制。这些是应用元数据限制，不是整个控制盘/SQLite的内核硬配额。

systemd模式须显式unit_dir且中央管理unit**初始已停止**；协调器使用原unit刷新、manager加载与一次启动计划，不直接执行任意systemctl命令。该组合尚未通过真实PID1验收，不能把进程模式的成功当成systemd或远程推广已完成；依赖基线Node新鲜观察的启动门禁仍保留。

当前627项检查及JS语法通过，73个非文档生产源码与最终Root包一致。同机自有隔离Root验证44个连续中断恢复点、初始中止、第二个新用户一次安装CLI开户和实际个人UID认证/同伴读取拒绝；等待期间旧用户真实IR任务成功。新用户沿交付凭据在第二Node的执行UID完成单attempt IR。七个文件盘的loop/角色/卷已释放，后备文件作为私有证据保留；未创建宿主账号或修改旧容器、驱动及全局服务。完整目标仍未完成。

## 最后激活与本地个人目录的一次请求

前面的身份、中央存储、Node绑定/ACK/健康心跳必须已完成。以下操作由选定中央所在主机的可信Root管理员执行；不创建Linux账号、不持有用户SSH或模型密钥，不通过个人MCP开放提权。个人parent须已属于明确指定的UID/GID且祖先没有链接/其他人写权限；最后的name目录必须不存在。准备一个**已存在、Root所有0700、与个人parent在同一文件系统和挂载**的spool，例如由管理员在/home文件系统准备/home/.ascend-lab-delivery。不会自动改用户HOME权限或选择其他盘作为替代。

Root私有0600请求示例（摘要/Node ID/UID/GID按实际替换）：

```json
{
  "operation_id":"替换为一次生成并在重试时保持不变的32位十六进制ID",
  "activation":{"owner":"researcher_a","stage_id":"替换为中央64位摘要","required_nodes":[{"node_id":"worker-替换为实际ID","plan_id":"替换为原Node交接64位摘要"}]},
  "delivery":{"owner":"researcher_a","parent":"/home/researcher_a","name":"ascend-lab","uid":23004,"gid":23004,"spool":"/home/.ascend-lab-delivery"}
}
```

```bash
/opt/ascend-lab-controller/current/servicectl owner-finish --prefix /opt/ascend-lab-controller --spec /root/finish-owner.json --plan
/opt/ascend-lab-controller/current/servicectl owner-finish --prefix /opt/ascend-lab-controller --spec /root/finish-owner.json
```

第二条命令完成原user-activate及受管交付；中断后重复同一operation_id和请求。中止记录重放仍为中止；要开始新尝试须明确生成新operation_id，可沿用同一激活合同与未占用的个人目标目录。结果只含操作ID与回执，不输出密码/token。Root先持久保存prefix/owner-finish-change.json，固定服务UID子程序执行原激活，再持久保存原始交付计划，完成后保留Root私有owner-finish-history回执。服务子程序没有可注入的用户命令，输出丢弃；不设置会限制SQLite正常写入的全进程文件大小上限。已激活后被管理员禁用/撤销的身份不会被完成重放重新启用或签发凭据。

交付单独使用Root私有owner-delivery-change.json：只读取对应已提交激活的原私有client及可选相对CA，校验当前账号、Basic密码与Bearer有效性/范围/到期时间。计划绑定安装/配置、源文件身份/SHA、目标UID/父目录/Root spool。文件在Root私有暂存目录完整写入并fsync，可续写自身未完成的正确前缀；以FD相对renameat2 NOREPLACE发布新目录，再持久保存handoff授权，最后把0600文件及0700目录交给个人UID。未知文件、已有目录、链接/硬链接、父目录身份变化或跨文件系统均拒绝，绝不覆盖个人配置。[Linux不覆盖重命名语义](https://man7.org/linux/man-pages/man2/rename.2.html)

所有权转交后，个人可以用该目录的client.json配置CLI/MCP及归档偏好；恢复只完成历史交接记录，不重拷旧凭据或覆盖个人修改。delivery.json仅含操作/身份元数据。两种pending阻断离线备份、代码切换和资源维护，其他维护恢复标志不能绕过；它们不改变执行/资源合同，因此中央启动/停止入口可在这两个凭据阶段恢复服务，仍会校验代码与其他资源journal。现有运行服务和其他账号不因此停止。Root历史资料位于安装prefix，应随安装管理资料保留。

放弃未完成请求可重复同一命令并加`--abort`。协调器只处理自己原始激活/交付：未提交激活沿原中止流程清理，已提交激活仅完成其恢复，不自动禁用或撤销；已交给个人的文件、未知副本、原生资源和未完成的Root私有文件均不删除。返回prior_state_retained=true，管理员仍须按实际状态决定禁用/撤销及留存文件清理。完成回执是历史记录，不是当前文件/令牌有效性证明；Source已变或凭据已过期而尚未交接时拒绝继续，不另发凭据绕过。

对已完成激活、仅需交付的用户，也可分别调用`owner-delivery-plan --prefix ... --spec Root私有delivery定义 --output Root新计划`、`owner-delivery-apply --prefix ... --plan 原计划`及`owner-delivery-abort`。该定义就是上例delivery对象。跨主机个人目录仍通过可信加密交付渠道处理，本命令不会远程登录；资源预配总协调参见本页开头，owner-finish单独使用仍要求其前置条件。

## 中央待预配身份

在选定中央的管理端使用 `accessctl user-stage --spec FILE`。请求文件必须是运行管理命令的服务 UID 所有、仅本人可读的普通文件；通过已安装的 `servicectl admin` 执行时，它会降到中央服务 UID，文件及父目录也须允许该 UID 读取。此步骤不创建 Linux 账号。

```json
{"user":"researcher_a","environments":["worker-python"],"quota":{"cpu":1,"memory_mib":512,"npu":0,"max_running":1}}
```

```bash
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- \
  accessctl user-stage --spec /var/lib/ascend-lab-controller/new-owner.json
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- accessctl user-status researcher_a
```

同一身份、目录、环境及额度重复登记返回相同 `stage_id`，不同定义拒绝。数据库一次提交禁用身份和不可用密码标记；不生成密码、令牌或 `client.json`。当前只允许受管 `worker-python` 环境；中央额度是申报及授权上限，不代表已经分配了节点 UID、存储或 NPU。

管理侧可为待预配身份登记项目角色和 Node 用户映射，用户仍不能认证。普通 `user-enable`、`token-renew`、额度修改或环境追加不能跳过预配阶段；未知或不完整的预配元数据也拒绝登录。完成资源各层交接后使用下述 `user-activate`，不要手工删标记或改数据库启用。历史 `user-add` 继续保留原有测试账号行为，不是统一开户流程。

离线恢复到新状态目录会重新计算待预配身份的定义摘要，账号仍保持禁用；旧预配计划须重新核对，不能沿用旧路径。存在待预配元数据时，代码切换拒绝降级到不认识该状态的旧身份实现。普通旧账号及其禁用/启用行为保持兼容。

## 中央新增 owner 存储组

中央必须已安装匹配版本，使用专用 ext4 组配额载荷盘，且已登记原 owner。先排空并核对执行/回执，再停止这个中央服务；管理操作需要独占状态锁，不能在线改正在使用的配额合同。

```bash
/opt/ascend-lab-controller/current/servicectl payload-owner-plan \
  --prefix /opt/ascend-lab-controller --owner researcher_a --gid 23004 \
  --mib 4096 --inodes 131072 --output /root/owner-storage-plan.json
/opt/ascend-lab-controller/current/servicectl payload-owner-apply \
  --prefix /opt/ascend-lab-controller --plan /root/owner-storage-plan.json
```

示例 GID 和额度须由管理员按实际分配替换。新 GID 不能与已有 owner 或服务主组相同，内核中必须没有数据或限额；即使既有限额恰好匹配，也不自动接管。硬额度须容纳待预配身份的输入/归档上限及元数据余量。规划同时核对总额和实际空闲字节/inode；不创建 Linux 组，不改变原 owner 的额度或文件归属。

计划绑定待预配 `stage_id`、禁用且无凭据状态、发布版本、完整配置基线、控制目录设备/inode、原 owner 用量及配额修订。先写 Root 私有日志，再追加原生组配额，依次发布配置、安装元数据、载荷绑定和回执。中断后重复同一计划恢复；未完成时阻断普通启动/备份/代码切换，以及原 owner 的另一配额调整操作。不要删除日志或另起计划绕过。

返回仍有 `owner_activation_still_required=true`。进程模式重新启动时根据新合同设置补充存储组；若使用 systemd，现有 unit 的 `SupplementaryGroups` 也必须按新合同更新并重新加载。现在可使用 `unit-refresh-plan` / `unit-refresh-apply` 在停机且静默时更新受管文件，保留原始备份并恢复中断；不会调用manager或重启。随后仍须管理员完成daemon-reload及所选服务启动并核对实际进程组。具体步骤见 [DEPLOYMENT.md](DEPLOYMENT.md)；真实PID1加载/重启尚未验收，不能仅凭文件已更新启用账号。

## 当前可用的存储步骤

操作要求 Root、匹配的已安装 Node 代码、已登记的专用 ext4 UID quota 文件系统。先结束并核对全部执行尝试、产物回执与缓存租约，再停止选定 Node。运行中的服务持有共享状态锁，管理操作获取不到独占锁时拒绝；未知/未清理任务不会被强制取消或遗忘。

准备 Root 私有的新增 UID 请求，只列新分配，不重复原 UID。示例额度须根据实际磁盘及任务需求替换：

```json
{"users":[{"uid":23004,"bytes":134217728,"inodes":8192}]}
```

```bash
/opt/ascend-lab-node/current/servicectl workspace-add-plan \
  --prefix /opt/ascend-lab-node --root /srv/ascend-lab-work \
  --limits /root/new-task-uids.json --output /root/uid-add-plan.json
/opt/ascend-lab-node/current/servicectl workspace-add-apply \
  --prefix /opt/ascend-lab-node --plan /root/uid-add-plan.json
```

计划绑定安装版本、Node ID、完整配置摘要、控制目录设备/inode、原文件系统标记及新增槽位身份。实际字节/inode总额仍留元数据余量，另检查物理空闲量。已有数据或限额的未登记 UID 不自动接管；原 UID、路径、槽位、限额和数据不得被计划修改。

应用先在控制盘写持久日志，随后在配额盘写同一计划的 intent，再设置新 UID 配额、创建 Root 私有空槽、发布标记和回执。中断后保留原计划并重复同一`workspace-add-apply`，不要删除日志、改配额或重新准备整个盘。日志未完成时，普通 Node 验证、启动、备份及代码切换被阻断。其他计划、已占用路径、改变的 Node 配置或过期文件系统绑定会被拒绝；不能通过重试另一个计划绕过。

这是原生存储预配：不创建宿主账号、不格式化/挂载系统盘、不改变 Node 登记或配置、不增加任务可见设备。返回的新 policy 还需要后续受保护的绑定流程，不能直接覆盖运行中的配置；私有缓存 policy 改变还涉及已有绑定的迁移，不能清空其他用户缓存来代替。

## Node 本地用户、工作区及缓存绑定

该步骤要求已安装匹配代码的 Root Node、现有 UID 配额工作盘和独立私有缓存盘。先停止并核对全部执行、回执和缓存租约，再用前述原生槽追加命令在两个已登记盘上为同一新 UID 准备槽位。旧 UID、文件、缓存目录及限额保持原样。

准备 Root 私有请求，槽位 ID 使用实际原生追加结果，`stage_id` 使用中央待预配身份的定义摘要：

```json
{"owner":"researcher_a","stage_id":"替换为中央登记的64位十六进制摘要","uid":23004,"gid":23004,"workspace_slot":"替换为工作槽ID","cache_slot":"替换为缓存槽ID"}
```

```bash
/opt/ascend-lab-node/current/servicectl node-owner-plan \
  --prefix /opt/ascend-lab-node --spec /root/node-owner-spec.json \
  --output /root/node-owner-plan.json
/opt/ascend-lab-node/current/servicectl node-owner-apply \
  --prefix /opt/ascend-lab-node --plan /root/node-owner-plan.json
```

计划固定原配置、控制目录身份、原生源标记、所有旧缓存控制记录/命名空间身份及用量；只追加一个用户映射、一个工作槽和一个缓存槽。旧缓存数据不复制、不删除、不改归属、不换命名空间 key。应用时先写恢复日志，再同步旧槽的控制与磁盘绑定、初始化新槽、发布配置/安装元数据和回执；旧缓存控制 generation 增加以使旧管理计划失效。存在活跃租约、旧缓存元数据已变化、槽位有未知数据或其他未完成原生追加时拒绝。

中断后保留原计划，重复同一 `node-owner-apply`。完成后有 `central_acknowledgement_still_required=true` 和 `startup_blocked=true`，Root 私有状态留下 `node-owner-awaiting-controller.json`。普通启动/维护、直接 Worker 启动、另一原生 UID 追加和代码回滚都被阻断；不能删除标记或手工改中央 profile 绕过。

Node 本地接受管理员提供的stage摘要，仍需下述中央批准和ACK才能恢复启动。不能把本地回执当作中央授权；新账号此时仍无登录凭据。

## 中央批准及Node确认

中央服务须运行支持 `owner-handoff-v1` 的代码；旧运行实例会被管理工具拒绝。把Node输出的原始计划复制到中央的私有管理目录，并交给中央服务UID所有、仅本人可读。该计划包含预配元数据，不需要个人SSH、模型密钥或会话内容。

```bash
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- \
  workerctl owner-approve worker-替换为实际ID --plan /var/lib/ascend-lab-controller/node-owner-plan.json
```

推荐在Node执行 `node-owner-apply` 前批准。中央核对真实禁用/无凭据身份、stage摘要、原Node映射及策略修订、中央硬配额和其他Node的存储分配；有在途任务或物理执行清单hold时拒绝。批准后该Node暂停新领取，其他Node继续受原资源域约束；配置修改与不兼容代码切换被阻断。待交接节点即使撤销凭据，其存储也保持分配，不能被新登记接管。

完成本地绑定后，在同一Node执行：

```bash
/opt/ascend-lab-node/current/servicectl node-owner-ack \
  --prefix /opt/ascend-lab-node --plan-id 替换为原计划64位摘要
/opt/ascend-lab-node/current/servicectl start --prefix /opt/ascend-lab-node
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- workerctl owner-status worker-替换为实际ID
```

Node使用本节点凭据调用专用HTTP接口，普通个人token无权代办。中央先原子提交匹配登记并返回稳定commit ACK；Node落盘后更新本地ready投影并请求ready ACK，保存最终回执后才清理启动阻断标记。网络超时或进程中断后重复同一命令；不另建计划、不删除进度文件。中央收到更新后Node的健康心跳才记录 `node_online_seen=true`，此前仍不允许继续修改Node配置。身份始终待预配，后续还需最终激活/凭据发布步骤。

撤销节点凭据始终有效；若交接中轮换密钥，先完成已有本地绑定，再只更新实际Node配置中的 `node_key`，保留已绑定的映射/缓存投影，随后用原计划继续ACK。不要把旧登记配置整体覆盖到已绑定的Node。当前验证是同机隔离角色和进程模式，不是两台真机或systemd PID1验收。

## 已验证范围

最后激活：实际安装的中央CLI、服务UID23820与Root Node，明确指定原交接计划。激活同一操作在intent/trust/client/before_commit/committed/receipt/published七个边界连续SIGKILL恢复；最终只有一份令牌。Root管理员将0600凭据复制给私有0700目录的个人UID23802，由该UID真实调用API、上传输入、幂等提交同一个IR任务并读取产物；peer UID23801无法读个人凭据，另一个应用账号无法读该任务，个人token不能调用Node ACK接口。受控任务实际UID23802、零capability/NoNewPriv1，真实BishengIR编译19+23=42、单次尝试、归档和清理通过。之后管理员禁用账号，重复激活不重新启用或签发。暂存盘为新建1024MiB/262144inode；没有放宽现有准入。该激活阶段完整515项/无跳过，64个生产源码摘要与最终Root包一致。这是同机数值UID模拟，没有创建宿主账号；环境由可信fixture管理员登记，不是新保护探针/NPU/systemd验收。

中央/Node交接：同一隔离Root角色内的实际非Root中央UID23820与Root Node，真实stage定义、中央GID追加和HTTP节点认证。旧本地10断点后，ACK另有9个连续SIGKILL边界(含中央已提交但本地未落盘、AWAIT已删除但进度未清理)及同计划恢复。中央/本地投影、两阶段稳定ACK和本地回执独立一致，实际Worker健康心跳确认后可继续配置管理；新账号仍禁用、密码标记不可用、token0。没有新用户凭据交付或队列任务验收；不是两机、systemd或新的中央保护环境探针验收。

Node本地绑定：真实安装、fixture stage摘要、独立的两个128MiB ext4 UID盘；同一计划的10个连续持久化边界SIGKILL、原计划继续恢复和CLI回执重放通过。活跃缓存租约、服务共享锁及变化的旧控制记录被拒绝；暖命中前完整旧控制元数据符合预期迁移，仅绑定和generation变化。旧UID23801的真实BishengIR答案42迁移后仍命中原inode/UID/GID/SHA，新UID23802另编译成功。交接未获中央确认时，普通/直接Worker启动、原生追加和代码回滚均被拒绝。该验证没有连接中央，也没有启用账号。

中央存储组：实际安装的中央和CLI、服务UID23820、256MiB文件后备ext4组配额盘。7个持久边界真实SIGKILL后原计划恢复，旧owner哨兵数据与限额不变；过期计划、改变的stage定义和未登记既有限额在新配额变更前拒绝。零capability可信写入进程在新增GID触发EDQUOT、其他组仍可写；新数据存在时回执重放和进程重启后的补充组通过。真实前一公开代码60c2002回滚/再升级保留配置和数据；新工具拒绝跨版本修改旧安装。不是用户任务容器、Node绑定或真实systemd验收。

自有 256MiB 文件后备 ext4、实际安装的 Node 代码、数值 UID 进程完成：安装后 CLI 计划/应用、服务共享锁拒绝、7 个命名持久化边界的 SIGKILL及原计划恢复、旧 UID 数据/限额保持、新 UID 写满触发 EDQUOT、完成后带数据重复应用、过期计划和已有路径/限额拒绝。自有容器、卷和 loop 关联已清理。该 Node 配置使用测试注册信息且未连接中央；不证明开户、真实宿主账号、两机或掉电恢复。

以真实的前一公开代码树`34d33c4`重建旧包，完成代码回滚与再升级并保留原数据/配置。历史服务包可缺少新的管理附加模块，当前bundle仍必须包含它；新 UID 管理要求运行代码与目标已安装模块匹配，不能直接用新管理工具修改缺少恢复保护的旧 Node。这是相邻版本兼容证据，不承诺任意历史版本可互换。

## 整体开户仍需完成

1. 已有中央不可登录的待预配身份、定义摘要及项目预登记；仍须与后续各层操作组合。
2. 已有中央 owner GID/硬配额追加、Node 原生槽与本地用户/工作/缓存绑定原语；仍需中央协调及 systemd 组更新。
3. 已有中央批准、登记提交与Node持久ACK、健康心跳观察；实际两机接入和真实systemd仍待验收，不要求中央持有个人SSH或模型密钥。
4. 已有最后校验、账号激活与可恢复私有凭据发布，新增本地个人UID交付及最后两步的一次请求。仍需先配置目标UID目录和同盘Root spool；未自动对接组织身份或远程个人主目录，也不创建宿主账号。

当前阶段不会把底层槽追加成功当成这四步已完成。最新验收与缺口见 [PROGRESS.md](PROGRESS.md)。

## 最后激活与私有凭据

准备由中央服务UID所有、0600的请求文件。必需节点列表不得为空，必须逐一列出原始交接计划；节点离线时不能通过省略该节点来代表同一激活操作。

```json
{"owner":"researcher_a","stage_id":"替换为中央stage摘要","required_nodes":[{"node_id":"worker-替换为实际ID","plan_id":"替换为该节点原始64位计划摘要"}]}
```

```bash
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- \
  accessctl user-activate --spec /var/lib/ascend-lab-controller/activate-owner.json
```

最小任务校验同时要求用户内存额度至少256MiB、归档额度覆盖2MiB产物和既有8MiB元数据预留。系统核对原stage定义、禁用且无凭据身份、中央原生配额、每个指定Node的ready回执和当前完整profile、30秒内健康心跳、环境、工作/缓存槽和暂存容量。最低工作流的暂存字节/inode门槛复用实际调度准入函数。中央开户进程及正在运行的同UID中央服务必须都具备新owner存储组；因此旧systemd组列表不能被一次管理CLI的正确组列表掩盖。中央服务须运行支持 `owner-activation-v1` 的代码。NPU额度非零时，还要求各必需节点有当前本用户设备访问资格；当前CPU开户演练不证明NPU开户，缺资格时保持禁用，不自动启动设备探针。

在冻结Node账本和身份事务后，先持久保存私有凭据intent及个人client文件，再一次提交密码哈希、token摘要、启用位和激活记录，最后发布无密码/token的交付回执并清理intent。密码/token仅在服务私有文件中，命令输出只含路径和回执；默认个人token期限24小时，仅jobs读/提交/取消。本地个人目录可使用前述owner-finish/owner-delivery交付；其他主机仍通过可信加密渠道交给对应实际用户，个人目录0700、文件0600，不上传网站或共享目录。

中断后保留文件并重复同一请求，不另建凭据或覆盖已有个人配置。提交后管理员禁用、撤销或续发的状态优先，恢复不得复活旧令牌或账号。未完成时备份/代码切换被阻断；已有激活记录拒绝降级到缺相应恢复支持的代码。已完成激活的离线恢复会重定位交付回执路径。若未提交intent已过期或资源上下文改变，可使用下述显式中止流程，再按现有就绪条件重新激活；私有回执丢失或账号已提交时仍须核对原操作，不能手工删记录绕过。


## 未提交激活的显式中止与重新激活

管理命令只在中央本地服务身份下执行，没有用户/节点可调用的远程中止接口。先用状态命令取得原始32位激活ID，不要为了找ID而打印含密码/token的pending文件：

```bash
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- \
  accessctl user-activation-status researcher_a
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- \
  accessctl user-activation-abort-plan researcher_a \
  --activation-id 替换为状态中的原始ID \
  --output /var/lib/ascend-lab-controller/abort-plan.json
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- \
  accessctl user-activation-abort-apply \
  --plan /var/lib/ascend-lab-controller/abort-plan.json
```

计划输出需是服务UID所有的私有目录中的新文件，已有文件拒绝覆盖；计划、journal和回执仅含绑定及文件指纹，不复制明文凭据。只允许原stage定义仍有效、账号禁用/不可用密码标记、从未发放token且无激活提交记录的操作；已经提交的账号包括停在最后回执阶段的账号，须走禁用/撤销流程，不能中止回退。中央仍运行旧代码时先升级/重载，活跃实例须声明owner-activation-abort-v1。Node离线、意图过期或资源上下文改变本身不妨碍中止。

中止绑定原激活ID、stage摘要、控制及个人目录身份、原intent和相关client/CA文件的设备/inode/时间/权限/SHA。先保存持久journal，再删除完全匹配的本次client及由新意图标记为本次创建的CA，提交永久已中止ID记录和审计，写回执后清原intent和journal。原有CA以及旧意图未记录归属的CA保留；未知或变化的文件拒绝删除。账号保持禁用，不新建token。八个连续进程中断边界可按同一计划恢复，期间普通激活和离线备份/代码切换拒绝；不要删除journal另起计划。存在已中止ID时拒绝降级到缺少相应恢复支持的代码。

完成后可按同一既有stage/资源合同重新执行user-activate，产生新激活ID和凭据。旧意图即使被重新放回也不能启用，旧中止计划的完成重放保留后来新建的凭据。中止不会删除用户、回收中央GID、Node UID槽/缓存或修改资源/环境合同；资源合同改变后的重授权与完整一键协调仍待实现。只清理中央服务私有目录，不接管个人主目录或外部副本。

当前同机安装演练使用实际服务UID23820：真实激活在client落盘后SIGKILL，模拟意图过期，显式中止再经历journal/client/trust/before_commit/committed/receipt/intent_removed/published八次连续SIGKILL并恢复。中止后仍禁用、token0、已中止ID1；重新激活产生不同ID，旧Basic密码及Bearer均401，旧中止重放保留新配置；随后个人UID23802完成真实单次排队BishengIR编译和归档清理。原有用户配置未改，实际systemd管理器和NPU未参与本次验收。

## 已激活用户接入另一个 Node

新增独立的 `owner-expand-plan/apply`，不放宽 `owner-approve` 的禁用且无凭据条件。已有激活标记和提交记录的用户，须在中央服务身份下为新 Node 明确批准。原始 stage ID 在 Node 计划中只引用用户来源；新的 grant ID、当前权限指纹和完整 Node 计划摘要共同构成此次授权。没有重新发放或复制凭据，也不修改原 Node 的 UID、环境和缓存。

先按前面的 Node 本地流程安装新节点、追加原生工作/缓存 UID 槽，并生成它自己的 `node-owner-plan`。将该私有计划按已有受保护交付渠道交给中央服务 UID；不向中央授予 Root SSH 或个人 HOME 读取权限。中央运行：

```bash
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- \
  workerctl owner-expand-plan worker-替换为新节点ID \
  --plan /var/lib/ascend-lab-controller/new-node-bind.json \
  --output /var/lib/ascend-lab-controller/new-node-grant.json
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- \
  workerctl owner-expand-apply \
  --plan /var/lib/ascend-lab-controller/new-node-grant.json
```

命令要求同服务 UID 的活跃中央声明 `owner-node-expansion-v1`。计划输出必须是新文件；批准时再次核对用户、原生中央存储额度、目标登记投影和其他节点的槽位预留。获批但尚未提交的工作/缓存槽也计入占用。Node 管理员随后应用同一原始 Node 计划，并按原来的 `node-owner-ack` 完成 authenticated commit/ready；首次健康心跳前不接新任务。Node 无法通过用户凭据调用此 ACK API。

首次健康心跳前，用户禁用、凭据更新或撤销、配额、环境、项目成员关系及中央存储绑定变化，会使原 grant 失效。身份数据库触发器持久记录单调权限版本；改动后恢复相同字段也不能复用旧授权。批准、ACK和首次健康心跳在持有身份写保留锁时提交任务数据库，避免在最后检查与提交之间漏过撤权。未知执行和不空闲的目标节点仍拒绝扩容。

明确审阅新的权限状态后，可用**同一原始 Node 计划**再次运行 `owner-expand-plan`，增加 `--reauthorize`，输出到另一个新文件后 apply。此操作只替换尚未完成的授权，旧 grant 标记 superseded；保留原审批/ACK标识和已提交的节点资源投影，不再次应用本地绑定、不改槽位或重新发放 token。已经完成首次健康心跳的 grant 不能这样重新授权修改资源。资源容量、路径或 UID 需要改变时，仍须另行设计相应资源变更协议。

交接完成后的 apply/ACK 重放仅返回历史结果，不作为当前权限或健康证明，也不恢复后来被禁用的账号。实际任务继续检查当前用户权限。数据库中存在扩容授权记录后，拒绝降级到缺少该协议的发布包。此增量不等于完整一键开户、远程管理员操作自动化、真实两台物理机或 NPU 验收。

2026-10-31验收：590项检查及JS语法通过，包含新增12项扩容检查。同机隔离Root使用实际服务UID23820、原个人UID23802及第二Node任务UID23812，原凭据仅一个、不新增；通过安装后的CLI计划/批准、10个绑定与9个ACK连续SIGKILL、两次改后恢复权限导致拒绝并明确重授权、真实个人客户端排队BishengIR `19+23=42`、单attempt及产物读取。原Node登记投影、已有缓存文件inode/SHA及个人配置保持不变，七个自有盘均释放。完整记录保存在本地私有证据，公开范围见reports/public-source-check.json.owner_expansion。此验收未创建真实宿主账号，未使用第二物理机、NPU或实际systemd PID1。

## Node侧原生资源与绑定的单请求协调

`node-owner-provision` 是完整开户流程的 Node 参与步骤：一个 Root 私有请求固定 operation_id、owner/stage、UID/GID、已登记的工作/缓存配额盘和额度。默认命令会**实际追加原生配额槽**，随后生成唯一绑定提案，等待中央独立批准。它不创建 Linux 账号、不启动服务、不发个人凭据、不远程登录，也不把中央变成任意 Root 命令的执行入口。

节点管理员准备0600的请求文件。下例额度仅适合小IR演练；完整编译须按实际工作盘/缓存消耗配置。两个root必须是本Node已经登记并由管理员预先准备的独立原生UID配额文件系统；新UID不能已有数据、配额或节点映射。不要先手动追加同一UID再调用此协调器。

```json
{
  "operation_id": "0123456789abcdef0123456789abcdef",
  "owner": "researcher_a",
  "stage_id": "替换为中央已有身份的64位stage摘要",
  "uid": 23003,
  "gid": 23003,
  "workspace": {"root": "/srv/ascendlab-work", "bytes": 16777216, "inodes": 128},
  "cache": {"root": "/srv/ascendlab-cache", "bytes": 16777216, "inodes": 128}
}
```

```bash
/opt/ascend-lab-node/current/servicectl node-owner-provision \
  --prefix /opt/ascend-lab-node --spec /root/new-owner-node.json
```

返回awaiting_approval、proposal_id及Root私有proposal_path；运行同一请求保留原提案和资源，不重新分配。operation_id在本安装中唯一，同ID改请求即使旧流程已经完成/中止也拒绝。提案只包含原有Node身份/配置、原生槽及空闲缓存快照，不包含节点密钥或个人凭据。经受保护的管理员交付渠道将原提案转为中央服务UID所有的0600文件；新stage使用前述owner-approve，已激活用户增加节点使用owner-expand-plan/apply。当前该管理员交付和中央批准仍是明确步骤，不代表跨主机运输已自动化。

中央批准后，节点管理员运行：

```bash
/opt/ascend-lab-node/current/servicectl node-owner-provision \
  --prefix /opt/ascend-lab-node --spec /root/new-owner-node.json --complete
```

此命令应用同一原始绑定计划，经节点自己的凭据完成中央commit/ready ACK，再保存bound回执。中央尚未批准时确认失败，保留本地绑定和原请求，节点仍禁止启动；批准后重试同一请求即可继续。bound只说明资源绑定与ACK完成，仍须正常启动Node、取得当前健康心跳，再由中央激活/交付个人凭据。完成重放是历史结果，不代表现在健康或当前用户仍有权限。

若本地绑定已完成、正在恢复ACK时管理员撤销并更新节点密钥，可以仅将新node_key安全更新到原Node私有config；同一参与请求会用新凭据继续确认，保留原提案和ACK。此例外仅适用于acknowledging/acknowledged阶段，控制地址、信任配置、设备/存储及其他配置指纹仍必须完全匹配。绑定前更新凭据会使原计划失效，不自动改写或重签资源计划。

工作区/缓存的原始追加计划先于副作用持久化；Node共用一个原生追加journal，恢复只推进当前未完成步骤。提案写出但父阶段未更新时采用原文件，不重新生成；提案丢失或变动、安装/物理主机/非本流程配置改变、异物子事务或未知执行均拒绝。准备前通过独占状态锁排除现有Worker/租约；持久journal发布后，直接Worker、服务启动、普通验证/备份/升级和其他配额操作均阻断。内部恢复仅允许当前Root进程针对精确Node状态目录执行，不接受用户参数或环境变量绕过。

仅在原生准备阶段、尚未写出提案时可以加`--abort`。中止会完成已开始的原生子事务并保留已分配额度/目录，不分配尚未开始的另一盘，不删除数据或清空配额。已经写出提案后，无法凭本地状态判断中央是否批准，须保留原请求并与中央协调；不会直接中止或改计划。此行为避免把未知远端状态当作未批准。

单条控制记录最多12MiB，历史最多256个文件且总量128MiB，新请求预留后续元数据空间；遗留原子写入临时文件保留并计入有限预算。未知文件、权限异常或超限拒绝推进，不自动删除审计记录。这些是模块的应用边界，不能替代整个控制盘的内核配额或运维保留策略。

Node回执不能证明中央身份状态。`central_owner_activation_still_required`是新开户的保守前置提示；已经激活的用户扩新Node时，中央核对既有身份和当前权限，沿用原凭据，不因Node回执重新激活或发放token。

2026-10-31验收：607项检查和JS语法通过，新增17项参与步骤检查；72个非文档生产源码与最终Root包匹配。真实隔离Root中12个协调边界、14个原生配额边界、10个绑定边界、9个ACK边界共45次连续SIGKILL恢复，另有两个原生quota中断后的显式中止。一个中止保留work且cache未分配，另一个恢复正在进行的cache而保留已完成work。中央未批准时ACK拒绝且身份仍stage；批准后恢复同一提案。commit响应之后实际轮换Node密钥，旧密钥HTTP401、新密钥继续同一ACK。operation_id改请求、丢失提案、未知历史文件和未确认启动均拒绝。

随后原流程实际观察健康心跳、最后激活/个人UID交付、个人客户端单attempt排队BishengIR，以及第二Node扩容和原凭据复用仍通过；源用户禁用、中央停止后，Node完成请求仅重放历史。七个自有测试盘释放，旧用户映射/槽/环境及暖缓存IR的inode/SHA/namespace保留。首次Root发现并修复共享原生journal恢复顺序问题；另一轮测试因同一宿主目录的两个bind挂载之间rename触发EXDEV，修正fixture后通过。原始证据仅本地保存，公开范围见reports/public-source-check.json.node_owner_provision。中央身份/存储组/服务生命周期的总协调器、跨机受保护交付、真实PID1与NPU仍待；本增量不是完整一键开户已完成。
