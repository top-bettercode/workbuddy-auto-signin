import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import trae_signin as trae


NODE = shutil.which("node")
ENCRYPT_FIXTURE_JS = r"""
const fs = require('fs');
const crypto = require('crypto');
const A = Uint8Array.from([82,9,106,213,48,54,165,56,191,64,163,158,129,243,215,251,124,227,57,130,155,47,255,135,52,142,67,68,196,222,233,203,84,123,148,50,166,194,35,61,238,76,149,11,66,250,195,78,8,46,161,102,40,217,36,178,118,91,162,73,109,139,209,37]);
const B = Uint8Array.from([31,221,168,51,136,7,199,49,177,18,16,89,39,128,236,95,96,81,127,169,25,181,74,13,45,229,122,159,147,201,156,239,160,224,59,77,174,42,245,176,200,235,187,60,131,83,153,97,23,43,4,126,186,119,214,38,225,105,20,99,85,33,12,125]);
let input = '';
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
  const req = JSON.parse(input);
  const random = Buffer.alloc(32, 7);
  const salt = Buffer.from(A.map((v, i) => v ^ B[i]));
  const first = crypto.createHash('sha512').update(random).digest();
  const key = crypto.createHash('sha512').update(Buffer.concat([first, salt])).digest();
  const text = Buffer.from(JSON.stringify({token: req.token}), 'utf8');
  const hash = crypto.createHash('sha512').update(text).digest();
  const cipher = crypto.createCipheriv('aes-128-cbc', key.subarray(0, 16), key.subarray(16, 32));
  const encrypted = Buffer.concat([cipher.update(Buffer.concat([hash, text])), cipher.final()]);
  const raw = Buffer.concat([Buffer.from([116,99,5,16,0,0]), random, encrypted]);
  const storage = {'iCubeAuthInfo://icube.cloudide': raw.toString('base64')};
  if (req.device === 'dc') storage['iCubeAuthInfo://icube-dc:device-from-dc'] = {};
  if (req.device === 'telemetry') storage['telemetry.devDeviceId'] = 'device-from-telemetry';
  if (req.device === 'machine') storage['telemetry.machineId'] = 'machine-fixture';
  fs.writeFileSync(req.output, JSON.stringify(storage));
});
"""


class FakeResponse:
    def __init__(self, payload, status=200):
        self.raw = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, _limit):
        return self.raw


class FakeApi:
    def __init__(self, statuses, claim_response=None):
        self.statuses = list(statuses)
        self.claim_response = claim_response
        self.claim_calls = 0

    def status(self, retry=True):
        return self.statuses.pop(0)

    def claim(self):
        self.claim_calls += 1
        return self.claim_response


def status_payload(checked=False, enabled=True, credits=100):
    return 200, {"enable": enabled, "checked_in": checked, "credits": credits}, None


@unittest.skipUnless(NODE, "需要 node 生成合成加密夹具")
class AuthHelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Path(self.temp.name) / "User" / "globalStorage" / "storage.json"
        self.storage.parent.mkdir(parents=True)

    def make_fixture(self, token="synthetic.jwt.token", device="dc"):
        request = {"output": str(self.storage), "token": token, "device": device}
        subprocess.run([NODE, "-e", ENCRYPT_FIXTURE_JS], input=json.dumps(request).encode(),
                       check=True, capture_output=True, timeout=30)

    def run_helper(self):
        request = {"version": 1, "storageFile": str(self.storage),
                   "machineIdFile": str(Path(self.temp.name) / "machineid")}
        return subprocess.run([NODE, "-e", trae.AUTH_HELPER_JS],
                              input=json.dumps(request).encode(), capture_output=True,
                              timeout=30)

    def test_decrypts_and_extracts_device_without_leaking(self):
        self.make_fixture()
        process = self.run_helper()
        reply = json.loads(process.stdout)
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["session"]["token"], "synthetic.jwt.token")
        self.assertEqual(reply["session"]["deviceId"], "device-from-dc")
        self.assertEqual(reply["session"]["deviceIdSource"], "icube-dc")
        self.assertNotIn(b"synthetic.jwt.token", process.stderr)

    def test_device_fallbacks(self):
        self.make_fixture(device="telemetry")
        session = json.loads(self.run_helper().stdout)["session"]
        self.assertEqual(session["deviceId"], "device-from-telemetry")
        self.assertEqual(session["deviceIdSource"], "telemetry.devDeviceId")
        self.make_fixture(device="machine")
        session = json.loads(self.run_helper().stdout)["session"]
        self.assertEqual(session["deviceIdSource"], "sha256:telemetry.machineId")
        self.assertEqual(len(session["deviceId"]), 32)

    def test_rejects_missing_token(self):
        self.make_fixture(token="")
        process = self.run_helper()
        self.assertFalse(json.loads(process.stdout)["ok"])
        self.assertEqual(json.loads(process.stdout)["reason"], "TOKEN_MISSING")


class ApiTests(unittest.TestCase):
    def test_matches_client_request_contract(self):
        api = trae.Api("synthetic.jwt.token", "device-id")
        response = FakeResponse({"enable": True, "checked_in": False})
        with mock.patch.object(trae.urllib.request, "urlopen", return_value=response) as call:
            self.assertEqual(api._raw(trae.STATUS_PATH)[0], 200)
        request = call.call_args[0][0]
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b'{"req_source": 1}')
        self.assertEqual(headers["authorization"], "Cloud-IDE-JWT synthetic.jwt.token")
        self.assertEqual(headers["x-device-id"], "device-id")
        self.assertEqual(headers["content-type"], "application/json")

    def test_status_retries_but_claim_does_not(self):
        api = trae.Api("synthetic.jwt.retry-token", "device")
        with mock.patch.object(api, "_raw", side_effect=[(0, None, "URLError"),
                                                         status_payload()]) as raw, \
             mock.patch.object(trae.time, "sleep"):
            self.assertEqual(api.status()[0], 200)
            self.assertEqual(raw.call_count, 2)
        with mock.patch.object(api, "_raw", return_value=(0, None, "URLError")) as raw:
            self.assertEqual(api.claim()[0], 0)
            raw.assert_called_once_with(trae.CLAIM_PATH)


class StatusTests(unittest.TestCase):
    def test_classifies_client_states(self):
        cases = [
            (status_payload(True), "signed"),
            (status_payload(False), "not_signed"),
            (status_payload(False, False), "disabled"),
        ]
        for response, expected in cases:
            status, error = trae.parse_status(response)
            self.assertIsNone(error)
            self.assertEqual(status["state"], expected)
        response = (200, {"enable": True, "checked_in": False,
                          "did_checked_in": True}, None)
        self.assertEqual(trae.parse_status(response)[0]["state"], "signed")

    def test_rejects_invalid_protocol_and_business_errors(self):
        status, error = trae.parse_status((200, {"enable": True}, None))
        self.assertIsNone(status)
        self.assertEqual(error["result"], "ERROR")
        status, error = trae.parse_status((200, {"code": 1002}, None))
        self.assertEqual(error["result"], "AUTH_REJECTED")
        status, error = trae.parse_status((200, {"code": 9090}, None))
        self.assertEqual(error["result"], "INACTIVE")


class ClaimTests(unittest.TestCase):
    def test_already_signed_never_claims(self):
        api = FakeApi([status_payload(True)])
        out = trae.claim_once(api)
        self.assertEqual(out["result"], "ALREADY")
        self.assertEqual(api.claim_calls, 0)

    def test_dry_run_never_claims(self):
        api = FakeApi([status_payload(False)])
        out = trae.claim_once(api, dry_run=True)
        self.assertEqual(out["result"], "DRY_RUN")
        self.assertEqual(api.claim_calls, 0)

    def test_successful_claim(self):
        api = FakeApi([status_payload(False)], (200, {"code": 0}, None))
        out = trae.claim_once(api)
        self.assertEqual(out["result"], "CLAIMED")
        self.assertEqual(api.claim_calls, 1)

    def test_uncertain_claim_is_confirmed_without_repeating(self):
        api = FakeApi([status_payload(False), status_payload(True)],
                      (0, None, "URLError"))
        out = trae.claim_once(api)
        self.assertEqual(out["result"], "CLAIMED")
        self.assertTrue(out["confirmed_by_status"])
        self.assertEqual(api.claim_calls, 1)

    def test_uncertain_claim_stays_pending_without_repeating(self):
        api = FakeApi([status_payload(False), status_payload(False)],
                      (500, {}, None))
        out = trae.claim_once(api)
        self.assertEqual(out["result"], "PENDING")
        self.assertEqual(api.claim_calls, 1)

    def test_token_is_redacted_from_output(self):
        trae._sensitive_values.add("secret-token")
        self.assertEqual(trae._redact("token=secret-token"), "token=[REDACTED]")


if __name__ == "__main__":
    unittest.main()
