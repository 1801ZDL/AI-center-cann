# 用户预配：当前阶段与交付条件

当前完成**中央待预配身份**和**已安装 Node 上的底层 UID 配额槽追加**，尚未接通完整开户。中央归档组配额、Node 用户映射、缓存绑定和个人凭据交付仍需协调。`workspace-add-apply`返回`node_and_central_owner_binding_still_required=true`，不得据此启用账号或交付可用凭据。

## 中央待预配身份

在选定中央的管理端使用 `accessctl user-stage --spec FILE`。请求文件必须是运行管理命令的服务 UID 所有、仅本人可读的普通文件；通过已安装的 `servicectl admin` 执行时，它会降到中央服务 UID，文件及父目录也须允许该 UID 读取。此步骤不创建 Linux 账号。

```json
{"user":"researcher_a","environments":["worker-python"],"quota":{"cpu":1,"memory_mib":512,"npu":0,"max_running":1}}
```

```bash
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- \
  accessctl user-stage --spec /var/lib/ascend-lab-controller/new-owner.json
/opt/ascend-lab-controller/current/servicectl admin \
  --prefix /opt/ascend-lab-controller -- accessctl user-status researcher_a
```

同一身份、目录、环境及额度重复登记返回相同 `stage_id`，不同定义拒绝。数据库一次提交禁用身份和不可用密码标记；不生成密码、令牌或 `client.json`。当前只允许受管 `worker-python` 环境；中央额度是申报及授权上限，不代表已经分配了节点 UID、存储或 NPU。

管理侧可为待预配身份登记项目角色和 Node 用户映射，用户仍不能认证。普通 `user-enable`、`token-renew`、额度修改或环境追加不能跳过预配阶段；未知或不完整的预配元数据也拒绝登录。当前还没有完成多层回执校验后的激活命令，不要手工删标记或改数据库启用。历史 `user-add` 继续保留原有测试账号行为，不是统一开户流程。

离线恢复到新状态目录会重新计算待预配身份的定义摘要，账号仍保持禁用；旧预配计划须重新核对，不能沿用旧路径。存在待预配元数据时，代码切换拒绝降级到不认识该状态的旧身份实现。普通旧账号及其禁用/启用行为保持兼容。

## 当前可用的存储步骤

操作要求 Root、匹配的已安装 Node 代码、已登记的专用 ext4 UID quota 文件系统。先结束并核对全部执行尝试、产物回执与缓存租约，再停止选定 Node。运行中的服务持有共享状态锁，管理操作获取不到独占锁时拒绝；未知/未清理任务不会被强制取消或遗忘。

准备 Root 私有的新增 UID 请求，只列新分配，不重复原 UID。示例额度须根据实际磁盘及任务需求替换：

```json
{"users":[{"uid":23004,"bytes":134217728,"inodes":8192}]}
```

```bash
/opt/ascend-lab-node/current/servicectl workspace-add-plan \
  --prefix /opt/ascend-lab-node --root /srv/ascend-lab-work \
  --limits /root/new-task-uids.json --output /root/uid-add-plan.json
/opt/ascend-lab-node/current/servicectl workspace-add-apply \
  --prefix /opt/ascend-lab-node --plan /root/uid-add-plan.json
```

计划绑定安装版本、Node ID、完整配置摘要、控制目录设备/inode、原文件系统标记及新增槽位身份。实际字节/inode总额仍留元数据余量，另检查物理空闲量。已有数据或限额的未登记 UID 不自动接管；原 UID、路径、槽位、限额和数据不得被计划修改。

应用先在控制盘写持久日志，随后在配额盘写同一计划的 intent，再设置新 UID 配额、创建 Root 私有空槽、发布标记和回执。中断后保留原计划并重复同一`workspace-add-apply`，不要删除日志、改配额或重新准备整个盘。日志未完成时，普通 Node 验证、启动、备份及代码切换被阻断。其他计划、已占用路径、改变的 Node 配置或过期文件系统绑定会被拒绝；不能通过重试另一个计划绕过。

这是原生存储预配：不创建宿主账号、不格式化/挂载系统盘、不改变 Node 登记或配置、不增加任务可见设备。返回的新 policy 还需要后续受保护的绑定流程，不能直接覆盖运行中的配置；私有缓存 policy 改变还涉及已有绑定的迁移，不能清空其他用户缓存来代替。

## 已验证范围

自有 256MiB 文件后备 ext4、实际安装的 Node 代码、数值 UID 进程完成：安装后 CLI 计划/应用、服务共享锁拒绝、7 个命名持久化边界的 SIGKILL及原计划恢复、旧 UID 数据/限额保持、新 UID 写满触发 EDQUOT、完成后带数据重复应用、过期计划和已有路径/限额拒绝。自有容器、卷和 loop 关联已清理。该 Node 配置使用测试注册信息且未连接中央；不证明开户、真实宿主账号、两机或掉电恢复。

以真实的前一公开代码树`34d33c4`重建旧包，完成代码回滚与再升级并保留原数据/配置。历史服务包可缺少新的管理附加模块，当前bundle仍必须包含它；新 UID 管理要求运行代码与目标已安装模块匹配，不能直接用新管理工具修改缺少恢复保护的旧 Node。这是相邻版本兼容证据，不承诺任意历史版本可互换。

## 整体开户仍需完成

1. 已有中央不可登录的待预配身份、定义摘要及项目预登记；仍须与后续各层操作组合。
2. 追加中央 owner 的存储 GID/硬配额，并协调各 Node 的 UID/GID及工作/缓存槽位；不修改已有用户分配。
3. 在中央登记与 Node 私有配置间进行有修订、有回执、可恢复的绑定；保留已有缓存和环境资格边界。实际两机接入不要求中央持有个人 SSH 或模型密钥。
4. 校验各层就绪后再启用身份和交付个人客户端凭据；中断不能留下已启用但无可用资源的账号。

当前阶段不会把底层槽追加成功当成这四步已完成。最新验收与缺口见 [PROGRESS.md](PROGRESS.md)。
