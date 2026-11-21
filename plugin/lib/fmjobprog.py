"""Old import path, kept for two releases (T-2742): the module is sampo.jobprog."""
import sys
import sampo.jobprog
sys.modules[__name__] = sys.modules["sampo.jobprog"]
