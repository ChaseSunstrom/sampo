"""Old import path, kept for two releases (T-2742): the module is sampo.hotfn."""
import sys
import sampo.hotfn
sys.modules[__name__] = sys.modules["sampo.hotfn"]
