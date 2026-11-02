"""Native-call boundary and retained snapshots; never contacts the host bus."""
import copy
import ctypes
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import unit_bus_pin as pin
import unit_manager as unit

ANCHOR={'owner':':1.42','bus_id':'a'*32,'pid1':{'pid':1,'uid':0,'start_ticks':17,'boot_id':'12345678-1234-1234-1234-123456789abc'},'version':'249'}
VALUE={'unit':'ascend-lab-controller.service',**{k:ANCHOR[k] for k in ['owner','bus_id','pid1']}}


class Native:
    """Exact ABI boundary double, not a manager protocol emulator."""
    def __init__(self,overrides=None):
        self.rows={};self.calls=[];self.overrides=overrides or {}
        for name,fn in [('sd_bus_open_system',self.open),('sd_bus_set_method_call_timeout',lambda *_:0),
          ('sd_bus_call_method',self.call),('sd_bus_message_get_signature',lambda p,_:self.rows[p.value][0]),
          ('sd_bus_message_read_basic',self.read),('sd_bus_message_unref',lambda *_:None),
          ('sd_bus_flush_close_unref',lambda *_:None),('sd_bus_error_free',lambda *_:None)]:setattr(self,name,Mock(side_effect=fn))
    def open(self,p):ctypes.cast(p,ctypes.POINTER(ctypes.c_void_p))[0]=91;return 0
    def call(self,bus,dest,path,interface,method,error,reply,signature,*args):
        name=method.decode();self.calls.append((name,dest,signature,[x.value for x in args]))
        values={'GetId':(b's',b'a'*32),'GetNameOwner':(b's',b':1.42'),
                'GetConnectionUnixUser':(b'u',0),'GetConnectionUnixProcessID':(b'u',1),'RefUnit':(b'',None)}
        selected=self.overrides.get(name,values[name])
        if selected=='missing':ctypes.cast(error,ctypes.POINTER(pin.Error))[0].name=b'org.freedesktop.systemd1.NoSuchUnit';return -2
        key=len(self.rows)+1;self.rows[key]=selected;ctypes.cast(reply,ctypes.POINTER(ctypes.c_void_p))[0]=key;return 1
    def read(self,message,kind,out):
        signature,value=self.rows[message.value]
        if kind==b's':ctypes.cast(out,ctypes.POINTER(ctypes.c_char_p))[0]=value
        else:ctypes.cast(out,ctypes.POINTER(ctypes.c_uint32))[0]=value
        return 1


class PinTests(unittest.TestCase):
    def test_management_cli_error_classification_does_not_expose_private_path_or_message(self):
        import contextlib,runpy
        output=io.StringIO()
        with patch.object(sys,'argv',['servicectl','verify','--prefix','/opt/private-test']), \
             patch('deployment.verify',side_effect=PermissionError(13,'private-credential-message','/private/credential.json')), \
             contextlib.redirect_stderr(output),self.assertRaises(SystemExit) as error:
            runpy.run_path(str(ROOT/'servicectl'),run_name='__main__')
        self.assertEqual(error.exception.code,1);self.assertIn('PermissionError errno=13',output.getvalue())
        self.assertNotIn('private-credential-message',output.getvalue());self.assertNotIn('/private/credential.json',output.getvalue())

    def native(self,library,value=None):
        output=io.StringIO()
        with patch.dict(os.environ,{'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},clear=True), \
             patch.object(pin,'library_file',return_value=Path('/usr/lib/libsystemd.so.0')), \
             patch.object(pin,'pid1_matches',return_value=True),patch('ctypes.CDLL',return_value=library), \
             patch.object(sys,'stdin',SimpleNamespace(buffer=io.BytesIO())),patch.object(sys,'stdout',output):
            pin.retain(value or copy.deepcopy(VALUE))
        return json.loads(output.getvalue())

    def test_fixed_native_calls_verify_identity_before_the_only_reference_method(self):
        lib=Native();result=self.native(lib)
        self.assertTrue(result['ready']);self.assertEqual([v[0] for v in lib.calls],['GetId','GetNameOwner','GetConnectionUnixUser','GetConnectionUnixProcessID','RefUnit'])
        self.assertEqual(lib.calls[-1][1:],(b':1.42',b's',[b'ascend-lab-controller.service']))
        lib.sd_bus_flush_close_unref.assert_called_once();self.assertEqual(lib.sd_bus_message_unref.call_count,5)

    def test_bus_owner_uid_and_pid_mismatch_never_reference_a_unit(self):
        for name,value in [('GetId',(b's',b'b'*32)),('GetNameOwner',(b's',b':1.99')),
                           ('GetConnectionUnixUser',(b'u',1000)),('GetConnectionUnixProcessID',(b'u',2))]:
            with self.subTest(name=name):
                lib=Native({name:value})
                with self.assertRaises(pin.PinError):self.native(lib)
                self.assertNotIn('RefUnit',[r[0] for r in lib.calls]);lib.sd_bus_flush_close_unref.assert_called_once()

    def test_native_missing_and_wrong_signature_fail_closed_and_close_connection(self):
        for selected,kind in [('missing',pin.PinMissing),((b's',b'unexpected'),pin.PinError)]:
            lib=Native({'RefUnit':selected})
            with self.assertRaises(kind):self.native(lib)
            lib.sd_bus_flush_close_unref.assert_called_once()

    def test_foreign_unit_and_malformed_identity_rejected_before_native_loading(self):
        for change in [{'unit':'sshd.service'},{'unit':[]},{'owner':'org.freedesktop.systemd1'},
                       {'bus_id':'a'*31},{'pid1':{**ANCHOR['pid1'],'pid':True}}]:
            with self.subTest(change=change),patch('ctypes.CDLL') as load,self.assertRaises(pin.PinError):pin.retain({**VALUE,**change})
            load.assert_not_called()

    def test_inherited_bus_or_loader_configuration_is_rejected(self):
        for name in ['DBUS_SYSTEM_BUS_ADDRESS','LD_LIBRARY_PATH','PYTHONPATH']:
            with self.subTest(name=name),patch.dict(os.environ,{name:'private'},clear=True),patch('ctypes.CDLL') as load,self.assertRaises(pin.PinError):pin.retain(VALUE)
            load.assert_not_called()

    def test_stdin_eof_closes_actual_helper_process_without_any_bus(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as temp:
            helper=Path(temp)/'fixed-helper.py'
            helper.write_text('import json,sys;v=json.loads(sys.argv[1]);print(json.dumps({"ready":True,**{k:v[k] for k in ["unit","owner","bus_id"]}}),flush=True);sys.stdin.buffer.read(1)\n')
            with patch.object(pin,'helper_file',return_value=helper):
                holder=pin.Hold(VALUE['unit'],ANCHOR)
                with holder:self.assertIsNone(holder.process.poll())
                self.assertEqual(holder.process.returncode,0)

    def test_bad_readiness_identity_terminates_original_helper(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'runtime') as temp:
            helper=Path(temp)/'fixed-helper.py';helper.write_text('import json,sys;print(json.dumps({"ready":True,"owner":":1.99"}),flush=True);sys.stdin.buffer.read(1)\n')
            with patch.object(pin,'helper_file',return_value=helper):
                holder=pin.Hold(VALUE['unit'],ANCHOR)
                with self.assertRaises(pin.PinError):holder.__enter__()
                self.assertIsNotNone(holder.process.returncode)

    def test_snapshot_survives_inactive_unit_collection_only_while_retained(self):
        held=[];bus=unit.Bus();bus.anchor=Mock(return_value=ANCHOR)
        class Lease:
            def __init__(self,*args):pass
            def __enter__(self):held.append(True)
            def __exit__(self,*args):held.pop()
        def get(*args):
            if not held:raise unit.MissingUnit('inactive unit collected')
            return '/org/freedesktop/systemd1/unit/ascend_2dlab_2dcontroller_2eservice'
        bus.call=Mock(side_effect=get);bus.properties=Mock(side_effect=[{'Id':VALUE['unit']},{'MainPID':0}])
        with patch('unit_manager.os.geteuid',return_value=0),patch.object(pin,'Hold',Lease):result=bus.unit(VALUE['unit'])
        self.assertEqual(result['MainPID'],0);self.assertEqual(held,[])
        bus.call.assert_called_once();self.assertEqual(bus.call.call_args.args[3],'GetUnit')

    def test_missing_observation_does_not_implicitly_load_metadata(self):
        bus=unit.Bus();bus.call=Mock(side_effect=unit.MissingUnit('not loaded'))
        with patch.object(pin,'Hold') as hold:self.assertIsNone(bus.unit(VALUE['unit'],missing=True))
        hold.assert_not_called()

    def test_control_group_uses_the_native_service_interface(self):
        bus=unit.Bus();bus.anchor=Mock(return_value=ANCHOR);bus.call=Mock(return_value='/org/freedesktop/systemd1/unit/a')
        def properties(path,interface,schema):
            if interface==unit.BUS+'.Unit':
                if 'ControlGroup' in schema:raise unit.DeployError('Native Unit interface has no ControlGroup property')
                return {'Id':VALUE['unit']}
            self.assertEqual(schema['ControlGroup'],'s');return {'ControlGroup':''}
        bus.properties=Mock(side_effect=properties)
        with patch('unit_manager.os.geteuid',return_value=0),patch.object(pin,'Hold'):self.assertEqual(bus.unit(VALUE['unit'])['ControlGroup'],'')

    def test_manager_change_during_retained_readback_rejects_snapshot(self):
        bus=unit.Bus();bus.anchor=Mock(side_effect=[ANCHOR,{**ANCHOR,'bus_id':'b'*32}]);bus.call=Mock(return_value='/org/freedesktop/systemd1/unit/a')
        bus.properties=Mock(side_effect=[{},{}])
        with patch('unit_manager.os.geteuid',return_value=0),patch.object(pin,'Hold') as hold,self.assertRaises(unit.DeployError):bus.unit(VALUE['unit'])
        hold.return_value.__exit__.assert_called_once()

    def test_service_identity_reads_active_metadata_without_privileged_reference(self):
        bus=unit.Bus();bus.anchor=Mock(return_value=ANCHOR);bus.call=Mock(return_value='/org/freedesktop/systemd1/unit/a')
        bus.properties=Mock(side_effect=[{'ActiveState':'active'},{'MainPID':123}])
        with patch('unit_manager.os.geteuid',return_value=997),patch.object(pin,'Hold') as hold:
            self.assertEqual(bus.unit(VALUE['unit'])['MainPID'],123)
        hold.assert_not_called()

    def test_service_identity_cannot_load_or_retain_stopped_units(self):
        bus=unit.Bus();bus.anchor=Mock(return_value=ANCHOR);bus.call=Mock(return_value='/org/freedesktop/systemd1/unit/a')
        bus.properties=Mock(return_value={'ActiveState':'inactive'})
        with patch('unit_manager.os.geteuid',return_value=997),patch.object(pin,'Hold') as hold,self.assertRaises(unit.DeployError):bus.unit(VALUE['unit'])
        hold.assert_not_called();self.assertEqual(bus.properties.call_count,1)
