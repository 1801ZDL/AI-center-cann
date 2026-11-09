# 结果正文迁出与归档读取

2026-11-09追加[失效授权与预算恢复保护](GRANT_HISTORY.md)：新计划增加grant_rotate第五步，
旧批准不变；本地失效状态决定权限，数字账本不因归档返还。

2026-11-09开发源码。本功能只迁出结构化结果正文，完整关联SQL轮转仍未完成。
本次没有安装、启动服务、处理现有任务、回收配额或运行设备测试。

## 保留的事实和读取规则

workflow_results保留原job_id、rowid顺序、owner/project/kind筛选字段和精确副本引用。
性能/精度指标、诊断及其他正文从原control-history副本读取，不重新计算或填0。
结果页先执行当前任务ACL、类型筛选、排序和分页，再读取该页副本；同页同副本只核验一次，
只保留一个副本的解析缓存，每个新请求重验。CSV沿原分页导出及32MiB上限，原数值保留。
项目成员关系变化按当前权限处理，无权限或类型不匹配时不打开别人的私有文件。

每次读取核对原副本校验、结果字符串SHA、任务迁出批准、新结果批准及完整筛选投影。
副本、索引或批准丢失/不一致均拒读，不返回stub作为结果，不补0或重提计算。
常规结果写入器拒绝覆盖已迁出的记录。设备观测与数值资格原样保留，不因此认证设备健康。
本操作不删除任务、attempt、授权、计费、任务图或归档，不释放任何配额或改变原回收凭据。
SQLite正文更新不保证数据库文件立即缩小，不执行VACUUM。

## 维护入口

新生成计划依次为control_copy、control_verify、control_rotate、result_rotate。
末步先保存派生的结果CAS计划，再单次应用；丢回包沿原计划恢复，不重新规划。
旧三步v1/v2批准和journal不变，不自动追加步骤。第四步需要一次准备及一次apply，
使用[执行合同](MAINTENANCE_EXECUTION.md)时应另计这两次预算；不足不会自动扩大。
没有结果、正文较小或不能缩减空间的行保留原样，空选择不建立无意义的审批账本。

处理已迁出旧任务时，在匹配安装、Root私有目录中显式执行：

```bash
umask 077
opsctl result-history-plan --prefix /opt/ascend-lab-controller \
  --export-id 原64位control-history副本ID > /root/private/result-history-plan.json
# 核对原批准对象后再应用；丢回包仍重用同一文件
opsctl result-history-apply --plan /root/private/result-history-plan.json
```

仍要求Root、匹配代码、停止且排空的中央、原离线锁和稳定存储，不自动停服务或绕过pending。
一份计划绑定一个原verified副本及1..32个原任务，任务正文须先沿control_rotate迁出。
restore_frozen副本继续允许只读结果，不批准新的迁出。
原副本上限256KiB、单结果128KiB、选中结果合计256KiB；超限拒绝，不截断正文。
数据库最多4096个结果索引及128份结果批准；达到上限保留原SQL，不承诺无限历史。

全部结果更新、索引和批准在一个事务中提交。提交前中断回滚，提交后丢回包重验原索引、
原副本后返回reused，不重复建立记录。缺索引时不会把普通正文当作已完成的迁出。
Root副本状态新增migrated_result_count/result_read_authority，不作为新的配额释放或授权。
缺少读取器的旧版本拒绝降级；恢复须保留原结果投影、索引、批准及可读副本。
Root前缀中的副本仍须单独备份/迁移，服务状态目录备份不能代替。

## 当前证据与限制

[公开摘要](../../reports/result-history-check.json)记录本轮范围。新增24项小型检查覆盖SQL迁出、
当前权限、分页/CSV一致性、校验复用、丢失/改变的副本或索引、事务回滚/丢回包、恢复、
降级和维护衔接。其中一项使用真实小文件与原校验器验证身份、权限及SHA，篡改后拒读。
该项将Root UID常量替代为当前UID，离线上下文仍替代，不是真实Root安装验收。

开发中103项及104项合并检查曾通过；复核后两次合并运行触发原8秒截止，进程终态142。
另一次旧快照分组也因同一8秒截止退出；共3次终态142均保留。
最终49+25+31三组直接源检查合计105项串行通过，CPU合计约0.94秒、峰值内存约36MiB。
不宣称一次合并检查或完整回归通过，未上调预算。
临时SQLite夹具使用内存journal和synchronous=OFF，验证SQL逻辑而非断电持久性；
生产持久化未改，没有用这些检查冒充真实SIGKILL恢复。
每组仍限单CPU、256MiB地址空间、5CPU秒、8秒截止，禁止真实子进程、网络和后台线程。

执行尝试、授权撤销、计费、任务图等其余关联记录以及批准/副本/索引最终留存仍待。
真实Root只读挂载、进程故障、跨机迁移、安装恢复和当前完整回归未验收。
领域Agent、夜间PR、网页扩展暂缓，真实逐卡日程未启用。

低IO复核命令（逐一运行，不并发）：

```bash
python3 scripts/check-focused.py --direct-source --module test_result_history --module test_maintenance_policy --module test_repository_layout
python3 scripts/check-focused.py --direct-source --module test_maintenance_execution
python3 scripts/check-focused.py --direct-source --module test_core_operations --module test_collector_safety --module test_read_retry
```

显式direct-source仅支持已审查的有限专项模块；默认入口仍保留旧平铺源码视图用于兼容检查。
