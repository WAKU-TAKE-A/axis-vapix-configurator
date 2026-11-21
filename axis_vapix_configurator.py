#!/usr/bin/env python3
"""axis-vapix-configurator

複数台の Axis ネットワークカメラへ、VAPIX 経由で共通設定を一括適用する。
サブネットが異なる対象には TemporaryIP（Windows）で一時 IP を追加して到達する。

Python 標準ライブラリのみで動作する。設計の詳細は docs/DESIGN.md を参照。
"""

import argparse
import ctypes
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__version__ = "0.9.0.0"

logger = logging.getLogger("axis_vapix_configurator")

LOG_FILENAME = "axis_vapix_configurator.log"
STATE_FILENAME = ".axis_vapix_temporary_ip.json"

RESTORE_WAIT_SECONDS = 180
RESTORE_POLL_INTERVAL = 5
ADDRESS_STATE_WAIT_SECONDS = 10


def app_base_dir() -> Path:
    """スクリプト（exe 化時は exe）のあるフォルダを返す。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def state_file_path() -> Path:
    return app_base_dir() / STATE_FILENAME


# ---------------------------------------------------------------------------
# 引数の展開と検証
# ---------------------------------------------------------------------------

def expand(values, n, name):
    """値リストを台数 n に展開する。

    None -> [None] * n（その処理をしない）
    1 個 -> 全台共通として複製
    n 個 -> そのまま
    それ以外は ValueError
    """
    if values is None:
        return [None] * n
    values = list(values)
    if len(values) == 1:
        return values * n
    if len(values) == n:
        return values
    raise ValueError(
        f"{name}: 値の個数は 1 個（全台共通）か {n} 個（台別）で指定してください"
        f"（{len(values)} 個が指定されました）"
    )


def _is_ipv4(value) -> bool:
    try:
        parts = str(value).split(".")
        if len(parts) != 4:
            return False
        return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    except Exception:
        return False


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


@dataclass
class DevicePlan:
    """1 台分の実行計画（展開・検証済みの値）。"""

    ip: str
    user: str
    password: str
    hz: str = None
    new_ip: str = None
    new_mask: str = None
    new_gateway: str = None
    new_password: str = None
    ntp: str = None


def build_plans(args) -> list:
    """argparse の結果を検証し、台ごとの DevicePlan に展開する。

    検証はすべてここで行い、問題があれば ValueError を送出する
    （呼び出し側は 1 台も触らずに終了する）。
    """
    targets = list(args.ip)
    n = len(targets)

    for ip in targets:
        if not _is_ipv4(ip):
            raise ValueError(f"--ip: IP アドレスの形式が不正です: {ip}")
    if len(set(targets)) != n:
        raise ValueError("--ip: 対象 IP が重複しています")

    users = expand(args.user, n, "--user")
    passwords = expand(getattr(args, "password"), n, "--pass")
    hz_list = expand(args.hz, n, "--hz")
    new_ips = expand(args.new_ip, n, "--new-ip")
    new_masks = expand(args.new_mask, n, "--new-mask")
    new_gateways = expand(args.new_gateway, n, "--new-gateway")
    new_passwords = expand(getattr(args, "new_password"), n, "--new-pass")
    ntp_list = expand(args.ntp, n, "--ntp")

    for hz in hz_list:
        if hz is not None and hz not in ("50", "60"):
            raise ValueError(f"--hz: 値は 50 か 60 のみ許可されます: {hz}")

    for name, values in (("--new-ip", new_ips), ("--new-mask", new_masks),
                         ("--new-gateway", new_gateways)):
        for v in values:
            if v is not None and not _is_ipv4(v):
                raise ValueError(f"{name}: IP アドレスの形式が不正です: {v}")

    specified_new_ips = [v for v in new_ips if v is not None]
    if len(set(specified_new_ips)) != len(specified_new_ips):
        raise ValueError("--new-ip: 変更後の IP が重複しています")

    for v in ntp_list:
        if v is not None and v != "manual" and not _is_ipv4(v) \
                and not _HOSTNAME_RE.match(v):
            raise ValueError(f"--ntp: NTP サーバーの指定が不正です: {v}")

    change_options = (
        ("--hz", args.hz), ("--new-ip", args.new_ip),
        ("--new-mask", args.new_mask), ("--new-gateway", args.new_gateway),
        ("--new-pass", getattr(args, "new_password")), ("--ntp", args.ntp),
    )
    if args.restore:
        used = [name for name, v in change_options if v is not None]
        if used:
            raise ValueError(
                "--restore は単独実行専用です。他の変更オプションと併用できません: "
                + ", ".join(used))
    if args.dump_params:
        used = [name for name, v in change_options if v is not None]
        if args.restore:
            used.append("--restore")
        if used:
            raise ValueError(
                "--dump-params は他の変更と併用できません: " + ", ".join(used))

    plans = []
    for i in range(n):
        plans.append(DevicePlan(
            ip=targets[i], user=users[i], password=passwords[i],
            hz=hz_list[i], new_ip=new_ips[i], new_mask=new_masks[i],
            new_gateway=new_gateways[i], new_password=new_passwords[i],
            ntp=ntp_list[i],
        ))
    return plans


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="axis_vapix_configurator.py",
        description="複数台の Axis カメラへ VAPIX 経由で共通設定を一括適用する")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--ip", nargs="+", required=True,
                        help="対象カメラの変更前 IP（複数指定可。これが台数 N になる）")
    parser.add_argument("--user", nargs="+", default=["root"],
                        help="アクセス用ユーザー（1 個=全台共通、N 個=台別）")
    parser.add_argument("--pass", dest="password", nargs="+", required=True,
                        help="アクセス用パスワード（1 個=全台共通、N 個=台別）")
    parser.add_argument("--hz", nargs="+",
                        help="電源周波数 50 / 60（1 個=共通、N 個=台別）")
    parser.add_argument("--new-ip", nargs="+", help="変更後の IP")
    parser.add_argument("--new-mask", nargs="+",
                        help="変更後のサブネットマスク（未指定なら変更しない）")
    parser.add_argument("--new-gateway", nargs="+",
                        help="変更後のデフォルトゲートウェイ（未指定なら変更しない）")
    parser.add_argument("--new-pass", dest="new_password", nargs="+",
                        help="変更後のパスワード")
    parser.add_argument("--ntp", nargs="+",
                        help="NTP サーバーの IP/ホスト名、または manual（マニュアル時刻）")
    parser.add_argument("--restore", action="store_true",
                        help="IP 保持の初期化（単独実行専用）")
    parser.add_argument("--interface",
                        help="TemporaryIP を追加する NIC のエイリアス名"
                             "（未指定なら既定ルートのあるアダプタを自動選択）")
    parser.add_argument("--dry-run", action="store_true",
                        help="変更系リクエストを送らず、何をするか表示だけする")
    parser.add_argument("--dump-params", action="store_true",
                        help="各対象の param.cgi 全件リストを表示/保存して終わる診断モード")
    parser.add_argument("--timeout", type=float, default=10,
                        help="HTTP タイムアウト秒（既定 10）")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# VAPIX クライアント
# ---------------------------------------------------------------------------

class VapixError(Exception):
    """VAPIX リクエストの失敗（HTTP エラー、または本文中の Error 応答）。"""


class VapixClient:
    def __init__(self, ip, user, password, timeout=10):
        self.ip = ip
        self.timeout = timeout
        self._user = user
        self.opener = None
        self.set_credentials(user, password)

    def set_credentials(self, user, password):
        """認証情報を差し替える（パスワード変更後に使う）。値はログに出さない。"""
        self._user = user
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, f"http://{self.ip}/", user, password)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(mgr),
            urllib.request.HTTPBasicAuthHandler(mgr),
        )

    def _open(self, request) -> str:
        try:
            with self.opener.open(request, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            raise VapixError(f"HTTP {e.code}: {self.ip}") from e
        except urllib.error.URLError as e:
            raise VapixError(f"接続失敗: {self.ip}: {e.reason}") from e
        # Axis は HTTP 200 でも本文でエラーを返すことがある
        if "Error" in body:
            first = body.strip().splitlines()[0] if body.strip() else "Error"
            raise VapixError(f"デバイスがエラーを返しました: {self.ip}: {first}")
        return body

    def get(self, path_and_query) -> str:
        req = urllib.request.Request(f"http://{self.ip}{path_and_query}",
                                     method="GET")
        return self._open(req)

    def post(self, path, data_dict) -> str:
        data = urllib.parse.urlencode(data_dict).encode("utf-8")
        req = urllib.request.Request(f"http://{self.ip}{path}", data=data,
                                     method="POST")
        return self._open(req)

    def list_params(self) -> dict:
        """param.cgi の全件リストを dict で返す（キーは root. 接頭辞を除く）。"""
        body = self.get("/axis-cgi/param.cgi?action=list")
        params = {}
        for line in body.splitlines():
            line = line.strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("root."):
                key = key[len("root."):]
            params[key] = value.strip()
        return params

    def update_param(self, name, value):
        self.update_params({name: value})

    def update_params(self, mapping):
        """複数パラメータを 1 回の update で送る（IP 変更の中身専用）。"""
        data = {"action": "update"}
        data.update(mapping)
        self.post("/axis-cgi/param.cgi", data)

    def set_password(self, user, new_pwd):
        self.post("/axis-cgi/pwdgrp.cgi",
                  {"action": "update", "user": user, "pwd": new_pwd})

    def factory_default(self):
        self.get("/axis-cgi/factorydefault.cgi")


def find_param(params, *regex_patterns):
    """正規表現パターンを優先順に検索し、最初に一致したパラメータ名を返す。"""
    for pattern in regex_patterns:
        rx = re.compile(pattern)
        for name in params:
            if rx.search(name):
                return name
    return None


def find_hz_param(params):
    """Hz（電源周波数）パラメータを一意に特定する。

    候補：名前が Frequency を含み現在値が 50/60 のもの、
    または名前に PowerLine / Mains を含むもの。
    一意に決まらなければ VapixError（候補一覧付き）。
    """
    candidates = []
    for name, value in params.items():
        if ("Frequency" in name and value in ("50", "60")) \
                or "PowerLine" in name or "Mains" in name:
            candidates.append(name)
    candidates = sorted(set(candidates))
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise VapixError("Hz パラメータの候補が見つかりませんでした")
    raise VapixError(
        "Hz パラメータの候補が複数あり一意に決まりません: "
        + ", ".join(candidates))


# ---------------------------------------------------------------------------
# ネットワーク補助（到達判定・候補選択）
# ---------------------------------------------------------------------------

def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_admin() -> bool:
    if not is_windows():
        return False
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def can_connect(ip, port=80, timeout=3) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def network24(ip) -> str:
    """IP の /24 ネットワーク部（第3オクテットまで）を返す。"""
    return ".".join(str(ip).split(".")[:3])


def group_by_network(plans) -> dict:
    """計画を /24 ネットワーク単位でグループ化する（順序は保持）。"""
    groups = {}
    for plan in plans:
        groups.setdefault(network24(plan.ip), []).append(plan)
    return groups


def run_command(cmd, timeout=30):
    """subprocess 実行の小さなラッパ（テストでモックしやすくするため）。"""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def run_powershell(script) -> str:
    result = run_command(["powershell", "-NoProfile", "-Command", script])
    if result.returncode != 0:
        raise RuntimeError(
            f"PowerShell が失敗しました: {script} / {result.stderr.strip()}")
    return result.stdout


def ping(ip) -> bool:
    cmd = ["ping", "-n", "1", "-w", "500", ip] if is_windows() \
        else ["ping", "-c", "1", "-W", "1", ip]
    try:
        return run_command(cmd, timeout=5).returncode == 0
    except Exception:
        return False


def arp_ips() -> set:
    try:
        out = run_command(["arp", "-a"], timeout=10).stdout
    except Exception:
        return set()
    return set(re.findall(r"\d{1,3}(?:\.\d{1,3}){3}", out))


def get_existing_ips() -> set:
    if is_windows():
        try:
            out = run_powershell(
                "(Get-NetIPAddress -AddressFamily IPv4).IPAddress")
            return {line.strip() for line in out.splitlines() if line.strip()}
        except Exception:
            return set()
    try:
        return set(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        return set()


def candidate_suffixes():
    """.250-.254、次に .249-.240 の順。"""
    return list(range(250, 255)) + list(range(249, 239, -1))


def iter_candidates(prefix, existing_ips, target_ips, ping_fn, arp_set):
    for suffix in candidate_suffixes():
        ip = f"{prefix}.{suffix}"
        if ip in existing_ips or ip in target_ips:
            continue
        if ip in arp_set:
            continue
        if ping_fn(ip):
            continue
        yield ip


def select_candidate(prefix, existing_ips, target_ips,
                     ping_fn=None, arp_set=None):
    """コンフリクトしない一時 IP 候補を 1 つ返す（無ければ None）。"""
    ping_fn = ping if ping_fn is None else ping_fn
    arp_set = arp_ips() if arp_set is None else arp_set
    for ip in iter_candidates(prefix, existing_ips, target_ips, ping_fn, arp_set):
        return ip
    return None


def get_default_interface() -> str:
    """既定ルート（0.0.0.0/0）を持つ有効アダプタの InterfaceAlias を返す。"""
    out = run_powershell(
        "Get-NetRoute -DestinationPrefix '0.0.0.0/0' "
        "| Sort-Object -Property RouteMetric "
        "| Select-Object -First 1 -ExpandProperty InterfaceAlias")
    alias = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not alias:
        raise RuntimeError("既定ルートのあるネットワークアダプタが見つかりません")
    return alias


# ---------------------------------------------------------------------------
# TemporaryIP（Windows 専用）
# ---------------------------------------------------------------------------

class TemporaryIPManager:
    def __init__(self, interface=None, state_path=None):
        self.interface = interface
        self.state_path = Path(state_path) if state_path else state_file_path()
        self.added = []

    # -- PowerShell 呼び出し（テストで差し替え可能にするため分離） --
    def _add_ip(self, ip):
        run_powershell(
            f"New-NetIPAddress -InterfaceAlias '{self.interface}' "
            f"-IPAddress {ip} -PrefixLength 24 -SkipAsSource $true | Out-Null")

    def _remove_ip(self, ip):
        run_powershell(
            f"Remove-NetIPAddress -InterfaceAlias '{self.interface}' "
            f"-IPAddress {ip} -Confirm:$false")

    def address_state(self, ip):
        out = run_powershell(
            f"(Get-NetIPAddress -IPAddress {ip} "
            f"-ErrorAction SilentlyContinue).AddressState")
        return out.strip().splitlines()[0].strip() if out.strip() else ""

    # -- 状態ファイル --
    def _write_state(self):
        data = {"interface": self.interface, "addresses": list(self.added)}
        self.state_path.write_text(json.dumps(data), encoding="utf-8")

    def _clear_state(self):
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass

    def cleanup_stale(self):
        """前回残存した自作の一時 IP があれば削除してから始める。"""
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            self._clear_state()
            return
        if data.get("interface"):
            self.interface = self.interface or data["interface"]
        for ip in data.get("addresses", []):
            try:
                self._remove_ip(ip)
                logger.info("前回残存した一時 IP を削除しました: %s", ip)
            except Exception as e:
                logger.warning("残存した一時 IP の削除に失敗しました: %s: %s", ip, e)
        self._clear_state()

    # -- 追加・削除 --
    def add(self, prefix, target_ips) -> str:
        """ネットワーク prefix 向けの一時 IP を追加し、その IP を返す。"""
        if not is_admin():
            raise RuntimeError(
                "一時 IP の追加には管理者権限が必要です。"
                "管理者として実行してください")
        if not self.interface:
            self.interface = get_default_interface()
        existing = get_existing_ips()
        arp_set = arp_ips()
        for ip in iter_candidates(prefix, existing, set(target_ips), ping, arp_set):
            self._add_ip(ip)
            state = self._wait_preferred(ip)
            if state == "Preferred":
                self.added.append(ip)
                self._write_state()
                logger.info("一時 IP を追加しました: %s (%s)", ip, self.interface)
                return ip
            # Duplicate 等は削除して次候補へ
            logger.warning("一時 IP 候補が使えませんでした: %s (状態: %s)", ip, state)
            try:
                self._remove_ip(ip)
            except Exception:
                pass
        raise RuntimeError(f"ネットワーク {prefix}.0/24 に空き IP が見つかりません")

    def _wait_preferred(self, ip) -> str:
        deadline = time.time() + ADDRESS_STATE_WAIT_SECONDS
        state = ""
        while time.time() <= deadline:
            state = self.address_state(ip)
            if state in ("Preferred", "Duplicate"):
                return state
            time.sleep(1)
        return state

    def remove(self, ip):
        try:
            self._remove_ip(ip)
            logger.info("一時 IP を削除しました: %s", ip)
        finally:
            if ip in self.added:
                self.added.remove(ip)
            if self.added:
                self._write_state()
            else:
                self._clear_state()

    def remove_all(self):
        for ip in list(self.added):
            try:
                self.remove(ip)
            except Exception as e:
                logger.warning("一時 IP の削除に失敗しました: %s: %s", ip, e)


# ---------------------------------------------------------------------------
# ステップ実行
# ---------------------------------------------------------------------------

@dataclass
class DeviceResult:
    ip: str
    ok: bool = True
    steps: list = field(default_factory=list)  # (name, ok, message)
    notes: list = field(default_factory=list)

    def add_step(self, name, ok, message):
        self.steps.append((name, ok, message))
        if not ok:
            self.ok = False


class DeviceContext:
    """1 台分の実行状態（クライアントとパラメータ一覧のキャッシュ）。"""

    def __init__(self, plan, client):
        self.plan = plan
        self.client = client
        self._params = None

    def params(self) -> dict:
        if self._params is None:
            self._params = self.client.list_params()
        return self._params


def step_hz(ctx):
    name = find_hz_param(ctx.params())
    ctx.client.update_param(name, ctx.plan.hz)
    return f"{name}={ctx.plan.hz}"


def step_ntp(ctx):
    params = ctx.params()
    if ctx.plan.ntp == "manual":
        sync_name = find_param(params, r"^Time\.SyncSource$") or "Time.SyncSource"
        ctx.client.update_param(sync_name, "None")
        now = datetime.now()
        path = ("/axis-cgi/admin/date.cgi?action=set"
                f"&year={now.year}&month={now.month}&day={now.day}"
                f"&hour={now.hour}&minute={now.minute}&second={now.second}")
        ctx.client.get(path)
        return "マニュアル時刻に切替し、PC の現在時刻をセットしました"
    server_name = find_param(params, r"^Time\.NTP\.Server$") or "Time.NTP.Server"
    ctx.client.update_param(server_name, ctx.plan.ntp)
    sync_name = find_param(params, r"^Time\.SyncSource$") or "Time.SyncSource"
    ctx.client.update_param(sync_name, "NTP")
    return f"NTP サーバーを {ctx.plan.ntp} に設定しました"


def step_new_pass(ctx):
    ctx.client.set_password(ctx.plan.user, ctx.plan.new_password)
    ctx.client.set_credentials(ctx.plan.user, ctx.plan.new_password)
    return "パスワードを変更しました"


def step_new_ip(ctx):
    params = ctx.params()
    mapping = {}
    ip_name = find_param(params, r"^Network\.IPAddress$",
                         r"^Network\..*\.IPAddress$")
    if not ip_name:
        raise VapixError("IP アドレスのパラメータが見つかりません")
    mapping[ip_name] = ctx.plan.new_ip
    if ctx.plan.new_mask is not None:
        mask_name = find_param(params, r"^Network\.SubnetMask$",
                               r"^Network\..*\.SubnetMask$")
        if not mask_name:
            raise VapixError("サブネットマスクのパラメータが見つかりません")
        mapping[mask_name] = ctx.plan.new_mask
    if ctx.plan.new_gateway is not None:
        gw_name = find_param(params, r"^Network\.DefaultRouter$",
                             r"^Network\..*\.DefaultRouter$")
        if not gw_name:
            raise VapixError("デフォルトゲートウェイのパラメータが見つかりません")
        mapping[gw_name] = ctx.plan.new_gateway
    # IP／マスク／ゲートウェイは 1 つの変更として同時に送る
    ctx.client.update_params(mapping)
    return f"IP を {ctx.plan.new_ip} に変更しました（この台の処理はここで終了）"


# ステップの実行順。将来の変更追加は関数を足してここへ 1 行登録する。
STEPS = [
    ("hz", "hz", step_hz),
    ("ntp", "ntp", step_ntp),
    ("new_pass", "new_password", step_new_pass),
    ("new_ip", "new_ip", step_new_ip),
]


def verify_new_ip(plan, interface=None) -> str:
    """新 IP への疎通確認。変更自体の成否とは分けて報告する。"""
    if can_connect(plan.new_ip):
        return "新 IP への疎通を確認しました"
    if is_windows():
        mgr = TemporaryIPManager(interface=interface)
        try:
            temp_ip = mgr.add(network24(plan.new_ip), [plan.new_ip])
            try:
                if can_connect(plan.new_ip):
                    return "新 IP への疎通を確認しました（一時 IP 経由）"
            finally:
                mgr.remove(temp_ip)
        except Exception as e:
            return f"新 IP への疎通は未確認です（{e}）"
    return "新 IP への疎通は未確認です（変更自体は送信済み）"


def process_plan(plan, timeout=10, client_factory=None, verify_fn=None,
                 interface=None) -> DeviceResult:
    """1 台分の変更ステップを順に実行する。"""
    factory = client_factory or (
        lambda p: VapixClient(p.ip, p.user, p.password, timeout=timeout))
    verify_fn = verify_new_ip if verify_fn is None else verify_fn
    result = DeviceResult(ip=plan.ip)
    ctx = DeviceContext(plan, factory(plan))
    for step_name, plan_attr, func in STEPS:
        if getattr(plan, plan_attr) is None:
            continue
        try:
            message = func(ctx)
        except Exception as e:
            result.add_step(step_name, False, str(e))
            logger.error("[%s] %s 失敗: %s", plan.ip, step_name, e)
            break
        result.add_step(step_name, True, message)
        logger.info("[%s] %s: %s", plan.ip, step_name, message)
        if step_name == "new_ip":
            note = verify_fn(plan, interface=interface) \
                if verify_fn is verify_new_ip else verify_fn(plan)
            result.notes.append(note)
            logger.info("[%s] %s", plan.ip, note)
    return result


def process_restore(plan, timeout=10, client_factory=None, connect_fn=None,
                    wait_seconds=RESTORE_WAIT_SECONDS,
                    poll_interval=RESTORE_POLL_INTERVAL) -> DeviceResult:
    """IP 保持の初期化（単独モード）。送信できれば記録し、再起動を待つ。"""
    factory = client_factory or (
        lambda p: VapixClient(p.ip, p.user, p.password, timeout=timeout))
    connect_fn = can_connect if connect_fn is None else connect_fn
    result = DeviceResult(ip=plan.ip)
    client = factory(plan)
    try:
        client.factory_default()
    except Exception as e:
        result.add_step("restore", False, str(e))
        logger.error("[%s] restore 失敗: %s", plan.ip, e)
        return result
    result.add_step("restore", True, "初期化を送信しました")
    logger.info("[%s] restore: 初期化を送信しました。再起動を待ちます", plan.ip)
    deadline = time.time() + wait_seconds
    while time.time() <= deadline:
        if connect_fn(plan.ip):
            result.notes.append("再起動後の応答を確認しました")
            return result
        time.sleep(poll_interval)
    result.notes.append("再起動後の応答は未確認です（初期化は送信済み）")
    return result


def process_dump_params(plan, timeout=10, client_factory=None) -> DeviceResult:
    factory = client_factory or (
        lambda p: VapixClient(p.ip, p.user, p.password, timeout=timeout))
    result = DeviceResult(ip=plan.ip)
    try:
        params = factory(plan).list_params()
    except Exception as e:
        result.add_step("dump_params", False, str(e))
        return result
    for key in sorted(params):
        print(f"{plan.ip}\t{key}={params[key]}")
    out_path = app_base_dir() / f"params_{plan.ip.replace('.', '_')}.txt"
    out_path.write_text(
        "".join(f"{k}={params[k]}\n" for k in sorted(params)), encoding="utf-8")
    result.add_step("dump_params", True, f"{len(params)} 件を {out_path.name} に保存")
    return result


# ---------------------------------------------------------------------------
# 全体実行
# ---------------------------------------------------------------------------

def _process_group(plans, processor):
    return [processor(plan) for plan in plans]


def run(args) -> int:
    plans = build_plans(args)

    if args.dry_run:
        for plan in plans:
            actions = []
            if args.restore:
                actions.append("restore（IP 保持の初期化）")
            if args.dump_params:
                actions.append("dump-params")
            for step_name, plan_attr, _ in STEPS:
                if getattr(plan, plan_attr) is not None:
                    actions.append(step_name)
            print(f"{plan.ip} (user={plan.user}): "
                  + (", ".join(actions) if actions else "処理なし"))
        print("dry-run のため、リクエストは送信していません")
        return 0

    if args.restore:
        processor = lambda p: process_restore(p, timeout=args.timeout)
    elif args.dump_params:
        processor = lambda p: process_dump_params(p, timeout=args.timeout)
    else:
        processor = lambda p: process_plan(p, timeout=args.timeout,
                                           interface=args.interface)

    mgr = None
    if is_windows():
        mgr = TemporaryIPManager(interface=args.interface)
        mgr.cleanup_stale()

    results = []
    unreachable = []
    for plan in plans:
        if can_connect(plan.ip):
            results.extend(_process_group([plan], processor))
        else:
            unreachable.append(plan)

    if unreachable:
        if not is_windows():
            for plan in unreachable:
                r = DeviceResult(ip=plan.ip)
                r.add_step("connect", False,
                           "到達できません（TemporaryIP は Windows のみ対応）")
                results.append(r)
        else:
            all_targets = [p.ip for p in plans]
            for prefix, group in group_by_network(unreachable).items():
                try:
                    temp_ip = mgr.add(prefix, all_targets)
                except Exception as e:
                    for plan in group:
                        r = DeviceResult(ip=plan.ip)
                        r.add_step("temporary_ip", False, str(e))
                        results.append(r)
                    continue
                try:
                    results.extend(_process_group(group, processor))
                finally:
                    mgr.remove(temp_ip)

    print("\n=== 結果サマリ ===")
    failed = 0
    order = {p.ip: i for i, p in enumerate(plans)}
    for r in sorted(results, key=lambda r: order.get(r.ip, 0)):
        status = "OK" if r.ok else "NG"
        if not r.ok:
            failed += 1
        detail = "; ".join(
            f"{name}:{'OK' if ok else 'NG'} {msg}" for name, ok, msg in r.steps)
        notes = (" / " + " / ".join(r.notes)) if r.notes else ""
        print(f"[{status}] {r.ip}  {detail}{notes}")
        logger.info("結果 [%s] %s %s%s", status, r.ip, detail, notes)
    print(f"対象 {len(plans)} 台 / 成功 {len(plans) - failed} 台 / 失敗 {failed} 台")
    return 1 if failed else 0


def setup_logging():
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(app_base_dir() / LOG_FILENAME, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(sh)
    logger.propagate = False


def main(argv=None) -> int:
    args = parse_args(argv)
    setup_logging()
    try:
        return run(args)
    except ValueError as e:
        logger.error("引数エラー: %s", e)
        print(f"エラー: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
