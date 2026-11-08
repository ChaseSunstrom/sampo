"""Old import path, kept for two releases (T-2742): the module is sampo.gatemap."""
import sys
import sampo.gatemap
sys.modules[__name__] = sys.modules["sampo.gatemap"]
