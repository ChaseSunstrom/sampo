"""The shim for sampo.targets (T-5709): `sampo targets [--json]`, the outcome targets and their verdicts."""
import sys
import sampo.targets
sys.modules[__name__] = sys.modules["sampo.targets"]
