"""Old import path, kept for two releases (T-2742): the module is sampo.scorecard."""
import sys
import sampo.scorecard
sys.modules[__name__] = sys.modules["sampo.scorecard"]
