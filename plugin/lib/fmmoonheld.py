"""Old import path, kept for two releases (T-2742): the module is sampo.moonheld."""
import sys
import sampo.moonheld
sys.modules[__name__] = sys.modules["sampo.moonheld"]
