import { constants } from 'node:fs'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { lstat, open, realpath } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

const MAX_INPUT = 64 * 1024
const MAX_ARCHIVE = 50 * 1024 * 1024
const SUPPORTED_KINDS = new Set(['series-formula', 'topic-pack', 'visual-style', 'thumbnail-recipe', 'lettering-style', 'metadata-formula'])
class ValidationError extends Error {}
const fail = message => { throw new ValidationError(message) }

async function readInput() {
  const chunks = []
  let length = 0
  for await (const chunk of process.stdin) {
    length += chunk.byteLength
    if (length > MAX_INPUT) fail('request exceeds the input limit')
    chunks.push(chunk)
  }
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString('utf8'))
    if (!value || typeof value !== 'object' || Array.isArray(value)) fail('request must be an object')
    return value
  } catch (error) {
    if (error instanceof ValidationError) throw error
    fail('request must contain valid JSON')
  }
}

async function realDirectory(path, label) {
  const info = await lstat(path).catch(() => undefined)
  if (!info?.isDirectory() || info.isSymbolicLink()) fail(`${label} is unavailable or unsafe`)
  return realpath(path)
}

async function readArchive(path) {
  if (!Number.isInteger(constants.O_NOFOLLOW)) fail('safe archive reads are unavailable')
  let handle
  try {
    handle = await open(resolve(path), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    const before = await handle.stat({ bigint: true })
    if (!before.isFile() || before.size < 1n || before.size > BigInt(MAX_ARCHIVE)) fail('archive is unavailable or exceeds 50 MiB')
    const bytes = Buffer.alloc(Number(before.size))
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0)
    const after = await handle.stat({ bigint: true })
    const current = await lstat(resolve(path), { bigint: true }).catch(() => undefined)
    const stable = ['dev', 'ino', 'mode', 'nlink', 'size', 'mtimeNs', 'ctimeNs']
    if (bytesRead !== bytes.length || stable.some(field => before[field] !== after[field])
      || !current?.isFile() || current.isSymbolicLink() || current.dev !== after.dev || current.ino !== after.ino) {
      fail('archive changed while being validated')
    }
    return bytes
  } catch (error) {
    if (error instanceof ValidationError) throw error
    fail('archive is missing or unreadable')
  } finally {
    await handle?.close()
  }
}

async function loadStory(storyRoot) {
  const registryPath = join(storyRoot, 'scripts', 'content-packs', 'authoring-registry.mjs')
  const archivePath = join(storyRoot, 'electron', 'server', 'content-packs', 'archive.ts')
  try {
    const registry = await import(pathToFileURL(registryPath).href)
    const requireFromStory = createRequire(pathToFileURL(join(storyRoot, 'package.json')))
    const tsxApi = await import(pathToFileURL(requireFromStory.resolve('tsx/esm/api')).href)
    const unregister = tsxApi.register()
    const archive = await import(pathToFileURL(archivePath).href)
    return { registry, archive, unregister }
  } catch {
    fail('Story Content Pack validation dependencies are unavailable')
  }
}

async function snapshot(request, storyRoot, repositoryRoot) {
  const registry = await import(pathToFileURL(join(storyRoot, 'scripts', 'content-packs', 'authoring-registry.mjs')).href)
  const validated = await registry.validateAuthoringRegistry({ registryPath: resolve(request.registry_path), repositoryRoot })
  return {
    registry_revision: validated.revision,
    pack_ids: [...new Set(validated.records.map(record => record.pack_id))].sort(),
    family_ids: [...new Set(validated.records.map(record => record.stocking_proposal.family.id))].sort(),
  }
}

async function validate(request, storyRoot, repositoryRoot) {
  const bytes = await readArchive(request.archive_path)
  const story = await loadStory(storyRoot)
  try {
    const registry = await story.registry.validateAuthoringRegistry({
      registryPath: resolve(request.registry_path), repositoryRoot,
    })
    const verified = await story.archive.verifyDeliveryAttestedContentPackArchive(bytes)
    const manifest = verified.manifest
    if (manifest.items.length !== 1 || manifest.bundles.length !== 0 || verified.documents.size !== 1) {
      fail('private shelf publications require exactly one standalone content item and no bundles')
    }
    const item = manifest.items[0]
    const document = verified.documents.get(item.id)
    if (!document || !SUPPORTED_KINDS.has(document.kind) || document.kind !== item.kind) {
      fail('this Content Pack kind requires a coordinated Story update before private delivery')
    }
    const record = registry.records.find(candidate => candidate.pack_id === manifest.pack_id && candidate.version === manifest.version)
    if (!record) fail('archive identity has no current Story authoring registry receipt')
    if (record.status !== 'reserved' && record.status !== 'authored') fail('archive authoring identity is not publishable')
    if (record.publisher_id !== manifest.publisher_id || record.kind !== document.kind
      || record.stocking_proposal.family.id !== manifest.entitlement.family
      || record.stocking_proposal.sku.id !== manifest.entitlement.sku
      || !(record.item_ids ?? [record.item_id]).includes(item.id)) {
      fail('archive identity does not match its Story authoring registry receipt')
    }
    const batch = registry.batches.find(candidate => candidate.receipt_ref === record.receipt_ref
      && candidate.request.records.some(identity => identity.pack_id === manifest.pack_id && identity.version === manifest.version))
    const identity = batch?.request.records.find(candidate => candidate.pack_id === manifest.pack_id && candidate.version === manifest.version)
    if (!batch || !identity) fail('archive authoring receipt is not bound to a persisted registry batch')
    await story.registry.verifyAuthoringReceipt({
      registryPath: resolve(request.registry_path), repositoryRoot,
      revision: batch.revision, receiptRef: record.receipt_ref, expectedIdentity: identity,
    })
    return {
      registry_revision: registry.revision,
      receipt_revision: batch.revision,
      receipt_ref: record.receipt_ref,
      pack_id: manifest.pack_id,
      publisher_id: manifest.publisher_id,
      version: manifest.version,
      min_story_version: manifest.min_app_version,
      product_id: manifest.entitlement.sku,
      entitlement_family: manifest.entitlement.family,
      item_id: item.id,
      kind: document.kind,
      title: document.name,
      description: document.description ?? '',
      tags: Array.isArray(document.tags) ? document.tags : [],
      changelog: manifest.changelog ?? '',
      dependencies: manifest.dependencies ?? [],
      archive_sha256: createHash('sha256').update(bytes).digest('hex'),
      archive_bytes: bytes.byteLength,
    }
  } finally {
    await story.unregister()
  }
}

async function catalogRow(request, storyRoot) {
  // The parser is the authority on its own limits, so ask it rather than copy the
  // numbers. `parseContentStoreCatalog` throws on the first invalid product and
  // Story then answers 500 for the whole Content Store, so one 14-tag pack takes
  // every product down — refuse it here instead of shipping it.
  const requireFromStory = createRequire(pathToFileURL(join(storyRoot, 'package.json')))
  const tsxApi = await import(pathToFileURL(requireFromStory.resolve('tsx/esm/api')).href)
  const unregister = tsxApi.register()
  try {
    const store = await import(pathToFileURL(join(storyRoot, 'electron', 'server', 'lib', 'genstudio-content-store.ts')).href)
    store.parseContentStoreCatalog({ products: [request.row] })
    return { ok: true }
  } catch (error) {
    const message = error instanceof Error ? error.message : ''
    return { ok: false, reason: message && message.length <= 200 ? message : 'Story rejected the catalogue row' }
  } finally {
    await unregister()
  }
}

async function run() {
  const request = await readInput()
  if (request.action === 'validate-catalog-row') {
    const keys = Object.keys(request).sort().join(',')
    if (keys !== 'action,row,story_root' || typeof request.story_root !== 'string' || !request.story_root
      || !request.row || typeof request.row !== 'object' || Array.isArray(request.row)) {
      fail('unsupported validation request')
    }
    return catalogRow(request, await realDirectory(resolve(request.story_root), 'Story source checkout'))
  }
  const expected = request.action === 'snapshot'
    ? ['action', 'registry_path', 'story_root']
    : request.action === 'validate'
      ? ['action', 'archive_path', 'registry_path', 'story_root'] : []
  if (!expected.length || Object.keys(request).sort().join(',') !== expected.sort().join(',')
    || expected.some(field => typeof request[field] !== 'string' || !request[field])) fail('unsupported validation request')
  const storyRoot = await realDirectory(resolve(request.story_root), 'Story source checkout')
  const registryPath = resolve(request.registry_path)
  const repositoryRoot = await realDirectory(dirname(dirname(registryPath)), 'Story registry repository')
  return request.action === 'snapshot'
    ? snapshot(request, storyRoot, repositoryRoot)
    : validate(request, storyRoot, repositoryRoot)
}

try {
  if (process.argv.length !== 2 || resolve(process.argv[1]) !== fileURLToPath(import.meta.url)) fail('validation helper takes requests only on stdin')
  process.stdout.write(`${JSON.stringify(await run())}\n`)
} catch (error) {
  const message = error instanceof Error ? error.message : ''
  const detail = error instanceof ValidationError ? error.message
    : message.startsWith('Authoring registry:') ? 'Story authoring registry is invalid or unavailable.'
      : message.startsWith('Invalid content-pack archive:') ? 'Story rejected the Content Pack archive.'
        : 'Story Content Pack validation prerequisites are unavailable.'
  process.stdout.write(`${JSON.stringify({ error: detail })}\n`)
  process.exitCode = 1
}
