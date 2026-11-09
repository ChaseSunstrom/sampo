"""Old import path, kept for two releases (T-2742): the module is sampo.spar."""
import sys
import sampo.spar
sys.modules[__name__] = sys.modules["sampo.spar"]
