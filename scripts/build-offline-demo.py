#!/usr/bin/env python3
"""Build a self-contained file:// presentation from public UI and synthetic data.

No service imports, credentials, databases, network calls or resource probes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
STAMP = '2026-11-13T06:00:00+00:00'
GIB = 1024**3


def fixture():
    identity = {'id': 'demo_user', 'role': 'viewer', 'workspace': '/workspace/demo',
                'execution_mode': 'command', 'projects': [{'id': 'operator-demo', 'role': 'viewer'}],
                'quota': {}, 'scopes': ['jobs:read', 'monitor:read'], 'isolation': 'offline-synthetic'}
    policy = {'cpu_ids': list(range(16)), 'memory_mib': 32768, 'shm_mib': 8192,
              'max_running': 4, 'max_timeout_seconds': 3600, 'aging_seconds': 300}
    specs = [
        ('perf', 'KDA 算子 · 性能采样', 'performance', 'running', 'demo_user', 1, '已分配物理卡 1；示例采样阶段'),
        ('build', 'Triton-Ascend · 增量构建', 'build', 'running', 'demo_b', 0, 'CPU 构建阶段；不占用 NPU'),
        ('wait', 'MatMul · 精度回归', 'accuracy', 'queued', 'demo_c', 1, '等待满足环境及设备条件的执行槽位'),
        ('sim', '融合算子 · 仿真流水采集', 'simulation', 'queued', 'demo_user', 0, '等待仿真工作区；按 CPU / 内存 / 磁盘预算排队'),
        ('accuracy', '向量加法 · 精度检查', 'accuracy', 'succeeded', 'demo_user', 1, '合成结果：12 项比较一致'),
        ('ir', 'IR 导出 · 短路径归档', 'ir', 'succeeded', 'demo_user', 0, '合成结果：IR 与编译日志已归档'),
        ('perf-done', '归约算子 · A/B 性能对照', 'performance', 'succeeded', 'demo_user', 1, '合成结果：保留基线与候选样本'),
        ('compile-error', 'NPU-IR · 编译接口排查', 'build', 'failed', 'demo_user', 0, '合成错误：工具与 IR 接口不匹配；保留失败现场'),
    ]
    jobs = []
    for i, (key, name, kind, state, owner, npu, reason) in enumerate(specs):
        active = state == 'running'
        done = state in {'succeeded', 'failed'}
        resources = {'cpu': 4 if kind == 'build' else 2, 'memory_mib': 4096 if kind == 'build' else 2048,
                     'shm_mib': 256, 'npu': npu}
        allocation = {'cpu_ids': [0, 1] if active else [], 'npu_ids': [1] if active and npu else []}
        if active:
            allocation['workspace'] = {'slot_id': 'demo-slot-' + str(i), 'bytes': 8 * GIB,
                                       'inodes': 100000, 'durable': True}
        jobs.append({'id': 'job-demo-' + key, 'name': name, 'kind': kind, 'owner': owner,
                     'state': state, 'reason': reason, 'resources': resources, 'allocation': allocation,
                     'environment': 'worker-python', 'template': kind + '_v1', 'parameters': {'demo_only': True},
                     'project': 'operator-demo', 'created_at': '2026-11-13T05:20:00+00:00',
                     'started_at': '2026-11-13T05:30:00+00:00' if active or done else None,
                     'finished_at': '2026-11-13T05:50:00+00:00' if done else None,
                     'timeout_seconds': 1800, 'node_id': 'worker-demo-01' if active or done else None,
                     'attempt_id': 'attempt-demo-' + key if active or done else None,
                     'cwd': '/work', 'archive_dir': 'demo-archive/' + kind + '/' + key,
                     'archive_status': {'state': 'available'}, 'depends_on': [],
                     'origin': {'client': 'CLI / MCP（示例）', 'model': '可替换的个人 Agent', 'agent_run_id': 'demo-session'},
                     'result': {'demo_only': True, 'exit_code': 0 if state == 'succeeded' else 1,
                                'note': '仅为展示构造，没有执行真实工作负载。'} if done else None,
                     'device_conditions': {'status': 'unverified' if npu else 'not_applicable',
                                           'note': '合成数据，没有实机设备资格或测量证据。'}})
    devices = [{'id': i, 'name': 'Ascend NPU（示例）', 'health': 'OK',
                'utilization_pct': [12, 76, 0, 0][i], 'hbm_total_bytes': 64 * GIB,
                'hbm_used_bytes': [4, 28, 0, 0][i] * GIB, 'temperature_c': 39 + i * 2,
                'power_w': 70 + i * 15, 'scoped_process_count': int(i == 1),
                'external_process_count': int(i == 0)} for i in range(4)]
    processes = []
    for i, name in enumerate(['python · 性能采样', 'ninja · 增量构建', 'python · 结果归档']):
        processes.append({'pid': 10001 + i, 'start_ticks': 100 + i, 'uid': 2001,
                          'name': name, 'script': ['/work/benchmark.py', '/work/build.sh', '/work/archive.py'][i],
                          'category': ['compute', 'build', 'agent'][i],
                          'category_label': ['性能测试', '编译构建', '结果归档'][i],
                          'scopes': ['user', 'container'], 'cpu_pct': [142, 280, 4][i],
                          'rss_bytes': [1.8 * GIB, 4.2 * GIB, .2 * GIB][i], 'elapsed_seconds': 380 + i * 40,
                          'state': 'R' if i < 2 else 'S'})
    snapshot = {'sampled_at': STAMP, 'scope': {'username': 'demo_user'},
                'host': {'hostname': 'BLUE-ZONE-DEMO', 'cpu_count': 128, 'cpu_utilization_pct': 28.4,
                         'memory_total_bytes': 512 * GIB, 'memory_used_bytes': 146 * GIB,
                         'memory_available_bytes': 366 * GIB, 'swap_used_bytes': 0,
                         'workspace_disk': {'free_bytes': 780 * GIB},
                         'shm': {'used_bytes': 2 * GIB, 'total_bytes': 64 * GIB}},
                'user_summary': {'cpu_cores': 4.26, 'process_count': 3},
                'container_summary': {'cpu_cores': 4.26, 'process_count': 3},
                'container': {'status': 'running', 'name': 'demo_dev', 'image': 'operator-env:demo',
                              'id': 'demo-container-0001', 'ipc_mode': 'private',
                              'started_at': '2026-11-13T01:00:00+00:00',
                              'cgroup_memory': {'current_bytes': 6.2 * GIB, 'effective_limit_bytes': 32 * GIB},
                              'shm': {'used_bytes': .6 * GIB, 'total_bytes': 8 * GIB}},
                'processes': processes, 'warnings': [],
                'npu': {'status': 'ok', 'sampled_at': STAMP, 'devices': devices}}
    reserved = {'cpu': 6, 'memory_mib': 6144, 'shm_mib': 512, 'npu_ids': [1], 'slots': 2}
    domain = {'physical_id': 'demo-domain-01', 'policy': policy, 'reserved': reserved,
              'admission': {'blocked': False, 'reason': ''}, 'draining': False}
    summary = {'jobs': jobs, 'counts': {'queued': 2, 'running': 2, 'succeeded': 3, 'failed': 1},
               'policy': policy, 'resource_pool': policy, 'reserved': reserved, 'resource_domains': [domain],
               'environments': [{'id': 'worker-python', 'label': '受控算子环境（示例）'}],
               'identity': identity, 'default_cwd': '/workspace/demo',
               'control_storage': {'enabled': False}, 'audit_retention': {'kind': 'rolling', 'blocks_new_work': False}}
    node = {'id': 'worker-demo-01', 'name': '蓝区执行节点 · 展示样例', 'physical_id': 'demo-domain-01',
            'status': 'online', 'capacity': policy, 'resource_domain': domain, 'devices': devices,
            'enforcement': 'container-cgroups', 'runtime_pinned': True, 'npu_configured': True,
            'sampled_at': STAMP, 'device_sampled_at': STAMP, 'device_status': 'ok',
            'device_access': {'counts': {'unverified': 4}},
            'observed': {'host_cpu_utilization_pct': 28.4, 'memory_available_bytes': 366 * GIB,
                         'disk_free_bytes': 780 * GIB},
            'workspace': {'enforced': True, 'slots': [{'slot_id': 'demo-work', 'bytes': 8 * GIB,
                           'inodes': 100000, 'durable': True, 'status': 'ok', 'clean': False}]}}
    rows = []
    for j in jobs:
        if j['state'] not in {'succeeded', 'failed'}:
            continue
        metrics = {'checks': 12, 'max_abs_error': 0} if j['kind'] == 'accuracy' else (
            {'samples': 90, 'mean_us': 23.18} if j['kind'] == 'performance' else {'artifacts': 4, 'artifact_bytes': 183210})
        rows.append({'job_id': j['id'], 'name': j['name'], 'kind': j['kind'], 'passed': j['state'] == 'succeeded',
                     'metrics': metrics, 'environment_locked': True, 'recorded_at': STAMP,
                     'device_conditions': j['device_conditions'], 'demo_only': True})
    experiments = [{'id': 'demo-' + str(i), 'kind': kind, 'title': title, 'modified_at': STAMP,
                    'facts': facts, 'source': 'demo-archive/experiment-' + str(i) + '.json',
                    'data': {'demo_only': True, 'task': title, 'next_step': '对照来源并归档；此包不执行实验。'}}
                   for i, (kind, title, facts) in enumerate([
                       ('performance', '性能对照 · 从样本到结论', ['固定输入与工具版本', '保留原始采样；此处均为合成示例']),
                       ('validation', '精度检查 · 结果关联环境', ['同一任务关联日志、输入与结果表', '有限比较不等于完整硬件资格']),
                       ('environment', '环境记录 · 跨会话可交接', ['记录版本与产物来源', '个人 Agent 通过统一入口访问调度器'])])]
    return {'identity': identity, 'snapshot': snapshot, 'summary': summary, 'nodes': {'nodes': [node]},
            'rows': rows, 'experiments': experiments,
            'history': [{'ts': STAMP, 'user_cpu': x} for x in [1, 1.6, 2.2, 2, 3.2, 3.1, 4.1, 3.8, 4.26]],
            'events': [{'ts': STAMP, 'kind': kind, 'message': msg} for kind, msg in [
                ('start', '演示：性能任务获配 NPU 1；CPU 构建使用独立预算'),
                ('exit', '演示：精度任务已归档，可在结果表查看'),
                ('start', '演示：仿真任务等待工作区，不重复启动')]]}


SHIM = r'''
const OFFLINE_CLOCK = Date.parse('2026-11-13T06:00:00Z');
let offlineScenario = 'normal';
function offlineState() {
  const d = JSON.parse(JSON.stringify(OFFLINE_DATA));
  if (offlineScenario === 'pressure') {
    d.snapshot.host.memory_used_bytes = 474 * 1073741824;
    d.snapshot.host.memory_available_bytes = 38 * 1073741824;
    d.snapshot.warnings = ['合成场景：公共内存压力升高，暂停新分配；已经运行的任务继续，等待任务不重复提交。'];
    d.summary.jobs.filter(j=>j.state==='queued').forEach(j=>j.reason='等待公共内存压力恢复；不自动扩容或抢占其他任务');
    const a=d.summary.resource_domains[0].admission;a.blocked=true;a.reason='公共内存压力（合成演示）';
    d.nodes.nodes[0].resource_domain=JSON.parse(JSON.stringify(d.summary.resource_domains[0]));
  } else if (offlineScenario === 'unknown') {
    const j=d.summary.jobs[0];j.state='needs_attention';j.reason='执行回执中断；保留原资源和任务，不自动重跑（合成场景）';
    d.snapshot.warnings=['合成场景：执行状态待核对；取消请求不等于进程已经退出。'];
    d.nodes.nodes[0].status='unknown';
  }
  return d;
}
async function offlineAPI(path, body, signal) {
  if(signal?.aborted)throw new DOMException('Aborted','AbortError');
  if(body!==undefined)throw new Error('离线展示包不执行提交、取消或任何服务端操作。');
  const u=new URL(path,'https://offline.invalid'),d=offlineState();
  const p=u.pathname;
  if(p==='/api/me')return d.identity;
  if(p==='/api/snapshot')return {snapshot:d.snapshot};
  if(p==='/api/history')return d.history;
  if(p==='/api/events')return d.events;
  if(p==='/api/experiments')return d.experiments;
  if(p==='/api/jobs')return d.summary;
  if(p==='/api/activity')return {jobs:d.summary.jobs.filter(j=>['queued','running','needs_attention'].includes(j.state))};
  if(p==='/api/nodes')return d.nodes;
  if(p==='/api/results') {
    const rows=d.rows.filter(r=>!u.searchParams.get('kind')||r.kind===u.searchParams.get('kind'));
    const offset=Number(u.searchParams.get('offset')||0),limit=50;
    return {rows:rows.slice(offset,offset+limit),total:rows.length,offset,limit};
  }
  if(p==='/api/export')return {demo_only:true,notice:'全部数据为合成示例，非实机记录',...d};
  if(p==='/api/device-schedules')return {plans:[]};
  if(p==='/api/device-evidence')return {policies:[],total:0,offset:0,limit:4};
  if(p==='/api/performance-references')return {references:[],total:0,offset:0,limit:4};
  const match=p.match(new RegExp('^/api/jobs/([^/]+)(?:/(log|artifact))?$'));
  if(match) {
    const j=d.summary.jobs.find(j=>j.id===decodeURIComponent(match[1]));
    if(!j)throw new Error('此演示任务不存在。');
    if(match[2]==='log')return {text:u.searchParams.get('stream')==='stderr'?(j.state==='failed'?'[DEMO] 编译工具与 IR 接口不匹配；保留现场，未自动重试。\n':''):
      '[OFFLINE DEMO / 合成日志]\n任务编号：'+j.id+'\n任务：'+j.name+'\n环境：operator-env:demo\n资源：'+j.resources.cpu+' CPU / '+j.resources.memory_mib+' MiB / '+j.resources.npu+' NPU\n状态：'+j.state+'\n说明：'+j.reason+'\n此包未执行以上命令或检查。\n',truncated:false};
    if(match[2]==='artifact')return {demo_only:true,job_id:j.id,result:j.result,note:'这是合成演示产物，无真实 IR / profiler / wheel 文件。'};
    return {job:j,permissions:{cancel:false,storage:false},artifacts:[{name:'results/demo-summary.json',bytes:256}]};
  }
  throw new Error('离线包未包含该操作：'+p);
}
function offlineSave(name,content,type='application/json') {
  const url=URL.createObjectURL(new Blob([content],{type})),a=document.createElement('a');
  a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
document.addEventListener('click',async event=>{
  const link=event.target.closest('a[href^="/api/"]');if(!link)return;
  event.preventDefault();event.stopImmediatePropagation();
  try {
    if(link.getAttribute('href').startsWith('/api/results.csv')) {
      const d=await offlineAPI('/api/results?'+new URL(link.getAttribute('href'),'https://offline.invalid').searchParams);
      const cell=v=>'"'+String(v??'').replaceAll('"','""')+'"';
      const rows=[['DEMO_ONLY','job_id','name','kind','passed','metrics'],...d.rows.map(r=>[true,r.job_id,r.name,r.kind,r.passed,JSON.stringify(r.metrics)])];
      offlineSave('offline-demo-results.csv','\ufeff'+rows.map(r=>r.map(cell).join(',')).join('\r\n'),'text/csv;charset=utf-8');
    } else offlineSave('offline-demo-record.json',JSON.stringify(await offlineAPI(link.getAttribute('href')),null,2));
  } catch(error){toast(error.message);}
},true);
'''

TAIL = r'''
function offlineLabels() {
  document.getElementById('connection').innerHTML='<i></i>离线展示';
  document.getElementById('updated-at').textContent='合成快照 · 2026-11-13 14:00（演示时间）';
  document.getElementById('npu-updated').textContent='合成设备数据 · 不查询 NPU';
  document.getElementById('auto-refresh').hidden=true;
  document.getElementById('refresh').textContent='↻ 重载示例';
  document.getElementById('job-access-note').textContent='离线展示：可查看任务与结果；提交、取消和清理均不执行。';
  document.getElementById('job-access-note').hidden=false;
}
const originalRefresh=refresh;
refresh=async function(manual=false){await originalRefresh(manual);offlineLabels();};
document.getElementById('demo-scenario').addEventListener('change',async event=>{
  offlineScenario=event.target.value;closeDrawer();await refresh();toast('已切换离线演示场景，不会执行任务。');
});
offlineLabels();
'''

README = '''# Ascend Lab 离线网页展示包

解压后，双击 `index.html`。也可以只复制该 HTML 文件；样式、脚本和数据全部内嵌。
使用较新的 Chrome / Edge / Firefox，无需 Python、Node、Docker、账号或服务器。

## 建议展示顺序

1. 运行总览：资源卡片、设备占用、托管任务和动态记录。
2. 托管作业：排队、运行、失败与已完成的任务；点击箭头查看任务详情及合成日志。
3. 任务结果：精度、性能、IR 和构建结果；按类型筛选，导出带 DEMO_ONLY 标记的 CSV。
4. 进程观察、容器环境、实验记录：搜索、排序和详情抽屉。
5. 切换顶部场景：日常协作、资源紧张、异常待核对，演示等待原因和资源保留。

## 展示范围

- 复用项目当前网页布局和渲染逻辑，增加离线数据适配和场景切换。
- 全部用户、主机、任务、日志、资源和测量数值均为合成示例，不来自服务器或个人会话。
- 场景仅用于说明预期行为，不证明生产部署、硬件健康、数值正确性或算子性能收益。
- 固定展示时间为 2026-11-13 14:00（Asia/Shanghai），没有实时刷新。
- 不连接 API、模型、CDN、字体站点或 NPU；不收集浏览器信息，不持久保存数据。
- 提交、取消、清理、节点控制和夜间任务均不执行。导出只生成浏览器本地示例文件。
- 没有真实 IR、wheel、输入张量或 profiler 原始数据；产物下载是标注过的示例 JSON。
- `manifest.json` 记录包内文件的大小和 SHA256，便于离线核对。

若不能下载本地 CSV，可在 Chrome / Edge 中重新打开 HTML；该功能受浏览器本地下载策略影响。
'''


def build(destination):
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    html = (ROOT / 'web/index.html').read_text()
    css = (ROOT / 'web/style.css').read_text()
    app = (ROOT / 'web/app.js').read_text()
    pattern = r'async function api\(path, body, signal\) \{.*?\n\}\n\nfunction toast'
    app, count = re.subn(pattern, 'async function api(path, body, signal) { return offlineAPI(path, body, signal); }\n\nfunction toast', app, flags=re.S)
    assert count == 1, 'API entry changed; inspect UI before rebuilding'
    app = app.replace('automatic = true', 'automatic = false').replace('Date.now()', 'OFFLINE_CLOCK')
    loop = 'setInterval(()=>{if(automatic&&!document.hidden) refresh();},5000);'
    assert app.count(loop) == 1
    app = app.replace(loop, '')
    # Delay initial rendering until all original listeners and offline adapters exist.
    initial = "setView(location.hash.slice(1));\nrefresh();"
    assert app.count(initial) == 1
    app = app.replace(initial, "setView(location.hash.slice(1));")
    app = app.replace('真实进程', '合成进程样例').replace('实时连接', '离线展示')
    app = app.replace('每 15 秒更新', '固定示例快照').replace('每 30 秒查询', '离线合成示例')
    app = app.replace('已读取最新采样；后台每 15 秒更新。', '已重新载入合成示例；不连接服务器。')
    app = app.replace('这是既有实验的原始记录。本次只读取该文件，没有重新执行测试，也不将历史状态当作当前设备状态。',
                      '这是离线演示构造的记录，不是真实实验、实际分析结果或当前设备状态。')
    for old, new in [('m00951283', 'demo_user'), ('zdl', 'demo_dev')]:
        html = html.replace(old, new)
        app = app.replace(old, new)
    html = html.replace('<title>Ascend Lab · 个人实验工作台</title>', '<title>Ascend Lab · 离线展示</title>')
    html = html.replace('<link rel="icon" type="image/svg+xml" href="/favicon.svg">', '<link rel="icon" href="data:,">')
    html = html.replace('<link rel="stylesheet" href="/style.css">', '<style>' + css + '\n' +
                        '.offline-banner{border:1px solid #d4dacc;background:#f1f4ed;padding:16px 18px;border-radius:12px;margin-bottom:24px;display:flex;justify-content:space-between;gap:16px;align-items:center}.offline-banner strong{font-size:14px}.offline-banner p{margin:7px 0 0;font-size:12px;line-height:1.6;color:#5b6658}.offline-banner select{padding:9px;max-width:100%;border:1px solid #c8d0c2;border-radius:7px;background:white}.offline-banner label{font-size:12px;white-space:nowrap}@media(max-width:620px){.offline-banner{align-items:flex-start;flex-direction:column}}' + '</style>')
    html = html.replace('<script src="/app.js" defer></script>', '')
    csp = '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data: blob:; connect-src \'none\'; object-src \'none\'; base-uri \'none\'; form-action \'none\'">'
    html = html.replace('<meta charset="UTF-8">', '<meta charset="UTF-8">\n  ' + csp)
    banner = '<section class="offline-banner" aria-label="离线演示说明"><div><strong>离线展示 / OFFLINE DEMO</strong><p>全部数据均为合成示例，非服务器实时状态或实机验证结果。<br>无需登录与联网；可查看详情、筛选和导出，不执行任何任务。</p></div><label>演示场景 <select id="demo-scenario"><option value="normal">日常协作</option><option value="pressure">资源紧张 · 等待准入</option><option value="unknown">异常待核对 · 保留资源</option></select></label></section>'
    html = html.replace('<div class="page">', '<div class="page">\n' + banner)
    html = html.replace('个人队列已启用', '离线示例 · 不执行任务').replace('个人空间 · 私有访问', '合成账号 · 无需登录')
    html = html.replace('单机协作式调度 · 内存按申报预留 · OpenCode / MCP 已接入', '离线界面演示 · 合成数据 · 不连接任何服务')
    html = html.replace('设备每 30 秒查询', '离线合成设备数据')
    data = json.dumps(fixture(), ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c')
    javascript = 'const OFFLINE_DATA=' + data + ';\n' + SHIM + '\n' + app + '\n' + TAIL + '\nrefresh();\n'
    assert 'fetch(' not in javascript and 'setInterval(' not in javascript
    assert '</script' not in javascript.lower()
    html = html.replace('</body>', '<script>\n' + javascript + '\n</script>\n</body>')
    (destination / 'index.html').write_text(html)
    (destination / 'README.md').write_text(README)
    manifest = {'format': 'ascend-lab-offline-demo/v1', 'data': 'synthetic-only',
                'snapshot_time': STAMP, 'live_services_required': False, 'files': {}}
    for name in ['index.html', 'README.md']:
        raw = (destination / name).read_bytes()
        manifest['files'][name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    archive = destination.with_suffix('.zip')
    assert not archive.exists(), 'Archive already exists; choose a fresh output directory'
    with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(destination.iterdir()):
            bundle.write(path, destination.name + '/' + path.name)
    print(json.dumps({'html': str(destination / 'index.html'), 'zip': str(archive),
                      'zip_bytes': archive.stat().st_size, 'data': 'synthetic-only'}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/offline-demo-20261113')
    build(parser.parse_args().output.resolve())
