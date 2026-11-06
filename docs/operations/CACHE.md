# 私有持久缓存（v0.31集成候选）

v0.34候选增加真实工具链的托管工作流演练。此前原生提供器已验证Triton五阶段到ELF、跨进程暖0stage、实际CMake/ccache空build目录暖2hit/0miss、跨owner/环境隔离，见evidence/triton-native-cache-validation.json；当时不是中央受保护认证。新安装/HTTP/MCP与控制恢复的实测范围以evidence/validation-v034.json为准。脚本、私有离线适配与配额要求见WORKFLOWS.md；原个人配置保持不自动迁移。

2026-10-30。已将`persistent_cache.py`接入中央缓存槽预留、Node心跳与执行、安装/CLI/同盘控制恢复和网页摘要；显式请求才启用，原个人Demo配置不自动迁移。集成源码存在不代表已完成验收，实际完成范围以`evidence/validation-v031.json`为准。原底层提供器的Root11证据只对应当时源码，不替代当前完整安装与受保护Docker入口验证。

## 已实现的边界

- 只消费`quota_workspace.prepare`已经准备的专用ext4 UID硬配额槽。沿用现有内核字节/inode限额校验，不提供普通目录或物理余量替代。每个owner唯一映射一个quota UID；共享文件系统上各UID限额合计必须留出原提供器要求的空间。
- 缓存盘与Node控制记录、用户工作区、Root输入/产物暂存分别隔离。Root控制目录必须已存在且私有；非Root目录在任何初始化写入前拒绝。缓存根绑定物理Node、Node ID、控制目录、政策SHA和storage ID。
- namespace由owner、固定镜像、认证环境摘要、执行runtime文件摘要、版本lock、编译环境变量及NPU政策摘要计算。用户不能指定宿主路径或替换其他用户的slot/key。即使编译环境相同，也不跨owner共享可写缓存。
- 单个owner配额下最多128个环境namespace，总字节和inode预算覆盖全部版本。namespace目录由Root建立后交给对应UID，Root祖先目录不可由用户遍历；执行入口仅向任务绑定它自己的`/cache`。
- 同一owner的缓存槽同时只持有一个写租约。控制文件上的短事务锁与独立持久租约分别保护并发操作和进程重启；进程退出不自动释放持久租约。原attempt/lease摘要相同可以恢复，其他attempt、错误release和GC均拒绝。
- 调用方必须先确认任务容器已终止才能release。当前模块接口只能检查这一确认参数；Node集成以实际Docker终止与工作区清理作为调用依据，不能把一个布尔参数当成设备或进程已经结束的证明。
- 新目录的创建意图记录device/inode。只有原持久租约下匹配的空Root目录可以继续chown；未知Root数据拒绝接管。中断发生在mkdir之后、意图落盘之前时保留给管理员核对，不猜测所有权。
- GC先生成只读plan，包含政策SHA、控制generation及目录device/inode；应用时重查、拒绝活跃/未知租约及旧plan。通过目录FD、nofollow和同设备校验递归删除，只unlink用户symlink，不读取其目标。过深或未知对象保留待核对。当前没有TTL或自动GC调度。
- 失挂载、配额失效或绑定变化上报error/零余量，拒绝创建目录。恢复原盘可以继续使用原namespace；不会把宿主普通目录当缓存盘。

## 当前真实证据

`evidence/cache-provider-validation.json`绑定精确的provider源码SHA和最终队列父任务。演练用自有128MiB文件后备ext4、两个固定UID及独立控制tmpfs；Root角色无Docker socket/网络/宿主namespace/NPU，只有测试提供器需要的管理能力。执行actor已核实零capabilities和NoNewPrivs。

真实BishengIR将20+22折叠为42后写入缓存，重建provider并恢复相同租约、切换到新attempt均复用同一IR且累计调用数保持1。环境变量改变后使用另一namespace。一个UID实际触发字节/inode EDQUOT时，另一个UID仍完成IR转换；活跃租约不可回收。旧plan、symlink目标、未知Root目录和失挂载不降级均有实际检查。原挂载恢复后，另一环境的缓存仍能命中。

这验证的是缓存存储和协议提供器，**不是实际Triton JIT/ccache/CMake加速、Node重启恢复、中央资源预留或生产缓存命中率**。版本事实由该固定Root测试直接测量，不冒称已获得中央受保护环境认证。全部测试盘、loop和容器结束后清理，未创建宿主账号或变更旧Docker。

## 准备、登记和使用

Root先在另一块专用空ext4启用UID配额，并沿用`servicectl workspace-prepare --root /srv/ascend-cache --limits /root/cache-limits.json --output /root/cache.json`生成缓存政策。该盘不能兼作工作区、Node暂存或中央载荷盘；安装器不格式化、启用配额或接管未知数据。每个用户的缓存总额度覆盖其所有环境版本，而不是每个任务额外获得一份额度。

中央登记节点时增加`workerctl enroll ... --cache-config /root/cache.json`。同一物理机上的缓存文件系统不能重复登记给其他Node，也不能与其他存储角色重叠。Node doctor只读检查空槽与分盘；install建立磁盘标记和独立控制记录，systemd加入缓存挂载路径及现有配额查询权限。

五类工作流的`parameters`可增加：

```json
"cache": {"mode":"persistent", "min_free_mib":1, "min_free_inodes":8}
```

这只请求本人的缓存。认证环境不匹配、没有本人缓存槽、槽仍有租约、字节/inode不足或失挂载时任务等待；禁止借用其他owner或静默改用工作区缓存。Node在取得新租约前再次核对实际余量；同一原租约恢复时保留已有数据。未请求缓存的工作流继续使用独立工作区cache，缓存盘故障不会阻止这些任务。

可信引导器检查实际`/cache`设备/inode/UID/GID后才执行用户程序，并设置`TRITON_CACHE_DIR=/cache/triton`、`CCACHE_DIR=/cache/ccache`及`LAB_CACHE_DIR=/cache`。不会自动安装ccache或修改用户CMake命令。缓存文件不归档成任务结果，精度/性能报告与IR仍写各自/work/results、ir、profile；缓存是可被该用户修改的数据，不是受信任的编译成功证据。

`examples/workflows/cache_ir.py`用输入文件SHA作为内部缓存键，调用实际bishengir-opt后把IR复制到独立归档路径；用于冷/热与协议验收，不代表真实Triton JIT或完整CMake工程的命中率。

## 管理与控制目录恢复

Root在Node安装目录中操作：

```bash
servicectl cache-list --prefix /opt/ascend-lab-node
servicectl cache-plan --prefix /opt/ascend-lab-node --slot-id SLOT --key SHA256
# 把返回的plan对象保存为Root私有600文件，再应用同一plan。
servicectl cache-prune --prefix /opt/ascend-lab-node --plan /root/cache-plan.json
```

list可观察活跃租约，plan/apply拒绝活跃租约或旧generation。当前为显式管理员GC，没有后台TTL自动删除。普通用户的CLI/MCP不能自行删除其他任务缓存，MCP会话仍固定于预先批准的工作流请求，并显示其中的缓存要求。

Node停机备份要求全部attempt已确认清理、Spool为空、所有缓存租约已释放。快照包含缓存控制元数据和政策，**不包含原生缓存文件**；缓存留在同一专用盘上。恢复必须显式提供`restore ... --cache-config /root/cache.json`（有Spool时也要提供`--spool-config`），只支持同一物理Node和同一缓存政策/文件系统。恢复保留当前盘上的namespace与最新控制generation，重新绑定新控制目录，不把旧快照的过期GC计划重新变成有效计划。

新盘或新机器需登记新Node，不提供进行中缓存迁移或隐式重新分配UID。与Spool重绑相同，发布中断可能保留部分更新的标记，需要Root核对原、新控制目录后恢复，不能盲重试或删除原缓存。带缓存政策的Node/中央拒绝丢失缓存生命周期能力的降级。

## 仍需完成的验收

完整安装候选必须验证固定受保护环境下的冷/热IR、两个实际UID、真实Docker /cache挂载、Node在任务执行中重启而不重复启动、满额时peer推进、失挂载门禁及原盘恢复、计划GC和同盘控制目录restore后的暖缓存。还需要真实Triton JIT/ccache/CMake工程、真实systemd与两物理机测试；当前不由提供器Root11推导这些已通过。阶段DAG、控制元数据增长硬边界、组织身份和完整NPU计算资格仍在整体目标内，PUBLIC-001继续延期。

已有owner缓存额度的调整见[NODE_QUOTA.md](NODE_QUOTA.md)：同一namespace key和目录inode保留，更新配额合同及控制绑定，不复制或删除缓存。目标Node须停机清退，其他Node共享该原生盘时拒绝调整；尚无在线或换盘迁移。
