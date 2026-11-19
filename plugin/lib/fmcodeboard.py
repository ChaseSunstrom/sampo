"""Old import path, kept for two releases (T-2742): the module is sampo.smithboard."""
import sys
import sampo.smithboard
sys.modules[__name__] = sys.modules["sampo.smithboard"]
