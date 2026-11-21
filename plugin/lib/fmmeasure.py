"""Old import path, kept for two releases (T-2742): the module is sampo.measure."""
import sys
import sampo.measure
sys.modules[__name__] = sys.modules["sampo.measure"]
