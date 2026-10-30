"""Test teardown may terminate only authenticated CPU fixture holders."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from node_owner_binding_check import cleanup_owned_attempts,IMAGE


class OwnedFixtureCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime');self.base=Path(self.temp.name)
        self.attempt='attempt-'+'a'*32;self.calls=[];self.stopped=False
        self.info={'Config':{'Labels':{'io.ascend-lab.attempt':self.attempt},'Image':IMAGE},'Name':'/ascend-lab-'+'a'*32,
            'State':{'Running':True,'Pid':42,'ExitCode':0}}

    def tearDown(self):self.temp.cleanup()

    def docker(self,args,**kwargs):
        self.calls.append(args)
        if args[0]=='run':
            self.assertIn('type=bind,src='+str(self.base)+',dst=/state,readonly',args)
            self.assertFalse(any('docker.sock' in v for v in args));return SimpleNamespace(stdout=json.dumps([self.attempt]),stderr='')
        if args[0]=='ps':self.assertEqual(args[-1],'label=io.ascend-lab.attempt='+self.attempt);return SimpleNamespace(stdout='owned-id\n',stderr='')
        if args[0]=='inspect':
            value=json.loads(json.dumps(self.info))
            if self.stopped:value['State'].update(Running=False,Pid=0)
            return SimpleNamespace(stdout=json.dumps([value]),stderr='')
        if args[0]=='stop':self.stopped=True
        return SimpleNamespace(stdout='',stderr='')

    def test_termination_is_verified_before_removal_or_state_repair(self):
        cleanup_owned_attempts(self.base,self.base,[0],self.docker)
        actions=[v[0] for v in self.calls];self.assertLess(actions.index('stop'),actions.index('rm'))
        record=json.loads((self.base/'owned-attempt-cleanup.json').read_text());self.assertTrue(record['lease_history_preserved']);self.assertFalse(record['containers'][0]['after']['Running'])

    def test_foreign_label_or_image_is_not_stopped_or_removed(self):
        for field in ['label','image']:
            self.calls=[];self.info['Config'].update(Labels={'io.ascend-lab.attempt':self.attempt},Image=IMAGE)
            if field=='label':self.info['Config']['Labels']['io.ascend-lab.attempt']='attempt-'+'b'*32
            else:self.info['Config']['Image']='foreign-image'
            with self.assertRaises(AssertionError):cleanup_owned_attempts(self.base,self.base,[0],self.docker)
            self.assertFalse(any(v[0] in {'stop','rm'} for v in self.calls))


if __name__=='__main__':unittest.main()
