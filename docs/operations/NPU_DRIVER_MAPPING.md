# 驱动编号映射诊断

`tests/npu_driver_mapping_check.py`是可信维护者从完整源码 checkout 通过现有中央队列运行的固定诊断，不是安装包中的新执行后端，也不向普通 HTTP/MCP 增加权限。它用于区分宿主身份与受限容器中的驱动设备视图，补充 CANN 在 `aclInit` 前失败时尚未取得的映射证据。

固定调用仅有 DSMI 的设备数、指定物理 ID 到逻辑 ID、该逻辑 ID 到物理 ID 往返三个查询。参数分别使用 `int*`、`unsigned int` 与 `unsigned int*`，返回值为 `int`。原型参照 [Ascend 官方 exporter 源码](https://gitee.com/ascend/ascend-npu-exporter/blob/057b5e9528ff362ad7054386f5ada00a35b164e0/dsmi/dsmi_common_interface.h?skip_mobile=true)，实际运行还必须显式指定本机 Root 保护的 ABI 参考头文件并检查三个原型。没有调用猜测签名的 HAL 接口。

工具不调用 `dsmi_enable_container_service`、ACL、Context、设备 reset 或应用 kernel，不强设 SoC、修改驱动/Docker 配置或接触既有容器。映射成功也不证明运行时初始化、算子精度、性能、未授权卡隔离或完整设备健康；`qualification_granted`始终为 false，普通工作流资格不变。

这里限制的是显式调用；没有由此认证驱动库内部完全不建立管理状态或注册记录。宿主与容器的对照同时改变了身份、namespace、seccomp和部分系统依赖，结果不是某个单变量的因果证明。

先提交 `--metadata`，CPU=1、内存768MiB、NPU=0，检查镜像中的 Python、源文件摘要与任务身份，无设备且不加载 DSMI。实际查询使用相同固定镜像和 ABI 参考，去掉 `--metadata`，请求 NPU=1、有限超时；仅使用原队列分配且启动前再次确认健康空闲的一张卡。若观测改变，退出且不启动驱动查询；检查原任务和新鲜管理证据后再决定后续动作，不能因为等待或观测超时复制提交。

父进程为当前宿主身份启动一个清空环境、15秒超时的固定查询子进程；这不是 Root 负载，也不能称为容器 cgroup 隔离。随后在新容器中查询：UID/GID23901、设备补充组1001、零 capability、no-new-privileges、只读根、无网络/host PID/host IPC、硬256MiB内存与交换上限、1CPU、32PID、16MiB私有 tmpfs。仅暴露分配的 `/dev/davinciN` 及明确的管理设备，驱动与参考头只读挂载。不会拉取镜像、安装包或写入共享环境。

通过队列提交的程序参数为：

```text
/usr/bin/python3 -B tests/npu_driver_mapping_check.py
  --metadata
  --image sha256:已预检的本地镜像摘要
  --abi-header /Root保护的ABI参考/dsmi_common_interface.h
```

记录四个选中源文件（ABI头、DSMI库、HAL库、驱动版本文件）及程序 SHA，前后重新核对；不证明全部传递依赖或运行时 ABI 兼容。逻辑 ID 不假定等于物理 ID，也不假定一定是0；调用失败、未写输出或往返不一致时不生成已验证映射。输出包含两种身份、返回码、选中设备、硬限制及前后管理观测。原 stdout/stderr 留在私有任务目录，不导出其他用户的 PID、命令或环境变量。

创建前保存本任务的容器意图；无论 create/start 是否正常返回，按原名称、精确标签及镜像核对原对象，确认进程退出后删除。不能借清理删除其他对象。父 job 成功只表示生成了完整诊断，仍须检查 `host.status` 和 `container.status`，不能将查询失败包装成映射成功。

辅助的 `tests/npu_namespace_mapping_check.py`不暴露 NPU，只在无设备的限额容器里有界读取驱动 namespace 表，输出计数和已授权 zdl 的匹配数量。既有 zdl 只查询必要身份/状态字段及自己的 namespace/PID，不读取完整 `Config.Env`。驱动表的 `udevid` 没有被当成已证明的物理 ID；注册记录也不证明独占或故障根因。读取器同样保存意图并清理本任务对象。

真实结果、失败历史及最终源码/测试范围见 [PROGRESS.md](../development/PROGRESS.md)。当前仍需非 root CANN 初始化和真实计算的正向验收；不能因驱动管理查询通过而放宽普通任务准入。

## 固定子进程的系统调用观测

可选 `--trace`只跟踪新建容器中的固定诊断子进程及其超时包装器，不接受PID、任意命令或跟踪参数，不附着既有进程。先以NPU=0的`--metadata --trace`验证工具可运行，再按原健康空闲/单卡队列条件作一次实际查询。默认seccomp、零capability、非Root身份及全部硬限制保持；若跟踪被拒，应保留原日志，不能自动改成privileged、增加SYS_PTRACE或关闭seccomp。

该实现针对x86_64，显式选取宿主Root保护的strace、动态加载器及五个共享库，核对摘要并只读挂载在独立`/trace-runtime`目录。由加载器的`--library-path`仅为strace选择这些依赖，不把该目录加到被跟踪Python/驱动的LD_LIBRARY_PATH。没有下载工具、安装镜像或改变系统库；别的发行版若缺少这套固定依赖，应明确失败，而不是猜路径或改宿主安装。

strace只记录open/openat/ioctl/close。诊断进程在输出结果前，有界读取自己仍存活时的trace前缀：私有regular/nlink=1、拒绝链接/FIFO、最多256KiB，原字节SHA、截断及并发变化标记保留。原始base64只在私有任务归档；公开摘要最多64个已分配卡及管理节点事件，仅包含操作、设备路径、整数返回值和errno，不包含PID、指针、ioctl载荷或其他文件路径。缺返回值/过长行明确为不完整，不猜成功；整个快照始终不是完整syscall历史，也不证明退出后的驱动清退。

因此可以区分“设备节点open被拒”和“open成功但ioctl失败”，而不是仅据库返回87猜测其来源。跟踪本身会影响时序，结果不能用作性能测量；未观测到某事件也不能证明它从未发生。父进程即使在容器退出状态检查时失败，也会先保存本次原stdout/stderr，并核对原标签对象后清理，不创建替代尝试。

[厂商故障案例](https://www.hiascend.com/document/caselibrary/detail/topic_0000002238354198)列举了设备namespace占用相关的几种容器冲突。该案例列出的产品范围不能直接覆盖本机A5，只能作为待验证线索；其停止进程/容器的建议不构成对共享服务器的操作授权。本工具不识别或退出其他holder，也不会借namespace表推导自动修复。

## 2026-11-03 实际受限容器 trace

原队列任务`job-052aad3b1e46`在新鲜健康空闲窗口完成一次查询；此前`--metadata --trace`已验证依赖闭包。宿主DSMI计数/映射往返通过，受限UID23901/设备补充组1001/零cap/default seccomp容器计数返回87。有界存活前缀中`/dev/davinci_manager`的open与多个ioctl成功，而分配卡`/dev/davinci1`的open返回EBUSY。诊断完成不等于设备资格通过，未初始化ACL或运行应用kernel。

这把失败位置落实到设备节点open边界；仍不能确定哪个namespace/holder或驱动条件造成EBUSY，也不能据此排除所有其他因素。原始trace私有保留，公开仅选中设备操作/返回值/errno，不附着他人进程、不放宽seccomp/能力、不重置设备或停既有容器。下一步在管理员准备的独立受控池验证非root访问，当前不重复无变化探针；同卡每日数值/性能自检仍须后续真机正向验收。
