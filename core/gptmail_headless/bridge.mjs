import { GptMailHeadlessClient } from './client.mjs'

function createLogger() {
  const format = (value) => {
    if (value instanceof Error) return value.stack || value.message || String(value)
    if (typeof value === 'string') return value
    if (value === undefined) return 'undefined'
    if (value === null) return 'null'
    try {
      return JSON.stringify(value)
    } catch {
      return String(value)
    }
  }

  const write = (...args) => {
    process.stderr.write(`${args.map(format).join(' ')}\n`)
  }

  return {
    info(...args) { write(...args) },
    error(...args) { write(...args) },
    warn(...args) { write(...args) },
    log(...args) { write(...args) },
  }
}

async function readStdinJson() {
  const chunks = []
  for await (const chunk of process.stdin) chunks.push(chunk)
  const raw = Buffer.concat(chunks).toString('utf8').trim()
  return raw ? JSON.parse(raw) : {}
}

function seedClientState(client, state = {}) {
  const cookies = state.cookies && typeof state.cookies === 'object' ? state.cookies : {}
  for (const [name, value] of Object.entries(cookies)) {
    if (typeof name === 'string' && typeof value === 'string') {
      client.runtime.cookieJar.store.set(name, value)
    }
  }
  if (state.gm_sid && !client.runtime.cookieJar.store.has('gm_sid')) {
    client.runtime.cookieJar.store.set('gm_sid', state.gm_sid)
  }
  if (state.email) {
    client.runtime.localStorage.setItem('gptmail_address', state.email)
  }
  if (Array.isArray(state.read_emails)) {
    client.runtime.localStorage.setItem('gptmail_read_emails', JSON.stringify(state.read_emails))
  }
}

function syncAuthState(client, state = {}) {
  const auth = state.inbox_token
    ? {
        token: state.inbox_token,
        email: state.auth_email || state.email || '',
        expires_at: state.token_expires_at || 0,
      }
    : null
  if (auth && typeof client.runtime.window?.syncBrowserAuthFromResponse === 'function') {
    client.runtime.window.syncBrowserAuthFromResponse({ auth })
    return
  }
  if (auth) {
    client.runtime.window.__BROWSER_AUTH = auth
  }
}

function buildState(client, fallbackState = {}) {
  const auth = client.getCurrentAuth?.() || client.runtime.window?.__BROWSER_AUTH || null
  const currentEmail = client.getCurrentEmail?.() || client.runtime.localStorage.getItem('gptmail_address') || fallbackState.email || ''
  return {
    email: currentEmail,
    gm_sid: client.runtime.cookieJar.store.get('gm_sid') || fallbackState.gm_sid || '',
    inbox_token: auth?.token || fallbackState.inbox_token || '',
    token_expires_at: auth?.expires_at || auth?.expiresAt || fallbackState.token_expires_at || 0,
    auth_email: auth?.email || fallbackState.auth_email || '',
    cookies: Object.fromEntries(client.runtime.cookieJar.store.entries()),
    iframe_html: client.getIframeContent?.() || '',
  }
}

async function fetchEmailsWithDetails(client) {
  const payload = await client.fetchEmails()
  const emails = payload?.data?.emails
  if (!Array.isArray(emails) || emails.length === 0) {
    return payload
  }

  const enrichedEmails = []
  for (const email of emails) {
    if (!email?.id) {
      enrichedEmails.push(email)
      continue
    }
    try {
      const detailPayload = await client.fetchEmailDetail(email.id)
      const detail = detailPayload?.data
      enrichedEmails.push(detail && typeof detail === 'object' ? { ...email, ...detail } : email)
    } catch {
      enrichedEmails.push(email)
    }
  }

  return {
    ...payload,
    data: {
      ...(payload?.data || {}),
      emails: enrichedEmails,
    },
  }
}

async function main() {
  const payload = await readStdinJson()
  const logger = createLogger()
  const origin = (payload.origin || 'https://mail.chatgpt.org.uk').replace(/\/$/, '')
  const state = payload.state || {}
  const proxyUrl = state.proxy || payload.proxy || ''

  logger.info('[bridge] start', { action: payload.action || '', origin, path: payload.path || '/zh/', hasState: Boolean(state && Object.keys(state).length), proxyUrl })

  const client = new GptMailHeadlessClient({ origin, logger, proxyUrl })
  const path = payload.path || '/zh/'

  try {
    logger.info('[bridge] seed state')
    seedClientState(client, state)
    logger.info('[bridge] loadPage start', { path })
    await client.loadPage(path)
    logger.info('[bridge] loadPage done', client.snapshot())
    logger.info('[bridge] waitForStable start', { delayMs: 500 })
    await client.waitForStable(500)
    logger.info('[bridge] waitForStable done')
    logger.info('[bridge] syncAuthState start')
    syncAuthState(client, state)
    logger.info('[bridge] syncAuthState done', { currentEmail: client.getCurrentEmail?.() || '', hasAuth: Boolean(client.getCurrentAuth?.()) })

    let result = null
    switch (payload.action) {
      case 'warm_up':
        logger.info('[bridge] action warm_up start')
        result = { success: true }
        logger.info('[bridge] action warm_up done', result)
        break
      case 'register':
        logger.info('[bridge] action register start', { prefix: payload.prefix || '', domain: payload.domain || '' })
        result = await client.generateEmail(payload.prefix || '', payload.domain || '')
        logger.info('[bridge] action register done', result)
        if (!result?.success || !result?.data?.email) {
          const message = result?.error || 'headless register did not produce an email'
          throw new Error(message)
        }
        break
      case 'fetch_messages':
        logger.info('[bridge] action fetch_messages start')
        result = await fetchEmailsWithDetails(client)
        logger.info('[bridge] action fetch_messages done', { emailCount: Array.isArray(result?.data?.emails) ? result.data.emails.length : 0 })
        break
      case 'fetch_email_detail':
        logger.info('[bridge] action fetch_email_detail start', { emailId: payload.email_id || '' })
        result = await client.fetchEmailDetail(payload.email_id)
        logger.info('[bridge] action fetch_email_detail done', { hasData: Boolean(result?.data) })
        break
      case 'refresh_auth':
        logger.info('[bridge] action refresh_auth start', { email: payload.email || state.email || '' })
        result = await client.refreshAuth(payload.email || state.email || '')
        logger.info('[bridge] action refresh_auth done', { hasAuth: Boolean(result) })
        break
      default:
        throw new Error(`Unsupported action: ${payload.action}`)
    }

    const output = {
      ok: true,
      result,
      state: buildState(client, state),
    }
    logger.info('[bridge] success', { action: payload.action || '', email: output.state?.email || '', networkCount: client.snapshot()?.networkCount ?? null })
    process.stdout.write(JSON.stringify(output))
  } finally {
    logger.info('[bridge] destroy')
    client.destroy()
  }

  process.exit(0)
}

main().catch((error) => {
  process.stderr.write(`[bridge] fatal ${error?.stack || error?.message || String(error)}\n`)
  process.stdout.write(JSON.stringify({ ok: false, error: error.message || String(error) }))
  process.exit(1)
})
