"""Old import path, kept for two releases (T-2742): the module is sampo.artfill."""
import sys
import sampo.artfill
sys.modules[__name__] = sys.modules["sampo.artfill"]
