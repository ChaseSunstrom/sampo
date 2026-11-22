"""Old import path, kept for two releases (T-2742): the module is sampo.sinks."""
import sys
import sampo.sinks
sys.modules[__name__] = sys.modules["sampo.sinks"]
