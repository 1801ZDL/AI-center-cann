import copy
import io
import json
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from simulation_data import analyze,capture_summary,config_schema,core_summary,instruction_table,sha,strict_json,union


def fixture():
    def event(name,pc,start,duration,code='template/Copy1D.cpp:3\noperator.py:4'):
        return {'ph':'X','pid':0,'tid':1,'name':name,'ts':start,'dur':duration,'args':{'pc_addr':hex(pc),'code':code}}
    trace={'schemaVersion':1,'profilingType':'op','displayTimeUnit':'ns','traceEvents':[
        {'ph':'M','name':'process_name','pid':0,'args':{'name':'PUSHQ'}},
        {'ph':'M','name':'process_name','pid':0,'args':{'name':'WARP_0'}},
        event('SIMT_LDG',16,10,4),event('SIMT_LDG',16,12,5),event('RV_VCMAX',32,12,1),event('WAIT_FLAG',48,18,0,''),
        {'ph':'s','name':'flow','pid':0,'tid':1,'ts':10}]}
    csv=b'instr,addr,pipe,call_count,cycles,running_time(us),detail\nSIMT_LDG,16,RVECLD,2,7,7,\nRV_VCMAX,32,RVECEX,1,1,1,\nWAIT_FLAG,48,SCALAR,1,0,0,\n'
    blobs={'core0.trace.json':json.dumps(trace).encode(),'core0.instructions.csv':csv,
        'capture.log':b'[WARN] Child process killed by signal 11\n[WARN] Running task failed, proceeding to data parsing\n[INFO] Profiling data parse finished.\n'}
    def item(name):return {'file':name,'sha256':sha(blobs[name])}
    value={'schema':'ascend-lab-simulation-input/v1','name':'Historical test','timestamp_unit':'us',
        'origin':{'id':'caller-supplied','kind':'historical_simulator_output','elf_sha256':'a'*64,'claimed_exit_code':None,'scope':'Synthetic unit fixture'},
        'log':item('capture.log'),'cores':[{'id':'core0.veccore0','trace':item('core0.trace.json'),'instructions':item('core0.instructions.csv')}]}
    return value,blobs


class SimulationDataTests(unittest.TestCase):
    def test_union_handles_overlap_adjacency_nested_and_zero(self):
        self.assertEqual(union([(2,4),(1,5),(5,6),(4,4),(9,11)]),7)

    def test_real_fields_are_joined_by_instruction_pc_despite_reused_pid_and_display_ns(self):
        value,blobs=fixture();result=analyze(value,lambda name,limit:blobs[name]);core=result['cores'][0]['analysis']
        self.assertEqual(core['pipes']['RVECLD'],{'events':2,'union_us':7.})
        self.assertEqual(core['recorded_union_us'],7.);self.assertEqual(core['span_us'],8.)
        self.assertEqual(core['ambiguous_process_ids'],[0]);self.assertFalse(core['pid_used_for_pipe_classification'])
        self.assertEqual(core['zero_duration_events'],1);self.assertTrue(core['csv_trace_counts_match'])
        self.assertEqual(core['instruction_groups']['copy_template']['union_us'],7.)
        self.assertEqual(core['timestamp_unit'],'us');self.assertEqual(core['display_time_unit'],'ns')
        self.assertEqual(result['capture']['capture_state'],'abnormal');self.assertFalse(result['capture']['capture_completeness_certified'])
        self.assertTrue(result['capture']['logged_parse_finished']);self.assertIsNone(result['optimization_speedup'])

    def test_parse_success_and_claimed_zero_do_not_certify_capture(self):
        self.assertEqual(capture_summary(b'Profiling data parse finished.',0)['capture_state'],'unverified')
        value=capture_summary(b'Child process killed by signal 11\nProfiling data parse finished.',0)
        self.assertTrue(value['exit_claim_conflicts_with_log']);self.assertEqual(value['capture_state'],'abnormal')

    def test_missing_mapping_and_count_differences_are_explicit(self):
        _,blobs=fixture();csv=blobs['core0.instructions.csv'].replace(b'RV_VCMAX,32,RVECEX,1,1,1,\n',b'').replace(b'RVECLD,2,',b'RVECLD,1,')
        result=core_summary(blobs['core0.trace.json'],csv)
        self.assertEqual(result['unmapped_events'],1);self.assertEqual(result['count_discrepancies'],2)
        self.assertFalse(result['csv_trace_counts_match']);self.assertEqual(result['pipes']['UNMAPPED']['events'],1)

    def test_ambiguous_csv_pipe_and_malformed_rows_are_rejected(self):
        _,blobs=fixture();raw=blobs['core0.instructions.csv']
        for extra in [b'SIMT_LDG,16,OTHER,1,1,1,\n',b'A,4,X,1,1,NaN,\n',b'A,4,X,-1,1,1,\n',b'A,4,X,1,1,1,,extra\n']:
            with self.subTest(extra=extra),self.assertRaises(ValueError):instruction_table(raw+extra)

    def test_incomplete_phases_negative_duration_nonfinite_and_missing_pc_rejected(self):
        _,blobs=fixture();original=json.loads(blobs['core0.trace.json'])
        for patch in [{'ph':'B'},{'dur':-1},{'ts':float('inf')},{'ts':True},{'args':{}},{'name':[]}]:
            data=copy.deepcopy(original);data['traceEvents'][2].update(patch)
            with self.subTest(patch=patch),self.assertRaises(ValueError):core_summary(json.dumps(data).encode(),blobs['core0.instructions.csv'])

    def test_hash_extent_manifest_paths_and_units_are_bounded(self):
        value,blobs=fixture()
        with self.assertRaises(ValueError):analyze(value,lambda name,limit:blobs[name]+b'changed')
        changes=[lambda v:v.update(timestamp_unit='ns'),lambda v:v['log'].update(file='../capture.log'),
            lambda v:v['cores'][0]['trace'].update(file='/tmp/trace.json'),lambda v:v.update(cores=v['cores']*5),
            lambda v:v['origin'].update(claimed_exit_code=True)]
        for change in changes:
            v=copy.deepcopy(value);change(v)
            with self.assertRaises(ValueError):config_schema(v)

    def test_duplicate_keys_and_huge_event_counts_rejected(self):
        with self.assertRaises(ValueError):strict_json(b'{"traceEvents":[],"traceEvents":[]}')
        _,blobs=fixture();data=json.loads(blobs['core0.trace.json']);data['traceEvents']=[data['traceEvents'][2]]*100001
        with self.assertRaises(ValueError):core_summary(json.dumps(data).encode(),blobs['core0.instructions.csv'])


if __name__=='__main__':unittest.main()
