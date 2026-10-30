"""The Gateway exposes each Mac's own preferred-work policy through its controller."""
import json
import os
import subprocess
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from tests._isolation import sandbox_env
sandbox_env("gateway-preferred-work-")
os.environ.setdefault("GATEWAY_HOST", "127.0.0.1")

from gateway import api, startup_control


class PreferredWorkTests(unittest.TestCase):
    def setUp(self):
        self.policy = {"preferred_choices": ["chat", "voice"],
                       "preferred_work": {"enabled": True, "modality": "chat", "idle_seconds": 300}}
        self.requests = []
        self.client = TestClient(api.app, client=("127.0.0.1", 41001))
        self.path = "/admin/api/fleet/controllers/site/machines/mac/work-policy"
        self.target = patch.object(startup_control, "_target", return_value=("http://controller", "token"))
        self.target.start()
        self.addCleanup(self.target.stop)
        self.audit = patch.object(startup_control, "audit")
        self.audit.start()
        self.addCleanup(self.audit.stop)

    def transport(self, handler):
        def respond(request):
            self.requests.append(request)
            return handler(request)
        return httpx.Client(transport=httpx.MockTransport(respond))

    def test_read_returns_mac_choices_and_current_value(self):
        with self.transport(lambda _: httpx.Response(200, json={"ok": True, "work_policy": self.policy})) as client:
            result = startup_control.get_node_work_policy("site", "mac", client=client)
        self.assertEqual(result, self.policy)
        self.assertEqual(self.requests[0].url.path, "/api/hub/fleet/nodes/mac/work-policy")
        self.assertEqual(self.requests[0].headers["x-hub-token"], "token")

    def test_save_relays_the_exact_body_and_returns_confirmation(self):
        preference = self.policy["preferred_work"]
        with self.transport(lambda _: httpx.Response(200, json={"ok": True, "work_policy": self.policy})) as client:
            result = startup_control.set_node_preferred_work("site", "mac", preference, client=client)
        self.assertEqual(result, self.policy)
        self.assertEqual([r.method for r in self.requests], ["GET", "POST"])
        self.assertEqual(json.loads(self.requests[1].content), {"preferred_work": preference})
        with patch.object(startup_control, "set_node_preferred_work", return_value=self.policy) as save:
            response = self.client.post(self.path, json={"preferred_work": preference})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.policy)
        save.assert_called_once_with("site", "mac", preference)

    def test_unreachable_mac_refuses_before_post(self):
        with self.transport(lambda _: httpx.Response(502, json={"ok": False, "code": "NODE_UNREACHABLE", "detail": "Mac did not answer"})) as client:
            with self.assertRaises(startup_control.StartupError) as error:
                startup_control.set_node_preferred_work("site", "mac", self.policy["preferred_work"], client=client)
        self.assertEqual(error.exception.body["code"], "NODE_UNREACHABLE")
        self.assertEqual([r.method for r in self.requests], ["GET"])

    def test_mac_refusal_for_unoffered_value_reaches_api(self):
        refusal = {"detail": "preferred_work.modality must be a generation function this Mac serves"}
        def handler(request):
            return httpx.Response(200, json={"ok": True, "work_policy": self.policy}) if request.method == "GET" else httpx.Response(400, json=refusal)
        clients = [self.transport(handler), self.transport(handler)]
        with patch.object(startup_control.httpx, "Client", side_effect=clients):
            response = self.client.post(self.path, json={"preferred_work": {"enabled": True, "modality": "other", "idle_seconds": 300}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], refusal["detail"])
        self.assertEqual(json.loads(self.requests[-1].content)["preferred_work"]["modality"], "other")

    def test_queued_after_read_is_not_confirmation(self):
        def handler(request):
            return httpx.Response(200, json={"ok": True, "work_policy": self.policy}) if request.method == "GET" else httpx.Response(202, json={"ok": True, "queued": True})
        with self.transport(handler) as client:
            result = startup_control.set_node_preferred_work("site", "mac", self.policy["preferred_work"], client=client)
        self.assertTrue(result["queued"])
        self.assertNotIn("preferred_work", result)

    def test_web_control_reads_choices_and_marks_queued_unconfirmed(self):
        html = (Path(__file__).resolve().parents[1] / "gateway/admin_ui.html").read_text()
        script = "// ---- Preferred work" + html.split("// ---- Preferred work", 1)[1].split("// ---- Work priorities", 1)[0]
        harness = r'''
const assert = require('assert');
let policy = {preferred_choices:['chat','voice'], preferred_work:{enabled:true, modality:'chat', idle_seconds:300}};
let shown, note, posted;
const fields = {'fleet-preferred-choice':{value:'chat'}, 'fleet-preferred-seconds':{value:'300',validity:{valid:true}}};
const document = {getElementById: id => fields[id]};
const fleetMachineById = () => ({label:'Mac',machine:'mac'});
const fleetSeg = encodeURIComponent;
const esc = s => String(s);
const fleetPriorityTitle = s => s;
const fleetPayload = body => ({body:JSON.stringify(body)});
const fleetDialogOpen = s => {shown=s};
const fleetDialogClose = () => {shown=null};
const fleetNote = (_, s) => {note=s};
const fleetDialogNote = s => {note=s};
let reply = policy;
const api = async (_, options) => {if(options) posted=JSON.parse(options.body); return reply;};
''' + script + r'''
(async () => {
 const button = {};
 await fleetPreferredOpen(button,'site','mac');
 assert(shown.body.includes('value="chat" selected'));
 assert(shown.body.includes('value="voice"'));
 assert(shown.body.includes('value="300"'));
 reply = {queued:true};
 await fleetPreferredSave(button, shown);
 assert.equal(posted.preferred_work.idle_seconds,300);
 assert(note.includes('not confirmed'));
 reply = policy;
 await fleetPreferredSave(button, shown);
 assert(note.includes('confirmed by this Mac'));
 reply = {};
 await fleetPreferredOpen(button,'site','mac');
 assert(note.includes('unknown'));
})().catch(e => { console.error(e); process.exit(1); });
'''
        result = subprocess.run(["node", "-e", harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_non_object_is_refused(self):
        response = self.client.post(self.path, json={"preferred_work": "chat"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "BAD_PREFERRED_WORK")
