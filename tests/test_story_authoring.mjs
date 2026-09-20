import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { cp, mkdir, mkdtemp, readFile, rm, stat, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test, { after, before } from 'node:test'

const ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)))
const CLI = join(ROOT, 'gateway', 'story_authoring.mjs')
const STORY_SOURCE = process.env.GATEWAY_STORY_SOURCE || '/build/Developer/story-studio-kh'
const STORY_REGISTRY = join(STORY_SOURCE, 'scripts', 'content-packs', 'authoring-registry.mjs')
const STORY_LIB = join(STORY_SOURCE, 'scripts', 'content-packs', 'lib.mjs')
const STORY_RELEASE_FLOOR = join(STORY_SOURCE, 'content-packs', 'release-floor.json')

const VALID_DOCUMENT = {
  kind: 'topic-pack',
  schema_version: 1,
  id: 'com.storystudio.topic.fixture-topic',
  name: 'Fixture Topic Pack',
  description: 'A small isolated Topic Pack used only for bridge contract tests.',
  compatible_formula_ids: ['com.storystudio.formula.fixture-formula'],
  subjects: ['one bounded fixture subject'],
  episode_objects: ['one fixture object'],
  time_place_scopes: ['one fixture time and place'],
  mechanisms: ['one fixture mechanism'],
  evidence_lenses: ['one fixture evidence lens'],
  exclusions: [],
  max_siblings_per_subject: 4,
  prompt_context: 'Use the fixture subject to produce a careful, specific episode idea.',
}

const TEMPLATE_DOCUMENT = {
  ...VALID_DOCUMENT,
  id: 'com.storystudio.topic.fixture-template',
  name: 'Fixture Authored Template',
  description: 'An authored single-document Topic Pack used as a read-only template.',
}

const FIXTURE = {
  root: undefined,
  outputRoot: undefined,
  registryPath: undefined,
  reserved: {
    pack_id: 'com.storystudio.pack.topic.fixture-topic',
    item_id: 'com.storystudio.topic.fixture-topic',
    version: '1.0.0',
    publisher_id: 'com.storystudio',
    min_app_version: '{{MIN_APP_VERSION}}',
    min_app_version_status: 'release-bound',
    kind: 'topic-pack',
    source_path: 'content-packs/fixture/topic/fixture-topic',
    status: 'reserved',
    dependencies: [],
    stocking_proposal: {
      family: { id: 'com.storystudio.family.topic.fixture-topic', status: 'proposed' },
      sku: { id: 'com.storystudio.sku.topic.fixture-topic', status: 'proposed' },
    },
  },
  template: {
    pack_id: 'com.storystudio.pack.topic.fixture-template',
    item_id: 'com.storystudio.topic.fixture-template',
    version: '1.0.0',
    publisher_id: 'com.storystudio',
    min_app_version: '{{MIN_APP_VERSION}}',
    min_app_version_status: 'release-bound',
    kind: 'topic-pack',
    source_path: 'content-packs/fixture/topic/fixture-template',
    status: 'reserved',
    dependencies: [],
    stocking_proposal: {
      family: { id: 'com.storystudio.family.topic.fixture-template', status: 'proposed' },
      sku: { id: 'com.storystudio.sku.topic.fixture-template', status: 'proposed' },
    },
  },
  withdrawn: {
    pack_id: 'com.storystudio.pack.topic.fixture-withdrawn',
    item_id: 'com.storystudio.topic.fixture-withdrawn',
    version: '1.0.0',
    publisher_id: 'com.storystudio',
    min_app_version: '{{MIN_APP_VERSION}}',
    min_app_version_status: 'release-bound',
    kind: 'topic-pack',
    source_path: 'content-packs/fixture/topic/fixture-withdrawn',
    status: 'reserved',
    dependencies: [],
    stocking_proposal: {
      family: { id: 'com.storystudio.family.topic.fixture-withdrawn', status: 'proposed' },
      sku: { id: 'com.storystudio.sku.topic.fixture-withdrawn', status: 'proposed' },
    },
  },
}

let reserveAuthoringBatch
let transitionAuthoringRecord
let validateContentPack

function keyFor(record) {
  return `${record.pack_id}@${record.version}`
}

function digest(bytes) {
  return createHash('sha256').update(bytes).digest('hex')
}

async function createAuthoredSource(root, record, document) {
  const source = join(root, record.source_path)
  await mkdir(join(source, 'content'), { recursive: true })
  await writeFile(join(source, 'pack.json'), `${JSON.stringify({
    pack_format: 1,
    pack_id: record.pack_id,
    publisher_id: record.publisher_id,
    key_id: '{{KEY_ID}}',
    version: record.version,
    min_app_version: record.min_app_version,
    lifecycle: 'active',
    entitlement: {
      sku: record.stocking_proposal.sku.id,
      family: record.stocking_proposal.family.id,
    },
    dependencies: record.dependencies,
  }, null, 2)}\n`)
  await writeFile(join(source, 'content', 'topic-pack.json'), `${JSON.stringify(document, null, 2)}\n`)
}

async function installStoryModules(root) {
  await mkdir(join(root, 'scripts', 'content-packs'), { recursive: true })
  await mkdir(join(root, 'content-packs'), { recursive: true })
  await cp(STORY_REGISTRY, join(root, 'scripts', 'content-packs', 'authoring-registry.mjs'))
  const libSource = await readFile(STORY_LIB, 'utf8')
  const precommit = '  await onBeforeOutputCommit?.()'
  assert.equal(libSource.split(precommit).length, 2, 'actual Story builder precommit hook must be present once')
  await writeFile(join(root, 'scripts', 'content-packs', 'lib.mjs'), libSource.replace(precommit, `
  if (process.env.GATEWAY_TEST_PRECOMMIT_MARKER) {
    await writeFile(process.env.GATEWAY_TEST_PRECOMMIT_MARKER, 'ready', { flag: 'wx' })
    while (!await lstat(\`${'${process.env.GATEWAY_TEST_PRECOMMIT_MARKER}'}.release\`).catch(() => undefined)) {
      await new Promise(resolve => setTimeout(resolve, 5))
    }
  }
${precommit}`))
  await cp(STORY_RELEASE_FLOOR, join(root, 'content-packs', 'release-floor.json'))
  await symlink(join(STORY_SOURCE, 'electron'), join(root, 'electron'), 'dir')
  await symlink(join(STORY_SOURCE, 'node_modules'), join(root, 'node_modules'), 'dir')
}

async function runCli(root, request, outputPath, environment) {
  return runCliRaw(root, JSON.stringify(request), outputPath, environment)
}

async function runCliRaw(root, input, outputPath, environment = {}) {
  const args = [CLI, root]
  if (outputPath !== undefined) args.push(outputPath)
  return new Promise((resolveResult, reject) => {
    const child = spawn(process.execPath, args, {
      cwd: ROOT,
      env: { ...process.env, NODE_NO_WARNINGS: '1', ...environment },
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    const stdout = []
    const stderr = []
    child.stdout.on('data', chunk => stdout.push(chunk))
    child.stderr.on('data', chunk => stderr.push(chunk))
    child.once('error', reject)
    child.once('close', code => {
      const stdoutText = Buffer.concat(stdout).toString('utf8')
      const stderrText = Buffer.concat(stderr).toString('utf8')
      let json
      try {
        json = JSON.parse(stdoutText)
      } catch (error) {
        reject(new Error(`CLI did not return one JSON result: ${error.message}; stdout=${stdoutText}; stderr=${stderrText}`))
        return
      }
      resolveResult({ code, json, stdout: stdoutText, stderr: stderrText })
    })
    child.stdin.end(input)
  })
}

function expectedReservation(record = FIXTURE.reserved, receiptRef = FIXTURE.receiptRef) {
  return {
    key: keyFor(record),
    item_id: record.item_id,
    pack_id: record.pack_id,
    version: record.version,
    receipt_ref: receiptRef,
  }
}

async function waitForPath(path) {
  const deadline = Date.now() + 10_000
  while (Date.now() < deadline) {
    if (await stat(path).catch(() => undefined)) return
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  throw new Error(`timed out waiting for fixture path ${path}`)
}

async function assertActionError(root, request, outputPath) {
  const result = typeof request === 'string'
    ? await runCliRaw(root, request, outputPath)
    : await runCli(root, request, outputPath)
  assert.notEqual(result.code, 0)
  assert.deepEqual(Object.keys(result.json), ['error'])
  assert.equal(typeof result.json.error, 'string')
  assert.ok(result.json.error.length > 0 && result.json.error.length <= 400)
  assert.equal(result.stderr, '')
  return result.json.error
}

before(async () => {
  ({ reserveAuthoringBatch, transitionAuthoringRecord } = await import(pathToFileURL(STORY_REGISTRY).href))
  ;({ validateContentPack } = await import(pathToFileURL(STORY_LIB).href))

  FIXTURE.root = await mkdtemp(join(tmpdir(), 'gateway-story-bridge-'))
  FIXTURE.outputRoot = await mkdtemp(join(tmpdir(), 'gateway-story-bridge-output-'))
  FIXTURE.registryPath = join(FIXTURE.root, 'content-packs', 'authoring-registry.json')
  await installStoryModules(FIXTURE.root)
  await mkdir(join(FIXTURE.root, 'content-packs'), { recursive: true })
  await writeFile(FIXTURE.registryPath, `${JSON.stringify({ revision: 1, batches: [], transitions: [], records: [] }, null, 2)}\n`)

  const reservationRequest = {
    expected_revision: 1,
    coordinator: { task: 'gateway-story-bridge-tests', branch: 'fixture-only' },
    proposal_ref: 'fixture proposal only',
    records: [FIXTURE.reserved, FIXTURE.template, FIXTURE.withdrawn],
  }
  const reservation = await reserveAuthoringBatch({
    registryPath: FIXTURE.registryPath,
    repositoryRoot: FIXTURE.root,
    request: reservationRequest,
  })
  FIXTURE.receiptRef = reservation.receipt_ref
  FIXTURE.batchRevision = reservation.revision

  await createAuthoredSource(FIXTURE.root, FIXTURE.template, TEMPLATE_DOCUMENT)
  await transitionAuthoringRecord({
    registryPath: FIXTURE.registryPath,
    repositoryRoot: FIXTURE.root,
    request: {
      expected_revision: reservation.revision,
      coordinator: reservationRequest.coordinator,
      receipt_ref: reservation.receipt_ref,
      pack_id: FIXTURE.template.pack_id,
      version: FIXTURE.template.version,
      changes: { status: 'authored' },
    },
  })
  await transitionAuthoringRecord({
    registryPath: FIXTURE.registryPath,
    repositoryRoot: FIXTURE.root,
    request: {
      expected_revision: reservation.revision + 1,
      coordinator: reservationRequest.coordinator,
      receipt_ref: reservation.receipt_ref,
      pack_id: FIXTURE.withdrawn.pack_id,
      version: FIXTURE.withdrawn.version,
      changes: { status: 'withdrawn' },
    },
  })
})

after(async () => {
  if (FIXTURE.root) await rm(FIXTURE.root, { recursive: true, force: true })
  if (FIXTURE.outputRoot) await rm(FIXTURE.outputRoot, { recursive: true, force: true })
})

test('catalogue exposes only reserved Topic Pack reservations and authored single-document templates', async () => {
  const result = await runCli(FIXTURE.root, { action: 'catalogue' })
  assert.equal(result.code, 0)
  assert.equal(result.stderr, '')
  assert.deepEqual(result.json.reservations, [{
    key: keyFor(FIXTURE.reserved),
    item_id: FIXTURE.reserved.item_id,
    pack_id: FIXTURE.reserved.pack_id,
    version: FIXTURE.reserved.version,
    receipt_ref: FIXTURE.receiptRef,
  }])
  assert.deepEqual(result.json.templates, [{
    key: keyFor(FIXTURE.template),
    name: TEMPLATE_DOCUMENT.name,
  }])
  assert.equal(result.json.registry_revision, 4)
})

test('reservation returns the exact reserved identity after receipt verification', async () => {
  const result = await runCli(FIXTURE.root, { action: 'reservation', key: keyFor(FIXTURE.reserved) })
  assert.equal(result.code, 0)
  assert.deepEqual(result.json, {
    key: keyFor(FIXTURE.reserved),
    item_id: FIXTURE.reserved.item_id,
    pack_id: FIXTURE.reserved.pack_id,
    version: FIXTURE.reserved.version,
    receipt_ref: FIXTURE.receiptRef,
  })
})

test('template returns a shared-schema-validated authored Topic Pack document', async () => {
  const result = await runCli(FIXTURE.root, { action: 'template', key: keyFor(FIXTURE.template) })
  assert.equal(result.code, 0)
  assert.deepEqual(result.json, { document: TEMPLATE_DOCUMENT })
})

test('validate returns the exact active Store manifest derived from the reserved identity', async () => {
  const result = await runCli(FIXTURE.root, {
    action: 'validate',
    key: keyFor(FIXTURE.reserved),
    expected_reservation: expectedReservation(),
    document: VALID_DOCUMENT,
  })
  assert.equal(result.code, 0, JSON.stringify(result.json))
  assert.deepEqual(result.json.document, VALID_DOCUMENT)
  const documentBytes = Buffer.from(JSON.stringify(VALID_DOCUMENT))
  assert.deepEqual(result.json.manifest, {
    pack_format: 1,
    pack_id: FIXTURE.reserved.pack_id,
    publisher_id: FIXTURE.reserved.publisher_id,
    version: FIXTURE.reserved.version,
    min_app_version: '1.0.843',
    lifecycle: 'active',
    delivery_profile: 'genstudio-store-receipt-v1',
    entitlement: {
      sku: FIXTURE.reserved.stocking_proposal.sku.id,
      family: FIXTURE.reserved.stocking_proposal.family.id,
    },
    dependencies: [],
    items: [{
      id: FIXTURE.reserved.item_id,
      kind: 'topic-pack',
      schema_version: 1,
      path: 'content/topic-pack.json',
    }],
    bundles: [],
    files: [{
      path: 'content/topic-pack.json',
      sha256: digest(documentBytes),
      bytes: documentBytes.byteLength,
      mime: 'application/json',
    }],
  })
  assert.equal(Object.hasOwn(result.json.manifest, 'key_id'), false)
})

test('build exports a consumer-verifiable deterministic Store archive to fresh output files', async () => {
  const outputOne = join(FIXTURE.outputRoot, 'fixture-one.sspack')
  const outputTwo = join(FIXTURE.outputRoot, 'fixture-two.sspack')
  const request = {
    action: 'build', key: keyFor(FIXTURE.reserved),
    expected_reservation: expectedReservation(), document: VALID_DOCUMENT,
  }
  const first = await runCli(FIXTURE.root, request, outputOne)
  const second = await runCli(FIXTURE.root, request, outputTwo)
  assert.equal(first.code, 0, JSON.stringify(first.json))
  assert.equal(second.code, 0, JSON.stringify(second.json))
  assert.equal(first.stderr, '')
  assert.equal(second.stderr, '')
  assert.deepEqual(first.json, second.json)
  assert.equal(first.json.pack_id, FIXTURE.reserved.pack_id)
  assert.equal(first.json.version, FIXTURE.reserved.version)
  assert.match(first.json.sha256, /^[a-f0-9]{64}$/)

  const firstBytes = await readFile(outputOne)
  const secondBytes = await readFile(outputTwo)
  assert.deepEqual(firstBytes, secondBytes)
  assert.equal(first.json.sha256, digest(firstBytes))
  assert.equal(first.json.bytes, firstBytes.byteLength)
  assert.deepEqual(await validateContentPack({
    archivePath: outputOne,
    deliveryProfile: 'genstudio-store-receipt-v1',
    includeManifest: true,
  }), {
    packId: FIXTURE.reserved.pack_id,
    version: FIXTURE.reserved.version,
    sha256: digest(firstBytes),
    manifest: {
      pack_format: 1,
      pack_id: FIXTURE.reserved.pack_id,
      publisher_id: FIXTURE.reserved.publisher_id,
      version: FIXTURE.reserved.version,
      min_app_version: '1.0.843',
      lifecycle: 'active',
      delivery_profile: 'genstudio-store-receipt-v1',
      entitlement: {
        sku: FIXTURE.reserved.stocking_proposal.sku.id,
        family: FIXTURE.reserved.stocking_proposal.family.id,
      },
      dependencies: [],
      items: [{
        id: FIXTURE.reserved.item_id,
        kind: 'topic-pack',
        schema_version: 1,
        path: 'content/topic-pack.json',
      }],
      bundles: [],
      files: [{
        path: 'content/topic-pack.json',
        sha256: digest(Buffer.from(JSON.stringify(VALID_DOCUMENT))),
        bytes: Buffer.byteLength(JSON.stringify(VALID_DOCUMENT)),
        mime: 'application/json',
      }],
    },
  })
})

test('unknown and withdrawn identities are not usable authoring reservations', async () => {
  const unknownError = await assertActionError(FIXTURE.root, {
    action: 'reservation',
    key: 'com.storystudio.pack.topic.no-such-reservation@1.0.0',
  })
  assert.match(unknownError, /reservation|registry/i)

  const withdrawnError = await assertActionError(FIXTURE.root, {
    action: 'reservation',
    key: keyFor(FIXTURE.withdrawn),
  })
  assert.match(withdrawnError, /reserved|withdrawn|registry/i)
})

test('validate rejects a document whose ID does not match the reserved item identity', async () => {
  const mismatched = { ...VALID_DOCUMENT, id: 'com.storystudio.topic.not-the-reservation' }
  const error = await assertActionError(FIXTURE.root, {
    action: 'validate',
    key: keyFor(FIXTURE.reserved),
    expected_reservation: expectedReservation(),
    document: mismatched,
  })
  assert.match(error, /identity|item|id/i)
})

test('validate rejects shared-schema violations without echoing document content', async () => {
  const sentinel = 'TOP_SECRET_FIXTURE_SENTINEL'
  const invalid = { ...VALID_DOCUMENT, unexpected_field: sentinel }
  const error = await assertActionError(FIXTURE.root, {
    action: 'validate',
    key: keyFor(FIXTURE.reserved),
    expected_reservation: expectedReservation(),
    document: invalid,
  })
  assert.match(error, /schema|document|contract|unrecognized|unknown/i)
  assert.equal(error.includes(sentinel), false)
})

test('missing Story dependencies return an actionable safe error', async () => {
  const missingRoot = join(FIXTURE.root, 'missing-story-source')
  const error = await assertActionError(missingRoot, { action: 'catalogue' })
  assert.match(error, /Story|source|dependency|unavailable/i)
  assert.equal(error.includes(missingRoot), false)
})

test('the CLI rejects malformed, duplicate-key, oversized, and symlink output inputs', async () => {
  const malformed = await assertActionError(FIXTURE.root, '{"action":"catalogue","value":NaN}')
  assert.match(malformed, /JSON|request|finite|invalid/i)

  const duplicate = await assertActionError(FIXTURE.root, '{"action":"catalogue","action":"reservation"}')
  assert.match(duplicate, /duplicate|JSON|request/i)

  const oversized = `{"action":"catalogue","padding":"${'x'.repeat(4 * 1024 * 1024)}"}`
  const oversizedError = await assertActionError(FIXTURE.root, oversized)
  assert.match(oversizedError, /large|size|byte|bound|request/i)

  const outputTarget = join(FIXTURE.outputRoot, 'target.sspack')
  const outputLink = join(FIXTURE.outputRoot, 'link.sspack')
  await writeFile(outputTarget, 'existing')
  await symlink(outputTarget, outputLink)
  const symlinkError = await assertActionError(FIXTURE.root, {
    action: 'build',
    key: keyFor(FIXTURE.reserved),
    expected_reservation: expectedReservation(),
    document: VALID_DOCUMENT,
  }, outputLink)
  assert.match(symlinkError, /symlink|symbolic|output|exists/i)
  assert.equal((await stat(outputTarget)).isFile(), true)
})

test('validate rejects a receipt that differs from the saved reservation identity', async () => {
  const error = await assertActionError(FIXTURE.root, {
    action: 'validate',
    key: keyFor(FIXTURE.reserved),
    expected_reservation: expectedReservation(FIXTURE.reserved, `authoring:${'0'.repeat(64)}`),
    document: VALID_DOCUMENT,
  })
  assert.match(error, /receipt|reservation.*changed|saved.*identity/i)
})

test('build rechecks the saved reservation immediately before output commit', async () => {
  const marker = join(FIXTURE.outputRoot, 'precommit-marker')
  const output = join(FIXTURE.outputRoot, 'precommit-change.sspack')
  const pending = runCli(FIXTURE.root, {
    action: 'build',
    key: keyFor(FIXTURE.reserved),
    expected_reservation: expectedReservation(),
    document: VALID_DOCUMENT,
  }, output, { GATEWAY_TEST_PRECOMMIT_MARKER: marker })
  await waitForPath(marker)
  const registry = JSON.parse(await readFile(FIXTURE.registryPath, 'utf8'))
  await transitionAuthoringRecord({
    registryPath: FIXTURE.registryPath,
    repositoryRoot: FIXTURE.root,
    request: {
      expected_revision: registry.revision,
      coordinator: { task: 'gateway-story-bridge-tests', branch: 'fixture-only' },
      receipt_ref: FIXTURE.receiptRef,
      pack_id: FIXTURE.reserved.pack_id,
      version: FIXTURE.reserved.version,
      changes: { status: 'withdrawn' },
    },
  })
  await writeFile(`${marker}.release`, 'continue')
  const result = await pending
  assert.notEqual(result.code, 0)
  assert.match(result.json.error, /reserved|receipt|reservation.*changed/i)
  assert.equal(await stat(output).catch(() => undefined), undefined)
})
