"""Old import path, kept for two releases (T-2742): the module is sampo.kalevala_levels."""
import sys
import sampo.kalevala_levels
sys.modules[__name__] = sys.modules["sampo.kalevala_levels"]
