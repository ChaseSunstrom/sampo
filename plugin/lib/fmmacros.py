"""Old import path, kept for two releases (T-2742): the module is sampo.macros."""
import sys
import sampo.macros
sys.modules[__name__] = sys.modules["sampo.macros"]
