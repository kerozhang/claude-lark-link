import assert from 'node:assert/strict'
import { createHash, createHmac } from 'node:crypto'
import { mock, test } from 'node:test'
import { register } from '../plugins/claude-lark-link/hooks/register.ts'

const session = '0e634d98-127f-4cba-8379-c2ad7c3fff00'
const ticket = '0'.repeat(64)
const code = '0'.repeat(16)

mock.method(crypto.subtle, 'digest', async (algorithm, body) => {
  assert.equal(algorithm, 'SHA-256')
  return Uint8Array.from(createHash('sha256').update(body).digest()).buffer
})

function mount(mode, responder, { desktop = true, managed = false, drop = false } = {}) {
  const hooks = {}
  const calls = []
  const tasks = []
  const changes = []
  let currentSession = session
  let pulse
  let model = 'sonnet'
  const engine = {
    fs: { read: async (path) => path.endsWith('/config.json') ? '{}' : `CLAUDE_LARK_HOOK_TOKEN=${ticket}` },
    http: { fetch: async (url, request) => {
      assert.equal(url, 'http://127.0.0.1:8765/api/v1/inbox')
      const mac = value => createHmac('sha256', Buffer.from(ticket, 'hex')).update(value).digest('hex')
      const signature = mac(`inbox:${request.body}`)
      assert.equal(request.headers['X-Claude-Lark-Signature'], signature)
      const body = JSON.parse(request.body)
      calls.push(body)
      const result = JSON.stringify(responder(body) ?? {})
      return { ok: true, text: JSON.stringify({ result, proof: mac(`inbox-result:${signature}${result}`) }) }
    } },
    session: {
      id: async () => currentSession,
      cwd: async () => '/work',
      surfaces: async () => desktop ? ['desktop'] : ['terminal'],
      model: async () => model,
    },
    env: { get: async name => name === 'HOME' ? '/test-home' : name === 'CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST' && managed ? '1' : undefined },
    command: { register: async () => {}, run: async ({ args }) => {
      changes.push(args)
      model = args
      return { text: 'Set model to opus for this session only' }
    } },
    config: { list: async () => [{ key: 'model', options: ['sonnet', 'opus'] }] },
    clock: { every: (_ms, callback) => {
      pulse = callback
      return { cancel: () => { pulse = undefined } }
    } },
    prompt: { submit: async ({ text }) => {
      tasks.push(text)
      return drop ? { drop: 'native refusal detail' } : { text }
    } },
  }
  register((name, matcher, handler) => { hooks[name] = handler ?? matcher }, { connection_mode: mode })
  return {
    calls, tasks, changes,
    open: () => hooks['session.start'](engine, { isInteractive: !desktop }, async () => ({})),
    close: () => hooks['session.end'](engine, {}, async () => ({})),
    pair: (kind = 'composer') => hooks['command.run'](engine, { args: '', origin: { kind } }),
    tick: async () => { pulse?.(); await new Promise(resolve => setImmediate(resolve)) },
    switchSession: (next) => { currentSession = next },
  }
}

test('manual mode requires a composer command, then routes a task', async () => {
  const app = mount('manual', ({ op }) => op === 'pair' ? { code, ticket } : { connected: true, task: 'Check the README' })
  await app.open()
  await app.tick()
  await app.pair('plugin')
  assert.equal(app.calls.length, 0)
  assert.match((await app.pair()).text, /\/bind/)
  await app.tick()
  assert.deepEqual(app.tasks, ['[Feishu / Lark task]:\nCheck the README'])
  await app.close()
  await app.tick()
  assert.equal(app.calls.length, 2)
})

test('auto retries after lost connection but pauses until manually resumed', async () => {
  let tries = 0
  const app = mount('auto', ({ op }) => {
    if (op === 'resume') return { ticket }
    if (op === 'auto') return ++tries === 1 ? { ticket } : { paused: true }
    return { connected: false }
  })
  await app.open()
  for (let i = 0; i < 6; i++) await app.tick()
  assert.deepEqual(app.calls.map(row => row.op), ['auto', 'poll', 'auto'])
  assert.match((await app.pair()).text, /已登记/)
  assert.equal(app.calls.at(-1).op, 'resume')
})

test('auto mode does not attach a terminal session', async () => {
  const app = mount('auto', () => ({ ticket }), { desktop: false })
  await app.open()
  await app.tick()
  await app.pair()
  assert.deepEqual(app.calls, [])
})

test('gateway-managed models never invoke the CLI model switch', async () => {
  const id = '1'.repeat(32)
  let asked = false
  const app = mount('manual', ({ op }) => {
    if (op === 'pair') return { ticket, code }
    if (asked) return { connected: true }
    asked = true
    return { connected: true, model_request: { request: id, model: 'opus' } }
  }, { managed: true })
  await app.open()
  await app.pair()
  await app.tick()
  await app.tick()
  assert.deepEqual(app.changes, [])
  assert.deepEqual(app.calls.at(-1).model_response, { request: id, model: 'sonnet', options: [], outcome: 'host_managed' })
})

test('reports a dropped prompt once without exposing the native reason', async () => {
  const deliveryId = 'f'.repeat(32)
  const app = mount('manual', ({ op }) => op === 'pair' ? { code, ticket }
    : { connected: true, task: 'Check the README', delivery_id: deliveryId }, { drop: true })
  await app.open()
  await app.pair('sdk')
  await app.tick()
  assert.deepEqual(app.calls.map(body => body.op), ['pair', 'poll', 'reject'])
  assert.equal(app.calls.at(-1).delivery_id, deliveryId)
  assert.ok(!JSON.stringify(app.calls).includes('native refusal detail'))
})

test('switching session invalidates the old manual ticket', async () => {
  const app = mount('manual', ({ op }) => op === 'pair' ? { code, ticket } : { connected: true, task: 'stale' })
  await app.open()
  await app.pair()
  app.switchSession('113f385d-2b3c-44ef-8872-2135cd58b890')
  await app.tick()
  await app.tick()
  assert.deepEqual(app.tasks, [])
  assert.deepEqual(app.calls.map(body => body.op), ['pair'])
})
