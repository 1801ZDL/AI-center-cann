"""Driver ABI/mapping failures must not become device or workload qualification."""
import ctypes
import unittest
from npu_driver_mapping_check import ABI,mapping,verify_header


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


if __name__=='__main__':unittest.main()
