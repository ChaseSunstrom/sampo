"""Old import path, kept for two releases (T-2742): the module is sampo.racemetrics."""
import sys
import sampo.racemetrics
sys.modules[__name__] = sys.modules["sampo.racemetrics"]
