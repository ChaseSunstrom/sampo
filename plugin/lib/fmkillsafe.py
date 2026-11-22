"""Old import path, kept for two releases (T-2742): the module is sampo.killsafe."""
import sys
import sampo.killsafe
sys.modules[__name__] = sys.modules["sampo.killsafe"]
