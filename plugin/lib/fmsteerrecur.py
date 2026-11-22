"""Old import path, kept for two releases (T-2742): the module is sampo.steerrecur."""
import sys
import sampo.steerrecur
sys.modules[__name__] = sys.modules["sampo.steerrecur"]
