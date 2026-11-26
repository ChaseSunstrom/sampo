# ANDON: T-5241 check_unwired has no census.json writer on this branch

Question: the reviewer is right. Nothing on this branch writes census.json with the "unwired" and "previous" keys. check_unwired (plugin/lib/sampo/doctor.py:602) reads them, so on this branch it returns PASS every time. The writer is the capture run from T-5239, which the CHANGELOG cites and which is not in this lane's history.

Assumption I proceed on: T-5239 lands the writer (it owns the capture run and the unwired count), and this lane only adds the read side. I made no code edit in this fix round, because I have no shell to run tests and no spec for the capture run's write path.

If the assumption is wrong: this lane must also write census.json. The write would go in the capture run, with the unwired count from wired.scan_full rows (len of the rows), "previous" set to the prior "unwired" value, and an atomic write. That needs a test first. Otherwise, merge T-5239 before T-5241, or add it as a Built on dependency.
