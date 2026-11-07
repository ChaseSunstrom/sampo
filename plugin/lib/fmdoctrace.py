"""Old import path, kept for two releases (T-2742): the module is sampo.doctrace."""
import sys
import sampo.doctrace
sys.modules[__name__] = sys.modules["sampo.doctrace"]
