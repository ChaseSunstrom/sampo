// T-1534: what replay.test.tsx draws. tools/pane_replay.py writes its own copy of this file (a recorded view, the size,
// print: true) into a temporary copy of the mod; here it is the synthetic fixture at a fixed size, printing nothing.
import view from './pane-busy'

export default { view, columns: 80, rows: 40, print: false }
