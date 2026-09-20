import { constants } from 'node:fs'
import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import { lstat, mkdir, mkdtemp, open, readdir, realpath, rm, stat, writeFile } from 'node:fs/promises'
import { basename, dirname, join, relative, resolve, sep } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath, pathToFileURL } from 'node:url'

const MAX_REQUEST_BYTES = 4 * 1024 * 1024
const MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
const MAX_OUTPUT_BYTES = 50 * 1024 * 1024
const MAX_RESULT_BYTES = 8 * 1024 * 1024
const STORE_PROFILE = 'genstudio-store-receipt-v1'
const RELEASE_BOUND_TOKEN = '{{MIN_APP_VERSION}}'
const SEMVER = /^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$/
const RECEIPT = /^authoring:[a-f0-9]{64}$/
const RESERVATION_FIELDS = ['key', 'item_id', 'pack_id', 'version', 'receipt_ref']
const ACTION_FIELDS = {
  catalogue: new Set(['action']),
  template: new Set(['action', 'key']),
  reservation: new Set(['action', 'key']),
  validate: new Set(['action', 'key', 'expected_reservation', 'document']),
  build: new Set(['action', 'key', 'expected_reservation', 'document']),
}

class BridgeError extends Error {}

function fail(message) {
  throw new BridgeError(message)
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null)
}

function scanJson(text, label) {
  let index = 0
  let nodes = 0
  const length = text.length

  const failJson = () => fail(`${label} must contain valid JSON`)
  const whitespace = () => {
    while (index < length && /\s/.test(text[index])) index += 1
  }
  const string = () => {
    if (text[index] !== '"') failJson()
    const start = index
    index += 1
    let escaped = false
    while (index < length) {
      const char = text[index]
      if (escaped) {
        escaped = false
        index += 1
        continue
      }
      if (char === '\\') {
        escaped = true
        index += 1
        continue
      }
      if (char === '"') {
        index += 1
        try {
          return JSON.parse(text.slice(start, index))
        } catch {
          failJson()
        }
      }
      if (char < ' ') failJson()
      index += 1
    }
    failJson()
  }
  const value = depth => {
    nodes += 1
    if (nodes > 250_000 || depth > 32) fail(`${label} exceeds the JSON complexity limit`)
    whitespace()
    const char = text[index]
    if (char === '"') return string()
    if (char === '{') {
      index += 1
      whitespace()
      const result = {}
      const keys = new Set()
      if (text[index] === '}') {
        index += 1
        return result
      }
      while (index < length) {
        whitespace()
        const key = string()
        if (keys.has(key)) fail(`${label} contains duplicate object keys`)
        keys.add(key)
        whitespace()
        if (text[index] !== ':') failJson()
        index += 1
        Object.defineProperty(result, key, {
          configurable: true,
          enumerable: true,
          value: value(depth + 1),
          writable: true,
        })
        whitespace()
        if (text[index] === '}') {
          index += 1
          return result
        }
        if (text[index] !== ',') failJson()
        index += 1
      }
      failJson()
    }
    if (char === '[') {
      index += 1
      whitespace()
      const result = []
      if (text[index] === ']') {
        index += 1
        return result
      }
      while (index < length) {
        result.push(value(depth + 1))
        whitespace()
        if (text[index] === ']') {
          index += 1
          return result
        }
        if (text[index] !== ',') failJson()
        index += 1
      }
      failJson()
    }
    const literal = text.slice(index).match(/^(?:true|false|null)/)?.[0]
    if (literal) {
      index += literal.length
      return literal === 'true' ? true : literal === 'false' ? false : null
    }
    const number = text.slice(index).match(/^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/)?.[0]
    if (number) {
      index += number.length
      const parsed = Number(number)
      if (!Number.isFinite(parsed)) fail(`${label} must contain finite JSON numbers`)
      return parsed
    }
    failJson()
  }

  const result = value(0)
  whitespace()
  if (index !== length) failJson()
  return result
}

function assertJson(value, label) {
  const stack = [{ value, depth: 0 }]
  let nodes = 0
  while (stack.length) {
    const current = stack.pop()
    nodes += 1
    if (nodes > 250_000 || current.depth > 32) fail(`${label} exceeds the JSON complexity limit`)
    if (typeof current.value === 'number' && !Number.isFinite(current.value)) {
      fail(`${label} must contain finite JSON numbers`)
    }
    if (current.value && typeof current.value === 'object') {
      if (!Array.isArray(current.value) && !isPlainObject(current.value)) fail(`${label} must contain plain JSON values`)
      for (const child of Array.isArray(current.value) ? current.value : Object.values(current.value)) {
        stack.push({ value: child, depth: current.depth + 1 })
      }
    }
  }
  return value
}

function parseJson(text, label, maximum = MAX_REQUEST_BYTES) {
  if (Buffer.byteLength(text, 'utf8') > maximum) fail(`${label} exceeds the ${maximum}-byte limit`)
  return assertJson(scanJson(text, label), label)
}

async function readBoundedJson(path, label, maximum = MAX_DOCUMENT_BYTES) {
  if (!Number.isInteger(constants.O_NOFOLLOW)) fail('safe no-follow reads are unavailable on this platform')
  let handle
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW)
    const before = await handle.stat({ bigint: true })
    if (!before.isFile()) fail(`${label} must be a regular file`)
    if (before.size > BigInt(maximum)) fail(`${label} exceeds the ${maximum}-byte limit`)
    const bytes = Buffer.alloc(maximum + 1)
    let length = 0
    while (length < bytes.length) {
      const result = await handle.read(bytes, length, Math.min(64 * 1024, bytes.length - length), length)
      if (result.bytesRead === 0) break
      length += result.bytesRead
    }
    const after = await handle.stat({ bigint: true })
    if (length > maximum || length !== Number(after.size)
      || before.dev !== after.dev || before.ino !== after.ino
      || before.size !== after.size || before.mtimeNs !== after.mtimeNs || before.ctimeNs !== after.ctimeNs) {
      fail(`${label} changed while being read`)
    }
    const pathInfo = await lstat(path, { bigint: true }).catch(() => undefined)
    if (!pathInfo?.isFile() || pathInfo.isSymbolicLink() || pathInfo.dev !== after.dev || pathInfo.ino !== after.ino) {
      fail(`${label} must remain a regular file`)
    }
    return parseJson(bytes.subarray(0, length).toString('utf8'), label, maximum)
  } catch (error) {
    if (error instanceof BridgeError) throw error
    fail(`${label} is unavailable or unsafe`)
  } finally {
    await handle?.close()
  }
}

async function readBoundedBytes(path, label, maximum) {
  if (!Number.isInteger(constants.O_NOFOLLOW)) fail('safe no-follow reads are unavailable on this platform')
  let handle
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW)
    const before = await handle.stat({ bigint: true })
    if (!before.isFile() || before.size > BigInt(maximum)) fail(`${label} is not a bounded regular file`)
    const bytes = Buffer.alloc(Number(before.size))
    let offset = 0
    while (offset < bytes.length) {
      const result = await handle.read(bytes, offset, bytes.length - offset, offset)
      if (result.bytesRead === 0) break
      offset += result.bytesRead
    }
    const after = await handle.stat({ bigint: true })
    const pathInfo = await lstat(path, { bigint: true }).catch(() => undefined)
    if (offset !== bytes.length || before.dev !== after.dev || before.ino !== after.ino
      || before.size !== after.size || before.mtimeNs !== after.mtimeNs || before.ctimeNs !== after.ctimeNs
      || !pathInfo?.isFile() || pathInfo.isSymbolicLink() || pathInfo.dev !== after.dev || pathInfo.ino !== after.ino) {
      fail(`${label} changed while being read`)
    }
    return bytes
  } catch (error) {
    if (error instanceof BridgeError) throw error
    fail(`${label} is unavailable or unsafe`)
  } finally {
    await handle?.close()
  }
}

function inside(path, root) {
  const remainder = relative(root, path)
  return remainder === '' || (!remainder.startsWith(`..${sep}`) && remainder !== '..' && !remainder.startsWith(sep))
}

async function assertDirectory(path, label) {
  const info = await lstat(path).catch(() => undefined)
  if (!info?.isDirectory() || info.isSymbolicLink()) fail(`${label} is unavailable or must not be a symlink`)
  return realpath(path)
}

async function existingParent(path, label) {
  let cursor = dirname(path)
  const missing = []
  while (true) {
    const info = await lstat(cursor).catch(() => undefined)
    if (!info) {
      const parent = dirname(cursor)
      if (parent === cursor) fail(`${label} parent is unavailable`)
      missing.unshift(basename(cursor))
      cursor = parent
      continue
    }
    if (info.isSymbolicLink() || !info.isDirectory()) fail(`${label} parent must be a real directory`)
    const physical = await realpath(cursor)
    return resolve(physical, ...missing)
  }
}

async function assertOutputPath(path, storyRoot) {
  if (typeof path !== 'string' || !path) fail('build output path is required')
  const output = resolve(path)
  const current = await lstat(output).catch(() => undefined)
  if (current) {
    if (current.isSymbolicLink()) fail('build output path must not be a symlink')
    fail('build output path already exists; choose a fresh candidate path')
  }
  const physicalParent = await existingParent(output, 'build output')
  const storyPhysical = await realpath(storyRoot)
  if (inside(physicalParent, storyPhysical)) fail('build output must be outside the Story source checkout')
  return output
}

function safeError(error) {
  if (error instanceof BridgeError) {
    const message = error.message.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim()
    return message.length <= 400 ? message : 'Story authoring request was rejected.'
  }
  const message = error instanceof Error ? error.message : ''
  if (message.startsWith('Authoring registry:')) return 'Story authoring registry is invalid or unavailable; check the Story checkout.'
  if (message.startsWith('Content Pack publisher error:')) return 'Story Store-profile builder rejected the candidate; check the Story checkout.'
  return 'Story authoring prerequisites are unavailable; check the Story source checkout and its installed dependencies.'
}

async function loadStory(storyRoot) {
  const registryModulePath = join(storyRoot, 'scripts', 'content-packs', 'authoring-registry.mjs')
  const registryPath = join(storyRoot, 'content-packs', 'authoring-registry.json')
  const libPath = join(storyRoot, 'scripts', 'content-packs', 'lib.mjs')
  const schemaPath = join(storyRoot, 'electron', 'server', 'content-packs', 'schemas.ts')
  try {
    const [registry, lib] = await Promise.all([
      import(pathToFileURL(registryModulePath).href),
      import(pathToFileURL(libPath).href),
    ])
    const requireFromStory = createRequire(pathToFileURL(join(storyRoot, 'package.json')))
    const tsxApi = await import(pathToFileURL(requireFromStory.resolve('tsx/esm/api')).href)
    const unregister = tsxApi.register()
    let schemas
    try {
      schemas = await import(pathToFileURL(schemaPath).href)
    } finally {
      await unregister()
    }
    return { registry, lib, schemas, registryPath }
  } catch {
    fail('Story authoring dependencies are unavailable; check the Story source checkout and its installed dependencies')
  }
}

async function readRegistry(story, storyRoot) {
  try {
    return await story.registry.validateAuthoringRegistry({
      registryPath: story.registryPath,
      repositoryRoot: storyRoot,
    })
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('Authoring registry:')) throw error
    fail('Story authoring registry is unavailable or invalid')
  }
}

function keyFor(record) {
  return `${record.pack_id}@${record.version}`
}

function expectedReservation(value, key) {
  if (!isPlainObject(value) || Object.keys(value).length !== RESERVATION_FIELDS.length
    || RESERVATION_FIELDS.some(field => typeof value[field] !== 'string' || !value[field])
    || Object.keys(value).some(field => !RESERVATION_FIELDS.includes(field))
    || value.key !== key || value.key !== `${value.pack_id}@${value.version}`
    || !RECEIPT.test(value.receipt_ref)) {
    fail('saved Topic Pack reservation identity is invalid')
  }
  return value
}

function assertExpectedReservation(record, expected) {
  if (keyFor(record) !== expected.key || record.item_id !== expected.item_id
    || record.pack_id !== expected.pack_id || record.version !== expected.version
    || record.receipt_ref !== expected.receipt_ref) {
    fail('Topic Pack reservation no longer matches the saved draft identity')
  }
}

function recordFor(registry, key) {
  if (typeof key !== 'string' || !key) fail('a Topic Pack reservation key is required')
  const record = registry.records.find(candidate => keyFor(candidate) === key && candidate.kind === 'topic-pack')
  if (!record) fail('Topic Pack reservation was not found in the Story authoring registry')
  return record
}

function identityWithoutReceipt(record) {
  const { receipt_ref: _receipt, ...identity } = record
  return identity
}

async function verifyReserved(story, storyRoot, registry, record) {
  if (record.status !== 'reserved') fail('Topic Pack identity is not a currently reserved authoring record')
  if (!RECEIPT.test(record.receipt_ref)) fail('Topic Pack reservation has no valid authoring receipt')
  const batch = registry.batches.find(candidate => candidate.receipt_ref === record.receipt_ref
    && candidate.request.records.some(item => item.pack_id === record.pack_id && item.version === record.version))
  if (!batch) fail('Topic Pack reservation receipt is not bound to its original batch')
  try {
    return await story.registry.verifyAuthoringReceipt({
      registryPath: story.registryPath,
      repositoryRoot: storyRoot,
      revision: batch.revision,
      receiptRef: record.receipt_ref,
      expectedIdentity: identityWithoutReceipt(record),
    })
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('Authoring registry:')) {
      fail('Topic Pack reservation receipt no longer verifies against its original registry batch')
    }
    fail('Topic Pack reservation receipt could not be verified')
  }
}

async function parseDocument(story, input) {
  if (!isPlainObject(input)) fail('Topic Pack document must be a JSON object')
  try {
    const document = story.schemas.ContentPackDocumentSchema.parse(input)
    if (document.kind !== 'topic-pack') fail('only topic-pack documents are supported by this bridge')
    return document
  } catch {
    fail('document rejected by Story shared Content Pack schema')
  }
}

async function readTemplate(story, storyRoot, record) {
  if (record.status !== 'authored') fail('template is available only from an authored Topic Pack source')
  const source = resolve(storyRoot, record.source_path)
  const storyPhysical = await realpath(storyRoot)
  if (!inside(source, resolve(storyRoot))) fail('authored Topic Pack source path escaped the Story checkout')
  const sourceInfo = await lstat(source).catch(() => undefined)
  if (!sourceInfo?.isDirectory() || sourceInfo.isSymbolicLink()) fail('authored Topic Pack source is unavailable or unsafe')
  const content = join(source, 'content')
  const contentInfo = await lstat(content).catch(() => undefined)
  if (!contentInfo?.isDirectory() || contentInfo.isSymbolicLink()) fail('authored Topic Pack content directory is unavailable or unsafe')
  const entries = await readdir(content, { withFileTypes: true })
  const documents = entries.filter(entry => entry.name.endsWith('.json'))
  if (documents.length !== 1 || documents.some(entry => entry.isSymbolicLink() || !entry.isFile())) {
    fail('authored Topic Pack template must contain exactly one regular JSON document')
  }
  const path = join(content, documents[0].name)
  const physical = await realpath(path)
  if (!inside(physical, storyPhysical)) fail('authored Topic Pack source escaped the Story checkout')
  const document = await readBoundedJson(path, 'authored Topic Pack document')
  const parsed = await parseDocument(story, document)
  if (parsed.id !== record.item_id) fail('authored Topic Pack document identity does not match its registry record')
  return parsed
}

async function releaseFloor(storyRoot) {
  const value = await readBoundedJson(join(storyRoot, 'content-packs', 'release-floor.json'), 'Story release floor', 1024 * 1024)
  if (!isPlainObject(value) || typeof value.min_app_version !== 'string' || !SEMVER.test(value.min_app_version)) {
    fail('Story release floor is missing or invalid')
  }
  return value.min_app_version
}

function manifestFor(story, record, document, minAppVersion) {
  if (record.min_app_version_status !== 'release-bound' || record.min_app_version !== RELEASE_BOUND_TOKEN) {
    fail('reserved Topic Pack does not use Story release-bound identity')
  }
  const documentBytes = Buffer.from(JSON.stringify(document))
  const manifest = {
    pack_format: story.schemas.CONTENT_PACK_FORMAT,
    pack_id: record.pack_id,
    publisher_id: record.publisher_id,
    version: record.version,
    min_app_version: minAppVersion,
    lifecycle: 'active',
    delivery_profile: STORE_PROFILE,
    entitlement: {
      sku: record.stocking_proposal.sku.id,
      family: record.stocking_proposal.family.id,
    },
    dependencies: record.dependencies,
    items: [{ id: record.item_id, kind: 'topic-pack', schema_version: 1, path: 'content/topic-pack.json' }],
    bundles: [],
    files: [{
      path: 'content/topic-pack.json',
      sha256: createHash('sha256').update(documentBytes).digest('hex'),
      bytes: documentBytes.byteLength,
      mime: 'application/json',
    }],
  }
  try {
    return story.schemas.ContentPackManifestSchema.parse(manifest)
  } catch {
    fail('manifest rejected by Story shared Content Pack schema')
  }
}

async function validateInput(story, storyRoot, registry, key, expected, input) {
  const record = recordFor(registry, key)
  assertExpectedReservation(record, expected)
  await verifyReserved(story, storyRoot, registry, record)
  const document = await parseDocument(story, input)
  if (document.id !== record.item_id) fail('Topic Pack document identity does not match the reserved registry item')
  const manifest = manifestFor(story, record, document, await releaseFloor(storyRoot))
  return { record, document, manifest }
}

async function build(story, storyRoot, registry, key, expected, input, outputPath) {
  const output = await assertOutputPath(outputPath, storyRoot)
  const validated = await validateInput(story, storyRoot, registry, key, expected, input)
  const stageRoot = await mkdtemp(join(tmpdir(), 'gateway-topic-pack-')).catch(error => {
    if (error instanceof BridgeError) throw error
    fail('private Topic Pack staging is unavailable')
  })
  const source = join(stageRoot, 'source')
  try {
    await mkdir(join(source, 'content'), { recursive: true, mode: 0o700 })
    const sourceManifest = {
      pack_format: validated.manifest.pack_format,
      pack_id: validated.manifest.pack_id,
      publisher_id: validated.manifest.publisher_id,
      version: validated.manifest.version,
      min_app_version: validated.manifest.min_app_version,
      lifecycle: 'active',
      entitlement: validated.manifest.entitlement,
      dependencies: validated.manifest.dependencies,
    }
    await writeFile(join(source, 'pack.json'), `${JSON.stringify(sourceManifest)}\n`, { mode: 0o600, flag: 'wx' })
    await writeFile(join(source, 'content', 'topic-pack.json'), JSON.stringify(validated.document), { mode: 0o600, flag: 'wx' })
    const result = await story.lib.buildContentPack({
      sourceDirectory: source,
      outputPath: output,
      deliveryProfile: STORE_PROFILE,
      onBeforeOutputCommit: async () => {
        const current = await readRegistry(story, storyRoot)
        const currentRecord = recordFor(current, key)
        assertExpectedReservation(currentRecord, expected)
        await verifyReserved(story, storyRoot, current, currentRecord)
      },
    })
    const bytes = await readBoundedBytes(output, 'candidate archive', MAX_OUTPUT_BYTES)
    const info = await stat(output)
    if (info.size !== bytes.byteLength || bytes.byteLength > MAX_OUTPUT_BYTES) fail('candidate archive exceeds the output limit')
    const sha256 = createHash('sha256').update(bytes).digest('hex')
    if (result.sha256 !== sha256 || result.packId !== validated.record.pack_id || result.version !== validated.record.version) {
      fail('Story builder returned an inconsistent candidate archive')
    }
    return { sha256, bytes: bytes.byteLength, pack_id: result.packId, version: result.version }
  } catch (error) {
    if (error instanceof BridgeError) throw error
    if (error instanceof Error && error.message.startsWith('Content Pack publisher error:')) throw error
    fail('Story Store-profile builder could not produce a valid candidate')
  } finally {
    await rm(stageRoot, { recursive: true, force: true })
  }
}

async function readStdin() {
  const chunks = []
  let bytes = 0
  for await (const chunk of process.stdin) {
    bytes += chunk.byteLength
    if (bytes > MAX_REQUEST_BYTES) fail(`request exceeds the ${MAX_REQUEST_BYTES}-byte limit`)
    chunks.push(chunk)
  }
  return Buffer.concat(chunks).toString('utf8')
}

function assertRequestShape(request) {
  if (!isPlainObject(request)) fail('request must be a JSON object')
  if (typeof request.action !== 'string' || !ACTION_FIELDS[request.action]) fail('request action is unsupported')
  for (const field of Object.keys(request)) {
    if (!ACTION_FIELDS[request.action].has(field)) fail('request contains an unsupported field')
  }
  if (request.action !== 'catalogue' && (typeof request.key !== 'string' || !request.key)) fail('request key is required')
  if ((request.action === 'validate' || request.action === 'build') && !Object.hasOwn(request, 'document')) {
    fail('request document is required')
  }
  if ((request.action === 'validate' || request.action === 'build') && !Object.hasOwn(request, 'expected_reservation')) {
    fail('saved Topic Pack reservation identity is required')
  }
  return request
}

async function run() {
  const args = process.argv.slice(2)
  if (args.length < 1 || args.length > 2) fail('usage: node story_authoring.mjs <story-source> [output-path]')
  const storyRoot = await assertDirectory(resolve(args[0]), 'Story source checkout')
  const request = assertRequestShape(parseJson(await readStdin(), 'request'))
  const expected = request.action === 'validate' || request.action === 'build'
    ? expectedReservation(request.expected_reservation, request.key)
    : undefined
  if (args[1] !== undefined && request.action !== 'build') fail('output path is only allowed for the build action')
  const story = await loadStory(storyRoot)
  const registry = await readRegistry(story, storyRoot)

  if (request.action === 'catalogue') {
    const reservations = registry.records
      .filter(record => record.kind === 'topic-pack' && record.status === 'reserved')
      .sort((left, right) => keyFor(left).localeCompare(keyFor(right)))
      .map(record => ({ key: keyFor(record), item_id: record.item_id, pack_id: record.pack_id, version: record.version, receipt_ref: record.receipt_ref }))
    const templates = []
    for (const record of registry.records.filter(candidate => candidate.kind === 'topic-pack' && candidate.status === 'authored')) {
      const document = await readTemplate(story, storyRoot, record)
      templates.push({ key: keyFor(record), name: document.name })
    }
    templates.sort((left, right) => left.key.localeCompare(right.key))
    return { registry_revision: registry.revision, reservations, templates }
  }

  const record = recordFor(registry, request.key)
  if (request.action === 'template') return { document: await readTemplate(story, storyRoot, record) }
  if (request.action === 'reservation') {
    await verifyReserved(story, storyRoot, registry, record)
    return { key: keyFor(record), item_id: record.item_id, pack_id: record.pack_id, version: record.version, receipt_ref: record.receipt_ref }
  }
  if (request.action === 'validate') {
    const { document, manifest } = await validateInput(story, storyRoot, registry, request.key, expected, request.document)
    return { document, manifest }
  }
  return build(story, storyRoot, registry, request.key, expected, request.document, args[1])
}

async function main() {
  try {
    const result = await run()
    const output = JSON.stringify(result)
    if (Buffer.byteLength(output, 'utf8') > MAX_RESULT_BYTES) fail('result exceeds the output limit')
    process.stdout.write(`${output}\n`)
    return 0
  } catch (error) {
    const output = JSON.stringify({ error: safeError(error) })
    process.stdout.write(`${output}\n`)
    return 1
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.noDeprecation = true
  process.exitCode = await main()
}
