"""Old import path, kept for two releases (T-2742): the module is sampo.steermeter."""
import sys
import sampo.steermeter
sys.modules[__name__] = sys.modules["sampo.steermeter"]
