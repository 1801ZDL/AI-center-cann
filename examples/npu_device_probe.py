"""Fixed device-file isolation check. It never initializes CANN or launches kernels."""
import json
import os
from pathlib import Path
import stat

ids=json.loads(os.environ['LAB_NPU_PHYSICAL_IDS']);assert len(ids)==1
paths=sorted(path for path in Path('/dev').glob('davinci[0-9]*') if path.name[7:].isdigit())
visible=sorted(int(path.name[7:]) for path in paths)
visibility_ok=visible==sorted(ids)
opened=[];errors=[]
for physical in ids:
    path=Path('/dev/davinci'+str(physical))
    try:
        assert stat.S_ISCHR(path.stat().st_mode)
        descriptor=os.open(path,os.O_RDWR|os.O_CLOEXEC);os.close(descriptor);opened.append(physical)
    except OSError as error:errors.append({'device_id':physical,'operation':'open O_RDWR','errno':error.errno,'message':error.strerror})
status={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in Path('/proc/self/status').read_text().splitlines() if ':' in line}
assert os.getuid()>=1000 and status['CapEff']=='0000000000000000' and status['NoNewPrivs']=='1'
result={'passed':visibility_ok and not errors,'visibility_ok':visibility_ok,'errors':errors,
        'physical_device_files':visible,'opened_devices':opened,'uid':os.getuid(),'gid':os.getgid(),
        'groups':os.getgroups(),'cap_effective':status['CapEff'],'no_new_privileges':status['NoNewPrivs'],
        'runtime_visible_ids':os.environ['ASCEND_RT_VISIBLE_DEVICES'],'npu_kernel_launches':0,
        'scope':'Device-node whitelist and non-root access; no ACL logical/physical mapping or numerical kernel verification'}
Path('/work/results/npu-device-probe.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result),flush=True)
raise SystemExit(0 if result['passed'] else 1)
