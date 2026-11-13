# 离线网页展示包

本包沿用 `web/` 中的网页布局与渲染逻辑，用完全合成的数据提供离线演示。
不读取中央配置、登录凭据、数据库、个人会话或设备信息，不需要服务器和网络。

## 打开与展示

解压交付的 ZIP 后，双击 `index.html`；也可只复制这个 HTML 文件。
HTML 内嵌样式、脚本和数据，没有 CDN、外部字体或需要运行的安装程序。
可使用较新的 Chrome、Edge 或 Firefox；本轮实际验证使用项目已有 Chromium。

建议顺序：运行总览 → 托管作业及任务详情 → 任务结果及类型筛选 → 进程观察 → 容器环境 → 实验记录。
顶部可切换“日常协作”“资源紧张”“异常待核对”三个合成场景。
结果 CSV、快照 JSON 和示例产物可由浏览器本地导出。

所有资源、日志、用户和测量结果均为合成示例，固定展示时间为
2026-11-13 14:00（Asia/Shanghai）。界面中的运行、成功、硬限额等状态只是场景数据，
不代表真实任务执行、硬件资格、生产验收或算子性能收益。
提交、取消、清理、设备控制和后台任务均不执行；没有真实 IR、wheel 或 profiler 数据。

## 生成

在源码仓库根目录执行：

```bash
python3 scripts/build-offline-demo.py --output runtime/offline-demo-20261113
```

输出目录和相邻 ZIP 均须不存在，避免覆盖已有交付。
生成只使用标准库，读取三个公开网页文件，写入约 150 KB 的展示内容，
不会启动服务、模型、Docker 或设备探测。生成目录包含：

```text
index.html       双击打开的独立网页
README.md        使用说明、展示顺序和能力边界
manifest.json    HTML 与 README 的字节数和 SHA256
```

## 验证范围

2026-11-13 使用一个浏览器、一个标签页、单 CPU、串行检查及 45 秒外层截止，
在浏览器禁网状态下完成 7 组检查：file URL 直接打开、任务和日志、结果筛选与 CSV 导出、
进程/容器/实验页面、三种场景、写操作拒绝、390px 布局，以及无 HTTP/WebSocket 请求或脚本错误。
检查不访问运行中的中央服务；并非跨浏览器兼容性、真实服务器或设备验收。

已有可选浏览器依赖时，可复核新生成的包：

```bash
timeout --signal=TERM --kill-after=3s 45s bash scripts/check-browser.sh \
  tests/browser_offline_demo_check.py runtime/offline-demo-20261113/index.html \
  --output runtime/offline-demo-20261113-qa
```

验证目录须不存在；其中保存检查报告、桌面/手机截图及一份导出 CSV。
浏览器依赖由项目现有私有 QA 环境提供，交付给演示使用者的 ZIP 不包含这些依赖。
不要用完整浏览器回归、真实任务提交或资源探测代替这项有限检查。
