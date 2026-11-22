"""Old import path, kept for two releases (T-2742): the module is sampo.examgrade."""
import sys
import sampo.examgrade
sys.modules[__name__] = sys.modules["sampo.examgrade"]
