#!/usr/bin/env node
// tests/mock_controller_release.mjs
//
// A contract-enforcing, in-memory stand-in for a Studio Hub *controller* that
// receives signed app releases from GatewayKH. The real fleet is switched off,
// so this mock is the authority on the wire contract:
//
//   * canonical JSON: sorted keys, no whitespace, UTF-8;
//   * gateway signature: Ed25519 over the canonical descriptor minus "signature";
//   * publisher_key_id: sha256 of the raw 32-byte Ed25519 public key (64 hex);
//   * controller_signature: Ed25519 over the canonical *stored* descriptor minus
//     only "controller_signature", so it binds the gateway signature and the
//     controller's own controller_key_id.
//
// The descriptor has exactly eleven fields and carries NO key material:
//   schema, schema_version, version, build, filename, bytes, sha256,
//   published_at, publisher_id, publisher_key_id, signature
// A public key inside the descriptor would defeat the point: a peer could supply
// its own key next to its own signature. Trust comes from the key the owner
// *installed*: POST /api/hub/release/trust stores the raw public key (and its
// fingerprint) in <state>/trusted.json, and the signature is verified against
// that stored key only -- never against anything in the request body. A
// fingerprint-only trust is stored but cannot verify a signature, so such a push
// is refused at the signature step with a 400 that says the controller needs the
// gateway's public key installed.
//
// The REFUSAL ORDER is the point of this file, not an accident:
//
//   1. shape         400  -- cheap, and the detail names the offending field;
//   2. trust         409  -- an untrusted controller says so before it does crypto;
//   3. key identity  400  -- publisher_key_id must be the trusted fingerprint;
//   4. signature     400  -- only now is the signature worth verifying at all,
//                            against the key the owner installed;
//   5. artifact      400  -- DECODE, then LENGTH, then HASH. Length is checked
//                            before the hash because a length mismatch is exact
//                            and cheap, while hashing a differently-sized payload
//                            would report a confusing "hash mismatch" for what is
//                            really a truncation.
//
// CLI:
//   node tests/mock_controller_release.mjs --state <dir> [--port 0]
//        [--fleet-token <token>] [--hub-token <token>]
//
// On startup it loads or generates <state>/controller-key.pem (PKCS8 PEM, 0600),
// binds 127.0.0.1 (never all interfaces: the tokens are the only door), and
// prints exactly one line of JSON to stdout:
//   {"port": <n>, "fingerprint": "<64 hex>", "public_key": "<64 hex>"}
// Everything else goes to stderr. SIGTERM/SIGINT exit 0.

import { createHash, createPrivateKey, createPublicKey, generateKeyPairSync, sign, verify } from 'node:crypto'
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const SCHEMA = 'studiohub.app-release.v1'
const SCHEMA_VERSION = 1

// Step 1 required fields. Every one is a non-empty string, `build` included: the
// gateway builds the descriptor in Python with `build: str` (a stamp such as
// `abc1234`), so a numeric build is a different producer, not a second spelling.
const REQUIRED_STRING_FIELDS = [
  'version', 'build', 'filename', 'sha256', 'publisher_id', 'publisher_key_id', 'signature',
]

// ponytail: one flat 512 MiB ceiling read into memory. The ceiling exists only
// so a runaway upload cannot take the mock down; raise it (or stream to disk)
// when a real release DMG approaches it.
const MAX_BODY_BYTES = 512 * 1024 * 1024

const ED25519_SPKI_PREFIX = Buffer.from('302a300506032b6570032100', 'hex')

class RequestError extends Error {
  constructor(status, detail) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

const sha256hex = value => createHash('sha256').update(value).digest('hex')

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

// Canonical JSON: objects with keys sorted, no insignificant whitespace, UTF-8.
// Arrays keep order; every field in this contract is an ASCII scalar.
function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`
  if (isPlainObject(value)) {
    const body = Object.keys(value).sort()
      .map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(',')
    return `{${body}}`
  }
  const encoded = JSON.stringify(value)
  if (encoded === undefined) throw new Error('descriptor is not canonical JSON')
  return encoded
}

// Mirror gateway/content_signing.mjs: export SPKI DER (44 bytes for Ed25519) and
// take bytes 12..44 as the raw public key.
function rawPublicKeyOf(privateKey) {
  const der = createPublicKey(privateKey).export({ type: 'spki', format: 'der' })
  if (der.byteLength !== 44) throw new Error('controller key is not Ed25519')
  return der.subarray(12)
}

function publicKeyObjectFromRaw(raw) {
  return createPublicKey({ key: Buffer.concat([ED25519_SPKI_PREFIX, raw]), format: 'der', type: 'spki' })
}

// Accept a raw Ed25519 public key as 64 hex or standard base64 (32 bytes).
function rawPublicKeyFromField(value) {
  if (typeof value !== 'string' || value.length === 0) return null
  if (/^[0-9a-fA-F]{64}$/.test(value)) return Buffer.from(value, 'hex')
  const decoded = Buffer.from(value, 'base64')
  if (decoded.length === 32 && decoded.toString('base64') === value) return decoded
  return null
}

// Strict standard base64 with padding: a round trip rejects unknown characters,
// missing padding and non-canonical trailing bits. Buffer.from alone is lenient.
function decodeBase64Strict(text) {
  if (typeof text !== 'string') return null
  if (text.length % 4 !== 0) return null
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(text)) return null
  const decoded = Buffer.from(text, 'base64')
  if (decoded.toString('base64') !== text) return null
  return decoded
}

// Single `bytes=` range only. Returns {start, end} (inclusive) or null when the
// range is unsatisfiable / unsupported, which the caller answers 416.
// ponytail: multipart/byteranges is the upgrade path; a release download only
// ever needs one range.
function parseRange(header, total) {
  const match = /^bytes=(.+)$/.exec(String(header).trim())
  if (!match) return null
  const spec = match[1].trim()
  if (spec.includes(',')) return null
  const parts = /^(\d*)-(\d*)$/.exec(spec)
  if (!parts) return null
  const [, startText, endText] = parts
  if (startText === '' && endText === '') return null
  if (startText === '') {
    const suffix = Number(endText)
    if (!Number.isSafeInteger(suffix) || suffix <= 0 || total === 0) return null
    return { start: Math.max(0, total - suffix), end: total - 1 }
  }
  const start = Number(startText)
  if (!Number.isSafeInteger(start) || total === 0 || start >= total) return null
  let end
  if (endText === '') end = total - 1
  else {
    end = Number(endText)
    if (!Number.isSafeInteger(end) || start > end) return null
  }
  return { start, end: Math.min(end, total - 1) }
}

function loadOrCreateControllerKey(stateDir) {
  const keyPath = resolve(stateDir, 'controller-key.pem')
  let pem
  if (existsSync(keyPath)) {
    pem = readFileSync(keyPath)
    // Re-assert 0600: the contract says the controller's own key is private.
    chmodSync(keyPath, 0o600)
  } else {
    const { privateKey } = generateKeyPairSync('ed25519')
    pem = privateKey.export({ type: 'pkcs8', format: 'pem' })
    writeFileSync(keyPath, pem, { mode: 0o600, flag: 'wx' })
    chmodSync(keyPath, 0o600)
  }
  const privateKey = createPrivateKey(pem)
  if (privateKey.asymmetricKeyType !== 'ed25519') {
    throw new Error(`${keyPath} is not an Ed25519 PKCS8 private key`)
  }
  const rawPublicKey = rawPublicKeyOf(privateKey)
  return { privateKey, rawPublicKey, fingerprint: sha256hex(rawPublicKey) }
}

// Absent file = no key trusted. A malformed file fails closed (no trust), never
// open: an unreadable trusted.json must not silently trust anything. The public
// key is what makes a signature verifiable; a fingerprint-only file is trusted
// but cannot verify, which the push route reports at the signature step.
function loadTrust(stateDir) {
  const trustedPath = resolve(stateDir, 'trusted.json')
  try {
    const value = JSON.parse(readFileSync(trustedPath, 'utf8'))
    if (isPlainObject(value) && typeof value.fingerprint === 'string'
      && /^[0-9a-fA-F]{64}$/.test(value.fingerprint)) {
      const fingerprint = value.fingerprint.toLowerCase()
      let publicKey = null
      if (value.public_key !== undefined) {
        const raw = rawPublicKeyFromField(value.public_key)
        if (raw && sha256hex(raw) === fingerprint) publicKey = raw
        else process.stderr.write('mock-controller: trusted.json public_key does not match its fingerprint; ignoring the key\n')
      }
      return { fingerprint, publicKey }
    }
    process.stderr.write('mock-controller: trusted.json has no valid fingerprint; treating as no trusted key\n')
  } catch (error) {
    if (error?.code !== 'ENOENT') {
      process.stderr.write(`mock-controller: trusted.json is unreadable (${error.message}); treating as no trusted key\n`)
    }
  }
  return { fingerprint: null, publicKey: null }
}

function persistTrust(stateDir, { fingerprint, publicKey }) {
  const target = resolve(stateDir, 'trusted.json')
  const tmp = resolve(stateDir, `.trusted.json.${process.pid}.tmp`)
  const value = publicKey ? { fingerprint, public_key: publicKey.toString('hex') } : { fingerprint }
  writeFileSync(tmp, JSON.stringify(value), { mode: 0o600 })
  renameSync(tmp, target)
}

function readBody(req) {
  return new Promise((resolveBody, rejectBody) => {
    let length = 0
    let tooLarge = false
    const chunks = []
    req.on('data', chunk => {
      length += chunk.length
      if (tooLarge) return
      if (length > MAX_BODY_BYTES) {
        tooLarge = true
        chunks.length = 0
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => tooLarge
      ? rejectBody(Object.assign(new Error('request body too large'), { code: 'TOO_LARGE' }))
      : resolveBody(Buffer.concat(chunks)))
    req.on('error', rejectBody)
  })
}

function sendJson(res, status, payload, extraHeaders = {}) {
  const body = Buffer.from(JSON.stringify(payload), 'utf8')
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    ...extraHeaders,
  })
  res.end(body)
}

export async function startController({
  stateDir,
  port = 0,
  fleetToken = 'fleet-token',
  hubToken = 'hub-token',
} = {}) {
  if (typeof stateDir !== 'string' || stateDir.length === 0) throw new Error('stateDir is required')
  const state = resolve(stateDir)
  mkdirSync(state, { recursive: true })

  const controllerKey = loadOrCreateControllerKey(state)
  const controller = {
    privateKey: controllerKey.privateKey,
    publicKey: controllerKey.rawPublicKey.toString('hex'),
    fingerprint: controllerKey.fingerprint,
  }

  // Mutable controller state, one closure per server. A fresh server trusts
  // nothing and stores nothing, even when it reuses an on-disk key.
  let trusted = loadTrust(state)
  let storedRelease = null

  async function handleTrust(req, res) {
    const body = await parseJsonBody(req, res)
    if (body === undefined) return
    const encoded = body.public_key ?? body.public_key_base64
    let fingerprint
    let publicKey = null
    if (encoded !== undefined) {
      const raw = rawPublicKeyFromField(encoded)
      if (!raw) {
        throw new RequestError(400, 'public_key must be the raw 32-byte Ed25519 public key as 64 hex, or standard base64 in public_key_base64')
      }
      publicKey = raw
      fingerprint = sha256hex(raw)
      if (body.fingerprint !== undefined) {
        if (typeof body.fingerprint !== 'string' || !/^[0-9a-fA-F]{64}$/.test(body.fingerprint)
          || body.fingerprint.toLowerCase() !== fingerprint) {
          throw new RequestError(400, `fingerprint ${JSON.stringify(body.fingerprint)} does not match the installed public key ${fingerprint}`)
        }
      }
    } else if (body.fingerprint !== undefined) {
      if (typeof body.fingerprint !== 'string' || !/^[0-9a-fA-F]{64}$/.test(body.fingerprint)) {
        throw new RequestError(400, 'fingerprint must be 64 hex characters')
      }
      fingerprint = body.fingerprint.toLowerCase()
      // A fingerprint is enough to trust but not to verify. Keep a matching key
      // that is already installed, or re-installing by fingerprint would silently
      // disarm signature verification for a key that is still the right one.
      if (trusted.publicKey && sha256hex(trusted.publicKey) === fingerprint) {
        publicKey = trusted.publicKey
      }
    } else {
      throw new RequestError(400, 'provide public_key (64 hex), public_key_base64, or fingerprint (64 hex)')
    }
    trusted = { fingerprint, publicKey }
    persistTrust(state, trusted)
    sendJson(res, 200, { trusted: fingerprint })
  }

  async function handlePush(req, res) {
    const body = await parseJsonBody(req, res)
    if (body === undefined) return
    const descriptor = body.descriptor

    // Step 1 -- shape. Detail names the field so the gateway can report it.
    if (!isPlainObject(descriptor)) throw new RequestError(400, 'descriptor must be an object')
    if (descriptor.schema !== SCHEMA) {
      throw new RequestError(400, `descriptor.schema must be "${SCHEMA}"`)
    }
    if (descriptor.schema_version !== SCHEMA_VERSION) {
      throw new RequestError(400, 'descriptor.schema_version must be 1')
    }
    for (const field of REQUIRED_STRING_FIELDS) {
      if (typeof descriptor[field] !== 'string' || descriptor[field].length === 0) {
        throw new RequestError(400, `descriptor.${field} is required and must be a non-empty string`)
      }
    }

    // Step 2 -- trust, before any crypto. An untrusted controller must say so
    // even when the signature would also fail, because "install my key" is the
    // actionable sentence and "signature invalid" is not.
    if (!trusted.fingerprint) {
      throw new RequestError(409, "no trusted gateway key is installed on this controller; install the gateway's publisher fingerprint with POST /api/hub/release/trust")
    }

    // Step 3 -- key identity. publisher_key_id is the trust anchor, so it is
    // compared before the signature. Case-insensitive by contract.
    const publisherKeyId = descriptor.publisher_key_id.toLowerCase()
    if (publisherKeyId !== trusted.fingerprint) {
      throw new RequestError(400, `publisher_key_id ${descriptor.publisher_key_id} does not match the trusted gateway fingerprint ${trusted.fingerprint}`)
    }

    // Step 4 -- signature, against the key the owner installed. Nothing in the
    // request body can supply key material; a trusted fingerprint without a
    // stored key is a refusal here, not a 409 -- a key *is* trusted, it just
    // cannot verify until the public half is installed.
    const { signature, ...signedPart } = descriptor
    if (!trusted.publicKey) {
      throw new RequestError(400, `the controller trusts fingerprint ${trusted.fingerprint} but holds no gateway public key, so it cannot verify descriptor.signature; install the gateway's public key with POST /api/hub/release/trust {"public_key": "<64 hex>"}`)
    }
    let signatureValid = false
    try {
      signatureValid = verify(
        null,
        Buffer.from(canonicalJson(signedPart), 'utf8'),
        publicKeyObjectFromRaw(trusted.publicKey),
        Buffer.from(signature, 'base64'),
      )
    } catch {
      signatureValid = false
    }
    if (!signatureValid) {
      throw new RequestError(400, `descriptor.signature does not verify over the canonical descriptor for publisher_key_id ${descriptor.publisher_key_id}`)
    }

    // Step 5 -- artifact: decode, then length, then hash.
    const artifact = decodeBase64Strict(body.artifact_base64)
    if (!artifact) {
      throw new RequestError(400, 'artifact_base64 is not valid standard base64 with padding')
    }
    if (descriptor.bytes !== artifact.length) {
      throw new RequestError(400, `artifact length ${artifact.length} does not match descriptor.bytes ${JSON.stringify(descriptor.bytes)}`)
    }
    const artifactHash = sha256hex(artifact)
    if (artifactHash !== descriptor.sha256.toLowerCase()) {
      throw new RequestError(400, `artifact sha256 ${artifactHash} does not match descriptor.sha256 ${descriptor.sha256}`)
    }

    // Store the gateway's descriptor verbatim, then bind it with the
    // controller's own identity. controller_signature covers controller_key_id
    // and the gateway signature (only controller_signature is removed).
    const storedDescriptor = { ...descriptor, controller_key_id: controller.fingerprint }
    storedDescriptor.controller_signature = sign(
      null,
      Buffer.from(canonicalJson(storedDescriptor), 'utf8'),
      controller.privateKey,
    ).toString('base64')
    storedRelease = { descriptor: storedDescriptor, artifact }
    // The POST body IS the stored descriptor (the addendum's contract); the
    // `{"release": ...}` envelope belongs to GET, which can also say null.
    sendJson(res, 200, storedDescriptor)
  }

  function handleGetRelease(req, res) {
    // Never a stub: null until something is stored, the stored descriptor after.
    // `trusted_publisher` mirrors Hub 0.6.51: null when nothing is trusted, the
    // installed fingerprint otherwise (with the stored release's publisher_id).
    sendJson(res, 200, {
      release: storedRelease ? storedRelease.descriptor : null,
      trusted_publisher: trusted.fingerprint
        ? {
            publisher_id: storedRelease?.descriptor?.publisher_id ?? null,
            fingerprint: trusted.fingerprint,
          }
        : null,
    })
  }

  function handleIdentity(req, res) {
    sendJson(res, 200, {
      algorithm: 'ed25519',
      public_key: controller.publicKey,
      fingerprint: controller.fingerprint,
    })
  }

  function handleGetArtifact(req, res) {
    if (!storedRelease) throw new RequestError(404, 'no release artifact is stored on this controller')
    const { artifact } = storedRelease
    const hash = sha256hex(artifact)
    const rangeHeader = req.headers.range
    if (rangeHeader === undefined) {
      res.writeHead(200, {
        'Content-Type': 'application/octet-stream',
        'Content-Length': artifact.length,
        'X-Content-SHA256': hash,
        'Accept-Ranges': 'bytes',
      })
      res.end(artifact)
      return
    }
    const range = parseRange(rangeHeader, artifact.length)
    if (!range) {
      sendJson(res, 416, { detail: `range ${rangeHeader} is not satisfiable for an artifact of ${artifact.length} bytes` },
        { 'Content-Range': `bytes */${artifact.length}` })
      return
    }
    const slice = artifact.subarray(range.start, range.end + 1)
    res.writeHead(206, {
      'Content-Type': 'application/octet-stream',
      'Content-Length': slice.length,
      'Content-Range': `bytes ${range.start}-${range.end}/${artifact.length}`,
      'X-Content-SHA256': hash,
      'Accept-Ranges': 'bytes',
    })
    res.end(slice)
  }

  async function parseJsonBody(req, res) {
    let raw
    try {
      raw = await readBody(req)
    } catch (error) {
      if (error?.code === 'TOO_LARGE') {
        sendJson(res, 413, { detail: `request body exceeds ${MAX_BODY_BYTES} bytes` })
      } else {
        sendJson(res, 400, { detail: 'request body could not be read' })
      }
      return undefined
    }
    let value
    try {
      value = JSON.parse(raw.toString('utf8'))
    } catch {
      sendJson(res, 400, { detail: 'request body must be valid JSON' })
      return undefined
    }
    if (!isPlainObject(value)) {
      sendJson(res, 400, { detail: 'request body must be a JSON object' })
      return undefined
    }
    return value
  }

  function routeFor(method, pathname) {
    if (pathname === '/api/hub/release/trust') {
      return { scope: 'hub', methods: ['POST'], handler: handleTrust }
    }
    if (pathname === '/api/hub/release/artifact') {
      return { scope: 'fleet', methods: ['GET'], handler: handleGetArtifact }
    }
    if (pathname === '/api/hub/release/identity') {
      return { scope: 'fleet', methods: ['GET'], handler: handleIdentity }
    }
    if (pathname === '/api/hub/release') {
      return { scope: 'fleet', methods: ['GET', 'POST'], handler: method === 'POST' ? handlePush : handleGetRelease }
    }
    return null
  }

  function authorize(scope, token, res) {
    if (scope === 'hub') {
      if (token === hubToken) return true
      // The real Hub refuses administration to a Fleet credential: 403, not 401.
      if (token === fleetToken) {
        sendJson(res, 403, { detail: 'the Fleet token cannot administer this controller; the Hub operator token is required' })
        return false
      }
      sendJson(res, 401, { detail: 'a valid Hub token is required' })
      return false
    }
    if (token === fleetToken) return true
    sendJson(res, 401, { detail: 'a valid Fleet token is required' })
    return false
  }

  async function dispatch(req, res) {
    const pathname = new URL(req.url, 'http://127.0.0.1').pathname
    const route = routeFor(req.method, pathname)
    if (!route) {
      sendJson(res, 404, { detail: `no route for ${req.method} ${pathname}` })
      return
    }
    // Every known route demands its header before anything else, including
    // before the method check: a missing token is 401, not 405.
    const token = req.headers['x-hub-token']
    if (!authorize(route.scope, typeof token === 'string' ? token : undefined, res)) return
    if (!route.methods.includes(req.method)) {
      sendJson(res, 405, { detail: `${req.method} is not allowed on ${pathname}` })
      return
    }
    await route.handler(req, res)
  }

  const server = createServer((req, res) => {
    dispatch(req, res).catch(error => {
      if (res.headersSent) {
        res.destroy()
        return
      }
      if (error instanceof RequestError) sendJson(res, error.status, { detail: error.detail })
      else {
        process.stderr.write(`mock-controller: request failed: ${error?.stack ?? error}\n`)
        sendJson(res, 500, { detail: 'the mock controller failed to handle the request' })
      }
    })
  })

  await new Promise((resolveListen, rejectListen) => {
    const onError = error => rejectListen(error)
    server.once('error', onError)
    // 127.0.0.1 deliberately: the fleet tokens are the only door.
    server.listen(port, '127.0.0.1', () => {
      server.off('error', onError)
      resolveListen()
    })
  })

  const actualPort = server.address().port
  return {
    server,
    port: actualPort,
    fingerprint: controller.fingerprint,
    publicKey: controller.publicKey,
    stateDir: state,
    close: () => new Promise(resolveClose => {
      server.closeAllConnections?.()
      server.close(() => resolveClose())
    }),
  }
}

function parseArgs(argv) {
  const options = { stateDir: null, port: 0, fleetToken: 'fleet-token', hubToken: 'hub-token' }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    const value = () => {
      index += 1
      if (index >= argv.length) throw new Error(`${arg} requires a value`)
      return argv[index]
    }
    if (arg === '--state') options.stateDir = value()
    else if (arg === '--port') {
      const port = Number(value())
      if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error('--port must be an integer from 0 to 65535')
      options.port = port
    } else if (arg === '--fleet-token') options.fleetToken = value()
    else if (arg === '--hub-token') options.hubToken = value()
    else throw new Error(`unknown argument: ${arg}`)
  }
  if (!options.stateDir) throw new Error('usage: mock_controller_release.mjs --state <dir> [--port 0] [--fleet-token <token>] [--hub-token <token>]')
  return options
}

async function main() {
  const controller = await startController(parseArgs(process.argv.slice(2)))
  // One line of JSON on stdout, and only this one line.
  process.stdout.write(`${JSON.stringify({
    port: controller.port,
    fingerprint: controller.fingerprint,
    public_key: controller.publicKey,
  })}\n`)
  const shutdown = () => {
    controller.close().then(() => process.exit(0), () => process.exit(0))
  }
  process.on('SIGTERM', shutdown)
  process.on('SIGINT', shutdown)
}

const invokedDirectly = process.argv[1] !== undefined
  && resolve(process.argv[1]) === fileURLToPath(import.meta.url)

if (invokedDirectly) {
  // Top-level await is available in an ES module.
  await main().catch(error => {
    process.stderr.write(`mock-controller: ${error?.message ?? error}\n`)
    process.exit(1)
  })
}
