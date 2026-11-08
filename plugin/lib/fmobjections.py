"""Old import path, kept for two releases (T-2742): the module is sampo.objections."""
import sys
import sampo.objections
sys.modules[__name__] = sys.modules["sampo.objections"]
