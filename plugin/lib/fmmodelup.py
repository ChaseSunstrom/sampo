"""Old import path, kept for two releases (T-2742): the module is sampo.modelup."""
import sys
import sampo.modelup
sys.modules[__name__] = sys.modules["sampo.modelup"]
