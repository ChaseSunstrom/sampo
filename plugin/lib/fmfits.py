"""Old import path, kept for two releases (T-2742): the module is sampo.fits."""
import sys
import sampo.fits
sys.modules[__name__] = sys.modules["sampo.fits"]
