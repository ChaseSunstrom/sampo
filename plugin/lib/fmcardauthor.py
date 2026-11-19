"""Old import path, kept for two releases (T-2742): the module is sampo.cardauthor."""
import sys
import sampo.cardauthor
sys.modules[__name__] = sys.modules["sampo.cardauthor"]
