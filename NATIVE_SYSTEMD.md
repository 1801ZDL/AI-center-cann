# 原生 systemd 的开发验收

新增 `--scenario storage` 实测原生服务与恢复器的不同挂载视图，经过真实隔离/备份/新目录恢复后由属主恢复输入并核对字节。完整 CPU 集成还可用 install_server_check.py 的 `--native-controller-image sha256:...`，连接原有独立进程模式 Node；原生控制器仍无 Docker socket，受信 Node 的容器管理权限与受限任务身份沿原合同。结果及限制见 [UNIT_RELOCATION.md](UNIT_RELOCATION.md) / [PROGRESS.md](PROGRESS.md)。

本说明面向源码维护者，验证同一发布包在原生 PID1 下的部署、启动和恢复。它不是空白宿主机安装器，不为普通任务或个人 agent 增加权限。最新结果和失败记录见 [PROGRESS.md](PROGRESS.md)。

## 已验证的范围

自有、无设备 Docker 容器使用 Ubuntu 22.04、systemd 249.11-0ubuntu3.23 和 D-Bus 1.12.20。真实 PID1 启动原生总线，安装发布包并创建容器内服务账号。原始 controller unit 以 UID999、零能力、NoNewPrivileges、ProtectSystem=strict、PrivateTmp 运行，未替换安全属性或给服务账号 unit 管理权限。

验收包含只读前置检查、文件安装、原计划加载、精确类型属性核对、ready/准入、属主鉴权 API 和原计划重放。启动调用方在收到 StartUnit 响应后被 SIGKILL，持久状态仍为 issued；恢复沿原计划找到原控制器，所有 apply 调用累计一次 StartUnit。随后杀死控制器自身，Restart=on-failure 恢复新 PID/invocation，旧启动回执保持旧身份，新服务的属主 API 再次通过。

真实管理器暴露两处兼容问题：短连接 LoadUnit 后，停止状态 unit 会被垃圾回收；ControlGroup 属于 Service 接口。现在 Root 用固定短命的原生 RefUnit 连接，在完整属性快照期间保留所选 unit 元数据，断开即释放。非 root 服务只读取已经 active 的自身 unit，不尝试 RefUnit，不在权限拒绝后切换执行方式。快照可以暂时加载元数据，但不会因此启动任务或重载管理器。

systemd-doctor 同时检查受 Root 保护的原生 libsystemd 文件；这项文件检查不加载库或连接总线。实际 helper 使用受保护安装脚本、固定系统总线和两个受支持的 unit 名，核对总线/管理器/PID1 身份，清除外部 loader/bus 环境，只在快照期间存活。

## 固定夹具与运行方式

维护者先提供已经存在的不可变 SHA256 基础镜像，满足本夹具的 Ubuntu 22.04、Python3 和 apt 前提。在原中央队列以 CPU2、内存2048MiB、NPU0 提交下列两个 argv，不向个人任务 API 开放这些管理员夹具：

```text
/usr/bin/python3 -B tests/native_systemd/prepare_image.py --base-image sha256:实际基础镜像摘要 --metadata-output /私有目录/全新-native-image.json
/usr/bin/python3 -B tests/native_systemd_check.py --image-metadata /私有目录/全新-native-image.json
```

LAB_JOB_DIR 由队列提供。镜像准备在独立有界容器中从 Ubuntu 官方 HTTPS 源下载并通过发行版签名验证，保存下载包及原生文件摘要，然后提交为本地不可变镜像；不安装宿主软件、不推送镜像。需保存仓库版本和下载记录，官方安全更新可能改变版本。实际执行的私有准备入口与公开 install_image.py 字节一致；参数化的公开 prepare_image.py 外层尚未单独重做网络安装，不称其为第二次完整构建验收。

运行夹具只使用新建自有容器：private cgroup namespace、无网络、无 NPU 或 Docker socket、512MiB、0.5CPU、128进程、有界 tmpfs。按完整容器 ID 验证宿主 cgroup leaf 及标签。固定 bootstrap 在私有挂载命名空间隔离传播，再仅将自身命名空间内 cgroup 挂载改为可写，前后验证父级限制。没有完整宿主可写 cgroup、父级资源调整或 privileged。

本机默认 AppArmor 阻止该挂载操作，因此仅自有开发夹具使用 apparmor=unconfined 及 systemd 启动能力；这不是生产 unit 或普通任务配置。最小原生启动 targets 避免无关系统服务，控制器自身仍为零能力。环境不允许夹具时报告未验，不扩大宿主权限或把模拟管理器当作原生通过。

证据在原 job 的 tmp/native-systemd：原生报告、unit 属性、响应边界、累计 RPC 计数、重启身份及私有日志。private-controller-evidence.tar.gz 含新生成的凭据和状态，禁止上传。发布只导出选定布尔值、计数、版本和源码摘要；扫描器在有界档案中仅读取固定 control/config.json 检查泄露，不提取其他成员。观测超时保留原 job，不换 key 重跑；清退前确认原容器身份和终止。

## 目标机仍须验收

这是单物理机内的真实容器 PID1，尚未证明宿主 PID1、完整开机/关机和挂载恢复。新服务器还需两个真实登录账号、目录/服务隔离、实际 Node unit、重启后盘/网络准备顺序、非 root NPU 访问与数值/性能/仿真、备份恢复。第二台物理机验证断网、重连和旧执行器隔离；没有 fencing 证据前保持单活中央，未知执行不重新分配。
