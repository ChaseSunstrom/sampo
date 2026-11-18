"""Old import path, kept for two releases (T-2742): the module is sampo.cardmine."""
import sys
import sampo.cardmine
sys.modules[__name__] = sys.modules["sampo.cardmine"]
