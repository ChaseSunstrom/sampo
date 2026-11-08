"""Old import path, kept for two releases (T-2742): the module is sampo.shape."""
import sys
import sampo.shape
sys.modules[__name__] = sys.modules["sampo.shape"]
