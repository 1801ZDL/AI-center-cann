"""Independently accept the existing source-to-wheel job; never submits work.

Use the installed personal client for the large download. Re-running this check
resumes/verifies that same archive rather than rerunning compilation.
"""
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab_client import Client, atomic_json, now, read_json
from deployment import digest
from node_storage import manifest_sha256
from docker_runtime import discover, command as docker_command

os.umask(0o077)
base = ROOT / 'runtime/full-build-20261030'
state = read_json(base / 'control.json')
entry = next(item for item in reversed(state['jobs']) if item['phase'] == 'build')
identifier = entry['job_id']
owner = Client.from_profile(ROOT / 'runtime/personas' / state['user'] / 'client.json')
detail = owner.request('/api/jobs/' + identifier)
job = detail['job']
if job['state'] != 'succeeded':
    print(json.dumps({'accepted': False, 'job_id': identifier, 'state': job['state'],
                      'action': 'Observe the original job; no compilation or download was started'}))
    raise SystemExit(2)
assert len(detail['attempts']) == 1
attempt = detail['attempts'][0]['id']
assert job['resources'] == {'cpu': 2, 'memory_mib': 49152, 'shm_mib': 0, 'npu': 0}

prefix = base / 'independent-client'
if not prefix.exists():
    subprocess.run([str(ROOT / 'install-client'), '--prefix', str(prefix)], check=True, capture_output=True)
assert (prefix / 'client-only').is_file() and not (prefix / 'server.py').exists()
profile = base / 'personal-profile.json'
if not profile.exists():
    # Intentionally excludes browser/admin credentials, model config and local paths.
    atomic_json(profile, {'controller': owner.url, 'token': owner.config['token']})
cli = subprocess.run([str(prefix / 'labctl'), '--profile', str(profile), 'archive', identifier,
                      '--local-source', str(base / 'submission/build.py')],
                     check=True, capture_output=True, text=True, timeout=900)
download = json.loads(cli.stdout)
assert download['complete'] and download['job_state'] == 'succeeded'
archive = Path(download['directory'])
assert archive == base / 'submission/.ascend-lab-results' / identifier
manifest = owner.request('/api/jobs/' + identifier + '/archive-manifest')
assert read_json(archive / '.lab-complete.json')['manifest_sha256'] == download['manifest_sha256']
for file in manifest['files']:
    path = archive / file['name']
    assert path.is_file() and not path.is_symlink()
    assert path.stat().st_size == file['size'] and digest(path) == file['sha256']
    assert path.stat().st_mode & 0o077 == 0

provenance = read_json(archive / 'results/full-build-provenance.json')
assert provenance['passed'] and provenance['phase'] == 'complete' and provenance['uid'] == 23701
assert provenance['cpu_ids'] == [254, 255]
assert provenance['npu_kernel_launches'] == 0 and not provenance['external_dependencies_rebuilt']
assert provenance['ninja_entries'] > 800 and provenance['compiled_ir_constant'] == 42
assert provenance['source_archive_sha256'] == read_json(base / 'source.json')['archive_sha256']
wheel = archive / 'results' / provenance['wheel_file']
assert wheel.stat().st_size == provenance['wheel_bytes'] and digest(wheel) == provenance['wheel_sha256']
verified = 0
with zipfile.ZipFile(wheel) as package:
    records = [name for name in package.namelist() if name.endswith('.dist-info/RECORD')]
    assert len(records) == 1
    for name, checksum, size in csv.reader(io.StringIO(package.read(records[0]).decode())):
        if not checksum:
            continue
        algorithm, expected = checksum.split('=', 1)
        assert algorithm == 'sha256'
        value = hashlib.sha256()
        total = 0
        with package.open(name) as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                value.update(block)
                total += len(block)
        assert total == int(size) and base64.urlsafe_b64encode(value.digest()).decode().rstrip('=') == expected
        if name.endswith('.so') and Path(name).name.startswith('libtriton'):
            assert value.hexdigest() == provenance['new_extension_sha256']
        verified += 1
assert verified == provenance['record_files_verified']
installed = provenance['installed_import']
assert installed['extension_sha256'] == provenance['new_extension_sha256']
assert Path(installed['extension_path']).is_relative_to('/work/installed-wheel')
assert 'arith.constant 42' in (archive / 'results/build-smoke-output.mlir').read_text()
assert '-Wno-error=free-nonheap-object' in provenance['build_configuration']['cmake_args']
assert provenance['build_configuration']['development_link_audit_sha256'] == job['parameters']['lock']['files']['/opt/lab-toolchain-link-audit.json']
assert provenance['build_configuration']['pybind_import_audit_sha256'] == job['parameters']['lock']['files']['/opt/lab-pybind-import-audit.json']
assert provenance['build_configuration']['pybind_version'] == '3.0.2'
assert provenance['new_extension_sha256'] in provenance['new_extension_candidates']
assert list((archive/'results').glob('ninja-log-*.txt')) and provenance['build_command_succeeded']
assert 'free-nonheap-object' in (archive / 'results/full-build.log').read_text(errors='replace')

environment = read_json(archive / 'environment.json')
assert environment['node_daemon_uid'] == 0 and environment['task_uid'] == 23701
assert environment['image'] == entry['image']
assert environment['runtime_files'] == job['runtime_files']
assert all(digest(ROOT/name) == checksum for name, checksum in environment['runtime_files'].items())
assert environment['read_only_root'] and not environment['privileged']
assert environment['network_mode'] == 'none' and not environment['device_nodes'] and not environment['npu_ids']
assert environment['memory_limit'] == environment['memory_swap_limit'] == 49152 * 1024**2
assert environment['workspace']['allocation']['bytes'] == 32 * 1024**3
assert environment['workspace']['allocation']['inodes'] == 262144
assert not environment['workspace']['allocation']['durable']
assert environment['termination']['ExitCode'] == 0 and not environment['termination']['OOMKilled']
assert job['result']['execution_cleanup_confirmed']

binding, _ = discover()
node_read = r'''import json,sys
from pathlib import Path
root=Path(sys.argv[1]);attempt=sys.argv[2];volume=Path(sys.argv[3])
p=root/'attempts'/attempt;s=json.loads((p/'control/state.json').read_text())
t=json.loads((p/'control/artifact-transfer.json').read_text())
print(json.dumps({'stage':s['stage'],'controller_acknowledged':s.get('controller_acknowledged'),
 'artifact_gc_requested':s.get('artifact_gc_requested'),'artifact_gc':s.get('artifact_gc'),
 'artifact_receipt':s.get('artifact_receipt'),'transfer':t,
 'artifact_payload_absent':not (p/'artifacts').exists(),
 'workspace_remaining_entries':[x.name for x in volume.iterdir()]}))
'''
argv, env = docker_command(binding, ['docker', 'exec', state['root_node_container'],
    '/opt/lab-build-venv/bin/python', '-I', '-c', node_read, state['root_node_state'], attempt, state['root_workspace_path']])
node = json.loads(subprocess.run(argv, env=env, check=True, capture_output=True, text=True, timeout=30).stdout)
assert node['stage'] == 'cleaned' and node['controller_acknowledged'] and not node['artifact_gc_requested']
assert node['artifact_gc']['state'] == 'purged' and node['artifact_payload_absent']
assert node['artifact_receipt']['job_id'] == identifier and node['artifact_receipt']['attempt_id'] == attempt
assert node['artifact_receipt']['manifest_sha256'] == manifest_sha256(node['transfer']['manifest'])
assert not node['workspace_remaining_entries']
assert any(item['sha256'] == provenance['wheel_sha256'] and item['size'] == provenance['wheel_bytes']
           for item in node['transfer']['manifest'])

row = next(row for row in owner.request('/api/results?limit=200')['rows'] if row['job_id'] == identifier)
assert row['passed'] and row['state'] == 'succeeded' and row['archive_state'] == 'available'
assert identifier in owner.request('/api/results.csv', raw=True).decode('utf-8-sig')
bob = Client.from_profile(ROOT / 'runtime/personas/demo_bob/client.json')
assert identifier not in {row['job_id'] for row in bob.request('/api/results?limit=200')['rows']}
request = urllib.request.Request(bob.url + '/api/jobs/' + identifier,
                               headers={'Authorization': 'Bearer ' + bob.config['token']})
try:
    urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10)
except urllib.error.HTTPError as error:
    assert error.code == 404
else:
    raise AssertionError('Foreign user could read the private build')

report = {'passed': True, 'verified_at': now(), 'job_id': identifier, 'attempt_id': attempt,
    'single_attempt': True, 'wheel_bytes': wheel.stat().st_size, 'wheel_sha256': digest(wheel),
    'wheel_record_files_independently_verified': verified,
    'new_extension_sha256': provenance['new_extension_sha256'], 'ninja_entries': provenance['ninja_entries'],
    'source_commit': provenance['source_commit'], 'source_archive_sha256': provenance['source_archive_sha256'],
    'development_link_audit_sha256': provenance['build_configuration']['development_link_audit_sha256'],
    'pybind_import_audit_sha256': provenance['build_configuration']['pybind_import_audit_sha256'],
    'installed_new_extension_import': True, 'ir_transform_constant': 42,
    'personal_client_sibling_archive': True, 'archive': download,
    'environment_evidence_sha256': digest(archive / 'environment.json'),
    'runtime_files': environment['runtime_files'], 'node_service_source_release': state['root_node_source_release'],
    'provenance_sha256': digest(archive / 'results/full-build-provenance.json'),
    'node_receipt': node['artifact_receipt'], 'node_artifact_gc': node['artifact_gc'],
    'workspace_empty': True, 'result_table_and_csv': True, 'foreign_user_hidden': True,
    'task_uid': 23701, 'node_uid': 0, 'task_memory_mib': 49152, 'workspace_gib': 32,
    'workspace_provider': 'dedicated bounded tmpfs; not disk-backed ext4',
    'external_llvm_bishengir_rebuilt': False, 'npu_kernel_launches': 0,
    'scope': 'Real clean Triton source-to-wheel compilation, private install/new native import and IR transform; no NPU or full external dependency build acceptance'}
atomic_json(ROOT / 'evidence/full-build-live-validation.json', report)
print(json.dumps({key: report[key] for key in ['passed', 'job_id', 'wheel_bytes', 'wheel_sha256',
    'wheel_record_files_independently_verified', 'ninja_entries', 'workspace_empty', 'npu_kernel_launches']}, indent=2))
