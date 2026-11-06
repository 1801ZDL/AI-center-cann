# 精度差异证据与离线重放

`accuracy_data.py`用标准库比较明确提供的actual/reference二进制数据，`accuracy_diagnostic.py`通过既有任务ACL重新读取完整归档、绑定封存摘要并独立重算。读端不加载NumPy/pickle或设备库，不提交任务、不调用模型、不修改算子、容差或参考。客户端manifest声明`accuracy-case-v1`。

这条CPU离线重放使用普通`build_v1`，不是`accuracy_v1`设备测试，也不绕过其NPU预留和资格要求。比较有差异时任务退出非零，原报告和CSV保留；没有把精度差异当作绿色分析任务。已有`accuracy_v1`和实际设备验收保持原合同。当前不宣称支持任意框架的tensor dump或自动根因/修复。

输入为封存的`accuracy_data.py`、`accuracy-input.json`、`actual.bin`、`reference.bin`及可选`source_context.bin`。所有数据为小端、C连续，形状相同且非空，最多8维/1,048,576元素，每个文件最多8 MiB。uint8仅用于逐编码比较，不把编码差值当成数值误差；numeric支持实际float32与参考float64等混合组合。

```json
{
  "schema":"ascend-lab-accuracy-input/v1",
  "name":"原始输出对明确参考",
  "shape":[256],
  "actual":{"file":"actual.bin","dtype":"float32","sha256":"完整实际字节SHA256","origin":{"id":"capture-001","kind":"historical_device_capture","source_sha256":"原始采集文件SHA256","scope":"历史采集；当前未重新运行设备"}},
  "reference":{"file":"reference.bin","dtype":"float64","sha256":"完整参考字节SHA256","origin":{"id":"reference-001","kind":"cpu_reference","source_sha256":"参考原始文件SHA256","scope":"明确参考算法和版本范围"}},
  "policy":{"mode":"numeric","atol":0,"rtol":0,"equal_nan":false,"signed_zero_equal":true}
}
```

占位SHA须替换为64位小写hex。编码比较的policy严格为`{"mode":"exact_encoding"}`，actual/reference均uint8。可选source_context同形状、float32/float64，用于展示发生差异时的输入值，不参与比较。origin只有historical_device_capture、cpu_reference、user_supplied三种声明；封存与SHA证明字节身份，不独立证明其来自真机或参考模型正确，历史原始文件仍须由操作者核对。

numeric采用`abs(actual-reference) <= atol + rtol*abs(reference)`，明确NaN配对与有符号零政策；同号Inf可配对，不同Inf或有限/Inf配对失败。有限算术溢出时以有理数重新判断，防止inf<=inf误判为通过；JSON不输出非有限数，错误指标无法用有限binary64表示时返回null与溢出标记。零参考值不强行除法；完整比较统计与最多16个差异样本归档，样本截断明确标记。

按WORKFLOWS.md封存输入和环境锁后，普通请求使用`template=build_v1`、`environment=worker-python`、`argv=["python","-I","-B","accuracy_data.py"]`、CPU=1/512 MiB/NPU=0、只读输入和明确超时。申请足够工作盘及输入/产物预算；完整数据会复制到`results/accuracy-input/`供独立核验，另产生`results/accuracy-analysis.json`与CSV。示例小数据可用8 MiB/128inode工作盘、file_mib=8/total_mib=32；大张量应扩大硬工作盘配额，schema不会自动扩容。

```bash
labctl --profile /本人/private/client.json accuracy-case job-ID
python3 /本人/client/accuracy_diagnostic.py mcp --profile /本人/private/client.json --job-id job-ID
```

只读MCP仍是固定任务的空参数`get_diagnostic_case`。读端核完整manifest、actual/reference/context字节及封存driver/config SHA，重算结果须与归档报告一致；再附本人可读的通用日志和受保护CPU环境绑定。模型获得形状、来源声明、政策、完整摘要和有限差异样本，不获得完整张量。换参考或放宽容差必须另行封存、明确标识并申请预算；即使变绿，也不能宣称原算子修复。原失败任务与原参考继续保留。

真实历史案例与当前安装验收状态见PROGRESS.md。没有新设备启动、实际精度成功、硬件故障或完整领域harness验收时，不以离线重放替代这些结论。

执行Node须明确登记build_v1模板并允许该用户；未登记时中央应保持排队、零attempt，不自动扩大Node开放范围。
