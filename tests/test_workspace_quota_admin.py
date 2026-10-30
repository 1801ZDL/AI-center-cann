"""Read-only plan boundaries for additive native UID workspace allocation."""
import copy
from pathlib import Path
import os
import subprocess
import sys
import unittest
import hashlib
import json

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import workspace_quota_admin as admin
from deployment import DeployError,ROOT,CORE,checked_manifest,SCHEMA
from quota_workspace import FORMAT
from test_quota_workspace import slot


def planned():
    old=slot();old['path']='/srv/quota/'+old['id']
    value={'format':admin.PLAN,'prefix':'/opt/node','release_id':'a'*64,'node_id':'worker-'+'b'*12,
        'control':'/var/lib/node','control_identity':[123,456],'configuration_sha256':'c'*64,'root':'/srv/quota',
        'before':{'format':FORMAT,'filesystem_id':old['filesystem_id'],'device':old['device'],'root_inode':2,
            'bytes':old['filesystem_bytes'],'inodes':old['filesystem_inodes'],'slots':[old]},
        'additions':[{'uid':23002,'bytes':16*1024**2,'inodes':128,'id':'slot-'+'d'*12,'storage_id':'e'*32}]}
    value['id']=admin.fingerprint(value);return value


def signed(value):
    value['id']=admin.fingerprint({k:v for k,v in value.items() if k!='id'});return value


class WorkspaceQuotaAdminTests(unittest.TestCase):
    def test_historical_service_manifest_may_omit_only_optional_admin_helper(self):
        records={name:{'size':0,'sha256':'a'*64} for name in CORE if name!='workspace_quota_admin.py'}
        def manifest(files):
            return {'format':'ascend-lab-release/v1','state_schema':SCHEMA,'files':files,
                'id':hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
        self.assertEqual(checked_manifest(manifest(records)),records)
        records.pop('worker.py')
        with self.assertRaises(DeployError):checked_manifest(manifest(records))

    def test_current_bundle_source_still_includes_the_admin_helper(self):
        self.assertIn('workspace_quota_admin.py',CORE)
        self.assertTrue((ROOT/'workspace_quota_admin.py').is_file())

    def test_plan_adds_only_a_new_allocation_and_preserves_existing_slots(self):
        value=planned();before=copy.deepcopy(value['before']);admin._validate_plan(value)
        after=copy.deepcopy(before);after['slots'].append(admin._candidate(value,value['additions'][0],987))
        self.assertTrue(admin._matches_after(value,after));self.assertEqual(value['before'],before)
        for change in [{'bytes':32*1024**2},{'path':'/elsewhere'}]:
            altered=copy.deepcopy(after);altered['slots'][0].update(change)
            self.assertFalse(admin._matches_after(value,altered))

    def test_tampering_unknown_fields_and_invalid_root_identity_are_rejected(self):
        value=planned();value['additions'][0]['bytes']*=2
        with self.assertRaises(DeployError):admin._validate_plan(value)
        for changed in [{'unexpected':1},{'control_identity':[True,456]},{'root':'/srv/quota/../other'}]:
            with self.subTest(changed=changed),self.assertRaises((DeployError,ValueError)):
                admin._validate_plan(signed({**planned(),**changed}))

    def test_existing_or_root_uid_and_unknown_quota_fields_are_rejected(self):
        for changes in [{'uid':23001},{'uid':0},{'uid':True},{'bytes':True},{'gid':23002}]:
            value=planned();value['additions'][0].update(changes)
            with self.subTest(changes=changes),self.assertRaises((DeployError,ValueError)):admin._validate_plan(signed(value))

    def test_capacity_and_duplicate_slot_identities_cannot_be_bypassed(self):
        for changed in [{'bytes':120*1024**2},{'inodes':4096},{'id':'bad/path'},
                        {'storage_id':planned()['before']['slots'][0]['storage_id']}]:
            value=planned();value['additions'][0].update(changed)
            with self.subTest(changed=changed),self.assertRaises((DeployError,ValueError)):admin._validate_plan(signed(value))
        value=planned();value['additions'].append(dict(value['additions'][0]))
        with self.assertRaises(ValueError):admin._validate_plan(signed(value))

    def test_unprivileged_cli_refuses_before_opening_plan_or_native_paths(self):
        if os.geteuid()==0:return
        result=subprocess.run([str(ROOT/'servicectl'),'workspace-add-apply','--prefix','/not-selected',
            '--plan','/not-readable-plan'],capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0);self.assertIn('requires root',result.stderr)


if __name__=='__main__':unittest.main()
