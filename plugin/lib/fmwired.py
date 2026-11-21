"""Old import path, kept for two releases (T-2742): the module is sampo.wired."""
import sys
import sampo.wired
sys.modules[__name__] = sys.modules["sampo.wired"]
