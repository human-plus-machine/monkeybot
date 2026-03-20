"""Unit tests for emonk.core.browser_profiles."""

from unittest.mock import MagicMock, patch

from src.core.browser_profiles import sync_profiles_from_gcs

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _completed(returncode: int = 0, stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = ""
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# No-op cases
# ---------------------------------------------------------------------------

def test_noop_when_gcs_disabled(monkeypatch):
    monkeypatch.setenv("GCS_ENABLED", "false")
    with patch("src.core.browser_profiles.subprocess.run") as mock_run:
        sync_profiles_from_gcs()
    mock_run.assert_not_called()


def test_noop_when_gcs_enabled_env_missing(monkeypatch):
    monkeypatch.delenv("GCS_ENABLED", raising=False)
    with patch("src.core.browser_profiles.subprocess.run") as mock_run:
        sync_profiles_from_gcs()
    mock_run.assert_not_called()


def test_noop_when_bucket_not_set(monkeypatch, tmp_path):
    monkeypatch.setenv("GCS_ENABLED", "true")
    monkeypatch.delenv("GCS_MEMORY_BUCKET", raising=False)
    with patch("src.core.browser_profiles.subprocess.run") as mock_run:
        sync_profiles_from_gcs(profile_dir=str(tmp_path))
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_syncs_when_gcs_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("GCS_ENABLED", "true")
    monkeypatch.setenv("GCS_MEMORY_BUCKET", "my-bucket")

    with patch("src.core.browser_profiles.subprocess.run", return_value=_completed(0)) as mock_run:
        sync_profiles_from_gcs(profile_dir=str(tmp_path))

    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "gsutil"
    assert "gs://my-bucket/browser-profiles/" in cmd
    assert str(tmp_path) in cmd


def test_uses_env_bucket_and_profile_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("GCS_ENABLED", "true")
    monkeypatch.setenv("GCS_MEMORY_BUCKET", "env-bucket")
    monkeypatch.setenv("BROWSER_PROFILE_DIR", str(tmp_path))

    with patch("src.core.browser_profiles.subprocess.run", return_value=_completed(0)) as mock_run:
        sync_profiles_from_gcs()

    cmd = mock_run.call_args[0][0]
    assert "gs://env-bucket/browser-profiles/" in cmd
    assert str(tmp_path) in cmd


def test_args_override_env(monkeypatch, tmp_path):
    monkeypatch.setenv("GCS_ENABLED", "true")
    monkeypatch.setenv("GCS_MEMORY_BUCKET", "env-bucket")

    with patch("src.core.browser_profiles.subprocess.run", return_value=_completed(0)) as mock_run:
        sync_profiles_from_gcs(profile_dir=str(tmp_path), gcs_bucket="arg-bucket")

    cmd = mock_run.call_args[0][0]
    assert "gs://arg-bucket/browser-profiles/" in cmd
    assert str(tmp_path) in cmd


# ---------------------------------------------------------------------------
# Failure resilience
# ---------------------------------------------------------------------------

def test_does_not_raise_on_nonzero_returncode(monkeypatch, tmp_path):
    monkeypatch.setenv("GCS_ENABLED", "true")
    monkeypatch.setenv("GCS_MEMORY_BUCKET", "my-bucket")

    with patch("src.core.browser_profiles.subprocess.run", return_value=_completed(1, "Access denied")):
        sync_profiles_from_gcs(profile_dir=str(tmp_path))  # must not raise


def test_does_not_raise_on_subprocess_exception(monkeypatch, tmp_path):
    monkeypatch.setenv("GCS_ENABLED", "true")
    monkeypatch.setenv("GCS_MEMORY_BUCKET", "my-bucket")

    with patch("src.core.browser_profiles.subprocess.run", side_effect=FileNotFoundError("gsutil not found")):
        sync_profiles_from_gcs(profile_dir=str(tmp_path))  # must not raise
