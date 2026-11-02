# 固定微用例的显式性能参考

管理员可批准同一物理卡、已认证环境、执行身份及固定配方下3..8个独立任务的经验参考。它只比较固定512×512 FP16矩阵乘的30份Event样本，不宣称稳定频率、完整硬件健康、任意算子正确或优化收益。`performance_qualified`始终false；“在参考界限内”与原任务精度断言/性能样本分列。

来源必须是原始成功的受控`npu_selfcheck.py --run`任务：只读封存配方、受保护环境/设备访问与清退、六个精确微用例独立CPU核对、原报告SHA、Node测量窗口闭合、管理条件无告警/缺项。原卡/主机/驱动、Node/身份修订、镜像及工具环境、配方与观测模块源码固定。CPU任务、旧不带窗口的结果、不同卡/环境、未知执行或自报摘要不能作为来源。原Event/GPU实际对应仍须真机验收。

私有spec中的数值由管理员按目标部署批准，示例不是通用阈值：

```json
{"name":"card0-mm512","source_job_ids":["job-0123456789ab","job-123456789abc","job-23456789abcd"],"ratio_bounds":[0.8,1.2],"max_round_ratio":1.1,"max_age_seconds":604800}
```

```bash
workerctl --controller-state /PRIVATE/CONTROLLER performance-reference-add --spec /PRIVATE/reference.json
workerctl --controller-state /PRIVATE/CONTROLLER performance-reference-approve perfref-替换为编号
workerctl --controller-state /PRIVATE/CONTROLLER performance-reference-list
```

add只准备draft，不批准、不运行任务。approve重新核原件、上下文与有效期。每次执行取三轮中位数的中位数，再取独立执行中心的中位数作为参考中心；批准界限为中心乘显式ratio_bounds。来源每次的轮间变化及独立执行中心变化都不能超过max_round_ratio。不是统计置信区间或自动学习阈值。有效期从最早来源的实际执行结束时间计算，重传、重新批准或恢复不能续期。

固定驱动逐个同步30对Event，来源还须满足其总Event时长为正且不超过Node标记区间；明显单位/时间不一致不能被稳定的比值掩盖。这是按固定配方顺序添加的保守一致性条件，不是独立GPU计时认证。Event同步及返回毫秒的语义依据[Ascend官方实现](https://github.com/Ascend/pytorch/blob/master/torch_npu/csrc/core/npu/impl/NPUGuardImpl.cpp)；目标硬件上的Event与窗口对应仍需验收。

业务可在[项目自检规则](DEVICE_EVIDENCE.md)增加可选`performance_references`，按源计划每个物理卡明确映射，不能缺卡或借其他卡参考：

```json
{"name":"team-check","source_plan_id":"schedule-0123456789abcdef01234567","target_node_ids":["worker-0123456789ab"],"projects":["operator-team"],"max_age_seconds":86400,"mode":"require","performance_references":{"0":"perfref-0123456789abcdef01234567"}}
```

启用规则固定每卡参考批准摘要。默认没有参考或映射，既有规则不改变。require还要求最新逐卡自检是参考批准后完成的独立任务，且三轮中位数均处于批准界限、轮间变化在批准范围、上下文和有效期足以覆盖业务申报时长。参考自身不能给自己开绿灯，过旧/未测/偏离/不稳定保持等待。annotate只记录。运行时保存原引用，后续通过不能补原启动前缺失；原结果不覆写。

区间内频率快照数与稳定性未验证声明保留；边界频率不能补成连续时钟。经验对照可以发现异常样本，但不能据一次偏离认定硬件故障、算子回归或性能优化。根因分析仍需同卡/同环境复现、健康对照及真实测量窗口验证，不自动重置或重跑任务。

pause/retire明确撤销参考批准；绑定require规则继续等待，不隐式删除要求。重新批准改变摘要，原规则须明确新建/替换；恢复后的旧参考仅历史，不可重新批准，需新测量与新参考。恢复前拒绝丢失/倒退的定义、来源和审批历史，旧代码缺支持不能激活。来源归档被草案、保留参考、有效规则或在途引用固定，不能普通留存删除。最多32个参考历史、2016条正常审批事件加32条恢复预留，不自动剪历史。

当前没有参考历史的安全导出/轮转命令。达到32条历史或事件上限后拒绝新增，不自动删除或覆盖旧记录；长期运行前须补齐管理员确认的导出、引用检查、裁剪及恢复保护。不能靠直接删数据库行扩容。

GET `/api/performance-references`按来源项目权限分页只读，默认4条/最多8条；预算Agent不获得额外全局视图。规则消费者只收到批准参考的有限比较摘要，不获得来源原件的额外访问权。当前共享主机不建立参考、日程或规则；协议/合成验收不能替代真实NPU参考采集与管理员部署验收。
