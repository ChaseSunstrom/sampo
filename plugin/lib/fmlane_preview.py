"""Old import path, kept for two releases (T-2742): the module is sampo.lane_preview."""
import sys
import sampo.lane_preview
sys.modules[__name__] = sys.modules["sampo.lane_preview"]
