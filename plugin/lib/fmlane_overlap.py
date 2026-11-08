"""Old import path, kept for two releases (T-2742): the module is sampo.lane_overlap."""
import sys
import sampo.lane_overlap
sys.modules[__name__] = sys.modules["sampo.lane_overlap"]
