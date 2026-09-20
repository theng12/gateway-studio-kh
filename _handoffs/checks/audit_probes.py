"""Observe known open defects without live data or upstream requests.

Run from the checkout with .venv/bin/python _handoffs/checks/audit_probes.py.
This is a diagnostic, not a passing regression suite: the recorded outcomes
should change when the defects are fixed. Environment overrides precede imports.
The source_commit field identifies the audited baseline, not future reruns.
"""
import json, os, sys, tempfile, time
from pathlib import Path
from unittest.mock import patch
root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root))
with tempfile.TemporaryDirectory(prefix='gateway-handoff-probes-') as temporary:
    base=Path(temporary)
    os.environ.update(GATEWAY_APP_SUPPORT=temporary,GATEWAY_DATA=str(base/'data'),GATEWAY_ARTIFACTS=str(base/'artifacts'),GATEWAY_DB=str(base/'jobs.sqlite3'),GATEWAY_CREDENTIALS=str(base/'credentials.json'),GATEWAY_MODEL_CACHE=str(base/'models.json'),GATEWAY_ENV_FILE=str(base/'.env'),GATEWAY_FLEET=str(base/'fleet.json'),GATEWAY_VOICES=str(base/'voices.json'),GATEWAY_VOICES_OUT=str(base/'voices.json'),GATEWAY_NO_MIGRATE='1',STUDIOHUB_READY_FILE=str(base/'no-hub.json'),GATEWAY_API_KEY='isolated-probe',GATEWAY_HOST='127.0.0.1')
    from gateway import api,retention
    from gateway.runtime import store
    from gateway.store import JobStore
    observations={}
    path=base/'restart.sqlite3'
    restart=JobStore(path)
    job,_=restart.submit(modality='chat',model_id='mock',params={},idempotency_key='restart')
    restart.mark_running(job['id']);restart.close();restart=JobStore(path)
    row=restart.get(job['id'])
    observations['restart']={'state':row['state'],'next_action_at':row['next_action_at'],'due_count':len(restart.due(time.time()+3600))}
    restart.close()
    job,_=store.submit(modality='chat',model_id='mock',params={},idempotency_key='cancel-running')
    store.mark_running(job['id']);store.cancel(job['id']);before=store.get(job['id'])['state'];store.succeed(job['id'],result={'text':'late result'})
    observations['running_cancel']={'before_late_completion':before,'after_late_completion':store.get(job['id'])['state']}
    upload=base/'artifacts/uploads/pending.wav';upload.parent.mkdir(parents=True);upload.write_bytes(b'x'*(2*1024*1024));old=time.time()-7200;os.utime(upload,(old,old))
    job,_=store.submit(modality='transcription',model_id='mock',params={'_upload_path':str(upload)},idempotency_key='pending-input')
    retention.save(max_bytes=1024*1024)
    result=retention.prune()
    observations['queued_input_pruned']={'state':store.get(job['id'])['state'],'input_exists':upload.exists(),'deleted_by_size':result['deleted_by_size']}
    api._UPLOADS['quote-a']=(b'mock','a.wav','audio/wav',time.time(),'model-a')
    observations['quote_model']={'quoted':'model-a','confirmed':'model-b','resolved':api._quote_model('quote-a','model-b')}
    artifact=base/'artifacts/result.png';artifact.write_bytes(b'mock')
    row={'state':'succeeded','asset_path':str(artifact),'media_type':'image/png'}
    real_read=Path.read_bytes
    def disappearing_read(p):
        if p==artifact:p.unlink()
        return real_read(p)
    try:
        with patch.object(Path,'read_bytes',disappearing_read):api._artifact_response(row,'image/png')
    except Exception as error:
        observations['artifact_delete_race']={'exception':type(error).__name__,'typed_gateway_error':isinstance(error,api.GatewayError)}
    store.close()
    report={'source_commit':'fcfde43','method':'isolated offline probes, no upstream dispatch','observations':observations}
    print(json.dumps(report,indent=2))
