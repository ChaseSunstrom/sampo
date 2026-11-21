"""Old import path, kept for two releases (T-2742): the module is sampo.foldpage."""
import sys
import sampo.foldpage
sys.modules[__name__] = sys.modules["sampo.foldpage"]
