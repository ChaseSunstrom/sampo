"""Old import path, kept for two releases (T-2742): the module is sampo.fastbuild."""
import sys
import sampo.fastbuild
sys.modules[__name__] = sys.modules["sampo.fastbuild"]
