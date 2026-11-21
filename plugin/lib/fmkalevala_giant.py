"""Old import path, kept for two releases (T-2742): the module is sampo.kalevala_giant."""
import sys
import sampo.kalevala_giant
sys.modules[__name__] = sys.modules["sampo.kalevala_giant"]
