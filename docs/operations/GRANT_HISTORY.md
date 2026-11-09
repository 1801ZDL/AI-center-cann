# 失效授权正文与预算恢复保护

2026-11-09开发源码。本轮未安装、执行维护、创建真实账号或启动服务/设备任务。
这是关联历史的一部分，尚未完成所有记录及副本/批准/索引最终留存。

## 当前状态决定权限

仅迁出已撤销、恢复冻结或已经过期的source_grants正文。原ID、owner/幂等键/fingerprint、
生产任务、目的实验、到期时间和当前失效状态保留为SQL/JSON投影，原文件名单/SHA从生产任务
的原verified副本读取。未过期active授权不迁出；revoked/frozen状态保留，过期但state仍为
active的行变为永久失效的archived_inactive，时钟回拨也不能重新可用。

owner和绑定实验先于私有文件核对。新任务先核本地状态、SQL/JSON一致性和历史标记，
历史授权直接拒绝，不为拒绝操作打开副本。普通active授权仍须原生产文件证据。
旧请求结构化重放可读原名单，但不获得新执行许可。列表先按当前owner/实验筛选，再分组
读副本，同次请求同副本一次核验，缓存不跨请求。迁出后usable=false、history_archived=true；
history_original_state仅供查历史，不能作为当前许可。幂等重放返回同一ID，不延长有效期。

撤销时间等失效覆盖层保留，snapshot之后的撤销可覆盖历史状态；文件、目的实验、owner或
审批变化则拒绝迁出。再次revoke不扩大正文或权限。读取核对原行SHA、生产任务迁出、
新批准、不可变SQL身份和完整投影；副本/索引丢失、scope改变或把历史状态改回active均拒绝。
恢复继续冻结旧授权，须保留原引用、投影和已知撤销；旧代码缺少读取器时拒绝降级。

## 维护入口和上限

新计划增加第五步grant_rotate，接在copy/verify/job-rotate/result-rotate之后。
旧三步/四步v1/v2批准对象不变；v2预算须另计prepare/apply两次调用。
派生CAS先写原journal再应用，提交前回滚，提交后丢回包复用原索引，不重规划。

```bash
umask 077
opsctl grant-history-plan --prefix /opt/ascend-lab-controller \
  --export-id 原64位生产任务control-history副本ID > /root/private/grant-history-plan.json
# 核对原选择和摘要后明确应用
opsctl grant-history-apply --plan /root/private/grant-history-plan.json
```

仍要求Root、匹配代码、停止排空、离线锁和稳定存储，不自动停服务或处理活任务。
只处理原副本所选生产任务的授权，不迁出碰巧关联的其他生产任务记录。
原副本最多256KiB、单授权64KiB、每次最多32授权/256KiB；没有授权或正文过小则保留原样。
索引最多4096行、批准最多128份，满时保留原数据；恢复冻结的副本只读，不产生新迁出。
Root副本状态增加migrated_inactive_grant_count，grant_authorization_restored明确false。
Root副本仍须独立备份/迁移，服务状态备份不能替代；批准/索引自身最终轮转仍待。

## 计费不因归档返还

experiment_charges数字行和experiment_budgets原批准仍是权威账本，本轮不迁出或删除。
匹配版本在恢复冻结旧授权前核对已知批准和扣费：原ID、owner、请求键、fingerprint、
limits/state正文、budget绑定及CPU/NPU秒数须保留，丢行或改写拒绝恢复。允许目标有更多记录，
不删成旧快照状态。排序游标只保留当前行，上限合计100000行/32MiB，超限停止。

保护针对当前可核对的已知历史；当前账本真正丢失时不能推断快照之后的未知消费。
原恢复冻结/委托令牌撤销继续生效，不能拿旧快照表面余量执行任务。
正文缩减不释放owner记录数额度、不退费或重新激活旧委托。

## 验证范围

[公开摘要](../../reports/grant-history-check.json)记录最终串行检查。31项新增小型SQL/文件
专项覆盖scope、过期回拨、active篡改、普通授权原证据、撤销/幂等、恢复/降级、事务/回包
和已知账本保留。一项使用真实小文件和原校验器核身份/权限/SHA并拒绝篡改；Root UID替代
为当前UID，离线上下文替代，不是真Root验收。临时SQL同步关/内存journal只验逻辑，
生产持久化未改。每组仍限单CPU、256MiB地址空间、5CPU秒、8秒截止，禁止真实子进程、
网络和后台线程。首轮夹具缺jobs.policy造成1个错误，补齐后通过，原错误记录保留。

执行尝试、任务图、部分预算/计费历史表示及全部副本/批准/索引最终留存仍待。
完整回归、真实Root进程故障/安装恢复、多账号/多物理机/NPU场景尚未验收。
领域Agent、夜间PR、网页扩展暂缓，真实逐卡日程未启用。

最终136项按80/25/31三组直接源串行通过，CPU合计约0.97秒、峰值约42MiB。

```bash
python3 scripts/check-focused.py --direct-source --module test_grant_history --module test_budget_restore --module test_result_history --module test_maintenance_policy --module test_repository_layout
python3 scripts/check-focused.py --direct-source --module test_maintenance_execution
python3 scripts/check-focused.py --direct-source --module test_core_operations --module test_collector_safety --module test_read_retry
```
