# 作业 API v0.7

有界报告摘要：`GET /api/jobs/JOB/diagnostic-report?kind=accuracy|compiler|simulation|operator`，需要原jobs:read和任务ACL。确认终态/清退及Node归档回执后，只核固定报告字节和清单SHA；源文件不在HTTP线程中重算。/api/me的report_view_capabilities声明archive-report-view-v1。返回409表示不完整/不一致，410沿归档生命周期，503表示两槽读取繁忙；没有提交或路径覆盖参数。详见[REPORT_VIEW.md](REPORT_VIEW.md)。

累计实验预算：`GET/POST /api/experiment-budgets`、`GET /api/experiment-budgets/<id>`及`POST .../<id>/close|token`。读取需jobs:read，创建/关闭/委托需jobs:submit且普通非预算个人凭据；仅本人可见。创建具备独立幂等键。预算凭据从认证派生experiment_id，省略请求字段仍绑定，指定其他预算403；新任务与计费同事务，超额409，原任务重试不重新计费。token接口返回一次私有token，不能记录或公开；CLI仅写新私有profile。详情见EXPERIMENT_BUDGET.md。原`GET /api/experiments`仍是历史实验观察，语义不变。

新增 `GET /api/audit-retention`（`jobs:read`），jobs摘要及MCP queue_status的`audit_retention`返回相同只读策略/积压。旧配置为rolling_rows；新管理员中央为archive_required，含retained_rows、maximum_unarchived_rows、status=ready|backlog|unavailable及blocks_new_work。积压/不可确认时新任务或输入返回503：`error=audit_retention_backlog,retryable=true,original_operation_required=true`；原任务ID、许可和结果恢复保留，不更换幂等键重跑。新claim无grant、未消费start等待，原grant恢复继续。接口不提供导出或裁剪权限，Root停机维护见[AUDIT.md](AUDIT.md)。control-monitor不查询该积压。

## 控制存储准入状态

`GET /api/control-storage`要求`jobs:read`，返回只读的`enabled,status,blocks_new_work,reason_code,free_bytes,free_inodes,minimum_free_bytes,minimum_free_inodes`和范围说明，不公开路径/设备身份。`GET /api/jobs`的`control_storage`字段返回同一类快照。状态为disabled/ready/pressure/unavailable；门槛是准入保护，非硬配额或预留保证。

启用原生控制盘后，状态额外包含 `filesystem_enforced=true,filesystem,filesystem_bytes,filesystem_inodes`；作业摘要里的 `policy.control_filesystem`也只公开类型和容量，不公开挂载路径、设备号或存储ID。外侧控制根不匹配时状态为unavailable并暂停新增工作，原服务的私有视图继续保存原任务状态。缺少这些字段不能推断已经启用原生模式，具体安装/恢复边界见[STORAGE.md](STORAGE.md)。

新任务、新输入及未提交输入写入在低余量时返回503：`error=control_storage_pressure,retryable=true,original_operation_required=true`，reason_code为control_storage_bytes/control_storage_inodes/control_storage_readonly/control_storage_unavailable。恢复容量后继续原幂等键、原input及原偏移，不据此重复计算。相同已提交分块和已有任务回执保持原身份。Node新claim无grant，未消费start返回wait；原grant恢复、心跳、结果上传及complete仍可继续。实际ENOSPC/EDQUOT仍可能返回原507；上述状态接口不能替代完成回执。

## 归档算子分析报告

个人客户端的 `operator_profile.py` 使用原有GET任务详情和固定artifact-chunk读取一个<=64KiB报告；没有新增执行型HTTP接口。先按服务端本人/项目ACL授权，再检查固定文件名、完整长度、SHA及schema，拒绝截断/超限/不一致的报告。其独立stdio MCP沿用无参数 `get_diagnostic_case`，启动时绑定job ID；不能提交/取消或覆盖任务/文件路径。报告属于用户工作负载证据，不授予设备资格或独立精度/性能认证。详见OPERATOR_ANALYSIS.md。


## 只读可运行性预览（2026-10-31）

`POST /api/jobs/preview` 接受与普通提交相同的请求，要求本人 `jobs:submit` 权限。返回当前可见节点的 `status`、`reason_code`、`reason`；托管节点另有 `capacity_fits` 和 `capacity_blockers`，只检查共同 CPU、负载内存加128MiB托管开销、SHM静态上限。这些容量字段不是完整环境匹配证明；镜像、工具链、设备、工作盘、暂存等继续复用实际 placement 判断。

顶层 `eligible_snapshot` 仅表示当前观测下未发现准入阻碍；`blocked` 表示当前候选有阻碍；`no_visible_node` 表示没有已映射给本人的节点。所有状态均无启动保证、无预计开始时间。至多输出128候选，计数覆盖全部本人候选，`truncated` 标记截断。未映射节点、他人任务/UID/目录、分配候选及凭据不输出。每节点只返回优先遇到的阻碍，不保证列出所有问题，也不把所有等待推断为临时忙碌。

预览使用只读账本事务，不创建 job/attempt、归档预留、排队保护、身份或输入交接，不领取节点、调用模型或执行任务。身份、采样和跨库信息不构成全局原子快照；正式提交的存储/维护/身份准入及领取/启动时仍须重新校验。依赖阶段需要提交后完成封存交接，预览不会提前准备。固定目标探针沿提交的静态目标规则拒绝非法目标；普通未绑定任务仍能在无现有匹配节点时正常入队，等新服务器或环境接入。旧 `POST /api/jobs/validate` 的返回合同保持不变。 预览不查询幂等键对应的旧任务；重试确认和已入队任务跟踪应读取原job ID，不能用预览替代。

网页“托管作业”复用同一预览接口及表单请求构造，预览和提交为独立按钮。修改表单/撤销提交权限/显式提交会失效旧结果并中断旧读取；15秒未完成的预览请求取消等待并显示可重试错误，成功结果15秒后标记历史。两种时间窗只涉及界面读取，不修改队列任务时限、租约或归档。预览失败不回退实际提交，当前blocked也不禁止用户显式提交等待未来条件。只读凭据的预览/提交按钮禁用，最终权限仍由服务端检查。网页另有五类工作流JSON导入/核对入口，保持请求原字段及明确的幂等键，详情见WORKFLOWS.md；没有前端执行或资源分配权限。


2026-10-30：旧非模板命令支持 `parameters.artifacts={file_mib,total_mib}`。非默认限额进入保存的请求、幂等指纹与归档预留；改变限额后重用同一键返回409。`parameters`必须为对象，旧命令只接受`artifacts`字段，错误结构返回400；模板仍使用各自原有验证器。省略参数、空对象和显式64/128MiB默认值保持旧默认请求的幂等兼容。

升级前曾被忽略的非默认限额不能追溯修改原任务：以保存的`artifact_limits`为准；升级后原键提交新的有效限额返回409，应查询原任务，不能自动换键重复计算。旧协作式执行在确认结束后检查`ir/profile/results`输出；超额时原件保留、输出不进入归档，成功命令改记`failed/failure_stage=artifact_budget`，日志和失败说明仍可下载。该检查不是文件系统硬配额；生产节点继续使用受控工作区和暂存合同。CLI与完整范围见[STORAGE.md](STORAGE.md)。

v0.30 Node摘要增加`spool_storage.enforced/filesystem/bytes/inodes/status/free_bytes/free_inodes`，不返回宿主路径或storage_id。作业`state=needs_attention`时可有`output_pending=true`，节点确认进程结束后另有`execution_ended=true`，表示保留原尝试等待产物恢复；不是新任务或重新执行许可。成功终态清除此暂存等待标记。配置见SPOOL.md。

v0.29中央载荷启用owner组配额时，`/api/me`及本人作业摘要的`storage.filesystem_quota`增加`byte_hard,bytes_used,inode_hard,inodes_used,enforced,shared_by`。这些是本人所有输入、归档和回收站合计的内核硬边界，不包含GID或其他用户映射。未预置owner的提交/上传返回409；实际限额或目录归属变化返回503且无目录回退；写入EDQUOT沿用HTTP507/原偏移续传。个人客户端无需新配置，完整范围见STORAGE.md。

v0.27：新增固定`npu_access_probe`，请求`environment=worker-python`，parameters仅含node_id/device_id/可选memory_mib；按本人额度申请1张指定卡，不初始化ACL。新普通设备工作流在资源预留前要求按节点/执行身份/卡及环境绑定的有效访问资格；未验收/失效/失败时保持排队。探针须显式提交，不由排队或定时器自动触发。`GET /api/nodes`新增device_access.counts汇总与device-files-only范围，不包含他人源任务、UID或回执；ACL/numerical标记仍为false。节点新资格协议、30分钟有效期、失败阻断、升级/恢复行为见[NPU.md](NPU.md)。个人CLI/MCP原五类工作流合同保持，未新增自动设备探测模型工具。

v0.19：原生HTTPS、可迁移CA配置和同CA证书续期见 [HTTPS.md](HTTPS.md)。默认本机HTTP不变；管理员可显式配置对外TLS监听。

v0.18：节点执行清单、当前中央实例确认与物理域对账阻断见 [RECOVERY.md](RECOVERY.md)。新安装默认启用；旧配置需更新全部启用节点后显式切换。

v0.17：中央环境认证与资源预留前的版本匹配见 [ENVIRONMENTS.md](ENVIRONMENTS.md)。新安装默认启用；旧配置需在空闲节点认证并确认后显式启用。

v0.16：GET /api/jobs的policy.require_pinned_docker公开中央是否要求固定Docker执行位置；新安装默认true。节点心跳可声明pinned-docker-runtime-v1，GET /api/nodes的受控节点新增runtime_pinned布尔值，不公开socket私有配置/daemon ID。要求开启但节点缺能力时，新claim返回409；已分配尝试重连保持原许可和资源。个人任务/MCP合同不变。身份检查失败通过既有attention回执进入needs_attention，不作为complete释放资源。

实例为 http://127.0.0.1:18765，除 /healthz 外都需要 Demo HTTP Basic 或 Bearer 认证，没有接入 SSH/PAM/SSO。每个请求独立验证身份；服务端确定 owner，请求不能指定。Basic 新增账号使用加盐 PBKDF2，Bearer 保存摘要、权限范围、失效时间和撤销状态。原个人账号作为管理员保留。

| 方法 | 路径 | 行为 |
|---|---|---|
| GET | `/api/jobs` | 预算、预留、计数、环境、最近 100 项及所有仍排队/占资源的可读作业 |
| POST | `/api/jobs` | 创建作业，首次 201，幂等复用 200 |
| GET | `/api/jobs/<id>` | 状态、资源、结果和产物索引 |
| GET | `/api/jobs/<id>/log?stream=stdout` | stdout/stderr 末尾最多 64 KiB |
| POST | `/api/jobs/<id>/cancel` | 取消等待项或请求执行器取消 |
| GET | `/api/jobs/<id>/artifact?name=<登记名称>` | 下载规范目录内登记文件，不允许任意路径或符号链接 |
| GET | `/api/me` | 当前身份、项目角色、配额、权限范围和隔离级别 |
| GET | `/api/activity` | 脱敏活动：账号、任务类型、状态与资源，不包含命令/路径/日志 |
| GET | `/api/nodes` | 当前本地节点、试用预算、设备健康和采样时间，不包含设备 PID |
| GET | `/api/audit` | 仅管理员：提交、取消、产物访问和拒绝记录 |

写接口只接受 application/json，最多 32 KiB；浏览器必须同源。没有任意 PID、设备重置或任意文件接口。认证后的任务以当前用户权限执行，不能公开该个人实例凭据。

```json
{
  "name": "CPU 流程验证",
  "environment": "host-python",
  "kind": "command",
  "cwd": "/home/m00951283/ascend-lab-demo",
  "argv": ["python", "-u", "examples/cpu_task.py", "--seconds", "6"],
  "cpu": 1,
  "memory_mib": 256,
  "shm_mib": 0,
  "npu": 0,
  "timeout_seconds": 60,
  "idempotency_key": "caller-generated-unique-key"
}
```

command 字符串可替代 argv，用 shlex 拆分而不解释管道。kind 支持 command/build/accuracy/performance/simulation/ir。精度和性能类型必须显式申请 NPU。可选 output 指定当前用户工作区内的额外归档根目录。shm_mib 包含于 memory_mib。

上述任意命令形式仅对个人可信管理员开放。新增逻辑用户的 execution_mode=templates，必须提交服务器固定模板：

```json
{"template":"cpu_demo","parameters":{"seconds":2},"environment":"host-python","project":"personal:demo_alice","idempotency_key":"stable-client-key"}
```

模板拒绝 argv/command、资源覆盖和未知参数；限制环境、工作目录、输出目录及用户配额。project 默认 personal:<认证用户 ID>，共享项目需 editor/manager 提交权限。viewer 可以读取共享详情和日志，但不能提交或取消他人任务；manager 可取消项目任务。私人任务未获授权时详情、日志、产物、取消均返回 404。jobs 列表和 export 同样按 ACL 过滤；只有 monitor:read 权限可读取原个人进程、观察事件和历史实验。

Bearer scopes 为 jobs:read / jobs:submit / jobs:cancel / monitor:read 的子集，不能超过账号自身权限。普通逻辑用户没有 monitor:read。get_job 响应增加 permissions.cancel 与 attempts；任务执行后包含 node_id/attempt_id。旧任务缺少这些字段时仍可读取；原 v0.3 请求幂等指纹保持兼容。

响应为 `{"job": {...}, "reused": false}`。同键不同内容返回 409。客户端断线后用原键重试或查询，不要自动创建新任务。

可选 origin 为只含 client/model/agent_run_id 的对象，每个值为最多 200 字符的字符串。用于网页和归档显示来源，不是独立验证的认证身份，也不能改变 owner 或资源权限。未传 origin 时保持 v0.2 请求幂等指纹兼容。

Agent 可使用 labctl 或相同 HTTP API。v0.3 增加标准 stdio MCP（mcp_bridge.py），支持 initialize、tools/list、tools/call、ping；stdout 仅传 JSON-RPC，工具错误返回 isError。工具如下：

| 工具 | 范围 |
|---|---|
| queue_status | 全局预算、预留、环境与会话策略，不返回其他作业详情 |
| submit_job | 会话绑定环境、固定 cpu_demo、稳定 request_key、1–10 秒、每会话最多一个任务 |
| get_job / wait_job | 查询本会话任务；单次等待最多 15 秒 |
| get_logs | 本会话任务 stdout/stderr，最多 8192 字符 |
| get_result | 成功任务的固定 results/demo-result.json，不能指定文件路径 |
| cancel_job | 仅请求取消本会话创建的任务 |

会话清单由本地可信启动器创建，MCP 私下读取个人服务凭据，不向模型返回。完整底层 API 仍可运行当前个人账号授权的命令；受限 MCP 不暴露该能力。MCP 不自行挑卡或直接运行任务，不维护第二份资源账本。Agent 断线后任务仍由原执行器管理，重试复用既有请求。未来多人认证应在中央 API 强制校验身份，不能仅依赖客户端 session 清单。

已验证 OpenCode + zdl 配置中标注 261017 的 DeepSeek 模型分别以个人账号与 demo_bob 逻辑身份完成真实 CPU 作业。MCP 会话可通过可信启动器绑定 client_profile；调用沿用该用户 Bearer 凭据，受中央权限及模板约束，不能退回管理员身份。逻辑多用户 ACL 已验证；真实多 UID/容器隔离、跨节点协议、完整环境锁定、NPU 真机全链路及 cgroup/设备硬隔离仍需后续实现。

## v0.5 节点协议与实际隔离

上段的“容器隔离/节点协议未实现”为 v0.4 历史状态。v0.5 已实现独立 worker 和受控 Docker，并验证不同任务 UID/cgroup 及无特权中央服务/Root 节点角色分离；仍未验收两台物理服务器、NPU 设备隔离及完整生产安装。

worker-python 为固定模板环境，不接受 argv/command、UID、镜像、挂载或 Docker 参数覆盖。cpu_demo 参数 seconds=1..10；isolation_probe 参数为空，固定 1 CPU / 256 MiB / SHM 16 MiB / NPU=0，仅在 worker-python 可用，账号需有环境和 SHM 配额。请求仍可选择有提交权限的 project，并使用相同的幂等键语义。

节点凭据独立于用户凭据；以下 POST 路由仅接受登记节点 Bearer 密钥，用户或管理员登录凭据也不能调用：

| 路径 | 行为 |
|---|---|
| `/api/worker/heartbeat` | 验证物理资源域、CPU 可见性与协议能力，更新节点状态和安全聚合指标 |
| `/api/worker/claim` | 中央事务准入后领取任务；重复领取返回相同尝试与令牌 |
| `/api/worker/start` | 一次性启动许可；已消费时明确拒绝再次启动 |
| `/api/worker/update` | 上报运行/不确定状态及受限日志预览，查询取消或已确认终态 |
| `/api/worker/blob` | 上传允许的日志/产物类别，连续偏移和相同字节重试幂等 |
| `/api/worker/complete` | 验证节点/任务/尝试/令牌、终止与清理证据、完整清单及 SHA256，然后结束预留 |

节点 JSON 请求上限 128 KiB，blob 数据每块最多 48 KiB（Base64 前），文件最多 64 MiB，总计 128 MiB / 2000 文件。仅接受 stdout.log、stderr.log、environment.json 及 results/ir/profile/source 下的规范相对名称；不能上传控制清单、回执、取消令牌或任意路径。服务端文件与 SQLite 偏移不一致时仅丢弃未提交尾部，不接受覆盖已提交字节。

协议版本为 1，节点必须声明 start-ticket-v1 与 cleanup-before-complete-v1 能力，缺少能力时拒绝领取。启动许可记录在中央账本，节点本地状态丢失不等于可以重新执行。完成必须有 termination_confirmed 与 execution_cleanup_confirmed；状态或回执不确定时保留资源。节点撤销密钥或重连不会重分配同一尝试。

同一 physical_id 下的本地/节点执行共享 CPU 等资源账本；不同执行入口的额度不能直接相加。物理 ID 在节点登记时固定，不能由普通任务请求指定。`/api/nodes` 增加名称、物理资源域、执行后端/隔离级别及安全聚合 CPU/内存/磁盘指标，不包含进程命令、环境变量或节点密钥。

容器 /work 是实际运行目录；原 request.cwd 是中央授权工作区，不能当作节点共享路径。节点执行使用模板和冻结源码哈希，产物通过 HTTP 回传到中央规范归档。环境产物包含镜像 ID、节点服务 UID、任务 UID、cgroup 配置、有限挂载目标及实际容器终态；完整 NPU 工具链版本/兼容约束仍待实现。

目前 MCP 的受限工具仍只开放 cpu_demo；可用 --environment worker-python 验证完整 Agent → MCP → 中央 → 节点 → Docker → 产物链路。msagent 分析能力尚未接入。

## v0.6 独立客户端

`lab_client.py` 不导入中央服务模块，仅从本人 600 权限的普通文件读取个人凭据。controller 指定 HTTPS 或 loopback SSH 地址，ca_file 可指定自建信任根；拒绝远程 HTTP、URL 凭据和所有 HTTP 重定向。节点 RPC 复用这一传输实现，认证仍在中央区分用户与节点，不能互换 token。节点及中央完成阶段对上传产物流式计算 SHA256，仍保留既有容量上限。

`GET /api/me` 返回认证身份、scopes、执行模式、项目与配额；可用执行环境由 `GET /api/jobs` 的 environments 返回。客户端不能因为自己拥有某个 profile 就断言已获得服务器授权。`labctl --profile ... me|nodes|submit|jobs|job|logs|wait|cancel` 不创建本地中央状态，不支持用个人 profile 管理服务生命周期。

独立 stdio MCP 支持 `--profile ... --session-dir ... --environment worker-python --seconds 2`。新会话通过 API 验证身份后绑定 policy 和 run_id，重连使用同一幂等请求；单进程会话锁拒绝同时写入。该接入仍只开放一个 cpu_demo，不等于可向节点提交任意命令/挂载/镜像。模型和 harness 密钥不经过中央服务。格式见 CLIENT.md。

## v0.7 输入、工作流与结果

上述 v0.6 MCP CPU 限制为历史状态：v0.7 可通过 --workflow 绑定一个经中央验证的固定工作流；模型不能改变输入、argv、环境锁或资源。默认无 --workflow 仍为原单 CPU 示例。

| 接口 | 权限与行为 |
|---|---|
| POST /api/inputs | jobs:submit；files 清单含 name/size/sha256/可选 executable；按本人+内容复用 ID |
| GET /api/inputs/{id} | jobs:read；仅本人，其他身份返回 404，中央管理员也不通过此接口任意读取他人输入 |
| POST /api/inputs/{id}/blob | jobs:submit；仅本人，name/offset/base64 data；单块 12 KiB，连续偏移与相同字节重试 |
| POST /api/inputs/{id}/reset-file | jobs:submit；仅本人未封存包，body={name}，事务撤销单文件上传进度；旧字节在下一次 offset=0 上传时截断，额度不提前释放 |
| POST /api/inputs/{id}/seal | jobs:submit；空对象；全尺寸/哈希核验后不可写 |
| POST /api/jobs/validate | jobs:submit；同提交合同，只验证/规范化，不建立作业、不占资源 |
| GET /api/results | jobs:read；同作业/项目 ACL；kind 筛选、limit 1..200、offset >=0 |
| GET /api/results.csv | jobs:read；同 ACL/kind，公式前缀转义；原始结果仍单独归档 |
| POST /api/worker/input | 节点凭据与 job_id/attempt_id/lease_token；只读本次已分配输入的 name/offset 范围 |

输入写入 JSON 上限 128 KiB，其他用户作业请求仍 32 KiB；输入预算默认每用户 32 包/512 MiB，每包 128 文件/128 MiB，每文件 64 MiB。未封存包占预算；尚未提供删除/GC API。worker 输入下载不依赖共享目录，恢复时按已下载长度继续并最终重新核验 SHA256。

工作流模板为 accuracy_v1 / performance_v1 / simulation_v1 / build_v1 / ir_v1。parameters 含 input_id、argv、resources、timeout_seconds、lock、result_file、受限 environment。前三类必须申请 NPU，后两类必须 NPU=0。未获得设备能力的节点保持它们等待；不能作为 CPU 模板绕过设备申报。新工作流还要求 immutable-input-v1、protected-environment-v1、short-artifact-paths-v1 能力，并固定引导器/探针/归档器/合同源码哈希。

lock 至少含精确 Python 版本；可固定镜像 ID、包版本、工具/file SHA256。Root 引导器先以任务 UID 运行探针，把记录保存到任务无法修改的 /receipt，匹配后才启动工作流入口。受保护记录归档为 results/_lab/environment.json。中央核对记录、运行时镜像/源码哈希和结果合同后生成 validated-result.json 与结果表；结果中的数值断言仍来自工作负载，不独立认证任意算法。

Node complete 校验的文件清单另存 artifact-hashes.json。归档器拒绝任务占用 ir/profile/results 下的 _lab，长/不适合传输的路径写为相应类别的 _lab/files/<路径哈希>.<后缀>，并产生受保护的 results/_lab/path-map.json。中央合同通过映射核对原声明路径。其他文件/日志/归档上限维持既有限额，尚不覆盖大 wheel 或任意量级 IR。

字段示例和使用方式见 WORKFLOWS.md；对应真实与负向证据见 evidence/workflow-live-validation.json、root-workflow-validation.json、workflow-mcp-validation.json。

## v0.9 物理预算与多槽领取

/api/jobs 新增 resource_domains：每项含 physical_id、policy、revision、draining 和 reserved。reserved.memory_mib 包含每个受控任务128MiB辅助执行额度，workload_memory_mib 单列工作负载申报；execution_overhead_mib 单列额外预留。旧顶层 reserved.memory_mib 仍是工作负载申报，resource_pool 保留本地域兼容字段；多服务器总量须按唯一资源域汇总，不能相加节点额度。/api/nodes 的受控入口新增 resource_domain 安全聚合，不包含任务内容或用户映射。

多槽节点heartbeat需声明 concurrent-attempts-v1。claim body 可传 exclude_attempts（最多32个本节点正在处理的attempt ID）：中央先返回未排除的原有分配，只有全部原分配均被排除且还有预算时才分配新任务。空body保留单任务重领语义；排除不释放预留，不改变完成令牌/一次性启动规则。工作节点自己的线程池和资源校验也限制总槽位、CPU冲突及内存/SHM。节点并发为1–32且不超过独占CPU数量；未具备能力的旧节点不能取得多槽授权。

资源域修订、排空及审计只提供中央本地管理员CLI，没有用户或节点可调用的预算修改接口。新远程域必须先登记，未知域拒绝节点注册/新任务领取；同域各入口共用同一预算事务。needs_attention继续计入全部资源，排空时允许恢复既有分配。

节点策略还有独立 policy_revision，初始1。管理员修改须无在途尝试，并递增修订/写审计；heartbeat必须匹配中央修订，旧节点缺省按1处理。更新后旧配置不能继续领取任务。域修订与节点修订不互相替代。

## v0.10 NPU设备接口（实验性）

节点profile可由管理员登记npu合同；普通提交请求不能提供设备路径/UID/挂载/特权设置。heartbeat须声明npu-device-whitelist-v1并发送npu_observation：status、sampled_at、policy_sha256、devices(id/health/utilization_pct/process_count)。服务端只保留这些聚合字段，读数最多15秒，物理设备ID与同域其它入口的预留去重。当前每任务只支持一张NPU，visible_ids必须按具体容器/镜像验证，不自动猜测。

NPUgrant增加npu_policy_sha256，固定管理员合同。节点启动前及Docker创建后分别查询获批设备；状态变化时不发送docker start。NPU complete必须有npu_release_confirmed：实际运行时任务还需清退查询；固定npu_probe不初始化运行时、无kernel，仍核对设备清退；只有从未发出start intent可依据未启动事实结束预留。查询失败或状态不明不释放计算任务资源。npu_probe为独立操作模板，需要用户NPU配额和节点模板授权，结果不属于算子精度证明。

/api/nodes新增npu_configured，设备读数仅代表管理查询，不代表运行时或算子已验收。npu_probe失败仍按失败记录，不能因只读挂载配置正确改成成功。现有个人Docker后端的npu-admission.json是协作式启动复查记录，不是受控节点安全证明。
# 个人归档接口（v0.11）

`GET /api/jobs/{job_id}/archive-manifest`沿用任务owner/project ACL，仅已确认终态可读，返回version=1、任务结果摘要和按名称排序的{name,size,sha256}清单。任务/未知执行状态返回409；非授权任务返回404。清单最多2000项、64MiB/文件、160MiB含中央元数据；不扩大任务输出128MiB限制。

`GET /api/jobs/{job_id}/artifact-chunk?name=ENCODED&offset=0&length=262144`只读已登记普通文件，逐层目录FD拒绝符号链接，拒绝硬链接/特殊文件。length为1..262144，offset为非负且不超过文件大小；返回name/offset/size、Base64 data和该块sha256。客户端依清单最终全文件SHA核对，不能把块校验当成完整归档校验。认证、项目访问撤销同样作用于每次续传。

个人archive_root、本地源文件和下载进度均留在客户端，不提交给中央。此接口是拉取归档，不在中央执行个人路径上的写入。
## 待预配用户的中央与节点交接

未提交意图的状态/中止仅开放中央本地CLI：`user-activation-status`、`user-activation-abort-plan`、`user-activation-abort-apply`，没有用户或节点远程接口。普通个人token无法中止开户。原激活ID和文件指纹绑定后只清理匹配的未提交凭据，保留已中止ID；账号已提交则拒绝，旧完成计划重放保留新凭据。操作见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。

最后激活只提供中央本地管理命令 `accessctl user-activate --spec FILE`，没有用户或节点可调用的远程提权接口。请求明确owner、stage_id及全部required_nodes的node_id/plan_id。活跃中央进程组、当前Node状态/环境/配额与回执就绪后，私有凭据先持久保存，再提交启用和token摘要；HTTP身份、个人CLI/MCP合同保持。操作输出不包含密码/token，实际个人文件交付由管理员完成。恢复及当前限制见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。

中央本地管理员通过 `workerctl owner-approve NODE --plan FILE` 批准原始 Node 绑定计划，以 `owner-status NODE` 查询交接状态。计划文件须由中央服务 UID 所有且仅本人可读；普通用户及节点无批准接口。批准核对真实禁用且无凭据的待预配身份、中央原生存储配额、原 Node 投影及修订、空闲执行与物理域清单。未完成交接的节点暂停新领取；已分配资源不因交接或密钥撤销而释放。

以下 POST 仅接受该登记 Node 的 Bearer 密钥，沿用节点 JSON 的 1MiB 上限；个人或管理员登录凭据无权调用：

| 路由 | 合同 |
|---|---|
| `/api/worker/owner-commit` | `{receipt: 本地绑定回执}`，核对已批准计划后原子提交登记投影，返回稳定的 commit ACK |
| `/api/worker/owner-ready` | `{plan_id, commit_ack_id}`，核对原 commit ACK 后返回稳定的 ready ACK |

Root Node 使用 `servicectl node-owner-ack --prefix PREFIX --plan-id ID` 持久保存两阶段 ACK、更新本地 ready 投影并清理启动阻断。网络超时或进程中断后重复同一命令，不另建计划。完成后的实际健康心跳才记录 `node_online_seen=true`；此前仍禁止后续 Node 配置变更。回执绑定节点、物理域、owner、stage 摘要、计划及投影修订；两接口均不启用用户、不签发个人凭据。完整管理与恢复步骤见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。

# 文件系统工作区节点（v0.12）

节点可登记workspace.slots，由管理员指定独立有限文件系统；用户不可指定宿主路径。能力filesystem-workspace-v1与workspace_observation.policy_sha256必须匹配合同；观测只含slot ID、状态、是否干净、剩余字节/inode，不含私有路径或任务内容。新分配在事务中独占storage_id，needs_attention和完成回执尚未确认的任务仍占用。grant附workspace_policy_sha256，allocation.workspace记录slot_id/storage_id/bytes/inodes/durable。任务引导器再次验证实际挂载身份。

五类工作流parameters.workspace可指定min_mib/min_inodes/durable；不匹配则排队，不退回普通目录/不持久文件系统。新安装中央require_bounded_workspace默认开启，CPU模板省略要求也不绕过。GET /api/nodes新增workspace.enforced及槽位摘要，网页显示是否启用硬边界；实际限制与未验证项见WORKSPACE.md。
# 有界大文件与存储账本（v0.13）

## 等待老化（v0.15）

GET /api/jobs的policy.aging_seconds返回管理员阈值（默认300秒）。用户不能传入优先级、排队时间或等待目标；重连/幂等复用保持原created_at。超过阈值的任务按提交顺序优先，仍需满足完整准入。符合条件的较老任务可暂停同物理域新分配，其他用户只看到通用等待原因，不获得其ID/owner。等待意图失效不释放任何运行/未知执行的实际资源。作业列表包含全部有权限读取的排队项，最近100条历史限制不再截掉长期等待项。详情接口和个人CLI/MCP协议保持。

## IO合同

GET /api/me新增io_limits、transfer_chunk_bytes和个人storage摘要；GET /api/jobs同样提供个人storage。任务artifact_limits冻结file_mib/total_mib；工作流parameters.artifacts请求不能超过中央上限。输入按清单预留，排队任务按最大产物加8MiB元数据预留，终态按实际登记文件加余量结算；未知状态不释放。该账本不是文件系统硬配额。

输入与节点输入/产物块上限256KiB；输入POST JSON最多512KiB、节点POST最多1MiB，任务提交仍32KiB。节点io_limits必须与登记合同相同，大产物还需bounded-large-artifacts-v1能力。个人归档清单仅在非默认额度时附limits，保持旧默认任务清单稳定。浏览器artifact接口以流式读取和单段Range返回200/206，越界416，逐层FD读取和任务ACL仍生效。详见STORAGE.md。
# v0.20 中央载荷留存

固定 environment_probe 的 parameters 可增加 memory_mib（256–65536 整数，默认256），用于让大型 tmpfs 槽位符合既有内存预留规则；CPU仍1、NPU仍0、命令固定，所有正常资源配额适用。领取带此字段的新探针须 environment-probe-memory-v1 能力；旧节点零次启动等待升级。不可通过此参数覆盖其他执行控制。

v0.21 节点 heartbeat 的 node-spool-v1 能力附带 disk_free_inodes（非负整数）及 artifact_gc_ready（布尔值）；新安装中央要求此能力才派发新任务，旧尝试仍可恢复。complete 在核验所有产物 SHA 后，保存并返回 artifact_receipt={job_id,attempt_id,manifest_sha256,state}；幂等 complete 返回原回执。回执用于节点副本清理，不扩展用户 token 权限，不向普通用户开放节点控制状态。公开节点摘要包含 spool_admission 与相应观测值。

`GET /api/storage?state=trash&limit=100&offset=0` 按本人所有权列出输入与归档（limit 1–200）；不提供跨用户删除。`POST /api/storage/plan` 接收 `{kind,id,action}`，kind 为 input/archive，action 为 trash/restore/purge/resume，返回固定修订与文件系统指纹的 plan、eligible 和 can_apply。`POST /api/storage/apply` 只接受该 plan 的五个字段，需要本人 `storage:manage` scope。pending 表示原文件操作尚须 resume；不是重提任务。默认 7 天隔离保留期、完整删除才退额度，详见 STORAGE.md。

任务详情保留 artifacts 清单、archive_status 和 retention_metadata；结果表/CSV 增加 archive_state。已隔离/永久删除载荷返回 410，进行中文件操作返回 409。GET 任务/输入元数据仍可用；POST 作业会在同一提交事务内拒绝不可用输入，旧幂等请求仍返回原任务。没有新增任意路径删除或自动 GC 接口。


版本化POST /api/jobs可携带顶层depends_on（最多8个同owner已有工作流）和artifact_inputs（最多32个来源/目标映射），不支持客户端路径访问中央文件。GET job返回dependency_state和完成后的input_provenance；未就绪时无attempt/allocation。POST /api/jobs/JOB/retry-inputs只接受{}，本人未启动任务可继续原摘要交接，不接受来源或命令覆盖。详见DEPENDENCIES.md。

compiler-case是对原任务/日志/artifact-chunk读取接口的固定路径组合，不新增执行端点。完整小IR与封存驱动、受保护工具锁绑定；未知归档仍按原终态门禁拒绝。读取仅需jobs:read，个人候选不能经该getter提交，详见COMPILER_DIAGNOSTICS.md。

## 指定来源产物授权

`/api/me.dependency_capabilities`声明`owner-source-grants-v1`。普通个人账号以`POST /api/source-grants/preview`读取有界、无写入的审批计划，再以`POST /api/source-grants`提交原计划；`GET /api/source-grants`及`GET /api/source-grants/ID`读取本人授权，`POST /api/source-grants/ID/revoke`接收空对象。预算凭据只看到目标为自身预算的授权元数据，不能创建/撤销，也不获得来源job读取权限。工作流`artifact_inputs[]`可添加`source_grant`，其来源、文件和目标预算须精确匹配。字段、限额和恢复语义见[SOURCE_GRANTS.md](SOURCE_GRANTS.md)。
