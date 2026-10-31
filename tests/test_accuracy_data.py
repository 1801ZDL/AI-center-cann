import copy
import json
import math
import struct
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from accuracy_data import compare,config_schema,csv_report,sha,strict_json


def fixture(actual,reference,*,actual_type='float32',reference_type='float64',policy=None):
    formats={'uint8':'B','float32':'f','float64':'d'}
    blobs={'actual':struct.pack('<'+formats[actual_type]*len(actual),*actual),
           'reference':struct.pack('<'+formats[reference_type]*len(reference),*reference)}
    value={'schema':'ascend-lab-accuracy-input/v1','name':'Explicit comparison','shape':[len(actual)],
        'policy':policy or {'mode':'numeric','atol':0.,'rtol':0.,'equal_nan':False,'signed_zero_equal':True}}
    for role,dtype in [('actual',actual_type),('reference',reference_type)]:
        value[role]={'file':role+'.bin','dtype':dtype,'sha256':sha(blobs[role]),
            'origin':{'id':role+'-fixture','kind':'user_supplied','source_sha256':'a'*64,'scope':'Synthetic unit data, not actual NPU evidence'}}
    return value,blobs


class AccuracyDataTests(unittest.TestCase):
    def test_mixed_float_types_use_reference_scaled_tolerance(self):
        value,blobs=fixture([1.,2.,3.],[1.,2.1,3.]);value['policy']['atol']=.11
        result=compare(value,blobs);self.assertTrue(result['passed']);self.assertEqual(result['elements'],3)
        self.assertAlmostEqual(result['numeric_metrics']['max_abs_error'],.1)
        value['policy']['atol']=.01;self.assertEqual(compare(value,blobs)['mismatches'],1)

    def test_nan_policy_is_explicit_and_never_outputs_nonfinite_json(self):
        value,blobs=fixture([math.nan,math.nan],[math.nan,1.]);result=compare(value,blobs)
        self.assertEqual(result['categories'],{'nan_mismatch':2});json.dumps(result,allow_nan=False)
        value['policy']['equal_nan']=True;self.assertEqual(compare(value,blobs)['mismatches'],1)

    def test_infinity_pairing_and_finite_statistics_are_separate(self):
        value,blobs=fixture([math.inf,-math.inf,3.,math.inf],[math.inf,math.inf,2.,4.])
        result=compare(value,blobs);self.assertEqual(result['categories'],{'infinity_mismatch':2,'tolerance_mismatch':1})
        self.assertEqual(result['numeric_metrics']['finite_pairs'],1);self.assertEqual(result['numeric_metrics']['max_abs_error'],1.)

    def test_signed_zero_and_zero_reference_do_not_fake_relative_error(self):
        value,blobs=fixture([-0.,1.],[0.,0.]);value['policy']['signed_zero_equal']=False
        result=compare(value,blobs);self.assertEqual(result['categories'],{'signed_zero_mismatch':1,'tolerance_mismatch':1})
        self.assertIsNone(result['numeric_metrics']['max_relative_error']);self.assertEqual(result['numeric_metrics']['nonzero_actual_zero_reference'],1)

    def test_encoding_difference_is_not_reported_as_numeric_error(self):
        value,blobs=fixture([111,239,128],[110,238,128],actual_type='uint8',reference_type='uint8',policy={'mode':'exact_encoding'})
        result=compare(value,blobs);self.assertEqual(result['mismatches'],2);self.assertIsNone(result['numeric_metrics'])

    def test_examples_are_bounded_and_keep_multidimensional_coordinates(self):
        value,blobs=fixture([1.]*40,[0.]*40);value['shape']=[4,10];result=compare(value,blobs)
        self.assertEqual(result['mismatches'],40);self.assertEqual(len(result['examples']),16);self.assertTrue(result['examples_truncated'])
        self.assertEqual(result['examples'][15]['coordinates'],[1,5])

    def test_tensor_hash_length_and_fixed_path_are_required(self):
        value,blobs=fixture([1.],[1.]);bad=copy.deepcopy(value);bad['actual']['file']='../other.bin'
        with self.assertRaises(ValueError):compare(bad,blobs)
        for raw in [b'x',b'\0'*4]:
            with self.assertRaises(ValueError):compare(value,{**blobs,'actual':raw})

    def test_shape_bool_product_and_nonfinite_or_huge_tolerance_fail(self):
        value,blobs=fixture([1.],[1.])
        for shape in [[True],[0],[1048576,2],[],[1]*9]:
            bad=copy.deepcopy(value);bad['shape']=shape
            with self.assertRaises(ValueError):compare(bad,blobs)
        for tolerance in [True,-1.,math.inf,math.nan,10**1000]:
            bad=copy.deepcopy(value);bad['policy']['rtol']=tolerance
            with self.assertRaises(ValueError):compare(bad,blobs)

    def test_finite_overflow_cannot_make_infinity_less_equal_infinity_pass(self):
        value,blobs=fixture([1e308],[-1e308],actual_type='float64');value['policy']['rtol']=1.8
        result=compare(value,blobs);self.assertFalse(result['passed']);self.assertIsNone(result['numeric_metrics']['max_abs_error'])
        self.assertTrue(result['numeric_metrics']['absolute_error_overflow']);self.assertEqual(result['numeric_metrics']['max_relative_error'],2.)
        value['policy']['rtol']=2.;self.assertTrue(compare(value,blobs)['passed']);json.dumps(result,allow_nan=False)

    def test_source_context_is_checked_but_not_compared(self):
        value,blobs=fixture([1.],[0.]);blobs['source_context']=struct.pack('<f',42.)
        value['source_context']={**value['actual'],'file':'source_context.bin','sha256':sha(blobs['source_context'])}
        self.assertEqual(compare(value,blobs)['examples'][0]['source_context'],42.)

    def test_duplicate_nonfinite_json_and_formula_csv_are_rejected_or_escaped(self):
        for raw in ['{"x":1,"x":2}','{"x":NaN}']:
            with self.assertRaises(ValueError):strict_json(raw)
        value,blobs=fixture([1.],[0.]);value['name']='=malicious()';text=csv_report(value,compare(value,blobs)).decode()
        self.assertIn("'=malicious()",text)


if __name__=='__main__':unittest.main()
