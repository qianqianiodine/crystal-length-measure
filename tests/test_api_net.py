"""手机传图那块地址的拼装：二维码和旁边那段文字 URL 必须一致，
而且都要带上「你在图库看的那个文件夹」。

另：哪个地址排第一 —— "第一个网址手机连不上、换第二个才行"是用户真报过的 bug，
见文件末尾那几条 _rank_ips 的测试。
"""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config, main, storage
from app.api import mobile


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "data" / "images" / "originals")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "data" / "images" / "cache")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")
    # 这台机器上有哪些网卡不归测试管：给一个固定的，地址才可比
    monkeypatch.setattr(mobile.storage, "local_ips", lambda: ["192.168.1.5"])
    config.ensure_dirs()
    with TestClient(main.app) as c:
        c.app.state.port = 8510
        yield c


def test_地址带folder时拼进URL(client):
    d = client.get("/api/net/info?folder=3").json()
    assert d["urls"] and all(u.endswith("?folder=3") for u in d["urls"]), d["urls"]


def test_不带folder时URL不带问号(client):
    d = client.get("/api/net/info").json()
    assert d["urls"] and all("?" not in u for u in d["urls"]), d["urls"]


def test_二维码接口接受folder(client):
    r = client.get("/api/net/qrcode?folder=3")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/png")


# ---- 地址排序：哪个排第一（手机得能连上）----
#
# 一次真实的 ipconfig 原文（用户机器，中文界面）。注意 aTrustVNIC（深信服 VPN）和
# vEthernet (WSL) 的「默认网关」都是空的，只有真连着 WiFi 的 WLAN 才有网关 ——
# 排序就是靠这一条把"手机连得上的"挑出来的。
_SAMPLE_IPCONFIG = """\
Windows IP 配置


以太网适配器 以太网:

   媒体状态  . . . . . . . . . . . . : 媒体已断开连接
   连接特定的 DNS 后缀 . . . . . . . :

未知适配器 aTrustVNIC:

   连接特定的 DNS 后缀 . . . . . . . :
   IPv6 地址 . . . . . . . . . . . . : fd53:414e:4746:4f52:d370:7e5f:4b0a:2b00
   IPv4 地址 . . . . . . . . . . . . : 2.0.0.1
   子网掩码  . . . . . . . . . . . . : 255.255.255.0
   默认网关. . . . . . . . . . . . . :

无线局域网适配器 WLAN:

   连接特定的 DNS 后缀 . . . . . . . :
   链路本地 IPv6 地址. . . . . . . . : fe80::47c3:6f6:d2f2:18ae%11
   IPv4 地址 . . . . . . . . . . . . : 10.200.30.175
   子网掩码  . . . . . . . . . . . . : 255.255.255.0
   默认网关. . . . . . . . . . . . . : 10.200.30.254

以太网适配器 vEthernet (WSL):

   连接特定的 DNS 后缀 . . . . . . . :
   链路本地 IPv6 地址. . . . . . . . : fe80::1e54:aaf2:71bd:c448%44
   IPv4 地址 . . . . . . . . . . . . : 172.22.48.1
   子网掩码  . . . . . . . . . . . . : 255.255.240.0
   默认网关. . . . . . . . . . . . . :
"""


def test_解析ipconfig认出每张网卡有没有网关(monkeypatch):
    """看清中文界面、看清"默认网关后面是空的"，也要认出名字像虚拟网卡的那两张。"""
    monkeypatch.setattr(storage.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=_SAMPLE_IPCONFIG.encode("gbk")))
    assert storage._ipconfig_adapters() == [
        ("2.0.0.1", False, True),            # 深信服 VPN：网关空，名字像虚拟网卡
        ("10.200.30.175", True, False),      # 真无线网卡：有网关，名字正常
        ("172.22.48.1", False, True),        # WSL 虚拟交换机：也是网关空 + 虚拟名字
    ]


def test_手机连得上的地址排第一():
    """用户报的 bug：VPN 靠 UDP 探测抢到第一个，而手机到不了它。"""
    adapters = [("2.0.0.1", False, True),
                ("10.200.30.175", True, False),
                ("172.22.48.1", False, True)]
    ips = ["2.0.0.1", "10.200.30.175", "172.22.48.1"]
    assert storage._rank_ips(ips, adapters) == ["10.200.30.175", "2.0.0.1", "172.22.48.1"]


def test_认不出网卡时顺序原样不动():
    """拿不到 ipconfig（非 Windows / 命令被禁）就退化回老行为，不能把列表排乱排空。"""
    ips = ["2.0.0.1", "10.200.30.175"]
    assert storage._rank_ips(ips, []) == ips
