"""Old import path, kept for two releases (T-2742): the module is sampo.seams."""
import sys
import sampo.seams
sys.modules[__name__] = sys.modules["sampo.seams"]
