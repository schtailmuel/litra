import importlib
import json
import zipfile
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
            f"error_span_{model_ids[0]}": '{"source_marks":[{"start":0,"end":5,"text":"Hallo","note":"wrong term"}],"style_marks":[]}',
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
            SELECT hrk.rank_value, hrk.comment, hrk.error_span
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
    assert json.loads(updated_rankings[0]["error_span"])["source_marks"][0]["note"] == "wrong term"

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
    public_stats_payload = public_stats_api.get_json()
    assert public_stats_payload["status"] == "ok"
    assert public_stats_payload["rank_levels"] == [1, 2]
    rows_by_model = {
        int(row["model_id"]): row
        for row in public_stats_payload.get("rows", [])
    }
    assert rows_by_model[model_ids[0]]["rank_distribution_display"] == "2: 1 (100.0%)"
    assert rows_by_model[model_ids[1]]["rank_distribution_display"] == "1: 1 (100.0%)"
    assert rows_by_model[model_ids[0]]["uncommented_word_rate_display"] == "100.0%"
    assert rows_by_model[model_ids[1]]["uncommented_word_rate_display"] == "100.0%"
    assert "wrong term" in rows_by_model[model_ids[0]]["comments_blob"]
    assert rows_by_model[model_ids[0]]["avg_rank_ci_display"].startswith("95% CI")
    assert rows_by_model[model_ids[0]]["significantly_better_than_next"] is False
    assert rows_by_model[model_ids[1]]["significantly_better_than_next"] is False

    top_model_id = min(
        rows_by_model.keys(),
        key=lambda model_id: float(rows_by_model[model_id]["avg_rank"]),
    )
    top_row = rows_by_model[top_model_id]
    assert top_row["next_pair_pvalue_display"] == "1.000"
    assert top_row["next_model_name"] in {"model-a", "model-b", "model-a.txt", "model-b.txt"}
    assert "wins=" in top_row["next_pair_summary"]

    pairwise_by_model = {
        int(row["model_id"]): row
        for row in public_stats_payload.get("pairwise", [])
    }
    row_a_cells = {
        int(cell["target_model_id"]): cell
        for cell in pairwise_by_model[model_ids[0]]["cells"]
    }
    row_b_cells = {
        int(cell["target_model_id"]): cell
        for cell in pairwise_by_model[model_ids[1]]["cells"]
    }
    assert row_a_cells[model_ids[1]]["display"] == "0.0%"
    assert row_b_cells[model_ids[0]]["display"] == "100.0%"

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


def test_human_evaluation_project_allows_missing_reference(monkeypatch, tmp_path):
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
            "name": "Eval without refs",
            "source_language": "German",
            "target_language": "English",
            "source_txt": (BytesIO("Hallo\nTschüss\n".encode("utf-8")), "source.txt"),
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
        item = conn.execute(
            "SELECT * FROM human_eval_items WHERE project_id = ? ORDER BY ordinal LIMIT 1",
            (project["id"],),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO human_eval_links
                (project_id, token, evaluator_name, credit_limit, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project["id"], "eval-token-empty-ref", "Eva", 5, litra_app.now_iso()),
        )
        conn.commit()

    assert item["reference_text"] == ""

    evaluator_page = client.get("/he/eval-token-empty-ref")
    assert evaluator_page.status_code == 200
    assert b"No reference provided." in evaluator_page.data


def test_human_evaluation_project_can_disable_evaluator_public_stats(monkeypatch, tmp_path):
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
        user_id = conn.execute("SELECT id FROM users WHERE username = ?", ("owner",)).fetchone()["id"]
        conn.commit()

    client = litra_app.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id

    create_response = client.post(
        "/human-evaluation/new",
        data={
            "name": "Eval stats toggle",
            "source_language": "German",
            "target_language": "English",
            "source_txt": (BytesIO("Hallo\nTschüss\n".encode("utf-8")), "source.txt"),
            "model_files": [
                (BytesIO("Hello\nBye\n".encode("utf-8")), "model-a.txt"),
                (BytesIO("Hi\nGoodbye\n".encode("utf-8")), "model-b.txt"),
            ],
        },
        content_type="multipart/form-data",
    )
    assert create_response.status_code == 302

    with litra_app.db() as conn:
        project = conn.execute("SELECT * FROM human_eval_projects WHERE owner_id = ?", (user_id,)).fetchone()
        assert project is not None
        conn.execute(
            """
            INSERT INTO human_eval_links
                (project_id, token, evaluator_name, credit_limit, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project["id"], "eval-token-stats-toggle", "Eva", 5, litra_app.now_iso()),
        )
        conn.commit()

    initial_evaluator_page = client.get("/he/eval-token-stats-toggle")
    assert initial_evaluator_page.status_code == 200
    assert b"Public Stats" in initial_evaluator_page.data

    initial_stats_page = client.get("/he/eval-token-stats-toggle/stats")
    assert initial_stats_page.status_code == 200
    initial_stats_api = client.get("/api/he/eval-token-stats-toggle/stats")
    assert initial_stats_api.status_code == 200

    disable_response = client.post(
        f"/human-evaluation/{project['id']}",
        data={
            "action": "update_public_stats_visibility",
            "evaluator_public_stats_enabled": "0",
        },
    )
    assert disable_response.status_code == 302

    with litra_app.db() as conn:
        updated_project = conn.execute(
            "SELECT evaluator_public_stats_enabled FROM human_eval_projects WHERE id = ?",
            (project["id"],),
        ).fetchone()
    assert updated_project["evaluator_public_stats_enabled"] == 0

    evaluator_page_after_disable = client.get("/he/eval-token-stats-toggle")
    assert evaluator_page_after_disable.status_code == 200
    assert b"Public Stats" not in evaluator_page_after_disable.data

    stats_page_after_disable = client.get("/he/eval-token-stats-toggle/stats")
    assert stats_page_after_disable.status_code == 302
    assert stats_page_after_disable.headers["Location"].endswith("/he/eval-token-stats-toggle")

    stats_api_after_disable = client.get("/api/he/eval-token-stats-toggle/stats")
    assert stats_api_after_disable.status_code == 403
    stats_api_payload = stats_api_after_disable.get_json()
    assert stats_api_payload["status"] == "error"
    assert "disabled" in stats_api_payload["message"].lower()


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


def test_project_detail_recompute_project_qa(monkeypatch, tmp_path):
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
            (owner_id, "QA Refresh", "English", litra_app.now_iso()),
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
            (
                project_id,
                "seg-1",
                1,
                "English",
                "Line • with (x) and <a>",
                "",
                "{}",
                litra_app.now_iso(),
            ),
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
                "Zeile with (x) and a",
                "",
                "submitted",
                "[]",
                1,
                "translator",
                litra_app.now_iso(),
            ),
        )
        conn.commit()

    client = litra_app.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = owner_id

    response = client.post(
        f"/projects/{project_id}",
        data={"action": "recompute_project_qa"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"QA recomputed for 1 translation row(s)." in response.data

    with litra_app.db() as conn:
        updated = conn.execute(
            "SELECT qa_warnings FROM translations WHERE segment_id = ? AND lower(target_language) = lower(?)",
            (segment_id, "German"),
        ).fetchone()
    warning_codes = {item["code"] for item in json.loads(updated["qa_warnings"])}
    assert "special_symbols" in warning_codes


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
        conn.execute(
            """
            INSERT INTO review_links
                (project_id, token, reviewer_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (project_id, "rev-1", "reviewer", litra_app.now_iso()),
        )
        conn.commit()

    client = litra_app.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = owner_id

    language_data = client.get(f"/projects/{project_id}/languages/German/data?comments=1")
    assert language_data.status_code == 200
    assert b"msg-1" in language_data.data

    language_table = client.get(f"/projects/{project_id}/languages/German/texts?comments=1")
    assert language_table.status_code == 200
    assert b"thread comment" in language_table.data

    project_data = client.get(f"/projects/{project_id}/translation-data?comments=1")
    assert project_data.status_code == 200
    assert b"msg-1" in project_data.data

    translator_view = client.get("/t/tok-1/translations?comments=1")
    assert translator_view.status_code == 200
    assert b"msg-1" in translator_view.data
    assert b"thread comment" in translator_view.data

    reviewer_fast_list = client.get("/r/rev-1/texts?comments=1")
    assert reviewer_fast_list.status_code == 200
    assert b"Thread comments" in reviewer_fast_list.data
    assert b"Needs tone adjustment" in reviewer_fast_list.data

    reviewer_overview = client.get("/r/rev-1")
    assert reviewer_overview.status_code == 200
    assert b"Needs tone adjustment" in reviewer_overview.data

    export_response = client.post(
        f"/projects/{project_id}/export-jsonl",
        data={"languages": ["German"]},
    )
    assert export_response.status_code == 200
    lines = [line for line in export_response.get_data(as_text=True).splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["identifier"] == "msg-1"


def test_translation_data_filters_by_warning_code_and_project_rows_clickable(
    monkeypatch, tmp_path
):
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
            (owner_id, "Warning Filters", "English", litra_app.now_iso()),
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
                json.dumps(
                    [
                        {
                            "code": "special_symbols",
                            "label": "Special symbol count differs",
                        }
                    ]
                ),
                1,
                "translator",
                litra_app.now_iso(),
            ),
        )
        conn.commit()

    client = litra_app.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = owner_id

    project_view = client.get(
        f"/projects/{project_id}/translation-data?warnings=1&warning_code=special_symbols"
    )
    assert project_view.status_code == 200
    assert b"msg-1" in project_view.data
    assert b"data-edit-url=" in project_view.data

    language_view = client.get(
        f"/projects/{project_id}/languages/German/data?warnings=1&warning_code=special_symbols"
    )
    assert language_view.status_code == 200
    assert b"msg-1" in language_view.data

    no_match_view = client.get(
        f"/projects/{project_id}/languages/German/data?warnings=1&warning_code=markdown_links"
    )
    assert no_match_view.status_code == 200
    assert b"No rows match the filters" in no_match_view.data


def test_export_docx_includes_thread_comments(monkeypatch, tmp_path):
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
            (owner_id, "DOCX Export", "English", litra_app.now_iso()),
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
            (
                project_id,
                "msg-1",
                1,
                "English",
                "Hello world",
                "",
                "{}",
                litra_app.now_iso(),
            ),
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
                "Summary comment",
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
                (segment_id, target_language, role, body, resolved, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment_id,
                "German",
                "reviewer",
                "Needs tone adjustment",
                0,
                "reviewer",
                litra_app.now_iso(),
            ),
        )
        conn.execute(
            """
            INSERT INTO translation_comments
                (segment_id, target_language, role, body, resolved, created_by, created_at, resolved_by, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                segment_id,
                "German",
                "manager",
                "Looks good now",
                1,
                "manager",
                litra_app.now_iso(),
                "manager",
                litra_app.now_iso(),
            ),
        )
        conn.commit()

    client = litra_app.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = owner_id

    response = client.post(
        f"/projects/{project_id}/export-docx",
        data={"languages": ["German"]},
    )

    assert response.status_code == 200
    assert (
        response.mimetype
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )

    with zipfile.ZipFile(BytesIO(response.data)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")

    assert "Thread comments:" in document_xml
    assert "Needs tone adjustment" in document_xml
    assert "Looks good now" in document_xml


def test_qa_warning_items_detects_special_symbol_count_mismatch(monkeypatch, tmp_path):
    litra_app = importlib.import_module("app")
    monkeypatch.setattr(litra_app, "DB_PATH", tmp_path / "app.sqlite3")
    monkeypatch.setattr(litra_app, "_DB_INITIALIZED", False)

    warnings = litra_app.qa_warning_items(
        "Point • value (A) <x> [ok] {n}",
        "Punkt value (A) x [ok] {n}",
    )

    warning_codes = {item["code"] for item in warnings}
    assert "special_symbols" in warning_codes


def test_qa_warning_items_accepts_matching_special_symbol_counts(monkeypatch, tmp_path):
    litra_app = importlib.import_module("app")
    monkeypatch.setattr(litra_app, "DB_PATH", tmp_path / "app.sqlite3")
    monkeypatch.setattr(litra_app, "_DB_INITIALIZED", False)

    warnings = litra_app.qa_warning_items(
        "Point • value (A) <x> [ok] {n}",
        "Punkt • wert (A) <x> [ok] {n}",
    )

    warning_codes = {item["code"] for item in warnings}
    assert "special_symbols" not in warning_codes


def test_qa_warning_items_detects_uppercase_style_mismatch(monkeypatch, tmp_path):
    litra_app = importlib.import_module("app")
    monkeypatch.setattr(litra_app, "DB_PATH", tmp_path / "app.sqlite3")
    monkeypatch.setattr(litra_app, "_DB_INITIALIZED", False)

    warnings = litra_app.qa_warning_items(
        "IMPORTANT: KEEP THIS TITLE UPPERCASE (V2)!",
        "Wichtig: Bitte diesen Titel groß schreiben (V2)!",
    )

    warning_codes = {item["code"] for item in warnings}
    assert "uppercase_text" in warning_codes


def test_qa_warning_items_accepts_matching_uppercase_style(monkeypatch, tmp_path):
    litra_app = importlib.import_module("app")
    monkeypatch.setattr(litra_app, "DB_PATH", tmp_path / "app.sqlite3")
    monkeypatch.setattr(litra_app, "_DB_INITIALIZED", False)

    warnings = litra_app.qa_warning_items(
        "IMPORTANT: KEEP THIS TITLE UPPERCASE (V2)!",
        "WICHTIG: DIESEN TITEL GROSS SCHREIBEN (V2)!",
    )

    warning_codes = {item["code"] for item in warnings}
    assert "uppercase_text" not in warning_codes
