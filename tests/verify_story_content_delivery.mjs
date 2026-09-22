// Runs Story's real content-store client/service/installer against an isolated Gateway.
// All imports with Story runtime effects remain dynamic, after temporary paths are set.
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { cp, mkdir, readFile, readdir, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { join, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'

const [mode, baseUrl, apiKey, fixtureRoot, storyRoot, gatewayData] = process.argv.slice(2)
if (!mode || !baseUrl || !apiKey || !fixtureRoot || !storyRoot) throw Error('Expected mode, Gateway URL/key, fixture root, Story root and Gateway data root.')

// This is intentionally before the Story runtime imports below.
process.env.STORY_STUDIO_DATA_DIR ??= join(fixtureRoot, 'story-data')
process.env.GATEWAY_TEST_CONTENT_DELIVERY = '1'

const adminHeaders = {
  authorization: `Bearer ${apiKey}`,
  'content-type': 'application/json',
}
const kinds = ['series-formula', 'topic-pack', 'visual-style', 'thumbnail-recipe']
// What the shelf *supports* is a superset of what this fixture publishes. Story
// 1.4.216 added two kinds, and those are withheld from older clients at product
// level — so they belong in `supported_kinds` and not in the products this
// fixture, which asks as 1.4.213, expects back.
const supportedKinds = [...kinds, 'lettering-style', 'metadata-formula']
const storyVersion = '1.4.213'
const templates = {
  'series-formula': 'content-packs/free-starters/formula/ancient-humans',
  'topic-pack': 'content-packs/baroque-myth/topic/gods-heroes-and-epics',
  'visual-style': 'content-packs/free-starters/style/cinematic-photoreal',
  'thumbnail-recipe': 'content-packs/free-starters/thumbnail/centered-hero-headline',
}

function exactKeys(value, keys) {
  assert.deepEqual(Object.keys(value).sort(), [...keys].sort())
}

function safeRefusalDetail(body) {
  const detail = typeof body?.detail === 'string' ? body.detail : 'no-detail'
  return detail.replace(/([?&](?:grant|token|signature)=)[^&\s]*/gi, '$1[redacted]').slice(0, 512)
}

async function traceGatewayPaths(work) {
  const originalFetch = globalThis.fetch
  const paths = []
  globalThis.fetch = async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input))
    const result = await originalFetch(input, init)
    if (url.origin === new URL(baseUrl).origin) paths.push(`${init?.method ?? 'GET'} ${url.pathname} ${result.status}`)
    return result
  }
  try {
    return { result: await work(), paths }
  } finally {
    globalThis.fetch = originalFetch
  }
}

async function response(path, init = {}) {
  return fetch(`${baseUrl}${path}`, init)
}

async function adminJson(path, body) {
  const result = await response(path, { method: 'POST', headers: adminHeaders, body: JSON.stringify(body) })
  if (result.status !== 200) throw Error(`${path}: ${await result.text()}`)
  return result.json()
}

function recordFor(kind, version = '1.0.0') {
  const namespace = {
    'series-formula': 'formula', 'topic-pack': 'topic', 'visual-style': 'style', 'thumbnail-recipe': 'thumbnail',
  }[kind]
  assert.ok(namespace, `unsupported fixture kind: ${kind}`)
  const slug = kind.replaceAll('-', '.')
  const release = version.replaceAll('.', '-')
  const packId = `com.storystudio.pack.${namespace}.gateway-fixture`
  return {
    pack_id: packId,
    item_id: `com.storystudio.${namespace}.gateway-fixture`,
    version,
    publisher_id: 'com.storystudio',
    kind,
    min_app_version: '{{MIN_APP_VERSION}}',
    min_app_version_status: 'release-bound',
    source_path: `content-packs/gateway-fixture/${slug}-${release}`,
    status: 'reserved',
    dependencies: [],
    stocking_proposal: {
      family: { id: `com.storystudio.family.${namespace}.gateway-fixture`, status: 'proposed' },
      sku: { id: `com.storystudio.sku.${namespace}.gateway-fixture`, status: 'proposed' },
    },
  }
}

async function makeSource(record) {
  const destination = join(fixtureRoot, record.source_path)
  await cp(join(storyRoot, templates[record.kind]), destination, { recursive: true })
  const manifestPath = join(destination, 'pack.json')
  const manifest = JSON.parse(await readFile(manifestPath, 'utf8'))
  manifest.pack_id = record.pack_id
  manifest.publisher_id = record.publisher_id
  manifest.key_id = '{{KEY_ID}}'
  manifest.version = record.version
  manifest.min_app_version = '{{MIN_APP_VERSION}}'
  manifest.entitlement = { sku: record.stocking_proposal.sku.id, family: record.stocking_proposal.family.id }
  manifest.dependencies = []
  manifest.changelog = `Synthetic ${record.kind} integration fixture ${record.version}.`
  delete manifest.delivery_profile
  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`)
  const contentDirectory = join(destination, 'content')
  const [filename] = (await readdir(contentDirectory)).filter(name => name.endsWith('.json'))
  assert.ok(filename, `fixture source for ${record.kind} must contain one JSON document`)
  const documentPath = join(contentDirectory, filename)
  const document = JSON.parse(await readFile(documentPath, 'utf8'))
  document.id = record.item_id
  document.name = `Gateway isolated ${record.kind} ${record.version}`
  if (typeof document.description === 'string') document.description = `Synthetic isolated ${record.kind}; never a live product.`
  await writeFile(documentPath, `${JSON.stringify(document, null, 2)}\n`)
  return destination
}

async function buildArchive(record, modules) {
  const sourceDirectory = await makeSource(record)
  const outputPath = join(fixtureRoot, 'archives', `${record.kind}-${record.version}.sspack`)
  await mkdir(join(fixtureRoot, 'archives'), { recursive: true })
  const built = await modules.buildContentPack({
    sourceDirectory, outputPath, minAppVersion: '1.4.213', deliveryProfile: 'genstudio-store-receipt-v1',
  })
  const validated = await modules.validateContentPack({
    archivePath: outputPath, deliveryProfile: 'genstudio-store-receipt-v1', includeManifest: true,
  })
  assert.equal(validated.sha256, built.sha256)
  assert.equal(validated.manifest.items.length, 1)
  assert.equal(validated.manifest.items[0].kind, record.kind)
  return { ...built, bytes: await readFile(outputPath) }
}

async function importAndPublish(archive) {
  // One request: importing is the only publish path now, and the shelf reads every
  // identity fact from the archive's own manifest.
  const published = await response('/admin/api/content-library/import-and-publish', {
    method: 'POST', headers: { authorization: `Bearer ${apiKey}`, 'content-type': 'application/octet-stream' }, body: archive.bytes,
  })
  if (published.status !== 200) throw Error(`archive import-and-publish: ${await published.text()}`)
  const body = await published.json()
  exactKeys(body, ['archive', 'release', 'replayed'])
  exactKeys(body.release, ['release_id', 'pack_id', 'version', 'archive_sha256', 'bytes', 'title', 'kind', 'current', 'available', 'published_at'])
  assert.equal(body.release.archive_sha256, archive.sha256)
  assert.equal(body.replayed, false)
  return body.release
}

async function importStory() {
  // _helpers installs only Story's test safe-storage double. It is not a Store mock.
  const helpers = await import(pathToFileURL(join(storyRoot, 'electron/server/__tests__/_helpers.ts')).href)
  const temporary = await helpers.useTempDataDir()
  const settings = await import(pathToFileURL(join(storyRoot, 'electron/server/services/app-settings.ts')).href)
  const secrets = await import(pathToFileURL(join(storyRoot, 'electron/server/services/app-secrets-store.ts')).href)
  const client = await import(pathToFileURL(join(storyRoot, 'electron/server/lib/genstudio-content-store.ts')).href)
  const delivery = await import(pathToFileURL(join(storyRoot, 'electron/server/services/content-store-delivery.ts')).href)
  const state = await import(pathToFileURL(join(storyRoot, 'electron/server/content-packs/state.ts')).href)
  const commerce = await import(pathToFileURL(join(storyRoot, 'electron/server/content-store/commerce-state.ts')).href)
  const { createContentStoreRouter } = await import(pathToFileURL(join(storyRoot, 'electron/server/routes/content-store.ts')).href)
  const requireStory = createRequire(join(storyRoot, 'package.json'))
  const express = requireStory('express')
  const request = requireStory('supertest')
  await settings.setGenStudioUrl(baseUrl)
  await secrets.setApiKey('genstudio', apiKey)
  const app = express()
  app.use('/api', createContentStoreRouter({ appVersion: '1.4.213' }))
  return { temporary, settings, secrets, client, delivery, state, commerce, app, request }
}

async function revalidate() {
  // A fresh Node process proves the persisted test-double encrypted Story state is reusable.
  await import(pathToFileURL(join(storyRoot, 'electron/server/__tests__/_helpers.ts')).href)
  const delivery = await import(pathToFileURL(join(storyRoot, 'electron/server/services/content-store-delivery.ts')).href)
  const state = await import(pathToFileURL(join(storyRoot, 'electron/server/content-packs/state.ts')).href)
  await delivery.restoreContentStoreOwnership()
  const installed = await state.readContentPackState()
  const topic = Object.values(installed.packs).find(pack => pack.active_version === '1.0.0')
  assert.ok(topic, 'restart must retain the explicitly installed v1')
  process.stdout.write(`${JSON.stringify({ active: topic.active_version })}\n`)
}

function runRestart() {
  return new Promise((resolveRestart, rejectRestart) => {
    const child = spawn(process.execPath, ['--import', 'tsx', new URL(import.meta.url).pathname,
      'revalidate', baseUrl, apiKey, fixtureRoot, storyRoot, gatewayData], {
      cwd: storyRoot, env: process.env, stdio: ['ignore', 'pipe', 'pipe'],
    })
    const stdout = []
    const stderr = []
    child.stdout.on('data', chunk => stdout.push(chunk))
    child.stderr.on('data', chunk => stderr.push(chunk))
    child.once('error', rejectRestart)
    child.once('close', code => {
      if (code !== 0) return rejectRestart(Error(`restart verification failed: ${Buffer.concat(stderr)}${Buffer.concat(stdout)}`))
      resolveRestart(JSON.parse(Buffer.concat(stdout).toString('utf8')))
    })
  })
}

async function roundTrip() {
  const initial = await response('/admin/api/content-shelf', { headers: { authorization: `Bearer ${apiKey}` } })
  if (initial.status !== 200) throw Error(`content shelf route unavailable: ${await initial.text()}`)
  const initialShelf = await initial.json()
  exactKeys(initialShelf, ['configured', 'available', 'detail', 'authority', 'supported_kinds', 'releases'])
  assert.equal(initialShelf.configured, false)
  assert.deepEqual(initialShelf.supported_kinds, supportedKinds)
  const setup = await adminJson('/admin/api/content-shelf/setup', { acknowledge: true })
  exactKeys(setup, ['configured', 'available', 'detail', 'authority', 'supported_kinds', 'releases'])
  assert.equal(setup.configured, true)
  assert.ok(setup.authority)

  const packTools = await import(pathToFileURL(join(storyRoot, 'scripts/content-packs/lib.mjs')).href)
  const topicV1 = recordFor('topic-pack')
  const topicV2 = recordFor('topic-pack', '1.0.1')
  const records = [recordFor('series-formula'), topicV1, recordFor('visual-style'), recordFor('thumbnail-recipe'), topicV2]
  const archives = []
  for (const record of records) {
    archives.push({ record, ...(await buildArchive(record, packTools)) })
  }
  const v1 = archives.find(archive => archive.record === topicV1)
  const v2 = archives.find(archive => archive.record === topicV2)
  const smokeWave = archives.filter(archive => archive !== v1 && archive !== v2)
  const releases = [await importAndPublish(v1)]

  const story = await importStory()
  try {
    const wrongKey = await response('/v1/content/account', { headers: { authorization: 'Bearer wrong-key' } })
    assert.equal(wrongKey.status, 403)
    const account = await story.client.getGenStudioContentAccount(apiKey)
    const products = await story.client.listGenStudioContentProducts(apiKey, storyVersion)
    const shelves = await story.client.listGenStudioContentStoreShelves(apiKey, storyVersion, products.products.map(product => product.productId))
    const receipts = await story.client.getGenStudioContentReceipts(apiKey)
    assert.equal(account.owned.length, 1)
    assert.ok(account.owned.every(owned => owned.purchaseId === null))
    assert.equal(receipts.purchaseReceipts.length, 0)
    assert.ok(receipts.entitlements.every(entitlement => entitlement.purchaseId === null))
    assert.deepEqual(products.products.map(product => product.kind), ['topic_pack'])
    assert.equal(shelves.shelves.flatMap(shelf => shelf.productIds).length + shelves.fallbackProductIds.length, 1)

    // A pre-existing other-account anchor must survive the new authority's restore separately.
    await story.commerce.recordVerifiedContentRestore({
      account_id: 'old-account-fixture', content_account_id: 'old-account-fixture', genstudio_origin: 'https://old.example.invalid',
      key_id: 'old-key-fixture', public_key_base64: Buffer.alloc(32, 7).toString('base64'),
      receipt_canonical_json: '{}', receipt_signature: 'fixture-signature', verified_at: '2026-09-21T00:00:00.000Z',
    })
    const before = await story.request(story.app).get('/api/content-store')
    assert.equal(before.status, 200, `Story overview refusal: HTTP ${before.status} ${String(before.body?.code ?? 'no-code')}`)
    const topicProduct = before.body.account.owned.find(owned => owned.kind === 'topic_pack')
    assert.ok(topicProduct?.currentRelease)
    const installTrace = await traceGatewayPaths(() => story.request(story.app)
      .post(`/api/content-store/releases/${topicProduct.currentRelease.releaseId}/install`).send({}))
    const installed = installTrace.result
    assert.equal(installed.status, 200,
      `Story install refusal: HTTP ${installed.status} ${String(installed.body?.code ?? 'no-code')}; ${safeRefusalDetail(installed.body)}; paths=${installTrace.paths.join(',')}`)
    assert.equal(installed.body.install.activated, true)
    assert.equal(installed.body.install.version, '1.0.0')
    const after = await story.request(story.app).get('/api/content-store')
    assert.equal(after.status, 200, `Story post-install overview refusal: HTTP ${after.status} ${String(after.body?.code ?? 'no-code')}`)
    assert.equal(after.body.account.owned[0].installState, 'installed_current')
    const topicState = (await story.state.readContentPackState()).packs[topicV1.pack_id]
    assert.equal(topicState.active_version, '1.0.0')
    assert.equal((await story.commerce.readContentStoreCommerceState()).verified_restores?.length, 2)

    const grant = await story.client.issueGenStudioContentDownload(apiKey, { releaseId: topicProduct.currentRelease.releaseId })
    const archivePath = join(gatewayData, 'content-library', v1.sha256, 'archive.sspack')
    const original = await readFile(archivePath)
    const expectedArchive = { archiveSha256: grant.archiveSha256, archiveBytes: grant.archiveBytes }
    const downloaded = await story.client.downloadGenStudioContentArchive(grant.downloadUrl, expectedArchive)
    assert.deepEqual(Buffer.from(downloaded), original, 'the real Story client must first receive the exact preserved archive')
    await writeFile(archivePath, Buffer.from('tampered archive'))
    try {
      await assert.rejects(
        story.client.downloadGenStudioContentArchive(grant.downloadUrl, expectedArchive),
        error => error?.name === 'GenStudioContentStoreApiError'
          && error.status === 410 && error.code === 'CONTENT_ARCHIVE_UNAVAILABLE',
      )
    } finally {
      await writeFile(archivePath, original)
    }
    const restart = await runRestart()
    assert.equal(restart.active, '1.0.0')

    const smokeReleases = await Promise.all(smokeWave.map(importAndPublish))
    const smokeAccount = await story.client.getGenStudioContentAccount(apiKey)
    const smokeProducts = await story.client.listGenStudioContentProducts(apiKey, storyVersion)
    const smokeReceipts = await story.client.getGenStudioContentReceipts(apiKey)
    assert.equal(smokeAccount.owned.length, 4)
    assert.ok(smokeAccount.owned.every(owned => owned.purchaseId === null))
    assert.equal(smokeReceipts.purchaseReceipts.length, 0)
    assert.ok(smokeReceipts.entitlements.every(entitlement => entitlement.purchaseId === null))
    assert.deepEqual(smokeProducts.products.map(product => product.kind).sort(), kinds.map(kind => kind.replaceAll('-', '_')).sort())
    for (const release of smokeReleases) {
      const smokeInstall = await story.request(story.app)
        .post(`/api/content-store/releases/${release.release_id}/install`).send({})
      assert.equal(smokeInstall.status, 200, `Story ${release.kind} install refusal: HTTP ${smokeInstall.status} ${String(smokeInstall.body?.code ?? 'no-code')}`)
      assert.equal(smokeInstall.body.install.pack_id, release.pack_id)
      assert.equal(smokeInstall.body.install.version, release.version)
      assert.equal(smokeInstall.body.install.activated, true)
    }
    const v2Release = await importAndPublish(v2)
    const history = await story.client.listGenStudioOwnedContentReleases(apiKey, topicProduct.entitlementFamily)
    assert.deepEqual(history.releases.map(release => release.version).sort(), ['1.0.0', '1.0.1'])
    assert.equal((await story.state.readContentPackState()).packs[topicV1.pack_id].active_version, '1.0.0')
    const upgraded = { body: await story.delivery.installOwnedContentStoreRelease(v2Release.release_id) }
    assert.equal(upgraded.body.install.activated, true)
    assert.equal(upgraded.body.install.version, '1.0.1')
    assert.deepEqual(upgraded.body.install.retained_versions, ['1.0.0', '1.0.1'])
    const state = (await story.state.readContentPackState()).packs[topicV1.pack_id]
    process.stdout.write(`${JSON.stringify({
      installed_version: state.active_version, retained_versions: upgraded.body.install.retained_versions,
      published_kinds: [...releases, ...smokeReleases].map(release => release.kind).sort(), tampered_archive_refused: true,
      old_account_anchor_retained: (await story.commerce.readContentStoreCommerceState()).verified_restores?.some(row => row.account_id === 'old-account-fixture') === true,
    })}\n`)
  } finally {
    await story.temporary.cleanup()
  }
}

if (mode === 'revalidate') await revalidate()
else if (mode === 'round-trip') await roundTrip()
else throw Error(`Unknown mode: ${mode}`)
