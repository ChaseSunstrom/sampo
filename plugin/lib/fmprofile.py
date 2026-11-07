"""Old import path, kept for two releases (T-2742): the module is sampo.profile."""
import sys
import sampo.profile
sys.modules[__name__] = sys.modules["sampo.profile"]
