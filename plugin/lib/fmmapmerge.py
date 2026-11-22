"""Old import path, kept for two releases (T-2742): the module is sampo.mapmerge."""
import sys
import sampo.mapmerge
sys.modules[__name__] = sys.modules["sampo.mapmerge"]
