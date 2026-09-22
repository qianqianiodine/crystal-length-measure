"""图像质量评估与增强测试。

核心不变式：增强不能改变图像尺寸（否则标定失效）。
"""
import numpy as np
import pytest

from app import enhance


@pytest.fixture()
def low_contrast_img():
    """低对比度灰蒙蒙的图。"""
    rng = np.random.default_rng(0)
    base = rng.integers(118, 138, (200, 300, 3), dtype=np.uint8)
    return base


@pytest.fixture()
def good_img():
    """对比度正常的图。"""
    rng = np.random.default_rng(1)
    return rng.integers(0, 256, (200, 300, 3), dtype=np.uint8)


def test_assess_quality_returns_expected_keys(good_img):
    r = enhance.assess_quality(good_img)
    for k in ("blur", "contrast", "noise", "illumination", "warnings"):
        assert k in r
    assert isinstance(r["warnings"], list)


def test_assess_flags_low_contrast(low_contrast_img):
    r = enhance.assess_quality(low_contrast_img)
    assert r["contrast"] < 20          # 标准差很小
    assert any("对比度" in w for w in r["warnings"])


def test_assess_does_not_flag_good_image(good_img):
    r = enhance.assess_quality(good_img)
    assert not any("对比度偏低" in w for w in r["warnings"])


def test_enhance_preserves_shape(low_contrast_img):
    """★ 关键不变式：尺寸不能变。"""
    p = enhance.EnhanceParams(clahe_clip=3.0, denoise_h=5.0, sharpen_amount=0.5)
    out = enhance.apply_enhance(low_contrast_img, p)
    assert out.shape == low_contrast_img.shape
    assert out.dtype == np.uint8


def test_enhance_increases_contrast(low_contrast_img):
    p = enhance.EnhanceParams(clahe_clip=3.0, denoise_h=0.0, sharpen_amount=0.0)
    out = enhance.apply_enhance(low_contrast_img, p)
    assert out.std() > low_contrast_img.std()


def test_enhance_zero_params_is_near_identity(low_contrast_img):
    """参数全为 0 时应几乎不变（允许极小数值误差）。"""
    p = enhance.EnhanceParams(clahe_clip=0.0, denoise_h=0.0, sharpen_amount=0.0,
                              illum_balance=False)
    out = enhance.apply_enhance(low_contrast_img, p)
    assert np.abs(out.astype(int) - low_contrast_img.astype(int)).mean() < 1.0


def test_auto_params_reacts_to_low_contrast():
    rep = {"blur": 500.0, "contrast": 12.0, "noise": 3.0,
           "illumination": 5.0, "warnings": []}
    p = enhance.auto_params(rep)
    assert p.clahe_clip > 2.0          # 低对比度应加大 CLAHE


def test_auto_params_reacts_to_noise():
    rep = {"blur": 500.0, "contrast": 60.0, "noise": 25.0,
           "illumination": 5.0, "warnings": []}
    p = enhance.auto_params(rep)
    assert p.denoise_h > 0


def test_preview_limits_size(good_img):
    big = np.zeros((2400, 3200, 3), np.uint8)
    out = enhance.enhance_for_preview(big, enhance.EnhanceParams(), max_side=1200)
    assert max(out.shape[:2]) <= 1200


# ---------------- 画质参数的读回 ----------------

def _row(enhance):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT ? AS enhance", (enhance,)).fetchone()


def test_parse_enhance_没调过回中性值():
    from app import enhance

    assert enhance.parse_enhance(_row(None)) == {"b": 1.0, "c": 1.0, "s": 1.0}


def test_parse_enhance_读得回来():
    from app import enhance

    assert enhance.parse_enhance(_row('{"b": 1.5, "c": 1.2, "s": 0.8}')) == \
        {"b": 1.5, "c": 1.2, "s": 0.8}


@pytest.mark.parametrize("坏", ["{", "[]", '"甲"', '{"b": "x"}', '{"c": null}'])
def test_parse_enhance_存档坏了也回中性值(坏):
    from app import enhance

    assert enhance.parse_enhance(_row(坏)) == {"b": 1.0, "c": 1.0, "s": 1.0}


def test_parse_enhance_少一个键就补中性值():
    from app import enhance

    assert enhance.parse_enhance(_row('{"b": 1.5}')) == {"b": 1.5, "c": 1.0, "s": 1.0}


def test_apply_tone_和浏览器算的一模一样():
    """把 measure.html 的伽马曲线 + CSS 滤镜公式在 node 里跑一遍，逐像素比对。

    这道桥是**故意**搭的：屏幕和导出图必须是同一个样子，而实现天然是两份
    （浏览器一份、Python 一份）。没有这条测试，两边各自漂走也没人知道。

    ⚠️ JS 那边的 contrast/saturate 是照 CSS Filter Effects 规范写的，不是从
    Chrome 里抠出来的 —— 所以这条证明的是「两边算式一致」；「和真浏览器一致」
    靠人工验收第 14 条。node 没装就 skip，不假装通过。
    """
    import json
    import shutil
    import subprocess

    from app import enhance

    if not shutil.which("node"):
        pytest.skip("没装 node")

    js = r"""
    const L = (b) => { const a = new Uint8ClampedArray(256);
      for (let i = 0; i < 256; i++) a[i] = 255 * Math.pow(i / 255, 1 / b); return [...a]; };
    const px = (r, g, bl, b, c, s) => {
      const t = L(b);
      let x = [t[r] / 255, t[g] / 255, t[bl] / 255];
      if (c !== 1) x = x.map(v => c * (v - 0.5) + 0.5);
      if (s !== 1) { const lum = 0.213 * x[0] + 0.715 * x[1] + 0.072 * x[2];
                     x = x.map(v => lum * (1 - s) + v * s); }
      return x.map(v => Math.round(Math.min(1, Math.max(0, v)) * 255));
    };
    const cases = [[0,0,0],[255,255,255],[40,40,40],[128,128,128],[200,30,90],[10,220,60]];
    const out = [];
    for (const b of [0.5, 1, 1.5, 2]) for (const c of [0.5, 1, 2.5]) for (const s of [0, 1, 2])
      for (const p of cases) out.push([b, c, s, ...p, ...px(p[0], p[1], p[2], b, c, s)]);
    console.log(JSON.stringify(out));
    """
    raw = subprocess.run(["node", "-e", js], capture_output=True, text=True,
                         check=True, encoding="utf-8").stdout
    rows = json.loads(raw)
    assert rows, "node 没吐东西出来"

    bad = []
    for b, c, s, r, g, bl, *want in rows:
        img = np.array([[[r, g, bl]]], np.uint8)
        got = enhance.apply_tone(img, {"b": b, "c": c, "s": s})[0, 0].tolist()
        # 只允许差 1：JS 的 Math.round 是四舍五入、numpy 默认是四舍六入五取偶，
        # 正好落在 .5 上的像素会差 1。差 ≥2 就说明是算式分叉了。
        if max(abs(a - w) for a, w in zip(got, want)) > 1:
            bad.append(f"b={b} c={c} s={s} RGB({r},{g},{bl}): "
                       f"浏览器 {want} vs 服务端 {got}")
    assert not bad, "屏幕和导出图会不一样：\n" + "\n".join(bad[:8])
