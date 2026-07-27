import importlib
import json
from io import BytesIO


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


def test_human_evaluation_project_flow(monkeypatch, tmp_path):
    litra_app = importlib.import_module("app")
    monkeypatch.setattr(litra_app, "DB_PATH", tmp_path / "app.sqlite3")
    monkeypatch.setattr(litra_app, "_DB_INITIALIZED", False)

    litra_app.init_db()
    litra_app.app.config["TESTING"] = True

    with litra_app.db() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("manager", "hash", litra_app.now_iso()),
        )
        user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("manager",)).fetchone()["id"]
        conn.commit()

    client = litra_app.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id

    response = client.post(
        "/human-evaluation/new",
        data={
            "name": "Eval 1",
            "source_language": "German",
            "target_language": "English",
            "source_txt": (BytesIO("Hallo\nTschüss\n".encode("utf-8")), "source.txt"),
            "reference_txt": (BytesIO("Hello\nBye\n".encode("utf-8")), "reference.txt"),
            "model_files": [
                (BytesIO("Hello\nBye\n".encode("utf-8")), "model-a.txt"),
                (BytesIO("Hi\nGoodbye\n".encode("utf-8")), "model-b.txt"),
            ],
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 302
    assert "/human-evaluation/" in response.headers["Location"]

    with litra_app.db() as conn:
        project = conn.execute("SELECT * FROM human_eval_projects WHERE owner_id = ?", (user_id,)).fetchone()
        assert project is not None
        item = conn.execute("SELECT * FROM human_eval_items WHERE project_id = ? ORDER BY ordinal LIMIT 1", (project["id"],)).fetchone()
        model_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM human_eval_models WHERE project_id = ? ORDER BY id",
                (project["id"],),
            ).fetchall()
        ]
        conn.execute(
            """
            INSERT INTO human_eval_links
                (project_id, token, evaluator_name, credit_limit, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project["id"], "eval-token", "Eva", 5, litra_app.now_iso()),
        )
        conn.commit()

    evaluate_page = client.get("/he/eval-token")
    assert evaluate_page.status_code == 200
    assert b"Candidate" in evaluate_page.data

    submit_response = client.post(
        "/he/eval-token",
        data={
            "item_id": str(item["id"]),
            "model_ids": [str(model_ids[0]), str(model_ids[1])],
            f"rank_{model_ids[0]}": "1",
            f"rank_{model_ids[1]}": "1",
            f"error_span_{model_ids[0]}": "",
            f"error_span_{model_ids[1]}": "",
            f"comment_{model_ids[0]}": "best",
            f"comment_{model_ids[1]}": "second",
        },
    )
    assert submit_response.status_code == 302

    with litra_app.db() as conn:
        rating_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM human_eval_ratings hr
            JOIN human_eval_links hel ON hel.id = hr.link_id
            WHERE hel.token = 'eval-token'
            """
        ).fetchone()["count"]
    assert rating_count == 1

    manager_download = client.get(f"/human-evaluation/{project['id']}/download-annotations")
    assert manager_download.status_code == 200
    manager_payload = json.loads(manager_download.get_data(as_text=True))
    assert manager_payload["project"]["id"] == project["id"]
    assert len(manager_payload["ratings"]) == 1

    evaluator_download = client.get("/he/eval-token/download-annotations")
    assert evaluator_download.status_code == 200
    evaluator_payload = json.loads(evaluator_download.get_data(as_text=True))
    assert evaluator_payload["project"]["id"] == project["id"]
    assert len(evaluator_payload["ratings"]) == 1

    public_stats_page = client.get("/he/eval-token/stats")
    assert public_stats_page.status_code == 200
    public_stats_api = client.get("/api/he/eval-token/stats")
    assert public_stats_api.status_code == 200
    assert public_stats_api.get_json()["status"] == "ok"

    annotations_page = client.get(f"/human-evaluation/{project['id']}/annotations")
    assert annotations_page.status_code == 200

    with litra_app.db() as conn:
        ranking_id = conn.execute(
            """
            SELECT hrk.id
            FROM human_eval_rankings hrk
            JOIN human_eval_ratings hr ON hr.id = hrk.rating_id
            JOIN human_eval_links hel ON hel.id = hr.link_id
            WHERE hel.token = 'eval-token'
            ORDER BY hrk.id
            LIMIT 1
            """
        ).fetchone()["id"]

    update_response = client.post(
        f"/human-evaluation/{project['id']}/annotations",
        data={
            "action": "save_entry",
            "ranking_id": str(ranking_id),
            "rank_value": "2",
            "comment": "manager-edited",
            "error_span": '{"source_marks":[],"style_marks":[]}',
        },
    )
    assert update_response.status_code == 302

    with litra_app.db() as conn:
        updated = conn.execute(
            "SELECT comment, rank_value FROM human_eval_rankings WHERE id = ?",
            (ranking_id,),
        ).fetchone()
    assert updated["comment"] == "manager-edited"
    assert updated["rank_value"] == 2

    delete_response = client.post(
        f"/human-evaluation/{project['id']}/annotations",
        data={
            "action": "delete_entry",
            "ranking_id": str(ranking_id),
        },
    )
    assert delete_response.status_code == 302

    with litra_app.db() as conn:
        remaining_rankings = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM human_eval_rankings hrk
            JOIN human_eval_ratings hr ON hr.id = hrk.rating_id
            JOIN human_eval_links hel ON hel.id = hr.link_id
            WHERE hel.token = 'eval-token'
            """
        ).fetchone()["count"]
    assert remaining_rankings == 1
