#!/usr/bin/env python3
"""
kilo_account.py - consumption and burn-rate view for Kilo Code, in tokens.

A thin wrapper over `kilo_doctor.py --account`, so the measurement half can be used on its own by
anyone wondering what their sessions are actually consuming - no stuck session required, and no
price table needed, which is what makes it work on a plan or allowance (a plan provider's catalog
entry is priced 0 on purpose).

    python kilo_account.py                 # last 24 h
    python kilo_account.py --hours 12

Run `python kilo_doctor.py --help` for the other modes (detect stuck sessions, archive-and-retire).
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DOCTOR = os.path.join(HERE, "kilo_doctor.py")

if not os.path.isfile(DOCTOR):
    sys.exit("kilo_doctor.py is missing next to this wrapper")

sys.exit(subprocess.call([sys.executable, DOCTOR, "--account"] + sys.argv[1:]))
