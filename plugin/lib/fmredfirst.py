"""Old import path, kept for two releases (T-2742): the module is sampo.redfirst."""
import sys
import sampo.redfirst
sys.modules[__name__] = sys.modules["sampo.redfirst"]
