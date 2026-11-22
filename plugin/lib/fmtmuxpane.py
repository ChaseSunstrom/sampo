"""Old import path, kept for two releases (T-2742): the module is sampo.tmuxpane."""
import sys
import sampo.tmuxpane
sys.modules[__name__] = sys.modules["sampo.tmuxpane"]
