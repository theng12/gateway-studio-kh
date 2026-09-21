import { constants } from 'node:fs'
import { createHash, createPrivateKey, createPublicKey, generateKeyPairSync, sign } from 'node:crypto'
import { lstat, open } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const MAX_INPUT = 1024 * 1024

class SigningError extends Error {}
const fail = message => { throw new SigningError(message) }

function object(value, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(`${label} must be an object`)
  return value
}

async function readInput() {
  const chunks = []
  let length = 0
  for await (const chunk of process.stdin) {
    length += chunk.byteLength
    if (length > MAX_INPUT) fail('request exceeds the input limit')
    chunks.push(chunk)
  }
  try { return object(JSON.parse(Buffer.concat(chunks).toString('utf8')), 'request') } catch (error) {
    if (error instanceof SigningError) throw error
    fail('request must contain valid JSON')
  }
}

function publicIdentity(privateKey) {
  const der = createPublicKey(privateKey).export({ type: 'spki', format: 'der' })
  if (der.byteLength !== 44) fail('signing identity is not Ed25519')
  const raw = der.subarray(12)
  const fingerprint = createHash('sha256').update(raw).digest('hex')
  return {
    key_id: `gateway.content.key.${fingerprint.slice(0, 24)}`,
    public_key_base64: raw.toString('base64'),
    public_key_fingerprint: fingerprint,
  }
}

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`
  }
  const encoded = JSON.stringify(value)
  if (encoded === undefined) fail('payload is not canonical JSON')
  return encoded
}

async function readPrivateKey(path) {
  if (!Number.isInteger(constants.O_NOFOLLOW)) fail('safe key reads are unavailable')
  let handle
  try {
    handle = await open(resolve(path), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    const before = await handle.stat({ bigint: true })
    if (!before.isFile() || before.size < 1n || before.size > 16_384n || (Number(before.mode) & 0o077) !== 0) {
      fail('signing identity is unavailable or unsafe')
    }
    const bytes = Buffer.alloc(Number(before.size))
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0)
    const after = await handle.stat({ bigint: true })
    const pathInfo = await lstat(resolve(path), { bigint: true }).catch(() => undefined)
    if (bytesRead !== bytes.length || before.dev !== after.dev || before.ino !== after.ino
      || before.size !== after.size || before.mtimeNs !== after.mtimeNs || before.ctimeNs !== after.ctimeNs
      || !pathInfo?.isFile() || pathInfo.isSymbolicLink() || pathInfo.dev !== after.dev || pathInfo.ino !== after.ino) {
      fail('signing identity changed while being read')
    }
    const key = createPrivateKey(bytes)
    if (key.asymmetricKeyType !== 'ed25519') fail('signing identity is not Ed25519')
    return key
  } catch (error) {
    if (error instanceof SigningError) throw error
    fail('signing identity is unavailable or invalid')
  } finally {
    await handle?.close()
  }
}

async function generate(path) {
  const target = resolve(path)
  const current = await lstat(target).catch(error => error?.code === 'ENOENT' ? undefined : Promise.reject(error))
  if (current) fail('signing identity already exists')
  const { privateKey } = generateKeyPairSync('ed25519')
  const pem = privateKey.export({ type: 'pkcs8', format: 'pem' })
  let handle
  try {
    handle = await open(target, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600)
    await handle.writeFile(pem)
    await handle.sync()
  } catch {
    fail('signing identity could not be stored')
  } finally {
    await handle?.close()
  }
  return publicIdentity(privateKey)
}

async function run() {
  const request = await readInput()
  const fields = request.action === 'generate' || request.action === 'inspect'
    ? ['action', 'private_key_path']
    : request.action === 'sign' ? ['action', 'payload', 'private_key_path'] : []
  if (!fields.length || Object.keys(request).sort().join(',') !== fields.sort().join(',')
    || typeof request.private_key_path !== 'string' || !request.private_key_path) {
    fail('unsupported signing request')
  }
  if (request.action === 'generate') return generate(request.private_key_path)
  const privateKey = await readPrivateKey(request.private_key_path)
  if (request.action === 'inspect') return publicIdentity(privateKey)
  object(request.payload, 'payload')
  return { signature: sign(null, Buffer.from(canonical(request.payload), 'utf8'), privateKey).toString('base64') }
}

try {
  if (process.argv.length !== 2 || resolve(process.argv[1]) !== fileURLToPath(import.meta.url)) fail('signing helper takes requests only on stdin')
  process.stdout.write(`${JSON.stringify(await run())}\n`)
} catch (error) {
  const detail = error instanceof SigningError ? error.message : 'signing operation failed'
  process.stdout.write(`${JSON.stringify({ error: detail })}\n`)
  process.exitCode = 1
}
