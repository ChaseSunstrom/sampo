"""Old import path, kept for two releases (T-2742): the module is sampo.moonground."""
import sys
import sampo.moonground
sys.modules[__name__] = sys.modules["sampo.moonground"]
