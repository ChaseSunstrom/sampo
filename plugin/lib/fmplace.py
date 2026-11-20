"""Old import path, kept for two releases (T-2742): the module is sampo.place."""
import sys
import sampo.place
sys.modules[__name__] = sys.modules["sampo.place"]
