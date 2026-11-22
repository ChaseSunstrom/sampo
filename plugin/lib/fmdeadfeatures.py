"""The import path for sampo.deadfeatures (T-5710): the dead-feature check, under its fm name like the other modules."""
import sys
import sampo.deadfeatures
sys.modules[__name__] = sys.modules["sampo.deadfeatures"]
