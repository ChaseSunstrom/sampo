"""Old import path, kept for two releases (T-2742): the module is sampo.jobsreg."""
import sys
import sampo.jobsreg
sys.modules[__name__] = sys.modules["sampo.jobsreg"]
