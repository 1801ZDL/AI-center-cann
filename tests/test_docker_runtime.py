"""Transport selection, daemon replacement and unknown-attempt recovery boundaries."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import docker_runtime as runtime
from container_engine import DockerAttempt
from jobs import ROOT,atomic_json

BINDING={'endpoint':'unix:///var/run/docker.sock','daemon_id':'offline-daemon-1'}


class DockerRuntimeTests(unittest.TestCase):
    def test_remote_host_is_rejected_before_any_cli_or_connection(self):
        for host in ['tcp://user:private-value@server:2376','ssh://someone@host','npipe:////pipe/docker']:
            with patch('docker_runtime.subprocess.run') as run:
                with self.assertRaises(runtime.DockerRuntimeError) as raised:runtime.selected_endpoint({'DOCKER_HOST':host})
            run.assert_not_called();self.assertNotIn('private-value',str(raised.exception))

    def test_context_precedence_matches_docker_without_connecting_to_its_host(self):
        done=subprocess.CompletedProcess([],0,BINDING['endpoint']+'\n','')
        with patch('docker_runtime.binary',return_value='/usr/bin/docker'),patch('docker_runtime.subprocess.run',return_value=done) as run:
            address=runtime.selected_endpoint({'DOCKER_HOST':'tcp://ignored:2376','DOCKER_CONTEXT':'local-demo'})
        self.assertEqual(address,BINDING['endpoint']);self.assertEqual(run.call_args.args[0][-2:],['--','local-demo'])

    def test_explicit_transport_clears_ambient_docker_controls_and_uses_empty_config(self):
        with patch.dict(os.environ,{'DOCKER_HOST':'tcp://wrong:2376','DOCKER_CONTEXT':'wrong','DOCKER_CONFIG':'/private/config'}),\
             patch('docker_runtime.information',return_value={'ID':BINDING['daemon_id'],**{k:True for k in runtime.HARD_LIMITS}}),patch('docker_runtime.binary',return_value='/usr/bin/docker'):
            argv,env=runtime.command(BINDING,['docker','inspect','owned-container'])
        self.assertEqual(argv[-4:],['--host',BINDING['endpoint'],'inspect','owned-container'])
        self.assertIn('--config',argv);self.assertTrue(argv[2].endswith('/_docker_cli_empty'))
        self.assertFalse(any(key.startswith('DOCKER_') for key in env))

    def test_daemon_identity_change_blocks_mutations_before_the_cli(self):
        with patch('docker_runtime.information',return_value={'ID':'replacement'}),patch('docker_runtime.subprocess.run') as cli:
            with self.assertRaisesRegex(runtime.DockerRuntimeError,'identity changed'):runtime.command(BINDING,['docker','rm','owned-container'])
        cli.assert_not_called()

    def test_missing_required_limits_or_rootless_daemon_cannot_be_discovered(self):
        info={'ID':BINDING['daemon_id'],**{key:True for key in runtime.HARD_LIMITS}}
        for value in [{**info,'MemoryLimit':False},{**info,'SecurityOptions':['name=rootless']}]:
            with patch('docker_runtime.selected_endpoint',return_value=BINDING['endpoint']),patch('docker_runtime.information',return_value=value):
                with self.assertRaises(runtime.DockerRuntimeError):runtime.discover()

    def test_pinned_discovery_does_not_consult_a_new_personal_context(self):
        info={'ID':BINDING['daemon_id'],**{key:True for key in runtime.HARD_LIMITS}}
        with patch('docker_runtime.selected_endpoint') as selected,patch('docker_runtime.information',return_value=info):
            binding,_=runtime.discover(BINDING)
        selected.assert_not_called();self.assertEqual(binding,BINDING)

    def test_unbound_historical_attempt_is_never_attached_to_the_current_daemon(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-runtime-binding-') as tmp:
            grant={'attempt_id':'attempt-'+'1'*32,'owner':'alice'}
            config={'users':{'alice':{'uid':23001,'gid':23001}},'image':'sha256:'+'0'*64}
            first=DockerAttempt(tmp,grant,config)
            first.state.update(stage='running',started_epoch=1);first.save()
            recovered=DockerAttempt(tmp,grant,{**config,'docker_runtime':BINDING})
            with patch('docker_runtime.discover') as discovery:
                with self.assertRaisesRegex(runtime.DockerRuntimeError,'Historical'):recovered.binding()
            discovery.assert_not_called()

    def test_attempt_freezes_its_daemon_even_if_the_node_configuration_changes(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime',prefix='test-runtime-freeze-') as tmp:
            grant={'attempt_id':'attempt-'+'2'*32,'owner':'alice'}
            config={'users':{'alice':{'uid':23001,'gid':23001}},'docker_runtime':BINDING}
            DockerAttempt(tmp,grant,config)
            recovered=DockerAttempt(tmp,grant,{**config,'docker_runtime':{**BINDING,'daemon_id':'replacement'}})
            self.assertEqual(recovered.binding(),BINDING)
