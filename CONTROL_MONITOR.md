# 控制存储只读检查与外部告警

在已安装的中央服务器上使用匹配版本的工具，以Root或中央专用服务UID运行：

```bash
/usr/bin/python3 -I -B /opt/ascend-lab-controller/current/servicectl control-monitor \
  --prefix /opt/ascend-lab-controller
```

命令只输出当前快照，不依赖HTTP、认证数据库查询或运行中的中央进程。它不会启动服务、连接SQLite、恢复journal、遍历目录、清理文件或改变准入条件。其他UID和Node安装返回unknown。当前个人Demo不是系统安装，不能拿其源码目录冒充已安装prefix；不自动改变已有部署。

检查固定的jobs/identities/observations数据库及其journal/WAL/SHM，以及server.log轮转文件和bootstrap.log。报告分量包含present_files、apparent_bytes和allocated_bytes；稀疏文件的逻辑大小不等于实际分配。拒绝链接、特殊文件和跨挂载文件；缺失的固定文件仅记为缺失，不据此判断服务可用。未扫描tmp、管理回执目录或载荷，所以分量总和不等于整个控制盘占用。

原生模式先校验控制根身份，外侧挂载缺失/替换时直接报告critical，既不读取错误挂载的config，也不统计其文件。该快照反映调用者所在挂载空间；原服务私有空间可能仍在原盘继续完成任务。底层设备IO、守护进程存活、任务执行、Node对账及NPU健康须分别监控。

退出码：0=ready、1=warning、2=critical、3=unknown。默认剩余字节warning128MiB/critical64MiB，剩余inode warning512/critical256；已分配的单个固定分量达到32MiB提示warning。可用以下参数显式调整监控阈值：

```bash
/usr/bin/python3 -I -B /opt/ascend-lab-controller/current/servicectl control-monitor --prefix /opt/ascend-lab-controller \
  --warning-free-mib 1024 --critical-free-mib 256 \
  --warning-free-inodes 10000 --critical-free-inodes 2000 \
  --component-warning-mib 128
```

监控阈值与调度准入是两套条件：报告实际control_storage准入开关和当前是否阻止新增；调整上述命令行参数不改config、队列或租约。已启用的准入暂停会报告critical，即使自定义监控阈值更低。unknown不是空闲或零占用，无法读取时free_bytes/free_inodes为null；原生根不可用时还会标明required_control_filesystem_unavailable。

## Prometheus输出

```bash
/usr/bin/python3 -I -B /opt/ascend-lab-controller/current/servicectl control-monitor --prefix /opt/ascend-lab-controller --format prometheus
```

输出采用[Prometheus文本格式](https://prometheus.io/docs/instrumenting/exposition_formats/)，固定gauge/标签，不包含用户名、作业ID、文件路径、配置或凭据。包括check_success、observation_timestamp_seconds、alert{severity=...}、free_bytes/free_inodes、readonly、admission_enabled/admission_blocks_new_work及component_*。无法观测的值没有数值sample，不填零。

这是一次性输出，不是已安装的HTTP exporter或告警发送器。管理员可以让现有监控执行它，或由外部collector原子发布textfile；需要在退出码1/2/3时仍发布完整有效的新快照，不能只在exit0时覆盖旧文件。输出不完整/进程被终止时保留上一有效文件，同时由时间戳/缺失规则告警。文本文件的目录权限、原子替换与外部监控服务由管理员另行配置，命令不写系统目录、不创建cron/timer。

至少监控critical、unknown/check_success=0、样本缺失/过期三个条件。可将`ascend_lab_control_alert{severity="critical"} == 1`作为critical规则，将`time() - ascend_lab_control_observation_timestamp_seconds > 180`及`absent(ascend_lab_control_observation_timestamp_seconds)`作为采集失效规则，按实际采集周期及主机标签配置。不要用`/healthz`的存活结果代替存储或任务就绪。

## 当前审计保留边界

认证审计原来就按全表（跨用户合计）滚动保留最近10,000条，新增记录后删除更旧行；这不是先导出再回收，也不代表长期审计归档已经实现。新检查明确输出该策略和archive_before_drop=false，不改变现有行为。身份撤销、开户、资源域和执行尝试等管理状态还分布在其他专用表/回执中，不随通用审计列表一起清理。

长期保留需另做有界导出、完整性校验、权限控制和明确确认后的裁剪。当前命令没有提供删除、VACUUM或定时清理能力。
