import { expect, test } from 'claude-code/testing'

import { C, paneMark, setPalette } from '../hooks/kit'

// T-2876: the heat language in the forge palette: what runs glows hot (the ember), what is finished cools to steel;
// cb, riso and a saved custom palette keep their own accent and ok for both, so nothing they showed changes.
test('the forge palette runs hot and finishes in steel', async () => {
  setPalette('forge') // T-3701: the default is the Board's now; the forge keeps its own name
  expect([C.hot, C.steel]).toEqual([0xff6a4a, 0xc8d8ec]) // tokens.json: ember, and the forge's steel highlight
  const luma = (c: number) => 0.2126 * (c >> 16) + 0.7152 * ((c >> 8) & 255) + 0.0722 * (c & 255)
  expect(Math.abs(luma(C.hot) - luma(C.steel))).toBeGreaterThanOrEqual(20) // apart on a 256-colour terminal too
})

for (const [name, custom] of [['cb', undefined], ['riso', undefined], ['default', { ok: '#00ff88' }]] as const) {
  test(`palette ${custom ? 'custom' : name} keeps its own accent and ok for running and finished work`, async () => {
    setPalette(name, custom)
    expect([C.hot, C.steel, C.todo]).toEqual([C.accent, C.ok, C.track])
    setPalette('default')
  })
}

test('the pane mark is the anvil-stone: rest at rest, rest and struck in turn while work runs', async () => {
  expect(paneMark(false, 0)).toBe('⊖')
  expect(paneMark(false, 7)).toBe('⊖')
  expect([paneMark(true, 0), paneMark(true, 1), paneMark(true, 2)]).toEqual(['⊖', '⊘', '⊖'])
})
