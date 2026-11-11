"""Old import path, kept for two releases (T-2742): the module is sampo.concerns."""
import sys
import sampo.concerns
sys.modules[__name__] = sys.modules["sampo.concerns"]
