"""T-5708: the lane pilot's trigger and report, the detached `python -m sampo.pilotsig TID` (sampo/pilotsig.py)."""
import sys
import sampo.pilotsig
sys.modules[__name__] = sys.modules["sampo.pilotsig"]
