# 维护执行窗口与累计调用预算

2026-11-09开发源码。默认没有计划、定时触发或维护进程；本次没有安装或执行维护。
本入口补齐[留存选择](MAINTENANCE_POLICY.md)之后的有限执行约束，继续使用
[原单步维护](CORE_OPERATIONS.md)的Root、匹配代码、停止排空、存储及原操作恢复保护。

## v2批准对象

v1计划继续支持显式手动操作。需要窗口和预算约束时，明确生成新的v2批准对象。
执行合同为私有JSON，例如以下**有固定到期日**的示意配置，使用时必须核对实际日期：

```json
{
  "not_before_epoch": 1794182400,
  "expires_epoch": 1794441600,
  "start_minute_utc": 120,
  "window_minutes": 60,
  "max_invocations": 16
}
```

日期范围为2026-11-09 00:00 UTC至2026-11-12 00:00 UTC，到期时刻不包含在许可范围内。
每天UTC02:00–03:00内允许开始一步，即中国时区10:00–11:00；中国时区02:00需用UTC18:00，
start_minute_utc=1080。支持跨UTC午夜窗口。一次批准最多有效7天、每日窗口最多180分钟、
累计最多128次调用，字段必须是整数。它不是永不过期的日程。

Root私有目录与输入文件保持0700/0600，进程先设置umask077。可以直接在选择时绑定合同：

```bash
opsctl maintenance-select --prefix /opt/ascend-lab-controller \
  --policy /root/private/retention-policy.json \
  --execution-contract /root/private/execution-contract.json \
  > /root/private/selection.json
```

返回maintenance_plan就是v2批准对象，按[选择器文档](MAINTENANCE_POLICY.md)明确提取、核对后保存。
也可对一份尚未开始的v1计划生成新的有界审批对象：

```bash
opsctl maintenance-window --plan /root/private/original-v1.json \
  --contract /root/private/execution-contract.json > /root/private/bounded-v2.json
```

此操作只读取私有文件、计算新的摘要，不接管旧journal。已经开始的v1流程应沿原ID继续，
不要同时运行原v1和新v2审批。不能用maintenance-window修改已存在的v2预算；工具会拒绝。
系统约束以每份批准对象为单位，不限制Root另行明确批准的不同操作。

确认后仍是逐次显式调用，不自动创建守护进程或安装cron/timer：

```bash
opsctl maintenance --plan /root/private/bounded-v2.json
opsctl maintenance --plan /root/private/bounded-v2.json --apply
opsctl maintenance-status --plan /root/private/bounded-v2.json
```

preview不写文件，显示合同与当前执行阻碍。maintenance-status需要匹配安装和Root，
只读原journal，显示步骤、是否有未确认意图、已用/剩余调用数，窗口外也可读，不推进操作。
它不返回原任务正文、凭据、原命令或私有文件位置。

## 持久状态与串行执行

Root前缀的maintenance-runs目录保存原journal、原计划锁（含独立启动标记）及共享.execution.lock。
不同计划共享非阻塞执行锁：已有维护执行时，后来的调用立即拒绝，不排队等待、不起线程。
原离线状态锁、数据库和每项操作的生命周期保护继续生效。不要更换journal目录绕过这些约束。

每次真正推进前，先原子写入并fsync：原plan_id、原步骤、累计calls、调用时刻及pending意图。
失败或进程中断也消耗这次调用；恢复继续同一原步骤，不能清计数、换plan或猜测操作没有发生。
空文件、损坏JSON、丢失预算、无效计数/时间或缺少已完成结果一律拒绝，不重新初始化。
启动标记先在原锁inode中fsync并核目录持久化，再发布首次journal；已启动后journal丢失，
或journal存在而标记丢失，也会拒绝续接，不能把原批准当作新的预算。若首次标记后、
journal发布前中断，保守停止供管理员核对，不自动抹除标记重来。
轮转派生的CAS预像仍在apply前持久化，丢回包后用原预像恢复，不重新规划。

窗口在拿锁前、拿锁后和意图fsync后再次核对；窗口跨越或到期不会启动下一项操作。
若已经持久预留调用、随后越过窗口，保留该预算和pending原意图，下个许可窗口再续接。
已经开始的一步允许在现有短时预算内收尾，不能在截止瞬间强行中断数据库提交并当作未执行。
系统时钟早于上一次持久调用时刻时暂停，避免回拨绕过时间约束。

每次复制仍至多64KiB、审计至多32行/64KiB，opsctl限制单CPU、256MiB地址空间、
2CPU秒和5秒观察。max_invocations必须给复制分块、verify、轮转预像准备及apply留足次数；
它是操作开始次数的硬预算，不是成功步骤数。预像准备也消耗一次。

预算耗尽返回budget_exhausted，不执行下一步。已完整完成的计划再次调用不消耗预算。
到期返回expired阻碍；状态读取仍可看到原pending。失败或到期不删除journal、不释放未知
生命周期状态、不自动取消、扩大预算或创建另一份计划。管理员须核对原操作后决定后续明确批准。
保留整个maintenance-runs目录及计划文件，不用旧副本覆盖已用计数；此目录在Root安装前缀内，
不因服务状态目录备份就自动获得独立备份或灾备保证。启动标记防护单独文件丢失，
不宣称可检测整个Root前缀一起回滚到旧快照；不可用旧副本覆盖已用状态。

## 验证与未完成范围

[公开专项摘要](../../reports/maintenance-execution-check.json)记录本轮范围，详细测量留在本机私有记录。
新增25项临时小文件检查，加原56项合并81项通过。检查包括有限合同、跨午夜/窗口结束、
到期、回拨、失败计数、缺失/损坏状态、丢回包恢复、轮转预像、不同计划串行锁及选择器绑定。
动作回调为替代实现，没有运行真实Root操作、子进程、HTTP、Docker、NPU、线程或定时器。
锁和journal的本进程文件操作是真实执行；进程SIGKILL、原生服务和真实文件系统故障仍待验收。

这完成的是有限窗口、跨次调用预算和状态读取。定时触发部署、维护任务的完整运维生命周期、
关联SQL与历史批准/副本/索引的最终留存回收仍未完成；不等于长期自动维护或生产验收通过。
