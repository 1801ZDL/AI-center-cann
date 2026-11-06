"""Explicit IPv4 listener and verified native TLS; no certificate issuance or network probes."""
import hashlib
from http.server import ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import ssl
import stat
import threading
import urllib.parse
from contextlib import ExitStack

MAX_PEM=256*1024

class ListenerError(ValueError):pass


def address(value):
    try:parsed=ipaddress.IPv4Address(value)
    except (ValueError,TypeError):raise ListenerError('Bind address must be a literal IPv4 address') from None
    if str(parsed)!=value or parsed.is_multicast:raise ListenerError('Invalid bind address')
    return value


def open_pem(stack,value,private=False):
    if not isinstance(value,str) or not Path(value).is_absolute() or value!=str(Path(value)) or '..' in Path(value).parts:
        raise ListenerError('TLS material paths must be canonical absolute paths')
    path=Path(value);owners={0,os.geteuid()}
    for parent in reversed([path.parent,*path.parent.parents]):
        info=parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners or info.st_mode&0o022:
            raise ListenerError('TLS material requires protected directories without symlinks')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    source=stack.enter_context(os.fdopen(fd,'rb'));before=os.fstat(source.fileno())
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid not in owners
        or before.st_mode&(0o077 if private else 0o022) or before.st_size>MAX_PEM):
        raise ListenerError('TLS material must be a protected regular file; private keys require mode 600')
    data=source.read(MAX_PEM+1)
    if not data or len(data)>MAX_PEM:raise ListenerError('TLS material is empty or too large')
    return source,before,data


def verify_name(server_context,hostname,ca):
    client_context=ssl.create_default_context(cadata=ca.decode('ascii') if ca else None)
    client_context.minimum_version=ssl.TLSVersion.TLSv1_2
    client_context.hostname_checks_common_name=False
    ci,co,si,so=[ssl.MemoryBIO() for _ in range(4)]
    client=client_context.wrap_bio(ci,co,server_side=False,server_hostname=hostname)
    server=server_context.wrap_bio(si,so,server_side=True)
    client_done=server_done=False;transferred=0
    for _ in range(100):
        for connection in [client,server]:
            try:
                connection.do_handshake()
                if connection is client:client_done=True
                else:server_done=True
            except ssl.SSLWantReadError:pass
        for output,input_ in [(co,si),(so,ci)]:
            if output.pending:
                block=output.read();transferred+=len(block)
                if transferred>1024*1024:raise ListenerError('TLS validation exceeds handshake budget')
                input_.write(block)
        if client_done and server_done:return client.version()
    raise ListenerError('TLS handshake validation did not complete')


def assess(bind_address,port,public_url=None,tls_cert=None,tls_key=None,tls_ca=None):
    from lab_client import controller_url
    bind_address=address(bind_address)
    if type(port) is not int or not 1024<=port<=65535:raise ListenerError('Listener port must be 1024..65535')
    present=any(x is not None for x in [tls_cert,tls_key,tls_ca])
    if not present:
        if bind_address!='127.0.0.1' or public_url is not None:
            raise ListenerError('Non-loopback listeners require explicit HTTPS URL, certificate and private key')
        return {'bind_address':bind_address,'port':port,'scheme':'http','public_url':'http://127.0.0.1:'+str(port),
                'read_only':True,'tls':False},None,{}
    if not tls_cert or not tls_key or not public_url:raise ListenerError('Provide TLS certificate, private key and explicit public HTTPS URL together')
    try:url=controller_url({'controller':public_url});parsed=urllib.parse.urlsplit(url)
    except ValueError:raise ListenerError('Invalid public HTTPS URL') from None
    if parsed.scheme!='https' or parsed.path not in {'','/'}:raise ListenerError('Native TLS requires an HTTPS origin without a path prefix')
    try:
        with ExitStack() as stack:
            cert,cs,cert_bytes=open_pem(stack,tls_cert)
            key,ks,key_bytes=open_pem(stack,tls_key,private=True)
            authority,cas,ca_bytes=open_pem(stack,tls_ca) if tls_ca else (None,None,None)
            context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
            context.set_alpn_protocols(['http/1.1'])
            def no_password():raise ListenerError('Encrypted private keys need an external TLS terminator; unattended key prompts are disabled')
            context.load_cert_chain('/proc/self/fd/'+str(cert.fileno()),'/proc/self/fd/'+str(key.fileno()),password=no_password)
            version=verify_name(context,parsed.hostname,ca_bytes)
            for source,before in [(cert,cs),(key,ks),*([(authority,cas)] if authority else [])]:
                after=os.fstat(source.fileno())
                if any(getattr(before,k)!=getattr(after,k) for k in ['st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns']):
                    raise ListenerError('TLS material changed while validating')
        materials={'certificate':cert_bytes,'private_key':key_bytes,**({'authority':ca_bytes} if ca_bytes else {})}
        hashes={name:hashlib.sha256(value).hexdigest() for name,value in materials.items()}
        material_sha256=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
        report={'bind_address':bind_address,'port':port,'scheme':'https','public_url':url,'tls':True,'read_only':True,
          'certificate_sha256':hashes['certificate'],'authority_sha256':hashes.get('authority'),
          'material_sha256':material_sha256,'hostname_and_chain_verified':True,'key_pair_verified':True,
          'minimum_tls':'TLSv1.2','validated_tls':version,'validation_network_connections':0}
        return report,context,materials
    except (OSError,ssl.SSLError,UnicodeError,ValueError):
        raise ListenerError('TLS material, key pair, certificate validity, trust chain or public hostname validation failed') from None


def runtime(config):
    tls=config.get('tls')
    if tls is not None and (not isinstance(tls,dict) or not {'certificate','private_key'}<=set(tls) or set(tls)-{'certificate','private_key','authority','material_sha256'}):
        raise ListenerError('Invalid TLS listener configuration')
    tls=tls or {}
    report,context,_=assess(config.get('host'),config.get('port'),config.get('controller') if tls else None,
      tls.get('certificate'),tls.get('private_key'),tls.get('authority'))
    if tls.get('material_sha256') and report['material_sha256']!=tls['material_sha256']:
        raise ListenerError('TLS material differs from the installed configuration; rotate it explicitly before restarting')
    return report,context


class HTTPServer(ThreadingHTTPServer):
    """Queue a bounded burst of team clients instead of the stdlib backlog of 5."""
    request_queue_size=64


class TLSHTTPServer(HTTPServer):
    """Perform bounded handshakes in request threads, never on the accept loop."""
    daemon_threads=True
    def __init__(self,server_address,handler,context,max_connections=64):
        self.context=context;self.slots=threading.BoundedSemaphore(max_connections)
        super().__init__(server_address,handler)
    def process_request(self,request,client_address):
        if not self.slots.acquire(blocking=False):
            request.close();return
        try:super().process_request(request,client_address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,request,client_address):
        connection=request
        try:
            request.settimeout(5)
            connection=self.context.wrap_socket(request,server_side=True)
            connection.settimeout(8)
            self.finish_request(connection,client_address)
        except (OSError,ssl.SSLError):pass
        finally:
            self.shutdown_request(connection);self.slots.release()


def rotate(prefix,tls_cert,tls_key,tls_ca=None):
    """Stopped-controller leaf renewal under the same explicit trust authority."""
    import uuid
    import deployment as deploy
    from state_lock import state_lock
    from lab_client import atomic_json
    from archive_api import open_artifact
    deploy.require_root();_,meta,_=deploy.verify(prefix)
    if meta['role']!='controller':raise ListenerError('TLS renewal belongs to a controller installation')
    state=Path(meta['state_dir'])
    with state_lock(state,exclusive=True):
        config=deploy.regular_json(state/'config.json',private=True)
        if not config.get('tls'):raise ListenerError('Install a TLS controller before renewing its certificate')
        report,_,materials=assess(config['host'],config['port'],config['controller'],tls_cert,tls_key,tls_ca)
        previous_authority=None
        if config['tls'].get('authority'):
            old=Path(config['tls']['authority'])
            if not old.is_relative_to(state):raise ListenerError('Installed trust authority must reside within this private state directory')
            with open_artifact(state,str(old.relative_to(state)),max_file=MAX_PEM) as source:
                previous_authority=hashlib.sha256(source.read()).hexdigest()
        if report['authority_sha256']!=previous_authority:
            raise ListenerError('Trust authority changes require a separate staged rollover; routine renewal preserves existing node/client trust')
        if report['material_sha256']==config['tls'].get('material_sha256'):
            return {'rotated':False,'started':False,'public_url':config['controller'],'authority_changed':False}
        directory=state/('tls-rotation-'+uuid.uuid4().hex[:12]);directory.mkdir(mode=0o700);os.chown(directory,meta['uid'],meta['gid'])
        tls={}
        for name,data in materials.items():
            path=directory/(name+'.pem')
            with path.open('xb') as output:output.write(data)
            path.chmod(0o600);os.chown(path,meta['uid'],meta['gid']);tls[name]=str(path)
        tls['material_sha256']=report['material_sha256'];config['tls']=tls
        if 'authority' in tls:config['ca_file']=tls['authority']
        else:config.pop('ca_file',None)
        atomic_json(state/'config.json',config);os.chown(state/'config.json',meta['uid'],meta['gid'])
        return {'rotated':True,'started':False,'public_url':config['controller'],'authority_changed':False,
                'certificate_sha256':report['certificate_sha256'],'previous_tls_material_retained':True}
