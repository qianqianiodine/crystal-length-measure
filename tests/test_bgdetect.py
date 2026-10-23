"""后台预识别孔：起点圆缓存 + 后台队列 + 打开时优先读缓存。"""
import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import bgdetect, config, db, main, storage


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path / "data" / "images" / "originals")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "data" / "images" / "cache")
    monkeypatch.setattr(config, "THUMBS_DIR", tmp_path / "data" / "thumbs")
    monkeypatch.setattr(config, "STAGING_DIR", tmp_path / "data" / "staging")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")
    # 测试里绝不能真的启动后台线程（它会去处理测试造的图）。
    monkeypatch.delenv("JLTX_BG_DETECT", raising=False)
    config.ensure_dirs()
    conn = db.get_conn()
    db.init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def client(env):
    with TestClient(main.app) as c:
        yield c


def _png(w=80, h=60, val=200) -> bytes:
    ok, buf = cv2.imencode(".png", np.full((h, w, 3), val, np.uint8))
    assert ok
    return buf.tobytes()


def _make_image(conn, name="A", folder_ids=()):
    stored = storage.store_original(_png())
    iid = db.create_image(conn, name, name + ".png", stored, "upload")
    if folder_ids:
        db.set_image_folders(conn, iid, list(folder_ids))
    return iid


def _folder(conn, name="实验A"):
    return db.create_folder(conn, name, None)


class _Cand:
    def __init__(self, cx=30.0, cy=20.0, r=10.0):
        self.center = (cx, cy)
        self.radius_px = r


# ---------------- 纯函数 ----------------

def test_circle_from_image_uses_the_first_candidate(monkeypatch):
    monkeypatch.setattr(bgdetect.detect, "detect_wells", lambda img: [_Cand()])
    c = bgdetect.circle_from_image(np.zeros((60, 80, 3), np.uint8))
    assert c == {"cx": 30.0, "cy": 20.0, "a": 10.0, "b": 10.0,
                 "th": 0.0, "auto": True}


def test_circle_from_image_falls_back_to_the_center(monkeypatch):
    monkeypatch.setattr(bgdetect.detect, "detect_wells", lambda img: [])
    c = bgdetect.circle_from_image(np.zeros((60, 80, 3), np.uint8))
    assert c["cx"] == 40.0 and c["cy"] == 30.0
    assert c["a"] == pytest.approx(15.0)      # 0.25 × 短边
    assert c["auto"] is True


def test_parse_cached_rejects_broken_json(env):
    row = {"rough_circle": "not json"}
    assert bgdetect.parse_cached(row) is None
    assert bgdetect.parse_cached({"rough_circle": ""}) is None
    assert bgdetect.parse_cached({"rough_circle": '{"cx":1}'}) is None
    assert bgdetect.parse_cached({"rough_circle": '{"cx":1,"cy":2,"a":3,"b":3,"th":0}'})["auto"] is True


# ---------------- 打开时的缓存 ----------------

def test_cached_or_detect_detects_once_and_stores(env, monkeypatch):
    conn = env
    iid = _make_image(conn)
    row = db.get_image(conn, iid)
    path = storage.resolve_path(row["path"])

    calls = []
    monkeypatch.setattr(bgdetect.detect, "detect_wells",
                        lambda img: calls.append(1) or [_Cand()])

    first = bgdetect.cached_or_detect(conn, row, path, 80, 60)
    second = bgdetect.cached_or_detect(conn, db.get_image(conn, iid), path, 80, 60)
    assert len(calls) == 1                       # 第二次走缓存，不再检测
    assert first == second
    assert json.loads(db.get_image(conn, iid)["rough_circle"])["cx"] == 30.0


def test_circle_from_file_uses_the_model_first(env, monkeypatch):
    """模型可用时优先用模型；不该再去跑 5 秒的 Hough。"""
    conn = env
    iid = _make_image(conn)
    path = storage.resolve_path(db.get_image(conn, iid)["path"])
    monkeypatch.setattr(bgdetect.detect_dl, "predict",
                        lambda img: {"cx": 11.0, "cy": 12.0, "a": 13.0,
                                     "b": 13.0, "th": 0.0, "auto": True})

    def boom(_img):
        raise AssertionError("模型可用时不该跑 Hough")

    monkeypatch.setattr(bgdetect.detect, "detect_wells", boom)
    c = bgdetect.circle_from_file(path)
    assert c["cx"] == 11.0 and c["a"] == 13.0


def test_circle_from_file_falls_back_when_model_unavailable(env, monkeypatch):
    """模型返回 None（文件缺失/onnxruntime 不可用/输出离谱）时才回退 Hough。"""
    conn = env
    iid = _make_image(conn)
    path = storage.resolve_path(db.get_image(conn, iid)["path"])
    monkeypatch.setattr(bgdetect.detect_dl, "predict", lambda img: None)
    monkeypatch.setattr(bgdetect.detect, "detect_wells", lambda img: [_Cand()])
    c = bgdetect.circle_from_file(path)
    assert c["a"] == 10.0


def test_cached_or_detect_does_not_detect_when_cached(env, monkeypatch):
    conn = env
    iid = _make_image(conn)
    db.set_image_rough_circle(conn, iid, json.dumps(
        {"cx": 1, "cy": 2, "a": 3, "b": 3, "th": 0, "auto": True}))

    def boom(_img):
        raise AssertionError("有缓存时不该再检测")

    monkeypatch.setattr(bgdetect.detect, "detect_wells", boom)
    c = bgdetect.cached_or_detect(conn, db.get_image(conn, iid),
                                  storage.resolve_path(db.get_image(conn, iid)["path"]),
                                  80, 60)
    assert c["cx"] == 1


# ---------------- 后台 _process ----------------

def test_process_caches_a_folder_image(env, monkeypatch):
    conn = env
    fid = _folder(conn)
    iid = _make_image(conn, folder_ids=[fid])
    monkeypatch.setattr(bgdetect.detect, "detect_wells", lambda img: [_Cand()])
    bgdetect._process(conn, iid)
    assert json.loads(db.get_image(conn, iid)["rough_circle"])["cx"] == 30.0


def test_process_skips_an_image_outside_folders(env, monkeypatch):
    conn = env
    iid = _make_image(conn)

    def boom(_img):
        raise AssertionError("不在文件夹里的图不该检测")

    monkeypatch.setattr(bgdetect.detect, "detect_wells", boom)
    bgdetect._process(conn, iid)
    assert db.get_image(conn, iid)["rough_circle"] is None


def test_process_skips_a_calibrated_image(env, monkeypatch):
    conn = env
    fid = _folder(conn)
    iid = _make_image(conn, folder_ids=[fid])
    db.set_calibration(conn, iid, "manual", 100.0, 3400.0)

    def boom(_img):
        raise AssertionError("已标定的图不该再预识别")

    monkeypatch.setattr(bgdetect.detect, "detect_wells", boom)
    bgdetect._process(conn, iid)
    assert db.get_image(conn, iid)["rough_circle"] is None


def test_process_skips_an_already_cached_image(env, monkeypatch):
    conn = env
    fid = _folder(conn)
    iid = _make_image(conn, folder_ids=[fid])
    db.set_image_rough_circle(conn, iid, '{"cx":9,"cy":9,"a":1,"b":1,"th":0}')

    def boom(_img):
        raise AssertionError("有缓存的图不该再检测")

    monkeypatch.setattr(bgdetect.detect, "detect_wells", boom)
    bgdetect._process(conn, iid)
    assert json.loads(db.get_image(conn, iid)["rough_circle"])["cx"] == 9


def test_missing_ids_only_folder_images_without_cache_or_calibration(env):
    conn = env
    fid = _folder(conn)
    a = _make_image(conn, "A", folder_ids=[fid])
    b = _make_image(conn, "B", folder_ids=[fid])
    c = _make_image(conn, "C", folder_ids=[fid])
    _make_image(conn, "D")                       # 不在文件夹里
    db.set_image_rough_circle(conn, b, '{"cx":1,"cy":1,"a":1,"b":1,"th":0}')
    db.set_calibration(conn, c, "manual", 100.0, 3400.0)
    assert bgdetect.missing_ids(conn) == [a]


# ---------------- API：打开时用缓存 ----------------

def _upload(client, name="照片.png", folder=None):
    params = {"mode": "direct"}
    if folder is not None:
        params["folder"] = str(folder)
    return client.post("/api/images/upload", params=params,
                       files=[("files", (name, _png(), "image/png"))]).json()["ok"][0]


def test_open_uses_the_cache_without_detecting(client, env, monkeypatch):
    item = _upload(client)
    conn = db.get_conn()
    try:
        db.set_image_rough_circle(conn, item["id"], json.dumps(
            {"cx": 12, "cy": 13, "a": 14, "b": 14, "th": 0, "auto": True}))
    finally:
        conn.close()

    def boom(_img):
        raise AssertionError("有缓存时打开不该再检测")

    monkeypatch.setattr(bgdetect.detect, "detect_wells", boom)
    r = client.post("/api/open", json={"image_id": item["id"]})
    assert r.status_code == 200, r.text
    c = r.json()["circle"]
    assert c["cx"] == 12 and c["a"] == 14


def test_open_writes_the_cache_when_missing(client, env, monkeypatch):
    item = _upload(client)
    monkeypatch.setattr(bgdetect.detect, "detect_wells", lambda img: [_Cand(21, 22, 23)])
    r = client.post("/api/open", json={"image_id": item["id"]})
    assert r.status_code == 200, r.text
    conn = db.get_conn()
    try:
        raw = db.get_image(conn, item["id"])["rough_circle"]
    finally:
        conn.close()
    assert json.loads(raw)["cx"] == 21.0
    assert r.json()["circle"]["cx"] == 21.0


# ---------------- 触发点 ----------------

def test_single_folder_assignment_enqueues(client, env, monkeypatch):
    item = _upload(client)
    fid = client.post("/api/folders", json={"name": "实验A"}).json()["id"]
    got = []
    monkeypatch.setattr(bgdetect, "enqueue", lambda ids: got.extend(ids))
    r = client.put(f"/api/images/{item['id']}/folders", json={"folder_ids": [fid]})
    assert r.status_code == 200
    assert got == [item["id"]]


def test_batch_folder_assignment_enqueues(client, env, monkeypatch):
    a = _upload(client, "a.png")
    b = _upload(client, "b.png")
    fid = client.post("/api/folders", json={"name": "实验A"}).json()["id"]
    got = []
    monkeypatch.setattr(bgdetect, "enqueue", lambda ids: got.extend(ids))
    r = client.post("/api/images/batch-folders",
                    json={"image_ids": [a["id"], b["id"]],
                          "folder_ids": [fid], "mode": "add"})
    assert r.status_code == 200
    assert sorted(got) == sorted([a["id"], b["id"]])


def test_import_into_folder_enqueues(client, env, monkeypatch):
    fid = client.post("/api/folders", json={"name": "实验A"}).json()["id"]
    got = []
    monkeypatch.setattr(bgdetect, "enqueue", lambda ids: got.extend(ids))
    item = _upload(client, "a.png", folder=fid)
    assert got == [item["id"]]


def test_confirm_pending_with_folder_enqueues(client, env, monkeypatch):
    fid = client.post("/api/folders", json={"name": "实验A"}).json()["id"]
    client.post("/api/images/upload", params={"mode": "pending", "folder": str(fid)},
                files=[("files", ("a.png", _png(), "image/png"))])
    got = []
    monkeypatch.setattr(bgdetect, "enqueue", lambda ids: got.extend(ids))
    r = client.post("/api/imports/confirm", json={})
    assert r.status_code == 200, r.text
    assert len(got) == 1
