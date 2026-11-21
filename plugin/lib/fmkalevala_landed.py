"""Old import path, kept for two releases (T-2742): the module is sampo.kalevala_landed."""
import sys
import sampo.kalevala_landed
sys.modules[__name__] = sys.modules["sampo.kalevala_landed"]
