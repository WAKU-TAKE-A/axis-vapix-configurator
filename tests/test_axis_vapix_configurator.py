"""axis_vapix_configurator のテスト（標準ライブラリのみ、実ネットワークに出ない）。"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import axis_vapix_configurator as avc


def make_args(argv):
    return avc.parse_args(argv)


BASE = ["--ip", "192.168.0.90", "--pass", "passA"]


class TestExpand(unittest.TestCase):
    def test_none(self):
        self.assertEqual(avc.expand(None, 3, "x"), [None, None, None])

    def test_single_broadcast(self):
        self.assertEqual(avc.expand(["a"], 3, "x"), ["a", "a", "a"])

    def test_n_values(self):
        self.assertEqual(avc.expand(["a", "b"], 2, "x"), ["a", "b"])

    def test_count_mismatch(self):
        with self.assertRaises(ValueError):
            avc.expand(["a", "b"], 3, "x")


class TestValidation(unittest.TestCase):
    def test_restore_combined_with_hz_is_error(self):
        args = make_args(BASE + ["--restore", "--hz", "60"])
        with self.assertRaises(ValueError):
            avc.build_plans(args)

    def test_restore_alone_is_ok(self):
        plans = avc.build_plans(make_args(BASE + ["--restore"]))
        self.assertEqual(len(plans), 1)

    def test_invalid_hz(self):
        args = make_args(BASE + ["--hz", "55"])
        with self.assertRaises(ValueError):
            avc.build_plans(args)

    def test_duplicate_new_ip(self):
        args = make_args(
            ["--ip", "192.168.0.90", "192.168.0.91", "--pass", "p",
             "--new-ip", "10.0.0.1", "10.0.0.1"])
        with self.assertRaises(ValueError):
            avc.build_plans(args)

    def test_broadcast_and_per_device(self):
        plans = avc.build_plans(make_args(
            ["--ip", "192.168.0.90", "192.168.0.91",
             "--user", "root", "--pass", "pA", "pB",
             "--hz", "60", "--new-ip", "10.0.0.1", "10.0.0.2"]))
        self.assertEqual([p.user for p in plans], ["root", "root"])
        self.assertEqual([p.password for p in plans], ["pA", "pB"])
        self.assertEqual([p.hz for p in plans], ["60", "60"])
        self.assertEqual([p.new_ip for p in plans], ["10.0.0.1", "10.0.0.2"])
        self.assertEqual([p.ntp for p in plans], [None, None])

    def test_dump_params_combination_is_error(self):
        args = make_args(BASE + ["--dump-params", "--ntp", "192.168.10.5"])
        with self.assertRaises(ValueError):
            avc.build_plans(args)


class TestGroupingAndCandidates(unittest.TestCase):
    def test_group_by_network(self):
        plans = [avc.DevicePlan(ip=ip, user="root", password="p")
                 for ip in ("192.168.0.90", "192.168.0.91",
                            "192.168.1.90", "10.0.0.5")]
        groups = avc.group_by_network(plans)
        self.assertEqual(sorted(groups), ["10.0.0", "192.168.0", "192.168.1"])
        self.assertEqual(len(groups["192.168.0"]), 2)

    def test_select_candidate_skips_used(self):
        # .250 は ping 応答あり、.251 は ARP にあり、.252 は既存 IP、.253 が空き
        ping_fn = lambda ip: ip == "192.168.0.250"
        cand = avc.select_candidate(
            "192.168.0", existing_ips={"192.168.0.252"},
            target_ips={"192.168.0.90"}, ping_fn=ping_fn,
            arp_set={"192.168.0.251"})
        self.assertEqual(cand, "192.168.0.253")

    def test_select_candidate_skips_targets(self):
        cand = avc.select_candidate(
            "192.168.0", existing_ips=set(),
            target_ips={"192.168.0.250"}, ping_fn=lambda ip: False,
            arp_set=set())
        self.assertEqual(cand, "192.168.0.251")


class FakeResponse:
    def __init__(self, body):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """URL に応じて canned 応答を返し、リクエストを記録する。"""

    def __init__(self, bodies):
        self.bodies = bodies  # list of (url substring, body)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        url = request.full_url
        for key, body in self.bodies:
            if key in url:
                return FakeResponse(body)
        return FakeResponse("OK")


PARAM_LIST_BODY = (
    "root.Brand.Brand=AXIS\n"
    "root.Network.IPAddress=192.168.0.90\n"
    "root.Network.SubnetMask=255.255.255.0\n"
    "root.Time.NTP.Server=pool.ntp.org\n"
    "no-equals-line\n"
)


class TestVapixClient(unittest.TestCase):
    def make_client(self, bodies):
        client = avc.VapixClient("192.168.0.90", "root", "secret")
        client.opener = FakeOpener(bodies)
        return client

    def test_list_params_parse(self):
        client = self.make_client([("param.cgi", PARAM_LIST_BODY)])
        params = client.list_params()
        self.assertEqual(params["Brand.Brand"], "AXIS")
        self.assertEqual(params["Network.IPAddress"], "192.168.0.90")
        self.assertEqual(params["Time.NTP.Server"], "pool.ntp.org")
        self.assertNotIn("no-equals-line", params)

    def test_update_param_posts_action_update(self):
        client = self.make_client([("param.cgi", "OK")])
        client.update_param("Time.NTP.Server", "192.168.10.5")
        req = client.opener.requests[-1]
        data = req.data.decode("utf-8")
        self.assertIn("action=update", data)
        self.assertIn("Time.NTP.Server=192.168.10.5", data)

    def test_error_body_is_failure(self):
        client = self.make_client([("param.cgi", "Error: bad parameter")])
        with self.assertRaises(avc.VapixError):
            client.update_param("X.Y", "1")

    def test_set_password_uses_pwdgrp(self):
        client = self.make_client([("pwdgrp.cgi", "OK")])
        client.set_password("root", "newpass")
        req = client.opener.requests[-1]
        self.assertIn("pwdgrp.cgi", req.full_url)
        data = req.data.decode("utf-8")
        self.assertIn("action=update", data)
        self.assertIn("user=root", data)


class FakeClient:
    """ステップ実行を記録するフェイククライアント。"""

    PARAMS = {
        "ImageSource.I0.Frequency": "50",
        "Time.NTP.Server": "pool.ntp.org",
        "Time.SyncSource": "NTP",
        "Network.IPAddress": "192.168.0.90",
        "Network.SubnetMask": "255.255.255.0",
        "Network.DefaultRouter": "192.168.0.1",
    }

    def __init__(self):
        self.calls = []

    def list_params(self):
        self.calls.append(("list_params",))
        return dict(self.PARAMS)

    def update_param(self, name, value):
        self.calls.append(("update_param", name, value))

    def update_params(self, mapping):
        self.calls.append(("update_params", dict(mapping)))

    def set_password(self, user, new_pwd):
        self.calls.append(("set_password", user, new_pwd))

    def set_credentials(self, user, password):
        self.calls.append(("set_credentials", user))

    def get(self, path):
        self.calls.append(("get", path))
        return "OK"

    def factory_default(self):
        self.calls.append(("factory_default",))


class TestSteps(unittest.TestCase):
    def run_plan(self, plan, fake):
        return avc.process_plan(
            plan, client_factory=lambda p: fake,
            verify_fn=lambda p: "verified")

    def test_full_step_order(self):
        fake = FakeClient()
        plan = avc.DevicePlan(
            ip="192.168.0.90", user="root", password="old",
            hz="60", ntp="manual", new_password="newpass",
            new_ip="192.168.0.99", new_mask="255.255.255.0")
        result = self.run_plan(plan, fake)
        self.assertTrue(result.ok, result.steps)
        kinds = [c[0] for c in fake.calls if c[0] != "list_params"]
        # hz 更新 → SyncSource=None → date.cgi → パスワード → IP 一括更新
        self.assertEqual(kinds, ["update_param", "update_param", "get",
                                 "set_password", "set_credentials",
                                 "update_params"])
        self.assertEqual(fake.calls[1], ("update_param",
                                         "ImageSource.I0.Frequency", "60"))
        self.assertEqual(fake.calls[2], ("update_param",
                                         "Time.SyncSource", "None"))
        self.assertIn("date.cgi", fake.calls[3][1])
        ip_call = [c for c in fake.calls if c[0] == "update_params"][0]
        self.assertEqual(ip_call[1]["Network.IPAddress"], "192.168.0.99")
        self.assertEqual(ip_call[1]["Network.SubnetMask"], "255.255.255.0")
        self.assertEqual([s[0] for s in result.steps],
                         ["hz", "ntp", "new_pass", "new_ip"])

    def test_ntp_server_two_separate_requests(self):
        fake = FakeClient()
        plan = avc.DevicePlan(ip="192.168.0.90", user="root", password="p",
                              ntp="192.168.10.5")
        result = self.run_plan(plan, fake)
        self.assertTrue(result.ok, result.steps)
        updates = [c for c in fake.calls if c[0] == "update_param"]
        self.assertEqual(updates, [
            ("update_param", "Time.NTP.Server", "192.168.10.5"),
            ("update_param", "Time.SyncSource", "NTP"),
        ])

    def test_step_failure_stops_device_but_recorded(self):
        class BadClient(FakeClient):
            def update_param(self, name, value):
                raise avc.VapixError("boom")

        plan = avc.DevicePlan(ip="192.168.0.90", user="root", password="p",
                              hz="60", new_password="x")
        result = self.run_plan(plan, BadClient())
        self.assertFalse(result.ok)
        self.assertEqual([s[0] for s in result.steps], ["hz"])

    def test_hz_ambiguous_candidates(self):
        params = {"A.Frequency": "50", "B.Frequency": "60"}
        with self.assertRaises(avc.VapixError):
            avc.find_hz_param(params)

    def test_restore_waits_and_records(self):
        fake = FakeClient()
        plan = avc.DevicePlan(ip="192.168.0.90", user="root", password="p")
        result = avc.process_restore(
            plan, client_factory=lambda p: fake,
            connect_fn=lambda ip: True, wait_seconds=5, poll_interval=0)
        self.assertTrue(result.ok)
        self.assertIn(("factory_default",), fake.calls)
        self.assertTrue(any("応答" in n for n in result.notes))


class TestTemporaryIPManager(unittest.TestCase):
    def test_add_and_remove_with_mocked_powershell(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state.json"
            mgr = avc.TemporaryIPManager(interface="Ethernet",
                                         state_path=state)
            scripts = []

            def fake_run_command(cmd, timeout=30):
                script = cmd[-1] if cmd else ""
                scripts.append(script)
                if "AddressState" in script:
                    return SimpleNamespace(returncode=0, stdout="Preferred\n",
                                           stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with mock.patch.object(avc, "run_command", fake_run_command), \
                    mock.patch.object(avc, "is_admin", lambda: True), \
                    mock.patch.object(avc, "get_existing_ips",
                                      lambda: {"192.168.0.250"}), \
                    mock.patch.object(avc, "arp_ips", lambda: set()), \
                    mock.patch.object(avc, "ping", lambda ip: False), \
                    mock.patch.object(avc.time, "sleep", lambda s: None):
                ip = mgr.add("192.168.0", ["192.168.0.90"])
                # .250 は既存 IP と重なるので .251 が選ばれる
                self.assertEqual(ip, "192.168.0.251")
                self.assertTrue(any("New-NetIPAddress" in s
                                    and "192.168.0.251" in s for s in scripts))
                self.assertTrue(state.exists())
                mgr.remove(ip)
                self.assertTrue(any("Remove-NetIPAddress" in s
                                    for s in scripts))
                self.assertFalse(state.exists())

    def test_add_requires_admin(self):
        mgr = avc.TemporaryIPManager(interface="Ethernet")
        with mock.patch.object(avc, "is_admin", lambda: False):
            with self.assertRaises(RuntimeError):
                mgr.add("192.168.0", ["192.168.0.90"])


if __name__ == "__main__":
    unittest.main()
