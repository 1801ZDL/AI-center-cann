# systemd 状态目录恢复与存储指纹

面向部署管理员。恢复仍须停止、排空、使用新目录并保留原件。实际容器验收、失败和源码范围见 [PROGRESS.md](PROGRESS.md)；宿主开机、native Node unit、真实登录用户、两机与 NPU 仍需目标机验收。

## 恢复后的 unit 交接

原 restore 生成新目录模板，系统旧 unit 仍指向旧目录，严格刷新曾把它判作自定义文件。现在恢复先核对 Root 原模板，在 installation.json 保存其字节、前后目录、精确安装绑定及新模板摘要，再发布新绑定。记录和目录已 fsync，模板原子替换。

刷新仅接受当前生成的 unit、既有允许的存储组变化，或此次恢复记录中的精确原文件。执行命令、隔离属性、目录、角色、UID/GID、绑定或目标模板不符仍拒绝。记录来自 Root 安装元数据，不来自用户或 Agent 的 JSON。新绑定发布后、模板替换前被杀，可以依据该记录继续 offline refresh；模板更新不会自行启动或重载。

先准备私有计划目录，再为每个新操作选择未使用的输出文件：

```bash
install -d -m 700 /root/ascend-lab-plans
/opt/ascend-lab-controller/current/servicectl unit-refresh-plan --prefix /opt/ascend-lab-controller --output /root/ascend-lab-plans/restore-refresh.json
/opt/ascend-lab-controller/current/servicectl unit-refresh-apply --prefix /opt/ascend-lab-controller --plan /root/ascend-lab-plans/restore-refresh.json
/opt/ascend-lab-controller/current/servicectl unit-manager-plan --prefix /opt/ascend-lab-controller --output /root/ascend-lab-plans/restore-manager.json
/opt/ascend-lab-controller/current/servicectl unit-manager-apply --prefix /opt/ascend-lab-controller --plan /root/ascend-lab-plans/restore-manager.json
/opt/ascend-lab-controller/current/servicectl unit-start-plan --prefix /opt/ascend-lab-controller --output /root/ascend-lab-plans/restore-start.json
/opt/ascend-lab-controller/current/servicectl unit-start-apply --prefix /opt/ascend-lab-controller --plan /root/ascend-lab-plans/restore-start.json
```

pending 时保留并继续应用原计划，不另发启动或任务。恢复后的已登记 Node 须向新中央确认库存，才能完成启动准入；期间新提交可收到503且尚未创建任务，应保留原请求/幂等键。正常重启使用已经核验的 systemd unit，不在任务运行中重做要求排空的配置计划。

先激活含匹配 unit_relocation.py 的版本再恢复；保留新合同后不能降级到缺少它的版本。这里只修复绑定发布后的 unit 交接。复制阶段失败仍须检查保留目录，SIGKILL 也不能证明所有复制或整机掉电边界通过。

## 持久库存与实时挂载检查

Linux mount ID 属于进程的挂载视图，卸载后还可能复用，不能直接作为跨命名空间的持久对象身份，参见 [Linux mountinfo 文档](https://man7.org/linux/man-pages/man5/proc_pid_mountinfo.5.html)。本机离线恢复中，恢复器和服务看到同设备号、inode、权限、长度和时间戳，只有 mount ID 不同，旧指纹因此返回409。

新格式 ascend-lab-storage-inventory/v2 在原始库存中保留 mount ID，但持久指纹只排除这个临时字段，保留设备、inode、路径、权限、长度和时间戳。归一化前必须验证整棵树处于同一挂载。不会把另一块盘、另一 inode 或同名目录自动视为原对象。

扫描仍拒绝对象根的挂载边界和嵌套挂载，包括同设备 bind mount。删除在当前命名空间重新扫描，将 root FD、子 FD 和最终父目录绑定到本次实际挂载；扫描后替换挂载仍拒绝。分批删除保留原清单及计费，确认完成才释放配额。Node 的已确认产物清理沿用此合同。

无格式标记的旧库存继续按原指纹和 mount ID 检查，同一视图内可继续；不会在读取时自动忽略旧身份。验证过的离线快照恢复可重建新格式并增加修订。旧预览可能需重新预览，已提交操作仍保留原意图。旧 pending 操作若失去原视图，保留数据和计费，须管理员另行核查；当前没有自动重绑或强制删除入口。

保留新库存的中央、含新库存待清理记录的 Node，拒绝激活缺少 storage_inventory.py 的旧包。不增加任务、计算预算或存储配额，不合并用户产物与私有内部证据。

## 维护者验收

通过原中央队列提交以下 argv：

```text
/usr/bin/python3 -B tests/native_systemd_check.py --image-metadata /私有目录/native-image.json --scenario storage
```

该场景在自有、无网络/设备/Docker socket 的 PID1 容器中实际创建、隔离、备份和恢复输入，由属主申请恢复并核对原字节。挂载视图仅读取已验证的本容器服务 PID，以同一服务 UID 进行；不附着其他用户进程或增加 ptrace 权限。

完整 CPU 集成使用 tests/install_server_check.py 的 --native-controller-image sha256:不可变镜像。控制器连接独立的进程模式 Node；控制器无 Docker socket，受信 Node 沿用原容器管理权限，任务为受限 UID。这不是 native Node 或真实宿主验收，管理员测试入口也不向个人 Agent 开放。

配置、日志、数据库和 private-controller-evidence.tar.gz 留在私有 runtime。公开仅提供代码、文档和精选摘要。
