import { expect, test } from 'claude-code/testing'

test('registers the local slash command without enrolling in manual mode', async ($, on) => {
  const commands: string[] = []
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('command.register', (_, e) => {
    commands.push(e.name)
    return { value: { command: e.name } }
  })
  on('env.get', () => ({ value: '/test-home' }))
  on('fs.read', () => ({ value: '{}' }))
  await $.session.start({ cwd: '/', surface: 'desktop', isInteractive: false })
  expect(commands).toEqual(['lark-link'])
})

test('checks Desktop, signs the enrollment, and rejects an unsigned response', async ($, on) => {
  const urls: string[] = []
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('session.id', () => ({ value: '0e634d98-127f-4cba-8379-c2ad7c3fff00' }))
  on('session.cwd', () => ({ value: '/workspace' }))
  on('env.get', () => ({ value: '/test-home' }))
  on('fs.read', (_, e) => ({ value: e.path.endsWith('config.json') ? '{}' : `CLAUDE_LARK_HOOK_TOKEN=${'0'.repeat(64)}` }))
  on('http.fetch', (_, e) => {
    urls.push(e.url)
    expect(e.init?.headers?.['X-Claude-Lark-Signature']).toMatch(/^[0-9a-f]{64}$/)
    return { value: { status: 200, ok: true, headers: {}, text: '{"result":"{}","proof":"invalid"}' } }
  })
  await $.session.start({ cwd: '/workspace', surface: 'desktop', isInteractive: false })
  const bundle = await $.command.run({ command: 'lark-link', args: '', origin: { kind: 'sdk' },
    presentation: { columns: 80, isFullscreen: false } })
  expect(bundle.text).toMatch(/桥接未响应/)
  expect(urls).toEqual(['http://127.0.0.1:8765/api/v1/inbox'])
})

test('uses an explicit local auto setting for the connection mode', async ($, on) => {
  const operations: string[] = []
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('command.register', (_, e) => ({ value: { command: e.name } }))
  on('session.surfaces', () => ({ value: ['desktop'] }))
  on('session.id', () => ({ value: '0e634d98-127f-4cba-8379-c2ad7c3fff00' }))
  on('session.cwd', () => ({ value: '/workspace' }))
  on('env.get', () => ({ value: '/test-home' }))
  on('fs.read', (_, e) => ({ value: e.path.endsWith('config.json')
    ? '{"desktop_connection_mode":"auto"}' : `CLAUDE_LARK_HOOK_TOKEN=${'0'.repeat(64)}` }))
  on('http.fetch', (_, e) => {
    const body = e.init?.body
    if (typeof body === 'string') operations.push(JSON.parse(body).op)
    return { value: { status: 200, ok: true, headers: {}, text: '{}' } }
  })
  await $.session.start({ cwd: '/workspace', surface: 'desktop', isInteractive: false })
  await $.command.run({ command: 'lark-link', args: '', origin: { kind: 'sdk' },
    presentation: { columns: 80, isFullscreen: false } })
  expect(operations).toEqual(['resume'])
})
