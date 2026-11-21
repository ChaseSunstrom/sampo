"""T-2282: bin/fm as a test runs it: the state folder counts as the user's own for prove's trust file.

A test's home is never the user's (its FOREMAN_HOME is a scratch folder), and since T-2282 no tree is trusted under such a
home, so a test of what a trusted tree does would find every run refused. This script makes the one change that test
needs, in the process (never in an environment variable, which a session could set) and then runs bin/fm as it is.
helpers.fm runs it by default; fm(..., real_state=True) runs bin/fm itself, for the tests of the refusal.
"""
import os
import runpy
import sys

PLUGIN = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FM = os.path.join(PLUGIN, "bin", "fm")
sys.path.insert(0, os.path.join(PLUGIN, "lib"))
from sampo import prove  # noqa: E402

prove._state_is_real = lambda: True
sys.argv[0] = FM
runpy.run_path(FM, run_name="__main__")
