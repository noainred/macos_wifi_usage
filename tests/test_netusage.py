"""파서/저장/집계/귀속 로직 단위 테스트 (macOS 없이 실행 가능)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netusage import ifstats, netinfo  # noqa: E402
from netusage.storage import Storage  # noqa: E402
from netusage.util import human_bytes  # noqa: E402


NETSTAT_IB = """\
Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                        1543610     0  463773362  1543610     0  463773362     0
lo0        16384 127           127.0.0.1         1543610     -  463773362  1543610     -  463773362     -
lo0        16384 localhost     ::1               1543610     -  463773362  1543610     -  463773362     -
gif0       1280  <Link#2>                              0     0          0        0     0          0     0
stf0       1280  <Link#3>                              0     0          0        0     0          0     0
en0        1500  <Link#4>    a4:83:e7:11:22:33  28746591     0 38423942134 16384726     0 3284756291     0
en0        1500  192.168.0     192.168.0.42      28746591     -  38423942134 16384726     -  3284756291     -
en5        1500  <Link#9>    aa:bb:cc:dd:ee:ff   1000000     0  9000000000   500000     0  1000000000     0
"""

ROUTE_DEFAULT = """\
   route to: default
destination: default
       mask: default
    gateway: 192.168.0.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
"""

HW_PORTS = """\
Hardware Port: Wi-Fi
Device: en0
Ethernet Address: a4:83:e7:11:22:33

Hardware Port: Thunderbolt Ethernet
Device: en5
Ethernet Address: aa:bb:cc:dd:ee:ff

Hardware Port: Thunderbolt Bridge
Device: bridge0
Ethernet Address: ba:db:ee:f0:00:01
"""

AIRPORT_OK = "Current Wi-Fi Network: MyHomeWiFi\n"
AIRPORT_NONE = "You are not associated with an AirPort network.\n"

IPCONFIG_SUMMARY = """\
en0 : flags = ...
  LinkStatusActive : TRUE
  SSID : CoffeeShop_5G
  Security : WPA2 Personal
"""

SP_AIRPORT = """\
Wi-Fi:
      Interfaces:
        en0:
          Current Network Information:
            OfficeNet:
              PHY Mode: 802.11ax
              Channel: 36
            Other Local Wi-Fi Networks:
              Neighbor:
"""


class TestIfstats(unittest.TestCase):
    def test_parse_link_rows_only(self):
        c = ifstats.parse_netstat_ib(NETSTAT_IB)
        # link 행만, 인터페이스당 한 번
        self.assertEqual(c["en0"], (38423942134, 3284756291))
        self.assertEqual(c["en5"], (9000000000, 1000000000))
        self.assertEqual(c["lo0"], (463773362, 463773362))
        # IPv4 중복 행이 값을 덮어쓰지 않아야 함
        self.assertEqual(len([k for k in c if k == "en0"]), 1)

    def test_empty_input(self):
        self.assertEqual(ifstats.parse_netstat_ib(""), {})


class TestNetinfo(unittest.TestCase):
    def test_default_route(self):
        info = netinfo.parse_default_route(ROUTE_DEFAULT)
        self.assertEqual(info["interface"], "en0")
        self.assertEqual(info["gateway"], "192.168.0.1")

    def test_hardware_ports(self):
        ports = netinfo.parse_hardware_ports(HW_PORTS)
        self.assertEqual(ports["en0"], "Wi-Fi")
        self.assertEqual(ports["en5"], "Thunderbolt Ethernet")
        self.assertEqual(netinfo.conn_type_for(ports["en0"]), "wifi")
        self.assertEqual(netinfo.conn_type_for(ports["en5"]), "ethernet")
        self.assertEqual(netinfo.conn_type_for("Bluetooth PAN"), "other")

    def test_ssid_parsers(self):
        self.assertEqual(netinfo.parse_airport_ssid(AIRPORT_OK), "MyHomeWiFi")
        self.assertIsNone(netinfo.parse_airport_ssid(AIRPORT_NONE))
        self.assertEqual(netinfo.parse_ipconfig_ssid(IPCONFIG_SUMMARY), "CoffeeShop_5G")
        self.assertEqual(netinfo.parse_system_profiler_ssid(SP_AIRPORT), "OfficeNet")

    def test_match_named_by_gateway(self):
        named = [{"name": "Home", "gateway": "192.168.0.1"},
                 {"name": "Office", "ping": "10.0.0.1"}]
        # gateway 매칭은 ping 없이 즉시
        self.assertEqual(
            netinfo.match_named_network(named, "192.168.0.1", 1000), "Home"
        )

    def test_match_named_by_ping(self):
        named = [{"name": "Office", "ping": "10.0.0.1"}]
        orig = netinfo.ping
        try:
            netinfo.ping = lambda ip, t=1500: ip == "10.0.0.1"
            self.assertEqual(
                netinfo.match_named_network(named, None, 1000), "Office"
            )
            netinfo.ping = lambda ip, t=1500: False
            self.assertIsNone(netinfo.match_named_network(named, None, 1000))
        finally:
            netinfo.ping = orig


class TestStorageAggregate(unittest.TestCase):
    def setUp(self):
        self.db = Storage(":memory:")

    def tearDown(self):
        self.db.close()

    def test_insert_and_aggregate(self):
        # Home Wi-Fi 두 구간, Office ethernet 한 구간
        self.db.insert_sample(1000, "en0", "wifi", "Home", "Home", "192.168.0.1", 100, 50)
        self.db.insert_sample(1100, "en0", "wifi", "Home", "Home", "192.168.0.1", 200, 80)
        self.db.insert_sample(1200, "en5", "ethernet", "Office", None, "10.0.0.1", 1000, 300)

        rows = self.db.aggregate(group_by="network")
        by_label = {r["label"]: r for r in rows}
        self.assertEqual(by_label["Home"]["rx"], 300)
        self.assertEqual(by_label["Home"]["tx"], 130)
        self.assertEqual(by_label["Home"]["samples"], 2)
        self.assertEqual(by_label["Office"]["rx"], 1000)
        # 합계 정렬: Office(1300) 가 Home(430) 보다 위
        self.assertEqual(rows[0]["label"], "Office")

        self.assertEqual(self.db.totals(), (1300, 430))

    def test_group_by_type(self):
        self.db.insert_sample(1000, "en0", "wifi", "Home", "Home", None, 100, 50)
        self.db.insert_sample(1200, "en5", "ethernet", "Office", None, None, 1000, 300)
        rows = self.db.aggregate(group_by="type")
        labels = {r["label"] for r in rows}
        self.assertEqual(labels, {"wifi", "ethernet"})

    def test_time_filter(self):
        self.db.insert_sample(1000, "en0", "wifi", "Home", "Home", None, 100, 50)
        self.db.insert_sample(5000, "en0", "wifi", "Home", "Home", None, 200, 80)
        self.assertEqual(self.db.totals(since=2000), (200, 80))
        self.assertEqual(self.db.totals(until=2000), (100, 50))

    def test_counter_state(self):
        self.assertIsNone(self.db.get_last_counter("en0"))
        self.db.set_last_counter("en0", 500, 200, 1000)
        self.assertEqual(self.db.get_last_counter("en0"), (500, 200))
        self.db.set_last_counter("en0", 700, 250, 1100)
        self.assertEqual(self.db.get_last_counter("en0"), (700, 250))


class TestSampleAttribution(unittest.TestCase):
    """sample_once 가 delta 를 활성 인터페이스/네트워크에 올바로 귀속하는지."""

    def setUp(self):
        self.db = Storage(":memory:")

    def tearDown(self):
        self.db.close()

    def _patch(self, counters, ident):
        from netusage import monitor
        monitor.ifstats.read_counters = lambda: counters
        monitor.netinfo.identify_network = lambda cfg: ident

    def test_delta_attribution_and_reset(self):
        from netusage import monitor
        from netusage.netinfo import NetworkIdentity

        config = {}
        ident = NetworkIdentity(iface="en0", conn_type="wifi",
                                network="Home", ssid="Home", gateway="192.168.0.1")

        # 1) 첫 호출: 기준값만, 기록 없음
        self._patch({"en0": (1000, 500), "en5": (10, 10)}, ident)
        self.assertIsNone(monitor.sample_once(self.db, config, record=True))
        self.assertEqual(self.db.totals(), (0, 0))

        # 2) 두 번째: delta = (1500-1000, 800-500) = (500, 300) 기록
        self._patch({"en0": (1500, 800), "en5": (10, 10)}, ident)
        rec = monitor.sample_once(self.db, config, record=True)
        self.assertEqual((rec["rx"], rec["tx"]), (500, 300))
        self.assertEqual(self.db.totals(), (500, 300))

        # 3) 카운터 리셋(현재 < 직전): delta 미기록, 기준값만 갱신
        self._patch({"en0": (100, 50), "en5": (10, 10)}, ident)
        self.assertIsNone(monitor.sample_once(self.db, config, record=True))
        self.assertEqual(self.db.totals(), (500, 300))

        # 4) 리셋 이후 정상 증가: delta = (300-100, 120-50) = (200, 70)
        self._patch({"en0": (300, 120), "en5": (10, 10)}, ident)
        rec = monitor.sample_once(self.db, config, record=True)
        self.assertEqual((rec["rx"], rec["tx"]), (200, 70))
        self.assertEqual(self.db.totals(), (700, 370))


class TestUtil(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(human_bytes(0), "0 B")
        self.assertEqual(human_bytes(512), "512 B")
        self.assertEqual(human_bytes(1024), "1.00 KiB")
        self.assertEqual(human_bytes(1536), "1.50 KiB")
        self.assertEqual(human_bytes(1024 * 1024), "1.00 MiB")
        self.assertEqual(human_bytes(5 * 1024 ** 3), "5.00 GiB")


if __name__ == "__main__":
    unittest.main(verbosity=2)
