# -*- coding: utf-8 -*-
# Copyright (C) 2012-2014 MUJIN Inc
from distutils.core import setup
try:
    from mujincommon.setuptools import Distribution
except (ImportError, SyntaxError):
    from distutils.dist import Distribution

version = {}
exec(open('python/mujinzmqclient/version.py').read(), version)

setup(
    distclass=Distribution,
    name='mujinzmqclient',
    version=version['__version__'],
    packages=['mujinzmqclient'],
    package_dir={'mujinzmqclient': 'python/mujinzmqclient'},
    package_data={'mujinzmqclient': ['py.typed']},
    license='Apache License, Version 2.0',
    long_description=open('README.md').read(),
    # flake8 compliance configuration
    enable_flake8=True,  # Enable checks
    fail_on_flake=True,  # Fail builds when checks fail
    install_requires=[],
)
