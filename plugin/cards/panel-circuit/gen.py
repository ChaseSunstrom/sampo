"""panel-circuit: a circuit board: each stage a chip in its state (lit, blinking busy, warning, failed, idle), traces carrying current from the input pad through the chips to the output, dark after a failure, as a dark and a light SVG."""
from panelkit import card_main

card_main("circuit")
