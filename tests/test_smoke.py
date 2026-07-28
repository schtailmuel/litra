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
    assert b"How to annotate" in evaluate_page.data

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

    revisit_page = client.get(f"/he/eval-token?item={item['id']}")
    assert revisit_page.status_code == 200
    assert b"Update Evaluation" in revisit_page.data

    revisit_submit = client.post(
        "/he/eval-token",
        data={
            "item_id": str(item["id"]),
            "model_ids": [str(model_ids[0]), str(model_ids[1])],
            f"rank_{model_ids[0]}": "2",
            f"rank_{model_ids[1]}": "1",
            f"error_span_{model_ids[0]}": "",
            f"error_span_{model_ids[1]}": "",
            f"comment_{model_ids[0]}": "updated-first",
            f"comment_{model_ids[1]}": "updated-second",
        },
    )
    assert revisit_submit.status_code == 302

    with litra_app.db() as conn:
        rating_count_after_revisit = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM human_eval_ratings hr
            JOIN human_eval_links hel ON hel.id = hr.link_id
            WHERE hel.token = 'eval-token'
            """
        ).fetchone()["count"]
        updated_rankings = conn.execute(
            """
            SELECT hrk.rank_value, hrk.comment
            FROM human_eval_rankings hrk
            JOIN human_eval_ratings hr ON hr.id = hrk.rating_id
            JOIN human_eval_links hel ON hel.id = hr.link_id
            WHERE hel.token = 'eval-token'
            ORDER BY hrk.model_id
            """
        ).fetchall()

    assert rating_count_after_revisit == 1
    assert [row["rank_value"] for row in updated_rankings] == [2, 1]
    assert [row["comment"] for row in updated_rankings] == ["updated-first", "updated-second"]

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

    rated_items_page = client.get("/he/eval-token/rated")
    assert rated_items_page.status_code == 200
    assert b"Restricted Evaluator View" in rated_items_page.data

    delete_my_rating = client.post(
        "/he/eval-token/rated",
        data={
            "action": "delete",
            "item_id": str(item["id"]),
        },
    )
    assert delete_my_rating.status_code == 302

    with litra_app.db() as conn:
        remaining_my_ratings = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM human_eval_ratings hr
            JOIN human_eval_links hel ON hel.id = hr.link_id
            WHERE hel.token = 'eval-token'
            """
        ).fetchone()["count"]
    assert remaining_my_ratings == 0


def test_project_access_sharing_and_dashboards(monkeypatch, tmp_path):
    litra_app = importlib.import_module("app")
    monkeypatch.setattr(litra_app, "DB_PATH", tmp_path / "app.sqlite3")
    monkeypatch.setattr(litra_app, "_DB_INITIALIZED", False)

    litra_app.init_db()
    litra_app.app.config["TESTING"] = True

    with litra_app.db() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("owner", "hash", litra_app.now_iso()),
        )
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("collab", "hash", litra_app.now_iso()),
        )
        owner_id = conn.execute("SELECT id FROM users WHERE username = ?", ("owner",)).fetchone()["id"]
        collab_id = conn.execute("SELECT id FROM users WHERE username = ?", ("collab",)).fetchone()["id"]

        project_id = conn.execute(
            """
            INSERT INTO projects (owner_id, name, source_language, source_editable, import_mapping, created_at)
            VALUES (?, ?, ?, 1, '{}', ?)
            """,
            (owner_id, "Shared Translation", "German", litra_app.now_iso()),
        ).lastrowid

        eval_project_id = conn.execute(
            """
            INSERT INTO human_eval_projects (owner_id, name, source_language, target_language, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (owner_id, "Shared Eval", "German", "English", litra_app.now_iso()),
        ).lastrowid
        conn.commit()

    client = litra_app.app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = owner_id

    missing_user_response = client.post(
        f"/projects/{project_id}",
        data={
            "action": "grant_project_access",
            "shared_username": "does-not-exist",
        },
        follow_redirects=True,
    )
    assert missing_user_response.status_code == 200
    assert b"User does not exist." in missing_user_response.data

    grant_translation = client.post(
        f"/projects/{project_id}",
        data={
            "action": "grant_project_access",
            "shared_username": "collab",
        },
    )
    assert grant_translation.status_code == 302

    grant_human_eval = client.post(
        f"/human-evaluation/{eval_project_id}",
        data={
            "action": "grant_human_eval_project_access",
            "shared_username": "collab",
        },
    )
    assert grant_human_eval.status_code == 302

    with client.session_transaction() as session:
        session["user_id"] = collab_id

    translation_dashboard = client.get("/dashboard")
    assert translation_dashboard.status_code == 200
    assert b"Shared Translation" in translation_dashboard.data
    assert b"Shared" in translation_dashboard.data

    human_eval_dashboard = client.get("/human-evaluation")
    assert human_eval_dashboard.status_code == 200
    assert b"Shared Eval" in human_eval_dashboard.data
    assert b"Shared" in human_eval_dashboard.data

    with client.session_transaction() as session:
        session["user_id"] = owner_id

    remove_translation = client.post(
        f"/projects/{project_id}",
        data={
            "action": "remove_project_access",
            "access_user_id": str(collab_id),
        },
    )
    assert remove_translation.status_code == 302

    remove_human_eval = client.post(
        f"/human-evaluation/{eval_project_id}",
        data={
            "action": "remove_human_eval_project_access",
            "access_user_id": str(collab_id),
        },
    )
    assert remove_human_eval.status_code == 302

    with client.session_transaction() as session:
        session["user_id"] = collab_id

    translation_dashboard_after = client.get("/dashboard")
    assert translation_dashboard_after.status_code == 200
    assert b"Shared Translation" not in translation_dashboard_after.data

    human_eval_dashboard_after = client.get("/human-evaluation")
    assert human_eval_dashboard_after.status_code == 200
    assert b"Shared Eval" not in human_eval_dashboard_after.data


def test_translation_comment_filters_and_project_jsonl_export(monkeypatch, tmp_path):
    litra_app = importlib.import_module("app")
    monkeypatch.setattr(litra_app, "DB_PATH", tmp_path / "app.sqlite3")
    monkeypatch.setattr(litra_app, "_DB_INITIALIZED", False)

    litra_app.init_db()
    litra_app.app.config["TESTING"] = True

    with litra_app.db() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            ("owner", "hash", litra_app.now_iso()),
        )
        owner_id = conn.execute(
            "SELECT id FROM users WHERE username = ?",
            ("owner",),
        ).fetchone()["id"]

        project_id = conn.execute(
            """
            INSERT INTO projects (owner_id, name, source_language, source_editable, import_mapping, created_at)
            VALUES (?, ?, ?, 1, '{}', ?)
            """,
            (owner_id, "Translate Export", "English", litra_app.now_iso()),
        ).lastrowid

        conn.execute(
            "INSERT INTO project_languages (project_id, target_language, created_at) VALUES (?, ?, ?)",
            (project_id, "German", litra_app.now_iso()),
        )
        segment_id = conn.execute(
            """
            INSERT INTO segments
                (project_id, identifier, ordinal, source_language, source_text, instructions, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, "msg-1", 1, "English", "Hello world", "", "{}", litra_app.now_iso()),
        ).lastrowid

        conn.execute(
            """
            INSERT INTO translations
                (segment_id, target_language, target_text, comment, status, qa_warnings, version, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment_id,
                "German",
                "Hallo Welt",
                "",
                "submitted",
                "[]",
                1,
                "translator",
                litra_app.now_iso(),
            ),
        )
        conn.execute(
            """
            INSERT INTO translation_comments
                (segment_id, target_language, role, body, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (segment_id, "German", "reviewer", "Needs tone adjustment", "reviewer", litra_app.now_iso()),
        )
        conn.execute(
            """
            INSERT INTO share_links
                (project_id, token, target_language, label, translator_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, "tok-1", "German", "", "translator", litra_app.now_iso()),
        )
        conn.commit()

    client = litra_app.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = owner_id

    language_data = client.get(f"/projects/{project_id}/languages/German/data?comments=1")
    assert language_data.status_code == 200
    assert b"msg-1" in language_data.data

    project_data = client.get(f"/projects/{project_id}/translation-data?comments=1")
    assert project_data.status_code == 200
    assert b"msg-1" in project_data.data

    translator_view = client.get("/t/tok-1/translations?comments=1")
    assert translator_view.status_code == 200
    assert b"msg-1" in translator_view.data
    assert b"thread comment" in translator_view.data

    export_response = client.post(
        f"/projects/{project_id}/export-jsonl",
        data={"languages": ["German"]},
    )
    assert export_response.status_code == 200
    lines = [line for line in export_response.get_data(as_text=True).splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["identifier"] == "msg-1"
