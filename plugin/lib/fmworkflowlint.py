"""Old import path, kept for two releases (T-2742): the module is sampo.workflowlint."""
import sys
import sampo.workflowlint
sys.modules[__name__] = sys.modules["sampo.workflowlint"]
