"""Old import path, kept for two releases (T-2742): the module is sampo.reach."""
import sys
import sampo.reach
sys.modules[__name__] = sys.modules["sampo.reach"]
