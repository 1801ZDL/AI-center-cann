# 任务测量窗口与 Node 观测

可选`npu-measurement-window-v1`接口把任务请求的计时窗口与整个资源预留区间分开。任务程序选择begin/end标记，Node记录自己的UTC/monotonic时间并在窗口外读取所分配卡的管理状态与频率。它不证明GPU实际工作全部位于标记之间，不是连续频率监测，也不自动认定性能通过或设备故障。

固定微自检在10次预热/同步后请求`mm512_fp16`窗口，30份NPU Event计时完成后请求结束。开始观测在begin回执前完成；结束时间在after查询前记录，本接口的两次边界查询位于30份Event采样之外；原有Node心跳仍可能在区间内采样。普通程序可在受控任务中使用只读载荷中的同一客户端：

```python
import runpy
measurement=runpy.run_path('/payload/npu_measurement.py')['request']
opened=measurement('begin','my_benchmark')
# 已同步的目标测量；仍由程序负责GPU同步及原始样本。
closed=measurement('end','my_benchmark')
```

旧Node不提供接口时保持原任务行为，固定微自检记录unsupported。Node通过自身源码SHA协商此能力；中央只向匹配版本发冻结合同，Node不能用不同的接口代码接受原合同。CPU任务可请求时间窗口用于验收/诊断，但不查询或申请NPU。

每个attempt独立Unix socket，只绑定该任务UID、独立随机令牌、job/attempt；没有NodeKey、Docker socket、任意命令或自定义查询参数。端点父目录由Node保护，任务只读挂载；SO_PEERCRED核身份。固定最多4个不重叠窗口、8次设备观测尝试（每次为固定管理查询加分配卡频率查询）、32条连接，重复相同ID的begin/end返回原回执，不重复查询。管理读数只保留已分配卡，不导出PID/命令或其他卡。当前查询超时沿原管理查询预算，任务可把窗口不可用与原采样分别记录。

Node先落盘操作意图与查询计数，再执行外部只读查询并保存回执；应答丢失用相同ID恢复。Worker重启或任务结束时未闭合窗口记interrupted，不拼接新旧时钟区间，不重发未知查询，不重跑任务或释放未知租约。端点恢复须取得独占本地锁并匹配受保护目录/原端点身份；原受控任务监管和归档流程继续。

中央独立校验Node传回的身份、源码、时间与上限，丢弃令牌及通信回执，仅保存规范窗口与观测。NPU任务的`npu-conditions.json`、结果表/CSV中的`device_conditions.measurement`记录窗口；CPU任务在原结果/条件摘要中显示不适用NPU。保留原性能样本、精度断言与任务状态。缺少或非法窗口为unconfirmed，不伪造通过。

before/after频率是窗口外的边界观测。只有各自时间戳确实位于窗口内的已认证心跳/频率，才计入区间内采样；裁剪后的历史明确标记不完整。没有区间内样本，不能用边界值补造稳定频率；全任务低频仍保留在原条件证据中。实际性能基线、窗口与Event关联的真机验证，以及硬件故障归因仍需另行验收。

## 中断窗口中的独立观测

窗口未闭合、Node恢复或区间时钟不能核对时，仍保留可独立验证的原before/after快照。它们归入`partial_observations`，核对原job/attempt/Node/源码/卡绑定、管理数据与任务时间范围；错误侧用固定原因标记，不抹掉另一侧。已知Warning/Alarm仍计入设备条件，原任务终态、精度断言和性能样本保留。

这些快照没有闭合区间、可靠先后关系或GPU工作证明，不填补duration、窗口内心跳/频率范围，不参与固定参考来源资格。网页/条件归档/CSV分别保留“窗口未确认”和“已知告警”。旧归档不回写，旧Node或不匹配源码继续明确报告缺失支持；实际NPU计时仍待验收。
