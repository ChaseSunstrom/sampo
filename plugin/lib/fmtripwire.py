"""Old import path, kept for two releases (T-2742): the module is sampo.tripwire."""
import sys
import sampo.tripwire
sys.modules[__name__] = sys.modules["sampo.tripwire"]
