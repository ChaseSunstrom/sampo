"""Old import path, kept for two releases (T-2742): the module is sampo.update."""
import sys
import sampo.update
sys.modules[__name__] = sys.modules["sampo.update"]
