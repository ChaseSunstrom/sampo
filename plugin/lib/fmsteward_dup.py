"""Old import path, kept for two releases (T-2742): the module is sampo.steward_dup."""
import sys
import sampo.steward_dup
sys.modules[__name__] = sys.modules["sampo.steward_dup"]
