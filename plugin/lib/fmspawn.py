"""Old import path, kept for two releases (T-2742): the module is sampo.spawn."""
import sys
import sampo.spawn
sys.modules[__name__] = sys.modules["sampo.spawn"]
