# 开发与验证

使用 Python 3.10+ 与 Node.js，在仓库根运行 `bash scripts/check.sh`；可用 `bash scripts/check.sh /usr/bin/python3` 指定与服务安装器相同的系统解释器。记录实际 Python 版本和跳过项，自定义 Python 缺少 pidfd 时不能将相关跳过项当作通过。单元检查不调用模型、不运行 NPU、不创建宿主账号或安装系统服务。

`runtime/`、本机 `AGENTS.md`、模型/会话目录、第三方二进制、原始部署证据和测试盘不进入版本控制。公开源文件由 `scripts/publication_manifest.py` 显式选择；发布清单记录摘要和文件模式，不包含凭据值。原开发工作区可加 `--audit-local-private`，与本项目已知账号/节点测试凭据比对，不读取模型配置。

`tests/live_*`、`tests/package_*` 及 Root 安装/大构建脚本有环境和历史材料要求。执行前阅读脚本与 [DEPLOYMENT.md](DEPLOYMENT.md)，通过有预算的队列运行自有隔离对象，核对实际任务身份；不操作其他人的容器、共享安装或驱动。

任务提交响应丢失后使用原幂等键查询；观测超时或 `needs_attention` 不是终态，不重新提交未知任务。取消、归档和清理都有独立确认，确认之前保留资源。保存真实失败，不修改报告使其看起来通过。

每次可复核进展更新 [PROGRESS.md](PROGRESS.md)，说明变更、验证范围和局限。对受保护运行时的修改需重新验收，不继承旧包的成功结论。公开摘要应明确是历史记录、新运行还是仅准备阶段。

推送到 `1801ZDL/AI-center-cann` 前检查远端 HEAD、源文件清单和凭据扫描结果；只做普通快进更新，遇到他人变更先审查合并，不强推。GitHub 登录由调用者已有授权提供；不把 GitHub token 或模型密钥写入脚本、URL、日志或仓库。
