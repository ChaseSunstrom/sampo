"""Old import path, kept for two releases (T-2742): the module is sampo.watchdog."""
import sys
import sampo.watchdog
sys.modules[__name__] = sys.modules["sampo.watchdog"]
