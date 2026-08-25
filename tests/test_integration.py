"""CLI, config, store and server tests."""

import json
from pathlib import Path

import pytest

from syshealth import config
from syshealth.cli import EXIT_ERROR, EXIT_OK, EXIT_SATURATED, main
from syshealth.store import Store

RUNS = Path(__file__).parent / "fixtures" / "runs"
PROC = Path(__file__).parent / "fixtures" / "proc"


# ----------------------------------------------------------------- cli ----


def test_report_on_saturated_run_exits_nonzero(capsys):
    """Exit code 2 makes the tool usable as a CI or deploy gate."""
    code = main(["report", str(RUNS / "thrashing.jsonl"), "--instance-type", "t3.micro"])
    assert code == EXIT_SATURATED
    assert "UNDERSIZED" in capsys.readouterr().out


def test_report_on_healthy_run_exits_zero(capsys):
    code = main(["report", str(RUNS / "idle-oversized.jsonl"), "--instance-type", "t3.large"])
    assert code == EXIT_OK
    assert "OVERSIZED" in capsys.readouterr().out


def test_report_json_is_machine_readable(capsys):
    code = main(["report", str(RUNS / "cache-heavy.jsonl"), "--instance-type", "t3.large", "--json"])
    assert code == EXIT_OK

    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"]["sizing"] in {"OVERSIZED", "RIGHT_SIZED"}
    assert payload["summary"]["resources"]["memory"]["state"] == "HEALTHY"
    assert payload["verdict"]["evidence"]


def test_report_on_missing_file_fails_cleanly(capsys):
    assert main(["report", "/nonexistent/run.jsonl"]) == EXIT_ERROR
    assert "no such file" in capsys.readouterr().err


def test_report_skips_corrupt_lines(tmp_path, capsys):
    path = tmp_path / "mixed.jsonl"
    good = (RUNS / "thrashing.jsonl").read_text().splitlines()[:20]
    path.write_text("\n".join(good + ["{not json", ""]) + "\n")

    assert main(["report", str(path), "--instance-type", "t3.micro"]) in (EXIT_OK, EXIT_SATURATED)
    assert "skipping line" in capsys.readouterr().err


def test_doctor_reports_ok_on_fixture_tree(capsys):
    assert main(["doctor", "--proc-root", str(PROC)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "PSI available" in out
    assert "ready" in out


def test_doctor_explains_missing_psi(tmp_path, capsys):
    code = main(["doctor", "--proc-root", str(tmp_path)])
    assert code != EXIT_OK
    out = capsys.readouterr().out
    assert "PSI unavailable" in out
    assert "CONFIG_PSI" in out


def test_agent_without_server_url_fails_clearly(capsys, monkeypatch):
    monkeypatch.delenv("SYSHEALTH_SERVER_URL", raising=False)
    assert main(["agent", "--proc-root", str(PROC)]) == EXIT_ERROR
    assert "no server URL" in capsys.readouterr().err


def test_headroom_flag_changes_recommendation(capsys):
    main(["report", str(RUNS / "idle-oversized.jsonl"), "--instance-type", "t3.large",
          "--headroom", "20", "--json"])
    lean = json.loads(capsys.readouterr().out)["verdict"]["recommended"]["ram_gb"]

    main(["report", str(RUNS / "idle-oversized.jsonl"), "--instance-type", "t3.large",
          "--headroom", "400", "--json"])
    generous = json.loads(capsys.readouterr().out)["verdict"]["recommended"]["ram_gb"]

    assert generous > lean


# -------------------------------------------------------------- config ----


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("SYSHEALTH_INTERVAL_S", "7.5")
    monkeypatch.setenv("SYSHEALTH_SERVER_URL", "http://example.invalid:5000")
    settings = config.load()
    assert settings.interval_s == 7.5
    assert settings.server_url == "http://example.invalid:5000"


def test_explicit_override_beats_env(monkeypatch):
    monkeypatch.setenv("SYSHEALTH_INTERVAL_S", "7.5")
    assert config.load(interval_s=1.0).interval_s == 1.0


def test_none_overrides_are_ignored(monkeypatch):
    """Unset argparse flags are None and must not clobber env or file values."""
    monkeypatch.setenv("SYSHEALTH_INTERVAL_S", "7.5")
    assert config.load(interval_s=None).interval_s == 7.5


def test_bad_numeric_env_gives_a_readable_error(monkeypatch):
    monkeypatch.setenv("SYSHEALTH_INTERVAL_S", "not-a-number")
    with pytest.raises(ValueError, match="interval_s must be a float"):
        config.load()


def test_node_name_defaults_to_hostname():
    assert config.load().node_name


def test_config_file_is_read(tmp_path, monkeypatch):
    path = tmp_path / "syshealth.toml"
    path.write_text('[syshealth]\ninterval_s = 3.0\ninstance_type = "t3.small"\n')
    monkeypatch.delenv("SYSHEALTH_INTERVAL_S", raising=False)

    settings = config.load(path)
    assert settings.interval_s == 3.0
    assert settings.instance_type == "t3.small"


# --------------------------------------------------------------- store ----


def test_store_persists_across_reopen(tmp_path):
    """The failure the in-memory dict had: a restart lost everything."""
    path = tmp_path / "s.db"

    first = Store(path)
    first.record("node-a", {"duration_s": 2.0}, instance_type="t3.micro")
    first.close()

    second = Store(path)
    assert second.count() == 1
    assert second.samples("node-a")[0]["duration_s"] == 2.0
    assert second.nodes()[0]["instance_type"] == "t3.micro"


def test_store_marks_stale_nodes_offline(tmp_path):
    store = Store(tmp_path / "s.db")
    store.record("old", {}, now=1000.0)
    assert store.nodes(online_window_s=20.0, now=1000.0 + 5)[0]["online"] is True
    assert store.nodes(online_window_s=20.0, now=1000.0 + 60)[0]["online"] is False


def test_store_returns_samples_oldest_first(tmp_path):
    store = Store(tmp_path / "s.db")
    for i in range(5):
        store.record("n", {"i": i}, now=1000.0 + i)
    assert [s["i"] for s in store.samples("n")] == [0, 1, 2, 3, 4]


def test_store_limit_keeps_the_newest(tmp_path):
    store = Store(tmp_path / "s.db")
    for i in range(10):
        store.record("n", {"i": i}, now=1000.0 + i)
    assert [s["i"] for s in store.samples("n", limit=3)] == [7, 8, 9]


def test_store_enforces_retention(tmp_path):
    store = Store(tmp_path / "s.db", retention_s=100)
    store.record("n", {"i": "old"}, now=1000.0)
    store.record("n", {"i": "new"}, now=1000.0 + 500)
    assert store.count() == 1


# -------------------------------------------------------------- server ----

flask = pytest.importorskip("flask", reason="server extra not installed")


@pytest.fixture
def client(tmp_path):
    from syshealth.config import Settings
    from syshealth.server import create_app

    app = create_app(Settings(db_path=str(tmp_path / "t.db")))
    app.config["TESTING"] = True
    return app.test_client()


def test_healthz(client):
    assert client.get("/healthz").get_json()["ok"] is True


def test_ingest_rejects_malformed_payload(client):
    assert client.post("/metrics", json={"nope": 1}).status_code == 400
    assert client.post("/metrics", json={"node": "a"}).status_code == 400


def test_ingest_then_verdict(client):
    samples = [
        json.loads(line)
        for line in (RUNS / "thrashing.jsonl").read_text().splitlines()
        if line.strip()
    ]
    for sample in samples:
        response = client.post(
            "/metrics",
            json={"node": "web-1", "instance_type": "t3.micro", "sample": sample},
        )
        assert response.status_code == 202

    verdict = client.get("/nodes/web-1/verdict").get_json()
    assert verdict["sizing"] == "UNDERSIZED"
    assert verdict["monthly_delta_usd"] > 0

    fleet = client.get("/fleet").get_json()
    assert fleet["undersized"] == 1
    assert len(fleet["nodes"]) == 1


def test_verdict_for_unknown_node_is_404(client):
    assert client.get("/nodes/ghost/verdict").status_code == 404
