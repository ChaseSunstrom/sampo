"""Old import path, kept for two releases (T-2742): the module is sampo.funcreview."""
import sys
import sampo.funcreview
sys.modules[__name__] = sys.modules["sampo.funcreview"]
