"""Request identities and project ACLs. Logical personas are not OS sandboxes."""
from __future__ import annotations

import base64
import datetime as dt
from dataclasses import dataclass, replace
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from state_lock import state_lock

SCOPES = frozenset({'jobs:read', 'jobs:submit', 'jobs:cancel', 'monitor:read','storage:manage'})
IDENTIFIER = re.compile(r'^[a-zA-Z][a-zA-Z0-9_-]{0,47}$')


class AccessError(ValueError):
    def __init__(self, message='Permission denied', status=403):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Principal:
    id: str
    role: str
    workspace: str
    environments: tuple
    mode: str
    quota: dict
    scopes: frozenset = SCOPES
    legacy: bool = False

    @property
    def admin(self):
        return self.role == 'admin'

    def require(self, scope):
        if scope not in self.scopes:
            raise AccessError('此凭据不允许该操作。')


class AccessControl:
    def __init__(self, runtime, config):
        self.config = config
        self.path = Path(runtime)/'identities.sqlite3' if runtime else None
        self.state_lock=state_lock(runtime) if runtime else None
        self.local = Principal(config['username'], 'admin',
                               str(Path(config.get('scheduler', {}).get('workspace_root', Path.home())).resolve()),
                               ('*',), 'command', {},
                               SCOPES if config.get('monitor_local',True) else SCOPES-{'monitor:read'},legacy=True)
        if self.path:
            with self.connect() as db:
                db.executescript('''
                  CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
                    salt TEXT NOT NULL, profile TEXT NOT NULL, disabled INTEGER NOT NULL DEFAULT 0);
                  CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, label TEXT NOT NULL);
                  CREATE TABLE IF NOT EXISTS members (project TEXT NOT NULL, user TEXT NOT NULL, role TEXT NOT NULL,
                    PRIMARY KEY(project,user));
                  CREATE TABLE IF NOT EXISTS tokens (digest TEXT PRIMARY KEY, user TEXT NOT NULL,
                    scopes TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0, expires_at REAL NOT NULL);
                  CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at TEXT NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, outcome TEXT NOT NULL);
                ''')
                if 'expires_at' not in {r[1] for r in db.execute('PRAGMA table_info(tokens)')}:
                    db.execute('ALTER TABLE tokens ADD COLUMN expires_at REAL NOT NULL DEFAULT 0')
            self.path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def close(self):
        lock=getattr(self,'state_lock',None)
        if lock:lock.close()

    def __del__(self):
        self.close()

    def user(self, user_id):
        if user_id == self.local.id:
            return self.local
        if not self.path:
            return None
        with self.connect() as db:
            row = db.execute('SELECT profile,disabled FROM users WHERE id=?', (user_id,)).fetchone()
        if not row or row[1]:
            return None
        profile = json.loads(row[0])
        return Principal(user_id, profile['role'], profile['workspace'], tuple(profile['environments']),
                         profile['mode'], profile['quota'], SCOPES-{'monitor:read'})

    def authenticate(self, header):
        if not isinstance(header, str) or len(header)>4096:
            return None
        if header.startswith('Basic '):
            try:
                user, password = base64.b64decode(header[6:], validate=True).decode().split(':', 1)
            except (ValueError, UnicodeError):
                return None
            principal = self.user(user)
            if principal is None:
                return None
            if principal.legacy:
                return principal if hmac.compare_digest(password.encode(), self.config.get('password', '').encode()) and password else None
            with self.connect() as db:
                row = db.execute('SELECT password_hash,salt FROM users WHERE id=? AND disabled=0', (user,)).fetchone()
            digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(row[1]), 300000).hex()
            return principal if hmac.compare_digest(digest, row[0]) else None
        if header.startswith('Bearer ') and self.path:
            digest = hashlib.sha256(header[7:].encode()).hexdigest()
            with self.connect() as db:
                row = db.execute('SELECT user,scopes FROM tokens WHERE digest=? AND revoked=0 AND expires_at>?', (digest,time.time())).fetchone()
            if row:
                principal = self.user(row[0])
                if principal:
                    return replace(principal, scopes=principal.scopes & frozenset(json.loads(row[1])))
        return None

    def create_user(self, user_id, workspace, environments=('host-python',), quota=None):
        if not IDENTIFIER.fullmatch(user_id) or user_id == self.local.id:
            raise AccessError('无效或保留的用户 ID。', 400)
        workspace = Path(workspace).resolve()
        if not workspace.is_dir() or not workspace.is_relative_to(Path(self.local.workspace)):
            raise AccessError('逻辑测试用户目录必须在当前已授权工作区内。', 400)
        limits = {'max_running':1,'max_queued':10,'cpu':1,'memory_mib':512,'shm_mib':0,'npu':0,
                  'input_storage_mib':512,'archive_storage_mib':2048}
        if quota:
            for key, value in quota.items():
                if key not in limits or isinstance(value, bool) or not isinstance(value, int) or value<0:
                    raise AccessError('无效配额。', 400)
            limits.update(quota)
        password = secrets.token_urlsafe(24)
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 300000).hex()
        profile = {'role':'user','workspace':str(workspace),'environments':list(environments),
                   'mode':'templates','quota':limits,'simulation_uid':os.getuid()}
        try:
            with self.connect() as db:
                db.execute('INSERT INTO users(id,password_hash,salt,profile) VALUES(?,?,?,?)',
                           (user_id,digest,salt.hex(),json.dumps(profile)))
        except sqlite3.IntegrityError:
            raise AccessError('用户 ID 已存在。', 409)
        self.path.chmod(0o600)
        return password

    def issue_token(self, user_id, scopes, lifetime_seconds=86400):
        principal = self.user(user_id)
        if not principal or not scopes or not set(scopes)<=principal.scopes or not 1<=lifetime_seconds<=30*86400:
            raise AccessError('无效用户或凭据权限。', 400)
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('INSERT INTO tokens(digest,user,scopes,expires_at) VALUES(?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(),user_id,json.dumps(sorted(scopes)),time.time()+lifetime_seconds))
        return token

    def revoke_token(self, token):
        with self.connect() as db:
            db.execute('UPDATE tokens SET revoked=1 WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),))

    def add_member(self, project, user_id, role='editor'):
        if not IDENTIFIER.fullmatch(project) or not self.user(user_id) or role not in {'viewer','editor','manager'}:
            raise AccessError('无效项目、用户或项目角色。', 400)
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO projects VALUES(?,?)', (project,project))
            db.execute('INSERT OR REPLACE INTO members VALUES(?,?,?)', (project,user_id,role))

    def remove_member(self, project, user_id):
        with self.connect() as db:
            db.execute('DELETE FROM members WHERE project=? AND user=?', (project,user_id))

    def disable_user(self, user_id, disabled=True):
        if user_id==self.local.id:
            raise AccessError('不能通过逻辑账号工具禁用本机服务账号。',400)
        with self.connect() as db:
            db.execute('UPDATE users SET disabled=? WHERE id=?', (int(disabled),user_id))
            if disabled:db.execute('UPDATE tokens SET revoked=1 WHERE user=?',(user_id,))

    def set_quota(self, user_id, changes):
        allowed={'max_running','max_queued','cpu','memory_mib','shm_mib','npu','input_storage_mib','archive_storage_mib'}
        if not changes or set(changes)-allowed or any(isinstance(v,bool) or not isinstance(v,int) or not 0<=v<=10**6 for v in changes.values()):
            raise AccessError('无效配额。',400)
        with self.connect() as db:
            row=db.execute('SELECT profile FROM users WHERE id=?',(user_id,)).fetchone()
            if not row:raise AccessError('找不到逻辑用户。',404)
            profile=json.loads(row[0]);profile['quota'].update(changes)
            db.execute('UPDATE users SET profile=? WHERE id=?',(json.dumps(profile),user_id))

    def grant_environment(self,user_id,environment):
        with self.connect() as db:
            row=db.execute('SELECT profile FROM users WHERE id=?',(user_id,)).fetchone()
            if not row:raise AccessError('找不到逻辑用户。',404)
            profile=json.loads(row[0])
            profile['environments']=sorted(set(profile['environments'])|{environment})
            db.execute('UPDATE users SET profile=? WHERE id=?',(json.dumps(profile),user_id))

    def record(self, principal, action, target, outcome='allowed'):
        if not self.path:return
        with self.connect() as db:
            db.execute('INSERT INTO audit(at,actor,action,target,outcome) VALUES(?,?,?,?,?)',
                       (dt.datetime.now(dt.timezone.utc).isoformat(),principal.id,action,target[:100],outcome))
            db.execute('DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 10000)')

    def audit(self, principal):
        if not principal.admin:raise AccessError()
        with self.connect() as db:
            db.row_factory=sqlite3.Row
            return [dict(row) for row in db.execute('SELECT at,actor,action,target,outcome FROM audit ORDER BY id DESC LIMIT 100')]

    def project_role(self, principal, project):
        if principal.admin or project == 'personal:'+principal.id:
            return 'manager'
        if not self.path:
            return None
        with self.connect() as db:
            row = db.execute('SELECT role FROM members WHERE project=? AND user=?', (project,principal.id)).fetchone()
        return row[0] if row else None

    def projects(self, principal):
        result = [{'id':'personal:'+principal.id,'role':'manager'}]
        if self.path:
            with self.connect() as db:
                result += [{'id':row[0],'role':row[1]} for row in db.execute('SELECT project,role FROM members WHERE user=? ORDER BY project', (principal.id,))]
        return result

    def can_read(self, principal, job):
        return principal.admin or job['owner']==principal.id or bool(self.project_role(principal, job.get('project', 'personal:'+job['owner'])))

    def readable_jobs(self, principal, jobs):
        if principal.admin:return list(jobs)
        # One membership lookup per list request, rather than one DB connection per task.
        projects={p['id'] for p in self.projects(principal)}
        return [job for job in jobs if job['owner']==principal.id or job.get('project','personal:'+job['owner']) in projects]

    def can_cancel(self, principal, job):
        return principal.admin or job['owner']==principal.id or self.project_role(principal, job.get('project', 'personal:'+job['owner']))=='manager'

    def public_profile(self, principal):
        return {'id':principal.id,'role':principal.role,'workspace':principal.workspace,
                'execution_mode':principal.mode,'projects':self.projects(principal),
                'quota':principal.quota,'scopes':sorted(principal.scopes),
                'identity_kind':'application-account' if not principal.legacy else 'service-administrator',
                'isolation':'execution-environment-dependent' if not principal.legacy else 'personal-trusted-account'}
