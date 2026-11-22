"""Old import path, kept for two releases (T-2742): the module is sampo.pulse."""
import sys
import sampo.pulse
sys.modules[__name__] = sys.modules["sampo.pulse"]
