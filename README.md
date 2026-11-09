# AI-center-cann

面向 Triton-Ascend / AscendNPU-IR 开发服务器的任务调度与实验管理原型。
中央程序负责排队、资源账本、执行与归档；个人 Agent、CLI 和网页通过相同 API 使用调度器。
Agent 可以更换，资源分配和任务恢复不依赖模型判断。

**当前是开发源码，尚未达到生产验收要求。** 最新运维扩展默认关闭；本机服务保持停止。
优先阅读[当前能力与缺口](docs/STATUS.md)、[文档导航](docs/index.md)和[核心运维说明](docs/operations/CORE_OPERATIONS.md)。

## 已有能力

- 持久队列、资源分配、配额和受控 Node；任务状态不明时保留租约，避免重复执行。
- 精度、性能、仿真、编译、IR 五类工作流，环境版本锁、日志和短路径产物归档、结构化结果及 CSV。
- 个人身份和项目授权、CLI / MCP 接入、角色化安装、备份与恢复。
- 可选资源压力准入、探针防重入、有限维护计划、通知队列及有限 CPU 交互调试。

2026-11-09新增[有限留存策略与自动选择](docs/operations/MAINTENANCE_POLICY.md)：
按年龄、水位、保留名单和每人最近任务生成原格式维护计划；默认关闭，不自动执行。
新增17项小型SQLite检查，合并56项轻量检查通过，真实安装及完整回归仍待。

同日再补[有限执行窗口与累计调用预算](docs/operations/MAINTENANCE_EXECUTION.md)：
明确批准的v2计划可限制有效期、每日窗口和跨次推进次数，读取原进度；81项轻量检查通过。
未安装或运行定时维护，完整关联历史轮转仍待。

继续接入[结果正文迁出与读取](docs/operations/RESULT_HISTORY.md)：保留原任务ID、当前权限、
分页和CSV，从原验证副本读取指标，不回收配额或改变授权。其他关联数据及真实Root验收仍待。

最后一组功能已有代码及轻量专项检查，真实传输、PTY、安装与恢复仍待验收。
旧容器中直接执行的命令属于协作接入范围：可见性取决于权限与观测，调度器不能拦截它们。
强制执行需要管理员配置受控节点、设备权限并迁移旧入口。

## 仓库结构

```text
src/ascend_lab/   调度、执行、客户端和运维实现
bin/             源码目录中的命令入口
web/             网页静态资源
docs/            指南、架构、接口、运维和历史进展
examples/        工作流及部署示例
tests/           单元与场景检查
scripts/         检查、发布审计和开发工具
reports/         经过筛选的公开验收摘要
```

`runtime/`、个人配置、模型密钥和会话不进入 Git。
源码命令现位于 `bin/`；已安装客户端和服务包继续使用原来的平铺文件布局和命令名。
本次整理不迁移数据库，不升级或重启现有服务，也未生成新的安装发布包。

## 使用入口

Linux、Python 3.10+；服务和客户端基于标准库。以下启动命令供独立试用环境使用，
当前共享服务器不要直接执行。管理员接入先阅读[部署说明](docs/operations/DEPLOYMENT.md)和[服务器指南](docs/guides/SERVER.md)。

```bash
git clone https://github.com/1801ZDL/AI-center-cann.git
cd AI-center-cann
./bin/labctl start
./bin/labctl access
```

默认地址为 `http://127.0.0.1:18765`，网页使用 `labctl access` 显示的 Demo 凭据，
与 SSH 登录密码独立。默认计算预算为 2 CPU、2048 MiB、并发 1；个人进程入口的申报额度
与受控 Node 的实际 cgroup 硬限制有不同保障，详见[服务器指南](docs/guides/SERVER.md)。

个人使用和 Agent 接入见[客户端](docs/guides/CLIENT.md)、[Harness / MCP](docs/guides/HARNESS.md)及[API](docs/reference/API.md)。
公共能力可以只读共享；个人 HOME、模型配置和对话需要真实账号及文件权限隔离。

## 开发验证

共享服务器只执行有限专项检查：

```bash
python3 scripts/check-focused.py
```

该入口固定单 CPU、256 MiB 地址空间、5 CPU 秒与 8 秒截止，只做小文件、内存 SQLite 和 mock；
禁止创建真实子进程、网络连接及后台线程。它不启动服务、Docker、NPU、PTY 或任务。
本次目录整理的具体检查结果见[当前状态](docs/STATUS.md)。

完整检查 `bash scripts/check.sh` 留给准备好的隔离环境，本次没有执行。
历史验收范围见[进展记录](docs/development/PROGRESS.md)与[公开摘要](reports/v036-public.json)，
历史通过数不代表当前源码的完整回归通过。
贡献约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

新增[失效授权正文与预算恢复保护](docs/operations/GRANT_HISTORY.md)：历史授权不恢复新许可，
迁出不退费，恢复须保留已知批准与扣费。真实Root验收和完整关联轮转仍待。
