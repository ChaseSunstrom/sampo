"""Old import path, kept for two releases (T-2742): the module is sampo.recipe."""
import sys
import sampo.recipe
sys.modules[__name__] = sys.modules["sampo.recipe"]
