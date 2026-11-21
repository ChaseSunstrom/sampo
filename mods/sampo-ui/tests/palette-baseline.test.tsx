import { expect, test } from 'claude-code/testing'

import { C, setPalette } from '../hooks/kit'
import { BASELINE } from './fixtures/baseline-palettes'

// T-2876: the forge re-skin changes the default palette's look only. A user who chose cb (colourblind-safe), riso
// (print inks) or saved a custom palette sees exactly the colours they had: each key of the baseline, as it was.
for (const name of ['cb', 'riso', 'custom'] as const) {
  test(`palette ${name} keeps every colour it had before the re-skin (baseline)`, async () => {
    setPalette(name === 'custom' ? 'default' : name, name === 'custom' ? BASELINE.custom : undefined)
    const now: Record<string, number> = { ...C }
    for (const [k, v] of Object.entries(BASELINE.palettes[name])) expect([k, now[k]]).toEqual([k, v])
    setPalette('default')
  })
}
