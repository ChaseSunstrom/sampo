"""Old import path, kept for two releases (T-2742): the module is sampo.movemod."""
import sys
import sampo.movemod
sys.modules[__name__] = sys.modules["sampo.movemod"]
