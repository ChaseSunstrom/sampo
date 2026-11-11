"""Old import path, kept for two releases (T-2742): the module is sampo.big."""
import sys
import sampo.big
sys.modules[__name__] = sys.modules["sampo.big"]
