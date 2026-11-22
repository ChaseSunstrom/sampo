"""Old import path, kept for two releases (T-2742): the module is sampo.pilotsig."""
import sys
import sampo.pilotsig
sys.modules[__name__] = sys.modules["sampo.pilotsig"]
