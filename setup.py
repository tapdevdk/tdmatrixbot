from setuptools import setup, find_packages

setup(
    name="tdmatrixbot",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "asyncio>=4.0.0,<5.0.0",
        "httpx>=0.28.1,<0.29.0",
        "livekit>=1.1.2,<2.0.0",
        "markdown>=3.10.2,<4.0.0",
    ],
)
