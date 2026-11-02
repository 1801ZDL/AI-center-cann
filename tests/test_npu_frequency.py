"""Frequency parsing, fail-closed optional telemetry, bounded queries and history."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import npu_frequency as frequency
import test_npu_conditions as condition_cases
from test_npu_device import policy
from npu_device import observation,observe


def common(physical=0,current=1650,rated=1650):
    return f'NPU ID : {physical}\nAicore Freq(MHZ) : {rated}\nAicore curFreq(MHZ) : {current}\nTemperature(C) : 70\n'


def reading(physical,instant,current=1650,rated=1650):
    return {'schema':frequency.SCHEMA,'status':'sampled','source':frequency.SOURCE,'device_id':physical,
            'sampled_at':instant,'current_mhz':current,'rated_mhz':rated}


class FrequencyParserTests(unittest.TestCase):
    def test_actual_25_7_common_labels_keep_rated_and_current_separate(self):
        self.assertEqual(frequency.parse(common(current=100),0),{'current_mhz':100,'rated_mhz':1650})
    def test_rated_only_zero_duplicated_wrong_card_multi_chip_and_nonfinite_refused(self):
        for text in [common().replace('Aicore curFreq(MHZ) : 1650',''),common(current=0),common(current='NaN'),
                     common(current='Infinity'),common(current=True),common(physical=1),common()+'Aicore curFreq(MHZ) : 100\n',
                     common()+'Chip Count : 2\n',common()+'Chip ID : 1\n',common()+'NPU ID : 0\n','x'*20000]:
            with self.subTest(text=text[:80]),self.assertRaises(ValueError):frequency.parse(text,0)
    def test_query_is_fixed_readonly_argv_and_count_bounded(self):
        with patch('collector.command',side_effect=lambda argv,timeout:common(int(argv[-1]))) as run:
            result=frequency.collect(list(range(10)),'/trusted/env','/trusted/tool')
        self.assertEqual(run.call_count,8);self.assertEqual(result[9]['status'],'not_sampled_budget')
        for call in run.call_args_list:
            self.assertIn('info -t common -i',call.args[0][4]);self.assertNotIn('set -t',call.args[0][4])
            self.assertLessEqual(call.kwargs['timeout'],1)
    def test_total_deadline_skips_later_cards_and_timeout_is_unknown(self):
        with patch('npu_frequency.time.monotonic',side_effect=[0,0,.1,4]),patch('collector.command',side_effect=subprocess.TimeoutExpired('fixed query',1)) as run:
            result=frequency.collect([0,1],'/trusted/env','/trusted/tool')
        self.assertEqual(run.call_count,1);self.assertEqual(result[0]['reason'],'query_timeout')
        self.assertEqual(result[1]['status'],'not_sampled_budget')
    def test_failure_does_not_leak_stderr_or_fall_back_to_rated(self):
        for error in [RuntimeError('private command and key'),ValueError('private'),OSError('private')]:
            with patch('collector.command',side_effect=error):result=frequency.collect([0],'/env','/tool')[0]
            self.assertIsNone(result['current_mhz']);self.assertNotIn('private',json.dumps(result))
    def test_normalizer_enforces_target_time_schema_numeric_types_and_strips_private(self):
        instant=dt.datetime.now(dt.timezone.utc).isoformat();good=reading(0,instant,current=100)
        result=frequency.normalize({**good,'pid':123,'command':'private'},0,instant)
        self.assertEqual(result['current_mhz'],100);self.assertNotIn('private',json.dumps(result))
        for bad in [{**good,'device_id':1},{**good,'current_mhz':True},{**good,'rated_mhz':float('nan')},
                    {**good,'sampled_at':'2020-01-01T00:00:00+00:00'},{**good,'source':'user workload'},
                    {**good,'sampled_at':'2026-01-01T00:00:00'}]:
            self.assertEqual(frequency.normalize(bad,0,instant)['status'],'unavailable')
        self.assertEqual(frequency.normalize(None,0,instant)['status'],'not_collected')
    def test_optional_node_frequency_missing_or_invalid_does_not_rewrite_health(self):
        instant=dt.datetime.now(dt.timezone.utc).isoformat();p=policy()
        body={'status':'ok','sampled_at':instant,'policy_sha256':__import__('npu_device').fingerprint(p),
              'devices':[{'id':0,'health':'Alarm','utilization_pct':0,'process_count':3,'frequency':reading(0,instant)}]}
        normalized=observation(body,p)['devices'][0];self.assertEqual(normalized['frequency']['current_mhz'],1650)
        body['devices'][0]['frequency']['current_mhz']=True;normalized=observation(body,p)['devices'][0]
        self.assertEqual(normalized['health'],'Alarm');self.assertEqual(normalized['process_count'],3)
        self.assertEqual(normalized['frequency']['status'],'unavailable')
    def test_observe_queries_frequency_separately_and_keeps_management_time(self):
        devices=[{'id':0,'name':'Ascend-test','health':'OK','utilization_pct':0}]
        with patch('npu_device.validate'),patch('npu_device.query',return_value=(devices,[])),patch('npu_device.now',return_value='2026-01-01T00:00:00+00:00'),\
             patch('npu_frequency.collect',return_value={0:reading(0,'2026-01-01T00:00:01+00:00')}) as query:
            body=observe(policy(),frequency_ids=[0])
        self.assertEqual(body['sampled_at'],'2026-01-01T00:00:00+00:00');self.assertEqual(body['devices'][0]['frequency']['sampled_at'],'2026-01-01T00:00:01+00:00')
        self.assertEqual(query.call_args.args[0],[0])


class FrequencyHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture=condition_cases.ConditionHistoryTests('runTest');self.fixture.setUp();self.addCleanup(self.fixture.tearDown)
        self.job=self.fixture.job
    def sample(self,t,current=1650,health='OK'):
        return self.fixture.reading(t,health,frequency=reading(0,self.fixture.iso(t),current))
    def test_dropped_low_frequency_retained_without_automatic_failure_or_pass(self):
        import npu_conditions as conditions
        for t in range(100):conditions.capture(self.job,self.sample(t*10,100 if t==1 else 1650))
        summary=self.fixture.finish(pre=self.sample(0),post=self.sample(1000))
        card=summary['frequency']['cards'][0]
        self.assertEqual((card['min_mhz'],card['max_mhz']),(100,1650));self.assertEqual(card['sample_count'],100)
        self.assertEqual(card['boundary_count'],2)
        self.assertEqual(card['rated_min_mhz'],1650);self.assertEqual(summary['status'],'sampled_no_alerts')
        self.assertFalse(summary['frequency']['performance_qualified']);self.assertEqual(summary['performance_qualification'],'unverified')
        self.assertEqual(len(self.job['_npu_conditions']['samples']),64)
    def test_cached_duplicate_no_double_count_conflicting_and_out_of_order_extrema_retained(self):
        import npu_conditions as conditions
        conditions.capture(self.job,self.sample(10));conditions.capture(self.job,self.sample(10))
        self.assertEqual(self.job['_npu_conditions']['frequency_stats']['0']['sample_count'],1)
        conditions.capture(self.job,self.sample(10,200));conditions.capture(self.job,self.sample(5,100))
        summary=self.fixture.finish(pre=self.sample(0),post=self.sample(20));card=summary['frequency']['cards'][0]
        self.assertEqual(card['min_mhz'],100);self.assertEqual(card['uncertain_order_count'],2)
        self.assertEqual(summary['frequency']['status'],'partial');self.assertEqual(summary['status'],'incomplete')
    def test_old_trimmed_inflight_trace_new_suffix_never_becomes_full_frequency_coverage(self):
        import npu_conditions as conditions
        self.job['_npu_conditions']={'samples':[conditions.sample(self.fixture.reading(0),self.job)],'sample_count':80,
            'dropped_samples':79,'management_error_count':0,'out_of_order_count':0,'conflicting_sample_count':0,
            'alert_sample_count':0,'unknown_health_count':0,'max_gap_seconds':0,'first_sampled_at':self.fixture.iso(0),'last_sampled_at':self.fixture.iso(0)}
        conditions.capture(self.job,self.sample(10));summary=self.fixture.finish(pre=self.sample(0),post=self.sample(20))
        card=summary['frequency']['cards'][0];self.assertEqual(card['missing_count'],80);self.assertEqual(card['status'],'partial')
    def test_old_cached_snapshot_absent_optional_field_is_not_a_conflict_on_upgrade(self):
        import npu_conditions as conditions
        conditions.capture(self.job,self.fixture.reading(0))
        del self.job['_npu_conditions']['samples'][0]['devices'][0]['frequency']
        del self.job['_npu_conditions']['frequency_stats']
        conditions.capture(self.job,self.fixture.reading(0))
        self.assertEqual(self.job['_npu_conditions']['sample_count'],1)
        self.assertEqual(self.job['_npu_conditions']['conflicting_sample_count'],0)
        self.assertEqual(self.job['_npu_conditions']['frequency_stats']['0']['missing_count'],1)
    def test_reopen_trace_and_query_failure_preserve_extrema_and_missing(self):
        import npu_conditions as conditions
        conditions.capture(self.job,self.sample(0,100));self.fixture.job=json.loads(json.dumps(self.job));self.job=self.fixture.job
        conditions.capture(self.job,{'status':'error'});conditions.capture(self.job,self.sample(10))
        summary=self.fixture.finish(pre=self.sample(0),post=self.sample(20));card=summary['frequency']['cards'][0]
        self.assertEqual(card['min_mhz'],100);self.assertEqual(card['missing_count'],1);self.assertEqual(card['status'],'partial')


class FrequencyProtocolTests(unittest.TestCase):
    def test_authenticated_node_frequency_reaches_original_archive_and_csv_without_qualification(self):
        import csv,io
        fixture=condition_cases.ConditionProtocolTests('runTest');fixture.setUp();self.addCleanup(fixture.doCleanups)
        original=fixture.fixture.reading
        def observed(**change):
            body=original(**change);body['devices'][0]['frequency']=reading(0,body['sampled_at'],100,1650);return body
        with patch.object(fixture.fixture,'reading',side_effect=observed):job,_,_=fixture.run_case('Alarm')
        evidence=json.loads((fixture.jobs.directory(job['id'])/'npu-conditions.json').read_text())
        self.assertEqual(evidence['summary']['frequency']['cards'][0]['min_mhz'],100)
        self.assertEqual(evidence['boundaries']['release']['devices'][0]['frequency']['current_mhz'],100)
        self.assertEqual(evidence['summary']['frequency']['cards'][0]['boundary_count'],2)
        self.assertFalse(evidence['summary']['frequency']['performance_qualified']);self.assertEqual(job['state'],'failed')
        self.assertEqual(job['device_conditions']['status'],'adverse_observed');self.assertEqual(len(fixture.jobs.attempts(job['id'])),1)
        row={'job_id':job['id'],'owner':job['owner'],'project':job['project'],'kind':'performance','name':'Synthetic frequency protocol',
             'state':'failed','passed':False,'metrics':{'mean_us':30},'device_conditions':job['device_conditions']}
        with fixture.jobs.connect() as db:db.execute('INSERT INTO workflow_results VALUES(?,?)',(job['id'],json.dumps(row)))
        exported=list(csv.DictReader(io.StringIO(fixture.jobs.results.csv(fixture.jobs.access.user('alice')).decode('utf-8-sig'))))[0]
        self.assertEqual(json.loads(exported['device_conditions'])['frequency']['cards'][0]['min_mhz'],100)
        self.assertEqual(fixture.jobs.results.rows(fixture.jobs.access.user('bob'))['total'],0)
