import os
from setuptools import setup, find_packages

# Get the long description from the README file
here = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(here, "README.md"), encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="massspecgym",
    packages=find_packages(),
    version="0.2.0",
    description="MassSpecGym: Benchmark For the Discovery of New Molecules From Mass Spectra",
    author="MassSpecGym developers",
    license="MIT",
    long_description=long_description,
    long_description_content_type="text/markdown",
    # url="",  # TODO: Add URL to documentation
    install_requires=[
        "torch==2.3.0",
        "pytorch-lightning==2.2.5",
        "torchmetrics==1.4.0",
        "torch_geometric==2.5.3",
        "tokenizers==0.19.1",
        "numpy==1.24.4",
        "rdkit==2023.9.4",
        "myopic-mces==1.0.0",
        "matchms==0.26.2",
        "wandb==0.17.0",
        "huggingface-hub==0.23.2",
        "seaborn==0.13.2",
        "standardizeUtils @ git+https://github.com/boecker-lab/standardizeUtils@b415f1c51b49f6c5cd0e9c6ab89224c8ad657a35#egg=standardizeUtils",
        "chemparse==0.3.1",
        "chemformula==1.3.1",
        "networkx==3.3"
    ],
    extras_require={
        "dev": [
            "black==24.4.2",
            "pytest==8.2.1",
            "pytest-cov==5.0.0",
        ],
        "notebooks": [
            "jupyter==1.0.0",
            "ipywidgets==8.1.3",
            "h5py==3.11.0",
            "scikit-learn==1.5.0",
            "pandarallel==1.6.5",
        ],
    }
)
