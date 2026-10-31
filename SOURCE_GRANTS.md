# 跨轮实验的指定产物授权

`owner-source-grants-v1`允许普通个人账号批准：将本人一个已确认托管任务的指定产物交给一个实验预算。中央仅通过依赖输入复制所选文件，不为预算凭据开放来源任务、日志、归档下载或其他预算。不同owner和项目共享来源暂不支持。

来源必须是已确认结束并完成清退、具有Node完整产物回执的版本化工作流。授权固定来源job/attempt/状态/清退证明、完整manifest摘要、所选文件名/大小/SHA及可执行位许可、目标预算和绝对到期时间。允许`results/`、`ir/`、`profile/`下最多32个文件，单文件名最多512字节；不接受stdout、任意主目录或控制文件。

## 个人预览与批准

先批准新的实验预算，以普通个人profile准备spec.json：

```json
{
  "source_job_id": "job-替换为来源编号",
  "destination_experiment_id": "experiment-替换为新预算编号",
  "files": [{"name": "results/accuracy-analysis.json"}],
  "lifetime_seconds": 3600,
  "idempotency_key": "diagnosis-source-001"
}
```

```bash
labctl --profile /本人/private/client.json source-grant plan \
  --spec spec.json --output /本人/private/source-plan.json
# 阅读计划中的来源、文件摘要、目标预算和有效期，然后批准原计划。
labctl --profile /本人/private/client.json source-grant approve \
  --plan /本人/private/source-plan.json
labctl --profile /本人/private/client.json source-grant list
labctl --profile /本人/private/client.json source-grant revoke source-grant-替换为返回编号
```

预览无数据库写入。计划目录需本人0700，输出0600且不覆盖已有文件。批准时复核来源和目标预算，源已变化、预算不可新增或计划已过期则拒绝。有效期最多24小时且不超过目标预算到期；幂等重放返回原授权，不续期、不撤销既有撤权。每owner最多1000个历史授权，最新100个可列表，其余按ID读取。只有普通个人凭据能批准或撤销；预算token、legacy和管理员凭据不能签发。

## 预算内任务使用

在现有工作流请求中添加依赖与导入；用目标预算profile提交，或普通个人profile明确相同`experiment_id`：

```json
{
  "depends_on": ["job-替换为来源编号"],
  "artifact_inputs": [{
    "job_id": "job-替换为来源编号",
    "name": "results/accuracy-analysis.json",
    "target": "previous/accuracy-analysis.json",
    "when": "terminal",
    "source_grant": "source-grant-替换为返回编号"
  }]
}
```

上例只是依赖字段，还需原工作流、封存基础输入和环境锁。`when`仍默认success；读取失败/取消/超时来源必须显式terminal。来自同一parent的全部导入须使用同一个授权，或全部采用普通读取权限；无文件的依赖屏障不支持借授权跨预算。只能导入批准的文件，可改安全目标路径；请求可执行位须已在计划中显式批准。输入不与原文件/目录冲突，复制按原限额、摘要封存和磁盘配额执行。归档`dependency-inputs.json`记录授权ID和实际导入文件。

授权不开放整个依赖祖先图：直接来源必须原本可读或获上述明确授权。内部祖先检查只核同owner、托管类型及有界无环范围，不给调用者读取祖先的权限。目标预算仍累计计费，授权不增加任务数、CPU/NPU额度或存储额度。本人输入仓库的既有owner权限保持不变；本授权不新增输入仓库按预算隔离，也不能收回已复制到本人输入中的字节。关闭预算保留此前已计费排队任务的语义；文件授权自身的撤销/到期独立生效。

## 撤销、故障与恢复

新提交、交接开始、每个复制分块以及输入就绪和首次启动许可均复核授权及来源证明。撤销/到期后，新提交拒绝，未启动任务等待，保留原输入、复制偏移和已分配尝试/租约，不自动换授权或重跑。首次启动许可已发出后，撤销不等于取消任务；显式取消仍走Node确认清退。已经复制进本人输入或结果的字节不会因撤销消失，也不能作为信息收回机制。

有效授权在到期前固定来源归档，防止尚未提交消费者时被GC；未结束消费者继续按原依赖固定来源。授权撤销或到期不会删除文件，也不自动回收占用。

备份恢复将所有授权置为`restored_frozen`，预算继续冻结并撤销旧预算token。用户可重新审阅历史来源，批准新预算和新授权；旧授权不能重开。历史授权或任务导入记录存在时，拒绝降级到不支持该合同的旧代码。恢复仍要求原有排空和存储检查；没有新增在途灾难接管或两机fencing。

HTTP：`POST /api/source-grants/preview`返回`plan`，`POST /api/source-grants`直接接收原plan，`GET /api/source-grants`，`GET /api/source-grants/ID`，`POST /api/source-grants/ID/revoke`接收空对象。预算凭据只可查看目标为自身预算的授权元数据；来源任务的原ACL保持不变。个人harness无需安装额外模型，固定工作流MCP透传这些字段；Agent不获得批准工具或普通个人凭据。
