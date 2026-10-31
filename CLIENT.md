# 个人 CLI / Agent 接入

新客户端声明`experiment-budget-client-v1`，可由个人账号批准累计任务数及CPU/NPU申报秒数，再签发单预算Agent profile。多个固定MCP会话共用中央计费，重连不重复扣费；原个人密码不交给Agent。命令和当前边界见[EXPERIMENT_BUDGET.md](EXPERIMENT_BUDGET.md)。

个人MCP `queue_status.audit_retention`及网页显示中央认证审计积压；blocks_new_work=true时保留原任务并等待管理员归档。旧中央缺字段为null，旧配置可显示rolling_rows；客户端无需Root权限，也不提供裁剪能力。详情见[AUDIT.md](AUDIT.md)。

## 控制盘余量

新版个人MCP的`queue_status`包含中央的`control_storage`状态，不暴露其他用户任务。`blocks_new_work=true`时暂停追加实验，查询原任务并等待容量恢复；503不表示旧任务没有提交，不得换幂等键重跑。旧中央未声明时该字段可能为null。已有任务回执、日志、取消和结果恢复继续使用原接口。配置及保护范围见[STORAGE.md](STORAGE.md)；个人客户端不需要安装控制盘管理模块或获得Root权限。

## 离线算子报告的只读接入

客户端新增 `operator-case job-ID`，读取已归档的 `results/operator-analysis.json`；普通日志诊断命令保持不变。`operator_profile.py mcp --profile 本人配置 --job-id 分析任务ID` 提供无参数 `get_diagnostic_case`，只读绑定任务，不暴露上游 SQL、采集、提交或任意路径。读取只需本人/项目的 jobs:read 授权，不需要安装分析工具依赖。工具环境、输入格式、CPU提交及当前范围见 [OPERATOR_ANALYSIS.md](OPERATOR_ANALYSIS.md)。个人模型密钥和对话仍由各自harness管理。


## 提交前预览（2026-10-31）

在原提交命令增加 `--preview`，读取当前可见节点及阻碍，不入队、不占用资源：

```bash
./labctl --profile /本人/private/client.json submit --preview --template cpu_demo
./labctl --profile /本人/private/client.json submit --preview --workflow /本人/request.json
```

移除 `--preview` 后才正式提交。预览不保存个人归档提交上下文，不能与 `--archive`、`--archive-root`、`--local-source` 组合。预览退出0只表示读取成功，须查看结果 `status`；它不保证稍后立即运行，不检查正式归档预留是否成功。输入上传/封存及工作流请求仍由本人事先准备。

调度 MCP 新增只读 `preview_job`，参数与本会话 `submit_job` 相同（含稳定 request_key），命令、环境、输入、资源及锁继续受固定会话约束。它不写 submitted-request、不消耗单任务名额、不修改会话已提交任务；预览后提交时沿用同一参数。网页“托管作业”表单现在也提供“检查可运行节点”，与实际提交独立。旧服务缺接口时明确报错，不回退为真实提交。


需要 Python 3.10+，仅使用标准库。客户端不需要中央服务的数据目录、Docker 权限或其他人的 Agent 配置。

在源码目录执行一次安装；目标必须是新目录，不修改 shell、系统 Python 或已有安装：

```bash
./install-client --prefix "$HOME/.local/ascend-lab-client"
```

管理员通过私有通道交付个人 token。把下面的占位值替换为本人的凭据，保存到自己的配置目录，并设置文件权限 600、目录权限 700。不要提交到仓库或聊天记录中。

```json
{
  "controller": "https://lab.example.org",
  "token": "REPLACE_WITH_YOUR_PERSONAL_TOKEN",
  "default_environment": "worker-python"
}
```

HTTPS 验证系统信任链；自建 CA 可增加 `ca_file` 绝对路径，或使用与client.json同目录的相对路径（如controller-ca.pem）。相对路径按配置文件所在目录解析，整体迁移目录即可；不依赖启动工作目录，不允许向上遍历。不支持跳过证书验证。SSH 隧道可用 `http://127.0.0.1:18765` 或 IPv6 loopback；非 loopback HTTP 和 HTTP 重定向会被拒绝，避免转发认证头。客户端默认不经过环境变量中的代理。旧 Demo 的 host/port 文件兼容，但不需要复制中央管理员配置。token 失效或撤销返回 HTTP 401，由管理员续发个人 token，不会退回管理员身份。

```bash
CLIENT="$HOME/.local/ascend-lab-client/labctl"
PROFILE="$HOME/.config/ascend-lab/client.json"
"$CLIENT" --profile "$PROFILE" me
"$CLIENT" --profile "$PROFILE" nodes
"$CLIENT" --profile "$PROFILE" submit --template cpu_demo --seconds 2 --key my-first-job
"$CLIENT" --profile "$PROFILE" jobs
"$CLIENT" --profile "$PROFILE" logs job-REPLACE_WITH_REAL_ID
```

作业编号以 `job-` 开头；日志示例需替换成提交返回的真实编号。同一个请求重试要复用同一 key，避免重复执行。`wait` 退出不取消任务；需要取消时使用 `cancel`。

其他 harness 接入同一个 stdio MCP。启动命令与参数数组如下，把路径改成本人的绝对路径：

```json
[
  "/usr/bin/python3", "USER_CLIENT_DIR/mcp_bridge.py",
  "--profile", "USER_CONFIG_DIR/client.json",
  "--session-dir", "USER_PRIVATE_STATE/conversation-001",
  "--environment", "worker-python", "--seconds", "2"
]
```

每个对话使用不同的私有 session-dir。目录首次创建为 700；重连同一目录恢复已提交作业，不能更换身份/环境，另一个 MCP 进程也不能同时占用该目录。MCP 启动通过 `/api/me` 和 `/api/jobs` 验证个人身份/环境，不读取中央身份库。

未传 --workflow 时只开放一个 1–10 秒、1 CPU / 256 MiB / NPU=0 的 cpu_demo。v0.7 也可增加 `--workflow /本人/固定请求.json`，绑定一个已上传输入、由 API 验证的版本化工作流。仍为队列、提交、查询、等待、日志、结果、取消七个工具；模型不能更换输入、argv、资源、环境锁或模板。命令在受限任务容器中执行，最终权限由中央 API 校验。模型和模型密钥由各自 harness 管理，本客户端不带任何供应商凭据，也不会读取 zdl。

v0.7 CLI 新增 `input-add 路径`、`input 输入编号`、`submit --workflow 请求.json`、`environment-lock 作业编号 --output 新文件`；不依赖中央私有目录。完整工作流合同与当前限制见同目录 WORKFLOWS.md。独立客户端可从控制器受保护证据生成环境锁。

网页也可导入这份工作流JSON进行预览和显式提交，须明确worker-python及idempotency_key。网页保持原键、参数和依赖，不使用普通表单覆盖它；刷新/响应丢失后导入原文件可复用原任务。上传源码和本地自动归档仍用个人客户端，见WORKFLOWS.md。

## 个人自动归档（v0.11）

```bash
# input-add在个人profile旁记录本地来源，不把本地路径发送给中央。
"$CLIENT" --profile "$PROFILE" input-add /本人/项目/输入目录
# 将返回的input ID填入已有工作流请求，再提交；等待结束后自动归档。
"$CLIENT" --profile "$PROFILE" submit --workflow /本人/请求.json --key experiment-001 --archive
# 已提交任务可断线重连；下载失败重跑archive，不重新提交计算任务。
"$CLIENT" --profile "$PROFILE" wait job-REPLACE_WITH_REAL_ID --seconds 120 --archive
"$CLIENT" --profile "$PROFILE" archive job-REPLACE_WITH_REAL_ID
```

目录优先级：本次`--archive-root`、profile的`archive_root`偏好、明确`--local-source`、提交时记录的输入来源，最后为当前目录。每个任务保存在所选根目录的`job-ID/`下；默认根目录是本地运行文件父目录或输入目录内的`.ascend-lab-results/`。本地来源由同一profile执行的`input-add`记录；一个内容包来自多个本地位置时复用首次记录，可用`--local-source /本人/脚本.py`明确指定。无法推断远程Docker或其他机器上的原始脚本路径，不把中央返回的cwd当作本地目标。

```bash
"$CLIENT" --profile "$PROFILE" archive-config --root /本人/实验归档
"$CLIENT" --profile "$PROFILE" archive-config --sibling
"$CLIENT" --profile "$PROFILE" archive job-REPLACE_WITH_REAL_ID --local-source /本人/脚本.py
```

个人偏好保存在原profile，输入/任务映射保存在其旁的`文件名.state/`（700），不放在共享安装目录。任务目录700、文件600；拒绝链接路径和硬链接，不覆盖内容不同的已有文件。每256KiB下载并落盘，断线保留`.lab-partial`，续传后核对完整SHA256再发布文件；`.lab-complete.json`仅在全部文件校验后生成。目录或文件内容冲突时保留原件，选择新归档根目录或自行检查冲突文件后重试。该校验用于传输完整性，不证明算子数值正确。

失败/取消/超时任务也能归档实际保留的日志和证据。运行中或needs_attention不产生完整归档；`wait`返回3表示执行状态需核对，不是任务失败，不应重提。等待期结束返回2且任务继续管理。`submit --archive`的默认等待时间120秒，可设置`--wait-seconds`；归档或客户端退出都不会释放仍在运行的资源。

MCP启动可加`--archive --archive-root /本人/归档`，或`--archive --local-source /本人/脚本.py`。归档路径在会话创建时固定；`get_result`同时下载已确认终态的产物。模型没有路径参数，无法更换归档目录；重连须沿用原参数。它不是后台下载服务：如果harness在任务结束前关闭，需要重连调用`get_result`或用CLI归档。

当前下载支持2000文件、64MiB/文件、160MiB总量（包括中央元数据）；任务输出本身仍受128MiB上限约束。没有扩大wheel/源码额度或提供磁盘硬配额。CLI/MCP归档需要中央v0.11新增接口；老中央返回404时不回退读取服务器目录。

个人隐私依赖独立 Linux UID/容器及文件权限。仅切换工作目录不能隔离同 UID 进程、root 或 Docker 管理员。中央网页的任务可见性由账号与项目权限控制；此处 MCP 进一步只展示本会话创建的作业。
# 大文件客户端（v0.13）

新版独立客户端从/api/me读取管理员IO上限与256KiB分块能力，支持按任务冻结额度下载大产物。工作流parameters.artifacts可声明file_mib/total_mib，仍需中央和节点授权及个人存储配额；不是用户自行扩大服务器上限。浏览器产物下载支持流式HTTP Range。默认64MiB单文件/128MiB输出不变，大wheel额度需要明确配置；任务空间、磁盘配额和GiB级备份仍是独立要求，详见STORAGE.md。
# v0.20 留存管理

独立客户端提供 `labctl storage list`、`storage plan input|archive ID trash|restore|purge|resume`、`storage apply --plan JSON文件`。管理 token 需显式 storage:manage，原任务 token 不自动扩权。先预览具体对象、再应用固定计划；默认 7 天后才允许永久删除，隔离仍计费，历史结果与审计保留。没有自动 GC 或个人目录清理。完整语义见 STORAGE.md。

# v0.22 只读诊断材料

`0.22-client` 新增 `diagnose`，通过现有任务 ACL 获取 `ascend-lab-diagnostic-case/v1`，不读中央磁盘，不调用模型，也不运行诊断命令：

```bash
"$CLIENT" --profile "$PROFILE" diagnose job-替换为真实编号
/usr/bin/python3 /本人/ascend-lab-client/diagnostic_case.py \
  --profile /本人/private/client.json --job-id job-替换为真实编号
```

第二个命令是 stdio MCP，供任意支持 MCP 的 harness 使用。只暴露无参数的 `get_diagnostic_case`，会话在启动时固定一个任务；不能传入其他 job ID、文件路径或命令，没有提交/取消工具。中央仍按本人/项目授权核对每次读取；需要的是任务读取权限，不要求提交权限。个人模型凭据继续保存在个人 harness，MCP 不接触模型配置。

材料包含执行状态、stdout/stderr 各最后 8192 字符，以及固定白名单中的环境/结果/编译 provenance 片段。默认原始报告 `results/workload-result.json` 也提供预览，保留逐项检查/误差、shape/dtype 或原始性能样本等报告已有内容；不从汇总数值猜测缺失字段。自定义结果文件仍可通过个人归档读取。单文件最多 8/16 KiB、总响应最多 160 KiB；分块核对身份和 SHA，不扫描大 wheel，不遍历 IR 目录。每条片段都有证据 ID、预览 SHA、截断或不可用标记，整份快照另有 case SHA。已知个人凭据和常见认证串会遮盖；这不是任意日志的完备秘密检测器，日志本身仍应避免写入凭据。

运行中/状态不明的任务也可读取现有日志，`terminal=false`，不会创建替代任务或释放资源。内容是待分析数据，不能作为工具执行指令；任务退出、环境匹配、精度正确、性能提升分别核对。这个入口收集证据，不自行判断根因或修复；后续复现、编译、精度和性能实验继续通过调度 MCP 提交。


`0.31-client`在固定工作流MCP会话的queue_status中回显预先批准的持久缓存要求；提交仍使用原固定请求，模型不能覆盖缓存政策、路径或任务命令。工作流JSON的cache字段由中央校验，旧服务器会拒绝不支持的字段。

`0.32-client`允许工作流JSON携带depends_on和artifact_inputs；固定MCP会话会显示这些约束并拒绝临时覆盖。`retry-inputs job-ID`只请求服务端继续原冻结交接，不在个人机器读取或复制中央文件。

固定离线报告的可选配方见OPERATOR_ANALYSIS.md：独立客户端包含operator_workflow.py及operator-recipe-v1能力，可由封存input ID和已确认环境锁生成请求，不自动提交。environment-lock现保留machine事实，便于架构准入。

新增compiler-case读取固定编译报告和完整小IR；compiler_diagnostic.py可作为单任务只读MCP。客户端附带compiler_compatibility.py用于封存的CPU ir_v1复现，读取不会调用模型或执行候选。证据绑定与限制见COMPILER_DIAGNOSTICS.md。
