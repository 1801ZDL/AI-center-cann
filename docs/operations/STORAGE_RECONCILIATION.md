# 旧格式未完成存储操作的管理员对账

这是停止态controller的Root维护入口，不是个人Agent/MCP或HTTP工具。它只处理`moving_to_trash`、`restoring`、`purging`中的旧v1库存。正常v2操作继续使用原owner存储接口，不自动迁移旧记录。

## 它确认什么

原库存指纹、操作/状态、owner、计费和revision必须一致。工具在当前视图重新扫描，保留实时FD/父目录/嵌套挂载检查；核对device/inode/路径/权限/大小及文件mtime/ctime。完整搬移要求整个树除mount ID外一致；部分purge只允许原批准清单的剩余子集，目录时间可因已批准删除而变化。完整原清单与payload_bytes保留，不把剩余大小当原批准或计费。

Root计划绑定代码版本、安装/配置摘要、数据库device/inode、原记录和本次观察。apply再次核对；同一数据库事务写入v2库存、revision、归档任务的archive_status、事件和小回执。没有rename/unlink、预算减免、任务重跑或授权恢复。属主仍须通过正常鉴权接口完成原操作。

历史v1记录只包含元数据身份；对账不能补造原记录没有的历史内容哈希，也不认证算子数值或设备状态。原始文件变化、出现新文件、根身份改变、混合/嵌套挂载、两处都存在或位置不能确认时拒绝，不猜数据该在哪。

## 正常流程

先停止新准入、排空任务和上传并停止该controller。未知任务、库存hold或开户/配额交接仍阻止维护。其他存储pending对象不要求先结束，避免互锁；一次只核对一个明确对象，其他记录和计费保持原样。普通备份/配置维护仍拒绝pending。示例ID须替换为实际值，计划文件必须是新建Root私有文件。

```bash
python3 /opt/ascend-lab-controller/current/servicectl storage-reconcile-plan \
  --prefix /opt/ascend-lab-controller --kind input \
  --id input-aaaaaaaaaaaaaaaaaaaaaaaa --output /root/storage-reconcile.json
python3 /opt/ascend-lab-controller/current/servicectl storage-reconcile-apply \
  --prefix /opt/ascend-lab-controller --plan /root/storage-reconcile.json
```

plan不登记操作意图或修改负载；apply使用同一文件。响应丢失沿原计划重放，不换ID或重建批准。SQLite事务和Root历史分别恢复，已提交回执不重复插入。输出是原对账回执；当前对象状态仍以正常存储查询为准。

重启已核对的原unit，然后由原owner创建`resume`计划并apply，或重放其原来仍被准许的维护请求。不要为了这次恢复新建要求完全静默的manager/startup配置计划；已有unit未改时可先`unit-manager-check`，再由管理员正常`systemctl start`。如果unit确有配置变化或检查失败，先按其原管理流程处理，不能跳过检查。本工具不启动服务或代替owner执行维护。

## 旧安装缺少修复工具

普通activate仍拒绝pending状态。管理员可以显式调用已验证的新发布包入口，以唯一legacy对象选择code-only修复升级：

```bash
python3 /opt/new-release/servicectl activate \
  --prefix /opt/ascend-lab-controller --archive /root/releases/server.tar.gz \
  --repair-storage-kind input --repair-storage-id input-aaaaaaaaaaaaaaaaaaaaaaaa
```

选择必须是明确legacy对象；code-only升级允许其他存储pending同时保留，其他执行/库存/开户静默检查不变，新包必须带修复helper。这一步仅切换兼容代码，不启动服务、不改库存、负载或计费。随后使用current入口执行上面的Root计划和apply。

## 留存和恢复

Root完整计划与回执位于prefix下`storage-reconcile-history/<plan_id>/`，需独立私有保留；普通控制状态备份不包含这份prefix历史。数据库中的小回执和存储事件随控制备份保留。旧快照若丢失或改写已知回执，恢复拒绝；最新备份保留回执，但目录/数据库绑定变化会使旧Root计划失效。不能靠恢复重新获得旧计划权限。

保留回执时，不兼容代码降级被拒绝。历史上限1024；尚无自动历史轮转，不应手工删行腾名额。完全丢失最新状态、真实宿主掉电及跨机fencing仍是独立部署验收，本工具不提供故障接管。

## 已验证范围

同物理机自有无设备容器内的真实systemd PID1、UID999 controller、实际HTTP输入及取消任务归档。legacy pending意图由夹具明确模拟，库存来自真实服务视图；Root与服务实际mount ID不同。新helper在prepared/updated/committed/receipt四处实际SIGKILL，同一计划收敛；计费未提前释放，属主resume/restore后原字节和归档清单保持。旧快照拒绝、最新恢复及旧计划失效已验。部分purge、变更拒绝和空安装兼容有专项/回归覆盖，未据此宣称真实宿主开机、多机或NPU通过。

当前只补维护恢复前置条件。冷存副本、读路径切换、原件维护和在线历史计数拆分仍未实现，见HISTORY_CAPACITY.md。
