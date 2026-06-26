"""
Тесты API per-project тумблера deploy_check (GET/POST /api/settings/deploy-check/{slug}).
Включённый deploy_check:<slug> — это opt-in для детерминированного детекта «не выкачено»
(svc._deploy_not_done): задача с rc=0 и зелёной валидацией, но без нового коммита,
уходит в not_deployed вместо done. Здесь проверяем только хранение флага через API.
"""
import app
import db
from conftest import TOKEN

_H = {"Authorization": f"Bearer {TOKEN}"}  # POST под auth-middleware


def _iso(client, monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()


def test_deploy_check_default_off(client, tmp_path, monkeypatch):
    _iso(client, monkeypatch, tmp_path)
    r = client.get("/api/settings/deploy-check/-home-nel-proj")
    assert r.status_code == 200
    assert r.json() == {"slug": "-home-nel-proj", "enabled": False}


def test_deploy_check_enable_then_read(client, tmp_path, monkeypatch):
    _iso(client, monkeypatch, tmp_path)
    slug = "-home-nel-proj"
    assert client.post(f"/api/settings/deploy-check/{slug}",
                       json={"enabled": True}, headers=_H).json()["enabled"] is True
    # читается обратно как включённый, и сам детект увидит opt-in
    assert client.get(f"/api/settings/deploy-check/{slug}").json()["enabled"] is True
    assert db.get_setting(f"deploy_check:{slug}") == "1"


def test_deploy_check_disable(client, tmp_path, monkeypatch):
    _iso(client, monkeypatch, tmp_path)
    slug = "-home-nel-proj"
    client.post(f"/api/settings/deploy-check/{slug}", json={"enabled": True}, headers=_H)
    client.post(f"/api/settings/deploy-check/{slug}", json={"enabled": False}, headers=_H)
    assert client.get(f"/api/settings/deploy-check/{slug}").json()["enabled"] is False
    assert db.get_setting(f"deploy_check:{slug}") == "0"
