"""Old import path, kept for two releases (T-2742): the module is sampo.ahead."""
import sys
import sampo.ahead
sys.modules[__name__] = sys.modules["sampo.ahead"]
