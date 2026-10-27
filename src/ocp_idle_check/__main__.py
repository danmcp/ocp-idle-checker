"""`python -m ocp_idle_check` entry point."""

import sys

from .main import run

sys.exit(run(sys.argv[1:]))
