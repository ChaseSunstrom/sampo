"""Old import path, kept for two releases (T-2742): the module is sampo.cardgap."""
import sys
import sampo.cardgap
sys.modules[__name__] = sys.modules["sampo.cardgap"]
