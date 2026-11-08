"""Old import path, kept for two releases (T-2742): the module is sampo.readme."""
import sys
import sampo.readme
sys.modules[__name__] = sys.modules["sampo.readme"]
