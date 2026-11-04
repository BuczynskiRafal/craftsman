export const meta = {
  name: 'hunt',
  description:
    'Craftsman hunt: run the orchestrator\'s hunt plan in parallel, verify each hunter\'s findings as soon as it finishes, chase every newly confirmed root cause with a variant hunter (3 generations at most), return a compact summary',
}

// Optional fast path for steps 3-5 of craft-review. The orchestrator keeps every judgment: it has run
// `craftsman review prepare`, written the intent brief and decided who hunts where. This script only
// runs that plan. It has no shell, so a small schema'd agent runs `craftsman review show --json`.
//
// args (an object, or its JSON text):
// {
//   base: "HEAD",                     // base ref (used when `brief` is not given)
//   intent: "<intent brief>",         // intent claims, with their lines (used when `brief` is not given)
//   brief: "<shared brief>",          // optional: the exact text every hunter and verifier starts with
//   simplicityModel: "haiku",         // optional: model for hunters whose lenses are only craft/design
//   maxGenerations: 3,                // optional, 1-3 (default 3)
//   hunters: [{
//     id: "h1",
//     slice: "<view path or command>" | { view, hunks: ["H1","H2"], files: ["a.py"] },
//     lenses: ["intent", "failure"],
//     smaller: <slice>,               // optional: the slice for the one retry after a failure
//     simplicity: true,               // optional: force the simplicity model
//   }]
// }
// Returns { generations, hunters, counts, findings: {proven, probable, question, dismissed, unjudged},
//           uncoveredHunks, failedHunters, problems, trail }.
//
// Flow, per hunter (all lineages run at once; the runtime caps concurrency):
//   hunt -> verifier for that hunter's findings -> ledger read -> one variant hunter per newly confirmed
//   root cause -> same again, up to maxGenerations. A lineage that confirms nothing new stops there.
// "New" is a heuristic: a confirmed finding within 3 lines of one already chased, in the same file,
// counts as the same root cause. Afterwards, findings nobody judged get a verifier too.

const REVIEWER = 'craftsman:craftsman-reviewer'
const VERIFIER = 'craftsman:craftsman-verifier'

const A = typeof args === 'string' ? JSON.parse(args) : (args || {})
if (!Array.isArray(A.hunters) || A.hunters.length === 0) {
  throw new Error('hunt: args.hunters must be a non-empty array of {id, slice, lenses}')
}
const maxGen = Math.min(3, Math.max(1, A.maxGenerations || 3))
const simplicityLenses = new Set(['craft', 'design'])
const brief = A.brief || `Base ref: ${A.base || 'HEAD'}\nIntent brief:\n${A.intent || '(none given)'}`
const CHUNK = 10 // finding ids per verifier
const SAME_CAUSE_LINES = 3

const sliceText = s => {
  if (typeof s === 'string') return s
  const parts = []
  if (s.view) parts.push(`view: ${s.view}`)
  if (s.hunks && s.hunks.length) parts.push(`hunks: ${s.hunks.join(',')} (craftsman review view --hunks ${s.hunks.join(',')})`)
  if (s.files && s.files.length) parts.push(`files: ${s.files.join(',')} (craftsman review view --files ${s.files.join(',')})`)
  return parts.join('; ') || 'the whole diff'
}

// ---- the ledger reader: the only place the script learns what is in the ledger ----
const str = { type: 'string' }
const nullable = { type: ['string', 'null'] }
const arr = (items) => ({ type: 'array', items })
const obj = (required, properties) => ({ type: 'object', required, properties })
// Targeted read: the named findings in full, plus the hunks of the files they cite.
const detailSchema = obj(['findings', 'hunks'], {
  findings: arr(obj(['id', 'verdict', 'path', 'line', 'title'], {
    id: str, verdict: nullable, lens: str, path: str, line: { type: 'number' }, title: str,
  })),
  hunks: arr(obj(['id', 'path', 'start_line', 'end_line'], { id: str, path: str, start_line: { type: 'number' }, end_line: { type: 'number' } })),
})
// Snapshot: every finding as id/verdict/resolution only, plus the hunks nobody covered.
const snapshotSchema = obj(['findings', 'missing'], {
  findings: arr(obj(['id', 'verdict', 'resolution'], { id: str, verdict: nullable, resolution: nullable })),
  missing: arr(str),
})
const detailPrompt = ids =>
  'Run exactly one command, `craftsman review show --json`, and run nothing else. Copy from its output, verbatim:\n' +
  `- findings: only the entries of "findings" whose id is one of ${ids.join(', ')} (skip an id that is not listed), each with id, verdict (null if none), lens, path, line, title;\n` +
  '- hunks: the entries of "hunks" whose path equals the path of a finding you returned, each with id, path, start_line, end_line.\n' +
  'Do not interpret, summarize or fix anything.'
const SNAPSHOT_PROMPT =
  'Run exactly one command, `craftsman review show --json`, and run nothing else. Copy from its output, verbatim:\n' +
  '- findings: every entry of "findings" with only id, verdict (null if none) and resolution (null if none);\n' +
  '- missing: the list at coverage.missing.\n' +
  'Do not interpret, summarize or fix anything.'
const problems = []
const readWith = async (label, prompt, schema) => {
  const o = { phase: 'ledger', schema, model: 'haiku', effort: 'low' }
  let r = await agent(prompt, { ...o, label })
  if (r === null) r = await agent(prompt, { ...o, label: `${label}-retry` })
  if (r === null) problems.push(`ledger read failed (${label})`)
  return r
}
const readFindings = (label, ids) => readWith(label, detailPrompt(ids), detailSchema)
const snapshot = label => readWith(label, SNAPSHOT_PROMPT, snapshotSchema)

// ---- hunters and verifiers ----
const huntSchema = obj(['findings', 'summary'], {
  findings: { type: 'array', items: str, description: 'ids (F#) of the findings you recorded, none if none' },
  summary: { type: 'string', description: 'lenses and slice covered, what you could not examine' },
})
// One command per tool call: a batch spread over several lines is not matched by a narrow Bash allowlist.
const MECHANICS = 'Run each craftsman command as its own Bash call (no heredocs, no && chains, no --from-json batches).\n'
const huntPrompt = (slice, lenses, extra = '') =>
  `${brief}\n\nSlice: ${sliceText(slice)}\n${extra}Lenses: ${lenses.join(',')}\n${MECHANICS}` +
  'Finish by returning the ids of the findings you recorded and a short summary.'
const verifyPrompt = ids =>
  `${brief}\n\nJudge findings ${ids.join(', ')}, and only those; every other finding has its own verifier. ` +
  'Merge duplicates only among your findings; a duplicate of a finding outside your list is dismissed with a reference to its file:line.\n' +
  MECHANICS

const isSimplicity = h => h.simplicity === true || (h.lenses.length > 0 && h.lenses.every(l => simplicityLenses.has(l)))

const chased = [] // {path, line} of every root cause that got (or is getting) a variant hunter
const claimed = new Set() // finding ids handed to a verifier
const trail = []
const failedHunters = []
let lastGen = 0

// Verify the given finding ids (one verifier per CHUNK), then read their verdicts back.
const verify = async (ids, gen, tag) => {
  const live = ids.filter(id => !claimed.has(id))
  live.forEach(id => claimed.add(id))
  if (live.length === 0) return { findings: [], hunks: [] }
  const chunks = []
  for (let i = 0; i < live.length; i += CHUNK) chunks.push(live.slice(i, i + CHUNK))
  await parallel(chunks.map((c, i) => async () => {
    const opts = { agentType: VERIFIER, phase: `gen ${gen}`, label: `verify-${tag}${chunks.length > 1 ? `-${i + 1}` : ''}` }
    let r = await agent(verifyPrompt(c), opts)
    if (r === null) r = await agent(verifyPrompt(c), { ...opts, label: `${opts.label}-retry` })
    if (r === null) problems.push(`verifier failed for ${c.join(',')}`)
  }))
  return readFindings(`ledger-${tag}`, live)
}

// The mini-slice of a variant hunter: the root cause, its location, the hunks around it.
const variantOf = (f, hunks) => {
  const inFile = hunks.filter(h => h.path === f.path)
  const around = inFile.filter(h => h.start_line - 3 <= f.line && f.line <= h.end_line + 3)
  const ids = (around.length ? around : inFile.slice(0, 2)).map(h => h.id)
  return {
    id: `variant-${f.id}`,
    slice: ids.length ? { hunks: ids } : 'the whole diff',
    lenses: [...new Set([f.lens || 'failure', 'sweep'])],
    extra:
      `Variant hunt. A verifier confirmed this root cause (${f.id}, ${f.verdict}): ${f.title} at ${f.path}:${f.line}.\n` +
      'Find the same mistake elsewhere: grep the whole diff (`craftsman review show --hunks`) and the code it touches or is touched by.\n' +
      'First run `craftsman review show`: record only root causes that are not listed there yet.\n',
  }
}

// Launch one variant hunter for each newly confirmed root cause.
const expand = async (confirmed, hunks, gen, parent) => {
  const fresh = []
  for (const f of confirmed) {
    if (chased.some(c => c.path === f.path && Math.abs(c.line - f.line) <= SAME_CAUSE_LINES)) continue
    chased.push({ path: f.path, line: f.line })
    fresh.push(f)
  }
  if (gen >= maxGen || fresh.length === 0) return
  await parallel(fresh.map(f => () => chain(variantOf(f, hunks), gen + 1, parent)))
}

const isConfirmed = f => f.verdict === 'proven' || f.verdict === 'probable'

// One lineage: hunt -> verify -> a variant hunter per new confirmed root cause -> ... (maxGen generations).
const chain = async (h, gen, parent) => {
  lastGen = Math.max(lastGen, gen)
  const tag = `${gen}-${h.id}`
  const lenses = h.lenses && h.lenses.length ? h.lenses : ['sweep']
  const opts = { agentType: REVIEWER, phase: `gen ${gen}`, label: `hunt-${tag}`, schema: huntSchema }
  if (isSimplicity({ ...h, lenses }) && A.simplicityModel) opts.model = A.simplicityModel
  let r = await agent(huntPrompt(h.slice, lenses, h.extra || ''), opts)
  if (r === null) r = await agent(huntPrompt(h.smaller || h.slice, lenses, h.extra || ''), { ...opts, label: `${opts.label}-retry` })
  if (r === null) {
    failedHunters.push(h.id)
    trail.push({ id: h.id, gen, parent, failed: true })
    return
  }
  const recorded = (r.findings || []).filter(id => typeof id === 'string')
  const entry = { id: h.id, gen, parent, recorded: recorded.length, confirmed: [] }
  trail.push(entry)
  if (recorded.length === 0) return
  const got = await verify(recorded, gen, tag)
  if (got === null) return
  const confirmed = got.findings.filter(isConfirmed)
  entry.confirmed = confirmed.map(f => f.id)
  await expand(confirmed, got.hunks, gen, h.id)
}

// ---- run the plan ----
await parallel(A.hunters.map(h => () => chain(h, 1, null)))

// Safety net: a hunter may have recorded findings it did not report. Judge whatever nobody judged.
let snap = await snapshot('ledger-sweep')
if (snap !== null) {
  const orphans = snap.findings.filter(f => !f.verdict && !f.resolution && !claimed.has(f.id)).map(f => f.id)
  if (orphans.length) {
    const got = await verify(orphans, 1, 'orphans')
    if (got !== null) {
      const confirmed = got.findings.filter(isConfirmed)
      trail.push({ id: 'orphans', gen: 1, parent: null, recorded: orphans.length, confirmed: confirmed.map(f => f.id) })
      await expand(confirmed, got.hunks, 1, 'orphans')
    }
    snap = await snapshot('ledger-final')
  }
}

// ---- compact summary ----
const by = { proven: [], probable: [], question: [], dismissed: [], unjudged: [] }
if (snap !== null) for (const f of snap.findings) (Object.hasOwn(by, f.verdict) ? by[f.verdict] : by.unjudged).push(f.id)
const n = k => by[k].length
return {
  generations: lastGen,
  hunters: { launched: trail.filter(t => t.id !== 'orphans').length, failed: failedHunters.length },
  counts: { findings: snap === null ? null : snap.findings.length, proven: n('proven'), probable: n('probable'), question: n('question'), dismissed: n('dismissed'), unjudged: n('unjudged') },
  findings: by,
  uncoveredHunks: snap === null ? null : snap.missing,
  failedHunters,
  problems,
  trail,
}
