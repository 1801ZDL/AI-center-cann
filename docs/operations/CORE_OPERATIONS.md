# 核心运维功能：默认关闭、有限执行

2026-11-09追加[失效授权与预算恢复保护](GRANT_HISTORY.md)：新计划增加grant_rotate第五步，
旧批准不变；本地失效状态决定权限，数字账本不因归档返还。

2026-11-06 开发源码同步版本，非生产发布包。没有启动控制器、Worker、定时器、通知投递或调试任务。
本轮只能做短时、低资源开发检查；真实 Docker/PTY、安装、恢复和完整回归尚未执行。
不得把下面的接口已实现理解为部署验收已完成。

## 资源压力准入

中央配置的可选 `scheduler.resource_pressure`：

```json
{"enabled":true,"max_age_seconds":20,"memory_full_avg10":10,"io_full_avg10":25,"reserve_mib":128}
```

默认 enabled=false。开启后，新的本地任务和 Node 分配必须有有效新鲜观测。
读固定的 proc/cgroup v2 文件，不运行 df、Docker exec 或 NPU 命令。
可用内存取宿主 MemAvailable 与可见所有祖先的 memory.high/max 剩余额度的最小值；
同时保留 swap 余量、最高 memory/IO full avg10 PSI。高压力、未知或过期暂停新分配，
不终止既有任务、不释放未知租约。原资源账本、任务硬限额和存储预算继续生效。

应将 Node 放在与任务执行边界相符的宿主命名空间。隐藏的祖先限制无法凭空推断；
本采样器不能代替任务实际 cgroup 的硬限制，也不宣称观测可避免所有准入后竞争。
旧 Node 不提供新观测时，开启策略会保守等待；不要拿宿主空闲量代替缺失值。

`opsctl pressure` 是显式一次性只读检查，不启动采集线程。

2026-11-09新增可选CPU/PID反馈，仍在`scheduler.resource_pressure`内配置：

```json
{"enabled":true,"cpu_some_avg10":80,"reserve_pids":32}
```

两个新字段默认均为`null`，分别表示不启用该项准入；上例仅演示配置格式，不是当前服务器
已启用的阈值。CPU使用宿主与可见祖先的`some avg10`最高值，表示等待压力，不是CPU利用率。
PID余量取可见祖先有限`pids.max-pids.current`的最小值，下限为0；读取上限前后变化取较严者。
PID控制器也计入线程。[内核PSI说明](https://docs.kernel.org/accounting/psi.html)与
[cgroup v2 PID说明](https://docs.kernel.org/admin-guide/cgroup-v2.html#pid)定义了这些接口。

采样仍只读取固定proc/cgroup文件：最多32层、每文件16KiB，无进程枚举、fork、Docker exec、
设备调用或新增采集线程。CPU/PID分别记录未知，不因一种控制器缺失丢弃另一种有效观测。
根层可以没有pids接口；非根层缺失则PID未知。全部可见上限均为max时记为no_visible_limit，
不将其当成无限可用PID；启用PID准入后这种状态也等待管理员确认有限边界。

原`resource_pressure`观测格式保持v1，CPU/PID放在单独的`execution_pressure`字段，Node声明
`cgroup-cpu-pids-pressure-v1`能力。旧Node仍可在未启用新阈值时接入；启用后缺失、过期或坏
观测会暂停新分配。声明能力但不提供合法观测的心跳返回400，不覆盖原有效报告。新Worker
同时发送两份观测，旧中央按现有心跳字段过滤方式忽略扩展；真实混合版本滚动升级尚未验收。
Node资源API显示数值但不暴露cgroup路径。`opsctl pressure`保留旧顶层字段并附加新观测。

Node分配要求PID余量至少为现有任务容器上限128加reserve_pids，入口与容器参数共享同一常量；
辅助探针的32上限保持不变，统一按128检查是保守估算。本地协作入口只检查至少1个启动PID
加余量，不能据此证明后续任意命令都有足够PID。高压力不会终止既有任务、撤销原预留或
重提任务；健康事件沿原outbox记一次阻塞及一次恢复，不新增投递线程。

这些是**观测进程可见祖先**的压力反馈，不是实际容器/每租户的资源测量或原子PID预留。
Node服务与Docker任务可能处于不同cgroup分支；必须由管理员核对实际执行拓扑。隐藏祖先、
每租户执行边界关联、启动前再次核对及真实Root/容器/跨机压力场景仍待，不能承诺避免所有
准入后的竞争，也不能限制用户绕过调度器自行运行。

## 防止监控探针累积

周期采集不再进入租户容器探测共享内存；使用量为 unknown，仅保留配置上限。
监控的 inspect/top/NPU 查询使用私有持久 journal、非阻塞独占锁和一次一个探针。
超时先终止本次探针进程组，再最多观察0.2秒；仍存活则保留 PID/启动时刻/boot 身份，
阻断之后的探针，包括同一服务重启后的再次启动。启动状态不明也保守阻断，不反复 fork。
已确认退出的失败探针也进入持久冷却，首次60秒、连续失败逐步延长至900秒；
成功才清除失败计数。重启服务不会消除冷却或未知进程记录。

NPU 准入、频率和测量查询经同 UID 的 `/tmp/ascend-lab-npu-probe-UID` 共用保护，
个人监控另在当前 runtime/monitor-probes 留证。它们只允许固定命令；不允许任意 shell。
周期个人监控不再额外查询逐卡频率，字段保持 unknown。
不把 Docker 客户端退出视为远端 exec 已结束。没有自动清 journal 或重启容器的路径。

## 有限历史维护

2026-11-09新增[有限留存策略与自动选择](MAINTENANCE_POLICY.md)：按年龄、水位、
明确保留名单和每人最近提交任务，在原离线事务中生成下述原格式计划。默认关闭，
不会自行执行或安装定时触发；完整关联SQL最终轮转仍待。

同日补充可明确批准的[v2执行窗口与累计调用预算](MAINTENANCE_EXECUTION.md)：
最多7天/128次推进、私有持久计数、同前缀串行锁、只读状态；失败和中断也计费。
未配置v2合同的原v1保持手动入口，不自动获得时间或累计调用限制。没有启用任何timer。

`maintenance_runner.plan(prefix, steps)` 为明确选定的原始操作生成摘要绑定计划。
Root 把计划保存为0600文件，然后使用匹配安装中的：

```bash
opsctl maintenance --plan /root/private/maintenance.json
# 显式批准后，每次仅推进原计划的一步；未完成则下次继续同一计划
opsctl maintenance --plan /root/private/maintenance.json --apply
```

preview不写文件。真实执行要求匹配版本的已安装中央；底层维护仍要求停止态及全部原保护。
这里不会为了维护自动停服务、自动排空队列、启动工作负载或改变现有容量。

支持步骤：

- audit_export：输出到指定受保护、独立于控制盘的 archive；最多32行/64KiB。
- audit_prune：只能引用本计划更早 audit_export 步骤的确切 SHA 和原路径，先核验再裁剪。
- control_copy/control_verify/control_rotate：三步均绑定同一个原 control_history 计划。
  原选择最多32任务、总正文最多256KiB，每次copy最多64KiB；verify之后才沿原规则迁出。
  rotate再次核对原任务集合。未满足原 purge/cold-retired 条件的任务不会被自动回收。
- result_rotate：同样绑定原control_history计划，在任务正文迁出后先保存派生计划再按
  CAS执行。[结果正文读取](RESULT_HISTORY.md)保留当前ACL、分页和CSV，不新增回收额度或授权。
  旧三步批准对象保持原样。

计划journal保存在prefix/maintenance-runs/PLAN.json，原意图先落盘；失败或超时保留原步，
下次复用既有幂等操作，不更换任务ID、计划或归档。每计划最多32步，完成后不继续生成新计划。
opsctl限制单CPU、256MiB虚拟内存、2CPU秒和5秒观察；超时不代表底层已完成或允许换计划。

没有安装cron/timer。以后管理员可在批准的维护窗口外部触发这个有限入口；维护窗口、
新归档盘容量和下一份明确选择的计划仍由管理员控制。
原计划可用[永久撤销入口](MAINTENANCE_EXECUTION.md#永久撤销后续执行)阻止后续推进，
保留原预算、pending和预像；撤销不会回滚或代替原操作的故障核对，默认只preview。
执行尝试、授权/撤销、账单和任务图等关联SQL仍保留原语义，未实现整个关系图的最终轮转；
也没有自动VACUUM、删除用户源目录或无限期保留的承诺。

## 通知和告警

可选 `scheduler.notifications={"enabled":true,"max_pending":1024}`，默认关闭。
状态变更事件与任务写入同一事务；消息仅含owner、job ID、固定状态和时间，不含命令、
日志、对话、模型密钥或磁盘路径。`GET /api/notifications`仅返回本人最近50条和溢出数。
队列总行数有限；满时不删除未投递消息、不阻塞计算完成，记录可见溢出。

Root 私有 destinations 文件格式为 `{"alice":{"url":"https://notify.example/hooks/alice"}}`。
仅管理员配置目的地，HTTPS/443、校验证书、无重定向和环境代理。URL不进入用户API或错误输出。

```bash
opsctl notify-once --prefix /opt/ascend-lab-controller --destinations /root/private/destinations.json
# 显式许可后最多发一条；没有常驻发送线程
opsctl notify-once --prefix /opt/ascend-lab-controller --destinations /root/private/destinations.json --send
```

发送前有限检查最多32个Node的心跳/压力及控制存储；状态变化和恢复才记录。
超过32个Node用返回的health_next_after和--after-node显式分页，下一轮从头开始。
过期观测/未知不是健康。没有给接收端配置目的地的owner不会阻塞其他已配置owner。
独立连接先领取30秒发送租约，网络IO不占SQLite事务；失败60秒退避，默认最多8次。
发送工具持有正常共享状态锁，备份/恢复的独占维护不能与发送状态写入交错。
这是至少一次投递：接收端必须按Idempotency-Key去重，网络断开后不能保证对方未接收。
目的地变更不会把已绑定消息自动重定向。未完成/耗尽/溢出保留供运维核对。

`labctl --profile /本人/private/client.json notifications`可读取本人事件。
已投递事件也受队列总量限制；用以下显式有限维护入口释放已确认投递的行：

```bash
opsctl notification-archive --prefix /opt/ascend-lab-controller --directory /受保护的个人服务归档目录
opsctl notification-archive --prefix /opt/ascend-lab-controller --directory /受保护的个人服务归档目录 --apply
```

目录须预先存在且属中央服务UID、权限0700。每次最多32条/64KiB，处理delivered或明确closed行，
先写0600副本、fsync并独立重读核对，再同事务提交精确原行回收与单行链检查点。
未投递/失败消息未经明确close仍保留；最多256份文件，满后须显式安排外部档案轮转。
这些历史副本供管理员保存，个人通知接口只展示仍在线的最近事件；原任务历史不受影响。
恢复拒绝回退投递/溢出/归档检查点或交互输入记录，恢复后通知投递和交互新会话均关闭，
须管理员重新明确开启，不能从旧配置自动恢复外部副作用。

## 通知失败与目的地变更的明确处置

2026-11-09新增`notification_ops.py`和三个Root入口：只检查或修改原消息的控制状态，
不连接接收端、不启用通知策略或后台投递。使用匹配的已安装中央版本；文件须为Root私有
普通文件，目录按既有部署要求准备。以下在管理员Root shell中执行，**本机未运行**：

```bash
umask 077
opsctl notification-list --prefix /opt/ascend-lab-controller --destinations /root/private/destinations.json
opsctl notification-plan --prefix /opt/ascend-lab-controller --event-id event-原32位ID --decision retry --extra-attempts 2 --destinations /root/private/destinations.json > /root/private/notification-decision.json
opsctl notification-apply --prefix /opt/ascend-lab-controller --plan /root/private/notification-decision.json --destinations /root/private/destinations.json
```

list默认16条、最多32条，通过next_after/--after-id按原ID分页；每次调用是一个一致的SQL读取。
跨页期间的新消息应在下一轮从头核对。显示pending/leased/retry_exhausted/held/closed/delivered、
累计attempt与上限、目的地same/changed/unbound/not_configured及失效租约不确定标记。
未提供目的地文件时明确记not_checked，不推断配置缺失。列表给出固定事件/任务ID/状态摘要，
不输出Webhook URL、完整payload、租约token或模型/日志信息。本人通知API新增delivery_state和
delivery_attempt_limit，仍先按owner选择原消息。

decision支持四种处置：

- hold：暂停原事件，保留已有attempt和目的地；不关闭消息。
- retry：只允许原owner的原目的地（或首次尚未绑定的目的地），可显式加0至8次预算。
- rebind：明确绑定同一owner当前私有配置中的不同目的地；保留原ID及累计attempt。
- close：终态关闭，不设置delivered。尝试过的消息仍记投递结果unknown，不能宣称未送达。

活动30秒租约存在时四种操作均拒绝。失效但未结清的租约，或尝试过消息的close/rebind，
必须在plan阶段明确加`--accept-uncertain-delivery`；这表示接受原投递可能已经发生，不会
撤回接收端请求。仍在途的旧发送器以后返回时不能覆盖新处置，返回lease_superseded。
相同目的地重试沿原接收端按事件ID去重合同；跨目的地去重不能保证，须管理员先核对。
已送达或已关闭事件不允许重新打开，配置文件变更不会自动转发旧消息。

计划绑定中央release、服务UID/GID、状态路径和数据库设备/inode、原行与控制状态摘要，
有效期一小时。apply在原共享状态锁和单一SQL事务内核对预像、目的地摘要、租约及预算。
记录、状态和累计预算一起提交；失败不重规划。回包丢失后重放原计划只返回原receipt，
不再次处置、延长有效期或重复增加预算。已经归档的原计划不复活事件，须查原归档凭据。
这些入口允许在通知策略关闭时管理既有队列，但不会开启通知策略；后续投递仍需独立
`notify-once --send`，本轮没有实际发送。

每事件累计投递上限64；最多8份处置记录，最后一个位置只用于close。在线处置账本和
非closed事件的关闭预留合计最多1024，满后仍能使用既有预留关闭已登记事件，并沿归档
回收额度；不能静默删除失败消息或重置attempt。处置凭据/控制投影缺失或改变会拒绝后续
读取/发送。Root恢复检查增加这两张表，降级必须保留对应实现。

发送器在选择最老事件之前过滤目的地不符的记录，因此旧坏路由不会阻塞另一个owner或
同owner的新未绑定消息。原消息仍留存，只有明确rebind后才允许使用新路由。
归档v2将已送达/明确关闭的原事件、全部处置及控制投影一起封存；按完整事件缩小批次以
满足64KiB，验证原字节后同事务CAS删除这三类行。未处置事件不删除，原检查点链继续。
没有处置记录的旧归档仍用v1格式。文件计数仍有限，外部归档轮转和渠道/订阅管理仍待。

当前只有小型SQLite、私有临时文件及替代Root/HTTP检查；没有真实Root安装、HTTPS接收端、
进程崩溃或跨服务器通知验收。实际操作者应核对当前状态，不能把这些专项当成生产放行。

## 受控交互调试

可选 `scheduler.interactive_debug={"enabled":true,"owners":["alice"]}`，默认关闭。
Node必须明确登记debug_v1模板并提供新transport capability；旧Node不领取此类任务。
它是个人所有的独立调试作业：普通中央队列/许可/硬限额/日志/取消/归档全保留，
不附着到zdl等原容器，也不提供Node Docker socket、Root shell或宿主目录。

冻结请求沿普通工作流包含封存input_id和环境lock；template=debug_v1、
environment=worker-python，parameters.argv固定["/bin/bash"]。
1..2CPU、128..2048MiB、NPU=0/SHM=0、总时限1..300秒、空闲30秒；不接受持久缓存、
共享项目、Agent预算委托或任务图。命令只在原非root、无网络的受控容器里运行。
shell访问/inputs封存源码和/work个人工作区，环境受现有protected bootstrap核对。

```bash
debugctl --profile /本人/private/client.json open --request /本人/private/debug-request.json
debugctl --profile /本人/private/client.json status job-原ID
debugctl --profile /本人/private/client.json attach job-原ID --session-dir /本人/private/debug-session
debugctl --profile /本人/private/client.json close job-原ID
```

当前是显式逐行PTY客户端，/logs查看原任务输出，/status查看会话，/detach离开，/close取消。
不是完整网页终端、SFTP或IDE调试器，也不自动申请下一会话。
每会话最多64帧、保守累计64KiB输入，运行时输出上限1MiB；Node监控及既有日志上限仍生效。
输入先保存个人请求键，再经认证owner API和原attempt更新投递到原容器stdin，绝不docker exec。
Node发送意图在IO之前持久化；不确定时不重发，后续输入暂停，可查看日志或取消。
stdin_sent只表示字节已交给传输，不表示命令执行成功；实际结果以原日志和终态为准。
重连复用session-dir/pending-input.json，禁止换key重发原输入。

## 当前验证边界

只允许纯mock、临时小文件和内存SQLite检查，不启动PTY、shell、Docker、NPU或网络发送。
真实PTY attach、原生安装/中断恢复、异地档案持久性和完整回归仍待授权环境验收。
这不会恢复本机已停的demo，也不会重启zdl或改变当前服务器上的其他任务。
