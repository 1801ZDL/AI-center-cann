"""Fixed image entry: generate offline reports under the existing CPU workflow.

No controller credentials, task submission, collection, model or device access.
"""
import asyncio
import json
import os
from pathlib import Path
import sys

PREFIX=Path('/opt/ascend-lab-operator/app')


def main():
    if (len(sys.argv)!=1 or Path(__file__).resolve()!=PREFIX/'operator_node.py'
        or os.getuid()<1000 or os.getgid()<1000 or os.environ.get('LAB_JOB_DIR')!='/work'
        or os.environ.get('LAB_RESULT_FILE')!='/work/results/workload-result.json'):
        raise ValueError('Use the fixed image entry through its managed CPU workflow')
    status={key:value.strip() for key,sep,value in (line.partition(':') for line in Path('/proc/self/status').read_text().splitlines()) if sep}
    if status['CapEff']!='0000000000000000' or status['CapPrm']!='0000000000000000' or status['NoNewPrivs']!='1':
        raise ValueError('Managed analysis requires an unprivileged task identity')
    # Only this immutable, administrator-built image directory becomes importable.
    sys.path.insert(0,str(PREFIX))
    from operator_profile import analyze
    from lab_client import atomic_json
    report=asyncio.run(analyze(Path('/inputs'),Path('/work/results')))
    atomic_json('/work/results/workload-result.json',{'schema':'ascend-lab-result/v1','kind':'build','passed':True,
        'artifacts':['results/operator-analysis.json','results/operator-analysis.csv'],
        'purpose':'offline_operator_report_generation','uid':os.getuid(),'gid':os.getgid(),
        'cap_effective':status['CapEff'],'no_new_privileges':True,
        'generator_sha256':report['generator_sha256'],'npu_kernel_launches':0,
        'scope':'Existing build_v1 CPU artifact contract; no compiler build, NPU measurement or independent numerical certification.'})


if __name__=='__main__':main()
