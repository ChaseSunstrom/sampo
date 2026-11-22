"""Old import path, kept for two releases (T-2742): the module is sampo.targets."""
import sys
import sampo.targets
sys.modules[__name__] = sys.modules["sampo.targets"]
