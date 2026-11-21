"""panel-flame: a trace waterfall: each span a bar on a shared clock, nested under its parent and coloured by service, a flame strip of the whole call on top, a playhead replaying it and a failed span marked, as a dark and a light SVG."""
from panelkit import card_main

card_main("flame")
