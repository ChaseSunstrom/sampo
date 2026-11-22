"""Old import path, kept for two releases (T-2742): the module is sampo.keeper."""
import sys
import sampo.keeper
sys.modules[__name__] = sys.modules["sampo.keeper"]
