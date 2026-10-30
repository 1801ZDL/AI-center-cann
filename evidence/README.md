# 本地原始证据

技术文档中的 `evidence/*.json` 是开发服务器保留的原始验收文件引用，不随公开源码上传。它们可能包含本机路径、容器/节点身份和部署拓扑；私有状态、凭据、数据库、会话及测试盘始终排除。

公开范围与局限见 [PROGRESS.md](../PROGRESS.md) 和 [reports/v036-public.json](../reports/v036-public.json)。公开摘要是历史结果的整理，不能在新机器上替代实际验收。`tests/test_*.py` 为可移植单元检查；`tests/live_*`、`tests/package_*` 与安装验收脚本部分依赖本地历史材料和管理员准备的环境，不应直接作为公共 CI 运行。
