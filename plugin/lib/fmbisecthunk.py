"""Old import path, kept for two releases (T-2742): the module is sampo.bisecthunk."""
import sys
import sampo.bisecthunk
sys.modules[__name__] = sys.modules["sampo.bisecthunk"]
