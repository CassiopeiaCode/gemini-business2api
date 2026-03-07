import { LightweightPageRuntime } from './runtime.mjs'

function sleep(ms) { return new Promise((resolve) => setTimeout(resolve, ms)) }

export class GptMailHeadlessClient {
  constructor({ origin = 'https://mail.chatgpt.org.uk', logger = console, localStorageSeed = {}, sessionStorageSeed = {}, cookieSeed = {}, proxyUrl = '' } = {}) {
    this.origin = origin.replace(/\/$/, '')
    this.logger = logger
    this.runtime = new LightweightPageRuntime({ baseUrl: this.origin, logger, localStorageSeed, sessionStorageSeed, cookieSeed, proxyUrl })
  }

  async loadPage(path = '/zh/') {
    this.logger.info?.('[client] loadPage start', { path })
    const result = await this.runtime.load(new URL(path, this.origin).toString())
    this.logger.info?.('[client] loadPage done', { path, url: result?.url || '', htmlLength: (result?.html || '').length })
    return result
  }
  async waitForStable(delayMs = 300) {
    this.logger.info?.('[client] waitForStable start', { delayMs })
    await sleep(delayMs)
    this.logger.info?.('[client] waitForStable done', { delayMs })
  }
  getCurrentAuth() { return this.runtime.window?.__BROWSER_AUTH ?? null }
  getCurrentEmail() { return this.getCurrentAuth()?.email || this.runtime.localStorage.getItem('gptmail_address') || '' }

  syncState({ email = '', auth = null } = {}) {
    if (email) this.runtime.localStorage.setItem('gptmail_address', email)
    if (auth && this.runtime.window?.syncBrowserAuthFromResponse) {
      this.runtime.window.syncBrowserAuthFromResponse({ auth })
    }
  }

  async generateEmail(prefix = '', domain = '') {
    this.logger.info?.('[client] generateEmail start', { prefix, domain, currentEmail: this.getCurrentEmail() })
    await this.runtime.call('generateNewEmail', prefix, domain)
    await this.waitForStable(300)
    const result = { success: Boolean(this.getCurrentEmail()), data: { email: this.getCurrentEmail() }, auth: this.getCurrentAuth() }
    this.logger.info?.('[client] generateEmail done', result)
    return result
  }

  async refreshAuth(targetEmail = '') {
    const email = targetEmail || this.getCurrentEmail()
    this.logger.info?.('[client] refreshAuth start', { email })
    if (typeof this.runtime.window?.ensureBrowserAuth === 'function') {
      const auth = await this.runtime.call('ensureBrowserAuth', email)
      this.logger.info?.('[client] refreshAuth done', { hasAuth: Boolean(auth) })
      return auth
    }
    const auth = this.getCurrentAuth()
    this.logger.info?.('[client] refreshAuth noop', { hasAuth: Boolean(auth) })
    return auth
  }

  async fetchEmails() {
    const email = this.getCurrentEmail()
    this.logger.info?.('[client] fetchEmails start', { email })
    const response = await this.runtime.call('browserAuthFetch', `/api/emails?email=${encodeURIComponent(email)}`, {}, email)
    const payload = await response.json()
    this.logger.info?.('[client] fetchEmails done', { email, emailCount: Array.isArray(payload?.data?.emails) ? payload.data.emails.length : 0 })
    return payload
  }

  async fetchEmailDetail(id) {
    this.logger.info?.('[client] fetchEmailDetail start', { id, email: this.getCurrentEmail() })
    const response = await this.runtime.call('browserAuthFetch', `/api/email/${id}`, {}, this.getCurrentEmail())
    const payload = await response.json()
    const frameDoc = this.runtime.document.getElementById('emailFrame')?.contentWindow?.document
    if (frameDoc) {
      frameDoc.open(); frameDoc.write(payload?.data?.html || payload?.data?.content || payload?.html || ''); frameDoc.close()
    }
    this.logger.info?.('[client] fetchEmailDetail done', { id, hasData: Boolean(payload?.data) })
    return payload
  }

  snapshot() { return { ...this.runtime.snapshot(), currentEmail: this.getCurrentEmail(), iframeHtml: this.runtime.getIframeContent() } }
  destroy() { this.runtime.destroy() }
}
