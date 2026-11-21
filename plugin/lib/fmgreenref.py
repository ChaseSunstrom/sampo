"""Old import path, kept for two releases (T-2742): the module is sampo.greenref."""
import sys
import sampo.greenref
sys.modules[__name__] = sys.modules["sampo.greenref"]
