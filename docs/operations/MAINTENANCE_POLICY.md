# 有限留存策略与自动选择

2026-11-09：开发源码，默认关闭；本次没有安装、停启服务、执行维护或创建定时任务。
本功能为现有[单步维护](CORE_OPERATIONS.md)生成明确计划，不能代替完整关联历史轮转。

## 管理员使用入口

先在隔离环境安装匹配版本的中央。Root 在私有目录保存0600策略文件，例如：

```json
{
  "format": "ascend-lab-maintenance-policy/v1",
  "enabled": true,
  "min_age_seconds": 604800,
  "keep_recent_per_owner": 5,
  "control_high_watermark": 1000,
  "target_control_jobs": 900,
  "max_jobs": 16,
  "scan_rows": 128,
  "hold_job_ids": [],
  "selection_start_minute_utc": 120,
  "selection_window_minutes": 60,
  "archive_budget": {"bytes": 16777216, "exports": 32}
}
```

```bash
umask 077
/opt/ascend-lab-controller/current/opsctl maintenance-select \
  --prefix /opt/ascend-lab-controller --policy /root/private/retention-policy.json \
  > /root/private/retention-selection.json
```

命令需要现有的Root、匹配安装、停止并排空中央、稳定存储和离线状态锁。
程序不为满足这些前提自动停服务。窗口仅控制**选择计划的时间**，不是后台执行日程；
上例为UTC 02:00–03:00，即中国时区10:00–11:00。若希望中国时区02:00–03:00，
请设置selection_start_minute_utc=1080（UTC前一天18:00）。跨午夜的窗口也支持。

返回JSON只面向管理员，包含私有安装绑定及原任务ID，不进入普通用户网页或通知。
有候选时，maintenance_plan字段就是原维护入口可接受的审批对象：

```python
# 在Root私有目录中明确提取批准对象；目标必须不存在。
import json
from pathlib import Path
selection = json.loads(Path('/root/private/retention-selection.json').read_text())
with Path('/root/private/retention-plan.json').open('x') as output:
    json.dump(selection['maintenance_plan'], output)
```

上述示例需在umask077的进程中执行。核对选中ID及摘要后，用原
`opsctl maintenance --plan /root/private/retention-plan.json`预览，显式批准后每次加
`--apply`推进原计划一步；不反复选择替代已开始的计划。
返回的policy_sha256说明采用的策略内容；原maintenance_plan.id批准精确的原任务、
副本和安装绑定，不提供自动执行授权。修改策略不自动改写或撤销已有审批对象。

## 选择规则与资源边界

- 默认enabled=false，不打开中央数据库。窗口之外也不打开数据库。
- 需要已经明确启用的双历史容量策略；达到control_high_watermark才选择。
  水位统计未迁出的控制任务，既有compact记录继续占自己的有限索引，不当作无限容量。
- 每次最多选择min(max_jobs,当前控制任务数-target_control_jobs)，最多32任务。
  每页最多256行，单任务JSON最多64KiB，累计解析最多512KiB。
- 每人最近keep_recent_per_owner个**按提交rowid排序的任务**保留，包含活跃和已迁出任务；
  不是按完成时间排序。hold_job_ids最多128个明确ID，始终保留。
- 只选择有明确带时区finished_at且超过留存期的终态。未来、缺失、无时区时间不会猜测。
- 使用原online_proof核对purged或cold-retired凭据、计费与online=0记录；目录缺失不是凭据。
  超大正文跳过；列与JSON身份不一致、回收凭据或额度不一致则失败并保留全部原记录。
- 单次SQL进度回调最多约200万虚拟机指令；现有opsctl另限制单CPU、256MiB地址空间、
  2CPU秒及5秒观察。耗尽预算不产生部分批准对象；不清理原记录或重试执行任务。

next_after_rowid非null时可显式用`--after-rowid N`继续有界扫描；无候选且已读到尾部则为null。
达到候选数量上限时返回最后一行的cursor，下一次可能得到空尾页。cursor不是快照或授权，
每次重新按当前窗口、水位、策略和凭据选择；到下一轮扫描需显式从0开始。

选择、原副本计划生成共用一次已停止中央的原数据库事务，避免两个离线锁之间的状态变化。
原副本预算、128份总上限、未完成副本优先恢复、安装/配置/DB身份及匹配代码检查全部保留。
所选任务及关联数据的完整副本仍限256KiB；超出时拒绝，应减小max_jobs，不截断关联数据。
新生成计划依次为control_copy、control_verify、control_rotate、result_rotate；
末步沿[结果正文迁出](RESULT_HISTORY.md)协议，没有结果或正文较小时可无操作完成。
旧三步批准对象不会自动改变，真正执行继续使用原保守协议。

## 本轮证据与未完成范围

[公开专项摘要](../../reports/retention-policy-check.json)记录范围，详细测量仅留本机私有记录。
17项新增小型SQLite检查验证策略、时区窗口、保留规则、游标、大小与SQL预算、
原回收凭据、纯读取生成计划及原副本预算/未完成状态；和既有39项合并56项通过。
Root离线上下文在测试中替代，选取/凭据/源副本计划使用真实内存SQLite及现有函数。
没有真实安装、Root锁、冷盘、进程中断恢复、维护执行或完整回归验收。

后续同日已补可显式绑定的[v2执行窗口和跨次调用预算](MAINTENANCE_EXECUTION.md)，
不会自动升级已生成的v1计划。仍未实现：定时触发、完整维护任务管理、所有关联SQL与批准/副本/
索引的最终留存回收。当前保留原attempt/result/grant/账单/图等SQL语义，不能承诺无限历史。
默认不安装cron/timer，也不自动启动夜间PR分析、设备UT或任何计算任务。
