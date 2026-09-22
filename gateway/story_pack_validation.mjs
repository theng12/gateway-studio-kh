import { constants } from 'node:fs'
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { lstat, open, realpath } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

const MAX_INPUT = 64 * 1024
const MAX_ITEMS = 24  // the registry's own ceiling (MAX_PACK_ITEMS)
const MAX_ARCHIVE = 50 * 1024 * 1024
const SUPPORTED_KINDS = new Set(['series-formula', 'topic-pack', 'visual-style', 'thumbnail-recipe', 'lettering-style', 'metadata-formula'])
class ValidationError extends Error {}
const fail = message => { throw new ValidationError(message) }

/** A pack's display name when its items do not name it: its own slug, title-cased. */
function packTitle(packId) {
  const slug = String(packId).split('.').pop() ?? ''
  // The re-homed identity ends in `-v2`, which is bookkeeping rather than a name.
  const named = slug.replace(/-v\d+$/, '')
  return named.split('-').filter(Boolean).map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' ') || packId
}

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
    // A pack may hold several items — the lettering and metadata packs hold six
    // styles each — so the rule is 1 to MAX_ITEMS, not exactly one, and one document
    // per item. Bundles stay refused: a bundle would make it a coordinator, which is
    // what the standalone product model exists to prevent.
    if (!manifest.items.length || manifest.items.length > MAX_ITEMS
      || manifest.bundles.length !== 0 || verified.documents.size !== manifest.items.length) {
      fail(`private shelf publications require 1 to ${MAX_ITEMS} standalone content items and no bundles`)
    }
    const resolved = manifest.items.map(item => ({ item, document: verified.documents.get(item.id) }))
    if (resolved.some(({ item, document }) => !document || !SUPPORTED_KINDS.has(document.kind) || document.kind !== item.kind)) {
      fail('this Content Pack kind requires a coordinated Story update before private delivery')
    }
    const kind = resolved[0].document.kind
    if (resolved.some(({ document }) => document.kind !== kind)) {
      fail('a private shelf pack must hold one kind of content')
    }
    const itemIds = manifest.items.map(item => item.id)
    const primary = resolved[0].document
    const record = registry.records.find(candidate => candidate.pack_id === manifest.pack_id && candidate.version === manifest.version)
    if (!record) fail('archive identity has no current Story authoring registry receipt')
    if (record.status !== 'reserved' && record.status !== 'authored') fail('archive authoring identity is not publishable')
    // Every item must be receipted, and the counts must match: a pack could otherwise
    // claim six styles and ship a seventh the registry never reserved.
    const receipted = record.item_ids ?? [record.item_id]
    if (record.publisher_id !== manifest.publisher_id || record.kind !== kind
      || record.stocking_proposal.family.id !== manifest.entitlement.family
      || record.stocking_proposal.sku.id !== manifest.entitlement.sku
      || itemIds.length !== receipted.length
      || !itemIds.every(id => receipted.includes(id))) {
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
      // `item_id` stays for the single-item readers; `item_ids` is the truth.
      item_id: itemIds[0],
      item_ids: itemIds,
      kind,
      // A pack has no name of its own; a lone item names the product, and a pack of
      // several takes its name from the slug its author chose, with the release
      // changelog as the pack-level description.
      title: itemIds.length === 1 ? primary.name : packTitle(manifest.pack_id),
      description: (itemIds.length === 1 ? primary.description : (manifest.changelog || primary.description)) ?? '',
      tags: Array.isArray(primary.tags) ? primary.tags : [],
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
