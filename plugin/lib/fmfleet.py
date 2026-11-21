"""Old import path, kept for two releases (T-2742): the module is sampo.fleet."""
import sys
import sampo.fleet
sys.modules[__name__] = sys.modules["sampo.fleet"]
