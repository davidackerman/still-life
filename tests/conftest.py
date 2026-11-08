import os

import pytest


@pytest.fixture(autouse=True)
def fast_obituary(monkeypatch, tmp_path):
    """Universes that die in tests get a stand-in obituary (no ffmpeg, no minutes
    of rendering), written under tmp_path, never into the repo's out/ or archive/."""
    import death
    import obituary

    def fake(state_dir, shape, out, credit=None, scale=1, preview=None):
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "wb") as f:
            f.write(f"obituary of {state_dir} -> {shape} credit={credit}".encode())
        return out
    monkeypatch.setattr(obituary, "render", fake)
    monkeypatch.setattr(death, "OUT", str(tmp_path / "out" / "obituary.mp4"))
    monkeypatch.setattr(death, "ARCHIVE_ROOT", str(tmp_path / "archive"))
