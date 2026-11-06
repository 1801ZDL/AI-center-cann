# 权威冷存读取与原件回收

在 COLD_EXPORT.md 的已验证副本之上，新增显式 Root 读取批准、分步原件回收及恢复后重新核验。任务、执行尝试、幂等键、结果、来源授权和图历史仍由原中央账本管理；个人 Agent、CLI/MCP 和网页继续使用原任务 API。

**本功能可以回收原在线归档并释放其账本预留；尚未拆分历史任务与在线载荷的数量限制。** 当前 `max_jobs` 仍按原语义计数，不能把本功能当作已经解除长期历史上限。冷存目标本身也没有自动删除或预算轮转。

## 显式批准读取

先完成原副本至 `verified`，保存原 Root copy plan，停止中央并按现有流程确认没有活动/未知任务或库存 hold。使用匹配安装版本的工具，在 Root 私有目录保存新计划：

```bash
servicectl cold-read-plan --prefix /opt/ascend-lab-controller \
  --copy-plan /root/cold-maintenance/copy.json \
  --output /root/cold-maintenance/read.json
servicectl cold-retention-apply --prefix /opt/ascend-lab-controller \
  --plan /root/cold-maintenance/read.json
```

批准前重新核对原任务/结算额度、原件清单、完整冷副本和引用快照；提交读取权威不会删除原件或释放额度。计划绑定当前安装、配置、数据库身份、原 reader 和引用摘要；不能修改后重算 ID 来代替重新审查。

批准后，详情、日志、归档清单、分块/Range 下载、四类报告、依赖交接、来源授权、环境认证和性能参考使用统一归档解析。原任务/项目/实验预算 ACL 仍在解析之前检查；内部证据没有加入用户索引，文件授权不会扩大成整任务读取权限。主机路径字段 `archive_dir` 保留历史含义，不作为个人客户端的文件读取地址。

常规读取检查池、原计划/回执、完整树元数据，再逐个核对实际返回范围涉及的256KiB块。不会为每个小范围反复哈希全部大归档，也不会只相信当前文件生成的新校验值。损坏、挂载变化和丢盘拒绝读取，绝不退回原路径读取另一份数据。原生服务可在 `ProtectSystem=strict` 的只读挂载视图中读取；只有维护路径要求文件系统可写，控制器权限没有增加。

## 分步回收原件

读取权威启用后，普通属主 trash/restore/purge 接口不再操作该归档。Root 可单独准备原件回收计划；这不授权删除冷副本：

```bash
servicectl cold-retire-plan --prefix /opt/ascend-lab-controller \
  --copy-plan /root/cold-maintenance/copy.json \
  --output /root/cold-maintenance/retire.json
servicectl cold-retention-apply --prefix /opt/ascend-lab-controller \
  --plan /root/cold-maintenance/retire.json --max-entries 256
```

返回 `pending=true` 时沿原计划继续，每次最多删除256个已批准条目。先持久化意图，再在原载荷盘内无覆盖移动到专用隔离目录，最后分步删除。完整删除库存先持久化在 Root 私有 prefix/cold-retire-inventories，SQL保留批准摘要；每步重新核对冷副本和这份原库存，部分删除后不重建批准清单。库存丢失或变化保留账单并拒绝继续。链接、额外挂载、未知/变化条目或不明位置均停止。只有原目录及隔离目录均确认不存在，才在同一事务提交 `retired` 和额度变化。保留任务/结果/审批元数据的有限账本预留；释放的是原预留差额，不等于精确的物理磁盘空闲增量。

读取批准与回收各有原操作 ID、持久回执，响应丢失继续原计划，不重跑计算、不重复记账。回收未完时普通备份与所有代码切换（包括其他对象的code-only repair）拒绝，权威冷读取仍可用。活动/未知执行和开户交接门禁保持；引用快照涵盖现有任务、Node 环境、来源授权、图、自检/参考和设备资格。已有引用继续沿原 ID 与原授权读取，原件回收不创建新 grant，也不恢复已撤销的授权。

## 备份、恢复和只读重新核验

普通控制备份包含 reader、原件回收/计费与批准历史，**不包含独立冷盘字节或 Root 完整计划原像**。删除原件后，完整恢复必须同时保留冷盘及这些私有材料。旧快照丢失或改变任一已知 reader/回收/审批历史会被拒绝，缺少兼容代码的降级也被拒绝。

最新状态恢复后，旧 reader、池和传输分别冻结。任务元数据仍可查看，冷盘内容须 Root 重新批准才能读取。先完成原有离线 unit-refresh、manager 交接；在停止态用原 copy plan 核验保留的池/副本：

```bash
servicectl cold-read-resume-plan --prefix /opt/ascend-lab-controller \
  --copy-plan /root/cold-maintenance/copy.json \
  --output /root/cold-maintenance/resume-read.json
servicectl cold-retention-apply --prefix /opt/ascend-lab-controller \
  --plan /root/cold-maintenance/resume-read.json
```

然后沿正常 unit-manager/unit-start 流程启动。若原件尚保留，重新批准还会完整核验恢复后的文件字节，并绑定其新的inode/时间戳库存，之后可继续显式回收；不能直接沿旧库存删除。该批准只恢复既有 reader，旧池/复制写权仍冻结，原预算/来源授权的恢复撤销规则不变。当前要求保留池的原生身份和服务 UID/GID；换盘、不同 UID/路径/设备身份的跨机迁移需要后续显式迁移工具，不能静默修改标记或绑定。完全丢失最新控制状态时的接管/fencing 仍未完成。

## 当前验收与待项

单元检查分别验证状态、逐块完整性、恢复、旧快照和引用保护；另以已有协议夹具验证四类实际 HTTP 报告、依赖交接及预算限定的来源授权在删除在线目录后工作，授权撤销继续阻止新工作、原幂等重放仍返回原任务。这些协议夹具不是新 NPU 或编译器执行。

`tests/native_systemd_check.py --scenario cold-read --legacy-archive <原副本阶段服务包>` 使用自有原生控制器、有限 ext4 和明确的传输样本，在已回收原件恢复、保留原件恢复后再回收两条路径各覆盖17个真实 SIGKILL、真实 HTTP 分块/Range/日志/权限拒绝、原件确认删除与一次额度提交、旧代码/旧快照拒绝，以及恢复后先冻结再显式批准读取。最新证据和中间失败见 PROGRESS.md。

接下来仍需历史/在线容量计数拆分、冷盘与控制盘的完整异地备份迁移、取消/回迁/最终冷副本删除、有限历史轮转及持续负载验收。当前每次解析仍有有界元数据校验成本，尚未证明大量冷对象、20人峰值与大归档的长期吞吐。真实宿主登录用户、两物理机故障和 NPU 计算资格仍需目标环境验收。
