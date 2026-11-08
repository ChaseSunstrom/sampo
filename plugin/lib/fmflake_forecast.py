"""Old import path, kept for two releases (T-2742): the module is sampo.flake_forecast."""
import sys
import sampo.flake_forecast
sys.modules[__name__] = sys.modules["sampo.flake_forecast"]
