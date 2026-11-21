"""Old import path, kept for two releases (T-2742): the module is sampo.panelterm."""
import sys
import sampo.panelterm
sys.modules[__name__] = sys.modules["sampo.panelterm"]
