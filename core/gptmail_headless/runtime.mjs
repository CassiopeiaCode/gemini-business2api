import vm from 'node:vm'
import { ProxyAgent } from 'undici'

class EventTargetLike {
  constructor() { this._listeners = new Map() }
  addEventListener(type, handler) {
    if (!handler) return
    if (!this._listeners.has(type)) this._listeners.set(type, new Set())
    this._listeners.get(type).add(handler)
  }
  removeEventListener(type, handler) { this._listeners.get(type)?.delete(handler) }
  dispatchEvent(event) {
    if (!event?.type) return true
    event.target = event.target || this
    event.currentTarget = this
    for (const handler of this._listeners.get(event.type) || []) handler.call(this, event)
    const direct = this[`on${event.type}`]
    if (typeof direct === 'function') direct.call(this, event)
    return !event.defaultPrevented
  }
}

class StorageLike {
  constructor(seed = {}) { this.map = new Map(Object.entries(seed)) }
  getItem(key) { return this.map.has(key) ? this.map.get(key) : null }
  setItem(key, value) { this.map.set(String(key), String(value)) }
  removeItem(key) { this.map.delete(String(key)) }
  clear() { this.map.clear() }
}

class ClassList {
  constructor(element) { this.element = element; this.set = new Set() }
  add(...tokens) { tokens.forEach((token) => this.set.add(token)); this._sync() }
  remove(...tokens) { tokens.forEach((token) => this.set.delete(token)); this._sync() }
  toggle(token) { if (this.set.has(token)) this.set.delete(token); else this.set.add(token); this._sync(); return this.set.has(token) }
  contains(token) { return this.set.has(token) }
  _sync() { this.element.attributes.class = [...this.set].join(' ') }
}

class ElementLike extends EventTargetLike {
  constructor(tagName, ownerDocument) {
    super()
    this.tagName = tagName.toUpperCase()
    this.ownerDocument = ownerDocument
    this.children = []
    this.parentNode = null
    this.attributes = {}
    this.style = {}
    this.dataset = {}
    this.classList = new ClassList(this)
    this._textContent = ''
    this._innerHTML = ''
    this.value = ''
    this.disabled = false
    this.onclick = null
    if (this.tagName === 'IFRAME') {
      this.contentWindow = { document: new FrameDocumentLike(ownerDocument?.defaultView) }
    }
  }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child }
  removeChild(child) { const index = this.children.indexOf(child); if (index >= 0) this.children.splice(index, 1); child.parentNode = null; return child }
  setAttribute(name, value) {
    this.attributes[name] = String(value)
    if (name === 'id') this.ownerDocument?._registerId(this)
    if (name === 'class') this.classList.set = new Set(String(value).split(/\s+/).filter(Boolean))
    if (name.startsWith('data-')) this.dataset[toCamel(name.slice(5))] = String(value)
  }
  getAttribute(name) { return this.attributes[name] ?? null }
  get id() { return this.attributes.id ?? '' }
  set id(value) { this.setAttribute('id', value) }
  get innerText() { return this.textContent }
  set innerText(value) { this.textContent = value }
  get textContent() { return this._textContent }
  set textContent(value) { this._textContent = String(value); this._innerHTML = this._textContent }
  get innerHTML() { return this._innerHTML }
  set innerHTML(value) { this._innerHTML = String(value); this.children = []; populateChildrenFromHtml(this.ownerDocument, this, this._innerHTML) }
  get className() { return this.attributes.class ?? '' }
  set className(value) { this.setAttribute('class', value) }
  querySelector(selector) { return this.ownerDocument?.querySelector(selector, this) ?? null }
  querySelectorAll(selector) { return this.ownerDocument?.querySelectorAll(selector, this) ?? [] }
  focus() {}
  select() {}
  remove() { this.parentNode?.removeChild(this) }
}

class FrameDocumentLike {
  constructor(defaultView) { this.defaultView = defaultView; this.body = new ElementLike('body', null); this._buffer = '' }
  open() { this._buffer = ''; this.body.innerHTML = '' }
  write(html) { this._buffer += String(html) }
  close() { this.body.innerHTML = this._buffer }
}

class DocumentLike extends EventTargetLike {
  constructor(url) {
    super(); this.url = new URL(url); this.readyState = 'loading'; this.title = ''; this.defaultView = null
    this.head = new ElementLike('head', this); this.body = new ElementLike('body', this)
    this._idMap = new Map(); this._allElements = [this.head, this.body]; this._writeBuffer = ''
  }
  _registerId(element) { if (element.id) this._idMap.set(element.id, element); if (!this._allElements.includes(element)) this._allElements.push(element) }
  createElement(tagName) { const element = new ElementLike(tagName, this); this._allElements.push(element); return element }
  open() { this._writeBuffer = ''; this.body.innerHTML = '' }
  write(html) { this._writeBuffer += String(html) }
  close() { this.body.innerHTML = this._writeBuffer }
  getElementById(id) { return this._idMap.get(id) ?? null }
  get cookie() { return this.defaultView?.documentCookie ?? '' }
  set cookie(value) { this.defaultView.documentCookie = value }
  querySelector(selector, root = null) { return this.querySelectorAll(selector, root)[0] ?? null }
  querySelectorAll(selector, root = null) {
    const scope = root ? collectElements(root) : this._allElements
    if (selector.startsWith('#')) { const item = this.getElementById(selector.slice(1)); return item ? [item] : [] }
    if (selector.startsWith('.')) { const cls = selector.slice(1); return scope.filter((element) => element.classList?.contains(cls)) }
    const attrMatch = selector.match(/^([a-zA-Z0-9-]+)?\[([^=]+)="([^"]*)"\]$/)
    if (attrMatch) {
      const [, tagName, attr, value] = attrMatch; const tag = tagName ? tagName.toUpperCase() : null
      return scope.filter((element) => (!tag || element.tagName === tag) && String(element.getAttribute(attr) ?? '') === value)
    }
    const tag = selector.toUpperCase(); return scope.filter((element) => element.tagName === tag)
  }
}

function collectElements(root) { const output = []; const walk = (node) => { output.push(node); for (const child of node.children || []) walk(child) }; walk(root); return output }

class CookieJar {
  constructor(seed = {}) { this.store = new Map(Object.entries(seed)) }
  setFromHeader(setCookie) { if (!setCookie) return; const [pair] = setCookie.split(';'); const eqIndex = pair.indexOf('='); if (eqIndex <= 0) return; this.store.set(pair.slice(0, eqIndex).trim(), pair.slice(eqIndex + 1).trim()) }
  setFromDocumentCookie(cookieValue) { this.setFromHeader(cookieValue) }
  header() { return [...this.store.entries()].map(([key, value]) => `${key}=${value}`).join('; ') }
  toObject() { return Object.fromEntries(this.store.entries()) }
}

export class LightweightPageRuntime {
  constructor({ baseUrl, logger = console, localStorageSeed = {}, sessionStorageSeed = {}, cookieSeed = {}, proxyUrl = '' } = {}) {
    const envProxyUrl = process.env.HTTPS_PROXY || process.env.HTTP_PROXY || process.env.https_proxy || process.env.http_proxy || ''
    this.baseUrl = baseUrl; this.logger = logger; this.proxyUrl = proxyUrl || envProxyUrl || ''; this.proxyAgent = this.proxyUrl ? new ProxyAgent(this.proxyUrl) : null; this.cookieJar = new CookieJar(cookieSeed)
    this.localStorage = new StorageLike(localStorageSeed); this.sessionStorage = new StorageLike(sessionStorageSeed)
    this.networkLog = []; this.intervalHandles = new Set(); this.timeoutHandles = new Set(); this.document = null; this.window = null; this.context = null; this.scripts = []
  }
  async load(url, options = {}) {
    const target = new URL(url, this.baseUrl).toString(); this.logger.info?.(`[runtime] load ${target}`)
    if (this.proxyUrl) this.logger.info?.(`[runtime] proxy ${this.proxyUrl}`)
    const startedAt = Date.now()
    const response = await fetch(target, this._withProxy({ headers: this._buildHeaders(), redirect: 'follow', ...options })); this._captureCookies(response)
    this.logger.info?.(`[runtime] load response ${response.status} ${response.url} (${Date.now() - startedAt}ms)`)
    const html = await response.text(); this.document = new DocumentLike(target); this.window = this._createWindow(target); this.document.defaultView = this.window; this.context = vm.createContext(this.window)
    this.logger.info?.(`[runtime] html length ${html.length}`)
    this._parseHtml(html); await this._executeScripts(target); this.document.readyState = 'interactive'; this.document.dispatchEvent({ type: 'DOMContentLoaded' }); this.window.dispatchEvent({ type: 'DOMContentLoaded' }); this.document.readyState = 'complete'
    return { html, url: target }
  }
  _createWindow(url) {
    const runtime = this; const location = new URL(url); location.replace = (next) => runtime._setLocation(next)
    const history = { pushState(_state, _title, next) { if (next) runtime._setLocation(next); runtime.window.dispatchEvent({ type: 'popstate' }) }, replaceState(_state, _title, next) { if (next) runtime._setLocation(next) } }
    const windowObject = new EventTargetLike()
    Object.assign(windowObject, {
      window: null, self: null, globalThis: null, document: this.document,
      navigator: { userAgent: 'gptmail-headless/0.1', language: 'en-US', clipboard: { writeText: async () => {} } },
      get documentCookie() { return runtime.cookieJar.header() }, set documentCookie(value) { runtime.cookieJar.setFromDocumentCookie(value) },
      location, history, localStorage: this.localStorage, sessionStorage: this.sessionStorage, console: this.logger, Headers, URL, URLSearchParams,
      setTimeout(fn, delay = 0, ...args) { const handle = setTimeout(() => fn(...args), delay); runtime.timeoutHandles.add(handle); return handle },
      clearTimeout(handle) { clearTimeout(handle); runtime.timeoutHandles.delete(handle) },
      setInterval(fn, delay = 0, ...args) { const handle = setInterval(() => fn(...args), delay); runtime.intervalHandles.add(handle); return handle },
      clearInterval(handle) { clearInterval(handle); runtime.intervalHandles.delete(handle) },
      requestAnimationFrame(fn) { return setTimeout(() => fn(Date.now()), 16) }, cancelAnimationFrame(handle) { clearTimeout(handle) },
      alert(message) { runtime.logger.info?.(`[alert] ${message}`) }, confirm(message) { runtime.logger.info?.(`[confirm] ${message}`); return true },
      fetch: (...args) => runtime._fetch(...args), Event: class { constructor(type) { this.type = type } }, Node: ElementLike,
    })
    windowObject.window = windowObject; windowObject.self = windowObject; windowObject.globalThis = windowObject; return windowObject
  }
  _setLocation(next) { const location = new URL(next, this.window.location.href); location.replace = (value) => this._setLocation(value); this.window.location = location; this.document.url = location }
  _buildHeaders(extra = {}) { const headers = { ...extra }; const cookie = this.cookieJar.header(); if (cookie) headers.cookie = cookie; return headers }
  _captureCookies(response) { const setCookie = response.headers.get('set-cookie'); if (setCookie) this.cookieJar.setFromHeader(setCookie) }
  async _fetch(input, init = {}) {
    const url = new URL(typeof input === 'string' ? input : input.url, this.window.location.href).toString(); const headers = new Headers(init.headers || {}); const cookie = this.cookieJar.header()
    if (cookie && (init.credentials === 'include' || init.credentials === 'same-origin' || !init.credentials)) headers.set('cookie', cookie)
    const requestInfo = { url, method: init.method || 'GET', headers: Object.fromEntries(headers.entries()) }; this.logger.info?.(`[fetch] start ${requestInfo.method} ${url}`)
    const startedAt = Date.now()
    const response = await fetch(url, this._withProxy({ ...init, headers })); this._captureCookies(response); this.networkLog.push({ request: requestInfo, response: { status: response.status, url: response.url } }); this.logger.info?.(`[fetch] done ${requestInfo.method} ${url} -> ${response.status} (${Date.now() - startedAt}ms)`); return response
  }
  _withProxy(options = {}) { return this.proxyAgent ? { ...options, dispatcher: this.proxyAgent } : options }
  _parseHtml(html) {
    this.scripts = []; const titleMatch = html.match(/<title>([\s\S]*?)<\/title>/i); if (titleMatch) this.document.title = decodeHtml(titleMatch[1].trim())
    const idRegex = /<([a-zA-Z0-9-]+)([^>]*\sid=["']([^"']+)["'][^>]*)>/g; let idMatch
    while ((idMatch = idRegex.exec(html))) { const element = this.document.createElement(idMatch[1]); element.id = idMatch[3]; if (element.tagName === 'IFRAME') element.contentWindow = { document: new FrameDocumentLike(this.window) }; this.document.body.appendChild(element) }
    const scriptRegex = /<script([^>]*)>([\s\S]*?)<\/script>/gi; let scriptMatch
    while ((scriptMatch = scriptRegex.exec(html))) {
      const attrs = scriptMatch[1] || ''; const srcMatch = attrs.match(/src=["']([^"']+)["']/i); const typeMatch = attrs.match(/type=["']([^"']+)["']/i)
      this.scripts.push({ src: srcMatch?.[1] ?? null, code: scriptMatch[2] ?? '', type: typeMatch?.[1]?.toLowerCase() ?? 'text/javascript' })
    }
    this._ensureStubNodes()
  }
  _ensureStubNodes() {
    const ids = ['emailDisplay','copyFeedback','customPrefix','customPrefixContainer','emailModal','modalSubject','modalFrom','modalAvatar','modalTime','modalTo','emailFrame','emailList','refreshCountdown','refreshText','domainRequestForm','domainInput','domainFeedback','domainCount','copyrightYear','langText','inbox-title','refreshSpinner']
    for (const id of ids) if (!this.document.getElementById(id)) { const tag = id === 'emailFrame' ? 'iframe' : 'div'; const element = this.document.createElement(tag); element.id = id; this.document.body.appendChild(element) }
  }
  async _executeScripts(baseUrl) {
    for (const script of this.scripts) {
      if (!['', 'text/javascript', 'application/javascript', 'module'].includes(script.type)) continue
      let code = script.code
      if (script.src) {
        const resolved = new URL(script.src, baseUrl).toString(); if (new URL(resolved).origin !== new URL(baseUrl).origin) { this.logger.info?.(`[runtime] skip third-party script ${resolved}`); continue }
        this.logger.info?.(`[runtime] load script ${resolved}`); const response = await this._fetch(resolved); code = await response.text()
      }
      if (!code.trim()) continue
      const label = script.src ? new URL(script.src, baseUrl).toString() : '[inline-script]'
      this.logger.info?.(`[runtime] exec script start ${label}`)
      const startedAt = Date.now()
      vm.runInContext(code, this.context, { timeout: 10000 })
      this.logger.info?.(`[runtime] exec script done ${label} (${Date.now() - startedAt}ms)`)
    }
  }
  call(functionName, ...args) { const target = this.window[functionName]; if (typeof target !== 'function') throw new Error(`Function not found: ${functionName}`); this.logger.info?.('[runtime] call start', { functionName, args }); const result = target.apply(this.window, args); if (result && typeof result.then === 'function') return result.then((value) => { this.logger.info?.('[runtime] call done', { functionName, async: true }); return value }).catch((error) => { this.logger.error?.('[runtime] call failed', { functionName, error: error?.message || String(error) }); throw error }) ; this.logger.info?.('[runtime] call done', { functionName, async: false }); return result }
  getNodeText(id) { return this.document.getElementById(id)?.textContent ?? '' }
  getIframeContent() { return this.document.getElementById('emailFrame')?.contentWindow?.document?.body?.innerHTML ?? '' }
  snapshot() {
    const auth = this.window?.__BROWSER_AUTH ?? null
    return { url: this.window?.location?.href ?? '', title: this.document?.title ?? '', auth, localStorage: Object.fromEntries(this.localStorage.map.entries()), sessionStorage: Object.fromEntries(this.sessionStorage.map.entries()), cookies: this.cookieJar.toObject(), lastNetwork: this.networkLog.at(-1) ?? null, networkCount: this.networkLog.length }
  }
  destroy() { for (const handle of this.intervalHandles) clearInterval(handle); for (const handle of this.timeoutHandles) clearTimeout(handle); this.intervalHandles.clear(); this.timeoutHandles.clear(); this.proxyAgent?.close?.() }
}

function populateChildrenFromHtml(document, parent, html) {
  if (!document || !html) return
  const tagRegex = /<([a-zA-Z0-9-]+)([^>]*)>([\s\S]*?)<\/\1>|<([a-zA-Z0-9-]+)([^>]*)\/>/g; let match
  while ((match = tagRegex.exec(html))) {
    const tagName = match[1] || match[4]; const attrs = match[2] || match[5] || ''; const inner = match[3] || ''; const child = document.createElement(tagName); applyAttributes(child, attrs); parent.appendChild(child)
    if (inner && !inner.includes('<')) child.textContent = stripTags(inner); else if (inner) child.innerHTML = inner
  }
}

function applyAttributes(element, attrs) { const attrRegex = /([a-zA-Z0-9:-]+)(?:=["']([^"']*)["'])?/g; let attrMatch; while ((attrMatch = attrRegex.exec(attrs))) { const [, name, value = ''] = attrMatch; element.setAttribute(name, value) } }
function stripTags(value) { return String(value).replace(/<[^>]+>/g, '') }
function toCamel(value) { return value.replace(/-([a-z])/g, (_, char) => char.toUpperCase()) }
function decodeHtml(input) { return input.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').replace(/&quot;/g, '"').replace(/&#39;/g, "'") }
