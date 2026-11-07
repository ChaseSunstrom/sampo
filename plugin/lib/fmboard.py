"""Old import path, kept for two releases (T-2742): the module is sampo.board."""
import sys
import sampo.board
sys.modules[__name__] = sys.modules["sampo.board"]
