# 实施进度

## 失败产物交接、冻结来源与中断续传（2026-10-31）

**最终946项/183.404秒/无跳过与JS语法通过；同机隔离Root安装31项通过。** full `job-8838db5282be`，安装 `job-11cc578c6430`，focused `job-7483a86667cb`=31项/7.179秒。90个非文档CORE与final-frozen-r1及实际安装包一致，protected6仍v0.36、APP4和既有r4工具镜像未变，无重建；网页资产未改，未重新执行浏览器或开户49检查。本轮0模型调用、0新NPU启动。

新dependency_conditions.py让artifact_inputs逐项显式声明when=terminal，默认success及其失败传播不变；同一parent混用条件拒绝，显式success规范化后仍复用原幂等请求。只接受明确终态、已认证受控Node及确切attempt；要求结束/执行清退、实际使用的工作/缓存/NPU释放确认和匹配的中央归档回执。无attempt的排队取消不伪造来源。运行、未知或确认不全时继续等待，无提前计算预留。整个manifest摘要核实后冻结terminal_sources及每项来源；发布、领取和首次启动许可重核事实，变化保留原输入/attempt等待。旧代码缺能力时拒绝激活/恢复，备份仍要求全部排空。/api/me和就绪记录声明能力；客户端附DEPENDENCIES.md，未改lab_client.py的APP4边界。

实际安装中个人批准同一5任务/255声明CPU秒/0NPU预算，经独立固定stdio MCP提交五任务、实际四次执行。历史HiF8 PRE数据父任务failed，独立terminal诊断保留相同原始字节并重算出11/5376差异，仍为failed；默认success后继cancelled/0attempt。另一个明确的15秒CPU等待程序写入标记后timed_out，后继成功读取同一标记。任务UID23003、无设备/网络、原生UID工作盘配额；跨owner导入拒绝，上游失败归档在交接中受回收保护。6 MiB文件是明确合成的传输fixture，不是新增数值数据；CPU等待不是NPU死锁复现。

控制器在实际交接已复制1,048,576/6,336,048字节时停止，停止态DB记录后继queued、无attempt/allocation。重启沿同一input ID和原来源摘要继续，而不重新执行父任务。Controller/Node备份恢复保留五job的状态、四attempt及五条计费，旧执行token401、预算冻结，检查没有再次运行这些比较或等待任务；真实旧21bde7f包降级被拒，当前发布保持有效。主助手独立核原NPZ载荷/原文件SHA、归档报告与标记、完整合成文件、原/显式导出恢复DB integrity、attempt/计费和来源确认。恢复态文件是在当时RoleFS中读取，宿主保留的是明确导出的恢复DB。

本轮Main遇到HTTP409个人归档准入上限：802个留存任务的有效归档账本约4,285,906,429字节，含每任务元数据固定预算；所有任务已终态，并非在途预留未释放或磁盘写满。保留全部数据，备份原私有配置、确认空闲后，只把本Demo的archive_budget_mib从4096调到8192并重载。配置SHA由历史5caf...变为当前`43febea5bc7f2e572d65906feeed710bb46025e26a5ff80c78826c6887fa754f`。52个显式个人配额及全部存储的profile未改；27个未显式配置的逻辑身份继承新全局上限，其中2个是启用的历史Demo身份，其余为禁用fixture。没有修改宿主用户或CPU/NPU策略。此调整解决当前有界容量需求，不替代留存/增长规划；旧报告中的历史配置摘要仍保留。

Main的19 Node profiles、物理域、CPU254255/2048MiB/SHM1024MiB/1槽及zdl不变，零非终态/有效hold。2个自有loop关联已释放，5类fixture容器/卷namespace确认empty、backings私有保留，无宿主账号/全局unit/驱动变更。证据evidence/terminal-handoff-validation.json、runtime/terminal-handoff-20261031及runtime/main-current-invariants.json；公开精选reports/public-source-check.json.terminal_handoff。文档在实际包冻结后完善；非文档CORE/测试/网页及harness有明确最终冻结范围。

接下来继续真实设备失败产物、完整lowering/编译崩溃、仿真和预算内候选验证，以及一般DB/归档增长、显式跨预算源委托。当前同预算交接不代表恢复后的新预算自动可读旧源。真实宿主用户/PID1、两物理机断网fencing、非root NPU访问/数值/性能/仿真与长期规模仍待，接入顺序见DEPLOYMENT.md。PUBLIC-001继续延期，production_ready=false，整体目标仍未完成。

## 历史精度差异、参考契约与独立张量重算（2026-10-31）

**最终933项/183.313秒/无跳过与JS语法通过；同机隔离Root安装30项通过。** full `job-16d8061d7670`，安装 `job-bf46b25ca57b`，异常回执强化前focused `job-4fe2f02246e6`=17/6.558秒。89个非文档CORE与最终实际包/final-frozen-r3一致，protected6仍v0.36，APP4和既有r4镜像未变。本轮使用原基础镜像执行标准库CPU比较，没有重建工具镜像、重跑开户49/浏览器、调用模型或启动新NPU kernel。网页资产逐字节不变。

新增accuracy_data.py/accuracy_diagnostic.py/ACCURACY.md，独立客户端声明accuracy-case-v1，提供accuracy-case CLI和单任务空参数get_diagnostic_case只读MCP。输入为固定名称、完整SHA及明确dtype/shape的actual/reference二进制文件，另可带同形状的数值输入上下文。最多8维/1,048,576元素/每文件8 MiB；标准库处理小端uint8/float32/float64，不引入NumPy/pickle或设备库。exact_encoding只统计编码差异，不把编码相差1解释为数值误差；numeric明确容差、NaN/Inf和正负零政策，有限算术溢出时重新以有理数判断，防止inf<=inf误判。指标无法有限表示时为null并带标记；最多16个差异样本。CSV对公式字符串转义。

读取器先按原任务ACL授权，再校验完整归档、封存driver/config/全部张量SHA并重新计算；报告与重算结果不符则拒绝。MCP交付政策、形状、完整摘要及有限差异样本，不传完整张量，不修改参考/容差或提交实验。来源声明始终标注caller-supplied；CPU环境回执确认的是本次重放，不能认证历史设备来源、参考正确性、硬件故障或修复。比较有差异时CPU build_v1任务本身明确failed、报告仍归档，结果表和CSV保留失败；真实设备精度任务仍需原accuracy_v1及NPU预留/资格。

本轮只读提取三组真实历史HiF8数据，没有运行原测试脚本：原FP32 Hybrid实际输出与PRE摘录参考为11/5376差异；保持实际输出字节不变，对单独记录的Vector观测参考为0/5376；原16×32×16 dot的256个FP32输出与FP64参考最大绝对误差0。原PRE失败保留，参考变化不是算子修复，观测Vector模型也不是正式ISA条款认证。主助手最终用标准库重新从原NPZ中提取数值载荷，核对所有原文件SHA、实际输出和参考，以及原/后续采集的相同输入和输出后缀；不加载原模块或pickle，不改动原项目。

实际Root30中三个独立预算stdio MCP会话共享3任务/180声明CPU秒/0NPU，每个任务1attempt、Task UID23003、无设备/网络、原生UID工作盘配额；个人UID23642运行独立安装CLI/MCP，peer23004读不到其0700/0600 profile，其他应用owner读取任务/结果表被拒。MCP重连复用原任务且不重复计费。原失败、两个通过和各自参考ID都保留在结果表与CSV。Controller/Node恢复后仍是相同三job/attempt/摘要/计费，预算冻结、旧执行凭据401，历史只读诊断可用，恢复检查没有再次运行比较。主助手原归档重新重算并核原/显式导出恢复DB integrity、单attempt、三条计费；恢复态归档是在当时RoleFS上实际读取，宿主仅保留明确导出的恢复DB。

首安装 `job-cbdd46560941` failed保留：fixture漏登记build_v1，唯一比较job-a338046e0191一直queued/0attempt/无allocation，90秒观察时限触发fixture失败。原Node登记列表、零attempt状态已从清理后的原DB复核；没有比较执行或精度结论。计划取消时其服务已自行结束，取消请求并未发出，原任务行保持queued且没有手工改状态。修复仅为测试Node显式登记build_v1；不放宽生产调度。CORE和已发现test_*.py源码与完整932检查逐字节相同，因此仅重跑集成安装、未无意义重复完整回归；该次入口源码及实际安装绑定final-frozen-r2。最终复核另增加受保护回执observed字段结构检查和异常stdio回归：损坏结构返回工具错误而不退出进程；随后重新完整933及实际Root30验收，以final-frozen-r3为最终范围。原932/177.403秒和Root30保留为强化前中间版本，不移作最终933验收。

三次安装共6个自有loop关联released，5类fixture容器/卷namespace只读确认empty，backings私有保留。Main配置SHA5caf...、19 profiles/物理域/CPU254255/2048/SHM1024/1槽及zdl不变，零非终态/有效hold，无宿主账号/全局unit/驱动变更。证据evidence/accuracy-validation.json、runtime/accuracy-case-20261031，精选reports/public-source-check.json.accuracy。文档在冻结包后完善；当前非文档CORE/已发现单元源码及最终集成脚本有明确冻结范围。

下一步继续实际精度任务的设备产物→离线分析交接、卡死/编译崩溃及仿真证据，不能把历史CPU重放代替新设备验收。真实宿主账号/PID1、两物理机断网fencing、非root NPU访问/数值/性能/仿真与长期规模仍待。PUBLIC-001继续延期，goal active/production_ready=false。

## 完整历史算子 IR、有限诊断与空日志一致性（2026-10-31）

**最终915项/168.736秒/无跳过与JS语法通过；同机隔离Root安装33项通过。** full `job-fdcc48125511`，安装 `job-66d152f413e1`，87个非文档CORE及独立compiler_compatibility.py与final-frozen-r3、实际发布包一致。protected6仍v0.36、APP4/r4工具镜像未变，无重建；本轮没有重新运行开户49或浏览器，网页资产逐字节不变。0模型/0 NPU，不把先前模型、设备或全包构建证据转作本轮验收。

配方新增输入schema v2，完整IR输入/输出各最多8 MiB；v1仍保持8192字节。工具路径/文件SHA仍需同时匹配封存配置与受保护环境锁，只运行固定parse/verify。子进程同时排空stdout/stderr，仅各保留8192字节，记录读到的总数，固定20秒编译时限与输出文件大小上限；信号终止、超时或不完整错误不能进入原最小语法重命名实验。个人客户端新增compiler-large-ir-v2：按最多256 KiB的块验证完整归档的名称/偏移/大小/块SHA，再核对报告及封存源SHA。大文件只返回首尾及最多四处报错行附近的脱敏预览，source_ir=null/source_complete=false和完整已校验长度，不能把预览当作精确补丁输入；完整文件沿原授权归档下载。见COMPILER_DIAGNOSTICS.md。

真实输入是只读复制的历史MergeTopK baseline HIVM dump，1,935,540字节，SHA256 `633a18511572b8373360d042f9638234423ce95479612207c76465182ba867bd`；原项目文件前后摘要一致，没有执行原算子脚本。两套固定消费者分别通过独立stdio MCP会话、同一2任务/120声明CPU秒/0NPU预算执行。Triton工具job-84437e97526e明确failed/exit1，在第1行报expected '='；BiShengIR job-530fbc2897c0 succeeded/exit0并输出1,935,541字节verified.mlir。两者各1attempt、task UID23003、无设备/网络及原生UID工作盘配额；任务结果如实保留，不将前者改成测试通过。个人UID23642从独立安装客户端读取有限诊断，不能覆盖路径；peer读取任务404，MCP重连返回原任务且不再计费。这个接受差异不证明任意新旧工具迁移规则、完整lowering、数值等价或设备正确性。

实际Controller/Node恢复后两job/attempt、输入SHA与120秒计费保留，预算冻结，旧预算凭据401；恢复检查不新增编译任务。主助手从原归档独立核完整输入、成功输出与报告SHA、受限运行元数据，并从原/明确导出的恢复DB核integrity、原attempt与计费。恢复归档读取在当时的RoleFS中实际完成；宿主仅保留显式导出的恢复DB，不声称已挂载或保留整个恢复文件盘。

首安装job-204c8d8537b8 failed记录保留：两真实编译已各执行一次，第一失败任务的诊断成功；第二编译成功，但worker传输省略零字节stderr，客户端却要求该条目存在。经原DB/归档独立定位后修读取器：声明零字节且无条目时，明确compiler_stderr_archived=false、sha=null及声明来源；非零缺文件仍拒绝，缺归档错误不能批准原重命名实验。驱动与工具镜像没有因此改变。初次915前的full913/162.815秒与full913/163.834秒均为中间范围，不当作最终915源码。新增检查覆盖大IR中间块破坏/短读/总SHA/大小、跨块中文与脱敏、报错行上下文、两管道大输出、静默超时、输出文件上限及空stderr合同；不是新的实际编译器崩溃或硬件卡死复现。

两次安装共4个自有loop关联released，5类fixture容器/卷namespace只读确认为empty，backings私有保留。Main配置SHA5caf...、19 Node profiles/物理域/CPU254255/2048/SHM1024/1槽及zdl不变，零非终态/有效hold，无宿主账号/全局unit/驱动变更。私有evidence/large-ir-validation.json及runtime/large-ir-20261031，公开精选reports/public-source-check.json.large_ir。文档在冻结包后完善；非文档CORE/driver/测试/网页及harness源码冻结一致。

下一步继续真实精度错误、卡死和仿真证据及预算内候选验证；大IR的有界reducer和完整lowering失败仍待。真实宿主账号/PID1、两物理机断网fencing、NPU ACL/数值/性能/仿真与长期规模验收仍缺。PUBLIC-001继续延期，goal active/production_ready=false。

## 累计实验预算、委托凭据与旧快照冻结（2026-10-31）

**最终905项/159.147秒/无跳过与JS语法通过；87个非文档CORE与实际安装Root38、开户Root49一致，网页7组通过。** full `job-7b92e0f9fff1`，安装 `job-0ad2d7ca716c`，开户 `job-e366a5d2ed51`，网页 `job-a38570591796`。protected6仍v0.36，APP4/r4工具镜像未变，不重建。0模型/0 NPU；两卡计费只是数学账本fixture，不是设备执行。原901/903/904均保留中间范围，最终以905及final-frozen-r5为准。

新增experiment_budget.py：普通个人账号可批准一轮实验的任务数、CPU/NPU数量×任务时限、模板及有效期。预算创建有自己的幂等键，重试保留原ID/期限，不重置；新任务与计费在同一作业事务中提交，失败回滚、并发不会超额。失败/取消/未知状态不自动退款，任务幂等重试不再次扣费。计费是获批负载的保守申报量，不是实际CPU/NPU占用、初始化/清理或模型账单。已有工作/权限/环境/工作盘和资源域检查继续执行。

个人账号另签仅绑定该预算的token；请求省略experiment_id也由中央绑定，指定另一预算拒绝。预算token不能创建/关闭预算或再委托，只读/取消也限制到本预算任务。单预算最多16个历史委托token，撤销/过期不自动归还次数，签发受容量/审计保护。普通profile若仍能被Agent读取就不能称为单预算隔离，必须配合harness工具、真实UID与私有HOME/XDG。新安装个人客户端声明experiment-budget-client-v1；CLI可创建/列举/查询/关闭/私有文件委托，不打印token、不覆盖文件。固定MCP会话仍一次一个请求，多个会话共用中央预算；queue_status回显剩余额度，账号或预算绑定变化时拒绝继续。MCP固定JSON省略实验ID，由预算profile提供；普通CLI/网页可显式指定。说明见EXPERIMENT_BUDGET.md。

旧快照可能遗漏后续消费，不能据旧剩余额度自动恢复执行。恢复将预算标记restored_frozen并撤销旧委托；历史计费和结果保留，但计数仅代表检查点已知部分。新的执行需另行批准新预算，人工可签只读历史凭据继续诊断，不能重开额度。合法备份原本就要求所有queued/active任务终态，不能把直接SQLite复制的非静默fixture当作受支持备份。旧包缺少预算实现时，即使预算已关闭/旧token撤销也拒绝激活/恢复，避免丢失边界。

实际Root38从新安装公开CLI审批2任务/120 CPU秒/0NPU预算，交付不含个人密码的预算profile。两个独立stdio MCP会话真实bishengir-opt IR各1attempt，重连免费，第三会话超额失败，不能读取其他个人任务或扩大权限。Controller/Node恢复后旧委托401、预算冻结、同job/attempt及2条计费保留；人工只读profile能读历史任务，提交403。真实0c27e7b旧发布包回滚被拒。随后原生控制盘/挂载覆盖/跨盘恢复、审计128行积压、export exit77/prune exit78及恢复epoch等原验收一并通过。主助手另核原DB、正常恢复显式DB导出、原生roundtrip DB完整性与两任务单attempt/计费120；原状态closed、恢复态frozen，原归档两次task_uid23003及actual IR constant42。开户49同87，另6个真实CPU IR各1attempt；网页7同当前资产，6个排队/取消fixture零attempt。

保留失败：首full901仅旧identity错误文字regex失败，保留原兼容措辞后修复；首安装77d...在排队时入口源码变化被正确拒绝。prefence安装1ac...实际执行2个MCP IR后发现恢复仍保留旧委托，新增恢复门槛拒绝，旧包不能算当前验收。之后安装290...及开户c3f...在Docker run/create回执超时、Created/PID0时失败，尚未启动新安装/开户；清理所有权helper也超时并延迟留下一个Created对象。按精确ID/标签/镜像/挂载/intent inode核实，写释放标记并普通删除，未force。首最小CPU探针create回执也超时，不当作启动成功；所有已发布Created对象正常清退。第二最小CPU探针93a85d...真实create/start一次、输出/exit0并正常删除后，才重试最终Root38/owner49。运行时恢复但根因未证，未重启Docker/驱动或操作他人对象。

28个本轮自有loop关联released，5类fixture容器/卷namespace只读确认empty，backings保留。Main配置SHA5caf...、19 profiles/物理域/CPU254255/2048/SHM1024/1槽及zdl不变，零非终态/有效hold；只有预期新增空预算表及token可空绑定列，Main未审批实验或签预算token。无真实宿主账号、全局unit、驱动改动。私有证据evidence/experiment-budget-validation.json、runtime/experiment-budget-20261031，精选reports/public-source-check.json.experiment_budget。文档在包冻结后完善，只声明非md CORE/测试/网页及harness源码范围一致。

下一步继续大IR/精度错误/卡死/仿真证据和预算内候选生成闭环；本轮没有安装完整领域harness或执行新的模型调用。真实宿主账号/PID1、两物理机断网fencing、NPU ACL/数值/性能/仿真和长期多用户规模仍待，PUBLIC-001继续延期，goal active/production_ready=false。


## 网页五类冻结工作流提交与实际 CPU 闭环（2026-10-31）

**新增打包网页7组、原预览11组及同机Root安装26项通过；最终安装中网页触发的真实IR/编译任务各1attempt，恢复后身份和IR字节不变。** 网页 `job-6243e027be7e`、预览 `job-bb8363c950a7`、最终安装 `job-c687a0b99e05`。86个非文档CORE、73个已发现unit测试文件及protected6/APP4/r4镜像与审计阶段相同；本轮只改生产网页，不重建镜像、未重跑886单元/Root34/owner49，不把历史计数写成新整包验收。当前JS语法及网页资产SHA已核。本轮0模型/0 NPU。

“提交方式 → 版本化工作流”可导入/粘贴accuracy_v1、performance_v1、simulation_v1、build_v1、ir_v1的已有JSON，显示封存输入、执行参数、资源、环境锁、结果位置、工作盘/缓存和依赖。必须明确worker-python及原幂等键，普通命令的名称/项目/资源/cwd不覆盖JSON；下载当前请求不提交。导入文件限256KiB，实际序列化POST仍受中央32KiB限制；源码封存和原脚本同级自动归档继续用个人CLI/MCP。不会从浏览器读取服务器源码或自动保存私人请求。

相同构造同时用于只读预览和显式提交，后端仍核权限、封存输入、环境和容量。导入/编辑使旧预览失效，解析错误在发请求前显示；请求核对用textContent。明确JSON幂等键在响应丢失、刷新后导回原文件时保留原任务；同键改内容拒绝。五类实际HTTP队列语义在无worker的隔离两逻辑用户fixture验证，6个任务最终cancelled/0attempt；设备类提交通过不代表设备执行。7组另覆盖文件下载往返、跨用户输入拒绝、只读POST403、超限导入保留原稿和390px截图。原11组预览计时、乱序/失败/权限和实际Main只读检查也通过。

最终Root26通过实际新装公开代码启动网页：个人proof_user、中央无特权服务、Node0、任务23003。两个网页请求分别运行实际bishengir-opt和cc共享库/ctypes42，固定Python/镜像，资源及命令逐字段与预览/提交相同；重复提交各复用原job/单attempt。结果表/CSV含两条，实际IR下载核constant42。中央与Node备份新目录恢复后，两原job/attempt仍相同，IR字节与下载完全一致；停止态恢复DB显式导出，主助手另核两个数据库integrity及任务各1attempt。Role恢复态不等于宿主完整挂载树。

保留失败与范围：首网页fixture缺container键，第二次7组完成后报告误取release.id，修正为release_id后完整r3通过。安装r1未登记build_v1，原build正确等待/0attempt；r2两个CPU任务成功但Bearer头测试下载被取消，独立受保护HTTP探针复现Basic200/成功与额外Bearer头下载401/取消，测试改为正常Basic登录，不改CLI/MCP协议。r3误用旧管理员而非输入owner，预览被ACL拒绝，改回原个人凭据。r4仍见预览失败但未留下具体响应，原因未证；两次复制状态的Basic只读重放200，新增响应诊断后r5完整通过，不宣称修复了r4根因。早期Root/r3/r4均保留failed，不重试旧任务或放宽准入。

五个自有安装范围的loop均released，4类fixture容器/卷namespace只读确认empty；Main config SHA5caf...、19profiles/domain/CPU254255/2048/SHM1024/1槽及zdl不变，零非终态/有效hold。凭据/原始数据库/截图/下载及测试盘私有留存；精选公开报告在reports/public-source-check.json.workflow_web，私有evidence/workflow-web-validation.json和runtime/workflow-web-20261031。

下一步为累计实验预算及真实大IR/精度错误/卡死/仿真案例，继续稳定API/MCP能力适配。真实宿主用户/PID1、两物理机断网fencing、NPU ACL/数值/性能/仿真、组织身份及长期运维仍需目标机验收；PUBLIC-001继续延期，goal active、production_ready=false。


## 审计先保留、有界归档与确认裁剪（2026-10-31）

**最终886项/146.234秒/无跳过及JS语法通过；86个非文档CORE与同机隔离安装34、开户49冻结一致。** 全量 `job-acd14c77c18e`，安装 `job-abdd47fcde58`，开户 `job-27a0e58d0f6e`，独立归档/恢复验证 `job-db9d0784ef2a`。protected6保持v0.36，APP4/r4镜像不变；本轮0模型/0 NPU，不移用以前设备或编译案例作为当前验收。

新管理员中央默认archive_required；达到全表未裁剪记录门槛时暂停新增任务/输入、claim及未消费start，保留原任务、许可、监督和结果回传。新模式不自动删认证审计；缺省旧配置保持滚动10,000条，不自动迁移、不补回已丢记录。GET /api/audit-retention、jobs摘要、MCP queue_status和网页呈现同一积压。新作业幂等重试在积压期间复用原ID且不重复写审计；不声称所有API重试均无新增事件。软门槛不是物理容量上限，合法既有事件仍可能增长。

Root停中央后，用匹配安装的servicectl audit-export导出最旧前缀，最多10,000行/16MiB行数据；Root私有归档离开状态树，原生模式另盘。文件fsync/不覆盖发布先于数据库回执，支持原文件恢复孤立导出。audit-prune必须给出完整SHA，核对文件/原前缀/数据库与恢复epoch/持久回执后才在同一事务裁剪、记回执及事件。序号不复用，重复确认不再次删除。SQLite操作使用服务UID，避免Root hot journal；快照恢复更换epoch并使旧回执失效，降级旧代码被拒。文档见AUDIT.md。

实际安装34在原生控制盘下以128行门槛制造积压，新任务/输入503，follower零attempt；原真实CPU IR保持同attempt。仅停止中央导出，Node继续。导出发布后exit77、裁剪事务内exit78均从原文件恢复；崩溃时真实journal属主999。错误SHA拒绝、重复export/prune幂等。安装包真实网页显示积压和预览阻碍，零提交/页面错误/外部请求。实际4818529旧发布包激活拒绝；普通→原生→新盘快照恢复与stop等原检查仍通过。独立读取已释放的自有native4镜像明确导出identities DB，核integrity、epoch改变及旧回执invalidated；只读debugfs，无写挂载/物理设备操作。Role恢复态仍在私有盘镜像，Host仅明确导出的DB/归档，不声称完整恢复树挂载。

开户49故障点与6个实际CPU IR各单attempt通过。所有最终检查同86源码；206测试源码和网页冻结。针对性11通过；初次1error是测试预期ValueError而O_NOFOLLOW实际返回ELOOP，修正测试。首次安装在旧包未挂载到Role的测试路径失败，显式复制到本次自有Role后重新完整验收；不把失败尝试改为成功。首次full886/145.794属于最后小范围健壮性修正之前，以最终146.234秒为准。

本轮3个安装/开户范围共21个loop关联释放；自有installproof/nodeowner/nativeprobe/diskproof容器/卷namespace只读确认empty，backings保留。Main config SHA5caf...、19profiles/物理域/CPU254255/2048/SHM1024/1槽和zdl不变，零非终态/有效hold。私有证据evidence/audit-retention-validation.json；公开精选reports/public-source-check.json.audit_retention。

control-monitor仍不连接SQLite，只报告静态策略和文件系统余量，不能代表审计积压准入；未新增告警发送器、自动上传/定时维护/VACUUM。一般数据库及归档增长、累计实验预算、真实大IR/精度/卡死/仿真与五类工作流网页仍待；真实宿主账号/PID1/第二物理机fencing及NPU数值性能仿真仍待目标机验收，PUBLIC-001继续延期，goal active/production_ready=false。


## 控制存储外部检查与审计滚动策略披露（2026-10-31）

**最终875项/144.707秒/无跳过及JS语法通过；85个非文档CORE与安装Root31、开户Root49一致。** full `job-bfd53534788e`，安装 `job-02ba2ba3e5ce`，owner `job-de69a5cf5138`。protected6仍为v0.36；APP4/r4镜像未变，不重建镜像，不重新调用模型或NPU。旧browser41和前阶段Root30保持原源码范围。

新增可选管理模块control_monitor.py及 `servicectl control-monitor --prefix ... --format json|prometheus`：仅Root或指定中央服务UID，Node/peer拒绝；先校验匹配的只读安装代码及原生根，再通过FD检查固定数据库、journal/WAL/SHM和轮转日志的类型/挂载/逻辑大小/已分配大小。没有SQLite连接、目录扫描、journal恢复、网络、配置写入、删除、PID信号或调度动作；未知值为null/无sample，不记成0。容量快照不代表服务或任务就绪，也不是整个控制树占用。外侧根替换时critical且不读错误挂载config/文件；原私有服务可以仍在原盘完成任务。

退出0/1/2/3分别ready/warning/critical/unknown；监控字节/inode阈值独立于原准入，现有准入暂停也报告critical。固定低基数Prometheus gauge不含用户名、作业ID、路径或凭据，附采样时间供外部缺失/过期告警。命令单次输出，不安装HTTP exporter、node_exporter、Alertmanager、cron/timer或实际告警发送器；外部collector仍需管理员配置，不能只在exit0时更新textfile而把critical丢弃。文档见CONTROL_MONITOR.md。

实际Root31包含当前31项安装/普通到原生/原生跨盘恢复、准确stop和单attempt IR，以及服务UID999读指标、peer23004拒绝、真实字节/inode观测上的阈值注入0/1/2分类、告警条件不改准入、挂载覆盖和服务停止时仍可检查。这里没有新写满控制盘；以前真实ENOSPC41属于旧源码阶段。独立parser `job-4a8616134bd5` 使用仅项目runtime中的prometheus-client0.23.1纯Python wheel（PyPI官方SHA核对；非产品依赖/全局安装），正常26和不可用6 samples、11个families均通过。Root归档停止态DB显式导出完整性及原3任务各1attempt由主助手再核。

核实原认证审计本来会在新增时滚动删除全表10,000条以外行，没有先归档。提取同值AUDIT_ROWS常量和参数化LIMIT，保留行为；检查命令披露rolling/maximum_rows/archive_before_drop=false，修正文档中容易误解的表述。长期审计导出/校验/裁剪仍未实现。单元检查在独立fixture插入10,005条后验证保留10,000和新记录；不裁剪Main审计。

初始focused10有1fail：测试把29字节的journal常量写成28；修正期望后10/0.055秒通过。首轮875/146.671秒在部署CORE注册监控文档之前，最终重新全量并冻结当前85。最终Root和owner使用同85；不拿中间源码直接宣布最终验收。

14个本轮自有loop关联released，installproof/nodeowner/nativeprobe/diskproof容器与卷namespace只读确认empty，backings保留；Main config SHA5caf...、19 profiles/domain/CPU254255/2048/SHM1024/1槽及zdl不变，0非终态/有效hold。私有证据evidence/control-monitor-validation.json、runtime/control-monitor-20261031；公开精选reports/public-source-check.json.control_monitor。

下一步仍为有界审计导出与明确保留策略、累计实验预算、真实大IR/精度/卡死/仿真及五类工作流网页；真实宿主账号/PID1/两机fencing和非root NPU数值/性能/仿真仍待。目标active、production_ready=false，PUBLIC-001继续延期。


## 独立有限控制盘、原生启动身份与跨盘恢复（2026-10-31）

**最终865项/146.373秒/无跳过及JS语法通过；84个非文档CORE与实际安装30、开户Root49冻结一致。** 全量 `job-5c10576f12b0`，安装 `job-e4ddce2059c9`，开户 `job-56e51e943e56`。原6个受保护运行文件仍为v0.36哈希；固定r4工具镜像的APP4不变，无需重建。本轮0模型调用、0 NPU kernel，不把旧浏览器41、旧编译诊断或性能镜像验收移作当前84源码的新验收。

新增可选 `--control-config`（doctor/install/restore）：Root:0755、整个独立有限ext4、固定设备/inode/容量/随机存储身份，状态固定root/state；拒绝普通目录、共享FS子目录bind和混用控制/载荷盘。新盘至少实际128MiB/1024inode，另需独立payload盘、managed-only及余量准入。旧Main配置不变，不自动迁移；安装器不分区、格式化、清空未知目录或改变宿主挂载。

可信Root启动器先验证只读代码/保护元数据，建立私有挂载空间并保留原命名空间及父目录FD，再发布控制盘外Root:0444进程记录并降UID/GID。私有视图固定SQLite规范路径及journal；原父目录FD独立观测外侧覆盖。TMPDIR/SQLITE_TMPDIR位于控制盘私有tmp，关闭core dump；实际HTTP进程UID999、有效/许可capability均0，无Docker socket。直接无特权run/server启动该模式被拒绝；同一Root lifetime flock覆盖启动及备份/激活/恢复的短暂初始化窗口。

外侧控制根换盘时新任务/claim/start等待，原任务在原文件系统上回传并完成；不写错误挂载或下层普通目录。准确stop不依赖丢失的控制路径，pidfd仅指向原保护身份；缺CAP_KILL时用服务UID发送同一pidfd。真实退出采样发现/proc可先清空cmdline后变Zombie：现先保留原进程且不信号/不替代，等待身份确认；Root记录变化时拒绝向替代进程发送信号。不可把任意非空未知命令当成已验证服务。

30项安装验收包含基线分角色/实际CPU BishengIR/个人下载/节点备份恢复，以及新原生模式新装、普通快照迁入、挂载覆盖、原IR单attempt完成、新提交503/follower零attempt等待、准确stop、uncover/restart、原生快照迁入第三组新盘及再一次真实IR。两个停止态SQLite导出由主助手独立核integrity和3个任务各1attempt，拒绝请求无job行；最终IR SHA一致。备份带原生合同，省略恢复合同拒绝，不自动落回普通目录。Root49同当前84源码，保持原开户/个人隔离/单attempt IR证据；真实宿主账号/PID1仍未验收。

保留失败：初始full860为4error/1fail（非法测试存储ID及旧stop mock缺元数据），修正后24 focused通过；安装r1拒绝UID降权到exec间隔，r2受限Root无法跨UID信号，r4测试Root无SYS_PTRACE读取environ失败（改为确切服务UID读，不加cap），r5/r6退出窗口采样错误。r3的29项通过属于tmp绑定/原生快照完整恢复之前范围；最终以r7及865为准，失败日志不改成成功。见私有 `evidence/control-filesystem-validation.json`；公开精选 `reports/public-source-check.json.control_filesystem`。

最终65个自有loop关联已释放，安装/开户/原型/diskproof容器及卷namespace独立只读确认empty，backings留存；未宣称此刻生产quota仍挂载。Main config SHA5caf...、19 Node profiles/物理域/CPU254–255/2048MiB/SHM1024MiB/1槽及zdl不变，0非终态/有效hold。角色内恢复态只保存在盘镜像与明确SQLite导出；不宣称宿主直接挂载完整恢复态。

仍需目标机真实账号、PID1及挂载传播、底层设备IO/掉电恢复、两物理机断网fencing、非root NPU数值/性能/仿真、审计增长预算/留存/外部告警、累计实验预算和五类工作流网页。保持单可写中央及稳定API/MCP，推广指南见SCALING.md；PUBLIC-001继续暂缓，整体goal active且production_ready=false。


## 控制存储余量准入、个人MCP与旧代码保护（2026-10-31）

**最终852项/166.934秒/无跳过及JS语法通过，82个非文档CORE与实际安装41、开户Root49冻结一致。** 全量job-a35f4ace8f67，安装最终job-77ba33829c92，开户job-bc1896dc8121。初始focused16项是最终MCP/预检/恢复补充前的探索范围；最终852覆盖全部最终测试源码。六个受保护运行文件保持v0.36哈希，固定工具镜像r4的APP4未变，不重建镜像、不重新调用模型或重跑旧编译诊断案例。

新增control_storage.py仅对中央状态目录FD做即时fstatvfs观测，不扫描、删除或缓存。新管理员安装默认minimum_free_mib=64、minimum_free_inodes=256，doctor在创建目录/账号前只读检查；缺少实现的旧包不能作为该新安装的代码。旧配置缺省None/disabled，不自动给个人Demo启用。类型/范围/未知字段严格校验，状态为disabled/ready/pressure/unavailable；只读或计数不可用时阻断新增工作。该快照不预占独享空间，不是原生硬配额、增长预算、告警或自动留存，已有工作/外部写入仍可能耗尽磁盘。

新任务/输入预留及未提交输入分块/封存均在原事务中检查；压力返回503/control_storage_pressure，不为逐次拒绝增加审计写入。已存在的任务幂等回执、输入元数据和已提交相同分块保留原ID/偏移；输入重试先核对原字节，再决定是否需要增长，不先mkdir。未消费的start票据返回wait而非失败或消费；claim保留原grant恢复，暂停新grant。已运行/授权任务的心跳、退出/超时、取消与结果上传/complete继续；真正写满的507仍由上一阶段Worker恢复原attempt。权限撤销仍优先拒绝，不能被容量等待掩盖。

GET /api/control-storage及jobs摘要提供授权只读状态，无路径/设备号/其他用户对象；网页显示字节/inode门槛和暂停原因。个人MCP queue_status附带同一状态，无新工具/命令/文件权限，旧中央缺字段时为null。维护激活和恢复都拒绝让已启用状态落到缺少实现的代码，保留原绑定。新文档见STORAGE.md/API.md/CLIENT.md，外部监控和生产控制盘硬边界仍需管理员准备。

实际安装41在自有128MiB backing ext4里复制并绑定新装中央停止态，服务UID999、effective/permitted cap均0。先以真实已发布497cbba源码的旧CLI构建旧包：启用态激活及新安装预检拒绝，当前绑定不变；再用旧CLI创建一个未启动的旧安装，以当前维护工具恢复启用态快照时在发布状态/改绑定前拒绝。原安装之后正常继续运行；没有创建宿主账号或启动第二中央。

真实非零字节压力下，已预留IR任务的原start票据重复wait/未消费，Node能领回同grant，新claim不增attempt；新任务/输入返回503，部分输入已提交3字节仍可重试但不能追加，取消排队任务成功。实际Root doctor拒绝低余量目标且不创建prefix/state。Node启动后等待原许可、无任务容器；真实打包网页显示相同暂停原因与预览，0提交。另装独立公共只读客户端，实际UID23642用本人私有profile读取同一MCP状态、路径覆盖拒绝；中央状态/socket不可读，UID23004不可读取其profile。恢复余量后原IR和follower各1attempt成功，原部分输入从3字节续至6字节并封存。

随后在大量空闲字节下创建受限数量的自有空文件，实际剩余inode低于256，新任务被拒、原任务0attempt；仅核对精确dev/inode和空文件清单后释放自己的填充目录，原排队任务恢复为单attempt成功。最后继续两轮实际全盘ENOSPC：CPU正常结束/本地超时都在Node完成清理，原租约保留、follower不抢跑；释放自有填充文件后原attempt完成续传/marker校验/ACK/GC。主助手另核显式停机SQLite导出integrity_check、7个执行任务各1attempt、取消任务0attempt、拒绝任务0行、输入6字节/sealed及原attempt不变。归档和回执为角色内live检查，角色移除后host只保留明确数据库导出，不能声称完整恢复态目录仍在host或原生quota仍挂载。

范围/失败保留：首轮job-e3767cf64a20为39项成功，当时仅mcp_bridge.py与最终82源码不同，不能称最终客户端验收。第二轮job-6969804bae95在full注入的复合“free==0且size<128MiB”断言失败，保持failed；当时即时读数未保存，停止后的自有镜像只读stat不足以完整还原该瞬间。最终注入记录每次真实ENOSPC，至多3轮只向同一dev/inode追加并重试原授权worker update，直到实际507，不提交替代计算。最终两例首次ENOSPC后分别仍见20KiB/12KiB余量，第二轮追加后实际更新507，验证严格free==0不能充当唯一判据。旧失败loop记录确认释放，backing文件保留。

最终Root49以同82源码、启用容量准入且余量正常的配置重新覆盖原49个SIGKILL/恢复边界；主助手核6个实际CPU IR任务均succeeded/各1attempt、原domain policy恢复。这不表示在开户各阶段注入过低容量，也不是实际systemd PID1/宿主真实账号。两个最终成功范围共10个盘helper/角色/卷独立确认不存在，loop关联释放，backing文件私有保留。原Main configSHA、CPU254/255、2048MiB/SHM1024MiB/1槽、19个Node profile/domain及zdl运行起点/内存/SHM不变，非终态/有效hold为0；Main明确disabled，旧Node未升级、inventory未豁免。

私有证据evidence/control-headroom-validation.json及runtime/control-headroom-20261031，公开精选reports/public-source-check.json.control_headroom。本轮0模型调用/0NPU kernel；历史operator31、compiler模型和数值范围保持冻结。下一优先仍为控制状态硬边界/留存与外部告警、累计实验预算、实际大IR/精度/卡死/仿真案例及五类网页提交；真实管理员账号/PID1、两物理机断网fencing和非root NPU工作流仍待，PUBLIC-001继续延期，overall_goal_complete=false、production_ready=false。

下方为历史阶段。

## 控制盘耗尽恢复与多服务器接入边界（2026-10-31）

**最终833项/160.064秒/无跳过及JS语法通过，81个非文档CORE与实际控制盘安装33、新镜像安装31的冻结源码一致。** 新增HTTP状态/权限边界专项检查，focused8项通过。全量job-95309786af8c，控制盘最终job-899c6cc36765，镜像回归job-a00718d820ae。六个受保护运行文件保持v0.36哈希；本轮未重跑历史开户Root49、浏览器或实际编译诊断模型案例，不把历史证据升级为当前验收。

新增SCALING.md并随可选文档打包，明确单中央/每机Node/个人harness、协作旧容器与管理员受控池、真实UID/HOME隔离、存储故障域、两物理机与fencing及约20并发的验收顺序。安装/客户端/工作流接口继续沿用；没有启动多中央、修改驱动、创建宿主账号或启用每日重资源回归。PUBLIC-001继续延期。

先通过原队列执行只读本项目元数据审计job-48b8f2fd3e42：131546个目录项、8.437秒、无错误，不跟随链接或嵌套挂载。三个控制数据库合计约4MiB，主要占用来自保留测试盘及产物，稀疏文件表观大小与分配字节分别统计。没有读取业务行值/凭据或删除历史证据。当前不因容量规划而清空审计；生产控制盘硬边界、告警、日志轮转及留存仍需管理员准备。

ClientHTTPError在不读取远端错误正文的前提下保留HTTP状态，原ClientError兼容。Node只将明确507映射为控制容量不可用，沿用原ControllerUnavailable恢复路径：运行中继续本地tick/退出/超时监督，未开始或上传中保留原grant/intent，不能据此标为执行失败、释放租约或重提计算。401/403/409/500/503仍保持原拒绝/异常处理；个人客户端不自动重试提交。原有网络丢响应处理不变。

实际安装演练在自有角色命名空间内，把新装停止态中央复制到128MiB backing ext4并bind到原状态目录；Node控制、有限spool和UID工作盘保持独立。实际中央UID999、effective/permitted capability均0。两轮分别启动一个实际CPU ir_v1，再将该控制盘填至真实ENOSPC：worker update和新提交均返回507，后者没有留下提交记录。正常任务在盘满时本地succeeded、超时任务在盘满时本地timed_out，两者均确认终止及清理，中央尚未收到回执，原租约仍在、各自follower为0attempt。仅按记录的dev/inode删除自有填充文件后，两者均从原attempt续传、校验、ACK和GC；原幂等请求返回同job/reused，marker一致，两个follower各执行一次。主助手另查显式停止态SQLite导出，integrity_check通过，四个任务各1attempt，原两次状态及清理字段正确，盘满拒绝的新任务为0。归档marker与ACK/GC是在角色内实时验证；角色移除后仅该明确导出在host保留，不能声称当前host保留恢复态目录或仍挂载配额盘。

lab_client.py属于固定工具镜像APP4，故按现有44个冻结wheel及相同基础镜像禁网重建r4，sha256:fcfd0b12fc8f40becfbb055136e4e9ef6a986e4e97295952b6708deb905b7f18。其余三个APP文件、工具包与基础镜像未变。实际安装31项重新覆盖非root任务UID23003/23004、独立个人UID23642、封存输入、禁网只读固定分析、错误锁0attempt及NaN CSV明确失败、只读MCP/越权拒绝、备份恢复后的原报告读取与新单attempt报告。主助手另核初始持久DB、两份实际环境/工具文件回执与镜像SHA。旧r3及旧模型案例保留历史范围；没有调用模型或做新的性能优化测量。

两次前置演练保留failed：job-1906cc994fc5在注入前读取恢复前Node目录；job-38d5ae82cdc9已验证本地正常结束/清理但随后错误读取控制目录中的旧产物路径。修正测试定位至恢复后的Node控制目录和独立spool后，第三轮完整通过；未放宽归档、权限或租约条件。各自loop记录均已确认释放。两个成功范围的5个自有盘helper、角色和卷已独立查询不存在，loop关联已释放；backing文件保留作私有证据，不做泛化清理。

非operator安装演练按原入口暂时停止并恢复本项目Main，已有任务由独立执行器继续；最终核对原config SHA、CPU254/255、2048MiB/SHM1024MiB/1槽、19个Node profile及domain均不变，非终态与有效inventory hold为0，zdl运行起点/内存/SHM不变。旧Node未自动升级、inventory未豁免。证据evidence/control-storage-validation.json仅本地，公开精选reports/public-source-check.json.control_storage；本轮0模型调用、0NPU kernel。

下一优先仍包括生产控制状态预算/留存与外部告警、累计实验预算、实际精度/卡死/仿真案例及五类网页提交；管理员试点需真实账号、systemd PID1与非root NPU访问/数值/性能/仿真，再接第二物理机及断网fencing。当前同机数字UID/CPU故障演练不替代这些验收，overall_goal_complete=false、production_ready=false。

下方为历史阶段。

## 实际MLIR版本接口诊断与受控候选闭环（2026-10-31）

**最终831项/158.358秒/无跳过及JS语法通过，81个非文档CORE与最终full及实际安装包冻结一致。** 新增13项编译诊断/候选边界检查；最终focused job-1059188e6ce3为13项/22.004秒，全量job-7438a5d01dd8。实际安装job-7d809703c4a4为33项通过。本轮未重跑上一阶段80源码的开户Root49或浏览器；原owner逻辑未改，当前新增可选客户端/诊断代码由安装33与全量831覆盖，不能把历史49改称81源码新验收。六个受保护运行文件和原性能镜像APP4均未变，无需重建性能工具镜像。

实际预探使用现有固定工具镜像、UID23900/只读rootfs/禁网/512MiB/无设备：Triton工具LLVM22.0.0git拒绝bufferization.to_memref、接受to_buffer；BishengIR2.0.0（44b7bd25ae7a，LLVM19.1.7）接受前者、拒绝后者。第一轮job-2bdb834aee36的Triton --canonicalize在参数解析阶段就失败，不能当作IR错误；保留该结果后，job-7e4d6e1772d8改为parse-only才取得配对矩阵。上游LLVM8f91b108df21bee8ee64e88ffa7bdee230389800有对应NFC重命名依据。本例是结合工程背景构造的最小IR，使用真实固定工具，不是原始大kernel或完整lowering修复。

新examples/workflows/compiler_compatibility.py是普通ir_v1封存载荷：显式工具名/规范镜像路径/SHA，工具还必须匹配受保护请求锁；只parse/verify和输出IR，无任意pass/插件/shell参数。驱动按块哈希大工具文件，输入IR<=8192字节；失败前仍保存原IR、驱动/工具SHA、实际版本、调用及有界日志，并退出非零，不将“捕获错误”算作工作流成功。成功需真实IR产物并通过原结构化结果合同。

新compiler_diagnostic.py与labctl compiler-case提供单任务只读CLI/MCP，复用原通用收集器但不改变它，固定读取编译报告、完整小IR和stderr。报告、归档、封存input.mlir及封存驱动SHA相互绑定；另核对当前客户端认识的驱动、请求工具锁和完整受保护环境回执。源码脱敏/缺失、未知或清理未确认、旧未知驱动、工具事实不匹配时，不进入已知字面候选检查。任务元数据也在交给harness前脱敏。MCP仍仅空参数get_diagnostic_case，不接受路径/任务覆盖、提交、命令或修复操作；job ACL在任何领域产物读取前检查。客户端新增compiler-case-v1及载荷脚本，无SDK或模型配置安装。

当前安装演练复用上一阶段固定image f9a049...，探针额外认证两工具文件；两个任务UID/性能分析/独立个人客户端原范围继续通过。新增四个真实单attempt IR任务：旧工具+原IR succeeded；新工具+原IR failed（原失败保留）；单处候选重新封存后新工具succeeded；同候选在旧工具failed。Node实际UID23003、512MiB、无设备、禁网/只读rootfs、退出及工作区清理回执都与环境证据交叉核对；host独立读取原持久DB和四套源码/报告/运行环境/受保护回执，确认状态、次数及工具/输入摘要。个人UID23642运行已安装CLI及真实stdio只读MCP，其他owner不能读原失败任务。

一次既有授权DeepSeek261017的OpenCode run（agent-15069052d3b8）仅整理主助手已取得的IR、实际错误、配对观测和预先批准的局部迁移依据；全工具deny、MCP空、工具0/错误0/退出0，进程已确认结束。模型草稿不能直接执行；review_minimal_rename只检查原失败已确认终止/清理、完整未脱敏源码SHA、已知封存驱动、受保护工具锁、目标和精确单处替换，以及NPU/数值/全流水均未证明。主助手事先建立迁移依据，之后核对草稿表述；候选使用新input ID、固定CPU预算和唯一键提交，原失败仍failed/1attempt。模型没有发现未知bug根因、改共享代码或操作设备，本轮不宣称一般自主诊断。model_invocations按OpenCode run计1，供应商HTTP请求数未单独测量。

安装器doctor、升级/兼容回滚、归档、备份及恢复继续通过。恢复后原失败记录和证据可读，同一封存候选再单attempt成功，无第二次模型run。该恢复后state位于角色文件系统，正向由角色存活时的实际API/执行检查冻结进父报告；host不保留其新目录，不能宣称现在可独立读该目录。自有两盘helper/角色/卷已独立确认清退，backing文件私有保留，native quota目前未挂载。

失败与修正保留：首focused job-cdda0037506e为27项/1error，fixture假定needs_attention仍可读终态归档，实际原接口正确409；改测试并保持门禁。增加封存/驱动/受保护工具绑定后job-b3d84504675a为29项/24.239秒通过；最后任务元数据脱敏另由最终13与831覆盖。性能独立live脚本的新增flat载荷源路径别名已修，本轮未单独重跑该旧脚本；实际安装已运行性能与编译两套个人CLI/MCP。

Main仅在0非终态/有效hold时重载；原config SHA、CPU254/255、2048MiB/SHM1024MiB/1槽、19Node profile及domain保持不变，旧Node未升级、inventory未豁免、zdl运行起点/内存/SHM不变。无NPU kernel，无共享安装、用户项目或模型全局配置修改；没有永久新增分析Node，隔离角色已清理。完整私有evidence/compiler-diagnostic-validation.json与runtime/compiler-case-20261031，精选reports/public-source-check.json.compiler_diagnostic；原始日志、凭据、会话和盘不发布。

下一优先转控制盘增长/审计留存与一般资源合同，继续真实大IR/端到端失败与有界reducer、跨阶段IR方言/bytecode和CLI能力合同，以及累计实验预算；不要只重复打包和小样例。五类workflow网页提交、真实精度/卡死/仿真流水闭环仍待。实际宿主用户/PID1、第二物理机/fencing、NPU ACL/数值/性能/仿真门槛保留，不无变化重试旧EBUSY。PUBLIC-001继续延期；overall_goal_complete=false、production_ready=false。

下方为历史阶段。


## 固定工具镜像、受控Node配方与两UID分析（2026-10-31）

**最终818项/132.308秒/无跳过及JS语法通过，80个非文档CORE生产源码与最终full、实际安装包及Root49冻结一致。** 六个受保护运行文件仍为v0.36哈希。新增10项配方/镜像上下文边界检查；先前focused39项/9.701秒通过，其后添加的CLI machine导出由最终full与已安装实际CLI覆盖。最终unit job-97b3725a7504，私有evidence/operator-node-validation.json，公开精选reports/public-source-check.json.operator_node。

operator_workflow.py作为轻量个人客户端的一部分，使用封存input ID和受保护环境证据导出的锁，生成固定CPU报告请求；不自动提交、下载或联系模型。复用现有build_v1的封存输入/环境锁/产物合同，明确purpose为offline_operator_report_generation，不宣称执行了编译或新增第六种设备工作流。固定1CPU/768MiB/0SHM/0NPU/180秒、只读输入、有限持久工作区及2/4MiB归档。lock必须有CPython3.11/x86_64/不可变镜像、精确工具版本和当前应用/上游源码SHA；labctl environment-lock现导出machine。旧普通workflow参数及提交接口保持原合同。

新增固定镜像入口operator_node.py及离线Docker构建配方。44个wheel按接受过的包报告冻结精确版本和SHA；准备脚本只复制四个应用文件、Dockerfile、锁及完整核对过的wheel，拒链接/额外/缺失/重复/超限，不读凭据或调用Docker。镜像使用已有自有Python3.11.15/glibc2.39工具链基础镜像，RUN禁网、安装后应用/venv只读；没有修改基础镜像或其他人的容器。最终镜像为sha256:f9a049c9f1ab7241c63cafd09eb8788ea54f7643fd74bd0bce8383e253a5e674，镜像内四个应用文件SHA匹配当前源码。构建器本机命令按自有CPU/1GiB限制运行，legacy builder选择仅作用于该命令环境，不改全局Docker配置。别机需要管理员准备适合的固定基础镜像、独立重建和环境验收，不能复制本机路径假定通用。

工具子进程补Python -I -B：固定入口与上游msprof MCP、delta脚本避免输入目录同名模块进入导入路径。只调用已固定的CSV工具，生成原operator-analysis JSON/CSV，然后按既有workload-result声明两个产物；没有给模型原上游SQL/采集工具或Docker权限。个人客户端无需pandas/MCP SDK。使用与限制见OPERATOR_ANALYSIS.md。

最终实际安装演练job-bdfc7152b1c6 **31项通过**：在自有角色中安装非root中央、Root Node，原生UID字节/inode工作区、独立有限spool；固定CPU探针按实际非root任务UID读取镜像文件/包并由管理员确认环境、同步合同和新鲜Node心跳。两个实际任务UID23003/23004分别执行固定镜像分析；UID23642持有自己的私有profile和独立公共只读客户端，实际导出含machine的环境锁、生成请求（0提交）、通过stdio只读MCP读取报告，提交工具/任务覆盖被拒，其他owner不能读取输入/报告。

真实历史数据沿封存输入执行，封存后的本地副本修改不影响结果。输入额外放置msprof_mcp/__init__.py和csv.py的故意失败模块，固定入口仍成功且无shadow标记，验证隔离导入。错误工具锁保持queued/0attempt后取消；host独立对比持久合同确认server.py哈希不匹配。有效封存的NaN CSV经过真实单attempt明确failed，无成功分析报告；两正常报告各单attempt、JSON/CSV和原日志/环境回执归档，历史耗时比继续不能当优化收益。受保护回执locked/workload_started/exit0与Node采集的实际UID、无设备映射、禁网、只读rootfs、768MiB限制、UID原生quota和清理回执交叉核对。

安装器doctor、升级/回滚、归档和备份恢复继续通过；恢复后原分析报告可读、原input可复用并完成一个新单attempt报告。恢复后的正向是在角色内实际API/运行时检查后冻结到父报告；其新state位于角色文件系统，容器移除后host不保留原目录，不能冒充现在仍能独立读取该state。初始两正常/一失败/一取消的原持久DB及正常两份实际环境回执由host独立核对。自有两个文件盘helper/角色/卷确认清退，backing files私有保留；不宣称当前还挂载native quota。

最终开户回归job-286f87cb5d25同80源码的原 **49个SIGKILL恢复点通过**；host独立数据库6个实际CPU IR均succeeded/各1attempt，原domain policy恢复，7盘helper/角色/卷清退、backing files私有保留。两个Root范围均为同一物理机内的数字UID与角色服务，不是宿主真实账户或systemd PID1。

失败保留：初始锁来源检查只接受HTTPS URL，已接受的msprof本地wheel按其确切公开SHA显式识别后才生成锁。首个镜像在受控执行前加隔离导入后重建。首安装job-60b64e58c41c在固定probe因私有权限的app文件拒绝读取，分析未启动；修正镜像内目录/文件读取权限、保留严格probe后重建。第二安装job-e923c40413f7的探针/IR通过，但独立个人UID被中央私有目录中的客户端路径正确拒绝；改装单独公共只读客户端，未放宽中央目录权限，最终演练通过。两失败演练的自有盘均已核释放，无设备重试、驱动重置或原任务假成功。

Main未为安装演练停机，仅最终0非终态/有效hold时重载。原config SHA、CPU254/255、2048MiB/SHM1024MiB/1槽、19个Node profile及domain保持一致；旧Node不升级、inventory不豁免、zdl运行起点/内存/SHM不变。本轮0 NPU kernel/0模型调用；上一阶段1次DeepSeek摘要保持旧范围，未重用其结果当本轮运行验收。

下一优先：真实编译错误/精度错误/卡死及仿真流水诊断案例，预算内提出实验→中央排队→跟踪原attempt→归档闭环；控制盘增长/保留与一般资源合同、五类冻结workflow网页提交仍待。管理员试点先两名真实用户/专用卡/实际PID1，再第二物理机与断网fencing。完整msagent harness、NPU访问/ACL/数值性能仿真仍未验收，别无变化重试旧EBUSY。PUBLIC-001继续延期；overall_goal_complete=false、production_ready=false。

下方为历史阶段。


## 离线领域工具接入与中央通信中断恢复（2026-10-31）

当前最终完整检查 **808项/141.079秒/无跳过，JavaScript语法通过**。78个非文档CORE生产源码与最终full/Root冻结一致；六个受保护运行文件继续保持v0.36哈希。新增22项算子报告检查、6项通信恢复检查、2项自有清理范围检查。原网页11组仍保留此前静态资产验收，本轮未重新运行浏览器。

真实工具接入：独立CPU队列任务实际调用msprof-mcp0.1.8/MCP1.30.0的stdio `get_operator_details`，独立CSV解析交叉核对，再运行固定msagent提交5a9b2a1a8a38f35cc63109929850aa4a46db090e中的未改动delta_report.py。原项目文件只读，输入副本、来源和代码哈希私有保存，结果JSON/CSV归档。完整msagent、自动采集/上板/优化和正式Node工具镜像未安装或验收。安装依赖集中在本项目tools独立环境，不改共享Python或其他项目。

实际历史MergeTopK数据每份30次，均值563.0341667μs与33.5669667μs，CSV Device_id为0/1，回放记录的物理卡也为0/1，核心频率为100/1650MHz，声称同一ELF。报告保留16.77345982的耗时比，优化收益为null；上游比较脚本因物理卡不同返回2。CSV列值单独保留，不据其名称推导逻辑/物理映射；历史精度声明不是本轮精度认证。这不是新的NPU测量，也不说明当前设备仍有历史频率。

独立分发客户端增加`labctl operator-case`和单任务只读stdio MCP，只暴露无参数get_diagnostic_case。实际新目录安装、所有分发文件SHA、CLI与真实initialize/list/call通过；先按服务端ACL读任务，再读固定完整归档、校验SHA/schema并递归脱敏。其他身份、任务覆盖和提交工具被拒，读取前后无新增任务，个人端不导入上游分析依赖。最终生产者job-3c04b2b6e7b3单attempt成功，报告生成器SHA匹配最终源文件。接口与用法见OPERATOR_ANALYSIS.md。

使用opencode-simple-tasks的受限材料整理流程，按用户指定沿既有zdl模型配置实际调用一次DeepSeek（配置显示DeepSeek-V4.1-Flash261017）。最终权限检查全工具deny、MCP为空，工具调用0、错误0；只提交已经核实的最小历史数据，不委派设备操作或代码修改。原模型材料使用了过度具体的logical_device_ids字段名；主助手审阅后明确改为中性的CSV列值语义。原材料/草稿保留，之后仅代码字段改名与最终CPU报告重跑，未再调用模型。不能把这一次摘要当成完整msagent或自动bug诊断验收。

Root回归实际发现并修复通信窗口问题：控制器重启时一次连接失败曾让Worker退出原任务监管，后续演练清理又在任务仍运行时修改回执属主。现在连接/不可用响应有独立ClientUnavailable/ControllerUnavailable类型；运行中继续检查原容器的退出和本地超时，启动前或传输中保留原grant/意图，恢复沿同attempt处理；HTTP授权拒绝仍失败关闭。没有用网络超时推断执行失败、释放资源或生成替代任务。

清理器只核对本演练可信CPU grant/原数据库attempt、固定镜像和精确容器标签，先停止并确认Running=false/Pid0，再修复文件属主与卸载文件盘。额外真实微演练job-0c9081076cf8保持自有CPU任务及文件盘句柄运行，验证上述顺序、0600回执内容保留、原grant SHA不变、文件盘释放；使用合成本地grant，不是调度成功回执或任务重试。

最终Root job-ac67e5789873原49个SIGKILL恢复点通过。host独立读取持久数据库：6个实际CPU IR任务均succeeded/各1attempt，原domain policy恢复；旧用户运行期间新用户激活/个人排队及第二Node IR继续通过。7盘helper/角色/卷独立确认已退出/移除，backing files私有保留。/opt和/personal仍为角色tmpfs，Root数字UID不是宿主真实账号，当前没有挂载原生quota供重新查询。未验真实PID1、第二物理机或NPU计算。

失败保留：首次上游依赖解析到MCP2.3.0、FastMCP导入失败，另建固定MCP1.30.0环境后实际通过；首轮报告将JSON整体脱敏破坏结构，改递归字符串脱敏后通过。首Root job-9c1b8ba743c7保留失败与原needs_attention租约历史：其自有任务退出1后，独立核对退出/原label/盘身份，再清退准确对象；没有篡改历史完成态、盲目释放或重试原任务。首通信focused的3失败2错误为mock路径/未注入Docker callable，修正fixture后8项通过。最终full为job-ab3b47af3abb。独立证据汇总脚本初次误把kind当template，改按实际ir/ir_v1后读取6项通过，不影响生产源码或回归结果。

Main只在0非终态/有效inventory hold时重载；config SHA、CPU254/255、2048MiB总内存、1024MiB SHM、1槽、原19个Node profile及domain完全不变。旧Node未升级、fresh inventory不豁免，既有zdl运行起点/内存/SHM不变。本轮0 NPU kernel、1次受限模型摘要。完整私有证据evidence/domain-tools-validation.json，公开精选reports/public-source-check.json.domain_tools；原始输入、模型配置/会话、测试盘不发布。

下一步优先把分析生产者落入固定托管Node模板和已认证工具镜像，补真实编译/精度错误及卡死/仿真案例，再推进预算内提出实验→排队→观察原任务→归档；同时补控制盘增长/保留、一般资源合同、五类工作流网页提交。首管理员试点依次验证两名真实用户/专用卡/实际PID1，再第二物理机与断网fencing；保持同一API/MCP和单中央权威。PUBLIC-001继续暂缓，overall_goal_complete=false、production_ready=false。

下方为历史阶段。


## 网页可运行性预览与请求生命周期（2026-10-31）

“托管作业”当前表单新增“检查可运行节点”，与“提交到队列”独立；两者共用jobRequest构造，保留原提交幂等键。预览仅请求原POST /api/jobs/preview，不创建任务或归档，也不因blocked而禁止用户显式排队等待未来节点/环境。候选和原因用DOM textContent显示，未映射peer不进入预览候选（已有授权共享节点总览仍按原范围展示）。只读凭据禁用两按钮，服务端原授权不变。

表单变化、身份/提交权限变化和显式提交使旧预览失效；AbortController取消旧读取，序列/身份/请求指纹检查防迟到结果重绘。读取15秒未完成时提示可重试，成功结果15秒后标记历史；这不是任务执行时限、资源预留或开始时间承诺。503等读取失败不回退真实提交。

**最终浏览器job-8e019f3ca141：11组检查通过，browser errors=0、stderr干净，JS语法通过。** 从实际生成的服务发布包加载最终三个网页资产并验证SHA，测试真实HTTP/API；另通过当前Main网页实际预览，HTTP200/0.108秒，前后无新增job。桌面1440与手机390截图保留私有，已检查布局无横向溢出。验证包含真实15秒历史标记、真实服务端延迟导致请求超时/重试、表单修改及提交期间的迟到结果、授权按钮、候选隐私范围、恶意字符串文本显示、失败不提交，以及blocked仍能显式排队。自有fixture两逻辑用户、合成CPU节点观测，两任务均queued后取消/0attempt；不构成OS用户、NPU或真实Node执行验收。

失败及前期证据保留：首轮job-5b8887356afe为原9组通过。加入真实过期计时后job-935416191a85在最后Main浏览器读取超出测试原5秒等待，随后直调同API0.02秒，原因未证实；当前UI补了15秒超时/取消，测试等待有界结束。job-c279c9c9c685的11功能检查通过、Main HTTP200/5.39秒，但故意悬置Playwright route在清理时报CancelledError；改用事件延迟实际fixture服务端读取，最终无此回调告警。不把超时处理称作后端性能修复。

77非md后端文件及所有已发现unit源码均与上一阶段778项/Root49基线完全相同，本轮未重跑重型后端全量或Root。当前网页SHA由实际浏览器验证；测试包中网页是最终资产，文档后续更新，不把此当新整包Root安装验收。Main配置/物理域/19Node profile不变，未重启中央、未升级旧Node、无库存豁免；最终0nonterminal/有效hold，protected6仍v036原SHA。0NPU kernel/模型调用。证据本地evidence/web-preview-validation.json，精选reports/public-source-check.json.web_preview。

下一：真实日志/IR与领域工具的诊断闭环、五类冻结工作流网页提交；控制盘增长/留存、一般资源合同和真实管理员部署验收继续。已有网页结果/状态并不表示五类工作流JSON提交入口已实现。实际PID1/宿主用户/两机fencing/NPU ACL与精度性能仿真、msagent真实能力仍待；PUBLIC-001延期，整体目标未完成。下方为历史阶段。


## 本人任务的只读可运行性预览（2026-10-31）

新增POST /api/jobs/preview、labctl submit --preview、MCP preview_job。沿原normalize核对本人输入、环境、任务及资源授权，再用query_only账本读事务列本人已映射Node。静态容量仅检查Node/物理域共同CPU、负载内存+128MiB、SHM上限；当前心跳、停用、模板、并发能力、交接、Docker/环境能力门后，复用真正Node placement与现有排队保护。库存检查现在复用传入的账本连接。个人协作入口复用本地Scheduler placement及现有采样。

输出eligible_snapshot/blocked/no_visible_node和每候选单个当前阻碍；不输出未映射节点、他人任务/UID/目录或候选分配。容量fits不是完整环境匹配证明，不把所有等待叫作临时忙碌。无启动保证或ETA，不创建job/attempt、归档或资源预留、保护意图，也不执行输入交接/设备探针。身份库、控制账本和Node采样不是全局原子快照；正式提交额度及启动时重新检查。普通未绑定任务可以等待未来Node/新环境，未因预览无候选而拒提交；固定目标探针保留原静态拒绝规则。旧validate接口不变，预览不代替旧幂等任务/job ID跟踪。

MCP预览与提交共用同一请求构造、同一固定会话约束，模型不能改命令、输入、锁或资源；预览不写submitted-request或消耗单任务名额。CLI不记录个人归档上下文，拒与归档参数组合。网页按钮仍待。

**最终778项/123.119秒/无跳过和JS语法通过；77非文档生产源码与最终Root/full/focused冻结一致。** 系统Python focused job-1fe5f13ae1fc为21/3.333秒，覆盖真实HTTP、CLI、会话MCP、授权/不可写连接、原账本/目录不变、共享物理域满载、原attempt保留、CPU交集与开销、过期/未来心跳、隐私/撤权、未来Node排队、环境认证/锁差异及固定探针拒绝。初步21/3.473秒同样通过。首轮完整job-3ce62af8e81b为778项1失败2跳过：旧MCP协议断言固定7工具，改验8及preview只读声明；默认Conda Python缺pidfd，最终用与安装器一致/usr/bin/python3，0skip。失败保留。历史live_client脚本的工具数同步为8，本轮没有重跑该旧完整脚本。

Root job-e091e976bccb通过原49个SIGKILL恢复点/开户排空/并发身份交付/原CPU容量拒绝/第二Node等回归；host独立读取持久控制元数据，6个CPU IR任务均succeeded且各1attempt，原domain policy一致。七盘loop/helper/角色已清退，backing files私有保留；/opt和/personal仍角色tmpfs，不称host持有其历史或当前挂载quota。无NPU计算或新模型调用。

Main在0nonterminal/有效hold时stop/start加载；原configSHA、预算、19Node profile和domain不变，旧Node未升级、库存无豁免、既有zdl不变。另在全新独立客户端目录实际运行CLI和stdio initialize/list/preview：8工具、主服务eligible_snapshot，前后job数不变、session jobs空且无submitted-request、安装目录无runtime。旧服务404时也不回退提交。最终全量job-a0c89dc367ab。完整本地evidence/placement-preview-validation.json，精选reports/public-source-check.json.placement_preview。

下一步：网页入口及更全面的结构化阻碍说明、控制盘增长/保留与资源合同演进，然后领域harness真实诊断案例。真实管理员宿主用户、PID1、两物理机与断网fencing、NPU ACL/精度/性能/仿真仍待；预览和同机Root不能替代这些验收。PUBLIC-001延期，整体目标未完成。下方为历史阶段。


## 固定探针与开户前的静态容量准入（2026-10-31）

resources.require_managed_capacity共用一CPU/零SHM的静态可运行检查：Node与物理域CPU须有交集，负载内存+128MiB托管开销须同时不超过两侧预算；不扣减当前占用，不把draining当静态不可能。512MiB Node上384+128恰好可用，385+128在新授权/任务前拒绝。用户最大额度可大于单个Node，按实际探针内存判断。

owner_provision._nodes使用同一只读事务快照查目标profile和domain；每Node至少容纳256+128MiB CPU任务，显式device_probes按选定memory_mib验证，在主journal/身份/原生资源分配前拒不可能配置。管理员预激活_current在授权前及之后原授权验证时复核。JobStore.submit在原幂等返回之后、任务/归档预留/目录创建之前，于提交事务内调用Nodes.probe_submission：设备及环境固定探针须目标已登记启用、当前owner已映射、模板已授权，设备也须在合同内，并再次检查静态CPU/内存。

满载但足够容量仍正常排队；原活任务/未知租约不释放。旧幂等请求观察原job；配置在授权与入队间缩小时不写job或归档预留，保留原authorization/job=None意图，恢复合法容量可沿同operation继续。开户预检只是当前配置快照，不承诺后续配置、健康、超时、磁盘、环境一定满足。普通未绑定工作流的可运行节点集合预检仍待评估。

**最终757项/125.228秒/无跳过及JS语法通过；77非文档生产源码与最终Root/full/focused冻结一致。** focused job-57d111334f59为18/2.151秒。新18项覆盖Node/物理域边界、共同CPU不足、满载排队、普通设备/环境probe同规则、未登记/未映射/未授权模板/越界卡、CPU和NPU开户前拒绝、静态drain与实际准入分离、旧幂等/原staged job不替换、配置竞争下原授权保留、65536MiB+128上界及非法参数。合成设备观测不是设备打开或kernel。

最终Root job-3f881608a2f5：在自有隔离服务中通过真实管理员API将domain内存降到256MiB，已安装Root开户CLI明确因256+128不能放下而在主journal/身份前拒绝；原policy恢复后原49个SIGKILL、drain/abort、UID23801运行期间激活交付UID23802与实际个人IR排队/单attempt、第二用户单CLI/peer拒读/Node2 UID23812 IR等均通过。host独立核对capacity-original-domain.json、domain事件含256MiB及最终policy相同（修订递增保留审计），并核两IR时间顺序/单attempt和最终token/禁用状态。七文件盘loop/helper/角色/卷释放，backing files留private；/opt和/personal角色tmpfs不留host，未把host记录核查说成当前nativequota读取。

最终全量job-0c2873c72a86。基线repro job-8d5cd54ed6d5正确显示385+128被旧512MiB Node接收而永久排队。focused job-33550bd5188c的18项只有fixture异常类型错误：未知owner接口是AccessError，fixture误写JobError；改查identity DB确认不存在后最终18通过。既有environment probe目标测试改为真实已登记的另一Node，保留“不能被本Node领取”原含义；未登记目标现在按新规则提前拒绝。完整证据本地evidence/probe-capacity-validation.json，精选reports/public-source-check.json.probe_capacity；失败保留，未放宽预算。

Main空闲0nonterminal/有效hold时重载，configSHA/CPU254,255/2048MiB/SHM1024MiB/1槽及19Node profile SHA不变；旧Node未升级、fresh inventory无豁免。0NPU kernel/模型调用，protected6仍原v036SHA，未动共享驱动/宿主账号或既有zdl。

下一优先：普通工作流的可运行节点集合预检与面向用户的可解释准入，再完善领域harness/msagent诊断能力的实际接入。实际PID1/宿主真实用户/两物理机、NPU访问资格继承/ACL/数值/性能/仿真、远程交付/fencing、一般资源合同与控制盘硬边界仍待。PUBLIC-001延期，整体目标未完成。下方为历史阶段。


## 追加无关用户后的资格兼容审计（2026-10-31）

新增qualification_compatibility.py，中央stage批准与active-owner扩Node授权均在jobs→identities写保留内冻结原资格、成功固定probe来源、旧完整profile/新profile SHA、当前个人权限单调修订。只证明单个新用户及各一个原生work/cache槽的精确追加；旧UID/GID/槽及所有非追加字段必须相同。全局policy_revision继续递增并进入原受保护context，六个运行文件/设备协议无需改版；不是删除版本校验。

原ready交接与新鲜健康Node心跳到位后，比对当前boot/驱动/Docker及protected runtime、旧context、原资格和权限修订，再关联新context。原job/attempt/receipt/recorded_at/expires_at不变、不发新probe、不续30分钟有效期。连续追加保留原probe_context及此前兼容指针。新增用户/禁用stage/失败/过期/来源变化的资格不继承；首次不匹配记录skipped，后来恢复原值也不会再自动继承。等同域其他Node的当前库存时保留pending，并阻配置变更/离线维护/备份；原心跳和中央重启可恢复。资格、Node观察与完成审计在同一事务，重复HB不续期，备份恢复仍使verified过期。存在审计后拒缺少模块的旧中央包。

新管理员只读CLI workerctl node-qualification-compatibility NODE --limit/--offset返回历史计划计数，不输出旧profile/路径，不代表当前卡资格；当前状态仍用node-device-status。最多1024计划/4096候选每计划、每记录预留2MiB、总128MiB，超限明确处理历史，不自动删数据；不冒充SQLite/控制盘内核硬配额。

**最终739项/119.284秒/无跳过及JS语法通过；77个非文档生产源码与最终Root/全量/最终focused冻结完全一致。** focused job-307f6ad5c663为24/3.877秒。24项包括精确追加与设备/用户隔离、旧expiry/receipt不变、连续两次真实中央批准及保留历史、普通accuracy任务取得新context/旧卡grant且无新probe、active-owner扩Node、权限改后恢复、驱动变化后恢复、缺观测、被替换资格、expiry、policy实际API改后恢复仍需新probe、两受控事务异常回滚与重复HB、身份写锁、重开JobStore恢复、源意图篡改、审计预算回滚、恢复备份不复活、同域peer库存等待后正常确认、本地摘要CLI。设备观测和原生盘描述为合成可信协议，未执行设备打开或kernel。

最终Root job-2a550b58921b succeeded：原49 SIGKILL、忙队列排空/中止恢复、UID23801运行期间激活交付UID23802及个人实际IR排队/单attempt、第二新用户一次CLI、peer拒读、Node2 UID23812实际IR等均通过。已安装审计CLI读CPU节点空历史通过；host独立确认qualification_compatibility=0/device_qualifications=0，不能据此说Root/NPU资格继承正向通过。七文件盘loop/helper/角色/卷释放，backing files留private；/opt和/personal仍为角色tmpfs，host仅核持久DB/Node metadata，不宣称当前挂载quota或留存Root历史。

最终全量job-22adfa68b212。初始repro job-73b5d5b4dce3因fixture旧Alice默认NPU0在正常配额门拒绝；显式设置合法额度后job-f85eab8828c1正确复现无关追加导致stale。初步focused job-33f7bc0d4954为1/0.148秒；job-b519f1a5ffc6的19项有两个fixture错误：新计划rev4仍用旧硬编码回执rev2、错误引用JobStore.path；按原计划取回执版本与真实runtime DB路径修正。job-b45b01c2ef98为22/3.695秒，最终另补peer等待和CLI两项至24。失败留存，不放松准入。完整证据本地evidence/qualification-compatibility-validation.json，精选reports/public-source-check.json.qualification_compatibility。

Main空闲0nonterminal/有效hold时重载，原configSHA/CPU254,255/2048MiB/SHM1024MiB/1槽及19Node profile SHA不变；新表为空，旧Node未升级、fresh inventory无豁免。0 NPU kernel/模型调用、protected6仍原v036哈希；未改驱动/宿主账号/既有zdl。

下一优先：固定预激活探针内存须在stage前验证Node/物理域静态运行边界（含128MiB开销），不要把临时占用当成不可能运行。真实NPU资格继承及ACL/数值/性能/仿真、实际PID1/宿主真实多用户/两物理机、远程交付/fencing、一般资源合同及控制盘硬边界、组织身份/msagent仍待。PUBLIC-001继续延期，整体目标未完成。下方为历史阶段。


## 已知任务并行期间的身份激活与个人排队（2026-10-31）

最后激活不再复用结构交接的全Node空闲门。其他用户的已知reserved/running/cancelling任务可继续，原attempt、分配和资源账本不变；满载只让新用户任务排队。结构变更/绑定/ACK仍要求静默；同域未知执行（即使相关Node已禁用）、有效库存hold/过期轮次、待完成交接、draining、环境/存储或目标用户槽位异常仍阻断。其他物理域的其他用户未知任务不自动隔离本域。本用户自身排队/活动预激活任务会阻止最后改变身份，避免遗留授权失效任务。

固定预激活探针授权显式使用identity_change=False：只核当前可安全排队的合同，不改变身份，可与已知工作并存或为另一候选卡显式新建操作，仍禁用用户、无凭据、经原资源/归档账本。原完整候选计划仍串行、同操作仍同job，未自动增加探针或放弃原租约。NPU正向仅合成可信协议。

新增活中央库存轮次核对：本地Svc管理命令无进程内controller_instance时，读受保护server-ready，核活PID/启动身份、同服务UID、能力和32位轮次，再把预期轮次交给Inventory.reason。所有启用同域Node须已确认当前轮次，命令对象的已有冲突轮次、缺失/旧报告、检查期间轮次切换均拒绝；不改JobStore轮次、不伪造Node已对账、不豁免fresh inventory。

**最终715项/127.602秒/无跳过及JS语法通过；76个非文档生产源码与最终Root及全量冻结一致。** 新19项涵盖真实账本的旧任务保留/新IR排队授予、reserved/cancelling、目标与其他用户槽位、同域未知/禁用peer、独立域未知、结构交接仍静默、pending/drain/hold、全域库存轮次及管理轮次切换、预激活probe排队/自己的queued和reserved阻激活、显式另一候选排队与已有NPU资格并行激活。单位检查中的设备观测是合成，不是NPU硬件计算。

最终Root job-772c811b1692，原49个SIGKILL恢复点仍通过。旧用户UID23801的实际IR在运行时，新用户完成激活及UID23802个人交付；该个人沿交付profile认证、封存输入并提交实际IR，在满载原Node queued/0attempt，随后复用同CPU且各单attempt成功。独立host DB核：old job-938f4c7c6c38运行21:05:52.811955→21:06:25.039528 UTC，激活21:05:53.424601 UTC；new job-715c628a4f87在旧任务结束前提交、21:06:25.783685启动，两份锁定环境IR结果passed。个人/任务UID由角色内实际脚本验证，host核对持久任务/激活/身份/Node记录，不宣称host留存/opt Root历史或当前挂载quota。原排空、第二新用户单CLI、peer拒读、Node2 UID23812 IR及历史禁用重放仍过。七文件盘loop/helper/角色/卷释放，后备文件留私有。

最终全量job-50b6c1bf6358；测试源摘要在其仍queued时冻结，最终19项全通过。失败保留：repro job-b5e4b424f0f6是旧fixture占位spool不能签发真实grant；修正完整合同后job-814650bb29e4正确复现旧全Node空闲误阻。focused job-29a0fc5b44f6三个fixture问题为peer缺name、cpu_demo默认128MiB产物超过stage预算，以及重复claim被误当新分配；改合法登记、有限IR合同和exclude_attempts后job-6090efdb1cc2为12/2.295秒，增peer后job-a08ad32ddd01为14/2.384秒。Root job-2637e9c3eb6a揭示新库存检查未绑定本地Svc轮次，误等到finish_intent前；补活轮次后最终Root通过。19项job-cfc7b73bb043只有一个断言错误：缺cap时先由已有cap门拒绝，fixture却强求后置库存错误；按适用门断言后最终全量通过。未放松生产准入。完整证据本地evidence/owner-busy-admission-validation.json，精选reports/public-source-check.json.busy_owner_activation。

Main在0排队/活动/未知/有效库存hold时重载，configSHA/CPU254,255/2048MiB/SHM1024MiB/1槽及19个Node profile SHA不变；旧Node未升级、库存轮次无豁免。protected六文件仍原v036哈希。0 NPU kernel/模型调用，既有zdl启动时间和资源设置不变。

下一优先：无关用户追加后的既有设备资格兼容证明；探针内存在stage前校验Node/物理域运行边界。实际PID1/宿主多用户/两物理机、NPU访问ACL数值性能仿真、远程交付/fencing、一般资源合同/控制盘硬边界、组织身份/msagent仍待。PUBLIC-001继续延期，整体目标未完成。下方为各阶段历史。


## 忙队列维护排空与原进程恢复（2026-10-31）

新增owner_maintenance_drain.py及owner-maintenance-drain-v1。进程模式开户held先发布draining门：拒新提交，既有排队任务仍可claim/start，心跳/结果/归档/清理继续；事务内再次检查提交门，防止维护转换时越过准入。未知任务及inventory hold继续保留资源。缺失/维护门使已预留启动返回wait，同一attempt和分配不变。Node清理、ACK/GC、缓存租约和中央全部静默后记drained，再关闭执行准入、停止明确选择的本地进程Node及中央，最后重查中央，才进入stage。

首次冻结本地prefix/state/release/配置SHA及原运行状态；同Node/物理域/中央authority和辅助程序须匹配，拒用process模式操纵受管服务。最初未运行的服务不为了排空隐式启动。held中止不碰原Node；drained中止恢复原先运行的中央/Node。晚到工作退回排空时，先恢复进程再提交held；恢复中断或部分失败继续保留drained，确保再次中止仍记得恢复Node。systemd仍要求管理员先排空、初始inactive；实际PID1/受管忙排空未验收。恢复不扩大原停止范围，不删除原任务/目录/未知租约。

**最终696项/125.211秒/无跳过及JS语法通过；76个非文档生产源码与最终Root和全量冻结完全一致。** 新20项检查包含两个owner既有排队工作、拒新提交、事务竞争、缺失/维护门等待、未知资源保留、启动门优先、原进程快照/中止恢复、恢复崩溃与部分失败的持久顺序。后三项是受控单元异常，未宣称额外真机故障注入。

最终Root job-6c411e7fc099：49个SIGKILL恢复点通过；旧owner的两个真实IR任务在忙队列中止及完成排空路径各单attempt成功，停止后中止恢复原中央/Node。原第二新用户单次安装CLI开户、个人UID23802/23803认证与peer拒读、Svc23820、等待Node期间旧UID23801 IR、第二Node UID23812单attempt IR仍通过。独立host读取两任务/三个ready+onlineSeen交接/最终禁用及token计数/两个Node映射；Root /opt历史仅角色tmpfs内验证，不能当host留存历史或当前挂载quota。七文件盘loop/helper/角色/卷释放，后备文件留私有。

最终全量job-0dc9473b3bcc。初步focused job-f77217f8ff61为17/2.041秒；早期全量job-54973c7822d6为693/127.175秒、Root job-0fff89c656dc为49点，均在最后恢复顺序修复前，保留原范围。最终696另含3项恢复顺序检查且Root重新验收。完整证据本地evidence/owner-drain-validation.json，公开摘要reports/public-source-check.json.owner_maintenance_drain。

Main在无排队/活动/未知任务及有效库存保留时重载，已加载新capability，原configSHA/CPU254,255/2048MiB/SHM1024MiB/1槽和19个Node profile SHA不变；旧Node未升级，fresh inventory无豁免。库存表保留一条held=0历史记录，不是有效hold。0 NPU kernel/模型调用；protected六文件SHA与原基线一致，未改共享设备/驱动/账号/既有zdl。

下一优先：最终激活的全Node空闲条件与其他用户已知工作解耦；无关owner追加后的既有资格兼容证明；固定探针内存提前验证Node/物理域边界。真实设备/ACL/数值/性能/仿真、真实PID1/两OS用户/两物理机、远程交付/fencing、一般资源合同/控制盘硬边界、组织身份及msagent仍待。PUBLIC-001继续延期，整体目标未完成。下方为各阶段历史验收。



## 显式候选卡开户编排、观察与已成功任务采用（2026-10-31）

开户请求可选device_probes，每个所需Node显式列候选物理卡与有限内存；未选择不自动上卡。候选数量至少覆盖npu并发额度，可以多于额度，合计最多64个检查。预留全部固定探针再加一次最小托管任务的归档预算，Node/卡未登记、重复/布尔参数或预算不足在stage之前拒绝。Root逐个持久保存原意图、原job绑定和完成快照，固定Svc advance只提交一次并查询原job；失败、取消、未知、过期、权限变化和证据替换都停为attention，不自动重试。

新增owner_probe_adoption.py：Root --adopt-probe只接受管理员另行显式验证且当前仍有效的同owner/stage/Node/plan/卡/内存新任务。原任务仍queued/active/needs_attention时拒绝，保留原租约；原intent尚未提交时也在同一jobs→identities事务内禁止旧授权晚到提交，并追加CAS父流程采用链。中央提交记录是响应丢失后的权威，Root只缓存已提交记录；历史重复不倒退，旧失败/意图/快照保留。每卡最多8次采用、全局4096条应用限额，包含采用记录拒降级缺少模块。finishing阶段不改变前置计划，独立显式资格续验仍由最后激活检查当前来源。

servicectl owner-provision --wait-seconds 1..900观察同一请求；预算不打断管理步骤，到期不取消原job，attention立即返回。当前仍要求开始前排空；忙队列的无损维护门转换尚未实现，不将watch当作自动drain。

**最终676项/117.613秒/无跳过及JS语法通过；75非文档生产源码与最终Root及全量冻结一致。** 31项新协议检查覆盖逐卡/双卡、单并发额度多候选并真正选择空闲卡1的合法任务grant、原意图五个连续异常边界、Svc丢响应、job冲突/快照破坏、失败/取消/未知/过期与同ID恢复、显式采用/响应丢失/孤立意图禁止晚到提交/旧任务未终止拒绝/保持历史快照/不兼容降级、观察预算与历史回执。Root文件IO在非Root单测中适配，设备观测与回执均为合成，不是实际NPU执行。

最终Root job-6915ee7dad6a实际安装CLI验证：CPU节点的自动NPU计划在stage前拒绝；负等待参数无副作用；等待1秒返回pending而保留原请求；第二用户普通单CLI+wait完成CPU开户。原44个SIGKILL、Svc23820/个人23802与23803隔离、第二Node执行UID23812实际IR仍通过；七个文件盘loop/helper/角色/卷释放，后备文件保留私有。最终全量job-e47b5402877e，原Root75/full676另有早期冻结job-9db4525295e3/job-dfc8bc6960b8（118.512秒）；最终只规范化一个EOF空行并重核相同包源码。

早先focused job-0aa3e18144a5 20通过；job-42d80cbaf767 23项有一个fixture错误（目标Node未登记accuracy_v1，合法任务未领取）；job-6ed3e1a97168 31项有一个fixture断言错误（篡改回包也污染只读采用状态，先被更早门禁拒绝）。补齐fixture模板并限制篡改到目标probe回包后job-22badb71a08b 31通过。失败保留，不弱化准入。完整证据本地evidence/owner-probe-flow-validation.json，公开摘要reports/public-source-check.json.owner_probe_flow。

本轮0 NPU kernel/模型调用；protected六文件哈希不变，未改共享驱动/账号/既有zdl。真实硬件/ACL/数值/性能/仿真、真实PID1/两OS用户/两物理机、无损维护排空、普通用户并发下的预激活、Node无关用户追加的资格稳定性、一般资源合同与控制DB硬边界、组织身份/msagent仍待。PUBLIC-001继续延期，整体目标未完成。



## 管理员固定预激活NPU访问资格（2026-10-31）

新增owner_device_probe.py与本地服务管理员CLI workerctl owner-device-probe --spec。原disabled/token0阶段不能提交用户任务，但NPU激活要求先有本用户设备访问资格；现在为明确stage/原ready Node计划/物理卡授予一次固定npu_access_probe。它仍经过正常CPU/RAM/NPU/工作区/暂存/归档账本，固定1CPU/单卡/零SHM/120秒/1MiB文件与2MiB总产物；不临时启用身份，不生成凭据或执行用户命令。中央总协调可接受NPU额度，等待管理员显式探针，尚未自动编排探针或验收NPU一键流程。

授权冻结stage、profile、单调身份修订、Node原profile/ready交接、中央storage和受保护host/runtime context。提交/领取/启动许可/结果授予/最后激活均复核，改后恢复原值也失效；claim/start/complete按jobs→identities顺序持有身份写保留直到jobs提交。普通node-device-probe修复空身份回落成服务管理员的风险。原操作重试仍同一job，已提交/完成回执仅历史，不自动重试；新验收须明确新operation_id。授权历史4096条应用上限，包含记录后拒不兼容降级，整个控制盘/DB硬边界仍未完成。

**最终645项/119.488秒/无跳过及JS语法通过；74非文档源码与最终Root及全量快照一致。** 17项新增协议检查覆盖正/负资格、禁用用户无凭据、成功后再撤权阻激活、领取/启动/结果的权限变化、上下文变化、同operation恢复/冲突、固定模板限制、旧入口身份拒绝和身份锁。正向使用合成可信Node观测/回执，不是硬件计算。Root job-3e5d81b12916实际安装CLI拒CPU额度探针及普通入口缺失active身份；原同机真实Svc/Node/个人UID开户、第二用户单命令、配置隔离、第二Node IR及44个中断点仍通过，七个自有文件盘loop/helper/卷释放，后备文件保留私有。

最终全量job-1853b8ddfc01，针对性job-8492c1623ea3。早先642/121.438s及Root job-8423ff2d0b34在普通CLI修复前，保留其冻结范围。首次针对性job-4dc7893ca4c6有6个错误：fixture暂存合同不完整、两个排队任务超过fixture归档额度，以及参数异常类型；修正合同/fixture预算及统一参数错误后通过，没有放松资源门禁。一次首提交请求把资源字段错误嵌套被HTTP400拒绝，未创建job，已修正。完整证据仅本地evidence/owner-device-probe-validation.json，公开摘要reports/public-source-check.json.owner_device_probe。

本轮0 NPU kernel/模型调用、未改全局设备/驱动/账号或既有zdl。已有EBUSY没有新环境事实，不重跑相同探针。尚待：NPU探针显式选卡自动编排和真机设备/ACL/映射/数值/性能/仿真、实际PID1/两OS用户/两物理机、远程个人交付、一般资源变更、控制DB硬边界、组织身份/msagent。PUBLIC-001继续延期，整体目标未完成。



## 中央资源开户总协调与个人隔离（2026-10-31）

新增owner_provision.py及安装Root CLI owner-provision/owner-provision-node。固定原请求串联中央credential-free stage、原生GID配额、实际服务进程组加载、保护Node请求、原提案独立批准、Node绑定/认证ACK/健康心跳、最后激活和个人UID交付。初始重复GID/映射UID、错误Node/中央包在副作用前拒绝；批准使用原profile CAS，提交/claim在数据库事务内重查准入门。

资源维护先暂停准入并要求排空，在服务确认新组后开放旧用户；等待新Node时旧用户实际UID23801的IR任务单attempt成功。中央在等待Node和纯凭据阶段可沿原请求重启。中止只允许held，完成重放历史不复活账号、不重发凭据。元数据每记录12MiB、Root历史128操作/4096文件/128MiB及Svc参数4096文件/128MiB；整个控制盘/DB硬限制仍待。

最终**627项/127.415秒/无跳过及JS语法通过，73个非文档生产源码与最终Root包一致**。自有隔离Root连续44个中断恢复点（含初始无身份中止），另一个新用户使用普通安装CLI一次完成开户；实际个人UID23803认证成功，UID23802读取其私有配置被拒绝。首个新用户使用程序交付凭据在第二Node执行UID23812完成实际BishengIR单attempt任务，原Node/缓存/偏好及唯一原token保持，后来禁用后的历史重放不复活。七个文件后备盘的loop/helper/卷/角色已释放，原文件作为私有证据保留。

最终Root job-265951c1c22a；全量job-f7b3afe0b2ef。早先Root3/4通过但未覆盖第二用户普通CLI场景，保留原范围；失败保留：首次发现并修复finish pending恢复校验，随后receipt清理分支漏测试中断点；一次focused和一次625项全量因fixture误用atomic_json(mode=...)产生同3个错误，修正后当前627全通过。公开摘要reports/public-source-check.json.owner_provision；完整日志与独立host身份/Node元数据核验仅本地evidence/owner-provision-validation.json。

只验证同机进程模式，未运行新NPU/模型、创建宿主账号或改变旧zdl/全局服务。既有Main节点未自动升级，不豁免fresh inventory。跨主机仍管理员保护交付；真实PID1、两名宿主用户/两物理机、NPU预激活与精度性能仿真、一般资源合同变化、控制DB硬边界、组织身份和msagent案例仍待。PUBLIC-001继续延期，整体目标保持未完成。

以下为此前冻结阶段进度，不能用其“尚未实现”覆盖上方最新范围。

## Node资源准备、提案与确认的参与步骤

新增node_owner_provision.py及Root本地`servicectl node-owner-provision --prefix ... --spec ... [--complete|--abort]`。同一operation_id绑定原请求，默认准备原生work/cache槽并导出固定绑定提案；中央使用已有stage批准或active扩容授权后，complete推进原本地绑定及authenticated commit/ready ACK。Node不持有中央管理员身份，不创建宿主账号或远程Root通道、不发凭据、不启动服务。bound回执仍须等待正常Node启动/新鲜心跳及中央身份确认；完成后重放是历史，不复活被禁用用户。

原生计划先于副作用持久化；准备步骤跳过已完成资源，避免重放work撞到cache共用journal。Root内部作用域仅当前精确Node状态目录，普通验证、quota/startup恢复、Worker、备份/代码切换不能绕过；初次journal在独占状态锁下发布，关闭Worker进入窗口。提案写出后父阶段丢失可采用原文件，缺失/改变不能重生成。同ID换请求、换安装或物理机、无关子事务均拒绝。ACK阶段允许仅node_key轮换，其他配置摘要不变，仍用原计划/ACK。中止仅准备且未导出提案，先完成已开始的原生子事务，已分配资源保留、不创建未开始的另一盘。

模块单条记录12MiB、历史256文件/128MiB并预留后续空间，原子写入遗留Root文件保留且有界；不是整个控制盘/DB的内核配额。包含参与历史后拒降级到缺少协议的发布包。

最终**607项/109.872秒/无跳过及JS语法通过，72个非文档生产源码与Root包一致**。Root45次SIGKILL=协调器12+原生14+绑定10+ACK9，另两个quota中断后中止：work保留/cache未分配，以及cache在途恢复且work保留。原提案完整SHA保持；未批准ACK404、stage/token0保持，后续批准同计划恢复；commit response之后实际轮换Node密钥，旧key401、新key沿原ACK继续。新operation条件、提案丢失和未知历史文件均拒绝。原Node映射/槽/环境与缓存inode/IR SHA/namespace保留；随后真实健康心跳、激活/个人UID交付、单attempt实际IR、既有第二Node扩容/原凭据复用和禁用后历史重放仍通过，七个自有盘全部清理。

原始报告仅本地evidence/node-provision-validation.json；公开摘要为reports/public-source-check.json.node_owner_provision。保留两失败：首次真实缓存子事务恢复发现并修复work重放冲突；另一次fixture跨同源两个bind mount执行rename触发EXDEV，调整为同挂载内移出历史目录。早先606项及两份Root正向在节点密钥恢复改动前，只保留其原冻结范围，不重标当前验收。

完整目标继续未完成。下一步是中央stage/GID/配额、服务生命周期、Node请求/提案/回执及最后个人交付的总协调；跨主机只通过受保护的参与协议，不能把同机prefix调用当远程自动化。一般资源变更、控制DB增长、真实用户/PID1/两物理机、NPU数值/性能/仿真、组织身份及msagent案例仍待；PUBLIC-001继续延期。

## 已激活用户扩展第二个Node

新增owner_node_grant.py及服务UID本地`workerctl owner-expand-plan/apply`。经典owner-approve继续只接受禁用且无凭据的stage；扩容独立绑定已提交激活、单调权限版本、当前身份/凭据/成员/配置/中央存储指纹及完整不可变Node计划。权限触发器也覆盖直接SQL和改后恢复，不依赖字节恢复后仍相同的profile摘要。批准、ACK、首次健康心跳持有身份写保留锁直到资源数据库提交；批准后的槽位立即进入Node间冲突/容量预留。首次健康心跳前始终暂停新claim。

权限改变时不继续原grant；对同一Node计划明确`--reauthorize`仅替换未完成授权、使旧grant superseded，保留原资源投影和ACK，不重新绑定本地资源或发凭据。完成后重放仅返回历史结果，不反向恢复被禁用身份；包含授权记录的数据库拒绝降级到缺少协议的包。原个人客户端无需重装或更改profile，owner逻辑身份与每台Node的执行UID分离。

**590项/111.201秒/无跳过及JS语法通过，71个非文档生产源码与最终Root包一致。** 新12项覆盖enabled/disabled、凭据更新撤销、配额/成员/环境及直接SQL失效、同一计划重授权、首次健康心跳、槽位冲突、事务锁及降级。最终同机隔离Root：实际服务UID23820、个人UID23802，第二Node映射任务UID23812；一个既有token、不新增，原Node登记投影、旧缓存inode/SHA及个人偏好保持不变。通过10个本地绑定和9个ACK SIGKILL、两次权限改后恢复拒绝再显式授权；实际安装CLI及个人客户端完成第二Node排队BishengIR 19+23=42、幂等同任务/单attempt/产物读取。七个自有盘全部释放；旧激活/交付/中央重启及启动适配链仍按原范围通过。

本地私有evidence/owner-expansion-validation.json保留完整报告及六次失败定位：两次unit fixture字段/Node接口或能力标记问题，Root新增盘名白名单、两次预期详细错误但实际传输仅HTTP409、以及重复测试已持久ACK intent的中断点顺序。修正fixture后通过；不把这些失败改写成通过。公开摘要为reports/public-source-check.json.owner_expansion。

尚未完成整体目标：完整资源开户协调、一般UID/路径/容量变更协议、远程个人交付、真实宿主账号/实际PID1/第二物理机、NPU资格/精度/性能/仿真及msagent领域案例仍待。当前只验证同一物理机自有隔离Root环境，未改变共享驱动、旧容器或其他人的目录；PUBLIC-001夜间PR归因继续延期。

## 最后激活与个人UID交付的一次请求

新增owner_delivery.py、owner_finish.py及Root本地`owner-delivery-plan/apply/abort`、`owner-finish --spec ... [--plan|--abort]`。固定Root私有请求明确operation_id、原stage/Node计划和目标UID/GID/parent/name/同盘Root spool；中断保留原operation_id，新尝试显式换ID，避免将旧中止记录重放当成重新激活。**身份、中央配额、Node工作/缓存槽与ACK/健康心跳仍须先完成，owner-finish不是完整资源开户自动化。** 不创建宿主账号、不远程登录、不读取个人模型/skill/MCP配置。

服务身份子程序调用原激活事务，Root先持久原请求，再保存原始交付计划；计划/journal/回执只有绑定、身份和摘要，无密码/token。交付核对当前已提交且启用的身份、Basic/Bearer、范围/到期、源文件/目录身份及安装/config。Root私有spool完整落盘并可续写自身正确文件前缀，FD相对renameat2 NOREPLACE发布新目录，持久handoff授权后才转交0600文件与0700目录给个人UID；原始相对CA可交付，外部路径/私钥不接管。已有目录、链接/硬链接、父目录重建、跨挂载及过期凭据拒绝，不覆盖个人文件。用户接管后恢复不再重拷旧配置，归档偏好可保留。

中止仅处理原操作：未提交激活走已有spent-ID中止，已提交激活不自动撤销/禁用；交付及资源不回滚，未知/已交付/Root私有未完成文件不自动删除。Root回执明确保留原状态，完成重放是历史。pending阻断离线备份、代码切换和资源维护；纯凭据阶段不改变执行合同，中央可正常恢复启动/停止，其他resource/unit/startup journal仍阻断。已实际演练pending期间重启中央再恢复，避免激活等待live controller与禁止启动互相锁死。子程序输出丢弃，不用全进程FSIZE限制误伤超过256KiB的合法SQLite。

最终 **578项/103.558秒/无跳过** 及JS语法通过，70个非文档生产源码与最后Root包一致。实际Root/SVC23820/个人UID23802：原激活7、交付8、完成协调6个SIGKILL边界与同请求恢复；未提交完成中止后换operation_id沿用同一目标；一份最终激活token。实际部分Root文件续写、已有目标不覆盖、stale parent/跨文件系统拒绝、handoff前推进隔离管理员进程时钟导致过期拒绝并显式中止、300000字节身份DB填充通过。用户接管后实际UID修改archive_root，继续恢复保留；源用户禁用/令牌撤销后拒绝新交付，旧完成记录不重新启用或覆盖。随后个人UID用程序交付的client完成真实API登录/幂等提交/单attempt BishengIR 19+23=42及读取产物，peer UID读取凭据拒绝，无手工凭据复制。既有本地配额/缓存/Node ACK及中央/Node启动适配流程继续通过。

独立核对保留的master/交付回执、Svc原client SHA、激活DB记录与唯一已撤销token摘要一致；最终账户disabled1。三次成功Root各四盘均释放，所有自有角色/helper/volume缺失，原盘保留。私有完整evidence/owner-finish-validation.json、公开精选reports/public-source-check.json.owner_finish。两次测试失败保留：首focused夹具漏config.json导致2错误；后577回归的两个pidfd夹具漏合法role元数据导致2错误，已修夹具。此前Root/577绿色不重标为最后source范围。

旧zdl启动时间/Memory/SHM不变；没有当前宿主systemd写入、宿主账户、NPU/model或其他工程修改。实际PID1/两宿主用户/两物理机/远程HOME/NPU仍未验收，原新服务启动证据使用固定manager适配器。本阶段不替代完整资源开户协调、已激活用户扩新Node和合同变更重授权；整体goal仍未完成。

## 一次启动意图、真实进程就绪与准入交接

新增unit_start.py及本地Root命令`unit-start-plan/apply/abort`。绑定安装/配置/已加载unit/管理器/全部启用Node定义，持久journal和Root只读gate，发送前持久issued边界；只调用一次`StartUnit(name,"fail")`，不调用Restart/Stop/enable。响应不明或协调器崩溃后仅核对原请求；即使issued之后、RPC之前中断，也不猜测重发。原unit停止/无job且状态静默后可显式abort并生成新计划，不能在服务仍运行时清理中止记录。

先释放状态EX锁再启动，服务取得SH运行锁。入口严格核MainPID/INVOCATION_ID/UID/GID/cgroup，以O_EXCL持久消费nonce后才执行业务。安装服务通过固定prefix环境读取Root gate/journal；任一存在时提交返回HTTP503，中央本地调度、Node领取和Node本地执行暂停，身份查询、心跳和执行清单对账继续。中央须匹配本nonce的实际就绪进程、计划全部Node新鲜且属于本controller_instance的清单对账、无inventory hold；Node须有本nonce/PID/boot/starttick/NodeID的新鲜真实ACK。Root回执先持久，gate/journal最后清理；完成重放仅为历史，不能当作当前健康证明。管理器挂载依赖读回同时修正为显式路径加已知工作目录/PrivateTmp隐式路径，仍拒绝任意额外路径。

最终 **567项/95.628秒/无跳过** 及JS语法通过，68个非文档生产源码与最后Root包一致。固定管理器协议适配器真实启动中央UID23820和Root Node，各仅一次StartUnit。中央8个SIGKILL边界含“issued但未发送”和“已接受但无响应”，证明前者不重发可abort、后者恢复同一进程；持续HTTP503直到最后发布，身份API可用，live abort/改invocation拒绝。Node另5个边界，以实际安装Worker向中央发送认证心跳，nonce/PID/NodeID匹配，同一进程完成恢复并正常停止。原Node绑定/ACK、激活中止/重新激活、个人UID23802实际IR单attempt及归档仍通过。

独立读取保留的中央plan/claim/Root receipt及Node plan/claim/heartbeat逐项一致；所有自有角色/辅助容器/卷缺失、四盘loop均释放，原盘保留，旧zdl启动时间/Memory/SHM不变。完整私有证据evidence/unit-start-validation.json，公开精选reports/public-source-check.json.unit_start。两个失败Root均保留：第一次测试适配器在导入startup子类前替换Bus导致TypeError；第二次测试目录受umask变成0700，实际服务UID无法读回unit。仅修测试导入顺序、目录权限及可信Root测试的凭据读取方式，未放宽生产校验；两次loop释放/原盘保留。最初564项不是最终源码范围；首成功Root只含中央启动，最后Root增加实际Node启动，不相互重标。

本轮没有调用当前宿主systemd、创建宿主账户、运行NPU/model或修改他人容器。**固定适配器不能证明真实PID1的加载/启动/自动重启，两物理机和NPU验收仍缺。** 完整目标继续未完成；下一步为完整开户协调/私有交付、已激活用户扩新Node与资源合同重授权，及管理员试点的真实系统服务验收。

## systemd 生效配置读取、加载计划与中断恢复

新增 unit_manager.py 及本地 `unit-manager-check/plan/apply/abort` 管理命令。固定本地 system bus、清理继承的总线/进程环境、有界子进程输出与超时、Root PID1/总线身份绑定；严格核对属性及嵌套D-Bus类型、实际unit来源/别名/生成来源/附加配置、命令/身份/组/环境/可写路径与沙箱参数。不会导出原始环境属性。systemd启动入口还核对INVOCATION_ID、实际MainPID/UID/GID/存储组与cgroup；进程模式保持兼容。

Root在停止并排空时计划，持久journal后执行管理器Reload及选定unit的LoadUnit，再确认停止状态和生效配置。调用不确定时沿原计划恢复，管理器身份变化拒绝静默续接；显式abort只清理操作记录，不回滚已经发生的重载，也不发送总线写操作。普通启动、备份和代码切换不能绕过pending；完成回执重放明确仅为历史记录。这里没有StartUnit/RestartUnit/enable调用，启动和心跳协调器仍需补齐。Reload具有管理器级的daemon-reload语义。

最终 **554项/81.133秒/无跳过** 和JS语法通过，67个非文档生产源码与最终Root包一致。自有Root角色实际安装文件/journal，固定协议适配器连续六次SIGKILL恢复，身份变化拒绝、无写abort、新计划、来源拒绝、共享服务锁及pending门禁通过；后续已有Node本地绑定/持久ACK、激活/中止与重新激活、独立个人UID真实IR任务继续通过。四个自有loop/辅助容器/卷已清理，原盘和日志保留。最初553项和首Root通过范围早于最后的嵌套类型加固，未重标为最终结果。

宿主仅执行只读system bus身份/版本和公开安全属性查询，实际systemd255接口可读；**未在宿主调用Reload/LoadUnit/StartUnit**。适配器正向结果不等于真实PID1加载/启动验收；无新NPU/model、宿主账户/驱动/其他容器修改，原zdl启动时间与资源配置不变。完整私有证据为evidence/unit-manager-validation.json，精选公开范围见reports/public-source-check.json.unit_manager。

已补充当前资源下的推广优先级：保持个人API/MCP稳定，完善服务接管及一键开户、可执行资源合同、可复现实验与可替换领域Agent；先两名真实用户/单管理员测试机，再真实两机与约20人混合负载。公共夜间PR归因仍延期，完整目标保持未完成。

最后更新：2026-10-31。项目处于候选验证阶段，**尚未达到团队 NPU 生产运行条件**。

## 未提交激活：显式中止、已中止ID与重新激活

新增中央本地 `user-activation-status` / `user-activation-abort-plan` / `user-activation-abort-apply`。状态仅返回ID/阶段，不输出密码/token。账号必须仍是原stage定义、禁用且密码不可用、token0、无激活提交记录；已提交账号走禁用/撤销，不能中止回退。计划绑定原激活ID、stage摘要、私有目录和文件的身份/权限/SHA，不包含明文凭据；Node离线、意图过期和资源上下文改变可显式中止。

应用先持久journal，匹配删除本次client及有归属标记的本次CA，再提交永久已中止ID账本/审计、保存回执，最后清原intent和journal。原有/旧意图未标记归属的CA保留，未知文件拒绝删除。八个连续SIGKILL边界可沿原计划恢复；期间激活和备份/代码切换阻断，不兼容降级拒绝。旧意图恢复也不能启用，旧完成计划重放保留后来新建的凭据。不删除用户、配额、UID槽或缓存，重新激活仍受原资源/环境就绪合同约束。

实际同机安装中，服务UID23820真实生成client后中断、模拟过期、经状态和计划CLI中止，在同一计划八个边界逐次SIGKILL并恢复。此时禁用/token0/已中止ID1；再正常激活产生不同ID，旧Basic密码和Bearer均HTTP401，旧中止重放保留新凭据。随后个人UID23802上传输入、幂等提交单次真实BishengIR编译19+23=42、归档及清理继续通过，旧用户配置未变。最后账号禁用、新token唯一且撤销，独立核对SQLite已中止/激活/回执及原始ext4上的任务身份、结果和IR SHA。

同时修正已安装管理入口的状态目录覆盖约束，等号及缩写形式也拒绝；本地管理员命令不能借此误入另一安装状态。完整 **541项/87.928秒/无跳过**、JS语法通过，新增16项针对性检查；66个非文档生产源码摘要与最终Root包一致。此前540项和首个成功Root为补管理参数约束前范围。一个Root父因Docker辅助容器启动45秒超时而失败，未进入中央或中止流程；按确切镜像/标签/挂载及Created/PID0/未启动状态核对后，仅删除该自有对象，原盘和失败记录保留。不是把观察超时当作已执行成功或重复运行未知任务。后续Root均成功，自有4个loop、角色、卷及任务对象已清理。

空闲重载本Demo后声明owner-activation-abort-v1，原配置、CPU/RAM/SHM/槽位及既定4GiB管理归档预算保持。没有创建宿主账号、改宿主systemd或全局驱动、运行NPU kernel/模型；领域Agent、真实PID1/两机/NPU、资源合同变更重授权和完整一键协调仍待，整体目标保持未完成。

## 既有systemd unit：离线预览、CAS与中断恢复

新增Root管理命令 `unit-refresh-plan` / `unit-refresh-apply`。在停止且静默的选定安装上，核对安装/配置摘要、控制目录身份、unit目录与原文件元数据，预览当前生成文本。只接受本项目完整生成文本或仅缺少新存储组的历史生成文本；手工修改、未知组、链接和常见unit/前缀/type-wide drop-in拒绝。不会调用systemctl、启动/启用服务或修改无关unit。

应用先写Root持久journal，再保存unit和模板原始字节，原子发布两个文件和回执，最后清journal。六个连续SIGKILL边界在同一计划上恢复，未完成时正常验证/启动、备份、代码切换及其他配额维护阻断；配额恢复选项也不能绕过。Root私有历史目录保存原件与回执，重复操作核对记录，不覆盖后续修改。Node内容无需变化时保留inode，首次/重复应用状态有区分。输出明确manager未接触、仍需daemon-reload与服务启动；备份不是自动回滚许可。

最终隔离Root安装中，中央已有旧组23831的unit，新增GID23832后计划只补该组；共享服务锁、前缀drop-in、自定义内容和旧inode计划拒绝。6中断点恢复后两份原始备份字节一致，新unit与当前生成文本/回执SHA一致，无关unit未改。Node无需变化的unit同样计划/应用/重放且inode保持。随后原stage/GID、Node本地绑定与双ACK、个人UID23802激活/私有凭据、真实排队IR和归档清理继续通过。实际生成unit又经过本机systemd-analyze校验；此为文件更新与进程模式演练，未使用真实PID1加载/重启。

完整 **525项/80.944秒/无跳过** 与JS语法通过，新增10项unit针对性检查；65个非文档生产源码摘要与最终Root包一致。首轮524项和首Root为补路径/模式类型边界、Node无需变化案例之前的范围。两次针对性fixture失败保留：最初测试合同缺容量字段导致5处错误，后来新用例插入位置使1处变量未定义；只修测试，不放宽产品门禁。两个Root父均成功，4个loop/角色/卷/任务容器已清理，原盘和日志保留。源码、原始记录与精选公开摘要保持各自范围，旧Root54不重标。

下一步仍需实际manager生效配置读取/受控重载与重启、未提交激活intent中止重规划、完整一键协调、已激活用户扩新Node、NPU预激活资格和真实宿主多用户/两机。当前没有修改宿主systemd、旧zdl、全局驱动或其他工程；未运行新的NPU kernel或模型。

## 新用户激活、私有凭据与真实个人UID

新增中央本地 `accessctl user-activate --spec FILE`，明确owner、原stage摘要及每个必需Node的原交接计划，无远程提权接口。中央冻结Node账本并锁定身份事务，核对当前ready回执/profile、30秒心跳、环境、UID工作/缓存槽、有限暂存、物理域和原生配额。最低工作流的暂存字节/inode复用调度准入；NPU额度还需当前本用户设备资格。管理进程及实际运行的同UID中央服务都须具备新存储组，CLI临时补组不能掩盖旧systemd进程。

凭据intent和个人client文件在启用前持久保存，单身份事务提交启用位、密码哈希、一个个人token摘要和激活记录，随后发布私有交付回执。命令不输出密码/token，默认token24小时、仅jobs读/提交/取消。7个连续SIGKILL边界在同一激活ID上恢复，不重复发放；之后的禁用/撤销优先。未完成时备份/代码切换拒绝，含激活记录的降级拒绝；已完成记录离线恢复有路径迁移。未提交intent过期或合同改变的显式中止重规划仍待，当前保留证据并拒绝。

最终Root采用真实安装中央UID23820和Root Node，真实stage/GID/本地绑定/HTTP双ACK后，可信管理员将0600凭据交给0700个人目录的UID23802。该UID自行调用API、上传输入并幂等提交同一IR任务；UID23801无法读取个人凭据，另一个应用账号读任务返回404，个人token调用Node接口返回401。任务实际UID23802、零capability/NoNewPriv1，BishengIR编译19+23=42，单次attempt、归档及清理完成；最后禁用账号，激活重放保持禁用。独立只读原始ext4确认身份和IR SHA，身份库、私有回执及唯一token摘要另行一致核验。

当前完整 **515项/82.367秒/无跳过**、JS语法通过，18项激活针对性检查；64个非文档生产源码摘要与最终Root包一致。此前511项为补实际中央进程组检查前范围，513项为补最小用户额度/恢复路径检查前范围；各自保留原记录。5次失败保留：API字段误用、暂存字节不足、暂存inode不足、直接Worker测试漏生产umask导致GC拒绝、Worker多行状态触发父脚本JSON解析失败。两项容量问题补入激活门禁，下一新fixture使用1024MiB/262144inode盘；没有放宽原准入。最后两个失败的内层任务已成功，父失败仍保留，不能重标全成功。

四个自有盘/loop、角色、卷和任务容器均清理，原盘和失败日志保留。空闲重载本Demo后含owner-activation-v1，配置及CPU/RAM/SHM/槽位/既定4GiB管理账号保留预算不变。个人文件实际交付仍由可信管理员完成；这是同机数值UID模拟，环境由可信fixture管理员登记，未创建宿主账号，未验收新保护环境探针、NPU、systemd PID1或两台物理机。整体仍未完成，下一步见BACKLOG.md。

## 新用户预配：中央批准与持久ACK交接

新增中央 `workerctl owner-approve` / `owner-status` 与Node `servicectl node-owner-ack`，使用本节点凭据调用专用commit/ready接口。中央核对真实禁用/无凭据身份、GID硬配额、旧Node投影和修订及其他Node的分配，原子提交匹配登记；Node落盘两阶段ACK后才解除启动阻断。重复批准、请求和丢回复重试返回同一确认，不启用账号或发令牌。交接中暂停新领取、拒绝冲突配置/不兼容降级；撤销节点凭据后仍保留其待交接存储。配置继续修改须等更新后的健康心跳被中央观察。

最终同机联合演练采用实际安装的非Root中央UID23820与Root Node、真实中央stage定义和GID追加，四个独立文件后备ext4盘：工作/缓存各128MiB、中央载荷256MiB、Node暂存128MiB。Node本地绑定原10个连续SIGKILL链保留，中央ACK另有同一计划9个连续SIGKILL边界及原计划恢复；HTTP请求真实经过节点认证。最终本地/登记投影、中央提交/ready ACK和Node本地回执独立逐项一致；阻断文件已清，实际Worker健康心跳标记online。旧IR缓存暖命中和新UID独立IR编译仍通过。新身份保持disabled、不可用密码标记且令牌数0，尚未交付个人凭据。

完整 **497项/83.050秒/无跳过**、JavaScript语法通过，63个生产源码摘要与最终Root冻结包一致。此前495项是补交接撤权保留约束之前的范围，保留原记录。最初针对性测试有一处fixture引用不存在的JobStore.config；修fixture后9项通过，最终另补2项撤权/轮换场景。4次Root测试配置失败分别为隔离导入路径、组配额挂载参数、ext4有效容量/元数据余量和暂存盘最小容量；均在中央批准前失败，原日志/盘及清理证据保留，没有放宽产品校验。

所有自有角色、卷和4个loop均清理。空闲时仅重载本Demo，当前实际就绪信息包含owner-handoff-v1；配置、CPU/RAM/SHM/槽位和既定4GiB管理账号归档预算未改。环境由可信fixture管理员登记，不能称本轮重新完成中央保护探针认证；没有运行新用户队列任务或启用账号。完整开户还差最终激活/私有凭据发布、systemd组更新和实际多机/用户验收，见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。

## 新用户预配：Node 本地绑定阶段

新增 `node-owner-plan` / `node-owner-apply`。在停止且静默的已安装Root Node，将一个新用户映射与预先准备的工作/缓存UID槽一起绑定；更新全部旧缓存的策略摘要及新槽控制记录，保留原数据、UID/GID、命名空间key和环境。计划绑定配置/版本/控制目录/原生源、旧缓存状态及用量；中断只接受原计划。完成后仍等待中央确认，启动、维护、直接Worker运行、另一原生追加及代码回滚被阻断。

自有两个128MiB ext4 UID quota盘和真实安装代码完成：安装CLI、活跃租约/服务共享锁拒绝、同一计划在10个连续持久化边界SIGKILL并继续恢复。旧UID23801的真实BishengIR冷编译答案42，迁移后新租约命中同一文件、inode和SHA，调用次数仍1；新UID23802在独立命名空间另编译成功。两者均零capability/NoNewPriv1。旧配置映射、槽位和环境保持；独立控制记录核对旧key集合保留、新旧key不相交、租约均释放和交接计划一致。

演练使用fixture stage摘要，未连接中央，不证明中央授权或完整开户。真实前一代码9d025e0在绑定前回滚/再升级可保留缓存，新工具拒绝修改旧安装；进入待确认状态后回滚被拒绝。首次失败是外层umask收紧公共测试挂载目录，降权IR进程在绑定迁移前无法读入口；失败状态和日志保留，两个loop、角色和卷已清。修正目录权限并显式关闭拒绝启动时的状态锁后，Root及全量486项/75.161秒/无跳过、JS语法检查通过；8项新单元覆盖计划和启动边界。

随后仅增强Root验收脚本，直接核对暖命中前的完整旧缓存元数据，并验证控制generation变化后旧计划在写日志/修改配置前被拒绝；再次Root演练通过。生产61个文件与已通过486项回归的源码未变、与最终Root冻结包逐项一致；未因此重跑无变化的全量回归。原始报告及公开摘要分别保留完整和裁剪范围。

个人Main在验证通过后仅将管理账号的应用归档上限从2GiB显式扩至4GiB，以保留累计验证档案。事前观察物理磁盘可用约9.3TiB；所有逻辑用户额度行、CPU/RAM/SHM/槽位及其他容器未改，没有删除历史或原盘。空闲时重载本Demo加载当前源码和此容量配置；此前2GiB预算拒绝与正常结算后接纳的证据仍保留。这是运维容量变化，不是修改受测Root硬配额或放宽失败验收。

## 新用户预配：中央存储组阶段

新增已安装中央的 `payload-owner-plan` / `payload-owner-apply`。操作绑定无凭据的禁用身份及定义摘要、发布版本、控制目录身份、配置基线、原 owner 用量和配额修订；只追加一个空白 GID，不接管已有数据或兼容限额，不改旧 owner 分配。实际硬额度须覆盖身份声明的输入/归档预算，并检查静态总量与物理空闲字节/inode。控制盘恢复日志先于原生配额变更，普通启动/维护及另一类配额调整在未完成时被阻断。

自有256MiB ext4组配额盘上的实际中央安装和CLI验证通过：7个命名边界的SIGKILL与原计划恢复、旧用户哨兵数据及限额保持、过期计划/变更的身份定义/未登记既有限额拒绝。UID23820、零capability/NoNewPriv1固定可信写入进程在新增GID写满后返回EDQUOT122，另一GID仍可写入；带新数据回执重放及进程模式服务重启取得新补充组通过。真实前一公开提交60c2002重建旧包，代码回滚/再升级保留配置和数据；新管理工具拒绝修改缺少恢复保护的旧安装。

首轮演练因降权固定写入进程读不到Root私有测试源码，在开户/追加操作前失败；失败及清理证据保留。改用实际安装代码后通过；随后补回执重放的重启/组更新提示和实际身份定义变更拒绝，最终独立Root案例通过。没有把旧Root54或原生Node槽追加案例重标为本轮全量安装验收。

当前完整回归 **478项/78.009秒/无跳过**、JavaScript语法通过；9项新增单元涵盖计划/定义/数据拒绝/互斥及历史安装保护。最终Root案例冻结包的60个生产源码摘要与本轮受测源码一致，自有角色和卷另行确认已不存在。个人Main没有重载或修改预算；首次全量提交在Root父任务尚预留归档时被409拒绝，父任务结算后相同请求/幂等键正常接纳，未提高额度或删除历史数据。

**尚未完成统一开户。** 账号保持待预配，未交付凭据，Node工作/缓存绑定和最终激活仍待做。进程模式新组已测；真实systemd PID1和既有unit补充组更新未测、尚未自动化，返回明确更新标记。操作见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。

## 新用户预配：中央身份阶段

新增 `accessctl user-stage --spec` 和 `user-status`。中央一次提交禁用身份、固定定义摘要和不可用密码标记，不生成密码、令牌或个人客户端配置。同一定义可重复提交；不同定义、额度或环境修改被拒绝。项目和 Node 映射可提前登记，用户仍不能登录。普通启用或续发凭据无法跳过这个状态；完整资源就绪后的激活尚未实现。

新增12项检查覆盖：真实HTTP的Basic/Bearer拒绝、注入既有令牌仍拒绝、并发登记只创建一个身份、安装包内CLI/私有请求/链接路径拒绝、Node预登记不授予登录、旧禁用账号行为、发令牌与禁用竞争、恢复时重算定义摘要及拒绝不认识该状态的旧代码。全量 **469项/79.426秒/无跳过**、JavaScript语法通过；59个生产源码摘要已记录。测试运行在本项目的隔离状态目录，未创建宿主账号、重载Main或执行新的Root安装/NPU/模型调用。

后续仍需中央owner GID硬配额追加、Node工作/缓存策略的有修订绑定及各层就绪回执，最后才启用账号并发个人凭据。现有 `user-add` 保留旧测试账号行为，不是这套统一开户流程。详情见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。历史原生槽追加及Root54保留其冻结源码验收范围。

## 新用户预配：原生存储追加阶段

已实现已安装 Node 的 `workspace-add-plan` / `workspace-add-apply`：只向已登记的 ext4 UID quota 文件系统追加未使用 UID 的槽位，不修改原用户额度、数据或 Node 配置。计划绑定安装/配置/文件系统身份；控制盘和配额盘持久意图支持原计划恢复，未完成时阻断 Node 启动与维护。代码包携带 OWNER_PROVISION.md。

自有256MiB ext4和实际安装代码完成：CLI计划/应用、服务共享锁拒绝、7个SIGKILL边界及恢复、旧UID数据和限额保持、新UID内核EDQUOT、完成后带数据重放、过期计划/已有路径/既有限额拒绝。还从真实前一公开提交34d33c4重建旧包，验证代码回滚/再升级，原数据/配置保持；新管理代码拒绝跨版本修改缺少恢复保护的旧Node。该测试Node采用测试注册信息，未连接中央。

最初一次测试因角色镜像无Docker CLI，在安装前失败并完整清理；失败保留。修正为只读系统CLI和固定daemon绑定后通过，后续增加安装CLI和真实历史包兼容范围分别留证据。最终原生存储/兼容案例及全量回归均通过；457项/77.634秒/无跳过、JavaScript语法通过，58个生产代码摘要同时与Root案例冻结包匹配。没有重新运行历史Root54或大编译。

**完整开户尚未完成。** 原生存储步骤不创建/启用账号，不改中央GID配额、Node用户映射、缓存绑定或交付个人凭据；返回明确标记后续绑定仍必需。中央待预配身份已有本页顶部的新进展，后续继续owner存储与Node绑定，不能把槽位追加成功当成用户已可提交执行。边界见 [OWNER_PROVISION.md](OWNER_PROVISION.md)。

## 最新旧命令产物合同修复

旧非模板命令现在保留有效的产物限额，并一致用于幂等指纹、归档预留、日志/源码快照和输出收集。独立CLI新增成对参数 `--artifact-file-mib` / `--artifact-total-mib`；受控工作流仍在JSON声明。超额输出保留原件，工作负载目录不进入归档；成功命令改记失败，日志与说明仍能下载。旧协作式执行不因此获得文件系统硬配额。

9项针对性回归的修复前运行失败，修复后全部通过；随后补充实际2000/2001文件数量边界用例，覆盖单文件/总量、参数结构、幂等冲突、默认兼容、日志截断、源码快照以及合法129MiB归档。全量 **450项/75.213秒/无跳过**、JavaScript语法通过。

升级演练先用独立安装的CLI在仍加载旧代码的中央复现“声明8/16MiB却保存64/128MiB”，保留成功的单次尝试。空闲时仅重启本Demo中央加载修复：旧默认请求仍返回原任务；旧被忽略的非默认声明重试返回409，原任务契约不改、不重复计算。新1/2MiB声明准确保存，512KiB输出成功；2MiB输出明确失败，原件保留且失败日志完整下载。错误参数返回HTTP400。配置、资源策略均未改变，终态无在途/未知/阻断；无NPU或模型调用。公开摘要见 reports/public-source-check.json，接口及边界见 STORAGE.md/API.md。

## 最新混合负载验收

此前冻结发布包在 UID quota 提供器上的完整 Triton 冷暖构建与 19 名其他逻辑用户的 57 项 IR/CMake 任务已通过，独立原盘核验也已通过；不是旧命令修复后重新编译一轮。公开摘要见 [reports/mixed-compiler-stage.json](reports/mixed-compiler-stage.json)。

- 独立安装的中央与 Node、20 个逻辑账号及不同任务 UID；工作盘 32GiB、缓存 8GiB、Node 暂存盘 4GiB、中央归档盘 8GiB，使用自有文件后备 ext4 及 UID/GID 字节和 inode 配额。
- 外层队列预算 2CPU/12GiB，完整编译申请 1CPU/8GiB，实际最多两个计算槽。62 个任务全部成功且各只有一个尝试：3 个前置检查、57 个其他用户任务、2 个完整构建。
- 冷构建约 4038.445 秒，531 次 cache miss、0 次 hit；全新源码暖构建约 158.107 秒，531 次直接命中、0 次 miss。外部 LLVM/BishengIR 沿用固定镜像内容，未在本轮重建。
- 两份 wheel 各 1,253,270,190 字节、5,416 项 RECORD 通过；私有安装、新 C++ 扩展导入和离线 IR 常量折叠通过。
- 19 名其他逻辑用户的 IR 与 CMake 答案均为 42；小编译冷任务各 2miss，暖任务各 2hit/0miss。35 项任务与冷编译实际重叠；20 个私有缓存 namespace、跨用户私有任务不可见检查通过。
- 独立安装的个人客户端完整下载并核对两份 wheel、RECORD 和受保护环境回执。
- 清理后，另一个排队任务通过只读 SQLite 和持有的普通镜像文件 FD 执行独立核验：62 个单次成功任务、无 inventory hold、57 份产物的 UID/GID 归属、20 个空工作槽、空暂存目录、两份原盘 wheel 的 RECORD/新扩展/Node 摘要回执均一致。
- 自有角色容器、卷和辅助容器均移除，4 个 loop 关联释放，cleanup_errors 为空。Main 已恢复原 2CPU/2048MiB/1槽；原有 zdl 容器的启动时间、内存及 SHM 配置与基线一致。

这证明同一物理服务器上隔离角色和逻辑用户的 CPU/编译/归档链路，不证明真实宿主账号、20 人同时计算、两台物理机、NPU 计算或宿主掉电恢复。未创建宿主账号，未修改全局服务、驱动或共享编译环境。

## 当前代码检查

当前完整回归 **578 项/103.558 秒/无跳过**、网页 JavaScript 语法通过；此前567项为启动/心跳准入交接、554项为manager读回/加载恢复、541项为未提交激活中止、525项为unit离线更新、515项为最后激活、497项为中央/Node交接、486项为Node本地绑定、478项为中央存储组、469项为待预配身份、457项为原生槽追加、440项为独立原盘核验工具阶段。公开摘要 [reports/public-source-check.json](reports/public-source-check.json) 保存受测 70 个生产代码摘要。

新增 5 个核验工具回归使用真实子进程，覆盖有效元数据、单次短写截断、持续写达到内核文件限额、stderr 洪泛和文件系统警告拒绝。小记录输出在子进程写入时就受 2MiB 上限约束，避免 capture_output 读完后才检查大小；不影响已冻结的生产代码或构建工作负载。

Worker 的历史并发修复已有修复前确定失败与修复后证据：先取样的未清理心跳晚于正常完成回报到达，曾误触发资源域隔离。现在完成回报等待旧心跳发送完，后续清理态心跳仍可在慢 ACK 期间继续；真正未知租约的隔离规则未放宽。本轮完整混合案例未再触发 hold。

v0.36 历史安装验收为 432 项单元检查、54 项自有 Root 容器安装检查，范围见 [reports/v036-public.json](reports/v036-public.json)。后续长目录 socket 修复已有原失败深层克隆的 434 项回归及真实跨容器合成握手；当前混合案例覆盖新增 Worker 的实际使用。它们分别保留证据，不重标历史 Root54 为新版本全量安装验收。

## 已有实现

- 程序化中央调度、持久资源账本、物理服务器资源域、幂等提交和用户/项目授权。
- Node 主动领取、受限非 root 任务容器、执行尝试身份、未知状态保留资源和恢复对账。
- 封存输入、工具链摘要锁、精度/性能/仿真/编译/IR 五类任务合同、结构化结果与个人归档。
- 独立工作盘、中央输入/归档组配额、Node 暂存盘、私有持久缓存、阶段依赖与产物交接。
- 中央/Node/个人客户端分角色安装；停机备份、新目录恢复和兼容代码切换/回滚。
- 历史真实 OpenCode → MCP → 中央 → 执行器的 CPU/IR 任务通过；领域诊断能力仍需已知案例验收。

## 保留的失败与发现

混合验收前四次失败均保留，未改成成功：单文件/输入总额度不一致、Root600 合同未交给服务 UID、IR 产物声明类别错误，以及真实 Worker 心跳顺序问题。第四轮虽有 38 个成功小编译，IR 合同仍失败，完整编译明确取消；本轮通过前先验证真实 IR 合同和冻结源码配置，配置使用不同缓存 namespace。

新回归首次提交被个人归档预算拒绝；当时原父任务仍预留默认产物额度。父任务终态结算后，相同请求正常接纳，无需提高限额或删除旧证据。排查还发现旧非模板 command 入口会校验 parameters.artifacts，却未在规范化结果中保留它，后续仍按默认额度预留；已修复并验证，见本页顶部。该问题不影响托管工作流的显式产物合同；现已完成本页顶部的修复与升级验证。

## 未通过的生产门槛

1. 新 owner 自动预配、Node 工作/缓存配额变更、缓存保留/新盘迁移、控制数据库及审计增长硬边界。
2. 非 root NPU 编号/隔离、ACL 初始化、真实精度、性能、仿真及清退；已有设备失败保留，不用管理查询成功替代计算验收。
3. 两名真实 Linux 用户、两台物理服务器、真实 systemd PID1、旧中央 fencing、组织身份及宿主掉电/磁盘故障。
4. 领域 Agent 的已知错误/性能/流水案例验收；完整外部 LLVM/NPU-IR 构建。

## 后续公共项目

夜间 PR 回归与归因（PUBLIC-001）暂未启用。后续采用独立机器人身份、低优先级预算和固定环境；资源不足等待，先产出复现实验及修改/revert 建议。

## 同步约定

每次有可复核的代码变更、验收结果或失败定位时更新本文件并提交。运行状态、模型配置、对话、原始实验盘和私有部署凭据不上传。推送前重新检查远端 HEAD，使用普通快进更新；完成一个阶段不等于完成整个项目。
