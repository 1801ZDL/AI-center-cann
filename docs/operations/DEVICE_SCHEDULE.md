# 中央每日 NPU 微自检计划

这是NPU_SELFCHECK.md固定配方的可选中央编排。新/旧安装默认没有计划，不安装cron/systemd timer，不在当前共享主机创建或启用每日任务。仅中央本地服务管理员可创建、启用、暂停或退休计划；HTTP仅提供沿原owner/project权限的只读状态。个人Agent不能创建计划、修改预算、执行Root探测或重置设备。

前置：独立的已激活机器人，只有worker-python环境、模板模式、max_running=1、至少两个等待名额及足够CPU/RAM/NPU/输入/归档额度；指定Node必须已映射该身份、有认证的已知正常torch/torch_npu环境、明确卡名/设备合同、accuracy_v1与npu_access_probe模板和新鲜可信主机/受保护runtime观察。创建不会提升任何权限，缺项拒绝；未知/过期访问资格可通过计划内的固定访问探针验证，避免要求自检先已有自身资格的循环门。

本地私有spec示例（数值需由管理员按服务器实际批准）：

```json
{"name":"daily-npu","owner":"device_robot","project":"device-health","node_id":"worker-0123456789ab","device_ids":[0,1],"timezone":"Asia/Shanghai","at":"02:00","window_minutes":240,"memory_mib":2048,"max_probes_per_card":2,"daily_npu_seconds":840,"total_npu_seconds":25200}
```

```bash
workerctl --controller-state /PRIVATE/CONTROLLER device-schedule-add --spec /PRIVATE/daily.json
workerctl --controller-state /PRIVATE/CONTROLLER device-schedule-enable schedule-替换为编号
workerctl --controller-state /PRIVATE/CONTROLLER device-schedule-list
workerctl --controller-state /PRIVATE/CONTROLLER device-schedule-pause schedule-替换为编号
```

创建只封存普通owner输入并保存暂停态计划；enable是显式授权。现有中央调度循环在时区窗口内创建每卡日记录并通过普通队列提交，时间不是强制开跑时间。精确Node/卡、原资格、镜像/版本、硬配额、暂存和清退全部继续检查。只在管理读数空闲时准备新步骤；后台任务在前台之后，不积累阻塞前台的老化hold，并让位于同物理域活跃前台及可运行等待任务。不会抢占/取消其他用户工作，每个物理域只允许一个活动每日计划。

时区使用系统IANA数据库；缺失时拒绝创建，不自动安装全局包。重复时刻采用fold=0，缺失时刻按该偏移转换后的真实UTC时刻执行，同一授权/当地日期/卡只有一个日记录；重启或时间倒退不产生历史日期补跑。详见[Python ZoneInfo](https://docs.python.org/3.10/library/zoneinfo.html)。错过整个窗口不自动回填；没有完成的数据仍未验证。

访问探针成功后提交固定微用例。等待中资格过期只在窗口和max_probes_per_card内有限复探，原UT job保持不变；探针失败或执行未知时跟踪原handle，不自动替换/重跑UT。超过窗口取消仍未分配的本计划任务；已授权启动/运行/未知尝试继续原监督及清退，不能凭时钟释放资源。暂停只停止新工作并取消本计划已知queued任务，不中止已启动工作；重新启用须先协调原任务并显式批准当前上下文。

每次提交的job/action绑定与预算扣账在同一中央事务内，按申报NPU秒（访问120秒，UT180秒）计费；响应丢失继续原job，取消不退还已保留预算。当地日期账本和计划累计总账跨暂停/重新批准保留，不因新授权代数清零。没有自动增加预算。机器人原有用户/项目/存储预算仍独立检查。计划与输入保留至明确retire，不能由通用留存删除；退休要求原任务都已确认终态，历史预算不删除。当前上限32个计划历史、4096个日/卡记录；达到上限需明确维护，不自动剪历史。

主机/驱动、Node投影、机器人单调身份修订、认证环境或固定配方变化不会默默继承通过状态；需要显式新批准，配方改变须新建计划。离线恢复将非退休计划冻结为仅历史记录，不能重新启用；管理员须明确创建新计划并批准新预算，累计旧账本保留。恢复也不能丢弃当前仍保留的计划、原任务绑定和预算历史，避免旧快照恢复额度。已保留每日授权/预算历史时拒绝降级到缺少该模块的中央包。

日记录保存原job/attempt、环境上下文与有限结果。微用例通过只表示该有限参考的观察值一致，性能基线、实际频率、全卡覆盖、完整硬件健康和业务结果资格仍未认证。当前没有真实NPU正向或外部告警发送器；CPU/合成协议验收不能替代新服务器非root NPU数值/性能、实际系统时钟/时区维护及两机故障验收。
