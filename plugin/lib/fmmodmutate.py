"""Old import path, kept for two releases (T-2742): the module is sampo.modmutate."""
import sys
import sampo.modmutate
sys.modules[__name__] = sys.modules["sampo.modmutate"]
