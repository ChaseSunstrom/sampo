"""Old import path, kept for two releases (T-2742): the module is sampo.worksgrow."""
import sys
import sampo.worksgrow
sys.modules[__name__] = sys.modules["sampo.worksgrow"]
