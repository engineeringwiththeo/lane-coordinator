#!/usr/bin/env python3
"""Print this repo's lane-coordinator state dir, or 'missing <path>' if not set up.

Usage: python3 -I state_dir.py [repo_path]
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import state_dir

sd = state_dir(sys.argv[1] if len(sys.argv) > 1 else None)
print(sd if os.path.exists(os.path.join(sd, "routing.json")) else f"missing {sd}")
