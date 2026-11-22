"""Old import path, kept for two releases (T-2742): the module is sampo.lanepilot."""
import sys
import sampo.lanepilot
sys.modules[__name__] = sys.modules["sampo.lanepilot"]
