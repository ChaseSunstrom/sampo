"""Old import path, kept for two releases (T-2742): the module is sampo.vsearch."""
import sys
import sampo.vsearch
sys.modules[__name__] = sys.modules["sampo.vsearch"]
