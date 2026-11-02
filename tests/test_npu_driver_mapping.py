"""Driver ABI/mapping failures must not become device or workload qualification."""
import ctypes
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from npu_driver_mapping_check import ABI,mapping,verify_header,parse_trace,capture_trace


class Function:
    def __init__(self,callback):self.callback=callback
    def __call__(self,*args):return self.callback(*args)


class Library:
    def __init__(self,*,count=1,logical=0,physical=1,codes=None):
        self.seen=[];codes=codes or {}
        def function(name,value,kind):
            def invoke(*args):
                self.seen.append((name,args[:-1]))
                ctypes.cast(args[-1],ctypes.POINTER(kind))[0]=value
                return codes.get(name,0)
            return Function(invoke)
        self.dsmi_get_device_count=function('dsmi_get_device_count',count,ctypes.c_int)
        self.dsmi_get_logicid_from_phyid=function('dsmi_get_logicid_from_phyid',logical,ctypes.c_uint)
        self.dsmi_get_phyid_from_logicid=function('dsmi_get_phyid_from_logicid',physical,ctypes.c_uint)


class DriverMappingTests(unittest.TestCase):
    def test_local_header_matches_exact_types_not_unrelated_mapping_symbols(self):
        header='\n'.join('DLLEXPORT int '+name+'('+args+');' for name,(args,_) in ABI.items())
        verify_header(header)
        for bad in [header.replace('unsigned int phyid','int phyid'),header.replace('int *device_count','unsigned int *device_count'),
                    header.replace('dsmi_get_logicid_from_phyid','drvDeviceGetIndexByPhyId')]:
            with self.assertRaises(ValueError):verify_header(bad)

    def test_nonidentity_mapping_uses_returned_logic_and_proves_no_workload(self):
        lib=Library(logical=7);value=mapping(lib,1)
        self.assertEqual(value['mapping'],{'physical_id':1,'logical_id':7,'roundtrip_physical_id':1})
        self.assertEqual(lib.seen[-1][1],(7,))
        self.assertEqual(lib.dsmi_get_device_count.argtypes,[ctypes.POINTER(ctypes.c_int)])
        self.assertEqual(lib.dsmi_get_logicid_from_phyid.argtypes,[ctypes.c_uint,ctypes.POINTER(ctypes.c_uint)])
        self.assertEqual(lib.dsmi_get_phyid_from_logicid.restype,ctypes.c_int)
        self.assertFalse(value['qualification_granted']);self.assertFalse(value['acl_initialized'])
        self.assertEqual(value['container_service_setup_calls'],0)

    def test_missing_api_prevents_all_calls(self):
        lib=Library();del lib.dsmi_get_phyid_from_logicid
        self.assertEqual(mapping(lib,1)['status'],'api_unavailable');self.assertEqual(lib.seen,[])

    def test_failed_count_prevents_mapping(self):
        lib=Library(codes={'dsmi_get_device_count':-8020});value=mapping(lib,1)
        self.assertEqual(value['status'],'device_count_failed');self.assertEqual(len(lib.seen),1)

    def test_count_must_be_positive_bounded_and_written(self):
        for count in [-1,0,257]:
            lib=Library(count=count)
            self.assertEqual(mapping(lib,1)['status'],'device_count_unconfirmed');self.assertEqual(len(lib.seen),1)

    def test_physical_mapping_failure_prevents_reverse_call(self):
        lib=Library(codes={'dsmi_get_logicid_from_phyid':4})
        self.assertEqual(mapping(lib,1)['status'],'physical_mapping_failed');self.assertEqual(len(lib.seen),2)

    def test_unwritten_or_out_of_bound_logical_id_is_not_guessed_zero(self):
        for logical in [256,0xffffffff]:
            lib=Library(logical=logical)
            self.assertEqual(mapping(lib,1)['status'],'logical_id_unconfirmed');self.assertEqual(len(lib.seen),2)

    def test_reverse_error_or_wrong_card_is_unconfirmed(self):
        self.assertEqual(mapping(Library(codes={'dsmi_get_phyid_from_logicid':4}),1)['status'],'reverse_mapping_failed')
        value=mapping(Library(physical=0),1)
        self.assertEqual(value['status'],'mapping_roundtrip_unconfirmed');self.assertNotIn('mapping',value)

    def test_input_type_and_range_are_checked_before_calls(self):
        lib=Library()
        for physical in [True,False,-1,256,1.0,'1',None]:
            with self.assertRaises(ValueError):mapping(lib,physical)
        self.assertEqual(lib.seen,[])

    def test_noninteger_return_is_not_success(self):
        lib=Library(codes={'dsmi_get_device_count':None})
        with self.assertRaises(ValueError):mapping(lib,1)


class TraceBoundaryTests(unittest.TestCase):
    def test_device_open_then_manager_ioctl_busy_is_distinct_and_private(self):
        text=('876543 openat(AT_FDCWD, "/dev/davinci_manager", O_RDWR|O_CLOEXEC) = 3</dev/davinci_manager<char 236:0>>\n'
              '876543 ioctl(3</dev/davinci_manager<char 236:0>>, 0xc01044fe, 0xdeadbeef) = -1 EBUSY (Device or resource busy)\n')
        value=parse_trace(text,1)
        self.assertEqual([(e['operation'],e['return_value'],e['errno']) for e in value['events']],[('openat',3,None),('ioctl',-1,'EBUSY')])
        for forbidden in ['876543','0xdeadbeef','236:0','0xc01044fe']:self.assertNotIn(forbidden,json.dumps(value))
        self.assertFalse(value['complete_history'])

    def test_device_open_failure_is_not_reported_as_ioctl_failure(self):
        value=parse_trace('openat(AT_FDCWD, "/dev/davinci1", O_RDWR) = -1 EACCES (Permission denied)',1)
        self.assertEqual(value['events'],[{'operation':'openat','path':'/dev/davinci1','return_value':-1,'errno':'EACCES'}])

    def test_successful_ioctl_and_close_do_not_create_a_failure(self):
        value=parse_trace('11 ioctl(3</dev/davinci1>, 123, 0xabcdef) = 0\n11 close(3</dev/davinci1>) = 0',1)
        self.assertEqual(len(value['events']),2)
        self.assertTrue(all(e['return_value']==0 and e['errno'] is None for e in value['events']))

    def test_other_paths_other_cards_and_unparsed_payload_do_not_leak(self):
        text=('22 openat(AT_FDCWD, "/home/other/private-key", O_RDONLY) = 3\n'
              '22 ioctl(3</dev/davinci0>, 1, "sensitive-payload") = -1 EBUSY\n'
              '22 write(1, "/dev/davinci1 sensitive-payload", 40) = 40\n')
        self.assertEqual(parse_trace(text,1)['events'],[])

    def test_metadata_has_no_approved_physical_device(self):
        self.assertEqual(parse_trace('open("/dev/davinci1", O_RDWR) = 3',None)['events'],[])

    def test_truncated_relevant_line_is_not_treated_as_success(self):
        value=parse_trace('23 ioctl(3</dev/davinci1>, 1, 0xabc <unfinished ...>',1)
        self.assertTrue(value['parser_incomplete']);self.assertEqual(value['events'],[])

    def test_trace_event_and_line_budgets_are_explicit(self):
        text='ioctl(3</dev/davinci1>, 1, 0) = 0\n'*100+'x'*5000
        value=parse_trace(text,1)
        self.assertEqual(len(value['events']),64);self.assertTrue(value['parser_incomplete'])

    def test_snapshot_bytes_are_hashed_and_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'syscalls.log';raw=b'x'*(300*1024);path.write_bytes(raw)
            value=capture_trace(path,1)
            self.assertEqual(value['captured_bytes'],256*1024)
            self.assertEqual(base64.b64decode(value['raw_base64']),raw[:256*1024])
            self.assertTrue(value['prefix_truncated']);self.assertFalse(value['summary']['complete_history'])

    def test_links_fifo_and_multiple_link_file_are_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);original=root/'trace';original.write_text('private')
            link=root/'link';link.symlink_to(original)
            with self.assertRaises(OSError):capture_trace(link,1)
            os.link(original,root/'hardlink')
            with self.assertRaises(ValueError):capture_trace(original,1)
            fifo=root/'fifo';os.mkfifo(fifo)
            with self.assertRaises(ValueError):capture_trace(fifo,1)

    def test_bad_physical_types_are_rejected(self):
        for value in [True,-1,256,'1']:
            with self.assertRaises(ValueError):parse_trace('',value)


if __name__=='__main__':unittest.main()
