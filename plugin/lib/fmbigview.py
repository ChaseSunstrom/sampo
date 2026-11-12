"""Old import path, kept for two releases (T-2742): the module is sampo.bigview."""
import sys
import sampo.bigview
sys.modules[__name__] = sys.modules["sampo.bigview"]
