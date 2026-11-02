import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import deployment as deploy
from deployment import DeployError
from payload_quota_admin import fingerprint
import unit_refresh as units
import unit_relocation as relocation


class UnitRelocationTests(unittest.TestCase):
    prefix=Path('/opt/relocation-fixture')

    def fixture(self,role='controller'):
        before={'role':role,'uid':999 if role=='controller' else 0,'gid':999 if role=='controller' else 0,
            'state_dir':'/var/lib/old-state','release_id':'a'*64,'state_schema':1}
        after={**before,'state_dir':'/var/lib/new-state','previous_state_dir':before['state_dir']}
        text=deploy.unit_text(self.prefix,before);original={'text':text,'inode':4,'device':3,'mode':0o444}
        with patch.object(deploy,'require_root'),patch.object(units,'_file',return_value=original):
            after[relocation.FIELD]=relocation.prepare(self.prefix,before,after,original)
        return before,after,text,deploy.unit_text(self.prefix,after),original

    def test_exact_old_unit_for_exact_new_installation_is_recognized(self):
        _,after,text,desired,_=self.fixture()
        self.assertTrue(relocation.accepts(text,self.prefix,after,desired))
        units._managed(text,self.prefix,after,desired)

    def test_custom_command_or_security_or_extra_text_cannot_use_the_proof(self):
        _,after,text,desired,_=self.fixture()
        for altered in [text+'# custom\n',text.replace('NoNewPrivileges=true','NoNewPrivileges=false'),
            text.replace('/current/servicectl','/current/other')]:
            with self.subTest(altered=altered[-20:]),self.assertRaises(DeployError):units._managed(altered,self.prefix,after,desired)

    def test_proof_cannot_be_reused_for_another_prefix_role_identity_or_state(self):
        _,after,text,desired,_=self.fixture()
        for key,value in [('uid',1000),('role','node'),('state_dir','/var/lib/third-state'),
            ('release_id','b'*64),('previous_state_dir','/var/lib/unrelated')]:
            changed={**after,key:value}
            with self.subTest(key=key):self.assertFalse(relocation.accepts(text,self.prefix,changed,desired))
        self.assertFalse(relocation.accepts(text,'/opt/other',after,desired))

    def test_new_unit_changes_require_new_provenance(self):
        _,after,text,desired,_=self.fixture()
        self.assertFalse(relocation.accepts(text,self.prefix,after,desired.replace('ProtectSystem=strict','ProtectSystem=full')))

    def test_tampered_record_and_unknown_fields_fail_closed(self):
        _,after,text,desired,_=self.fixture()
        for field,value in [('before_unit',text+'changed'),('binding_sha256','0'*64),('extra',True)]:
            changed=copy.deepcopy(after);changed[relocation.FIELD][field]=value
            with self.subTest(field=field),self.assertRaises(DeployError):relocation.accepts(text,self.prefix,changed,desired)

    def test_invalid_paths_or_unbounded_before_image_rejected_even_with_rehashed_id(self):
        _,after,_,_,_=self.fixture()
        for field,value in [('old_state','/var/lib/../other'),('new_state','/var/lib/old-state'),
            ('prefix','/opt//other'),('before_unit','x'*65537)]:
            record=copy.deepcopy(after[relocation.FIELD]);record[field]=value
            if field=='before_unit':record['before_sha256']=relocation.sha(value)
            record['id']=fingerprint({k:v for k,v in record.items() if k!='id'})
            with self.subTest(field=field),self.assertRaises(DeployError):relocation.validate(record)

    def test_restore_rechecks_original_template_identity(self):
        before,after,_,_,original=self.fixture();after.pop(relocation.FIELD)
        with patch.object(deploy,'require_root'),patch.object(units,'_file',return_value={**original,'inode':5}):
            with self.assertRaisesRegex(DeployError,'changed during restore'):relocation.prepare(self.prefix,before,after,original)

    def test_restore_cannot_change_service_identity_under_same_proof(self):
        before,after,_,_,original=self.fixture();after['uid']=1000
        with patch.object(deploy,'require_root'):
            with self.assertRaisesRegex(DeployError,'identity differs'):relocation.prepare(self.prefix,before,after,original)

    def test_node_proof_uses_node_identity_and_remains_role_bound(self):
        _,after,text,desired,_=self.fixture('node')
        self.assertTrue(relocation.accepts(text,self.prefix,after,desired))
        self.assertFalse(relocation.accepts(text,self.prefix,{**after,'role':'controller'},desired))

    def test_retained_contract_refuses_older_code_without_relocation_support(self):
        from maintenance import require_features
        with self.assertRaisesRegex(DeployError,'refusing downgrade'):
            require_features('/var/lib/unopened','controller',{'files':{}},{relocation.FIELD:{}})

    def test_malformed_role_is_reported_as_a_contract_error(self):
        _,after,_,_,_=self.fixture();value=copy.deepcopy(after[relocation.FIELD]);value['role']=[]
        with self.assertRaises(DeployError):relocation.validate(value)
