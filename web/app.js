'use strict';
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const valid = value => typeof value === 'number' && Number.isFinite(value);
const num = (value, digits = 1) => valid(value) ? value.toLocaleString('en-US', {maximumFractionDigits:digits, minimumFractionDigits:digits}) : '—';
const gib = value => valid(value) ? num(value / 1073741824) : '—';
const bytes = value => !valid(value) ? '—' : value >= 1073741824 ? gib(value) + ' GiB' : num(value / 1048576) + ' MiB';
const clock = value => value ? new Date(value).toLocaleTimeString('zh-CN', {hour12:false, timeZone:'Asia/Shanghai'}) : '—';
const date = value => value ? new Date(value).toLocaleDateString('zh-CN', {timeZone:'Asia/Shanghai'}) : '—';
const elapsed = value => !valid(value) ? '—' : value < 60 ? Math.floor(value)+' 秒' : value < 3600 ? Math.floor(value/60)+' 分钟' : value < 86400 ? num(value/3600,1)+' 小时' : num(value/86400,1)+' 天';
let snapshot = null, records = [], activeScope = 'all', automatic = true, refreshing = false, currentView = 'overview';
let lastProcessFocus = null;
let jobSummary = null;
let identity = null;
let pendingSubmission = null;
let previewSequence = 0, previewTimer = null, submittingJob = false;
let previewController = null;
let resultOffset=0;
const views = {
  overview: ['运行总览','看清资源占用，让每一次实验有据可循。'],
  jobs: ['托管作业','通过统一队列执行任务，持续保留状态、日志和实验产物。'],
  results: ['任务结果','按类型查看结果，关联可复核的输入、环境和原始证据。'],
  processes: ['进程观察','从当前用户和容器的真实进程，了解正在发生的工作。'],
  container: ['容器环境','区分容器边界与主机容量，保留熟悉的开发环境。'],
  experiments: ['实验记录','把结果与原始证据联系起来，继续推进下一次实验。']
};

async function api(path, body, signal) {
  const response = await fetch(path, {cache:'no-store', credentials:'same-origin',
    ...(signal?{signal}:{}),
    ...(body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})});
  if (!response.ok) {
    let message='状态服务暂时不可用。';
    try { message=(await response.json()).error||message; } catch {}
    throw new Error(response.status === 401 ? '访问认证已失效，请刷新页面重新登录。' : message);
  }
  return response.json();
}

function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {$('toast').hidden = true;}, 3500);
}

function setView(view) {
  if(identity && !identity.scopes.includes('monitor:read') && !['jobs','results'].includes(view)) view='jobs';
  currentView = Object.hasOwn(views, view) ? view : 'overview';
  document.querySelectorAll('[data-view]').forEach(button => {
    button.classList.toggle('active', button.dataset.view === currentView);
    button.setAttribute('aria-current', button.dataset.view === currentView ? 'page' : 'false');
  });
  document.querySelectorAll('[data-section]').forEach(section => {
    section.hidden = !section.dataset.section.split(' ').includes(currentView);
  });
  const [title, description] = views[currentView];
  $('page-title').innerHTML = esc(title) + '<span class="title-dot">.</span>';
  $('breadcrumb-current').textContent = title;
  $('page-description').textContent = description;
}

function renderDevices(npu) {
  $('npu-updated').textContent = npu.sampled_at ? '设备采样 '+clock(npu.sampled_at)+(npu.status === 'error' ? ' · 上次读数 / 已过期' : ' · 每 30 秒查询') : '正在进行首次设备查询';
  if (!npu.devices.length) {
    $('devices').innerHTML = '<div class="placeholder device-placeholder">'+(npu.status === 'error' ? 'NPU 查询暂不可用。其他资源仍会继续采样；缺失数据不会记为零。' : '正在查询 npu-smi，首次结果可能稍后到达…')+'</div>';
    return;
  }
  const stale = npu.status !== 'ok' || Date.now() - new Date(npu.sampled_at) > 75000;
  $('devices').innerHTML = npu.devices.map(d => {
    const reservation=jobSummary?.jobs.find(j=>['reserved','running','cancelling','needs_attention'].includes(j.state)&&j.allocation.npu_ids?.includes(d.id));
    const utilization = valid(d.utilization_pct) ? Math.max(0,Math.min(100,d.utilization_pct)) : 0;
    const memoryPct = d.hbm_total_bytes ? d.hbm_used_bytes / d.hbm_total_bytes * 100 : 0;
    const good = d.health === 'OK' && !stale;
    return `<article class="device-card"><div class="device-top"><div class="device-name"><span class="chip-icon">▧</span>NPU ${esc(d.id)} <small>${esc(d.name)}</small></div><span class="status-tag ${good ? '' : 'warn'}">${stale ? '上次读数' : esc(d.health)}</span></div><div class="device-middle"><svg class="gauge" viewBox="0 0 80 80" aria-label="设备利用率 ${esc(d.utilization_pct ?? '未知')}%"><circle class="track" cx="40" cy="40" r="32"/><circle class="fill ${utilization>85 ? 'hot' : ''}" cx="40" cy="40" r="32" pathLength="100" stroke-dasharray="${utilization} 100"/><text class="gauge-value" x="40" y="45">${num(d.utilization_pct,0)}${valid(d.utilization_pct) ? '%' : ''}</text></svg><div class="device-memory">HBM 使用<strong>${gib(d.hbm_used_bytes)} <small>/ ${gib(d.hbm_total_bytes)} GiB</small></strong><progress max="100" value="${memoryPct}" aria-label="HBM 使用率"></progress></div></div><div class="device-details"><span>温度<strong>${num(d.temperature_c,0)} °C</strong></span><span>功率<strong>${num(d.power_w,1)} W</strong></span><span>我的进程<strong>${d.scoped_process_count}</strong></span></div><div class="device-bottom"><span>${reservation ? '<strong>队列预留 · '+esc(reservation.id)+'</strong>' : d.scoped_process_count ? '<strong>范围内设备进程 '+d.scoped_process_count+' 个</strong>' : '当前范围未观察到设备进程'}</span><span class="external">外部进程 ${d.external_process_count} · ${reservation?'已预留':'未预留'}</span></div></article>`;
  }).join('');
}

function renderProcesses() {
  if (!snapshot) return;
  const query = $('search').value.trim().toLowerCase();
  let rows = snapshot.processes.filter(p => (activeScope === 'all' || p.scopes.includes(activeScope)) && `${p.name} ${p.pid} ${p.script} ${p.category_label}`.toLowerCase().includes(query));
  const sort = $('sort').value;
  rows.sort((a,b) => sort === 'memory' ? b.rss_bytes-a.rss_bytes : sort === 'recent' ? a.elapsed_seconds-b.elapsed_seconds : (b.cpu_pct??-1)-(a.cpu_pct??-1) || b.rss_bytes-a.rss_bytes);
  $('process-count').textContent = snapshot.processes.length;
  $('nav-process-count').textContent = snapshot.processes.length;
  $('process-body').innerHTML = rows.length ? rows.slice(0,150).map(p => {
    const state = p.state === 'R' ? '运行' : p.state === 'S' ? '等待' : p.state === 'D' ? 'I/O 等待' : p.state === 'Z' ? '僵尸' : p.state === 'T' ? '暂停' : p.state;
    return `<tr><td><div class="process-identity"><span class="process-icon ${esc(p.category)}">${p.category === 'agent' ? 'ai' : p.category === 'build' ? '⌘' : p.category === 'profile' ? '↝' : '>_'}</span><div><div class="process-name">${esc(p.name)}</div>${p.script ? '<div class="process-script">'+esc(p.script)+'</div>' : ''}<div class="pid">PID ${p.pid}</div></div></div></td><td><span class="type-label">${esc(p.category_label)}</span></td><td><span class="scope-tag">${p.scopes.includes('container') ? 'Docker / zdl' : esc(snapshot.scope.username)}</span></td><td class="mono ${p.cpu_pct>50 ? 'number-strong' : ''}">${num(p.cpu_pct)}${valid(p.cpu_pct) ? '%' : ''}</td><td class="mono">${bytes(p.rss_bytes)}</td><td class="mono">${elapsed(p.elapsed_seconds)}</td><td><span class="process-state ${p.state === 'R' ? 'running' : ''}"><i></i>${esc(state)}</span></td><td><button class="detail-button" data-pid="${p.pid}" aria-label="查看 PID ${p.pid} 详情">↗</button></td></tr>`;
  }).join('') : '<tr><td colspan="8" class="empty-state">没有匹配的进程。可以切换范围或清空搜索。</td></tr>';
  $('table-summary').textContent = `显示 ${Math.min(rows.length,150)} / ${rows.length} 个匹配进程 · 采样 ${clock(snapshot.sampled_at)}`;
}

function fact(label, value) {
  return `<div><div class="fact-label">${esc(label)}</div><div class="fact-value">${esc(value)}</div></div>`;
}

function renderSnapshot(s) {
  snapshot = s;
  $('hostname').textContent = s.host.hostname.toUpperCase();
  $('scope-user').textContent = $('sidebar-user').textContent = s.scope.username;
  const age = (Date.now() - new Date(s.sampled_at))/1000;
  $('updated-at').textContent = `采样 ${clock(s.sampled_at)} · ${age>40 ? '数据已过期' : '每 15 秒更新'}`;
  $('connection').classList.toggle('offline',age>40);
  $('connection').innerHTML = '<i></i>'+(age>40 ? '采样延迟' : '实时连接');
  const devices = s.npu.devices;
  $('metric-npu').innerHTML = (devices.length || '—')+'<small>张</small>';
  $('metric-npu-note').textContent = devices.length ? `${devices.filter(d=>d.health==='OK').length} 张健康 · 利用率不代表可分配` : '等待设备状态';
  $('metric-cpu').innerHTML = num(s.user_summary.cpu_cores,2)+'<small>核</small>';
  $('metric-cpu-note').textContent = `${s.user_summary.process_count} 个进程 · 单独统计当前 UID`;
  $('metric-memory').innerHTML = gib(s.host.memory_used_bytes)+'<small>GiB</small>';
  $('metric-memory-note').textContent = `总计 ${gib(s.host.memory_total_bytes)} GiB · 含其他用户`;
  const c = s.container;
  const mem = c.cgroup_memory?.current_bytes;
  $('metric-container').innerHTML = gib(mem)+'<small>GiB</small>';
  $('metric-container-note').textContent = valid(mem) ? `${s.container_summary.process_count} 个进程 · cgroup 统计` : 'cgroup 内存暂不可读';
  renderDevices(s.npu);
  renderProcesses();
  $('container-status').textContent = c.status === 'running' ? 'RUNNING' : c.status.toUpperCase();
  $('container-status').classList.toggle('warn',c.status !== 'running');
  $('container-facts').innerHTML = fact('镜像',c.image??'—')+fact('容器 ID',c.id?.slice(0,12)??'—')+fact('进程 CPU 合计',valid(s.container_summary.cpu_cores) ? num(s.container_summary.cpu_cores,2)+' 核' : '等待连续采样')+fact('有效内存上限',c.cgroup_memory ? c.cgroup_memory.effective_limit_bytes ? bytes(c.cgroup_memory.effective_limit_bytes) : '未发现 cgroup 硬上限' : '暂不可读')+fact('IPC 模式',c.ipc_mode??'—')+fact('容器启动时间',c.started_at ? date(c.started_at)+' '+clock(c.started_at) : '—');
  $('shm-value').textContent = c.shm ? bytes(c.shm.used_bytes)+' / '+gib(c.shm.total_bytes)+' GiB' : '暂不可读';
  $('shm-progress').value = c.shm?.total_bytes ? c.shm.used_bytes/c.shm.total_bytes*100 : 0;
  $('shm-note').textContent = '这是共享内存容量上限，实际使用仍消耗主机内存。';
  $('host-facts').innerHTML = fact('逻辑 CPU',s.host.cpu_count+' 核')+fact('主机 CPU 使用率',num(s.host.cpu_utilization_pct)+'%')+fact('主机可用内存',bytes(s.host.memory_available_bytes))+fact('工作区磁盘可用',bytes(s.host.workspace_disk.free_bytes))+fact('主机 /dev/shm',bytes(s.host.shm.used_bytes)+' / '+bytes(s.host.shm.total_bytes))+fact('Swap 已用',bytes(s.host.swap_used_bytes));
  const warnings = [...s.warnings];
  if (age>40) warnings.push('采样已延迟。当前显示上次数据，请检查监控服务状态。');
  if (s.npu.sampled_at && Date.now()-new Date(s.npu.sampled_at)>75000) warnings.push('NPU 读数已过期，请勿据此判断当前设备空闲。');
  $('warning-box').textContent = warnings.join(' ');
  $('warning-box').hidden = !warnings.length;
}

function renderHistory(history) {
  const values = history.slice(-30).map(h=>h.user_cpu);
  if (!values.some(valid)) { $('cpu-spark').innerHTML=''; return; }
  const max = Math.max(.1,...values.filter(valid));
  // Null samples break the line; missing data is never plotted as measured zero.
  const segments=[];
  let current=[];
  values.forEach((v,i)=>{if(valid(v)) current.push(`${i*90/Math.max(1,values.length-1)},${23-v/max*21}`);else if(current.length){segments.push(current);current=[];}});
  if(current.length) segments.push(current);
  $('cpu-spark').innerHTML=segments.map(points=>`<polyline points="${points.join(' ')}"/>`).join('');
}

function renderEvents(events) {
  $('events').innerHTML = events.length ? events.slice(0,8).map(event=>`<div class="event"><span class="event-dot ${esc(event.kind)}"></span><div><div class="event-text">${esc(event.message)}</div><div class="event-time">${date(event.ts)} · ${clock(event.ts)}</div></div></div>`).join('') : '<div class="empty-state">尚未观察到进程变化。</div>';
}

function renderExperiments(items) {
  records = items;
  const labels={performance:'PERFORMANCE',validation:'VALIDATION',environment:'ENVIRONMENT'};
  $('experiments').innerHTML=items.map(item=>`<button class="experiment-card" data-record="${esc(item.id)}"><div class="experiment-kicker"><span>${labels[item.kind]??'RECORD'}</span><span class="arrow">↗</span></div><h3>${esc(item.title)}</h3>${item.facts.map(f=>'<p>'+esc(f)+'</p>').join('')}<div class="experiment-date">${date(item.modified_at)} <span>· 查看原始记录</span></div></button>`).join('');
}

async function refresh(manual=false) {
  if(refreshing) return;
  refreshing=true;
  $('refresh').disabled=true;
  try {
    const previousIdentity=identity;
    identity=await api('/api/me');
    if(previousIdentity&&(previousIdentity.id!==identity.id||
       previousIdentity.scopes.includes('jobs:submit')&&!identity.scopes.includes('jobs:submit')))clearJobPreview();
    updateJobActions();
    const monitor=identity.scopes.includes('monitor:read');
    $('sidebar-user').textContent=identity.id;
    $('scope-user').textContent=identity.id;
    document.querySelectorAll('[data-monitor-only]').forEach(element=>element.hidden=!monitor);
    document.querySelectorAll('[data-view]').forEach(element=>element.hidden=!monitor&&!['jobs','results'].includes(element.dataset.view));
    if(!monitor && !['jobs','results'].includes(currentView)) setView('jobs');
    const responses = await Promise.allSettled([
      ...['/api/snapshot','/api/history','/api/events','/api/experiments'].map(path=>monitor?api(path):Promise.resolve(null)),
      api('/api/jobs'),api('/api/activity'),api('/api/nodes'),api(workflowResultPath())]);
    if(responses[4].status==='fulfilled') renderManagedJobs(responses[4].value);
    if(responses[5].status==='fulfilled') renderActivity(responses[5].value);
    if(responses[6].status==='fulfilled') renderNodes(responses[6].value);
    if(responses[7].status==='fulfilled') renderWorkflowResults(responses[7].value);
    if(responses[0].status==='rejected') throw responses[0].reason;
    const result=responses[0].value;
    if(monitor && result.snapshot) renderSnapshot(result.snapshot);
    else if(monitor) {$('updated-at').textContent='正在准备第一份采样…';$('connection').innerHTML='<i></i>采样启动中';}
    else {
      $('updated-at').textContent='按账号与项目授权 · 执行隔离取决于所选环境';
      $('connection').innerHTML='<i></i>身份已验证';$('connection').classList.remove('offline');
      $('warning-box').hidden=true;
    }
    if(monitor && responses[1].status==='fulfilled') renderHistory(responses[1].value);
    if(monitor && responses[2].status==='fulfilled') renderEvents(responses[2].value);
    if(monitor && responses[3].status==='fulfilled') renderExperiments(responses[3].value);
    if(responses.slice(1).some(r=>r.status==='rejected')) toast('部分记录暂时无法加载，实时状态仍可查看。');
    else if(manual) toast('已读取最新采样；后台每 15 秒更新。');
  } catch(error) {
    $('connection').innerHTML='<i></i>连接中断';
    $('connection').classList.add('offline');
    $('warning-box').textContent=error.message+' 页面保留上次读数，请留意采样时间。';
    $('warning-box').hidden=false;
  } finally {
    refreshing=false;
    $('refresh').disabled=false;
  }
}

function openDrawer(title, html) {
  lastProcessFocus=document.activeElement;
  $('drawer-title').textContent=title;
  $('drawer-content').innerHTML=html;
  $('drawer').hidden=$('drawer-backdrop').hidden=false;
  $('close-drawer').focus();
}

function closeDrawer() {
  $('drawer').hidden=$('drawer-backdrop').hidden=true;
  lastProcessFocus?.focus();
}

document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>{location.hash=button.dataset.view;}));
window.addEventListener('hashchange',()=>setView(location.hash.slice(1)));
document.querySelectorAll('[data-scope]').forEach(button=>button.addEventListener('click',()=>{
  activeScope=button.dataset.scope;
  document.querySelectorAll('[data-scope]').forEach(b=>b.classList.toggle('active',b===button));
  renderProcesses();
}));
$('search').addEventListener('input',renderProcesses);
$('sort').addEventListener('change',renderProcesses);
$('refresh').addEventListener('click',()=>refresh(true));
$('auto-refresh').addEventListener('click',()=>{
  automatic=!automatic;
  $('auto-refresh').setAttribute('aria-pressed',String(automatic));
  $('auto-refresh').textContent='自动刷新 · '+(automatic?'开':'关');
  if(automatic) refresh();
});
$('process-body').addEventListener('click',event=>{
  const button=event.target.closest('[data-pid]');
  if(!button||!snapshot) return;
  const p=snapshot.processes.find(p=>p.pid===Number(button.dataset.pid));
  if(!p) return toast('该进程已离开当前采样。');
  const labels=[['PID',p.pid],['进程名称',p.name],['脚本名',p.script||'未展示 / 不适用'],['所属 UID',p.uid],['观察范围',p.scopes.map(x=>x==='user'?snapshot.scope.username:'Docker / zdl').join(' + ')],['CPU',num(p.cpu_pct)+'%（单核=100%）'],['内存 RSS',bytes(p.rss_bytes)],['运行时长',elapsed(p.elapsed_seconds)],['用途',p.category_label],['采样时间',date(snapshot.sampled_at)+' '+clock(snapshot.sampled_at)]];
  openDrawer(p.name+' · '+p.pid,'<dl>'+labels.map(([key,value])=>'<dt>'+esc(key)+'</dt><dd>'+esc(value)+'</dd>').join('')+'</dl><div class="detail-note">进程用途由名称推断，不能区分精度测试和性能测试。此处仅观察状态；不提供停止进程、设备重置或命令执行操作。</div>');
});
$('experiments').addEventListener('click',event=>{
  const button=event.target.closest('[data-record]');
  const record=records.find(r=>r.id===button?.dataset.record);
  if(!record) return;
  openDrawer(record.title,'<dl><dt>来源文件</dt><dd>'+esc(record.source)+'</dd><dt>记录时间</dt><dd>'+esc(date(record.modified_at))+'</dd></dl><button class="copy-button" id="copy-source">复制记录路径</button><pre>'+esc(JSON.stringify(record.data,null,2))+'</pre><div class="detail-note">这是既有实验的原始记录。本次只读取该文件，没有重新执行测试，也不将历史状态当作当前设备状态。</div>');
  $('copy-source').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(record.source);toast('记录路径已复制');}catch{toast('请从上方详情手动复制路径');}});
});
$('close-drawer').addEventListener('click',closeDrawer);
$('drawer-backdrop').addEventListener('click',closeDrawer);
document.addEventListener('keydown',event=>{
  if(event.key==='Escape') closeDrawer();
  if(event.key==='Tab'&&!$('drawer').hidden){
    const items=[...$('drawer').querySelectorAll('button,a,input,select,[tabindex="0"]')];
    const first=items[0],last=items.at(-1);
    if(event.shiftKey&&document.activeElement===first){last.focus();event.preventDefault();}
    else if(!event.shiftKey&&document.activeElement===last){first.focus();event.preventDefault();}
  }
});
setView(location.hash.slice(1));
refresh();
setInterval(()=>{if(automatic&&!document.hidden) refresh();},5000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&automatic) refresh();});

const jobStates={queued:'排队中',reserved:'已预留',running:'运行中',cancelling:'正在取消',needs_attention:'需人工核对',succeeded:'成功',failed:'失败',cancelled:'已取消',timed_out:'已超时'};
function jobStatus(job){
  if(job.result?.failure_stage==='dependency')return '上游失败，未启动';
  if(job.state==='queued'&&job.depends_on?.length&&job.dependency_state?.status!=='ready')return ({waiting:'等待上游',copying:'产物交接中',waiting_storage:'交接待恢复',blocked:'交接需核对'})[job.dependency_state?.status]||'等待阶段依赖';
  return job.state==='needs_attention'&&job.cache_pending?'缓存待恢复':job.state==='needs_attention'&&job.output_pending?(job.execution_ended?'已执行，待归档':'暂存待恢复'):(jobStates[job.state]||job.state);
}
function queueWaitDescription(job,threshold=300) {
  if(job.state!=='queued') return '';
  const seconds=Math.max(0,Math.floor((Date.now()-Date.parse(job.created_at))/1000));
  if(!Number.isFinite(seconds)) return '';
  return `已等待 ${seconds} 秒${seconds>=threshold?' · 老化优先，仍需满足运行条件':''}`;
}
function renderManagedJobs(summary) {
  jobSummary=summary;
  const active=['reserved','running','cancelling','needs_attention'];
  const running=summary.jobs.filter(j=>active.includes(j.state)).length;
  $('nav-job-count').textContent=summary.counts.queued+running;
  $('job-count').textContent=summary.jobs.length;
  $('job-summary').textContent=`${running} 个占用资源 · ${summary.counts.queued} 个等待 · 作业独立于页面运行`;
  const domains=summary.resource_domains||[];
  const pools=domains.map(domain=>domain.policy);
  const pool=pools.length?{cpu_ids:Array(pools.reduce((n,p)=>n+p.cpu_ids.length,0)),memory_mib:pools.reduce((n,p)=>n+p.memory_mib,0),shm_mib:pools.reduce((n,p)=>n+p.shm_mib,0),max_running:pools.reduce((n,p)=>n+p.max_running,0)}:summary.resource_pool||summary.policy;
  const reservedMemory=domains.length?domains.reduce((n,domain)=>n+domain.reserved.memory_mib,0):summary.reserved.memory_mib;
  $('job-budget').innerHTML=`<span>登记 CPU 预留 <strong>${summary.reserved.cpu} / ${pool.cpu_ids.length} 核</strong></span><span>内存预留（含执行开销） <strong>${reservedMemory} / ${pool.memory_mib} MiB</strong></span><span>其中 SHM <strong>${summary.reserved.shm_mib||0} / ${pool.shm_mib} MiB</strong></span><span>登记并发总预算 <strong>${pool.max_running}</strong></span><span>NPU 预留 <strong>${summary.reserved.npu_ids.length} 张</strong></span>`;
  const storageQuota=summary.storage?.filesystem_quota;
  $('job-budget').innerHTML+=storageQuota?.enforced
    ? `<span>本人中央存储硬限额 <strong>${bytes(storageQuota.bytes_used)} / ${bytes(storageQuota.byte_hard)}</strong></span><span>本人存储 inode <strong>${num(storageQuota.inodes_used,0)} / ${num(storageQuota.inode_hard,0)}</strong></span>`
    : '<span>中央存储：未启用每用户文件系统硬限额</span>';
  $('job-policy').textContent=`个人预算：${summary.policy.cpu_ids.length} CPU / ${summary.policy.memory_mib} MiB，最多同时运行 ${summary.policy.max_running} 个作业。等待超过 ${summary.policy.aging_seconds??300} 秒的任务启用老化优先，仍需通过权限和环境检查。`;
  $('job-cpu').max=summary.policy.cpu_ids.length;
  $('job-memory').max=summary.policy.memory_mib;
  $('job-shm').max=summary.policy.shm_mib||1024;
  $('job-timeout').max=summary.policy.max_timeout_seconds;
  if(!$('job-form').dataset.initialized){
    $('job-environment').innerHTML=summary.environments.map(e=>`<option value="${esc(e.id)}">${esc(e.label)}</option>`).join('');
    if(summary.environments.some(e=>e.id==='host-python')) $('job-environment').value='host-python';
    $('job-cwd').value=summary.default_cwd||'/home/m00951283/ascend-lab-demo';
    $('job-project').innerHTML=(summary.identity?.projects||[]).filter(p=>p.role!=='viewer').map(p=>`<option value="${esc(p.id)}">${esc(p.id)}</option>`).join('');
    if(summary.identity?.execution_mode==='templates') {
      $('job-template').innerHTML='<option value="cpu_demo">CPU 验收模板</option>'+(summary.environments.some(e=>e.id==='worker-python')&&summary.policy.shm_mib>=16?'<option value="isolation_probe">容器隔离验证模板</option>':'');
      $('job-template').value='cpu_demo';
    }
    configureTemplate();
    $('job-form').dataset.initialized='true';
  }
  $('job-body').innerHTML=summary.jobs.length ? summary.jobs.map(j=>`<tr><td><div class="process-name">${esc(j.name)}</div><div class="pid">${esc(j.id)}</div></td><td><span class="job-state ${esc(j.state)}">${esc(jobStatus(j))}</span><div class="job-reason">${esc(j.reason)}</div><div class="pid">${esc(queueWaitDescription(j,summary.policy.aging_seconds??300))}</div></td><td class="mono job-resource-cell">${j.resources.cpu} CPU · ${j.resources.memory_mib} MiB<br>${j.resources.npu} NPU${j.allocation.npu_ids?.length?' / '+esc(j.allocation.npu_ids.join(',')):''} · SHM ${j.resources.shm_mib||0} MiB</td><td class="mono">${esc(j.environment)}</td><td class="mono">${clock(j.created_at)}</td><td><button class="detail-button" data-job-id="${esc(j.id)}" aria-label="查看作业 ${esc(j.name)}">↗</button></td></tr>`).join(''):'<tr><td colspan="6" class="empty-state">尚无托管作业。可从“新建作业”提交轻量 CPU 示例。</td></tr>';
  if(snapshot) renderDevices(snapshot.npu);
}

async function showJob(jobId) {
  try {
    const detail=await api('/api/jobs/'+jobId);
    const j=detail.job;
    const archiveState=j.archive_status?.state||'available';
    const archiveLabels={available:'可用',trash:'回收站',purged:'已清理',moving_to_trash:'正在移入回收站',restoring:'正在恢复',purging:'正在清理'};
    const archiveReadable=archiveState==='available';
    const [stdout,stderr]=archiveReadable?await Promise.all([api('/api/jobs/'+jobId+'/log?stream=stdout'),api('/api/jobs/'+jobId+'/log?stream=stderr')]):[{text:'归档数据当前为“'+(archiveLabels[archiveState]||archiveState)+'”。任务状态与结果摘要仍保留。'},{text:''}];
    const storageAction=archiveState==='available'?'trash':archiveState==='trash'?'restore':['moving_to_trash','restoring','purging'].includes(archiveState)?'resume':null;
    const storageLabel={trash:'归档移入回收站',restore:'恢复归档',resume:'续接存储操作'};
    const storageButton=detail.permissions?.storage&&storageAction?'<button class="copy-button" id="storage-job">'+storageLabel[storageAction]+'</button>':'';
    const canCancel=detail.permissions?.cancel!==false && ['queued','reserved','running','cancelling'].includes(j.state);
    const remote=j.environment==='worker-python';
    const fields=[['状态',jobStatus(j)],['节点',j.node_id||'尚未分配'],['执行尝试',j.attempt_id||'尚未分配'],['所有者',j.owner],['项目',j.project||'个人历史作业'],['原因',j.reason],['执行环境',j.environment],...(j.origin?[['提交入口',j.origin.client],['Agent 模型',j.origin.model],['Agent 运行编号',j.origin.agent_run_id]]:[]),['工作目录',remote?'/work（容器内）':j.cwd],['申请资源',`${j.resources.cpu} CPU / ${j.resources.memory_mib} MiB（含 SHM ${j.resources.shm_mib||0} MiB）/ ${j.resources.npu} NPU`],['分配 CPU',(j.allocation.cpu_ids||[]).join(', ')||'尚未分配'],['工作区硬限额',j.allocation.workspace?`${j.allocation.workspace.slot_id} / ${gib(j.allocation.workspace.bytes)} GiB / ${j.allocation.workspace.inodes} inode / ${j.allocation.workspace.durable?'持久文件系统':'内存文件系统'}`:'未配置或尚未分配'],['私有持久缓存',j.allocation.cache?j.allocation.cache.slot_id+' / '+j.allocation.cache.key.slice(0,16):j.parameters?.cache?'等待匹配缓存租约':'未请求'],['上游任务',(j.depends_on||[]).join(', ')||'无'],['阶段输入',j.dependency_state?JSON.stringify(j.dependency_state):'直接封存输入'],['归档目录',j.archive_dir],['归档状态',archiveLabels[archiveState]||archiveState],['提交时间',date(j.created_at)+' '+clock(j.created_at)],['开始时间',clock(j.started_at)],['结束时间',clock(j.finished_at)]];
    openDrawer(j.name,'<dl>'+fields.map(([key,value])=>'<dt>'+esc(key)+'</dt><dd>'+esc(value)+'</dd>').join('')+'</dl><div class="job-detail-actions"><button class="copy-button" id="refresh-job-log">刷新状态与日志</button>'+(canCancel?'<button class="copy-button cancel-job" id="cancel-job">取消此作业</button>':'')+storageButton+'</div><h3 class="detail-subheading">'+(remote?'模板参数':'命令参数')+'</h3><pre>'+esc(JSON.stringify(remote?{template:j.template,parameters:j.parameters}:j.argv))+'</pre><h3 class="detail-subheading">stdout'+(stdout.truncated?' · 末尾 64 KiB':'')+'</h3><pre class="job-log">'+esc(stdout.text||'尚无输出')+'</pre><h3 class="detail-subheading">stderr</h3><pre class="job-log">'+esc(stderr.text||'尚无错误输出')+'</pre><h3 class="detail-subheading">产物</h3><div class="artifact-list">'+(detail.artifacts.length?detail.artifacts.map(f=>archiveReadable?`<a href="/api/jobs/${encodeURIComponent(jobId)}/artifact?name=${encodeURIComponent(f.name)}" download>${esc(f.name)} <small>${bytes(f.bytes)}</small> ↗</a>`:`<span class="muted">${esc(f.name)} · ${esc(archiveLabels[archiveState]||archiveState)}</span>`).join(''):'<span class="muted">作业完成后生成产物索引。</span>')+'</div>'+(j.result?'<h3 class="detail-subheading">结果</h3><pre>'+esc(JSON.stringify(j.result,null,2))+'</pre>':'')+'<div class="detail-note">只取消当前调度器创建的托管作业。取消需等待执行器确认；状态无法确认时不会释放资源。'+(remote?'本任务在受控容器中执行 CPU、内存和共享内存限制。':'此环境的内存仍按申报预算，尚未使用 cgroup 硬限制。')+'</div>');
    $('refresh-job-log').addEventListener('click',()=>showJob(jobId));
    if($('storage-job')) $('storage-job').addEventListener('click',async()=>{
      $('storage-job').disabled=true;
      try{
        const preview=await api('/api/storage/plan',{kind:'archive',id:jobId,action:storageAction});
        await api('/api/storage/apply',preview.plan);
        await refresh();await showJob(jobId);
        toast(storageAction==='trash'?'归档已移入回收站，恢复前暂不提供文件下载。':storageAction==='restore'?'归档已恢复。':'已续接原存储操作。');
      }catch(error){toast(error.message);await showJob(jobId);}
    });

    if(canCancel) $('cancel-job').addEventListener('click',async()=>{
      $('cancel-job').disabled=true;
      try{await api('/api/jobs/'+jobId+'/cancel',{});await refresh();await showJob(jobId);toast('取消请求已提交，等待执行器确认。');}catch(error){toast(error.message);}
    });
  } catch(error) {toast(error.message);}
}

$('job-body').addEventListener('click',event=>{
  const button=event.target.closest('[data-job-id]');
  if(button) showJob(button.dataset.jobId);
});
function updateJobActions() {
  const allowed=Boolean(identity?.scopes.includes('jobs:submit'));
  $('submit-job').disabled=!allowed||submittingJob;
  $('preview-job').disabled=!allowed||submittingJob||$('job-preview').dataset.loading==='true';
  $('job-access-note').textContent=identity&&!allowed?'当前凭据只有查看权限，不能预览或提交新任务。':'';
  $('job-access-note').hidden=!identity||allowed;
}

function clearJobPreview() {
  previewSequence++;
  previewController?.abort();previewController=null;
  clearTimeout(previewTimer);
  $('job-preview').hidden=true;
  $('job-preview').dataset.loading='false';
  $('job-preview').dataset.expired='false';
  $('preview-candidates').replaceChildren();
  updateJobActions();
}

function jobRequest() {
  let body={name:$('job-name').value,environment:$('job-environment').value,
    kind:$('job-kind').value,cwd:$('job-cwd').value,command:$('job-command').value,
    cpu:Number($('job-cpu').value),memory_mib:Number($('job-memory').value),shm_mib:Number($('job-shm').value),npu:Number($('job-npu').value),
    timeout_seconds:Number($('job-timeout').value)};
  if($('job-template').value!=='command') body={name:$('job-name').value,environment:$('job-environment').value,cwd:$('job-cwd').value,template:$('job-template').value,parameters:$('job-template').value==='cpu_demo'?{seconds:Number($('job-seconds').value)}:{}};
  if($('job-project').value) body.project=$('job-project').value;
  return body;
}

function showJobPreview(result) {
  if(result.version!==1||result.scope!=='placement'||!Array.isArray(result.candidates)||
     result.reservation_created!==false||result.start_guaranteed!==false||
     !['eligible_snapshot','blocked','no_visible_node'].includes(result.status)) {
    throw new Error('服务返回了不支持的预览格式，请更新服务后重新检查。');
  }
  const titles={eligible_snapshot:'当前检查未发现运行阻碍',blocked:'当前候选仍有运行阻碍',no_visible_node:'当前没有映射给本人的节点'};
  $('preview-title').textContent=titles[result.status];
  $('preview-summary').textContent=`检查于 ${clock(result.observed_at)} · ${result.eligible_count} / ${result.candidate_count} 个候选当前未发现阻碍${result.truncated?' · 仅展示前 128 个候选':''}。`;
  $('preview-note').textContent='预览不入队、不预留资源，也不保证开始时间。提交和启动时会重新检查；当前无匹配节点的普通任务仍可提交等待。';
  $('preview-candidates').replaceChildren();
  for(const item of result.candidates) {
    const row=document.createElement('li');
    const heading=document.createElement('div');
    const label=document.createElement('code');label.textContent=item.node_id;
    const state=document.createElement('span');state.className='preview-state';
    state.textContent=item.status==='eligible_snapshot'?'当前未发现阻碍':'当前受阻';
    heading.append(label,state);row.append(heading);
    const reason=document.createElement('p');reason.textContent=item.reason||'实际启动仍需重新确认资源、权限及环境。';row.append(reason);
    if(typeof item.capacity_fits==='boolean') {
      const capacity=document.createElement('small');
      capacity.textContent=item.capacity_fits?'CPU / 内存 / SHM 上限可容纳申报；其他运行条件见当前检查。':'CPU / 内存 / SHM 申报超过当前上限。';
      row.append(capacity);
    }
    $('preview-candidates').append(row);
  }
  const sequence=previewSequence;
  const expire=()=>{
    if(sequence!==previewSequence)return;
    $('job-preview').dataset.expired='true';
    $('preview-title').textContent='这份预览已过时，请重新检查';
    $('preview-note').textContent='节点和资源状态可能已改变。历史检查没有预留资源，也不保证实际启动。';
  };
  const sampled=Date.parse(result.observed_at);
  const remaining=Number.isFinite(sampled)?Math.max(0,15000-Math.max(0,Date.now()-sampled)):0;
  if(remaining)previewTimer=setTimeout(expire,remaining);else expire();
}

$('job-form').addEventListener('input',clearJobPreview);
$('job-form').addEventListener('change',clearJobPreview);
$('preview-job').addEventListener('click',async()=>{
  if(!$('job-form').reportValidity())return;
  clearJobPreview();
  const sequence=previewSequence,body=jobRequest(),fingerprint=JSON.stringify(body),actor=identity?.id;
  const controller=new AbortController();previewController=controller;
  let timedOut=false;
  const deadline=setTimeout(()=>{timedOut=true;controller.abort();},15000);
  $('job-preview').hidden=false;
  $('job-preview').dataset.loading='true';
  $('preview-title').textContent='正在检查当前可见节点…';
  $('preview-summary').textContent='';$('preview-note').textContent='只读取当前条件，不创建或启动任务。';
  updateJobActions();
  try {
    const result=await api('/api/jobs/preview',body,controller.signal);
    if(sequence!==previewSequence||actor!==identity?.id||fingerprint!==JSON.stringify(jobRequest()))return;
    showJobPreview(result);
  } catch(error) {
    if(sequence!==previewSequence)return;
    $('preview-title').textContent='预览未完成';$('preview-summary').textContent=timedOut?'检查超过 15 秒，请确认连接后重试。':error.message;
    $('preview-note').textContent='没有创建任务。可以修正请求或连接后重新预览；提交仍是独立操作。';
  } finally {
    clearTimeout(deadline);
    if(previewController===controller)previewController=null;
    if(sequence===previewSequence){$('job-preview').dataset.loading='false';updateJobActions();}
  }
});

$('job-form').addEventListener('submit',async event=>{
  event.preventDefault();
  clearJobPreview();
  submittingJob=true;updateJobActions();
  $('job-form-message').textContent='';
  $('job-form-message').hidden=true;
  try {
    const body=jobRequest();
    const fingerprint=JSON.stringify(body);
    if(!pendingSubmission||pendingSubmission.fingerprint!==fingerprint) pendingSubmission={fingerprint,key:crypto.randomUUID()};
    const result=await api('/api/jobs',{...body,idempotency_key:pendingSubmission.key});
    pendingSubmission=null;
    $('job-form-message').textContent='已提交 '+result.job.id+'。资源不足时会等待；关闭页面不会取消作业。';
    $('job-form-message').hidden=false;
    await refresh();
    toast('作业已进入统一队列。');
  } catch(error){$('job-form-message').textContent=error.message;$('job-form-message').hidden=false;}
  finally{submittingJob=false;updateJobActions();}
});
$('auto-refresh').title='暂停页面刷新不会暂停或取消托管作业。';

function configureTemplate() {
  if($('job-template').value==='command' && $('job-environment').value==='worker-python') {
    if([...$('job-environment').options].some(o=>o.value==='host-python')) $('job-environment').value='host-python';
    else $('job-template').value='cpu_demo';
  }
  const template=$('job-template').value!=='command';
  const cpu=$('job-template').value==='cpu_demo';
  if($('job-template').value==='isolation_probe' && [...$('job-environment').options].some(o=>o.value==='worker-python')) $('job-environment').value='worker-python';
  $('job-seconds').closest('label').hidden=!cpu;
  $('job-seconds').disabled=!cpu;
  $('job-command').closest('label').hidden=template;
  $('job-command').disabled=template;
  $('job-kind').disabled=template;
  document.querySelector('.job-resources').hidden=template;
  for(const id of ['job-cpu','job-memory','job-shm','job-npu','job-timeout']) $(id).disabled=template;
}
$('job-template').addEventListener('change',configureTemplate);
$('job-environment').addEventListener('change',()=>{
  if($('job-environment').value==='worker-python' && $('job-template').value==='command') $('job-template').value='cpu_demo';
  configureTemplate();
});

function renderActivity(summary) {
  $('activity-body').innerHTML=summary.jobs.length?summary.jobs.map(j=>`<tr><td>${esc(j.owner)}</td><td>${esc(j.kind)}</td><td>${esc(jobStatus(j))}</td><td>${j.resources.cpu} CPU · ${j.resources.memory_mib} MiB · ${j.resources.npu} NPU</td></tr>`).join(''):'<tr><td colspan="4" class="empty-state">当前没有等待或运行的托管任务。</td></tr>';
}

function renderNodes(summary) {
  const known=summary.nodes.every(n=>n.physical_id);
  $('node-summary').textContent=(known?new Set(summary.nodes.map(n=>n.physical_id)).size+' 个物理资源域':'物理资源域待核实')+' · '+summary.nodes.length+' 个执行入口 · 同域额度不可相加';
  $('node-list').innerHTML=summary.nodes.map(node=>{
    const domain=node.resource_domain;
    const domainInfo=domain?`<div>整机统一预算：${domain.policy.cpu_ids.length} CPU / ${domain.policy.memory_mib} MiB / ${domain.policy.max_running} 并发 · 已预留 ${domain.reserved?.slots??0} 个 · ${domain.admission?.blocked?esc(domain.admission.reason):(domain.draining?'正在排空，暂停新任务':'接收任务')}（同域不叠加）</div>`:'';
    const stale=node.device_status!=='ok'||!node.device_sampled_at||Date.now()-new Date(node.device_sampled_at)>45000;
    const workspaceInfo=node.workspace?.enforced?'<div>工作区硬限额：'+node.workspace.slots.map(slot=>esc(slot.slot_id)+' '+gib(slot.bytes)+' GiB / '+esc(slot.inodes)+' inode · '+(slot.durable?'持久文件系统':'内存文件系统')+' · '+(slot.status==='ok'?(slot.clean?'空闲':'占用或待清理'):'不可用')).join('；')+'</div>':'<div>工作区尚未启用文件系统硬限额。</div>';
    const inventoryInfo=node.attempt_inventory?.reconciled!==undefined&&node.attempt_inventory?.reconciled!==null?'<div>执行对账：'+(node.attempt_inventory.reconciled?'一致':'需要核对')+' · 待确认 '+esc(node.attempt_inventory.pending)+' / 不匹配 '+esc(node.attempt_inventory.unmatched)+'</div>':'';
    const environmentInfo=node.environment?'<div>环境：'+(node.environment.certified?(node.environment.sha256?'已认证':'已认证，等待节点确认'):'等待认证')+(node.environment.python?' · Python '+esc(node.environment.python)+' / '+esc(node.environment.machine):'')+'</div><div>镜像：'+esc((node.environment.image_sha256||'待登记').slice(0,23))+'</div>':'';
    const finiteSpool=node.spool_storage;
    const spoolInfo=node.enforcement==='container-cgroups'?'<div>节点暂存准入：'+(node.spool_admission?'字节 / inode · '+(node.observed?.artifact_gc_ready?'已确认副本自动回收':'副本回收待确认，暂停新任务'):'节点需升级')+'</div>':'';
    const finiteInfo=node.enforcement==='container-cgroups'?(finiteSpool?.enforced?'<div>有限暂存盘：'+bytes(finiteSpool.bytes)+' / '+num(finiteSpool.inodes,0)+' inode · '+(finiteSpool.status==='ok'?'可用 '+bytes(finiteSpool.free_bytes):'不可用，暂停派发')+'</div>':'<div>暂存尚未启用独立文件系统硬限额。</div>'):'';
    const cacheInfo=node.enforcement==='container-cgroups'?(node.persistent_cache?.enforced?'<div>私有持久缓存：'+node.persistent_cache.slots.map(s=>esc(s.slot_id)+' '+bytes(s.bytes)+' / '+num(s.inodes,0)+' inode · '+(s.status==='ok'?(s.leased?'租约占用':'空闲')+' · 可用 '+bytes(s.free_bytes)+' · 剩余 '+num(s.free_inodes,0)+' inode · '+num(s.namespaces,0)+' 个环境':'不可用，缓存任务等待')).join('；')+'</div>':'<div>持久缓存未启用，任务使用独立工作区缓存。</div>'):'';
    const a=node.device_access?.counts||{};
    const accessInfo=node.npu_configured?`<div>设备文件访问资格：已验证 ${num(a.verified||0,0)} · 未验证 ${num((a.unverified||0)+(a.needs_upgrade||0)+(a.unavailable||0),0)} · 已失效 ${num((a.stale||0)+(a.expired||0),0)} · 失败阻断 ${num(a.blocked||0,0)}（按执行身份与卡计数；ACL与算子另行验收）</div>`:'';
    return `<article class="device-card"><div class="device-top"><div class="device-name">${esc(node.name||node.id)}</div><span class="status-tag ${node.status==='online'?'':'warn'}">${node.status==='online'?'连接正常':'状态未知'}</span></div><div class="node-identity">${esc(node.id)} · 资源域 ${esc((node.physical_id||'待核实').slice(0,10))}</div><div class="device-details"><span>CPU 额度<strong>${node.capacity.cpu_ids.length} 核</strong></span><span>内存额度<strong>${node.capacity.memory_mib} MiB</strong></span><span>并发上限<strong>${node.capacity.max_running}</strong></span></div><div class="node-facts">${domainInfo}${workspaceInfo}${environmentInfo}${inventoryInfo}${spoolInfo}${finiteInfo}${cacheInfo}${accessInfo}${node.enforcement==='container-cgroups'?'<div>本地 Docker 身份检查：'+(node.runtime_pinned?'已启用':'节点需升级')+'</div>':''}${node.io_limits?'<div>产物额度：单文件 '+esc(node.io_limits.file_mib)+' MiB / 每任务 '+esc(node.io_limits.artifact_mib)+' MiB</div>':''}${node.npu_configured?'<div>设备文件接入；运行时映射和算子仍须单独验收。</div>':''}<div>主机 CPU ${num(node.observed?.host_cpu_utilization_pct)}% · 内存可用 ${gib(node.observed?.memory_available_bytes)} GiB · 数据盘可用 ${gib(node.observed?.disk_free_bytes)} GiB</div>${node.devices.map(d=>`<div>NPU ${esc(d.id)} · ${stale?'读数过期':esc(d.health)} · 利用率 ${num(d.utilization_pct,0)}% · 设备进程 ${(d.external_process_count||0)+(d.scoped_process_count||0)}</div>`).join('')||(node.npu_configured?'<div>设备查询不可用，暂停分配 NPU 任务。</div>':'<div>此执行节点尚未启用 NPU 任务。</div>')}</div><div class="device-bottom"><span>${node.enforcement==='container-cgroups'?'容器资源硬限制':'协作式资源预留'}</span><span>更新 ${clock(node.sampled_at)}</span></div></article>`;
  }).join('');
}

function workflowResultPath() {
  const kind=$('workflow-kind').value;
  return '/api/results?limit=50&offset='+resultOffset+(kind?'&kind='+encodeURIComponent(kind):'');
}
function renderWorkflowResults(data) {
  const labels={accuracy:'精度',performance:'性能',simulation:'仿真采集',build:'编译构建',ir:'IR 处理'};
  $('workflow-result-count').textContent=data.total;
  $('workflow-csv').href='/api/results.csv'+($('workflow-kind').value?'?kind='+encodeURIComponent($('workflow-kind').value):'');
  $('workflow-result-body').innerHTML=data.rows.length?data.rows.map(row=>{
    const m=row.metrics||{};
    const metric=row.kind==='performance'?`${m.samples??'—'} 次 · 均值 ${num(m.mean_us,3)} µs`:row.kind==='accuracy'?`${m.checks??'—'} 项 · 最大绝对误差 ${m.max_abs_error??'—'}`:m.artifacts!==undefined?`${m.artifacts} 个产物 · ${valid(m.artifact_bytes)&&m.artifact_bytes<1024?m.artifact_bytes+' B':bytes(m.artifact_bytes)}`:row.error||'执行未完成';
    return `<tr><td><strong>${esc(row.name)}</strong><div class="mono muted small">${esc(row.job_id)}</div></td><td>${esc(labels[row.kind]||row.kind)}</td><td><span class="job-state ${row.passed?'succeeded':'failed'}">${row.failure_stage==='dependency'&&row.command_executed===false?'上游失败，未启动':row.passed?'核对通过':'失败'}</span></td><td>${esc(metric)}</td><td>${row.environment_locked===true?'已匹配':row.environment_locked===false?'未匹配':'未记录'}</td><td>${date(row.recorded_at)} ${clock(row.recorded_at)}</td><td><button class="detail-button" data-result-job="${esc(row.job_id)}" aria-label="查看结果 ${esc(row.name)}">↗</button></td></tr>`;
  }).join(''):'<tr><td colspan="7" class="empty-state">此范围暂无工作流结果。</td></tr>';
  $('workflow-page').textContent=`${data.offset+1>data.total?0:data.offset+1}–${Math.min(data.offset+data.limit,data.total)} / ${data.total}`;
  $('workflow-prev').disabled=resultOffset===0;
  $('workflow-next').disabled=resultOffset+data.limit>=data.total;
}
$('workflow-kind').addEventListener('change',async()=>{resultOffset=0;try{renderWorkflowResults(await api(workflowResultPath()));}catch(error){toast(error.message);}});
$('workflow-prev').addEventListener('click',async()=>{resultOffset=Math.max(0,resultOffset-50);try{renderWorkflowResults(await api(workflowResultPath()));}catch(error){toast(error.message);}});
$('workflow-next').addEventListener('click',async()=>{resultOffset+=50;try{renderWorkflowResults(await api(workflowResultPath()));}catch(error){toast(error.message);}});
$('workflow-result-body').addEventListener('click',event=>{const button=event.target.closest('[data-result-job]');if(button)showJob(button.dataset.resultJob);});
