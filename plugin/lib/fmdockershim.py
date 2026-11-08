"""Old import path, kept for two releases (T-2742): the module is sampo.dockershim."""
import sys
import sampo.dockershim
sys.modules[__name__] = sys.modules["sampo.dockershim"]
