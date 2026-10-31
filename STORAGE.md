# 大产物传输、中央存储预算与留存（v0.20）

## 可选的独立有限控制盘（管理员模式）

新管理员安装可在 `doctor/install` 显式提供 `--control-config /root/control.json`。合同与载荷盘使用相同的 `path/filesystem/device/root_inode/bytes/inodes/storage_id` 字段，但控制根必须为 Root:0755、整个独立 `ext4` 文件系统的挂载（拒绝子目录 bind），实际容量至少128MiB/1024 inode、`nosuid,nodev`、空盘；状态路径固定为其 `state` 子目录。必须另提供不同文件系统的 `--payload-config`，并保留余量准入及 `local_enabled=false`。实际规划应按长期审计/数据库增长留足容量，128MiB只是校验下限。安装器不分区、格式化、挂盘、清空未知目录或改变已有部署。

可信 Root 启动器在加载服务私有配置前建立私有挂载命名空间，保留原命名空间和原父目录 FD，发布安装前缀中的 Root:0444 进程记录，然后降为专用服务 UID/GID。服务本身不带有效/许可 capability，不持有 Docker socket；`TMPDIR/SQLITE_TMPDIR`也绑定控制状态的私有`tmp`目录，启动后关闭core dump；任务仍由独立 Node 执行。SQLite 会解析 FD 别名路径，所以不能只把数据库改成 `/proc/self/fd/N/...` 就认为 journal 被固定；这里保持整个服务的实际挂载视图。SQLite临时目录选择见[官方临时文件说明](https://www.sqlite.org/tempfiles.html)。

外侧固定控制根被覆盖、卸载或换盘时，原父目录 FD 检查会使余量接口暂停新增工作；私有视图保留原数据库和日志路径，已有任务可继续对账/回传，不写入新挂载或下层普通目录。实际底层设备 I/O 故障不保证继续写入；原尝试和租约仍须保留。该模式要求管理员保持祖先目录及宿主挂载拓扑稳定，不是自动发现任意祖先替换、设备恢复、跨机 fencing 或高可用。

该模式的进程启动/停止需要 Root；准确 PID/start_ticks/boot_id 记录位于控制盘之外，控制根不可用时仍可停止已确认的原进程。直接使用无特权 `run/server.py` 启动带此合同的服务会被拒绝。生成的 systemd unit 只在初始化阶段以 Root 运行，实际控制进程会降权；真实 PID1 生命周期仍须在目标服务器验收。

停止态备份记录控制盘绑定且必须放在其他文件系统；恢复需要新的空控制盘和载荷盘，显式提供 `restore --control-config ... --payload-config ...`。可以把旧普通目录快照迁移到此模式，不会自动迁移当前个人 Demo。缺少相应控制盘/启动器实现的旧发布包不能承接该状态。恢复中断的候选目录与原盘应保留供管理员核对，不自动覆盖或清扫。

隔离验收入口为 `tests/install_server_check.py --native-control`。同机自有 Root 容器的30项安装/恢复检查通过：专用UID999、零有效/许可capability、临时目录绑定；挂载覆盖时原IR单attempt完成、新工作503、准确停止；挂载恢复及原生备份迁入另一组新盘后原结果保留，新IR仍单attempt。拒绝共享FS的子目录bind和省略原生恢复合同。证据 `control-filesystem-validation.json` 仅本地，公开精选见 `reports/public-source-check.json`。所有盘均为自有文件盘；不能替代真实宿主账号、PID1/挂载传播、断电、两物理机、NPU或独立硬件故障域验收。

## 控制存储余量准入

新管理员中央安装默认要求控制文件系统至少剩余64MiB和256个inode；低于任一门槛、只读或无法观测时，暂停新增工作。`doctor`只读检查目标状态目录所在文件系统的余量，并拒绝缺少实现的旧安装包。现有配置缺省保持未启用，不自动修改个人Demo、既有Node或磁盘布局。

配置属于中央私有`config.json`的`scheduler`，不能由任务请求覆盖：

```json
"control_storage": {"minimum_free_mib":64,"minimum_free_inodes":256}
```

字节门槛允许16–65536MiB，inode门槛64–1000000，必须同时提供严格整数；未知字段或布尔值拒绝。旧中央如需启用，应先核对发布包能力，再按停机维护流程设置私有配置、重启原服务，保留原状态及未知租约。启用后的状态不能切换到缺少实现的版本；恢复备份同样核对目标运行代码，避免静默丢失准入条件。

准入通过控制目录FD读取[fstatvfs/statvfs计数](https://docs.python.org/3.10/library/os.html#os.statvfs)，不扫描或删除文件。新任务/输入预留、未提交的输入分块及封存重新检查；低余量返回503、`error=control_storage_pressure`及固定reason_code，不追加逐次拒绝审计。原任务幂等回执、原输入元数据及已提交相同分块仍可读取。未消费的启动许可返回wait，恢复后沿用原attempt；已授权/运行任务的心跳、退出/超时监督、取消及结果回传不受此门槛阻断。实际写满导致的507仍采用原结果恢复路径。

网页显示控制盘准入、字节/inode及门槛；`GET /api/control-storage`和`/api/jobs.control_storage`需要jobs:read，不公开路径、设备号或他人对象。个人MCP的`queue_status`返回同一状态，未增加提交或文件权限。状态为disabled/ready/pressure/unavailable；旧中央未声明时不能推断已启用。`/healthz`继续是存活接口，不代表可以接新任务；外部告警需另行读取认证状态。

这是准入时的余量快照，**不预占独享空间，不替代文件系统硬配额、独立控制盘、日志轮转或留存**。外部写入、已有工作和管理员操作仍可能耗尽空间；UID/组配额也不一定反映在statvfs全盘余量中，门槛不保证任意任务完成。管理员仍须规划数据库/日志增长及外部告警。没有新增定时删除、自动VACUUM或生产文件系统迁移。

## 控制盘容量故障与扩展规划

控制数据库、身份/审计及服务日志不计入下述owner载荷预算，需要管理员单独准备有限文件系统/硬配额、告警和日志轮转。控制盘与载荷分离、历史证据回收和多服务器验收顺序见[SCALING.md](SCALING.md)。新管理员中央默认审计archive_required，积压暂停新工作，停中央后显式导出和确认裁剪；缺省旧配置仍滚动保留最近10,000条，详见[AUDIT.md](AUDIT.md)。审计门槛是软准入边界，既有任务事件和回执仍会增长，不能替代物理硬边界；不自动上传、定时清理或删除历史实验。只读外部检查及其边界见[CONTROL_MONITOR.md](CONTROL_MONITOR.md)。

Node把中央明确返回的HTTP507识别为容量不可用：运行中的原容器继续接受本地退出/超时监督；已结束的原结果保留，恢复后继续原许可和分块上传，收到校验回执才清理副本。个人客户端保留HTTP状态但不自动重提计算。401/403、冲突及其他状态仍遵循原处理，不扩大为自动可重试错误。控制盘耗尽与此前载荷盘耗尽是不同的故障域；真实SQLite写满的演练入口与范围见上述文档及本轮验收记录。

## v0.35候选：已有owner配额的离线调整

管理员先排空任务并停止中央服务，再生成只读计划及应用。未封存上传可以保留，其未结算预留量与已有文件用量共同参与缩容检查。中央账户、存储GID和其他owner限额不变，不清理数据、不关闭quota、不自动格式化或挂载磁盘。

```bash
/opt/ascend-lab-controller/current/servicectl stop --prefix /opt/ascend-lab-controller
/opt/ascend-lab-controller/current/servicectl payload-quota-plan \
  --prefix /opt/ascend-lab-controller --owner alice --mib 32768 --inodes 200000 \
  --output /root/alice-quota-plan.json
/opt/ascend-lab-controller/current/servicectl payload-quota-apply \
  --prefix /opt/ascend-lab-controller --plan /root/alice-quota-plan.json
/opt/ascend-lab-controller/current/servicectl start --prefix /opt/ascend-lab-controller
```

计划绑定安装版本、状态目录、原配额、其他配置摘要及当时用量，应用前重新检查。容量总和仍须满足底盘保留空间；新限额须容纳实占字节、未结算上传预留及64KiB余量，inode须至少保留16个。与实际使用重叠的上传预留采取保守计费。运行中的服务、排队或未知任务、过期计划、外部修改的quota和不安全缩容都会被拒绝。

应用前将原计划写入Root私有`payload-quota-change.json`并同步文件/目录，再修改原生组quota并对该文件系统执行Q_SYNC，随后发布服务配置、安装绑定及载荷标记。任一边界失败都保留原计划日志；正常启动、备份、代码切换和新的计划被阻断。管理员核对后重复同一`payload-quota-apply --plan ...`继续，不要手动删除日志或重建计划。已完成计划可以幂等重试，后续变更通过修订号使旧计划失效。完成回执保留在安装前缀的Root私有`payload-quota-history/`，最多1024条，之后需管理员归档再继续。

这是已有owner的停机配额调整；新owner/GID、Node工作盘和缓存配额、在线调整仍需分别配置。真实Root进程中断演练也不证明宿主掉电已通过。完整验收以evidence/validation-v035.json为准。

v0.30候选的Node输入/产物有限暂存盘、空间不足保留原执行结果和同attempt恢复、空盘控制目录恢复见 [SPOOL.md](SPOOL.md)。它与下述中央owner组配额分别配置，现有个人Demo不自动迁移。

## v0.29候选：中央每用户字节与inode硬配额

独立载荷盘合同可新增`owner_groups`，每个逻辑owner分配独立存储GID、字节及inode硬限额。输入、归档及回收站按owner合并计费；GID由管理员配置，不接受用户请求指定。完整安装/故障/恢复验收以`evidence/validation-v029.json`为准；中央仍是普通账号，无Docker socket或CAP_SYS_ADMIN。Node工作盘UID配额是另一层，不共用中央存储组。

下面片段须并入原完整payload合同；盘路径、设备/inode/实际容量和storage_id仍按实际挂载生成，不能直接复制示例到共享/home。选择专用、没有其他数据、已启用group quota accounting/enforcement的ext4，Root拥有的空挂载根700/nosuid/nodev：

```json
"owner_groups": {
  "alice": {"gid":23831,"bytes":17179869184,"inodes":262144},
  "bob":   {"gid":23832,"bytes":25769803776,"inodes":393216}
}
```

由管理员选择专用存储GID，避开系统、Docker、服务主组和其他用途；这些组只授予受信任的中央写入进程，不授予用户账号或任务。当前命令不创建宿主组、不修改已有组成员，支持1..256个明确owner；所有字节上限之和必须小于盘容量减32MiB，inode至少保留512个基础元数据位置，大盘需额外运维余量。

```bash
./servicectl payload-groups-prepare --contract /root/payload.json --plan
./servicectl payload-groups-prepare --contract /root/payload.json
# 然后沿用doctor/install --payload-config /root/payload.json
```

`--plan`只读；准备只设置尚未有限额且没有数据的组，已有完全相同限额可重复核对，不扩大或覆盖其他限额。不格式化/挂载/启用配额。准备中断留下intent，需Root核对，不能重试覆盖未知状态。安装预检要求所有组的实际硬限额匹配且尚未有数据。主组与存储组冲突拒绝。

安装器让普通中央进程仅获得合同列出的补充组；systemd生成`SupplementaryGroups=`，进程模式同样显式设置，控制账号仍无capability。Linux允许进程查询它所属组的quota，故无需Root查询代理。[Linux 6.8权限实现](https://github.com/torvalds/linux/blob/v6.8/fs/quota/quota.c)。容器化部署还必须让可信中央进程的seccomp允许quotactl_fd；固定Root安装演练为可信控制容器使用unconfined，用户任务仍保留原限制，不能将其复制为任务容器设置。实际systemd PID1仍需目标机验收。

新对象写入任何内容之前设定owner存储GID与SGID继承；后续按数据库真实owner核对目录归属、继承与限额。准备文件、启动、准入及路径验证都不把statvfs全盘余量伪装成本人额度。没有配置存储组的新owner不能上传或提交，实际限额/状态/归属变化时阻断，不回退至共享目录。普通API只返回本人硬限额和使用量，不公开GID或其他owner映射。

组配额针对**可信中央写入程序**，不是用户代码沙箱：同一中央账号可以在自己的存储组之间改变文件归属，底层实测已确认。因此不在中央服务中执行用户程序、个人agent或自定义插件；同机受控Node登记也拒绝让用户任务使用中央UID。个人agent保持个人身份，经API/MCP提交。中央身份若被攻破，组配额不是进程间隔离替代品。

预留把未结算输入和归档合并计入本人硬额度；已实际写入和预留可能保守重复扣算。inode硬限额由内核执行，当前准入仅保留基础inode余量，尚未对任意长目录输出提供完整inode承诺。额度耗尽的上传保持既有HTTP507/原偏移/续传语义，节点已执行结果继续保留而不重跑计算；用户可按既有保留策略处理本人其他已完成数据，管理员调额/新增owner仍需单独的离线管理流程。

恢复时，受限Root进程可能没有FSETID，也尚未属于目标存储组；此时chmod可能静默清掉SGID。安装/恢复现在仅在当前短时管理进程中临时加入合同列出的存储组，并在异常或成功后恢复原组向量；不修改宿主组数据库、不增加用户任务权限。每个恢复目录都再次核对实际UID/GID和完整模式，未生效时不发布绑定。此缺陷曾由真实新GID恢复演练发现，原失败及只读镜像证据保留在central-owner-quota-restore-first-failure-v029.json。[Linux SGID属性检查](https://github.com/torvalds/linux/blob/v6.8/fs/attr.c)。

停机快照仍包含输入、归档、回收站和owner数据库。恢复可为相同owner提供新盘及新的存储GID：按控制数据库重新赋予所有对象及子目录/文件的归属和SGID，重新核对内核限额与实际使用量后绑定新控制目录。源盘保留；已使用组配额的快照不能静默恢复为无配额布局，旧代码缺少实现时禁止降级。跨盘发布中断/配额不够可能留下需核对的候选，不能承诺自动修复。

这不覆盖控制数据库/审计增长、Node受信任暂存或跨任务持久缓存；它们仍需要独立硬边界与容量规划。已存在的个人Demo配置不会被自动迁移。

## v0.25 独立中央载荷文件系统

管理员可在新控制器的 `servicectl doctor/install` 显式提供 `--payload-config /root/payload.json`。合同包含 `path,filesystem,device,root_inode,bytes,inodes,storage_id`，格式与工作区固定文件系统相近，但只接受独立 ext4 挂载：64MiB–1TiB、512–1000万固定 inode、nosuid/nodev、Root拥有的空挂载根0700、受保护的Root祖先，且与控制状态/代码目录分离、控制状态在另一文件系统。字节数必须取实际 statvfs，不能填磁盘标称容量。管理员负责提前挂载并处理新文件系统的 lost+found；安装器不格式化、挂载、递归接管已有文件或迁移当前 Demo 数据。

预检只读；安装重新核对合同摘要和空盘后，把根目录交给新服务 UID、写入绑定控制目录与 storage_id 的私有标记，创建 jobs/inputs/storage-trash。数据库、身份、锁、日志和配置仍在控制目录，回收站与载荷同盘移动。默认不配置时保留旧布局。不得混合旧载荷目录与独立盘，也不支持复用别的控制器的盘。每次解析载荷路径重核挂载类型、几何/设备/inode/所有者及绑定；丢盘/换盘时503并停止新执行，不创建回退目录，不释放未知租约。服务停止不依赖盘在线。systemd模板增加 RequiresMountsFor 与载荷 ReadWritePaths；实际 systemd/PID1仍待目标机器验收。

个人/中央应用预算继续生效，独立盘还保留32MiB容量余量，对账本总额度和未结算预留做保守空间检查。它提供全盘物理边界，不是每用户项目配额；inode耗尽、外部管理员写盘和控制元数据增长仍需预算、监控与运行验收。普通网页策略只显示容量/类型，不公开盘路径或 storage_id。

停机备份同时收集控制目录和已登记的载荷根，用 `ascend-lab-snapshot/v2` 保存完整清单；未登记嵌套挂载（含同设备bind）、链接和特殊文件拒绝。写归档时再核实际读取SHA和文件变化，不发布不一致快照。恢复v2必须提供新的空盘 `--payload-config` 及新控制目录；载荷直接在新盘暂存，检查字节/inode、SHA、角色/schema后发布，重绑定隔离清单与归档路径，保留原盘。不能静默还原到控制盘。旧v1控制器备份可在显式提供新盘时迁移；节点备份不能使用中央盘参数。

恢复发布跨两个文件系统，发生中断时可能留下待核对候选；原安装不因失败而丢弃，未知候选不自动覆盖/清空，需管理员核对。当前没有自动化中断恢复工具、在途灾难恢复或多中央fencing。真实大盘/宿主重启、按用户FS配额和GiB备份尚需分别验收。独立安装演练入口 `tests/install_server_check.py --https-network --disk-workspace --central-payload`，实际完成范围见 `evidence/server-install-v025-validation.json`，不能仅根据此文视为已通过。

## v0.33 输入进度与目录持久化

输入创建、输入/节点产物分块在推进数据库之前，同步文件及从叶目录到载荷根的目录项；失败时不确认新偏移。空文件和封存权限同样先同步，再提交 sealed 状态。目录同步失败沿用原容量错误分类，不将 EIO 冒充可恢复的空间不足。

未封存文件的 reset-file 先事务性撤销已上传前缀，不先删除原文件。旧字节暂时作为未提交尾部保留，仍计入原存储额度；下一次 offset=0 上传会先截断。重置事务回滚时原字节和偏移都保留，重置提交后崩溃则从零继续。已封存输入仍拒绝重置。

这些保证要求控制数据库和载荷文件系统正确实现同步语义。进程 SIGKILL、同步边界故障注入与隔离 ext4 恢复的验收分别记录；不把进程终止等同于宿主掉电、磁盘控制器失效或真实两机灾难恢复。历史已确认上传不会因升级自动获得追溯性掉电保证。

## v0.24 写盘满响应与原传输恢复

输入和节点产物分块使用相同的有限写入实现：处理 short write，完整写入并 fsync 后才推进数据库偏移。发生 ENOSPC/EDQUOT 时尽力截去本次未提交尾部，保留已提交前缀；截断本身失败时仍不推进账本，下次重试先按原偏移截断。文件短于已提交前缀、硬链接或非普通文件拒绝自动覆盖。EIO/权限/数据库损坏不被归类为容量不足。

写接口收到 ENOSPC/EDQUOT 或可识别的 SQLite FULL 时，返回有界且不带底层路径的 HTTP 507：`error=storage_unavailable`、`retryable=true`。这是容量恢复后查询/重试原操作的提示，不允许重新提交计算或释放租约。失败响应不额外写审计，避免审计库同样写满时覆盖错误；成功的后续操作仍按原逻辑审计。SQLite FULL 分类有单元覆盖，尚未通过真实元数据库写满演练，不承诺数据库损坏或整个控制盘满时所有读接口仍可用。[HTTP 507 定义](https://www.rfc-editor.org/rfc/rfc4918#section-11.5)、[write 的部分写入语义](https://man7.org/linux/man-pages/man2/write.2.html)。

实际磁盘满验收入口为 `tests/install_server_check.py --https-network --disk-workspace --storage-pressure`：在自有 Root 容器的私有挂载命名空间中，仅把新建的一个归档和一个未封存输入目录绑定到专用 32MiB 文件后备 ext4，控制数据库保留在另一文件系统。固定测试管理员写满自有 filler，观察上传 HTTP507、原偏移、节点已完成结果及后续排队；仅移除经 dev/inode 核对的 filler 后恢复原传输，核验一次 attempt、运行标记、产物 SHA、回执和 GC。Root 为注入挂载临时具有 SYS_ADMIN，实际服务依旧以非root账号运行且无 socket；普通任务不获得该权限。

这不是新的生产存储挂载安装器或按用户项目配额提供器。原有备份恢复验收发生在注入挂载前，不包含这些后加的挂载；生产中分离中央载荷盘、相应备份与磁盘硬配额仍需专门实现。实际通过项以 `evidence/server-install-v024-validation.json` 为准，不能用测试入口或声明替代运行证据。

## v0.21 节点暂存与副本回收

节点在启动前及中央预留前按封存输入字节、冻结产物总额度、16MiB元数据余量检查暂存需求，并在整机同域的其他远程活动任务之外保留512MiB空闲余量；已有实际写入与全额意图可能重复扣算，属于保守准入。node-spool-v1 心跳上报实际空闲 inode 与回收状态；工作流按最多2000个、每条最多240字符的产物路径保守预算目录 inode。普通固定CPU模板使用较小固定预算。inode为0、空间不足或回收未确认时不分配新任务；已有尝试仍可恢复。新安装默认 require_node_spool=true，旧部署须先升级全部节点再显式启用。

上传流程不变更用户计算：中央核对完整清单及各文件SHA后，事务保存绑定 job/attempt/清单摘要/终态的回执。节点持久保存清单与回执，确认容器和工作区已清理后，重新核对本地字节，才移入自己 control 目录的退休位置并有界删除副本。只删除新协议确认的节点产物，不删除中央归档、个人下载、持久缓存、未知尝试或旧协议的历史副本。Root/节点管理员仍属于信任边界；这是受认证连接的摘要回执，不是第三方签名证明。

回执响应丢失时，本地保留原产物/清单，同一尝试重连查询中央终态后取回原回执；不启动第二次计算。确认后到删除完成前，持久回收标记阻断新领取；每次最多删除256项，中断续接同一文件操作。文件/摘要/身份变化或扫描拒绝时保留证据和阻断，不静默删除。摘要与控制记录长期保留；其保留策略及数据库规模仍待完善。

节点离线备份/代码激活也拒绝未完成的副本回收，避免恢复后的文件系统身份变化使半次删除无法继续。先由原节点续接完成，再执行维护。

以上是应用层容量准入与已确认副本回收，不能阻止外部写盘，也不等于节点盘文件系统硬配额。独立磁盘/硬配额和跨任务构建缓存仍需管理员部署；当前不配置共享缓存，也不会清理用户的 ~/.triton 或源码目录。

v0.20 新增本人输入/终态归档的显式隔离、恢复和延迟删除。没有启用中央自动 GC，也不删除共享缓存、个人下载目录或项目源码。应用账本不替代文件系统硬配额。

## 显式留存操作

普通任务 token 的既有权限不会自动扩大；管理员可在 `accessctl user-add` 或 `token-renew` 时显式加 `--storage-manage`。本人网页登录具有本人存储管理权限；项目可读或管理员可读不授予删除其他所有者数据的权限。网页只提供隔离/恢复/续接，永久删除使用独立 CLI/API。

```bash
./labctl --profile /个人目录/client.json storage list
./labctl --profile /个人目录/client.json storage plan archive job-0123456789ab trash > /个人目录/trash-plan.json
./labctl --profile /个人目录/client.json storage apply --plan /个人目录/trash-plan.json
./labctl --profile /个人目录/client.json storage plan archive job-0123456789ab restore > /个人目录/restore-plan.json
./labctl --profile /个人目录/client.json storage apply --plan /个人目录/restore-plan.json
```

对象种类为 `input` 或 `archive`；操作为 `trash`、`restore`、`purge`、`resume`。先预览，再原样应用其中的 `plan`。计划固定对象 ID、修订与文件系统身份指纹；预览后数据变化则拒绝应用。此指纹核对元数据与 inode/mount 身份，**不是文件内容 SHA**；已有输入清单/节点产物哈希另行保留，不为旧数据伪造内容校验。

隔离使用同一挂载内的原子不覆盖重命名，数据位于中央私有 `storage-trash`，仍计费。默认隔离 7 天后方可预览并执行 `purge`；管理员配置项为 `scheduler.storage_trash_grace_seconds`（1 秒至 365 天），每次隔离固定当时截止时间。普通操作不会自动缩短它。永久删除先写持久意图，每次最多移除 256 个已核对条目；`storage apply` 可有界续接原操作。中断时查询 `storage list`，重新预览 `resume`，不会重跑计算任务。

只有确认整个载荷已删除才归还相应预留；任务状态、结果表/CSV、原始清单、已有环境与哈希证据、审计仍保留并继续保守计费。因此删除小载荷未必降低计费，隔离/恢复不归还额度。归档不可用时下载返回 410，正在维护返回 409；计算成功状态不被改为失败。输入被永久删除后，可通过相同清单重新上传：同一 ID、新的未封存上传进度，再次预算检查与封存；旧任务的幂等重试只返回旧任务。

活动/未知任务引用的输入、已启用节点环境认证所依赖的归档及未完成库存对账均阻断维护。每次文件/分块读取和单条流式响应持共享锁，隔离/删除持排他锁；锁不跨越整段多请求下载会话，下载期间被隔离会明确暂停，恢复后可续传。扫描拒绝链接目录、硬链接及嵌套挂载（包括同设备 bind mount），不跟随符号链接目标，不接受任意磁盘路径。首次扫描最多 10,000 条/128 层，超过须管理员另行处理。

离线备份/升级拒绝尚在移动或删除的对象；已隔离对象可备份。恢复到新目录后重新绑定文件系统身份并增加修订，旧计划失效。中央留存副本、节点临时副本、个人副本和实际磁盘硬配额仍是不同管理范围。

输入、输出与留存空间分开控制。管理员IO上限不是任务实际申请额度；每个任务冻结自己的artifact_limits，节点和客户端沿用同一合同。默认仍为单文件64MiB、输入包128MiB、任务产物128MiB，不自动扩大现有用户或节点权限。

## 管理员上限与个人配额

中央scheduler.io_limits和节点登记的io_limits格式如下，字段均为MiB、上限8192；file_mib不得大于两个总量：

```json
{"file_mib": 2048, "input_mib": 4096, "artifact_mib": 4096}
```

中央上限由管理员在停机维护时修改私有配置并重启。新节点使用`workerctl enroll --io-config /管理员/600权限合同.json`登记，节点心跳需与登记上限相同；用户不能在作业中修改节点合同。当前不支持在线改已有节点IO合同，需排空后重新登记或明确迁移；不能直接编辑节点配置扩大权限。

个人配额由中央私有管理命令设置：

```bash
./accessctl quota 用户ID --input-storage-mib 2048 --archive-storage-mib 4096
```

输入默认512MiB、归档默认2048MiB；已有用户缺少这些字段时按中央默认值解释。scheduler.storage_budget_mib默认为32768，约束所有用户的输入/归档合计；可用input_budget_mib/archive_budget_mib进一步设置每人上限。配额缩减不会删除留存数据，超出后拒绝新增；没有自动GC或自动扩容。

中央账本在同一SQLite事务中预留空间：输入按清单字节预留，未上传部分同样占额度；排队作业按产物总额度加8MiB元数据余量预留。运行/未知任务不释放，终态按登记文件实际字节加8MiB结算。幂等提交不重复计费，重启会回填历史输入与任务。`GET /api/me`及`GET /api/jobs`提供个人空间摘要，不暴露其他用户存储明细。

这是**应用层准入账本**，不是磁盘或项目配额：不阻止管理员直接写文件，也不覆盖节点临时副本、所有诊断文件、个人下载和共享缓存。磁盘文件系统硬配额、回收/保留策略仍需继续部署和验收；不能把账本剩余额度当作宿主盘实际空闲。

## 用户提交与传输

个人可信账号的旧非模板命令也可声明产物额度：

```bash
./labctl submit --artifact-file-mib 8 --artifact-total-mib 16 \
  --cpu 1 --memory-mib 256 --timeout 60 -- python your_script.py
```

两个CLI参数须一起提供；HTTP请求对应`parameters.artifacts`。受控工作流继续在JSON中声明，不混用这两个旧命令参数。默认64/128MiB保持；非默认额度进入幂等指纹，按总量加8MiB元数据余量预留。源码快照须同时满足4MiB和申报单文件额度，日志截断提示包含在单文件额度内，额外归档复制使用申报值而非旧128MiB常量。

旧进程入口仍是协作式执行。确认命令及子进程结束后检查`ir/profile/results`的文件数、单文件及合计额度；超额输出原件保留，但这些工作负载目录整体不进入归档，日志、源码快照和失败说明继续归档。原成功命令记为`failed/failure_stage=artifact_budget`，取消/超时/既有失败保留原执行状态并增加产物拒绝说明。未知进程状态保持`needs_attention`和资源预留，不能靠输出检查释放资源。该边界不会阻止旧可信命令在执行时写任意个人可写目录，也不保证宿主磁盘硬隔离；生产使用受控Node和文件系统配额。

旧版本曾校验后丢弃非模板命令的产物参数，实际保存为默认限额。升级不改写这些任务：默认请求仍返回原任务；旧键若提交新的非默认有效限额则返回409，先查询原句柄，不自动换键重跑。

版本化工作流parameters可增加：

```json
"artifacts": {"file_mib": 1280, "total_mib": 1280}
```

本例允许输出单个约1.2GiB文件；中央/节点上限和个人归档额度都必须容纳请求，否则提交被拒绝或保持等待。未声明仍为64/128MiB。合同在提交时冻结，管理员后续放大上限不会改变旧任务；旧节点没有bounded-large-artifacts-v1能力时不能领取大产物任务。工作区容量、内存和持久性仍按WORKSPACE.md单独申请；大产物额度不意味着有对应磁盘或RAM。

默认input_mode=copy会把输入复制到/work/source-tree供编译器修改；应同时预算源码副本和输出空间。只读处理大wheel/IR时可在parameters明确设置`"input_mode":"readonly"`，从只读/inputs运行并把输出写到/work，不复制整份输入。节点不会把需要复制的输入分给连输入本身都放不下的工作区。首次大wheel演练因默认复制输入后再复制输出而OOM，保留失败证据；后续 v0.13 只读模式完成已有1,253,835,354字节wheel传输验收，见large-wheel-live-validation.json，不能把首轮OOM算作成功。

v0.14补齐只读原生可执行输入：节点按封存manifest将工具设为0555、数据设为0444，恢复下载也会重新校正，拒绝未登记文件。实际小型C编译产生的ELF再次封存后，由UID23002/零capability任务直接执行；任务自身核对/inputs挂载为ro，写入被拒绝，见readonly-input-live-validation.json。默认copy模式仍保留；这不等于完整工具链、全部二进制依赖或跨架构兼容性已验证。

独立客户端先从`/api/me`读取IO上限和分块能力。输入、节点下载/上传、个人归档均使用最多256KiB块，连续偏移重试，最终核对SHA256。服务端输入消息上限512KiB、节点消息1MiB，作业提交仍32KiB；不把用户命令接口扩大成任意大JSON。输入仍最多128文件、输出2000文件；完整源码可由用户显式打包后作为输入，在受限工作区内由自己的程序解包，不默认遍历整个仓库。

中央的浏览器文件接口流式读取并支持单段HTTP Range，不把大文件整体载入进程内存。个人客户端用清单/块接口断点续传，并在全部文件核对后生成完成标记。模型只读取结构化小结果，wheel等二进制通过产物接口下载；不会把大文件全文送给模型。旧客户端遇到超出自身上限的清单会明确失败，不读取中央目录绕过权限。

## 验收边界

大wheel演练使用已有的真实Triton cp311 wheel，在Python3.10任务中做流式复制、ZIP元数据检查和SHA核对，**不安装、不重编，也不声称ABI或数值通过**。备份恢复先对小状态演练，之后才上传大wheel，因此不得称为已验证GiB级备份。演练容器、卷和大文件测试副本会清理，原始wheel不修改。

应用层预算、受限工作区、文件传输和完整编译是不同的验收项。完整Triton/NPU-IR构建、实际磁盘工作区、持久缓存、GiB级备份恢复、归档GC/保留策略、跨服务器与NPU精度/性能/仿真仍需分别验证。
