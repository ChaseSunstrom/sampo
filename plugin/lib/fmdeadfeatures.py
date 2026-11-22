"""Old import path, kept for two releases (T-2742): the module is sampo.deadfeatures."""
import sys
import sampo.deadfeatures
sys.modules[__name__] = sys.modules["sampo.deadfeatures"]
