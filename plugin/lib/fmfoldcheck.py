"""Old import path, kept for two releases (T-2742): the module is sampo.foldcheck."""
import sys
import sampo.foldcheck
sys.modules[__name__] = sys.modules["sampo.foldcheck"]
