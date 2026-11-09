# Frontier gap scoreboard (T-3930)

`sampo exam gap [RUN]` sets each local profile's held-out pass % beside a Claude reference arm's (Haiku by default,
`--sonnet`) on the same Louhi run. `--ref` has the reference answer exactly the held-out ids the profile's newest
line answered, and both pass rates are taken over the ids both answered (the row's `n`), so the comparison is paired. Gap = reference minus profile, in
points; positive means behind Claude. `--ref` runs the reference (reused for 7 days); `--trend` shows the last 14 runs per
profile with the commits between, and a McNemar paired credit on the held-out scenarios both rows answered.
Rows live in `research/gap.jsonl` with the git revision. The night bench appends rows for free; a "frontier gap" night
step runs the paid reference only when no Haiku reference of any run is under 7 days old, estimated as the `exam-arm`
feature, inside the night's budget; sensitive projects and degraded nights skip it. The live models panel draws the trend.

## First table

No reference run has been paid for yet (the build ran offline), so the first table is filled by the next night:
`sampo exam gap --ref` then `sampo exam gap`.

| profile | pass % | reference % | gap (pts) | fixed/found | top failure classes |
|---|---|---|---|---|---|
| (first night fills this) | | | | | |
