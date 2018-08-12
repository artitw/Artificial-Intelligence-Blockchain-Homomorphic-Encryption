# Artificial Intelligence on Blockchain with Federated Learning and Homomorphic Encryption
# Customers protect their privacy 
# Companies protect their models
Custom Coverage Goals:
- Help companies customize their plans using their data. Relevant data could be:
- Location
- Demographic
- Risk profiles
- Preferences

This is a fork of PySyft. Insurance companies will be able to build artificial intelligence models using customer data to better serve their customers. PySyft is a Python library for secure, private Deep Learning. PySyft decouples private data from model training, using [Multi-Party Computation (MPC)](https://en.wikipedia.org/wiki/Secure_multi-party_computation) over PyTorch and tensorflow.  

## See PySyft in Action
- [Emulate remote PyTorch execution](https://colab.research.google.com/drive/1vsgH0ydHyel5VRAxO2yhRQfXYUuIYkp5) - This notebook demonstrates the tensor passing between workers, though both the workers live in the same environment.
- Emulate remote PyTorch execution using sockets: [Server](https://colab.research.google.com/drive/1-Jb_E_nDuBGHIJ_psI95k-ukh-P_aly-#scrollTo=lrcghOJOWGHw) | [Client](https://colab.research.google.com/drive/1Je1rk7olA9uTWWaqvvt4_gXf7yX1rTBm) - This notebook demonstrates the tensor passing and remote execution, with workers living in different environments.
  > Note: Run Server before Client
- [Federated Learning](https://colab.research.google.com/drive/1F3ALlA3ogfeeVXuwQwVoX4PimzTDJhPy#scrollTo=PTCvX6H9JDCt) - This notebook demonstrates the model training over distributed data (data belonging to multiple owners).

## Docker
```bash
git clone https://github.com/OpenMined/PySyft.git
cd PySyft
scripts/run_docker.sh
```
> Image size: 769.4MB

The container mount the examples folder on a volume so every change on the notebooks is persistent. 
Furthermore the container is deleted when it is stopped, in a way to facilitate development. You just have to change PySyft code, and run the run_docker.sh script to observe changes you've made on notebooks. 
## Installation
> PySyft supports Python >= 3.6 and PyTorch 0.3.1

Pick the proper PyTorch version according to your machine: [CPU](http://download.pytorch.org/whl/cpu/torch-0.3.1-cp36-cp36m-linux_x86_64.whl) | [CUDA9.1](http://download.pytorch.org/whl/cu91/torch-0.3.1-cp36-cp36m-linux_x86_64.whl) | [CUDA9.0](http://download.pytorch.org/whl/cu90/torch-0.3.1-cp36-cp36m-linux_x86_64.whl) | [CUDA8.0](http://download.pytorch.org/whl/cu80/torch-0.3.1-cp36-cp36m-linux_x86_64.whl)

```bash
conda install pytorch=0.3.1 -c soumith
pip3 install -r requirements.txt
python3 setup.py install
```

## Run Unit Tests

```
python3 setup.py test
```

## License

[Apache License 2.0](https://github.com/OpenMined/PySyft/blob/master/LICENSE)

[![FOSSA Status](https://app.fossa.io/api/projects/git%2Bgithub.com%2Fmatthew-mcateer%2FPySyft.svg?type=large)](https://app.fossa.io/projects/git%2Bgithub.com%2Fmatthew-mcateer%2FPySyft?ref=badge_large)
