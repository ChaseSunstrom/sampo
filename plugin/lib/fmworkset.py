"""Old import path, kept for two releases (T-2742): the module is sampo.workset."""
import sys
import sampo.workset
sys.modules[__name__] = sys.modules["sampo.workset"]
