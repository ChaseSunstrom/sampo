"""Old import path, kept for two releases (T-2742): the module is sampo.pluginmove."""
import sys
import sampo.pluginmove
sys.modules[__name__] = sys.modules["sampo.pluginmove"]
