"""Local authenticated dashboard. Python standard library only."""
from __future__ import annotations

import argparse
import base64
import datetime as dt
from http.server import BaseHTTPRequestHandler
from listener import HTTPServer as ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from collector import Collector, ROOT, cancel_commands, experiments, now
from jobs import ARTIFACT_NAMES, JobError, JobStore, Scheduler, public, read_json
from auth import AccessControl, AccessError

RUNTIME = ROOT / 'runtime'
STOP = threading.Event()


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY, ts TEXT, cpu REAL, memory REAL,
                    user_cpu REAL, container_memory INTEGER, npu TEXT);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, message TEXT);
            ''')

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def save(self, snapshot, previous):
        host, container = snapshot['host'], snapshot['container']
        mem = container.get('cgroup_memory') or {}
        current = {(p['pid'], p['start_ticks']): p for p in snapshot['processes']}
        events = []
        if previous is not None:
            for key in current.keys() - previous.keys():
                p = current[key]
                events.append(('start', f"观察到新进程 · {p['name']} · PID {p['pid']}"))
            for key in previous.keys() - current.keys():
                p = previous[key]
                events.append(('exit', f"进程离开采样范围 · {p['name']} · PID {p['pid']}（退出结果未知）"))
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', ('latest', json.dumps(snapshot, ensure_ascii=False)))
            db.execute('INSERT INTO history(ts,cpu,memory,user_cpu,container_memory,npu) VALUES (?,?,?,?,?,?)',
                       (snapshot['sampled_at'], host['cpu_utilization_pct'],
                        host['memory_used_bytes'] / host['memory_total_bytes'] * 100,
                        snapshot['user_summary']['cpu_cores'], mem.get('current_bytes'),
                        json.dumps([{'id': x['id'], 'utilization_pct': x['utilization_pct']} for x in snapshot['npu']['devices']])))
            db.executemany('INSERT INTO events(ts,kind,message) VALUES (?,?,?)',
                           [(snapshot['sampled_at'], kind, message) for kind, message in events[:20]])
            if previous is None:
                db.execute('INSERT INTO events(ts,kind,message) VALUES (?,?,?)',
                           (snapshot['sampled_at'], 'monitor', '监控采样已启动 · 仅观察当前用户与指定容器'))
            db.execute('DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 2880)')
            db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 300)')
        return current

    def latest(self):
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key='latest'").fetchone()
        return json.loads(row[0]) if row else None

    def history(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT ts,cpu,memory,user_cpu,container_memory,npu FROM history ORDER BY id DESC LIMIT 120').fetchall()
        result = [dict(x) for x in reversed(rows)]
        for row in result:
            row['npu'] = json.loads(row['npu'])
        return result

    def events(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(x) for x in db.execute('SELECT ts,kind,message FROM events ORDER BY id DESC LIMIT 25')]


def sample_loop(collector, store, interval):
    previous = None
    while not STOP.is_set():
        start = time.monotonic()
        try:
            previous = store.save(collector.snapshot(), previous)
        except Exception:
            logging.exception('Snapshot collection failed')
        STOP.wait(max(1, interval - (time.monotonic() - start)))


def npu_loop(collector):
    while not STOP.is_set():
        collector.collect_npu()
        STOP.wait(30)


def handler_class(store, config, jobs=None):
    access = jobs.access if jobs is not None else AccessControl(None, config)
    class Handler(BaseHTTPRequestHandler):
        server_version = 'AscendLabDemo/0.10'

        def log_message(self, fmt, *args):
            # Do not log Authorization, URLs, payloads, or query strings.
            pass

        def reply(self, body, content_type='application/json; charset=utf-8', status=200, filename=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header('Content-Disposition', "attachment; filename=\"download\"; filename*=UTF-8''" + quote(filename, safe=''))
            if status == 401:
                self.send_header('WWW-Authenticate', 'Basic realm="Ascend Lab private demo", charset="UTF-8"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def authenticated(self):
            self.principal = access.authenticate(self.headers.get('Authorization', ''))
            return self.principal is not None

        def do_GET(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            if path == '/healthz':
                return self.reply({'status': 'ok', 'service': 'ascend-lab-demo'})
            if not self.authenticated():
                return self.reply({'error': 'authentication_required'}, status=401)
            static = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                      '/style.css': ('style.css', 'text/css; charset=utf-8'),
                      '/favicon.svg': ('favicon.svg', 'image/svg+xml')}
            if path in static:
                name, mime = static[path]
                return self.reply((ROOT / 'web' / name).read_bytes(), mime)
            if path == '/api/me':
                value=access.public_profile(self.principal)
                if jobs is not None:value.update(io_limits=jobs.policy['io_limits'],transfer_chunk_bytes=256*1024,
                                               storage=jobs.storage.summary(self.principal))
                return self.reply(value)
            if path in {'/api/snapshot','/api/history','/api/events','/api/experiments'} and 'monitor:read' not in self.principal.scopes:
                return self.reply({'error':'此身份没有读取个人进程和历史实验的权限。'}, status=403)
            if path == '/api/snapshot':
                return self.reply({'snapshot': store.latest(), 'interval_seconds': config['interval']})
            if path == '/api/history':
                return self.reply(store.history())
            if path == '/api/events':
                return self.reply(store.events())
            if path == '/api/experiments':
                return self.reply(experiments())
            if jobs is not None:
                try:
                    self.principal.require('jobs:read')
                    if path == '/api/activity':
                        return self.reply(jobs.activity())
                    if path == '/api/nodes':
                        return self.reply({'nodes':([jobs.backend.inventory(store.latest(),jobs.policy)] if jobs.policy['local_enabled'] else [])+jobs.nodes.public_nodes()})
                    if path == '/api/audit':
                        return self.reply({'events':access.audit(self.principal)})
                    if path == '/api/jobs':
                        return self.reply(jobs.summary(self.principal))
                    if path in {'/api/results','/api/results.csv'}:
                        query=parse_qs(parsed.query);kind=query.get('kind',[None])[0]
                        if path.endswith('.csv'):
                            return self.reply(jobs.results.csv(self.principal,kind),'text/csv; charset=utf-8',filename='ascend-lab-results.csv')
                        try:limit=int(query.get('limit',['100'])[0]);offset=int(query.get('offset',['0'])[0])
                        except ValueError:raise JobError('Invalid result page')
                        return self.reply(jobs.results.rows(self.principal,kind,limit,offset))
                    if path=='/api/storage':
                        query=parse_qs(parsed.query)
                        try:limit=int(query.get('limit',['100'])[0]);offset=int(query.get('offset',['0'])[0])
                        except ValueError:raise JobError('Invalid storage page')
                        return self.reply(jobs.lifecycle.listing(self.principal,limit,offset,query.get('state',[None])[0]))
                    if path.startswith('/api/inputs/'):
                        return self.reply(jobs.inputs.get(path.removeprefix('/api/inputs/'),self.principal))
                    parts = path.strip('/').split('/')
                    if len(parts) >= 3 and parts[:2] == ['api', 'jobs']:
                        job_id = parts[2]
                        directory = jobs.directory(job_id)
                        job = jobs.get(job_id, principal=self.principal)
                        if len(parts) == 3:
                            index=read_json(directory/'artifacts.json') or jobs.lifecycle.record('archive',job_id).get('manifest') or {}
                            return self.reply({'job': public(job), 'attempts':jobs.attempts(job_id,self.principal), 'permissions':{'cancel':'jobs:cancel' in self.principal.scopes and access.can_cancel(self.principal,job),
                              'storage':'storage:manage' in self.principal.scopes and job['owner']==self.principal.id and job['state'] in {'succeeded','failed','cancelled','timed_out'}}, 'artifacts': index.get('files', []),
                              'retention_metadata':jobs.lifecycle.record('archive',job_id).get('evidence') if job.get('archive_status',{}).get('state','available')!='available' else None})
                        if parts[3:] == ['log']:
                            return self.reply(jobs.log(job_id, parse_qs(parsed.query).get('stream', ['stdout'])[0], self.principal))
                        if parts[3:] == ['archive-manifest']:
                            from archive_api import manifest
                            with jobs.lifecycle.reader('archive',job_id):value=manifest(directory, job)
                            access.record(self.principal, 'archive.manifest', job_id)
                            return self.reply(value)
                        if parts[3:] == ['artifact-chunk']:
                            from archive_api import chunk
                            with jobs.lifecycle.reader('archive',job_id):value=chunk(directory, job, parse_qs(parsed.query))
                            return self.reply(value)
                        if parts[3:] == ['artifact']:
                            name = parse_qs(parsed.query).get('name', [''])[0]
                            jobs.lifecycle.available('archive',job_id)
                            index=read_json(directory/'artifacts.json') or jobs.lifecycle.record('archive',job_id).get('manifest') or {}
                            indexed = {f['name'] for f in index.get('files', [])}
                            if name not in indexed:
                                raise JobError('归档文件不存在或未登记。', 404)
                            from archive_api import open_artifact
                            from io_policy import for_job,MIB,CHUNK
                            access.record(self.principal,'artifact.read',job_id)
                            with jobs.lifecycle.reader('archive',job_id),open_artifact(directory,name,for_job(job)['file_mib']*MIB) as source:
                                self.connection.settimeout(8)
                                size=os.fstat(source.fileno()).st_size;start=0;end=size-1;partial=False
                                requested=self.headers.get('Range')
                                if requested:
                                    import re
                                    match=re.fullmatch(r'bytes=(\d*)-(\d*)',requested)
                                    if not match or not any(match.groups()):raise JobError('Invalid artifact range',416)
                                    left,right=match.groups()
                                    if not left:
                                        count=int(right)
                                        if count<1:raise JobError('Invalid artifact range',416)
                                        start=max(0,size-count)
                                    else:
                                        start=int(left);end=min(end,int(right)) if right else end
                                    if start>=size or start>end:raise JobError('Artifact range exceeds file',416)
                                    partial=True
                                self.send_response(206 if partial else 200)
                                self.send_header('Content-Type','application/octet-stream')
                                self.send_header('Content-Length',str(max(0,end-start+1)))
                                self.send_header('Accept-Ranges','bytes')
                                if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                                self.send_header('Content-Disposition',"attachment; filename=artifact; filename*=UTF-8''"+quote(Path(name).name,safe=''))
                                self.send_header('Cache-Control','no-store');self.end_headers()
                                source.seek(start);remaining=max(0,end-start+1)
                                try:
                                    while remaining:
                                        block=source.read(min(CHUNK,remaining))
                                        if not block:break
                                        self.wfile.write(block);remaining-=len(block)
                                except (BrokenPipeError,ConnectionResetError):pass
                            return
                except (JobError, AccessError) as exc:
                    if path.startswith('/api/jobs/job-'):
                        target=path.split('/')[3]
                        if len(target)!=16 or any(c not in '0123456789abcdef' for c in target[4:]):target='invalid-id'
                        access.record(self.principal,'job.read',target, 'denied')
                    return self.reply({'error': str(exc)}, status=exc.status)
            if path == '/api/export':
                if 'monitor:read' not in self.principal.scopes:
                    return self.reply({'exported_at':now(),'scheduler':jobs.summary(self.principal) if jobs else None}, filename='ascend-lab-jobs.json')
                return self.reply({'exported_at': now(), 'snapshot': store.latest(),
                                   'history': store.history(), 'events': store.events(),
                                   'experiments': experiments(),
                                   'scheduler': jobs.summary(self.principal) if jobs is not None else None},
                                  filename='ascend-lab-observation.json')
            return self.reply({'error': 'not_found'}, status=404)

        def do_POST(self):
            if urlsplit(self.path).path.startswith('/api/worker/'):
                return self.worker_request()
            if not self.authenticated():
                return self.reply({'error': 'authentication_required'}, status=401)
            if jobs is None:
                return self.reply({'error': 'read_only_demo'}, status=405)
            try:
                origin = self.headers.get('Origin')
                origin_scheme='https' if config.get('tls') else urlsplit(config.get('controller','http://127.0.0.1')).scheme
                if origin and (urlsplit(origin).scheme != origin_scheme or urlsplit(origin).netloc != self.headers.get('Host')
                               or urlsplit(origin).path not in {'','/'} or urlsplit(origin).query or urlsplit(origin).fragment):
                    raise JobError('拒绝跨站写入请求。', 403)
                if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json' or self.headers.get('Transfer-Encoding'):
                    raise JobError('写入接口仅接受 application/json。', 415)
                path = urlsplit(self.path).path
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 < length <= (512*1024 if path.startswith('/api/inputs') else 32768):
                    raise JobError('请求内容长度无效或超过接口上限。', 413)
                self.connection.settimeout(5)
                request = json.loads(self.rfile.read(length))
                if path=='/api/storage/plan':return self.reply(jobs.lifecycle.plan(self.principal,request))
                if path=='/api/storage/apply':
                    value=jobs.lifecycle.apply(self.principal,request)
                    access.record(self.principal,'storage.apply',request.get('id',''))
                    return self.reply(value)
                if path=='/api/inputs':
                    bundle,reused=jobs.inputs.create(self.principal,request)
                    access.record(self.principal,'input.create',bundle['id'],'reused' if reused else 'created')
                    return self.reply({'input':bundle,'reused':reused},status=200 if reused else 201)
                if path.startswith('/api/inputs/'):
                    parts=path.strip('/').split('/')
                    if len(parts)!=4:raise JobError('Invalid input operation',404)
                    bundle_id=parts[2]
                    if parts[3]=='blob':return self.reply(jobs.inputs.upload(bundle_id,self.principal,request))
                    if parts[3]=='reset-file':return self.reply(jobs.inputs.reset_file(bundle_id,self.principal,request))
                    if parts[3]=='seal':
                        if request!={}:raise JobError('Seal does not accept fields')
                        bundle=jobs.inputs.seal(bundle_id,self.principal)
                        access.record(self.principal,'input.seal',bundle_id)
                        return self.reply({'input':bundle})
                if path == '/api/jobs':
                    job, reused = jobs.submit(request, self.principal)
                    access.record(self.principal,'job.submit',job['id'],'reused' if reused else 'created')
                    return self.reply({'job': job, 'reused': reused}, status=200 if reused else 201)
                if path == '/api/jobs/validate':
                    self.principal.require('jobs:submit')
                    normalized=jobs.normalize(request,self.principal)
                    return self.reply({'request':public(normalized),'input_available':not normalized.get('input') or jobs.lifecycle.record('input',normalized['input']['id'])['state']=='available'})
                parts = path.strip('/').split('/')
                if len(parts) == 4 and parts[:2] == ['api', 'jobs'] and parts[3] == 'cancel':
                    jobs.directory(parts[2])
                    job=jobs.cancel(parts[2], self.principal)
                    access.record(self.principal,'job.cancel',job['id'])
                    return self.reply({'job':job})
                if len(parts)==4 and parts[:2]==['api','jobs'] and parts[3]=='retry-inputs':
                    if request!={}:raise JobError('Dependency retry does not accept source or command overrides')
                    result=jobs.dependencies.retry(parts[2],self.principal)
                    access.record(self.principal,'job.retry-inputs',parts[2]);return self.reply(result)
                raise JobError('不存在此作业操作。', 404)
            except (JobError, AccessError) as exc:
                access.record(self.principal,'request.write','job.submit' if self.path=='/api/jobs' else 'job.cancel-or-unsupported','denied')
                return self.reply({'error': str(exc)}, status=exc.status)
            except (ValueError, TypeError, TimeoutError):
                return self.reply({'error': '请求不是有效的 JSON 或读取超时。'}, status=400)
            except (OSError, sqlite3.Error) as exc:
                from storage_io import exhausted,response
                if not exhausted(exc):raise
                # The audit database may share the exhausted device. Do not
                # turn a storage response into another write or log raw paths.
                return self.reply(response(),status=507)

        def worker_request(self):
            if jobs is None:return self.reply({'error':'Node protocol unavailable'},status=404)
            node_id=jobs.nodes.authenticate(self.headers.get('Authorization',''))
            if not node_id:return self.reply({'error':'Node authentication required'},status=401)
            try:
                if self.headers.get('Origin') or self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    raise JobError('Node requests require direct JSON transport',415)
                length=int(self.headers.get('Content-Length','-1'))
                if not 0<length<=1024*1024:raise JobError('Node request too large',413)
                self.connection.settimeout(8)
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict):raise JobError('Node body must be an object')
                action=urlsplit(self.path).path.removeprefix('/api/worker/')
                methods={'heartbeat':jobs.nodes.heartbeat,'start':jobs.nodes.authorize_start,'update':jobs.nodes.update,
                         'blob':jobs.nodes.upload,'complete':jobs.nodes.complete,'input':jobs.nodes.input_chunk,
                         'owner-commit':jobs.nodes.handoff.commit,'owner-ready':jobs.nodes.handoff.ready}
                if action=='claim':result=jobs.nodes.claim(node_id,body)
                elif action in methods:result=methods[action](node_id,body)
                else:raise JobError('Unknown node operation',404)
                return self.reply(result)
            except (JobError,AccessError) as error:return self.reply({'error':str(error)},status=error.status)
            except (ValueError,TypeError,KeyError,TimeoutError):return self.reply({'error':'Invalid node protocol message'},status=400)
            except (OSError,sqlite3.Error) as error:
                from storage_io import exhausted,response
                if not exhausted(error):raise
                return self.reply(response(),status=507)

        def no_write(self):
            return self.reply({'error': 'method_not_allowed'}, status=405)

        do_DELETE = no_write
        do_PUT = no_write
        do_PATCH = no_write
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--state-dir',default=str(RUNTIME))
    args = parser.parse_args()
    if os.getuid()==0:
        parser.error('Run the controller as a dedicated unprivileged service user; root is reserved for a trusted node role')
    runtime=Path(args.state_dir).resolve()
    runtime.mkdir(mode=0o700,parents=True,exist_ok=True)
    config = json.loads(Path(args.config or runtime/'config.json').read_text())
    from listener import runtime as listener_runtime,TLSHTTPServer,ListenerError
    try:listener_info,tls_context=listener_runtime(config)
    except ListenerError as error:parser.error(str(error))
    os.umask(0o077)
    os.nice(10)
    log = RotatingFileHandler(runtime / 'server.log', maxBytes=1024 * 1024, backupCount=2)
    logging.basicConfig(handlers=[log], level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    store = Store(runtime / 'observations.sqlite3')
    # Never expose a prior server's snapshot as a fresh sample after restart.
    with store.connect() as db:
        db.execute("DELETE FROM state WHERE key='latest'")
    collector = Collector(config['username'], config['container']) if config.get('monitor_local',True) else None
    jobs = JobStore(runtime, config)
    import uuid
    jobs.controller_instance=uuid.uuid4().hex
    scheduler = Scheduler(jobs, store, STOP)
    scheduler.acquire()
    handler=handler_class(store,config,jobs)
    server = TLSHTTPServer((config['host'],config['port']),handler,tls_context) if tls_context else ThreadingHTTPServer((config['host'],config['port']),handler)
    server.daemon_threads = True
    if collector:
        for target, argv in ((sample_loop, (collector, store, config['interval'])), (npu_loop, (collector,))):
            threading.Thread(target=target, args=argv, daemon=True).start()
    scheduler_thread = threading.Thread(target=scheduler.run, daemon=True)
    scheduler_thread.start()
    (runtime / 'server.pid').write_text(str(os.getpid()))
    from jobs import atomic_json,identity
    from owner_handoff import CAPABILITY as OWNER_HANDOFF
    from owner_node_grant import CAPABILITY as OWNER_EXPANSION
    from owner_provision import CAPABILITY as OWNER_PROVISION
    from owner_maintenance_drain import CAPABILITY as OWNER_DRAIN
    from owner_device_probe import CAPABILITY as STAGED_DEVICE_PROBE
    from owner_probe_adoption import CAPABILITY as PROBE_ORCHESTRATION
    from owner_activation import CAPABILITY as OWNER_ACTIVATION
    from owner_activation_abort import CAPABILITY as OWNER_ABORT
    from unit_start import START_ENV,CAPABILITY as STARTUP
    atomic_json(runtime/'server-ready.json',{**identity(os.getpid()),'controller_instance':jobs.controller_instance,
        'service_start_id':os.environ.get(START_ENV),'capabilities':[OWNER_HANDOFF,OWNER_ACTIVATION,OWNER_ABORT,OWNER_EXPANSION,OWNER_PROVISION,OWNER_DRAIN,STAGED_DEVICE_PROBE,PROBE_ORCHESTRATION,STARTUP]})
    logging.info('Started controller listener using %s on port %s',listener_info['scheme'],config['port'])

    def stop(signum, frame):
        STOP.set()
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        STOP.set()
        cancel_commands()
        scheduler_thread.join(timeout=3)
        server.server_close()
        if (runtime / 'server.pid').read_text().strip() == str(os.getpid()):
            (runtime / 'server.pid').unlink(missing_ok=True)
        logging.info('Stopped')


if __name__ == '__main__':
    main()
