# 固定非 root CANN 初始化诊断

初始化前的设备视图可用独立[驱动编号映射诊断](NPU_DRIVER_MAPPING.md)取证。它只调用已核对签名的固定 DSMI 查询，不运行 ACL 或开启容器服务；不会自动修正可见性配置或授予普通工作流资格。

`tests/npu_runtime_initialization_check.py`是可信维护者通过现有队列运行的独立诊断。它不在普通工作流/API/MCP中授予新权限，不修改设备资格，不安装或重启驱动、Docker，不进入或修改既有用户容器。代码和证据随仓库发布；它不是当前服务器发布包中的新执行后端。

先用`--metadata`请求NPU=0，验证固定镜像中的系统Python、UID23901/设备组1001、零capability、no-new-privileges、只读挂载及选中库/头文件/驱动文件SHA。该模式不暴露NPU且不调用CANN API。基础镜像不包含现有容器后装的包；应以实际预检为准，不从容器名称猜环境。

实际模式请求CPU=1、内存1024MiB、NPU=1及有限超时；忙卡留在原队列，领取后再次核健康/空闲。只为该任务创建新受限容器：硬内存/交换上限768MiB、1CPU、48PID、无网络/只读根、96MiB私有工作tmpfs，显式暴露单物理卡和合同中的管理设备，CANN/驱动只读。仅接受Root保护的CANN来源及固定镜像SHA，不拉镜像或改变系统安装。

固定C ABI顺序为aclInit、设备数、物理→逻辑→物理双向映射、一个显式Context创建/销毁、aclFinalize。映射失败或不一致不创建Context；不猜0号设备。清除ASCEND_RT_VISIBLE_DEVICES，使用户设备ID与逻辑ID一致的依据是[官方映射接口说明](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/910beta1/API/runtimeapi/aclcppdevg_03_2059.html)；实际API原型还核本机选中头文件。显式Context的含义见[官方Context说明](https://www.hiascend.com/doc_center/source/en/canncommercial/601/inferapplicationdev/aclcppdevg/aclcppdevg_03_0027.html)。

不调用reset、张量分配、应用kernel或显式stream创建接口。运行时自身可能有内部动作，不能据工具没有提交应用kernel就证明设备内部完全无工作。创建/销毁返回值只描述本进程的显式Context；仍需独立确认容器退出和后续管理状态，不自动认证完整资源清退或普通任务资格。

每次保留原job、镜像、程序及选中文件SHA、实际UID/组/capability、设备节点、只读挂载与cgroup实值、每步C返回码、原stdout/stderr和退出信息。未知/超时检查原意图及带本任务标签的容器，不创建替代尝试，不删除外来对象。诊断完成不等于CANN初始化成功；更不代表精度、性能或完整设备健康通过。

失败时还尝试读取当前线程的aclGetRecentErrMsg（最多4096字节），并在退出前保存本进程私有目录中的有界日志快照：最多8文件、处理128目录项的预算、6层、每文件64KiB/合计256KiB，拒绝链接、特殊文件和非本人文件。截断/并发修改明确标记，日志收集失败也不覆盖原API结果。原始信息只在私有任务归档中；公开报告不包含消息中的PID或日志正文。进程崩溃前未发出的tmpfs日志无法凭此恢复，原stdout/stderr仍单独保存。

本机第一次实际设备阶段在aclInit返回500000后停止，未调用设备映射/Context接口。该码在[官方错误码表](https://www.hiascend.com/doc_center/source/en/canncommercial/800/apiref/appdevgapi/aclcppdevg_03_1345.html)中表示未知内部错误，不能直接归因硬件。无设备的补充动态加载检查已成功加载libascendcl、libacl_rt和libacl_rt_impl；这不证明其全部延迟依赖或初始化路径正确。具体结论及源版本见[PROGRESS.md](PROGRESS.md)。

仅追加一次带详细观测的固定诊断，仍在aclInit返回500000；本线程消息出现Resource_Busy/EL0005和初始化SoC版本失败、chipType=0的下游提示。卡前后管理状态均健康空闲，没有成功映射/Context，也没有应用kernel。这些信息只将失败定位到初始化/SoC信息获取阶段，不能据chipType=0认定实际芯片不支持、据Resource_Busy认定他人在用，或证明是某一个驱动/命名空间原因。当前诊断清除了可见性环境，后续应先核驱动层映射及启动上下文，不能将其等同普通工作流已批准可见性配置的正向验收。

验收：最终1200项回归、11项专项及实际无设备/单卡诊断完成。中央与网页源码未改，既有Root42/网页12仅沿用其源码完全匹配的证据，本轮没有重新安装/浏览器验收。具体job/源码与限制见[PROGRESS.md](PROGRESS.md)。
