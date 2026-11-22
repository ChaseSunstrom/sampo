"""Old import path, kept for two releases (T-2742): the module is sampo.louhi_scratch."""
import sys
import sampo.louhi_scratch
sys.modules[__name__] = sys.modules["sampo.louhi_scratch"]
