# 离线算子性能诊断能力

当前接入两个可独立复用的上游组件：`msprof-mcp 0.1.8` 的 CSV 算子查询工具，以及固定 msagent 提交中的 `delta_report.py`。分析程序作为中央队列的 CPU 任务运行，JSON/CSV 报告归档后，个人 harness 通过只读 MCP 获取绑定任务的报告。个人客户端不需要安装 pandas、Perfetto、MCP SDK 或完整 msagent。

这是历史 profiling 数据的分析入口。新增固定工具镜像与个人请求配方，复用既有build_v1的受控CPU产物合同；执行对象是离线报告，不是编译任务或第六种设备工作流。当前验收状态以PROGRESS.md为准。先前生产者沿个人Main的协作式host command队列运行，其证据继续保留原范围。CLI 中的任务目录检查用于避免误操作，不能代替管理员设备权限/cgroup 隔离。测试状态见 [PROGRESS.md](PROGRESS.md)。

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


## 固定工具镜像与受控 Node 配方

`operator_workflow.py` 生成固定入口请求，复用既有 `build_v1` 的封存输入、环境锁、CPU预算和结构化产物机制。任务名称及结构化结果明确标记 `offline_operator_report_generation`；类型沿用build是现有CPU产物合同，不表示执行编译。请求固定为1 CPU、768MiB内存、0 SHM、0 NPU、180秒；只读输入、至少16MiB/256inode的持久工作区，单文件2MiB/总4MiB归档预算。Node和物理域还须容纳128MiB托管开销，实际调度继续按原准入和资源账本进行。

镜像中 `/opt/ascend-lab-operator/app/operator_node.py` 是固定执行入口，按既有bootstrap分配的非root UID运行，检查零能力及NoNewPrivs；仅使用 `/inputs` 和 `/work/results`，不接收命令、目录或设备覆盖。上游MCP子进程和比较脚本使用Python `-I -B`隔离模式，避免输入目录中的同名模块影响工具加载。个人客户端只负责生成请求和读取报告；配方校验不代替服务端身份授权、受限容器和镜像环境资格，也不限制管理员另行授权的其他build_v1程序。

准备镜像时先下载明确锁定的44个wheel，再生成仅含四个应用源文件、锁和wheel的独立上下文：

```bash
/本人/python311/bin/python -m pip --isolated download --no-cache-dir \
  --index-url https://pypi.org/simple --only-binary=:all: --require-hashes \
  -r examples/operator_tools/requirements-linux-x86_64-py311.txt \
  --dest /本人/private/operator-wheels
python3 scripts/prepare_operator_image.py \
  --wheels /本人/private/operator-wheels --output /本人/private/operator-context
```

准备脚本核对完整wheel集合及每个哈希，拒额外文件、链接、缺件、重复和超预算，不读取凭据，也不下载、调用Docker或安装服务。现有上下文不覆盖。锁限定CPython3.11/Linux x86_64；其他平台需独立锁和实测。镜像应用目录不包含模型配置、MCP凭据、会话或原始输入。

管理员在自己的构建环境使用固定基础镜像ID构建：

```bash
docker build --pull=false --network=none \
  --build-arg BASE_IMAGE=sha256:管理员核对过的64位镜像ID \
  --build-arg BASE_PYTHON=/基础镜像中/python3.11的绝对路径 \
  --tag 管理员自己的镜像名称 /本人/private/operator-context
```

管理员还须按构建器版本配置CPU/内存限制；`--network=none`作用于构建RUN，不代表解析未准备的基础镜像不会访问镜像仓库，因此基础镜像须预先具备。本机验收复用已有自有Python3.11.15/glibc2.39工具链镜像，离线安装wheel；没有拉取或修改其他人的镜像。部署任务时必须登记最终 `sha256:...` ID，不依赖可变tag。

分析Node的Python配置为 `/opt/ascend-lab-operator/venv/bin/python`，镜像应满足glibc>=2.34。可使用独立CPU分析profile，和其他profile在同一物理域记账；不必替换现有开发容器或给个人agent Docker socket。新增/修改image、Python、环境配置时，按ENVIRONMENTS.md先停止相关Node、运行固定environment_probe并由管理员确认，再同步合同、启动并等待新鲜心跳；不热改已认证Node或放宽环境门。

固定探针需要查询PACKAGES中的五个包和四个应用文件/两个上游文件。可用标准库生成请求（该步骤不提交）：

```bash
python3 - <<'PYQUERY'
import json
from operator_workflow import probe_queries
value={"template":"environment_probe","environment":"worker-python",
       "parameters":probe_queries("worker-替换为已登记的12位节点ID")}
with open("operator-probe.json","x") as target:json.dump(value,target)
PYQUERY
```

提交固定probe、管理员确认环境并观察Node就绪后，个人端执行：

```bash
./labctl --profile /本人/private/client.json environment-lock job-成功探针ID --output operator-lock.json
./labctl --profile /本人/private/client.json input-add /本人/private/profile-case
python3 /本人/client/operator_workflow.py \
  --input-id input-返回的封存ID --lock operator-lock.json \
  --idempotency-key 本人本次操作唯一键 --output operator-request.json
./labctl --profile /本人/private/client.json submit --workflow operator-request.json
```

生成请求不会提交任务或联系中央；已有输出文件不覆盖。配方要求锁包含固定镜像ID、明确x86_64/CPython3.11事实、精确包版本和当前客户端对应的镜像应用/上游代码SHA。`labctl environment-lock`现同时导出machine；旧锁缺该字段时应从原受保护证据重新导出。客户端版本对应的应用源码变化时，要明确重建/认证镜像，不能静默沿用旧代码。

后续仍按原job/attempt观察和下载，不因等待超时重新提交。读取结果使用原 `operator-case` 或单任务只读MCP；追加采集、复现、编译或设备实验仍分别申请资源。
