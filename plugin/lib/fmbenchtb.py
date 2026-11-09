"""Old import path, kept for two releases (T-2742): the module is sampo.benchtb."""
import sys
import sampo.benchtb
sys.modules[__name__] = sys.modules["sampo.benchtb"]
