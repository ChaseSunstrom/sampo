"""Old import path, kept for two releases (T-2742): the module is sampo.hooksmoke."""
import sys
import sampo.hooksmoke
sys.modules[__name__] = sys.modules["sampo.hooksmoke"]
