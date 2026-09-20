import pytest

try:  # dtm_agent のテストだけが必要とする。未インストールでも jev_usecases のテストは動くようにする
    import numpy as np
    import soundfile as sf
except ImportError:  # pragma: no cover
    np = sf = None


def _require_audio() -> None:
    if np is None or sf is None:
        pytest.skip("numpy / soundfile が未インストール (pip install -e '.[dev]')")

SR = 22050


def tone(freqs, seconds, sr=SR, amp=0.2, bpm=None):
    t = np.arange(0, seconds, 1 / sr)
    y = np.zeros_like(t)
    for i, f in enumerate(freqs):
        env = ((np.floor(t * 2) % len(freqs)) == i).astype(float)
        y += amp * np.sin(2 * np.pi * f * t) * env
    if bpm:
        rng = np.random.default_rng(0)
        for b in np.arange(0, seconds, 60.0 / bpm):
            i0 = int(b * sr)
            y[i0:i0 + 200] += rng.standard_normal(min(200, len(y) - i0)) * 0.5
    return y.astype(np.float32)


def noise_burst(seconds, sr=SR, seed=1, lowpass=False):
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    y = rng.standard_normal(n) * np.exp(-np.linspace(0, 8, n))
    if lowpass:
        k = 64
        y = np.convolve(y, np.ones(k) / k, mode="same")
    return (y / (np.abs(y).max() + 1e-9) * 0.9).astype(np.float32)


@pytest.fixture
def reference_wav(tmp_path):
    _require_audio()
    # A minor アルペジオ + 120 BPM のクリック、8 秒
    path = tmp_path / "ref.wav"
    sf.write(path, tone([220.0, 261.63, 329.63], 8.0, bpm=120), SR)
    return path


@pytest.fixture
def sample_library(tmp_path):
    """kick 系 (低域ノイズ) と hat 系 (高域ノイズ) と pad 系 (正弦波) の小さなライブラリ。"""
    _require_audio()
    root = tmp_path / "samples"
    (root / "drums" / "kicks").mkdir(parents=True)
    (root / "drums" / "hats").mkdir(parents=True)
    (root / "synth" / "pads").mkdir(parents=True)
    sf.write(root / "drums" / "kicks" / "kick_808_deep.wav", noise_burst(0.5, lowpass=True, seed=1), SR)
    sf.write(root / "drums" / "kicks" / "kick_punchy.wav", noise_burst(0.4, lowpass=True, seed=2), SR)
    sf.write(root / "drums" / "hats" / "hat_closed_bright.wav", noise_burst(0.2, seed=3), SR)
    sf.write(root / "synth" / "pads" / "pad_warm_Am.wav", tone([220.0, 261.63, 329.63], 2.0), SR)
    sf.write(root / "synth" / "pads" / "pad_bright_C.wav", tone([523.25, 659.25, 783.99], 2.0), SR)
    return root
