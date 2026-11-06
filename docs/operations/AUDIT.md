# 认证审计保留与显式归档

新管理员中央安装默认在config顶层设置：

```json
{"audit_retention":{"mode":"archive_required","maximum_unarchived_rows":10000}}
```

现有配置缺省保持全表最近10,000条滚动策略，不自动迁移。新模式保留所有新认证审计行，不自动删除；已被旧策略丢弃的记录无法恢复。门槛按全表合计，非每人额度。达到门槛或无法确认状态时暂停新增任务/输入、新claim和未消费start；原许可、任务监督、取消及结果回传继续。门槛是准入快照，既有工作仍会产生事件，行数可以超过门槛；它不保证磁盘容量，仍需有限控制盘及外部监控。

## 查看积压

已认证的GET /api/audit-retention、jobs摘要、个人MCP queue_status和网页显示策略、retained_rows和blocks_new_work。没有新删除接口或个人管理员工具。现有control-monitor只读取静态策略与文件系统余量，不查询审计行数；其Prometheus准入指标不能代表审计没有积压。

## 停中央、有界导出、确认裁剪

下面是进程模式已安装中央的Root维护示例；替换实际prefix及归档位置。受systemd管理的安装应通过已配置的管理流程停止中央并确认停止。使用匹配安装版本的维护代码，不直接编辑SQLite。独立Node无需停止，中央停止期间继续监管原任务并保留结果，重启后回传原attempt；应安排维护窗口。

```bash
/usr/bin/python3 -I -B /opt/ascend-lab-controller/current/servicectl stop \
  --prefix /opt/ascend-lab-controller
install -d -m 700 /root/private-audit
/usr/bin/python3 -I -B /opt/ascend-lab-controller/current/servicectl audit-export \
  --prefix /opt/ascend-lab-controller \
  --output /root/private-audit/batch-001.jsonl \
  --max-rows 10000 --max-bytes 16777216
```

输出祖先目录必须Root持有且不可被组/其他用户写，直接父目录Root:0700，文件Root:0600。输出必须离开中央状态树；原生控制盘模式还要求不同文件系统。命令不覆盖已有文件；默认最多导出最旧10,000行、16MiB行数据及一个有界manifest。只导出audit表的id/at/actor/action/target/outcome，不导出账户、口令哈希、盐、token或资源/执行表。原始审计含用户活动信息，必须私有保存；相同机器的另一文件系统并不等于异地备份。

核对导出回执rows及sha256，独立校验文件，并按组织保留策略确认保存位置后，再执行下面命令；将占位符替换为该次导出返回的完整小写SHA256：

```bash
sha256sum /root/private-audit/batch-001.jsonl
/usr/bin/python3 -I -B /opt/ascend-lab-controller/current/servicectl audit-prune \
  --prefix /opt/ascend-lab-controller \
  --archive /root/private-audit/batch-001.jsonl \
  --confirm-sha256 REPLACE_WITH_EXPORTED_SHA256
/usr/bin/python3 -I -B /opt/ascend-lab-controller/current/servicectl start \
  --prefix /opt/ascend-lab-controller
```

积压大时需另选新文件逐批维护。导出/裁剪本身也记审计，不能把“导出一批”当作“已清空”。命令重新校验整个文件、记录前缀、数据库/状态目录/恢复epoch和持久导出回执，只删除明确确认的前缀。未确认、被修改、丢失或属于另一实例的文件不能触发裁剪。

## 中断与恢复

导出文件fsync并发布后才提交数据库回执，写盘失败不裁剪。若响应丢失或进程中断，沿用相同输出文件和相同预算重试原export；不要覆盖它或另造一个替代文件。裁剪沿用原文件及原SHA重试，重复确认不再次删除或新增重复裁剪事件。

所有SQLite VFS操作以中央服务UID执行，避免维护崩溃留下Root属主的hot journal。裁剪、回执和审计事件在同一事务提交，单调id不随裁剪复用。快照恢复切换审计epoch并使旧回执失效；旧归档仍作历史证据保留，不能用来裁剪新实例。已启用模式的激活/恢复拒绝缺少对应实现的旧代码。

维护不改身份、口令、任务、租约或专用恢复回执，不提供HTTP删除、自动上传、定时器或VACUUM。删行不立即缩小SQLite文件，已分配页可以复用；不是介质安全擦除。服务和Root管理员属于信任边界，此SHA/回执协议不提供针对受信管理员的防篡改或断电/硬件故障下的持久性证明。
