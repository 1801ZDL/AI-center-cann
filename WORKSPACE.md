# 有硬边界的任务工作区

## v0.28 候选：专用 ext4 上的每用户 UID 硬配额

当前已发布验收基线仍为v0.27；以下provider已接入候选源码，完整安装验收尚未通过，不代表生产就绪。原独立ext4/tmpfs provider保持可用。

新增`ext4-userquota`使多个用户在同一专用文件系统上拥有各自字节和inode硬限额。Node按owner→任务UID选择对应槽位，不能使用另一UID的空余额。中央合计同盘跨节点额度并拒绝UID重复登记，保留至少16MiB及128个inode的基础元数据余量；这是最低预留，实际部署还需按盘规模另留运维空间。上限为256个配额槽位，管理员可分别配置任务并发上限。

UID限额聚合该UID在整块文件系统上的所有文件，不是每attempt独立追加额度。只给任务挂载本人工作目录，缓存、HOME、临时文件和结果在其中；任务非root、零capability，不获得Docker socket。Node的控制与产物暂存必须位于另一文件系统，避免Root拥有的暂存绕过UID限额。中央输入/归档的每用户硬配额、持久缓存仍未实现。

管理员先准备**全新、专用、已挂载且用户配额accounting/enforcement均启用的ext4**，挂载根Root拥有、700、nosuid/nodev。准备工具不分区、不格式化、不挂载、不启用配额，不修改已有数据或已有限额。不要直接对共享/home或已有工作盘运行。保存Root私有600权限请求，例如：

```json
{"users":[{"uid":23003,"bytes":17179869184,"inodes":262144},{"uid":23004,"bytes":25769803776,"inodes":393216}]}
```

示例UID和额度须替换为目标机器实际任务身份及容量；总额必须给文件系统和Root元数据留余量。

```bash
./servicectl workspace-prepare --root /srv/ascend-lab-work \
  --limits /root/lab-work-limits.json --output /root/lab-workspace.json --plan
./servicectl workspace-prepare --root /srv/ascend-lab-work \
  --limits /root/lab-work-limits.json --output /root/lab-workspace.json
```

`--plan`只读，不创建标记/槽位或设置限额；成功准备后可按相同请求重复运行。输出合同交给`workerctl enroll --workspace-config`，并为每个owner提供匹配`--user-map`。私有Root标记绑定文件系统身份、实际几何、槽位和限额；准备中断保留intent并要求管理员核对，不自动覆盖或撤销限额。Root输出合同既有但不一致时先报错，不触碰文件系统。

节点启动、心跳、执行前及清退均验证来源和实际用户配额；可用量取本人硬限额余量与全盘余量的较小值。使用`Q_XGETQSTATV`同时检查accounting/enforcement，关闭执行或无法查询立即拒绝，不用旧限额或statvfs代替。新增能力`uid-quota-workspace-v1`，缺少能力的节点不接受该合同。物理/work仍按实际文件系统身份核对，statvfs不会伪装成UID限额；受保护environment.json另记user_quota。

安装预检检查配额及控制目录分盘。生成的systemd节点unit加入工作盘挂载依赖/写路径，并仅为使用该provider的可信Root节点保留CAP_SYS_ADMIN供查询其他UID；中央和任务不获得该能力。真实systemd PID1尚未验收。自有容器安装演练的Root节点另需seccomp允许quotactl_fd；演练仅在该可信角色使用unconfined，用户任务配置保持原限制，不应照抄为公共任务设置。

底层真实验证`job-8e21b3c1dcbc`：自有128MiB ext4，UID23801=16MiB/128inode、UID23802=24MiB/192inode，前者分别触及EDQUOT，后者仍能写1MiB；修改限额/文件归属/跨用户链接被拒绝。首次失败揭示关闭enforcement后旧限额仍可读，已补状态检查并实测flags3→1后拒绝。证据`evidence/user-quota-primitives-acceptance-v028.json`绑定当时原语源码；它不等于新Node provider安装验收。[Linux quota接口](https://man7.org/linux/man-pages/man2/quotactl.2.html)、[Linux 6.8状态实现](https://github.com/torvalds/linux/blob/v6.8/fs/quota/quota.c)。

## 已交付的独立有限文件系统provider

2026-10-30 v0.23 验收补充：`tests/install_server_check.py --https-network --disk-workspace` 已让真实 UID23003 任务使用独立、文件后备的 ext4 工作区，验证持久工作区请求、字节/inode ENOSPC、清理后继续任务、受保护的挂载身份与合同一致。32MiB 镜像的实际 statvfs 容量会扣除 ext4 元数据，合同必须读取实测值，不能写死为镜像字节数。原 tmpfs 路径仍保留；服务端 API 和个人客户端协议未改变。最新完整报告见 `evidence/server-install-v023-validation.json`，初次验收脚本字段断言错误另存，不把该失败记为计算错误。

该测试只格式化 Demo 内 O_EXCL 新建、已实际分配空间的普通文件，绝不格式化宿主块设备。固定可信辅助容器没有 socket、网络或宿主命名空间；使用 [LOOP_CONFIGURE](https://man7.org/linux/man-pages/man4/loop.4.html) 原子附加并启用 AUTOCLEAR（Linux >=5.8），持有一个 FD 到自有卷卸载后关闭，以 backing device/inode 证明释放。设备节点只创建在该辅助容器的 /dev；Docker 通过固定 helper PID 的 /proc 路径挂载到自有卷，没有修改宿主 /dev 或挂载传播。失败保留控制证据，不使用 detach-all，不强制卸载未知挂载。此代码是测试辅助工具，生产安装器依旧只检查管理员已准备的文件系统，不自动格式化或创建 loop 设备。

跨容器挂载保留 fsync 文件已验证；不将其描述为断电、宿主重启或大编译盘验收。`durable=true` 表示文件系统非 tmpfs，不承诺应用检查点或任意任务断点续跑。中央输入/归档、节点暂存与共享缓存的磁盘硬配额和真实两机仍须分别验证。

大型 tmpfs 的首次环境认证可显式增加固定探针内存：`workerctl node-probe 节点ID --memory-mib 32768`。默认探针256MiB不能领取32GiB tmpfs槽；显式值仍须个人/节点/物理域预算容纳，并要求 environment-probe-memory-v1。这只改变资源预留，不改变固定命令或CPU/NPU数量，见 ENVIRONMENTS.md。

工作区槽位是管理员预先准备的**独立、有限容量文件系统**，每次独占给一个attempt。任务只挂载槽位内自己的工作目录，cache/tmp/HOME/结果均留在其中；不把普通宿主目录的可用空间视为每任务硬配额。中央只分配登记的slot ID和容量，任务用户不能选择宿主路径、设备或挂载参数。

## 管理员合同与准备

先在目标服务器准备专用文件系统挂载：可使用有明确容量和inode总数的ext4磁盘文件系统；挂载根必须root拥有、700、nosuid/nodev、可写且为空。不要使用系统根盘、共享/home的子目录或绑定普通目录。新ext4的空lost+found等初始目录应由管理员在专用文件系统准备阶段处理；节点不会自动删除未知文件。普通目录、符号链接、非独立设备、身份或容量变动均拒绝。

允许tmpfs用于小任务或演练，但它消耗内存，任务需要的持久性必须显式为false，且整槽容量不能超过任务内存预留；它不适合代替大编译的磁盘工作盘。tmpfs的size和nr_inodes分别给出字节和inode硬限额。[Linux内核说明](https://docs.kernel.org/filesystems/tmpfs.html)；本机演练通过Docker local driver创建了仅本项目使用的有限tmpfs卷，[Docker官方用法](https://docs.docker.com/reference/cli/docker/volume/create/)。

管理员在目标节点上核对挂载和实际stat/statvfs后，保存600权限合同：

```json
{
  "slots": [{
    "id": "work0",
    "storage_id": "REPLACE_WITH_32_LOWERCASE_HEX_UNIQUE_ID",
    "path": "/srv/ascend-lab/work0",
    "filesystem": "ext4",
    "bytes": 10737418240,
    "inodes": 655360,
    "device": 12345,
    "root_inode": 2
  }]
}
```

示例数字不可直接复制：bytes必须等于`statvfs.f_blocks * f_frsize`，inodes为`f_files`，device/root_inode为`stat.st_dev/st_ino`。每个storage_id唯一；同一物理机不能在不同节点重复登记相同ID、设备或路径，禁用节点仍有未知/在途任务时也不能借此重新分配。节点最大并发不能超过槽位数。当前每槽上限1TiB/1000万inode；每个任务独占整个槽位，尚不按任务动态创建/缩放文件系统。

```bash
# 在中央私有管理环境登记；工作区合同来自目标节点，不在中央机器上代替核验挂载。
./workerctl enroll --name server-a --cpus 0,1 --memory-mib 8192 \
  --max-running 2 --image sha256:实际镜像哈希 \
  --user-map 用户ID:目标UID:目标GID --physical-id 实际physical_id \
  --workspace-config /管理员/工作区合同.json
```

工作区节点要求受信任root角色。服务安装器和worker启动会核对真实挂载；心跳继续核对，身份/容量变化则该槽位不可用于新任务。新安装的中央默认`require_bounded_workspace=true`，没有槽位的节点不能承接任务，即使用户省略工作区要求。当前个人Demo保留false与原协作式入口，网页明确显示未启用硬限额；不要把它当成生产约束。

如果节点本身在容器内，所登记的绝对路径必须在Docker daemon宿主上指向同一个文件系统。可信任务引导器会再次核对实际/work的设备、inode、容量，匹配前不启动用户程序；不能靠“容器内存在同名路径”判断。演练将自有卷同时挂到其daemon Mountpoint路径，因此源路径一致。普通用户任务不获得Docker socket或节点配置。

## 任务要求、恢复与清理

五类版本化工作流可增加：

```json
"workspace": {"min_mib": 10240, "min_inodes": 100000, "durable": true}
```

这是parameters内的字段。中央按新鲜心跳、最小容量/inode、持久性、空闲状态与同物理域租约选槽。要求硬边界的请求不能降级到普通目录；要求持久的请求不能落到tmpfs。容量不够就排队，不因声明值较小而扩大实际限额。`min_mib`表示所需可用容量，文件系统元数据也会消耗空间，因此不能把文件系统标称总量当作可用量。

槽位根有root私有租约，任务仅拥有自己的子目录。节点在发起容器前保存租约；重连绑定原attempt，不重新执行。只有Docker确认任务结束、输出归档、工作目录清理完毕后才删除租约并上报完成。完成回执丢失时中央仍占用该槽，直到重传成功；不能仅因磁盘空闲就把状态未知的资源再分配。

没有自动挂载、格式化、缩容、重置配额、删除未知工作区或回收其他用户目录的操作。文件系统身份变化、未知租约、脏槽位、清理失败均保守保留或拒绝。tmpfs在节点容器/宿主重启后可能丢失数据，不能声称具备持久灾难恢复；磁盘挂载设备号变化也需要管理员核对合同与未完成任务，不能重提相同计算来掩盖丢失。

## 已验证与未完成

当前不接受XFS作为此提供器：XFS的statfs inode总量包含按剩余空间估算的可分配inode，不能把它当成固定inode硬限额。XFS应另接明确的项目配额提供器并实测，尚未实现。[内核XFS statfs实现](https://kernel.googlesource.com/pub/scm/linux/kernel/git/torvalds/linux.git/+/627b7865c062ff642c0000f3a4775f1a44b986a4/fs/xfs/xfs_super.c)

原自有Root安装演练让UID23003的任务在32MiB/512 inode tmpfs实际写至字节ENOSPC和inode ENOSPC，清理后继续执行真实BishengIR工作流；v0.23 新增的文件后备 ext4 验收见本页开头。中央UID999无socket、节点0、任务非root；宿主账号、全局配置和他人容器均未改。物理分区、大构建盘和宿主重启恢复仍未验收。

该实现约束任务工作目录及其中的临时缓存，不是整个存储系统已完成：节点输入副本、已归档产物、日志、中央存储和个人下载仍需各自预算、保留/GC和实际磁盘硬配额；还没有跨任务共享构建缓存。完整源码/大wheel限制、真实大编译、磁盘文件系统部署、多槽工作区并行、两台服务器和宿主重启恢复继续待验收。安装器只检查已准备的挂载，不替管理员自动分区或格式化磁盘。
