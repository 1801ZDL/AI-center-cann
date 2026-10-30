# 节点输入与产物暂存盘（v0.30候选）

Node的封存输入副本、待上传产物及收集scratch放到管理员明确准备的独立有限ext4；控制状态、租约、执行结果、回执和GC日志留在另一文件系统。用户工作区仍用WORKSPACE.md的独立FS或UID硬配额，不与Root暂存盘混用。当前版本验收以`evidence/validation-v030.json`为准；接口/源码存在本身不表示生产就绪。

## 准备和登记

管理员先准备专用空ext4挂载，Root拥有、700、nosuid/nodev，实际固定字节/inode总数；与节点控制目录、代码、任何任务工作区和中央载荷盘分别隔离。安装器不格式化/挂载/缩放/接管已有数据。普通目录、符号链接、原盘身份/容量变化、同机跨节点重复FS/路径/storage_id拒绝。小任务建议至少1GiB且具有足够inode；实际准入还保留512MiB余量及全部待定输入/产物意图，合法长输出路径会产生较多inode需求。

Root私有600合同与独立中央载荷的基础字段相同，不接受owner_groups：

```json
{
  "path":"/srv/ascend-lab-spool",
  "filesystem":"ext4",
  "storage_id":"REPLACE_WITH_32_LOWERCASE_HEX",
  "device":12345,
  "root_inode":2,
  "bytes":1073741824,
  "inodes":262144
}
```

device/root_inode取实际stat，bytes为实际statvfs.f_blocks*f_frsize（并非磁盘标称容量），inodes为f_files；示例不可直接复制。初始化盘上的lost+found等初始目录由管理员准备阶段处理，安装器不删除未知文件。

```bash
./workerctl enroll --name server-a --physical-id 目标机器实际ID \
  --cpus 0,1 --memory-mib 8192 --max-running 2 \
  --image sha256:实际固定镜像哈希 --user-map alice:23001:23001 \
  --workspace-config /root/workspace.json --spool-config /root/spool.json
# 将生成的私有node配置及CA送到目标节点，再使用servicectl doctor/install。
```

目标节点doctor只读检查挂载、空盘、Root祖先和控制/工作区分离。install再次检查并建立绑定state_dir、node_id、physical_id、policy摘要的Root私有标记。生成systemd挂载依赖和写路径；不因有限Spool给用户任务新增capability。节点本身容器化时，该绝对路径须与Docker daemon主机上的同一路径/文件系统一致，不能只在Node容器内部创建同名路径。

## 准入、故障与清理

finite-node-spool-v1能力及绑定观察进入心跳。配置有限盘的节点只报告该盘实际字节/inode余量，不能用宿主机余量替代；失挂载/换盘上报不可用及零容量，控制清单和心跳仍可对账。新安装中央默认require_finite_node_spool=true；未配置或失效的节点不能获新任务，既有尝试的资源继续保留。原个人Demo默认保持false，未自动迁移旧数据，界面不得称其已有硬边界。

同机每个有限Spool独占文件系统，准入只扣该节点的其他待定任务；不把其他独立Spool的占用重复扣算。每attempt有Root私有作业/租约摘要标记，只把本次inputs/artifacts挂给相应操作；用户容器没有Node控制或Spool写权限。

暂存数据操作锚定已核对的文件系统FD；目录名字被替换时不会穿透到下层普通目录。Docker只读输入挂载仍使用管理员登记的daemon同路径，启动前重新检查绑定。

导出每文件先写固定私有scratch并fsync，再在同盘原子不覆盖发布；重试逐字节核对已完成文件并直接复用，避免额外复制大文件。未知scratch、链接或已提交文件冲突保留供核对。空间/IO/完整性错误不会把已完成计算改成可重跑任务，也不会删掉未归档的工作区：保存原执行结果及结束时间，标记output_pending，空间恢复后在同一Docker/attempt上重收集。控制取消和超时不依赖Spool可用性；非法用户输出仍按既有失败合同处理。

工作区清理发生在完整收集后，节点产物仅在中央确认对应SHA清单与终态后删除。Root控制盘保留摘要回执/传输和清理日志；确认清理后才删除该attempt的空Spool目录。未知标记、脏目录、配置变化或GC失败均停止回收，不清其他用户数据。

## 停机备份与恢复

Node停机备份要求所有尝试已结束/确认/清理且Spool确实为空。当前快照只需控制元数据，记录Spool合同；活跃或未确认产物不能丢弃后强做快照。

```bash
./servicectl restore --prefix /opt/ascend-lab-node \
  --archive /root/node-backup.tar.gz --new-state-dir /var/lib/ascend-lab-node-restored \
  --spool-config /root/spool.json
```

仅支持相同物理Node、同一已核对空Spool的显式控制目录重绑，不会恢复成普通目录。新盘或新机器应登记新Node；任意进行中状态迁移和Spool策略原地替换尚未实现。旧控制目录保留，旧路径的worker无法通过已重绑的Spool标记；中断发布可能需Root核对。若Node容器化，新旧控制目录须在专用共享父目录下正确bind给Node和daemon，不能把中央私有父目录一并暴露。

该功能不是持久共享编译缓存或控制数据库增长硬配额，也未证明真实systemd PID1、宿主重启、两台物理服务器或NPU计算。安装演练使用自有文件后备FS，不更改共享宿主磁盘、账号、驱动和既有Docker。
