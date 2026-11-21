"""Old import path, kept for two releases (T-2742): the module is sampo.svgcards."""
import sys
import sampo.svgcards
sys.modules[__name__] = sys.modules["sampo.svgcards"]
