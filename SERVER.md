# 中央服务与节点安装

v0.36候选的`workerctl enroll --max-timeout-seconds N`设置节点私有配置的执行上限（默认300秒），心跳以`node-deadline-limit-v1`确认并在节点汇总公开。中央的`scheduler.max_timeout_seconds`默认仍3600秒，管理员可停机后显式配置为1至86400秒；工作流参数同样有86400秒硬上界。中央上限和节点实际上限须同时满足。新任务超过节点上报值时保持排队、0attempt/0计算预留；超过3600秒的任务也不会交给未确认此能力的旧节点。能力或上限变化不丢弃已有执行尝试，节点仍按原冻结许可处理。长任务安装者应同时为服务、工作盘和缓存准备足够的生命周期与容量；本功能不代表大型编译、硬件或多机已验收。

v0.35候选新增Root离线`payload-quota-plan`/`payload-quota-apply`，统一调整已登记owner的中央存储字节/inode限额，保留中断日志和修订回执。正常生命周期会阻断未完成调整；同计划可恢复。操作及明确范围见[STORAGE.md](STORAGE.md)，实际新包验收以evidence/validation-v035.json为准。

v0.34候选的中央/节点/个人客户端协议保持兼容；新增原生工具链示例仍使用`build_v1`及本人封存输入。任务镜像可以与中央/Node服务角色的镜像不同；固定任务Python/setup后，以保护探针锁定包内实际工具及兼容审计摘要。原生工作/缓存需独立充足字节与inode，不能直接复用小IR压力演练的128inode额度。新安装实际验证以evidence/validation-v034.json为准。

v0.33候选在文件和目录同步后确认输入/产物偏移，重置输入先撤销数据库前缀。备份格式和保护运行时合同未改变，现有输入不会自动重传；同步失败保留原偏移。隔离安装的强制终止与恢复证据以evidence/validation-v033.json为准，详见[STORAGE.md](STORAGE.md)。

v0.32候选由中央调度循环增量处理依赖输入，每次有限分块，Node只领取已就绪的封存输入。带依赖状态拒绝缺少此能力的降级；安装备份恢复保留任务边与交接证据。说明见[DEPENDENCIES.md](DEPENDENCIES.md)，完整Root验收仍以evidence/validation-v032.json为准。

v0.31候选支持workerctl enroll --cache-config、Node安装绑定及cache-list/cache-plan/cache-prune；控制恢复需要显式同盘--cache-config。准备与边界见[CACHE.md](CACHE.md)，完整Root/网页验收以evidence/validation-v031.json为准。

v0.30候选Node登记增加`workerctl enroll --spool-config ...`，doctor/install只消费Root预置的独立空ext4并建立私有绑定。Node控制目录恢复必须显式`restore --spool-config ...`且同一已核对空盘；新盘/新host登记新Node。详见 [SPOOL.md](SPOOL.md)；实际验收以`evidence/validation-v030.json`为准。

v0.29候选可通过`payload-groups-prepare --contract ... --plan`及无`--plan`准备专用空ext4上的owner组限额，再沿用doctor/install。中央仍无管理capability；部署进程/systemd只加入配置的存储组，迁移支持重新分配GID。详见 [STORAGE.md](STORAGE.md) 与`evidence/validation-v029.json`。

v0.28候选新增`servicectl workspace-prepare`，仅消费管理员预置的空专用ext4及已启用用户硬配额；先`--plan`再生成私有合同。Node doctor核验配额和控制目录分盘，systemd加入工作盘路径及所需查询能力。完整新包验收待运行，当前基线仍v0.27；详见 [WORKSPACE.md](WORKSPACE.md)。

v0.19：原生HTTPS、可迁移CA配置和同CA证书续期见 [HTTPS.md](HTTPS.md)。默认本机HTTP不变；管理员可显式配置对外TLS监听。

v0.18：节点执行清单、当前中央实例确认与物理域对账阻断见 [RECOVERY.md](RECOVERY.md)。新安装默认启用；旧配置需更新全部启用节点后显式切换。

v0.17：中央环境认证与资源预留前的版本匹配见 [ENVIRONMENTS.md](ENVIRONMENTS.md)。新安装默认启用；旧配置需在空闲节点认证并确认后显式启用。

2026-10-29，v0.10。此处的 root 命令面向管理员交付的新服务器；当前宿主机上不执行安装或写系统服务。安装、真实 IR、停机备份、新目录恢复、兼容切换与回滚已在自己容器中演练；systemd 文件只做语法验证，未在 systemd PID1 下验收。完整设备、磁盘配额及实际多服务器验收仍未完成。

## 安装前只读计划（v0.16）

先运行doctor（别名preflight）；它不创建安装目录/账号、不启动任务、不拉取镜像、不改变Docker context，也不初始化ACL/NPU。输出JSON，install_ready=true时退出0；有阻断项或需要管理员权限时退出2。无--archive/--prefix/--state-dir时可查看部分条件，未检查项保持未通过。

```bash
./servicectl doctor --role controller --archive /私有交付目录/ascend-lab-server.tar.gz \
  --prefix /opt/ascend-lab-controller --state-dir /var/lib/ascend-lab-controller \
  --create-service-user ascendlab --port 18765

./servicectl doctor --role node --archive /私有交付目录/ascend-lab-server.tar.gz \
  --prefix /opt/ascend-lab-node --state-dir /var/lib/ascend-lab-node \
  --node-config /私有交付目录/node.json
```

检查发布包内容/完整SHA、可移植绝对路径与Root保护的祖先目录、专用服务身份、端口、节点物理ID、CPU/内存/SHM/槽位、非root UID映射、固定本地镜像、实际Docker硬限制和已配置的工作区/NPU静态合同。中央账号必须能穿过已有代码/数据祖先目录，不能选/root等仅Root可遍历的位置；数据目录本身会分配给服务UID并保持0700。新服务账号/同名组冲突会在创建前拒绝。配置内容、token、node_key不写报告。节点Docker总内存与Node服务进程自身cgroup分开检查，不能将128MiB节点服务误当作整个任务池。工作区未配置时task_dispatch_prerequisites=false；新中央不会向这种节点降级分配无硬边界任务。

install会在任何安装目录/账号变更前再次执行同一预检；之后重新校验包与节点配置，变化则拒绝。install_ready表示安装前提满足，**production_ready仍为false**。实际任务、NPU初始化/精度、磁盘硬配额、多机恢复与systemd生命周期均明确为not_run，需按DEPLOYMENT.md另行验收。预检不预留端口/资源，运行时仍重新准入。

代码包验证和解包共享流式解析器，拒绝链接、路径冲突、缺失/篡改文件；压缩文件32MiB、解压tar流20MiB、代码内容16MiB/单文件4MiB上限，PAX/尾部填充也受限。该限制仅针对服务源码发布包，不是用户wheel/归档上限。安装后的install-client和labctl保留0555，可直接执行，个人客户端仍不含中央凭据。

## 固定本地Docker执行位置

节点默认拒绝TCP/SSH/npipe或rootless Docker后端；Root保护的本地Unix socket与daemon ID在安装后私有config及docker-runtime.json中固定，每个执行尝试再冻结绑定。所有创建、查询、日志、只读归档和清理均显式指定socket，重新核验daemon身份/硬限制，清除DOCKER_*路由并使用空CLI配置；不读取个人Docker代理/认证/HttpHeaders。代码目录中的_docker_cli_empty必须保持不存在，发现已有对象就拒绝，不删除或读取它。创建和helper均--pull never。

已有绑定的节点忽略后来继承的DOCKER_HOST/CONTEXT/CONFIG；未绑定的新安装则检查当前context实际选中的端点，远程端点被拒绝。Docker官方说明DOCKER_CONTEXT会覆盖DOCKER_HOST，所以两个变量与默认context都需处理。[Docker CLI说明](https://docs.docker.com/reference/cli/docker/)

新中央默认scheduler.require_pinned_docker=true，节点心跳声明并验证pinned-docker-runtime-v1才接受新任务；旧在途许可仍可重连，不因能力变化释放资源。当前个人Demo可在更新空闲节点后启用该开关，旧安装升级不自动改配置。普通用户不能覆盖绑定或此策略。

daemon身份变化、Socket不可达或历史尝试没有可信绑定时，不猜测执行位置、不删除容器、不把未找到误认成结束，保持needs_attention/资源占用。先停止新分配并由管理员核对旧daemon/实际容器；不能仅删除绑定文件来消除错误。历史无绑定的在途尝试需单独对账。跨主机迁移节点应确认旧任务清退后重新登记；当前不提供跨daemon在途迁移或完整中央fencing。Root/Docker管理员仍属于可信边界，本地socket也不证明不存在管理员配置的代理。

安装包由标准库生成，只包含显式列出的服务源码、示例与文档，不含 runtime、个人模型配置、密钥或会话：

```bash
./servicectl bundle --output /私有交付目录/ascend-lab-server.tar.gz
```

管理员校验源码包哈希、将可信安装源放在 root 控制的目录后，安装一个新中央实例。代码目录与数据目录必须分开，路径不能包含空格/遍历/符号链接；安装器拒绝覆盖已有前缀和非空数据目录。预先创建的空数据目录必须属于 root，避免从其他账号控制的目录接收特权节点状态。

```bash
./servicectl install --archive /私有交付目录/ascend-lab-server.tar.gz \
  --role controller --prefix /opt/ascend-lab-controller \
  --state-dir /var/lib/ascend-lab-controller --create-service-user ascendlab --port 18765 --start
```

也可用已准备好的专用非 root UID/GID 代替 --create-service-user。中央身份不得有 root/Docker 组权限。安装器不全局安装 Python 包、Docker、模型或 Agent，不修改已有实验容器。中央默认只绑定 loopback，不做本地执行/个人进程采集；远程通过管理员配置的 HTTPS 反向代理或 SSH 隧道接入。

版本目录 root 拥有、服务用户只读；私有数据和配置分别为 700/600。初始管理登录在数据目录的 config.json，独立于 SSH 密码，不打印到安装输出。个人账号通过管理工具创建并分别交付个人 profile，不能分发中央 config.json。

--start 在安装后以配置身份启动进程模式；不加该选项只安装。后续可独立管理：

```bash
/opt/ascend-lab-controller/current/servicectl start --prefix /opt/ascend-lab-controller
/opt/ascend-lab-controller/current/servicectl admin --prefix /opt/ascend-lab-controller -- accessctl user-add alice
```

节点先由中央登记物理标识、不可变镜像 ID、资源预算与用户 UID 映射，私有 node config 通过管理员安全通道传到目标服务器。节点 config 的 physical_id 必须属于该目标服务器。安装器检查本地已存在的镜像，不自动拉取镜像或安装 Docker：

服务端工具需要系统 Python 3.10+。节点安装还检查 Docker 的 CPU/cpuset、内存/交换区及 PID 限制能力；缺少其中任何一项就拒绝安装，不能把不生效的参数当成隔离。

```bash
./servicectl install --archive /私有交付目录/ascend-lab-server.tar.gz \
  --role node --prefix /opt/ascend-lab-node --state-dir /var/lib/ascend-lab-node \
  --node-config /私有交付目录/node.json --start
```

节点是受信任的 root/Docker 管理角色，任务仍以独立非 root UID 运行。节点已有可选 NPU 设备白名单接口，但实际初始化/数值验收未通过；工作区已支持有限文件系统槽位并通过 tmpfs 演练，真实磁盘、输入/归档和缓存硬配额仍缺。安装完成不代表可以向全体用户开放任意规模构建。

安装器生成 systemd 文件，可显式安装并启动新服务：

```bash
/opt/ascend-lab-controller/current/servicectl unit-install --prefix /opt/ascend-lab-controller --enable
```

已有同名 unit 时拒绝覆盖。进程模式和 systemd 选择一种管理方式。systemd 的只读文件系统设置使用 ReadWritePaths 放开本实例数据目录；UNIX socket 的连接不因只读挂载而受阻。节点 Docker 权限仍属于受信任边界。[systemd 执行环境文档](https://www.freedesktop.org/software/systemd/man/256/systemd.exec.html)

## 停机维护

start/status/stop 校验本实例的进程、启动时间、UID、代码版本与参数，不向任意 PID 发信号；观测超时不代表任务已结束，不盲目启动第二份服务。停止节点不会取消其容器，容器保留独立超时，确认完成后才能备份或切换。

进程模式使用系统 /usr/bin/python3；停止依赖 Linux/Python 的 pidfd 支持，避免 PID 复用误杀。Root 停止非 root 中央进程时由服务身份发送信号，不要求给受限管理容器增加 CAP_KILL。旧系统缺少 pidfd 时使用已配置的 systemd 管理，不回退到未经身份绑定的信号。

```bash
./servicectl stop --prefix /opt/ascend-lab-controller
./servicectl backup --prefix /opt/ascend-lab-controller --output /root/私有备份/controller.tar.gz
./servicectl activate --prefix /opt/ascend-lab-controller --archive /root/新版本/server.tar.gz
./servicectl start --prefix /opt/ascend-lab-controller
```

中央必须没有排队、运行或状态不明的任务；节点必须没有未完成/未确认回执的执行尝试。服务和管理工具持共享状态锁，离线操作取排他锁，不能在线随意复制 SQLite。备份对数据库做 SQLite backup/integrity_check，其他文件流式哈希。备份含私有凭据，要求 root 私有父目录和 600 文件，不上传网站。

仅支持当前状态 schema 的兼容代码切换；没有实现任意未来数据库迁移。旧版本代码保留，可停机后用 activate --release-id 选择旧版本，不丢弃当前数据。先保留验证过的停机备份，再切换；崩溃时若链接/元数据不一致会拒绝启动，需要管理员检查，不推测或重跑任务。

恢复到一个新数据目录，保留原目录：

```bash
./servicectl restore --prefix /opt/ascend-lab-controller \
  --archive /root/私有备份/controller.tar.gz --new-state-dir /var/lib/ascend-lab-controller-restored
```

恢复校验成员路径/类型/大小/哈希、数据库完整性、无在途任务和角色/schema，并迁移中央账号工作区与归档地址。生成的 unit 随路径更新；如已安装 systemd unit，需管理员同步新文件并 daemon-reload 后再启动。跨主机迁移必须先隔离旧中央并协调节点，不能同时启动两份资源权威；当前不提供自动高可用、在途灾难恢复或 epoch/fencing。

## 物理资源域与并发节点

先查看中央已有预算。新远程服务器必须先登记物理资源域，再 enroll 节点；physical_id 用目标服务器 workerctl identity 获取，经管理员核对后传入。下面的 ID、CPU、容量需按目标机器实际值替换，不能照抄。

```bash
./workerctl domain-list
./workerctl domain-set 目标服务器的64字符physical_id --cpus 0,1,2,3 \
  --memory-mib 8192 --shm-mib 1024 --max-running 4 --expected-revision 0
./workerctl enroll --name server-b --physical-id 目标服务器的64字符physical_id \
  --controller-url https://lab.example.org --node-state-dir /var/lib/ascend-lab-node \
  --cpus 0,1,2,3 --memory-mib 8192 --shm-mib 1024 --max-running 4 \
  --image sha256:替换为管理员固定的本地镜像ID --user-map alice:23001:23001
```

安装后的中央通过 servicectl admin -- workerctl 执行对应操作。域预算仅能由可访问中央私有状态的管理员变更；节点心跳和用户请求不能改预算。初次登记修订号为0，修改时传当前修订号；不匹配返回冲突，需先重新查看。预算变更和drain记录在任务数据库，不包含凭据。现有本地域首次从scheduler初始化，之后显式维护；本地CPU/内存/SHM不能超出该部署配置的安全范围。

每个受控任务额外计128MiB执行开销，节点和物理域都按“申报内存+128MiB”准入。例如两个256MiB任务至少需要768MiB节点/域预算。辅助容器CPU仍绑定本任务获批核心，任务完成清理和确认后才释放整个预留。系统/节点服务与未托管工作负载另留容量余量；本功能不提供磁盘/缓存硬配额。

```bash
./workerctl domain-drain 实际physical_id --expected-revision 当前修订号
./workerctl domain-events 实际physical_id
./workerctl domain-drain 实际physical_id --expected-revision 最新修订号 --resume
```

排空暂停新分配，已有预留仍可恢复并完成；这不是取消。中央已排队的任务仍保留，停机备份维持“无排队/运行/未知任务”的规则。不能强行缩减到当前预留以下，也不能移除仍在使用的CPU。多槽节点与控制器需匹配新版协议；v0.8/v0.9旧服务包缺少本版所需模块，不属于本版本可直接回滚的兼容代码。切换前保留排空后的私有备份，并验证候选包能力。

修改已有节点预算时，先在目标服务器停止该节点daemon，并确认没有在途或状态不明的尝试。中央私有管理工具更新策略和修订号，并将更新后的私有config传回目标节点再启动。旧修订心跳会被拒绝；CLI写config失败时以中央策略为准，不能继续运行旧配置。

```bash
./workerctl stop --config /目标节点私有目录/config.json
./workerctl node-policy 实际node_id --cpus 0,1 --memory-mib 2048 \
  --shm-mib 256 --max-running 2 --expected-revision 当前节点修订号
./workerctl node-events 实际node_id
```

节点策略修订与事件保存在中央事务中；运行/未知任务存在时拒绝修改，不提供强制覆盖。这里的节点修订号与物理域修订号各自独立，初始值均为1。默认工作流申报512MiB，因此节点总预算至少需要640MiB；默认512MiB节点仍能运行256MiB的CPU示例。

## 可选NPU节点（当前未完成实机验收）

CPU安装路径保持可用。NPU需管理员在登记时显式提供--npu-config，合同文件为本人600权限JSON；不会自动发现并授权全部设备，也不自动安装驱动或Ascend Docker Runtime。格式、验收门禁和当前失败见NPU.md。登记后仍须通过node-templates明确授权npu_access_probe、所需五类工作流或旧npu_probe诊断，并传回私有配置/重启节点；用户还需相应NPU配额。v0.27普通设备工作流须先通过绑定用户/卡/环境的固定访问资格；node-device-probe/node-device-status见NPU.md。未知或失败时排队不占资源；旧npu_probe不授予资格。升级中央和节点后先显式验收，不自动安排上板探针。

管理工具和CANN管理环境路径属于节点，environment_setup与镜像则属于任务环境；不能将两者版本混为一份兼容性保证。受控设备路径由管理员固定，任务只有设备组作为附加组，仍是非root/零capability/无网络/只读根。驱动路径只读挂载，不能传入任意目录。当前不支持多卡单任务。

推广前必须在目标驱动/镜像组合验证可见设备与物理ID映射、非root ACL初始化和实际数值用例。当前新容器probe的raw open返回EBUSY，现有容器烟测在aclInit失败；尚未确认是容器共享、运行时、身份还是其它驱动条件，不把查询健康当成兼容性通过。不要为了验收开启privileged、改成root任务、共享host IPC、重置设备、重装驱动或更改全局共享模式。需要改变共享机器配置的方案应由管理员另行安排迁移维护窗口。

## 个人归档发布（v0.11）

服务包包含无凭据install-client及client_archive.py。管理员可以从安装后的只读current代码向用户发布独立CLI/MCP；用户的profile、输入来源记录、归档偏好和下载进度均位于个人目录，不放进服务包。中央只提供按原owner/project ACL授权的终态清单和分块读取，不写入用户主目录；个人机器和节点不要求共享/home。

个人使用方式见CLIENT.md；新增接口见API.md。MCP的可选自动归档由启动参数固定路径，模型不能改变；harness提前关闭不会自动后台下载，重连get_result或CLI archive即可续传。默认仍为64MiB单文件/任务输出128MiB；v0.13可由管理员显式增大合同，已有大于1GiB wheel验收，见STORAGE.md。归档功能需要相应版本中央接口；不支持时明确失败，不绕过API读取中央目录。旧版代码切换仍只在排空后进行，不承诺任意版本回滚兼容。
# 可选文件系统工作区与生产默认准入（v0.12）

新增`workerctl enroll --workspace-config /管理员/私有合同.json`。管理员先在目标节点准备独立、有限容量/inode的专用文件系统，再登记路径、实际容量与身份；节点安装和运行核对实际挂载，不将普通目录当成硬配额。新安装中央默认scheduler.require_bounded_workspace=true，所有托管任务都需要受限槽位；旧个人Demo保留false兼容协作式入口。每个槽位独占到容器终止、归档、清理和完成确认，不根据磁盘瞬时空闲释放未知任务。

本机Root演练为32MiB/512 inode的自有Docker tmpfs卷，已经实际触发字节/inode ENOSPC并清理后执行后续IR。tmpfs需计入内存且不提供持久性；磁盘ext4、实际大编译、输入/归档存储硬配额、持久共享缓存、多槽并行仍待验收。服务安装器不自动分区/格式化/挂载宿主磁盘。详细合同、权限和限制见WORKSPACE.md。
# 大文件与中央存储准入（v0.13）

## 等待老化与跨入口积累资源（v0.15）

中央私有配置的scheduler.aging_seconds默认300，仅接受1–86400的整数；用户请求不能覆盖。变更由管理员按维护流程保存配置并重启中央，个人MCP/CLI/节点无需重新配置。/api/jobs的policy返回实际阈值。年轻任务保持用户轮转；达到阈值后按原始created_at与任务ID稳定排序。重试、网页重连和中央重启均不重置提交时间。

较老任务等待CPU/内存/SHM/名额时，当前执行入口复用实际准入检查，在“无托管占用”的条件下确认环境、预算、能力、观测和权限可满足后，才记录私有等待目标。同一任务只保护一个物理域，所有入口共享该意图，暂停其他新分配让已有任务自然结束。不抢占、不凭预计结束时间回填；可能暂时留空部分资源，这是避免持续碎片化的保守策略。

目标证明15秒有效，并核对域/节点修订、节点30秒心跳、账号/项目/环境授权及当前个人配额；失效后不挡住新任务。当前实际观测还不允许空闲准入时（例如NPU仍忙或工作区不干净），不凭未来推测建立保护。过期的是等待意图，**不是运行任务的资源租约**；未知执行仍保留实际资源。取消较老任务立即移除其保护效果；状态中不向其他用户暴露被保护任务ID或所有者。

tests/live_aging_check.py仅在Demo队列空闲时演练：临时阈值3秒、2CPU/768MiB/2槽、四逻辑用户/两个入口；实际C编译与不同UID、中央重启及自有任务取消后顺序均验证，结束恢复配置/预算并禁用临时节点和身份。不是20人混合NPU、预测回填、DAG或实际多机验收。服务包包含queue_policy.py及README/DEPLOYMENT/HARNESS说明；旧包缺少必需模块时会在发布包验证阶段拒绝，不能直接降级覆盖。

## 大文件准入配置

管理员可设置scheduler.io_limits，登记节点时提供--io-config；默认64MiB单文件/128MiB输入与输出保持。个人input-storage-mib/archive-storage-mib和中央storage_budget_mib约束预留与留存；排队即预留、未知不释放、终态结算，幂等不重复收费。新标准库客户端协商256KiB分块并核对完整SHA；浏览器流式Range下载。该应用账本不是输入/归档盘硬配额，详见STORAGE.md。新服务包增加IO/账本模块，旧包是否可回滚仍须按所需模块与状态合同逐项核验，不默认承诺任意旧版兼容。
# v0.20 存储运维补充

v0.21 新安装默认 require_node_spool=true；节点须上报 node-spool-v1、空闲 inode 和回收就绪状态。中央校验产物后返回持久摘要回执，节点只回收该回执确认的新产物副本；旧副本保留，当前没有删除任意节点路径的管理接口。暂存需求按输入/输出额度与同物理域活动任务保守核算，不是文件系统硬配额；详见 STORAGE.md。

中央载荷可由所有者显式隔离/恢复，默认 7 天后独立永久删除；策略字段 `scheduler.storage_trash_grace_seconds`。`accessctl user-add`/`token-renew --storage-manage` 显式授权，管理员读取权限不允许删除其他所有者数据。没有自动回收定时器。pending 文件维护阻断离线备份/激活；已隔离数据支持新目录恢复后的身份重绑定。需要的磁盘硬配额、节点缓存预算与大规模数据库保留策略仍待实机部署。详见 STORAGE.md。

