# 已有用户的 Node 工作区与缓存配额调整

`node-quota-resize-v1`用于已安装、已登记Node的维护操作。每次只调整一个既有owner在workspace或persistent_cache中的字节/inode硬限额，保留UID、槽位/文件系统身份、数据和缓存namespace key。它不创建账号、不更换磁盘、不格式化/开关配额，不改变中央归档限额。

目标Node必须停止，全部尝试已确认清退和中央ACK，暂存与缓存租约已清空。计划与应用持有Node状态排他锁；不强制取消他人任务。中央其他独立Node可继续运行。所选原生文件系统的全部槽必须登记在目标Node，不能与其他Node的工作/缓存/暂存共盘；共享盘的跨Node联合调整暂不支持。只接受原生ext4 UID quota，普通目录或磁盘物理余量不能代替。

## 管理员操作

先排空并停止目标Node，在Node的Root环境生成私有计划：

```bash
/opt/ascend-lab-node/current/servicectl node-quota-plan \
  --prefix /opt/ascend-lab-node --owner alice --kind persistent_cache \
  --mib 4096 --inodes 100000 --output /root/alice-cache-plan.json
```

`--kind workspace`选择工作区。预览只读，拒绝同额度、缩到已用量以下或元数据余量不足、整盘额度合计/当前剩余容量不足、活动租约及来源漂移。有效请求至少16MiB/128inode，字节按4096对齐，沿用原提供器容量上限；计划不超过1MiB。私有输出必须是新文件。

管理员将同一计划通过可信方式交给中央服务账号，保持文件0600且归其所有；不向普通用户、模型或公共仓库交付。中央侧执行：

```bash
/opt/ascend-lab-controller/current/workerctl --controller-state /var/lib/ascend-lab-controller \
  node-quota-approve worker-替换为节点编号 --plan /本人私有路径/alice-cache-plan.json
```

中央以事务核对原Node投影、用户、物理域、完整存储来源、无在途/未知执行和其他Node的盘别名，记录批准后提升policy revision、使旧心跳失效并暂停该Node新分配。响应丢失重复原命令，返回原批准，不生成另一轮。审批不直接修改远程文件系统。随后回到Node：

```bash
/opt/ascend-lab-node/current/servicectl node-quota-apply \
  --prefix /opt/ascend-lab-node --plan /root/alice-cache-plan.json
```

Node使用自己的凭据向配置中的中央核对该计划批准，只接受本Node/物理域/原与新投影一致的回执。再持久保存控制盘journal和原生盘intent，CAS修改实际内核限额，更新原生来源marker、必要的缓存绑定和Node配置/安装元数据。缓存绑定变化保留所有旧namespace目录、内容、key及目录inode；缓存控制generation递增，数据不复制、不删除或chown。其他owner内核限额保持不变。

Node保存Root私有收据并得到中央ready ACK后才清理intent/journal。此时需沿部署原流程启动Node；只有新鲜、匹配新投影、存储正常、库存已对账的心跳，才解除中央维护等待。普通任务仍需通过全部原资源/环境/设备准入；额度变化不授予NPU资格。

```bash
/opt/ascend-lab-controller/current/workerctl --controller-state /var/lib/ascend-lab-controller \
  node-quota-status worker-替换为节点编号 --plan-id 替换为原计划SHA
```

状态为approved、ready或observed。approved/ready时其他Node配置变更被拒绝；不存在“仅等几秒就自动完成”。复核实际Node健康和任务后，再扩大使用范围。

## 故障与保留边界

过程可在原计划上恢复。未知响应继续查询原批准/执行同一apply，不换计划、不手工删除journal。本地journal/intent存在时Node启动/备份/代码切换及相关存储管理拒绝执行；原生intent也阻断普通提供器验证。内核CAS只接受原或新限额，不覆盖第三种变更，不重置用量/所有者/配额执行状态。用量、namespace身份、其他配置或盘身份变化则保留现场供管理员处理。

计划生成后保持Node停止，不允许其他程序修改所选存储。当前不提供取消已批准变更、在线调整、迁移缓存盘或多个Node共享盘的联合事务；完成原维护后可明确批准另一计划调整回去，不能凭中央超时猜测远程未修改。中央待确认变更阻断中央离线备份/恢复切换。批准后、Node尚未应用时，其旧心跳已失效；维持目标Node停止，直到原apply完成再启动。历史控制器记录及Node收据阻断降级到不识别本合同的代码。

需要轮换/恢复被撤销的Node凭据时，中央沿用`workerctl node-rotate`；将新私有配置中的**node_key字段**通过可信管理员流程更新到停止中的Node，保留原存储投影和journal。计划的非存储配置摘要允许该字段轮换，URL、CA、镜像、UID等其他字段仍固定。原凭据不再可用，不以普通用户凭据降级。完成收据重放只证明历史应用，不是当前健康或新启动授权。

每个Node最多保留1024次调整，当前不自动裁剪这条审计链。Node备份记录维护修订号与完整收据链；在原安装仍保留当前状态时，恢复会拒绝遗漏或改变既有配额批准/收据的旧快照，并在任何暂存盘或缓存绑定改写前停止。安装元数据中的Node修订号也阻断丢失状态记录后降级旧代码。最新静默备份可正常恢复并保留暖缓存；旧检查点自动重建权限、当前控制盘已丢失后的异机灾难接管及多中央fencing仍需独立方案，不能通过删除记录解除拒绝。

维护保持原路径，生成的systemd挂载/写路径不因额度变化而扩大；仍须按原部署的systemd启停协调操作。当前同机隔离Root演练不代表真实PID1、两物理机、设备或掉电验收。最新验证范围以PROGRESS.md为准。
