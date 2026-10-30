"""One explicit Triton vector-add launch, only inside an allocated queue job."""
import json
import os
from pathlib import Path

if not os.environ.get('LAB_JOB_DIR') or not os.environ.get('ASCEND_RT_VISIBLE_DEVICES'):
    raise SystemExit('Submit through the central queue with one NPU; direct launch is disabled')
os.environ.setdefault('TRITON_KERNEL_DUMP','1')

import torch
import torch_npu
import triton
import triton.language as tl


@triton.jit
def add_kernel(X,Y,Z,N:tl.constexpr,B:tl.constexpr):
    offsets=tl.arange(0,B)
    a=tl.load(X+offsets,offsets<N,other=0)
    b=tl.load(Y+offsets,offsets<N,other=0)
    tl.store(Z+offsets,a+b,offsets<N)


def main():
    a_cpu=torch.arange(256,dtype=torch.float32)/256
    b_cpu=torch.full((256,),0.5,dtype=torch.float32)
    a=a_cpu.to('npu');b=b_cpu.to('npu');out=torch.empty_like(a)
    print('Device initialized. Issuing one explicit Triton launch.',flush=True)
    add_kernel[(1,)](a,b,out,256,256,num_warps=1)
    torch.npu.synchronize()
    actual=out.cpu();expected=a_cpu+b_cpu
    torch.testing.assert_close(actual,expected,rtol=0,atol=0)
    result={'passed':True,'npu_used':True,'elements':256,'dtype':'float32',
            'max_absolute_error':float((actual-expected).abs().max()),'explicit_triton_launches':1,
            'framework_internal_launch_count':'not measured','triton_version':triton.__version__,
            'triton_source':triton.__file__,'torch_version':torch.__version__,
            'visible_devices':os.environ['ASCEND_RT_VISIBLE_DEVICES'],
            'scope':'functional smoke; not a performance benchmark or full operator regression'}
    target=Path(os.environ['LAB_JOB_DIR'])/'results/npu-accuracy.json'
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
