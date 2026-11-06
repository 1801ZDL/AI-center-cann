# 原生 systemd 的开发验收

新增[旧pending库存管理员对账](https://github.com/1801ZDL/AI-center-cann/blob/main/docs/operations/STORAGE_RECONCILIATION.md)：停止态精确计划、同计划中断恢复、原计费/批准清单保留及缺helper的显式修复升级。输入和取消任务归档已做实际原生服务验收；它不执行冷存或自动清理，部署边界见PROGRESS.md。

新增 `--scenario storage` 实测原生服务与恢复器的不同挂载视图，经过真实隔离/备份/新目录恢复后由属主恢复输入并核对字节。完整 CPU 集成还可用 install_server_check.py 的 `--native-controller-image sha256:...`，默认连接原有独立进程模式Node；增加`--native-node-image sha256:...`可验证独立原生Node。原生控制器仍无Docker socket，受信Node的容器管理权限与受限任务身份沿原合同。结果及限制见 [UNIT_RELOCATION.md](UNIT_RELOCATION.md) / [PROGRESS.md](../development/PROGRESS.md)。

本说明面向源码维护者，验证同一发布包在原生 PID1 下的部署、启动和恢复。它不是空白宿主机安装器，不为普通任务或个人 agent 增加权限。最新结果和失败记录见 [PROGRESS.md](../development/PROGRESS.md)。

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

## 原生 Node 集成与旧 unit 升级

`tests/install_server_check.py`在`--user-quota-workspace --persistent-cache`及上述原生controller镜像参数基础上，可加`--native-node-image`。它使用同一个已验证本地native镜像，不下载新包；两个角色分别为真实PID1。初始进程安装用于兼容检查，业务任务前切换原生unit，不能称全部安装阶段均native。

本次48项实际使用完整参数如下，仍须从原中央以CPU2/内存2048MiB/NPU0及有限超时提交；两个镜像参数替换为同一个经过前置验收的本地immutable摘要。日程/规则/参考参数只演练隔离CPU夹具中的合成记录，不启用Main日程。

```text
/usr/bin/python3 tests/install_server_check.py
  --user-quota-workspace --persistent-cache
  --device-schedule --device-evidence --measurement-windows
  --performance-reference --reference-rotation --job-graph
  --native-controller-image sha256:已验收本地原生镜像摘要
  --native-node-image sha256:同一本地原生镜像摘要
```

Node是原有受信容器管理者，夹具保留其Docker socket、配额SYS_ADMIN以及既有seccomp/AppArmor测试条件；controller、个人agent和任务不获得这些权限。Node bootstrap要求其固定socket存在而无NPU设备；controller bootstrap仍要求无socket。双方都只操作已核对完整CID的private cgroup leaf，核对父CPU/内存/PID限制；没有privileged、全宿主cgroup可写或superblock-wide remount。这不是普通任务容器的配置。

Node生成unit固定CHOWN/DAC_OVERRIDE/FOWNER，分别用于端点/缓存属主转移、私有任务树访问和转移后的属主文件属性；只有UID-quota工作盘/缓存才另加SYS_ADMIN用于查询/核对。controller继续零能力。Node实际7次运行快照CAPeff2097163/UID0/NoNewPrivs，controller12次UID999/CAPeff0；没有修改任务运行时。

旧包生成的Node unit只有SYS_ADMIN或空能力行。升级应排空执行/上传、停止unit，再由匹配发布代码生成并应用offline refresh原计划，核对manager后沿startup原计划启动；仅接受该精确历史能力行且其他字节与当前生成unit一致，不采纳自定义安全属性/能力/drop-in。实际夹具先放置精确旧unit，再走刷新/加载/启动，并完成CPU业务。停Node不证明其外部任务退出；实际运行中重启验证同一容器/StartedAt/attempt持续，未因心跳中断重跑。

双原生角色48项含两个任务UID、真实BishengIR、硬配额、缓存、中央及Node运行中重启、目录恢复/回滚/任务图；设备日程/参考仍为合成协议。实际包非md源码与最终发布一致；集成后只修了夹具scope措辞和既有quota单测断言，最终完整回归单独冻结。原始私有报告/SQL/包/loop清退核对见PROGRESS.md。

## 目标机仍须验收

这是单物理机内的真实容器 PID1，尚未证明宿主 PID1、完整开机/关机和挂载恢复。新服务器还需两个真实登录账号、目录/服务隔离、宿主Node unit及重启后盘/网络准备顺序、非 root NPU 访问与数值/性能/仿真、备份恢复。第二台物理机验证断网、重连和旧执行器隔离；没有 fencing 证据前保持单活中央，未知执行不重新分配。
