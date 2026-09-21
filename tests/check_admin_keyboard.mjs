#!/usr/bin/env node
/**
 * S12 — keyboard-access pass on the admin page.
 *
 * Presses Tab in a real browser and asserts two things the plan asks for: every
 * interactive control in the Content Packs view is reachable, and every focused
 * control is visibly focused.
 *
 * It drives Chrome over the DevTools protocol using only Node built-ins (global
 * `fetch` and `WebSocket`) — no puppeteer to install and keep current. It never
 * touches the owner's app: it is a browser pointed at the gateway's own page.
 *
 * Usage:
 *   node tests/check_admin_keyboard.mjs
 *   ADMIN_URL=http://127.0.0.1:8100/admin#packs node tests/check_admin_keyboard.mjs
 */
import { spawn } from 'node:child_process'
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const TARGET = process.env.ADMIN_URL || 'http://127.0.0.1:8100/admin#packs'
const CHROME = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
const TAB_PRESSES = 90

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

class Devtools {
  constructor(socket) {
    this.socket = socket
    this.nextId = 0
    this.pending = new Map()
    this.listeners = new Map()
    socket.onmessage = event => {
      const message = JSON.parse(event.data)
      if (message.id) {
        const pending = this.pending.get(message.id)
        if (!pending) return
        this.pending.delete(message.id)
        message.error ? pending.reject(new Error(message.error.message)) : pending.resolve(message.result)
        return
      }
      for (const listener of this.listeners.get(message.method) ?? []) listener(message.params)
    }
  }

  static async connect(url) {
    const socket = new WebSocket(url)
    await new Promise((resolve, reject) => {
      socket.onopen = resolve
      socket.onerror = () => reject(new Error('could not connect to Chrome'))
    })
    return new Devtools(socket)
  }

  send(method, params = {}) {
    const id = ++this.nextId
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }

  once(method) {
    return new Promise(resolve => {
      const listeners = this.listeners.get(method) ?? []
      const handler = params => {
        this.listeners.set(method, listeners.filter(entry => entry !== handler))
        resolve(params)
      }
      listeners.push(handler)
      this.listeners.set(method, listeners)
    })
  }

  async evaluate(expression) {
    const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
    return result.result?.value
  }

  async pressTab() {
    const key = { key: 'Tab', code: 'Tab', windowsVirtualKeyCode: 9, nativeVirtualKeyCode: 9 }
    await this.send('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...key })
    await this.send('Input.dispatchKeyEvent', { type: 'keyUp', ...key })
  }

  close() { this.socket.close() }
}

const CONTROLS = `(() => {
  const view = document.getElementById('view-packs')
  if (!view) return null
  return [...view.querySelectorAll('button,input,select,textarea')]
    .filter(node => node.getClientRects().length && !node.disabled)
    .map(node => ({
      id: node.id || null,
      tab: node.dataset && node.dataset.tab ? node.dataset.tab : null,
      tag: node.tagName,
      text: (node.textContent || '').trim().slice(0, 40),
    }))
})()`

const FOCUSED = `(() => {
  const node = document.activeElement
  if (!node || node === document.body) return null
  const style = getComputedStyle(node)
  return {
    id: node.id || null,
    tab: node.dataset && node.dataset.tab ? node.dataset.tab : null,
    tag: node.tagName,
    text: (node.textContent || '').trim().slice(0, 40),
    outlineWidth: style.outlineWidth,
    outlineStyle: style.outlineStyle,
  }
})()`

const keyOf = control => control.id || (control.tab ? `tab:${control.tab}` : `${control.tag}:${control.text}`)

async function waitForPort(profile) {
  const marker = join(profile, 'DevToolsActivePort')
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (existsSync(marker)) {
      const [port] = readFileSync(marker, 'utf8').split('\n')
      if (port) return Number(port)
    }
    await sleep(100)
  }
  throw new Error('Chrome never opened its debugging port')
}

async function main() {
  if (!existsSync(CHROME)) throw new Error(`Chrome is not at ${CHROME}`)
  const profile = mkdtempSync(join(tmpdir(), 'kh-keyboard-'))
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--disable-background-networking', `--user-data-dir=${profile}`,
    '--remote-debugging-port=0', '--window-size=1280,900', 'about:blank',
  ], { stdio: 'ignore' })
  let devtools
  try {
    const port = await waitForPort(profile)
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()
    const page = targets.find(target => target.type === 'page')
    devtools = await Devtools.connect(page.webSocketDebuggerUrl)
    await devtools.send('Page.enable')
    await devtools.send('Runtime.enable')
    const loaded = devtools.once('Page.loadEventFired')
    await devtools.send('Page.navigate', { url: TARGET })
    await loaded
    await sleep(1200) // let the page render its first data fetch

    const controls = await devtools.evaluate(CONTROLS)
    if (!controls) throw new Error('the Content Packs view did not render')
    if (!controls.length) throw new Error('no interactive control found in the Content Packs view')

    const reached = new Map()
    for (let press = 0; press < TAB_PRESSES; press += 1) {
      await devtools.pressTab()
      const focused = await devtools.evaluate(FOCUSED)
      if (!focused) continue
      const key = keyOf(focused)
      if (!reached.has(key)) reached.set(key, focused)
    }

    const missing = controls.filter(control => !reached.has(keyOf(control)))
    const unlight = [...reached.values()].filter(focused =>
      focused.outlineStyle === 'none' || parseFloat(focused.outlineWidth) === 0)

    console.log(`controls in view: ${controls.length}`)
    console.log(`reached by Tab:   ${reached.size}`)
    if (missing.length) {
      console.log('NOT REACHABLE:')
      for (const control of missing) console.log(`   ${keyOf(control)} ${control.tag} "${control.text}"`)
    }
    if (unlight.length) {
      console.log('FOCUSED BUT NOT VISIBLY:')
      for (const focused of unlight) console.log(`   ${keyOf(focused)} ${focused.tag} outline=${focused.outlineWidth} ${focused.outlineStyle}`)
    }
    if (missing.length || unlight.length) {
      console.log(`FAIL ${missing.length} unreachable, ${unlight.length} without a visible focus ring`)
      return 1
    }
    console.log(`PASS every control reachable and visibly focused (${TARGET})`)
    return 0
  } finally {
    devtools?.close()
    chrome.kill('SIGKILL')
    // Chrome keeps writing its profile for a moment after the signal; removing the
    // directory too early throws ENOTEMPTY and would turn a pass into a failure.
    await sleep(400)
    try { rmSync(profile, { recursive: true, force: true }) } catch { /* best effort */ }
  }
}

main().then(code => { process.exitCode = code }).catch(error => {
  console.error(`FAIL ${error.message}`)
  process.exitCode = 1
})
