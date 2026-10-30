# 用户预配：当前阶段与交付条件

当前提供**中央待预配身份/GID配额**、**Node原生槽与本地绑定**及**中央批准/Node ACK交接**。systemd组更新、最终身份激活及个人凭据交付尚未接通，完整开户仍未完成。

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

管理侧可为待预配身份登记项目角色和 Node 用户映射，用户仍不能认证。普通 `user-enable`、`token-renew`、额度修改或环境追加不能跳过预配阶段；未知或不完整的预配元数据也拒绝登录。当前还没有完成多层回执校验后的激活命令，不要手工删标记或改数据库启用。历史 `user-add` 继续保留原有测试账号行为，不是统一开户流程。

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

返回仍有 `owner_activation_still_required=true`。进程模式重新启动时根据新合同设置补充存储组；若使用 systemd，现有 unit 的 `SupplementaryGroups` 也必须按新合同更新并重新加载，不能直接沿用旧组列表。当前没有自动更新既有 unit 的操作，且真实 systemd PID1 尚未验收，这也是统一交付仍需补齐的部分。

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

中央/Node交接：同一隔离Root角色内的实际非Root中央UID23820与Root Node，真实stage定义、中央GID追加和HTTP节点认证。旧本地10断点后，ACK另有9个连续SIGKILL边界(含中央已提交但本地未落盘、AWAIT已删除但进度未清理)及同计划恢复。中央/本地投影、两阶段稳定ACK和本地回执独立一致，实际Worker健康心跳确认后可继续配置管理；新账号仍禁用、密码标记不可用、token0。没有新用户凭据交付或队列任务验收；不是两机、systemd或新的中央保护环境探针验收。

Node本地绑定：真实安装、fixture stage摘要、独立的两个128MiB ext4 UID盘；同一计划的10个连续持久化边界SIGKILL、原计划继续恢复和CLI回执重放通过。活跃缓存租约、服务共享锁及变化的旧控制记录被拒绝；暖命中前完整旧控制元数据符合预期迁移，仅绑定和generation变化。旧UID23801的真实BishengIR答案42迁移后仍命中原inode/UID/GID/SHA，新UID23802另编译成功。交接未获中央确认时，普通/直接Worker启动、原生追加和代码回滚均被拒绝。该验证没有连接中央，也没有启用账号。

中央存储组：实际安装的中央和CLI、服务UID23820、256MiB文件后备ext4组配额盘。7个持久边界真实SIGKILL后原计划恢复，旧owner哨兵数据与限额不变；过期计划、改变的stage定义和未登记既有限额在新配额变更前拒绝。零capability可信写入进程在新增GID触发EDQUOT、其他组仍可写；新数据存在时回执重放和进程重启后的补充组通过。真实前一公开代码60c2002回滚/再升级保留配置和数据；新工具拒绝跨版本修改旧安装。不是用户任务容器、Node绑定或真实systemd验收。

自有 256MiB 文件后备 ext4、实际安装的 Node 代码、数值 UID 进程完成：安装后 CLI 计划/应用、服务共享锁拒绝、7 个命名持久化边界的 SIGKILL及原计划恢复、旧 UID 数据/限额保持、新 UID 写满触发 EDQUOT、完成后带数据重复应用、过期计划和已有路径/限额拒绝。自有容器、卷和 loop 关联已清理。该 Node 配置使用测试注册信息且未连接中央；不证明开户、真实宿主账号、两机或掉电恢复。

以真实的前一公开代码树`34d33c4`重建旧包，完成代码回滚与再升级并保留原数据/配置。历史服务包可缺少新的管理附加模块，当前bundle仍必须包含它；新 UID 管理要求运行代码与目标已安装模块匹配，不能直接用新管理工具修改缺少恢复保护的旧 Node。这是相邻版本兼容证据，不承诺任意历史版本可互换。

## 整体开户仍需完成

1. 已有中央不可登录的待预配身份、定义摘要及项目预登记；仍须与后续各层操作组合。
2. 已有中央 owner GID/硬配额追加、Node 原生槽与本地用户/工作/缓存绑定原语；仍需中央协调及 systemd 组更新。
3. 已有中央批准、登记提交与Node持久ACK、健康心跳观察；实际两机接入和真实systemd仍待验收，不要求中央持有个人SSH或模型密钥。
4. 校验各层就绪后再启用身份和交付个人客户端凭据；中断不能留下已启用但无可用资源的账号。

当前阶段不会把底层槽追加成功当成这四步已完成。最新验收与缺口见 [PROGRESS.md](PROGRESS.md)。
