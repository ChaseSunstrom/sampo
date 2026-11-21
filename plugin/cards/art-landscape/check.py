"""art-landscape's checker: both SVGs exist and pass the source gates, then render through works and pass; unrendered
(no browser, or the card jail) is reported as environment, never green."""
from artkit import art_check

art_check()
