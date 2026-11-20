import type { EngineInterface, Register } from 'claude-code'

const COMMAND = 'lark-link'
const TASK_PREFIX = '[Feishu / Lark task]:\n'
const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}(?:\[1m\])?$/
const TICKET = /^[0-9a-f]{64}$/

function modelId(value: unknown): value is string {
  return typeof value === 'string' && MODEL_ID.test(value) && !/\s/.test(value)
}

type ModelRequest = { request: string; model: string }
type ModelResponse = { request: string; model: string; options: string[]; outcome: string }
type Reply = {
  ticket?: string
  code?: string
  paused?: boolean
  connected?: boolean
  reason?: string
  task?: string
  delivery_id?: string
  model_request?: ModelRequest
}
type State = {
  automatic: boolean
  timer?: { cancel(): void }
  epoch: number
  ticket: string
  halted: boolean
  session: string
  cwd: string
  inFlight: boolean
  submitting: boolean
  modelResponse?: ModelResponse
}

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
}

async function hmac(key: Uint8Array, message: string): Promise<string> {
  // The module sandbox exposes SHA-256 but not SubtleCrypto.sign.
  const body = new TextEncoder().encode(message)
  const inner = new Uint8Array(64 + body.length).fill(0x36)
  const outer = new Uint8Array(96).fill(0x5c)
  key.forEach((byte, index) => {
    inner[index] = 0x36 ^ byte
    outer[index] = 0x5c ^ byte
  })
  inner.set(body, 64)
  outer.set(new Uint8Array(await crypto.subtle.digest('SHA-256', inner)), 64)
  return hex(new Uint8Array(await crypto.subtle.digest('SHA-256', outer)))
}

async function configDirectory($: EngineInterface): Promise<string | undefined> {
  const home = await $.env.get('USERPROFILE') || await $.env.get('HOME')
  if (home && /^(?:[A-Za-z]:[\\/]|\/(?!\/))/.test(home)) return `${home}/.claude-lark-link`
}

async function readConfig($: EngineInterface, directory: string) {
  return JSON.parse((await $.fs.read(`${directory}/config.json`)).replace(/^﻿/, ''))
}

// The bridge config is the only source of the connection mode.
async function modeOverride($: EngineInterface): Promise<'manual' | 'auto' | undefined> {
  try {
    const directory = await configDirectory($)
    if (!directory) return
    const mode = (await readConfig($, directory)).desktop_connection_mode
    if (mode === 'manual' || mode === 'auto') return mode
  } catch {
    // Fall back to the plugin setting when the local file is absent or invalid.
  }
}

async function request($: EngineInterface, state: State, payload: object): Promise<Reply | undefined> {
  const directory = await configDirectory($)
  if (!directory) return
  const config = await readConfig($, directory)
  const port = config.port ?? 8765
  const prefix = config.api_prefix ?? '/api/v1'
  if (!Number.isInteger(port) || port < 1 || port > 65535 || typeof prefix !== 'string' ||
      !/^(\/[A-Za-z0-9_-]+)+$/.test(prefix) || /\s/.test(prefix)) return
  const env = await $.fs.read(`${directory}/plugin/.env`)
  const row = env.split(/\r?\n/).filter(line => /^\s*CLAUDE_LARK_HOOK_TOKEN\s*=/.test(line)).at(-1)
  const token = row?.split('=')[1]?.trim()
  if (!token || !TICKET.test(token)) return
  const key = Uint8Array.from(token.match(/../g) ?? [], byte => Number.parseInt(byte, 16))
  const body = JSON.stringify({ ...payload, session: state.session, cwd: state.cwd,
    at: Math.floor(Date.now() / 1000), nonce: hex(crypto.getRandomValues(new Uint8Array(16))) })
  const signature = await hmac(key, `inbox:${body}`)
  const response = await $.http.fetch(`http://127.0.0.1:${port}${prefix}/inbox`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Claude-Lark-Signature': signature }, body,
  })
  if (!response.ok || response.text.length > 48000) return
  const reply = JSON.parse(response.text)
  if (typeof reply.result !== 'string' || typeof reply.proof !== 'string') return
  if (reply.proof !== await hmac(key, `inbox-result:${signature}${reply.result}`)) return
  return JSON.parse(reply.result)
}

async function changeModel($: EngineInterface, change: ModelRequest, active: () => Promise<boolean>) {
  const result: ModelResponse = { request: change.request, model: '', options: [], outcome: 'failed' }
  try {
    const before = await $.session.model()
    if (modelId(before)) result.model = before
    let hostManaged: string | undefined
    try {
      hostManaged = await $.env.get('CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST')
    } catch {
      result.outcome = 'host_managed'
      return result
    }
    if (hostManaged === '1') {
      result.outcome = 'host_managed'
      return result
    }
    if (!change.model) {
      if (!result.model) return result
      const config = (await $.config.list()).find(row => row.key === 'model')
      result.options = [...new Set((config?.options ?? []).filter(modelId))].slice(0, 8)
      result.outcome = 'current'
    } else if (await active()) {
      if (change.model === before) {
        result.outcome = 'unchanged'
      } else {
        const reply = await $.command.run({ command: 'model', args: change.model })
        if (!await active()) return result
        const after = await $.session.model()
        if (modelId(after)) {
          result.model = after
          const confirmed = reply?.text?.includes('for this session only') && reply.text.includes('Set model to ')
          if (after === change.model || confirmed) result.outcome = before === after ? 'unchanged' : 'changed'
        }
      }
    }
  } catch {
    // The host's errors can include gateway data.
  }
  return result
}

function startTimer($: EngineInterface, state: State) {
  if (state.timer) return
  state.timer = $.clock.every(2000, () => {
    if (state.inFlight || state.halted) return
    state.inFlight = true
    const version = state.epoch
    void (async () => {
      try {
        const session = await $.session.id()
        const cwd = await $.session.cwd()
        const active = async () => state.epoch === version && await $.session.id() === session &&
          await $.session.cwd() === cwd && state.epoch === version
        if (state.epoch !== version) return
        if (state.session !== session || state.cwd !== cwd) {
          state.session = session
          state.cwd = cwd
          state.epoch++
          state.ticket = ''
          state.modelResponse = undefined
          state.submitting = false
          return
        }
        if (!state.ticket) {
          if (!state.automatic || !(await $.session.surfaces()).includes('desktop')) return
          const enrolled = await request($, state, { op: 'auto' })
          if (!await active()) return
          if (enrolled?.paused) state.halted = true
          if (enrolled?.ticket && TICKET.test(enrolled.ticket)) state.ticket = enrolled.ticket
          return
        }
        const reporting = state.modelResponse
        const response = await request($, state, {
          op: 'poll', ticket: state.ticket, ready: !state.submitting, model_control: true, model_response: reporting,
        })
        if (!await active()) return
        if (response?.connected === false) {
          state.ticket = ''
          state.modelResponse = undefined
          return
        }
        if (response?.connected !== true) return
        if (state.modelResponse === reporting) state.modelResponse = undefined
        const change = response.model_request
        if (!state.submitting && change && /^[0-9a-f]{32}$/.test(change.request) && (change.model === '' || modelId(change.model))) {
          state.submitting = true
          void changeModel($, change, active).then(async result => {
            if (await active()) state.modelResponse = result
          }).catch(() => {}).finally(() => { if (state.epoch === version) state.submitting = false })
        } else if (!state.submitting && typeof response.task === 'string' && response.task.trim() && response.task.length <= 6000) {
          state.submitting = true
          try {
            const result = $.prompt.submit({ text: TASK_PREFIX + response.task, asUser: true })
            void result.then(async entry => {
              if (entry?.drop !== undefined && response.delivery_id && /^[0-9a-f]{32}$/.test(response.delivery_id) && await active()) {
                await request($, state, { op: 'reject', ticket: state.ticket, delivery_id: response.delivery_id })
              }
            }).catch(() => {}).finally(() => { if (state.epoch === version) state.submitting = false })
          } catch {
            state.submitting = false
          }
        }
      } catch {
        // A task whose receipt is uncertain is never submitted twice.
      } finally {
        state.inFlight = false
      }
    })()
  })
}

export const register: Register = (on) => {
  const state: State = {
    automatic: false, epoch: 0, ticket: '', halted: false,
    session: '', cwd: '', inFlight: false, submitting: false,
  }
  let interactive = false
  on('session.start', async ($, e, next) => {
    interactive = e.isInteractive
    await $.command.register({ name: COMMAND, description: '将当前 Claude Desktop 会话连接到飞书私聊' })
    const result = await next(e)
    if (!interactive) {
      state.automatic = (await modeOverride($)) === 'auto'
      if (state.automatic) startTimer($, state)
    }
    return result
  })

  on('session.end', (_, e, next) => {
    state.epoch++
    state.timer?.cancel()
    state.timer = undefined
    state.ticket = ''
    return next(e)
  })

  on('command.run', { command: COMMAND }, async ($, e) => {
    if (e.origin.kind !== 'composer' && e.origin.kind !== 'sdk') return { text: '请在 Claude Desktop 的输入框手动执行此命令。' }
    if (e.args.trim()) return { text: '用法：/lark-link' }
    if (interactive || !(await $.session.surfaces()).includes('desktop')) return { text: '请在 Claude Desktop 的 Code 会话中运行。' }
    const version = ++state.epoch
    state.halted = true
    state.ticket = ''
    state.modelResponse = undefined
    state.submitting = false
    try {
      const session = await $.session.id()
      const cwd = await $.session.cwd()
      if (state.epoch !== version) return { text: '有新的连接请求。' }
      state.session = session
      state.cwd = cwd
      const enrolled = await request($, state, { op: state.automatic ? 'resume' : 'pair' })
      if (await $.session.id() !== session || await $.session.cwd() !== cwd || state.epoch !== version) {
        return { text: '会话已变更，请在目标会话重试。' }
      }
      if (enrolled?.reason === 'workspace') return { text: '当前工作区未列入桥接配置的 workspaces。' }
      if (enrolled?.reason === 'auto_disabled') return { text: '桥接未开启自动连接。可改用手动配对。' }
      if (!enrolled?.ticket || !TICKET.test(enrolled.ticket)) return { text: '桥接未响应，请确认本机服务、Python 和配置可用。' }
      state.ticket = enrolled.ticket
      startTimer($, state)
      if (state.automatic) return { text: '已登记当前会话。请在机器人私聊发送 /sessions 查看。' }
      if (enrolled.code && /^[0-9a-f]{16}$/.test(enrolled.code)) {
        return { text: `请在两分钟内向机器人私聊发送 /bind ${enrolled.code}。` }
      }
      return { text: '配对码未生成，请重试。' }
    } catch {
      return { text: '无法连接桥接。请确认本机服务和配置可用。' }
    } finally {
      if (state.epoch === version) state.halted = false
    }
  })
}
