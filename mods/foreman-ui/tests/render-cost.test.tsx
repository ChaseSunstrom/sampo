import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import type { FmView } from '../types'
import BUSY from './fixtures/board-busy'
import GOLDEN_VIEW from './fixtures/pane-busy'

// T-3701: what one draw of the pane and the band costs, on one busy view (every panel's facts), so a restyle is
// measured before and after: the lower quartile of N mounts, printed as `FM-COST pane=… band=…` (ms). The bound is loose (a
// loaded host is slow); the printed numbers are what a change compares.
const N = 30

function world(on: On, view: unknown) {
  mock.clock(on)
  mock.store(on)
  mock.env(on, { FOREMAN_FM: 'fm', HOME: '/home/u' })
  on('process.run', async ($, e) => ({
    value: { exitCode: 0, stdout: e.argv[1] === 'ui' ? JSON.stringify(view as FmView) : 'ok', stderr: '', isStdoutTruncated: false, isStderrTruncated: false },
  }))
  on('ui.panes', async () => ({ value: [] }))
  on('session.id', async () => ({ value: 'sess-1' }))
  on('ui.open', async () => ({ value: undefined }))
  on('ui.toast', async () => ({ value: undefined }))
  on('ui.blit', async () => ({ value: undefined }))
  on('session.usage', async () => ({ value: { startedAt: 0, context: { window: 1000, percent: 30 }, rateLimits: [], cost: { usd: 1.5 } } }))
  on('agent.list', async () => ({ value: [] }))
  on('command.register', async ($, e) => ({ value: { command: e.name } }))
  on('tool.call', async () => ({ result: {} }))
  on('session.start', async ($, e) => ({ cwd: e.cwd }))
  on('ui.render', async ($, e) => {
    const { Text } = $.ui.resolve(e)
    return <Text>engine</Text>
  })
}

const median = (xs: number[]) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 4)]! // the lower quartile: a loaded host adds, never takes away

for (const [name, view] of [['busy', BUSY], ['golden', GOLDEN_VIEW]] as const)
  test(`render cost: the pane and the band on the ${name} view, median ms per mount (T-3701)`, { timeoutMs: 60000 }, async ($, on) => {
    world(on, view)
    await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
    const time = async (props: Record<string, unknown>, component: string) => {
      const ms: number[] = []
      for (let i = 0; i < N; i++) {
        const t = performance.now()
        const ui = await $.ui.mount({ plugin: 'sampo-ui', surface: 'terminal', component, requestId: 'foreman', props } as never)
        await ui.drawn()
        ms.push(performance.now() - t)
        await ui.unmount()
      }
      return median(ms)
    }
    const pane = await time({ title: 'Sampo', isFocused: true, bodyColumns: 90, placement: 'dock', scroll: { offset: 0, bodyRows: 60 }, view: {} }, 'Pane')
    // a dock tall enough for every panel of the board page (all nine drawn), the cost of the whole page
    const tall = await time({ title: 'Sampo', isFocused: true, bodyColumns: 104, placement: 'dock', scroll: { offset: 0, bodyRows: 120 }, view: {} }, 'Pane')
    const band = await time({ hasSurvey: false, isWorking: false, maxRows: 14, bodyColumns: 120, scroll: { offset: 0, bodyRows: 14 }, view: {} }, 'AbovePrompt')
    console.log(`FM-COST ${name} pane=${pane.toFixed(1)} band=${band.toFixed(1)} tall=${tall.toFixed(1)}`)
    expect(pane).toBeLessThan(500)
    expect(band).toBeLessThan(500)
  })
