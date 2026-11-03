# 冷存副本：登记、复制与校验

复制操作本身是完整冷存流程的前置阶段。Root 在停止且无活动任务的中央上登记有限专用 ext4 盘，按固定计划复制终态任务的完整归档，保存 SHA256、分块进度和校验回执。**复制本身不切换读路径、不删除原件或释放额度/历史名额。** 随后的读取批准和原件回收现有独立[维护流程](COLD_RETENTION.md)；HISTORY_CAPACITY.md 的数量限制拆分仍未完成。

未额外批准读取时，副本只能由管理员验证；启用权威读取后，个人客户端沿原任务 API 下载。在线载荷/控制历史计数拆分、跨机存储身份迁移、完整异地备份及自动回迁仍未完成。

## 存储与权限

- 中央须设置 `monitor_local=false`、`scheduler.local_enabled=false`。普通个人 Demo 不直接迁移。
- 目标为管理员已准备、已挂载且空的完整 ext4 文件系统，具有 `nosuid,nodev`，根目录 Root 所有、0700；拒绝子目录 bind、原载荷盘、控制盘和代码目录。程序不格式化、不挂载、不回退到普通目录。
- 合同固定设备号、根 inode、原生容量、路径与存储 ID；登记后使用控制器 UID。个人 Agent 和任务无权访问目标目录。
- 全局及每 owner 字节/inode、单任务字节和传输次数均有限。这里的 owner 限额是应用预留，**不是每 owner 内核硬配额**；原生盘容量与空闲余量独立检查。
- 完整计划和内部证据保持 Root 私有，不上传公共仓库。权限封存不保证抵抗 Root 或存储管理员改写。

私有 `--spec` JSON 须包含以下字段，实际身份与额度由目标机管理员观察、批准，不能复制其他服务器的设备身份：

```text
storage: {path, filesystem: "ext4", device, root_inode, bytes, inodes, storage_id}
budget_bytes, budget_inodes, max_job_bytes, max_transfers
owners: {<原任务 owner>: {bytes, inodes}}
```

当前单任务不超过8GiB、单步256KiB..16MiB、每池最多10000次传输。额外保留盘上16MiB/128 inode余量。控制历史仍需容量规划，不自动轮转。

## 离线操作

先按原维护流程停止中央并确认全部任务终止。使用已激活、与安装匹配的 `servicectl`；Root 计划目录必须私有、受保护。以下路径为管理员示例：

```bash
servicectl cold-register-plan --prefix /opt/ascend-lab-controller \
  --spec /root/cold-maintenance/pool.json \
  --output /root/cold-maintenance/register.json
servicectl cold-register-apply --prefix /opt/ascend-lab-controller \
  --plan /root/cold-maintenance/register.json

servicectl cold-copy-plan --prefix /opt/ascend-lab-controller \
  --job-id job-0123456789ab --output /root/cold-maintenance/copy.json
servicectl cold-copy-step --prefix /opt/ascend-lab-controller \
  --plan /root/cold-maintenance/copy.json --max-bytes 4194304
servicectl cold-copy-verify --prefix /opt/ascend-lab-controller \
  --plan /root/cold-maintenance/copy.json
```

`copy-step` 返回 `copying` 时继续同一计划，直到 `verified`。响应丢失先检查原操作；不准备替代计划，不重跑原计算任务。每池/任务只允许一个传输操作。当前没有未完成副本取消或失效写计划重绑入口；身份改变时保留副本、账单和原计划，不能换 ID 绕过。

完整私有清单分别枚举用户索引产物和内部证据。固定控制元数据与封存源码可识别；未知内部文件在读取内容前拒绝，须管理员审查后通过最多32个 `--private-file 相对路径` 显式批准。参数不接受目录，不扫描模型配置、会话或其他 HOME。完整树超过原已结算归档额度仍拒绝。

内部证据绝不加入用户产物索引；详情 API 仅沿原任务 ACL 增加有限副本状态，不返回冷存路径、内部清单或私有文件名。`original_retained` 等字段描述复制操作的影响，不保证以后原件仍存在。未启用权威读取前属主保留原存储维护接口；启用后改走显式管理员原件回收流程。

## 中断、备份与恢复

登记意图先持久化，再采用目标盘。复制先预留完整成本，再创建文件；每块写入并 fsync 后提交进度。重启核对原 inode 和已确认前缀后，才可丢弃未确认尾部。完成后全树校验、权限封存、无覆盖原子发布，再提交 verified 回执。链接、特殊文件、额外挂载、未知文件和身份/哈希变化均拒绝，不释放原资源。

`registering`/`copying` 阻断普通备份与代码切换；复制中原件不能 trash/purge。独立 metadata-only 存储对账可推进，避免维护互锁。活动/未知执行、Node 库存 hold 和开户交接检查保持。

副本阶段普通备份包含原在线归档和冷存登记、进度、预留与回执；原件回收后则保留控制历史，**不包含独立冷存盘副本字节或 Root 私有计划原像**，二者须另行私有保留。恢复不能丢失或改写当前已知冷存历史；最新状态恢复后旧池/传输写权冻结，旧计划失效。Root 可用原计划验证副本；恢复既有 reader 另需显式只读批准，见 COLD_RETENTION.md。完全丢失最新状态的接管/fencing 仍未完成。

## 验收范围

单元检查覆盖公开/私有完整树、源/副本变化、链接/挂载、预算、分块恢复、恢复冻结、旧快照及降级。非 Root 状态机单测明确使用 Root 权限替身；实际身份另做原生容器验收。

`tests/native_systemd_check.py --scenario cold` 使用自有 systemd PID1 控制器、独立有限文件后备 ext4：真实提交后未执行即取消的任务、明确注入的传输样本、3个登记和7个复制真实 SIGKILL、同计划恢复、属主原件维护拒绝、原 API/计费保留、旧备份拒绝和最新恢复后的历史验证。没有 Node 执行、NPU kernel、真实宿主用户或两物理机验收。最终结果及中间失败见 PROGRESS.md。
