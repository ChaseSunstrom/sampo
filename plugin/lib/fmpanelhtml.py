"""Old import path, kept for two releases (T-2742): the module is sampo.panelhtml."""
import sys
import sampo.panelhtml
sys.modules[__name__] = sys.modules["sampo.panelhtml"]
