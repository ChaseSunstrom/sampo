"""Old import path, kept for two releases (T-2742): the module is sampo.flowreport."""
import sys
import sampo.flowreport
sys.modules[__name__] = sys.modules["sampo.flowreport"]
