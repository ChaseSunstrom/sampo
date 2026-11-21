"""Old import path, kept for two releases (T-2742): the module is sampo.yardstick."""
import sys
import sampo.yardstick
sys.modules[__name__] = sys.modules["sampo.yardstick"]
