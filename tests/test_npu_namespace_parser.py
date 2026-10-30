"""No holder PIDs/identities leak; unknown layouts do not become admission facts."""
import json
import unittest
from npu_namespace_mapping_check import parse


class NamespaceParserTests(unittest.TestCase):
    def setUp(self):self.known={'namespace_inodes':[12345,67890],'mnt':12345,'pid':67890,'pids':[123],'identifiers':['own-container']}

    def test_text_and_numeric_identifiers_parse_but_never_export_their_values(self):
        raw='ns_id 1 identify unknown-container root_tgid 666666 dev_num 1 namespace 777777, udev list:\nlogic_devid udevid\n0 0\n'
        value=parse(raw,self.known);self.assertTrue(value['parse_complete'])
        self.assertEqual(value['devices'][0]['known_zdl_matches'],0)
        self.assertEqual(value['devices'][0]['nonzero_text_identity_mappings'],1)
        for forbidden in ['unknown-container','666666','777777']:self.assertNotIn(forbidden,json.dumps(value))
        self.assertTrue(parse(raw.replace('unknown-container','0'),self.known)['parse_complete'])

    def test_only_known_process_or_namespace_matches_are_reported(self):
        raw='ns_id 128 identify own-container root_tgid 123 dev_num 1 namespace 12345, udev list:\nlogic_devid udevid\n0 1\n'
        row=parse(raw,self.known)['devices'][0]
        self.assertEqual(row['udevid'],1);self.assertNotIn('physical_id',row)
        self.assertEqual(row['known_zdl_matches'],1);self.assertEqual(row['known_zdl_mnt_inode_matches'],1)
        self.assertEqual(row['known_zdl_id_text_matches'],1)
        self.assertEqual(row['namespace_ids_at_least_128'],1)
        self.assertEqual(row['below_128_known_zdl_matches'],0)
        self.assertEqual(row['at_least_128_known_zdl_matches'],1)

    def test_low_mapping_is_not_inferred_to_belong_to_the_known_container(self):
        raw=('ns_id 1 identify unknown root_tgid 999 dev_num 1 namespace 999, udev list:\n0 0\n'
             'ns_id 128 identify 0 root_tgid 123 dev_num 1 namespace 555, udev list:\n0 0\n')
        row=parse(raw,self.known)['devices'][0]
        self.assertEqual(row['namespace_ids_below_128'],1)
        self.assertEqual(row['below_128_known_zdl_matches'],0)
        self.assertEqual(row['at_least_128_known_zdl_matches'],1)
        self.assertEqual(row['below_128_nonzero_identity_mappings'],1)

    def test_incomplete_or_unknown_layout_never_yields_device_ownership(self):
        for raw in ['', 'ns_id broken root_tgid 3',
          'ns_id 1 identify 0 root_tgid 12 dev_num 2 namespace 99, udev list:\nlogic_devid udevid\n0 0\n']:
            value=parse(raw,self.known);self.assertFalse(value['parse_complete']);self.assertNotIn('devices',value)


if __name__=='__main__':unittest.main()
