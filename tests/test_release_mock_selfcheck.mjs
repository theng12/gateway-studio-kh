// tests/test_release_mock_selfcheck.mjs
//
// Proves that tests/mock_controller_release.mjs enforces the release wire
// contract: the refusal order, the encodings, the eleven-field descriptor (which
// carries no key material), trust coming only from the installed public key, and
// the controller's own countersignature. Run with:
//
//   node --test tests/test_release_mock_selfcheck.mjs
//
// The canonicaliser here is deliberately a *second* implementation (deep-sort
// then JSON.stringify) rather than the mock's own, so a bug in either is caught
// rather than shared. Temp state lives under os.tmpdir(), never in the repo.

import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { createHash, createPublicKey, generateKeyPairSync, sign, verify } from 'node:crypto'
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { startController } from './mock_controller_release.mjs'

const FLEET_TOKEN = 'fleet-token'
const HUB_TOKEN = 'hub-token'
const ED25519_SPKI_PREFIX = Buffer.from('302a300506032b6570032100', 'hex')

// The descriptor contract, exactly. A push carries only `signature` as crypto.
const ELEVEN_FIELDS = [
  'schema', 'schema_version', 'version', 'build', 'filename', 'bytes', 'sha256',
  'published_at', 'publisher_id', 'publisher_key_id', 'signature',
]

const sha256hex = value => createHash('sha256').update(value).digest('hex')

function sortDeep(value) {
  if (Array.isArray(value)) return value.map(sortDeep)
  if (value && typeof value === 'object') {
    const sorted = {}
    for (const key of Object.keys(value).sort()) sorted[key] = sortDeep(value[key])
    return sorted
  }
  return value
}

const canonical = value => JSON.stringify(sortDeep(value))

function publicKeyFromRaw(raw) {
  return createPublicKey({ key: Buffer.concat([ED25519_SPKI_PREFIX, raw]), format: 'der', type: 'spki' })
}

function rawPublicKeyOf(keyObject) {
  const publicKey = keyObject.type === 'public' ? keyObject : createPublicKey(keyObject)
  const der = publicKey.export({ type: 'spki', format: 'der' })
  assert.equal(der.length, 44, 'test key must be Ed25519')
  return der.subarray(12)
}

let controller
let stateDir
let base
let gateway
let gatewayRaw
let gatewayFingerprint
let artifact
let baseDescriptor
let storedDescriptor

function signDescriptor(descriptor, privateKey = gateway.privateKey) {
  const signature = sign(null, Buffer.from(canonical(descriptor), 'utf8'), privateKey).toString('base64')
  return { ...descriptor, signature }
}

const signed = overrides => signDescriptor({ ...baseDescriptor, ...overrides })

async function request(path, { method = 'GET', token = FLEET_TOKEN, body } = {}) {
  const headers = {}
  // `null` means "send no token header at all"; undefined would take the default.
  if (typeof token === 'string') headers['X-Hub-Token'] = token
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const response = await fetch(`${base}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  // Clone before reading so binary callers can still consume the body.
  const clone = response.clone()
  const text = await response.text()
  let parsed
  try {
    parsed = text.length === 0 ? undefined : JSON.parse(text)
  } catch {
    parsed = text
  }
  return { status: response.status, body: parsed, headers: response.headers, response: clone }
}

const push = (descriptor, artifactBase64, token = FLEET_TOKEN) => request('/api/hub/release', {
  method: 'POST',
  token,
  body: { descriptor, artifact_base64: artifactBase64 },
})

after(async () => {
  await controller?.close()
  if (stateDir) rmSync(stateDir, { recursive: true, force: true })
})

before(async () => {
  stateDir = mkdtempSync(join(tmpdir(), 'release-mock-selfcheck-'))
  controller = await startController({ stateDir, port: 0 })
  base = `http://127.0.0.1:${controller.port}`

  gateway = generateKeyPairSync('ed25519')
  gatewayRaw = rawPublicKeyOf(gateway.publicKey)
  gatewayFingerprint = sha256hex(gatewayRaw)

  artifact = Buffer.from('The quick brown fox jumps over the lazy dog. 0123456789', 'utf8')
  baseDescriptor = {
    schema: 'studiohub.app-release.v1',
    schema_version: 1,
    version: '0.2.234',
    build: 'abc1234',
    filename: 'gateway-kh-0.2.234.dmg',
    bytes: artifact.length,
    sha256: sha256hex(artifact),
    published_at: 1760000000,
    publisher_id: 'gatewaykh',
    publisher_key_id: gatewayFingerprint,
  }
})

test('identity publishes the controller own raw key and fingerprint', async () => {
  const identity = await request('/api/hub/release/identity')
  assert.equal(identity.status, 200)
  assert.equal(identity.body.algorithm, 'ed25519')
  assert.match(identity.body.public_key, /^[0-9a-f]{64}$/)
  assert.match(identity.body.fingerprint, /^[0-9a-f]{64}$/)
  assert.equal(identity.body.public_key, controller.publicKey)
  assert.equal(identity.body.fingerprint, controller.fingerprint)
  assert.equal(identity.body.fingerprint, sha256hex(Buffer.from(controller.publicKey, 'hex')))
})

test('nothing stored: release is null, no trusted publisher, and the artifact is 404', async () => {
  const release = await request('/api/hub/release')
  assert.equal(release.status, 200)
  assert.deepEqual(release.body, { release: null, trusted_publisher: null })
  const artifactResponse = await request('/api/hub/release/artifact')
  assert.equal(artifactResponse.status, 404)
})

test('a wrong or absent Fleet token on GET /release is 401', async () => {
  const wrong = await request('/api/hub/release', { token: 'not-the-fleet-token' })
  assert.equal(wrong.status, 401)
  assert.match(String(wrong.body.detail), /token/i)
  const missing = await request('/api/hub/release', { token: null })
  assert.equal(missing.status, 401)
})

test('an untrusted push is 409 and tells the owner how to install a fingerprint', async () => {
  const response = await push(signed({}), artifact.toString('base64'))
  assert.equal(response.status, 409)
  assert.match(String(response.body.detail), /install/i)
  assert.match(String(response.body.detail), /fingerprint/i)
  assert.match(String(response.body.detail), /\/api\/hub\/release\/trust/)

  // Trust is checked before the signature: a broken signature is still 409 here,
  // because "install my key" is the actionable sentence and "bad signature" is not.
  const brokenButUntrusted = await push({ ...signed({}), filename: 'tampered.dmg' }, artifact.toString('base64'))
  assert.equal(brokenButUntrusted.status, 409)
})

test('a Fleet token on the trust route is 403, not 401', async () => {
  const response = await request('/api/hub/release/trust', {
    method: 'POST',
    token: FLEET_TOKEN,
    body: { public_key: gatewayRaw.toString('hex') },
  })
  assert.equal(response.status, 403)
  // The refusal must not have installed trust.
  const release = await request('/api/hub/release')
  assert.deepEqual(release.body, { release: null, trusted_publisher: null })
})

test('trust with a fingerprint alone is stored but cannot verify a signature', async () => {
  const byFingerprint = await request('/api/hub/release/trust', {
    method: 'POST',
    token: HUB_TOKEN,
    body: { fingerprint: gatewayFingerprint },
  })
  assert.equal(byFingerprint.status, 200)
  assert.deepEqual(byFingerprint.body, { trusted: gatewayFingerprint })

  // A trusted fingerprint with no installed public key is NOT a 409: a key is
  // trusted, it just cannot verify, so the refusal is a 400 at the signature
  // step that asks for the public key -- never a field on the descriptor.
  const response = await push(signed({}), artifact.toString('base64'))
  assert.equal(response.status, 400)
  assert.match(String(response.body.detail), /public key/i)
  assert.doesNotMatch(String(response.body.detail), /publisher_public_key/)
})

test('trust installs the public key, by public_key or public_key_base64', async () => {
  const byHex = await request('/api/hub/release/trust', {
    method: 'POST',
    token: HUB_TOKEN,
    body: { public_key: gatewayRaw.toString('hex') },
  })
  assert.equal(byHex.status, 200)
  assert.deepEqual(byHex.body, { trusted: gatewayFingerprint })

  // The gateway holds both spellings; both mean the same raw 32 bytes.
  const byBase64 = await request('/api/hub/release/trust', {
    method: 'POST',
    token: HUB_TOKEN,
    body: { public_key_base64: gatewayRaw.toString('base64') },
  })
  assert.equal(byBase64.status, 200)
  assert.deepEqual(byBase64.body, { trusted: gatewayFingerprint })
})

test('a valid push with only the eleven contract fields is 200 and countersigned', async () => {
  const descriptor = signed({})
  // The descriptor carries no key material at all -- exactly eleven fields.
  assert.deepEqual(Object.keys(descriptor).sort(), [...ELEVEN_FIELDS].sort())

  const response = await push(descriptor, artifact.toString('base64'))
  assert.equal(response.status, 200, JSON.stringify(response.body))
  // The POST body IS the stored descriptor -- no `release` envelope on POST.
  assert.ok(response.body && typeof response.body === 'object')
  assert.equal(response.body.schema, 'studiohub.app-release.v1')
  assert.ok(!('release' in response.body))
  const stored = response.body
  storedDescriptor = stored

  // Stored verbatim, plus the controller's own two fields and nothing else.
  assert.deepEqual(
    Object.keys(stored).sort(),
    [...ELEVEN_FIELDS, 'controller_key_id', 'controller_signature'].sort(),
  )
  for (const field of ELEVEN_FIELDS) assert.deepEqual(stored[field], descriptor[field], field)
  assert.equal(stored.controller_key_id, controller.fingerprint)

  // controller_signature binds the stored descriptor with only
  // controller_signature removed -- including controller_key_id and the
  // gateway signature.
  const { controller_signature: controllerSignature, ...signedStored } = stored
  const verified = verify(
    null,
    Buffer.from(canonical(signedStored), 'utf8'),
    publicKeyFromRaw(Buffer.from(controller.publicKey, 'hex')),
    Buffer.from(controllerSignature, 'base64'),
  )
  assert.equal(verified, true, 'controller_signature must verify against the controller public key')
})

test('GET /release returns exactly what was stored, and names the trusted publisher', async () => {
  const release = await request('/api/hub/release')
  assert.equal(release.status, 200)
  // Hub 0.6.51 added `trusted_publisher` beside `release`: the installed
  // fingerprint, and the publisher id off the stored descriptor.
  assert.deepEqual(release.body, {
    release: storedDescriptor,
    trusted_publisher: { publisher_id: 'gatewaykh', fingerprint: gatewayFingerprint },
  })
})

test('step 1 refuses a bad schema or a missing required field and names it', async () => {
  const { version, ...withoutVersion } = baseDescriptor
  const missing = await push(signDescriptor(withoutVersion), artifact.toString('base64'))
  assert.equal(missing.status, 400)
  assert.match(String(missing.body.detail), /descriptor\.version/)

  const wrongSchema = await push(signed({ schema: 'studiohub.something-else.v1' }), artifact.toString('base64'))
  assert.equal(wrongSchema.status, 400)
  assert.match(String(wrongSchema.body.detail), /schema/)

  const wrongSchemaVersion = await push(signed({ schema_version: 2 }), artifact.toString('base64'))
  assert.equal(wrongSchemaVersion.status, 400)
  assert.match(String(wrongSchemaVersion.body.detail), /schema_version/)

  const notAnObject = await push('not-an-object', artifact.toString('base64'))
  assert.equal(notAnObject.status, 400)
  assert.match(String(notAnObject.body.detail), /descriptor/)
})

test('artifact length is checked before the hash', async () => {
  // Both the length and the hash are wrong; the refusal must be the exact one.
  const response = await push(
    signed({ bytes: artifact.length + 1, sha256: sha256hex(Buffer.from('a different payload')) }),
    artifact.toString('base64'),
  )
  assert.equal(response.status, 400)
  assert.match(String(response.body.detail), /length/i)
  assert.doesNotMatch(String(response.body.detail), /sha256/i)
})

test('GET /release/artifact serves the bytes, the sha header and Range 2-5', async () => {
  const full = await request('/api/hub/release/artifact')
  assert.equal(full.status, 200)
  assert.equal(full.headers.get('content-type'), 'application/octet-stream')
  assert.equal(full.headers.get('x-content-sha256'), sha256hex(artifact))
  assert.equal(full.headers.get('accept-ranges'), 'bytes')
  assert.deepEqual(Buffer.from(await full.response.arrayBuffer()), artifact)

  const ranged = await fetch(`${base}/api/hub/release/artifact`, {
    headers: { 'X-Hub-Token': FLEET_TOKEN, Range: 'bytes=2-5' },
  })
  assert.equal(ranged.status, 206)
  assert.equal(ranged.headers.get('content-range'), `bytes 2-5/${artifact.length}`)
  assert.equal(ranged.headers.get('x-content-sha256'), sha256hex(artifact))
  assert.deepEqual(Buffer.from(await ranged.arrayBuffer()), artifact.subarray(2, 6))

  const suffix = await fetch(`${base}/api/hub/release/artifact`, {
    headers: { 'X-Hub-Token': FLEET_TOKEN, Range: 'bytes=-4' },
  })
  assert.equal(suffix.status, 206)
  assert.equal(suffix.headers.get('content-range'), `bytes ${artifact.length - 4}-${artifact.length - 1}/${artifact.length}`)
  assert.deepEqual(Buffer.from(await suffix.arrayBuffer()), artifact.subarray(artifact.length - 4))

  const unsatisfiable = await fetch(`${base}/api/hub/release/artifact`, {
    headers: { 'X-Hub-Token': FLEET_TOKEN, Range: `bytes=${artifact.length + 10}-` },
  })
  assert.equal(unsatisfiable.status, 416)
  assert.equal(unsatisfiable.headers.get('content-range'), `bytes */${artifact.length}`)
})

test('a corrupted artifact byte is 400 and names the length or the hash', async () => {
  const corrupted = Buffer.from(artifact)
  corrupted[0] ^= 0xff
  const response = await push(signed({}), corrupted.toString('base64'))
  assert.equal(response.status, 400)
  assert.match(String(response.body.detail), /length|hash|sha256/i)
})

test('a wrong bytes length is 400 (checked before the hash)', async () => {
  const response = await push(signed({ bytes: artifact.length + 1 }), artifact.toString('base64'))
  assert.equal(response.status, 400)
  assert.match(String(response.body.detail), /length/i)
})

test('a signature over different bytes is 400', async () => {
  const tampered = { ...signed({}), filename: 'somewhere-else.dmg' }
  const response = await push(tampered, artifact.toString('base64'))
  assert.equal(response.status, 400)
  assert.match(String(response.body.detail), /signature/i)
})

test('a publisher_key_id that is not the trusted fingerprint is 400 and names both', async () => {
  const otherFingerprint = sha256hex(Buffer.from('not the trusted gateway key'))
  const response = await push(signed({ publisher_key_id: otherFingerprint }), artifact.toString('base64'))
  assert.equal(response.status, 400)
  assert.match(String(response.body.detail), new RegExp(otherFingerprint))
  assert.match(String(response.body.detail), new RegExp(gatewayFingerprint))
})

test('CLI prints one JSON line, writes a 0600 key, serves identity and exits on SIGTERM', async () => {
  const cliState = mkdtempSync(join(tmpdir(), 'release-mock-cli-'))
  const script = fileURLToPath(new URL('./mock_controller_release.mjs', import.meta.url))
  const child = spawn(process.execPath, [script, '--state', cliState, '--port', '0'], {
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  let stdout = ''
  let stderr = ''
  child.stdout.on('data', chunk => { stdout += chunk })
  child.stderr.on('data', chunk => { stderr += chunk })
  try {
    const line = await new Promise((resolveLine, rejectLine) => {
      const timer = setTimeout(() => rejectLine(new Error(`no startup line on stdout; stderr=${stderr}`)), 15_000)
      const check = () => {
        const newline = stdout.indexOf('\n')
        if (newline >= 0) {
          clearTimeout(timer)
          child.stdout.off('data', check)
          resolveLine(stdout.slice(0, newline))
        }
      }
      child.stdout.on('data', check)
      check()
    })
    const info = JSON.parse(line)
    assert.ok(Number.isInteger(info.port) && info.port > 0, `port must be ephemeral, got ${info.port}`)
    assert.match(info.fingerprint, /^[0-9a-f]{64}$/)
    assert.match(info.public_key, /^[0-9a-f]{64}$/)
    assert.equal(info.fingerprint, sha256hex(Buffer.from(info.public_key, 'hex')))
    assert.equal(statSync(join(cliState, 'controller-key.pem')).mode & 0o777, 0o600)

    const identity = await fetch(`http://127.0.0.1:${info.port}/api/hub/release/identity`, {
      headers: { 'X-Hub-Token': FLEET_TOKEN },
    })
    assert.equal(identity.status, 200)
    assert.equal((await identity.json()).fingerprint, info.fingerprint)

    // Nothing but the single startup line may reach stdout.
    assert.equal(stdout.trim().split('\n').length, 1, `unexpected stdout: ${JSON.stringify(stdout)}`)

    child.kill('SIGTERM')
    const exit = await new Promise(resolveExit => child.once('exit', (code, signal) => resolveExit({ code, signal })))
    assert.ok(exit.code === 0 || exit.signal === 'SIGTERM', `unexpected exit: ${JSON.stringify(exit)}`)
  } finally {
    child.kill('SIGKILL')
    rmSync(cliState, { recursive: true, force: true })
  }
})
