# 离线算子性能诊断能力

当前接入两个可独立复用的上游组件：`msprof-mcp 0.1.8` 的 CSV 算子查询工具，以及固定 msagent 提交中的 `delta_report.py`。分析程序作为中央队列的 CPU 任务运行，JSON/CSV 报告归档后，个人 harness 通过只读 MCP 获取绑定任务的报告。个人客户端不需要安装 pandas、Perfetto、MCP SDK 或完整 msagent。

这是历史 profiling 数据的分析入口。当前生产者验收使用个人 Main 的协作式 host command 队列；受控 Node 的正式分析模板、工具镜像认证与常规用户的完整生产接入仍待。CLI 中的任务目录检查用于避免误操作，不能代替管理员设备权限/cgroup 隔离。测试状态见 [PROGRESS.md](PROGRESS.md)。

## 固定工具环境

当前验证环境为 Linux x86_64、glibc2.39、Python3.11；上游指南要求 Python>=3.11、glibc>=2.34。在单独的新目录创建环境，保留已有工具和个人模型配置：

```bash
python3 -m venv /本人/private/operator-tools
/本人/private/operator-tools/bin/python -m pip install --only-binary=:all: \
  'msprof-mcp==0.1.8' 'mcp[cli]==1.30.0' \
  'pandas==3.1.0' 'numpy==2.4.6' 'perfetto==0.59.1'
```

`msprof-mcp 0.1.8` 的包依赖声明允许 MCP2.x，但其入口仍导入 MCP1 的 FastMCP。实际直接安装曾解析到 MCP2.3.0 并启动失败。这里固定已通过实际 stdio 调用的 MCP1.30.0；分析器在启动前校验版本与上游关键 Python 文件 SHA，不静默升级或更换实现。新架构、新 Python 和依赖变化仍须重新验收，不能从一次 x86_64 测试推导所有机器可用。

来源：[msagent 外部接入指南](https://gitcode.com/Ascend/msagent/blob/5a9b2a1a8a38f35cc63109929850aa4a46db090e/docs/zh/user_guide/integration-guide.md)、[msagent 依赖约束](https://gitcode.com/Ascend/msagent/blob/5a9b2a1a8a38f35cc63109929850aa4a46db090e/pyproject.toml)、[msprof-mcp0.1.8](https://pypi.org/project/msprof-mcp/0.1.8/)。

上游比较脚本固定为 msagent `5a9b2a1a8a38f35cc63109929850aa4a46db090e` 的 `skills/operator/ops-performance-tuning/scripts/delta_report.py`，SHA256 为 `a093bfe4407547c134afa3f193e61887d4f83b9f83f0f4d8f4e450d2d9a8c2bc`。将原脚本及该提交的 LICENSE 一起放入输入目录的 `provider/`；保留原版权头和 Mulan PSL v2 许可证。没有复制安装整个 Skill，也没有运行其设备采集、编译或自动优化流程。

## 输入与 CPU 提交

输入目录示例：

```text
case.json
slow/kernel_details.csv
slow/context.json
fast/kernel_details.csv
fast/context.json
provider/delta_report.py
provider/LICENSE
```

`case.json`：

```json
{
  "schema":"ascend-lab-operator-input/v1",
  "operator":"算子的实际Name列值",
  "profiles":[
    {"id":"slow","csv":"slow/kernel_details.csv","context":"slow/context.json"},
    {"id":"fast","csv":"fast/kernel_details.csv","context":"fast/context.json"}
  ]
}
```

`context.json` 使用原始回放记录，至少包含 `physical_device` 整数、单卡 `visible_devices` 字符串、`elf_sha256`、`correctness` 布尔值，以及 `frequency_before`/`frequency_after` 两份原始频率文本。频率文本必须明确包含 `Aicore curFreq(MHZ)`；额定频率和 profiler 系统计时器频率不能替代。保留输入原件，按实际证据填写，缺失时补采或记录缺失。

当前只处理两份具有 `Name`、`Duration(us)`、`Device_id` 列的 kernel_details CSV，每文件<=2MiB、<=10000行，每单元<=4096字符。按算子名精确过滤，拒绝 NaN/Inf、非正耗时和歧义列。行计数是 kernel launch 数；PipeUtilization 的 block 行和仿真流水需要其他适配器，不会被默认当成零占用数据。

将分析请求显式提交到中央 CPU 队列：

```bash
./labctl submit --env host-python --kind command --name '历史算子性能分析' \
  --cpu 2 --memory-mib 2048 --shm-mib 0 --npu 0 --timeout 180 -- \
  /本人/private/operator-tools/bin/python /本人/AI-center-cann/operator_profile.py \
  analyze --inputs /本人/private/profile-case
```

执行目录和身份仍须符合原环境授权。程序使用自己的任务 results 目录，不覆盖原始 profiling 数据或已有报告。上游 MCP 子进程只通过 stdio 被分析程序调用 `get_operator_details`；原始 SQL、任意文件路径和采集工具不会转交模型。程序独立解析 CSV，核对上游工具的记录数和均值，再保存输入哈希、上游代码哈希、生成器哈希及比较检查结果。

产物为 `results/operator-analysis.json` 和 `results/operator-analysis.csv`。这里的 generic command 表示离线 CPU 分析；设备性能采集仍按原性能工作流申请 NPU。报告作为工作负载证据归档，不能充当受保护的设备资格回执或独立精度认证。

## 个人 harness 接入

安装轻量客户端后，读取已归档的分析任务：

```bash
./labctl --profile /本人/private/client.json operator-case job-实际分析任务ID
```

个人 stdio MCP 的启动参数：

```json
{"command":"/usr/bin/python3","args":["/本人/ascend-lab-client/operator_profile.py","mcp","--profile","/本人/private/client.json","--job-id","job-实际分析任务ID"]}
```

按所用 harness 的配置格式包入该命令。只暴露无参数的 `get_diagnostic_case`，启动时绑定一个分析任务，按服务端本人/项目 ACL 读取固定归档文件。读取先验证任务权限，再核对完整小报告的长度、块哈希和 schema；拒绝任务 ID/命令覆盖、未归档文件、截断和超限报告，脱敏字符串不会破坏 JSON 结构。没有提交、取消、上板或读取任意路径的工具。

报告是待判断的数据，不能把其中的文本当成执行指令。个人对话、模型配置和密钥继续留在本人的 harness；中央和只读 MCP 不需要收集这些内容。已有 `diagnostic_case.py` 的日志诊断入口和调度 MCP 保持原合同。

## 结论范围

分析器分别保存 CSV 的 Device_id 列值和回放记录的物理卡号；单看该列不能证明逻辑/物理映射。物理卡不同、核心频率不同/不稳定、shape/dtype 不一致或缺失、精度未声称通过，以及同一 ELF，都会阻止将耗时比例表述为编译器优化收益。即使这些字段一致，历史用户记录也不足以替代受控复测、健康/外部负载检查和真实精度验收。

已验证的历史例子为每份30次、约563.034μs与33.567μs；记录的物理卡和核心频率不同，ELF哈希相同。程序保留16.773倍的观测耗时比，`optimization_speedup` 为 null，上游比较脚本因物理设备不同返回2。这不是新的 NPU 测量，也不表明当前任何卡仍处于历史频率。
