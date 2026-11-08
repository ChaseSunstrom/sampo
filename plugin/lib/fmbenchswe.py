"""Old import path, kept for two releases (T-2742): the module is sampo.bench_swe."""
import sys
import sampo.bench_swe
sys.modules[__name__] = sys.modules["sampo.bench_swe"]
