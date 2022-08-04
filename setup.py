from setuptools import setup, find_packages

setup(
    name="kazu",
    version="0.0.1",
    license="Apache 2.0",
    author="AstraZeneca AI and Korea University",
    description="NER",
    install_requires=[
        "spacy==3.2.1",
        "torch==1.12.0",
        "torchvision==0.13.0",
        "torchaudio==0.12.0",
        "transformers==4.12.5",
        "ray[serve]==1.13.0",
        "rdflib==6.0.2",
        "hydra-core==1.1.1",
        "pytorch-lightning==1.7.0",
        "pydash==5.1.0",
        "pandas==1.3.4",
        "pyarrow==8.0.0",
        "gilda==0.10.3",
        "pytorch-metric-learning==0.9.99",
        "rapidfuzz==1.8.2",
        "seqeval==1.2.2",
        "py4j==0.10.9.3",
        "fastparquet== 0.8.0",
        "strsimpy==0.2.1",
        "scikit-learn==1.0.1",
    ],
    extras_require={
        "dev": [
            "black~=22.0",
            "flake8",
            "bump2version",
            "pre-commit",
            "pytest",
            "pytest-mock",
            "pytest-cov",
            "pytest-timeout",
            "sphinx",
            "myst_parser",
        ],
    },
    packages=find_packages(exclude=["*.tests", "*.tests.*", "tests.*", "tests"]),
    include_package_data=True,
    package_data={},
)
