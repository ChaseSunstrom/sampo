"""Old import path, kept for two releases (T-2742): the module is sampo.panels."""
import sys
import sampo.panels
sys.modules[__name__] = sys.modules["sampo.panels"]
