// Isolated registry issued by the real authoring API; never edits the Story checkout.
import {cp, mkdir, symlink, writeFile} from 'node:fs/promises';
import {join, resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {homedir} from 'node:os';

export async function prepareAuthoringFixture(root, storySource) {
  const source = resolve(storySource);
  const scripts = join(root, 'scripts/content-packs');
  await mkdir(scripts, {recursive:true});
  for (const name of ['authoring-registry.mjs','lib.mjs']) {
    await cp(join(source,'scripts/content-packs',name),join(scripts,name));
  }
  for (const name of ['electron','node_modules']) await symlink(join(source,name),join(root,name),'dir');
  await mkdir(join(root,'content-packs'));
  const registryPath = join(root,'content-packs/authoring-registry.json');
  await writeFile(registryPath,JSON.stringify({revision:1,batches:[],transitions:[],records:[]}));
  await cp(join(source,'content-packs/release-floor.json'),join(root,'content-packs/release-floor.json'));
  const {reserveAuthoringBatch} = await import(pathToFileURL(join(source,'scripts/content-packs/authoring-registry.mjs')));
  const record = {
    pack_id:'com.storystudio.pack.topic.gateway-fixture', item_id:'com.storystudio.topic.gateway-fixture',
    version:'1.0.0',publisher_id:'com.storystudio',kind:'topic-pack',
    min_app_version:'{{MIN_APP_VERSION}}',min_app_version_status:'release-bound',
    source_path:'content-packs/fixture/topic/gateway-fixture',status:'reserved',dependencies:[],
    stocking_proposal:{family:{id:'com.storystudio.family.topic.gateway-fixture',status:'proposed'},
      sku:{id:'com.storystudio.sku.topic.gateway-fixture',status:'proposed'}},
  };
  await reserveAuthoringBatch({registryPath,repositoryRoot:root,request:{
    expected_revision:1,coordinator:{task:'Gateway isolated authoring verification',branch:'fixture-only'},
    proposal_ref:'Synthetic test fixture, not a product reservation',records:[record],
  }});
  return {source:root,key:record.pack_id+'@'+record.version,document:{
    schema_version:1,kind:'topic-pack',id:record.item_id,name:'River journeys — synthetic fixture',
    description:'An isolated demonstration of a Topic Pack draft. Not an installed or purchased product.',
    compatible_formula_ids:['com.storystudio.formula.fixture'],
    subjects:['River transport'],episode_objects:['A ferry crossing'],time_place_scopes:['A riverside town'],
    mechanisms:['Seasonal water levels'],evidence_lenses:['Maps and documented observations'],
    exclusions:['Unsupported historical claims'],max_siblings_per_subject:3,
    prompt_context:'Explain one crossing and its effect on daily life. Separate observations from inference.',
  }};
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const root = process.argv[2];
  if (!root) throw Error('Pass an empty temporary fixture directory.');
  const source = process.argv[3] || process.env.GATEWAY_STORY_SOURCE || join(homedir(),'Developer/story-studio-kh');
  console.log(JSON.stringify(await prepareAuthoringFixture(resolve(root),source)));
}
