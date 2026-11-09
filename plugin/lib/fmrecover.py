"""Old import path, kept for two releases (T-2742): the module is sampo.recover."""
import sys
import sampo.recover
sys.modules[__name__] = sys.modules["sampo.recover"]
