"""Old import path, kept for two releases (T-2742): the module is sampo.kickoff."""
import sys
import sampo.kickoff
sys.modules[__name__] = sys.modules["sampo.kickoff"]
