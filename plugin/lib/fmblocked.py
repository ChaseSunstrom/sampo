"""Old import path, kept for two releases (T-2742): the module is sampo.blocked."""
import sys
import sampo.blocked
sys.modules[__name__] = sys.modules["sampo.blocked"]
