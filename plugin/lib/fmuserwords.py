"""Old import path, kept for two releases (T-2742): the module is sampo.userwords."""
import sys
import sampo.userwords
sys.modules[__name__] = sys.modules["sampo.userwords"]
