"""Old import path, kept for two releases (T-2742): the module is sampo.localruns."""
import sys
import sampo.localruns
sys.modules[__name__] = sys.modules["sampo.localruns"]
