import importlib


def test_healthz_uses_sqlite_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REQUIRE_POSTGRES", raising=False)

    litra_app = importlib.import_module("app")
    monkeypatch.setattr(litra_app, "DB_PATH", tmp_path / "app.sqlite3")
    monkeypatch.setattr(litra_app, "_DB_INITIALIZED", False)

    litra_app.init_db()
    litra_app.app.config["TESTING"] = True

    response = litra_app.app.test_client().get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "database": "sqlite"}


def test_anonymous_index_redirects_to_login(monkeypatch, tmp_path):
    litra_app = importlib.import_module("app")
    monkeypatch.setattr(litra_app, "DB_PATH", tmp_path / "app.sqlite3")
    monkeypatch.setattr(litra_app, "_DB_INITIALIZED", False)
    litra_app.app.config["TESTING"] = True

    response = litra_app.app.test_client().get("/")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
