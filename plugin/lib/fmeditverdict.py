"""Old import path, kept for two releases (T-2742): the module is sampo.editverdict."""
import sys
import sampo.editverdict
sys.modules[__name__] = sys.modules["sampo.editverdict"]
