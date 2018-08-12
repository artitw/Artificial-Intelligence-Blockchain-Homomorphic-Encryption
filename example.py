! git clone https://github.com/artitw/Artificial-Intelligence-Blockchain-Homomorphic-Encryption.git
from os import path, system
system('apt-get -q -y install git')
system('pip3 install --no-cache-dir --upgrade wheel')
from wheel.pep425tags import get_abbr_impl, get_impl_ver, get_abi_tag
platform = '{}{}-{}'.format(get_abbr_impl(), get_impl_ver(), get_abi_tag())

accelerator = 'cu80' if path.exists('/opt/bin/nvidia-smi') else 'cpu'

!pip install -q http://download.pytorch.org/whl/{accelerator}/torch-0.3.0.post4-{platform}-linux_x86_64.whl torchvision
import torch

!cd PySyft; pip install -r requirements.txt; python setup.py install

import os
import sys
module_path = os.path.abspath(os.path.join('./PySyft'))
if module_path not in sys.path:
    sys.path.append(module_path)

from syft.core.hooks import TorchHook
import torch

# this is our hook
hook = TorchHook()

x = torch.FloatTensor([-2,-1,0,1,2,3])
local = hook.local_worker

from syft.core.workers import VirtualWorker

remote = VirtualWorker(id=1,hook=hook)
local.add_worker(remote)

x = torch.FloatTensor([1,2,3,4,5])
x2 = torch.FloatTensor([1,1,1,1,1])

x.send(remote)
x2.send(remote)