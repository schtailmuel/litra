# 🪜 LITRA

[![CI](https://github.com/schtailmuel/litra/actions/workflows/ci.yml/badge.svg)](https://github.com/schtailmuel/litra/actions/workflows/ci.yml)

🪜 LITRA is a Flask-based translation workflow tool for multilingual data projects. It helps teams import structured source data, assign translation and post-editing work, review source and target texts, collect comments, and export clean datasets.

The application is designed for dataset work where every source segment can have several target languages, alternative source-language versions, comments, review status, and export metadata.

## Features

- Project creation from JSONL and JSON files
- Reusable import-format manager for different data structures
- Schema detection for source text, source language, identifiers, instructions, translations, and translated instructions
- Target-language management with upload support for existing translations
- Alternative source-language uploads for the same segment IDs
- Translator workspaces with automatic text claiming, autosaved drafts, source tabs, comments, source flags, and adjustable source/target font size
- Creator workflow for source-level review
- Reviewer workflows for side-by-side review and fast table editing
- Project-level overview of all translations across all languages
- Language-level overview and editing for a single target language
- CSV comment import with configurable match fields, including metadata fields
- Batch status updates for filtered translation rows
- JSONL, TXT ZIP, and DOCX exports
- PostgreSQL production mode, upload limits, rate limits, and hot-query indexes

## Typical Workflow

1. Create an account and create a project.
2. Choose or define an import format.
3. Upload source data and map source text, source language, identifier, instructions, and optional seed translation fields.
4. Add target languages or upload existing translations.
5. Create translator, creator, and reviewer links.
6. Translators claim texts automatically, edit drafts, submit translations, and add comments or source flags.
7. Reviewers inspect translations across languages, edit translations, and mark rows as reviewed.
8. Managers filter project data, batch-update statuses, and export JSONL, TXT, or DOCX outputs.

## Data Model

The core unit is a segment. Each segment has:

- an identifier
- an ordinal position
- source language
- source text
- optional instructions
- metadata

Each segment can have translations in multiple target languages. A translation has:

- target language
- current submitted text
- draft text
- translated instructions
- comments
- status
- QA warnings
- version metadata

Supported statuses:

- `untranslated`
- `draft`
- `submitted`
- `needs_revision`
- `approved`

## Import Formats

Import formats define how files are mapped into project data. They support:

- JSONL rows
- JSON arrays or nested row paths
- source text as a single string
- source text as a sentence list
- manual or file-based source language
- identifier fields
- instruction fields
- seed translation fields
- translated instruction fields

The default profiles include RhaetoChat-style JSONL and Bouquet-style JSON structures.

## File Examples

Minimal JSONL:

```json
{"identifier":"doc-001","source_language":"English","source_text":"Text to translate","instructions":"Use a formal tone."}
```

JSONL with a preloaded translation:

```json
{"identifier":"doc-001","source_language":"English","source_text":"Text","instructions":"Use a formal tone.","target_language":"German","target_text":"Text","translated_instruction":"Formell schreiben."}
```

Bouquet-style sentence lists can be imported by mapping a list field as source text. The editor displays each sentence line-by-line and exports the joined text back in the configured format.

## Review And Editing

Managers can inspect translations at two levels:

- per-language translation data
- all translation data across the full project

Both views support filtering and direct editing. The project-level view also supports batch status updates for selected rows or all rows matching the current filter.

Reviewer links provide public review access without granting manager access. Reviewers can compare languages side by side, edit translations, and mark rows as reviewed.

Creator links provide source-review access for checking source text before or during translation work.

## Comments

Comments can be created during translation, review, or management. CSV comment import supports:

- first-column IDs
- multiple comment columns merged into one comment
- multiple rows for the same document or segment
- matching by segment identifier, ordinal, source fields, or metadata keys

## Exports

Supported exports:

- per-language JSONL
- selected-language JSONL
- source-data JSONL
- project-level filtered DOCX
- selected-language DOCX
- TXT ZIP packages with source and target files

DOCX exports render source languages first and translations second. If there is one source and one target language, they are displayed side by side. Larger language sets are rendered as two-column grids. DOCX content is anonymous and does not include account names.

## Local Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
flask --app app run --debug
```

Open the Flask URL, register an account, and create a project.

For development checks:

```bash
pip install -r requirements-dev.txt
python -m compileall app.py scripts tests
python -m pytest
```

## Docker

Create an environment file:

```bash
cp .env.example .env
python -c "import secrets; print('SECRET_KEY=' + secrets.token_urlsafe(48))"
python -c "import secrets; print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32))"
python -c "import secrets; print('REGISTRATION_TOKEN=' + secrets.token_urlsafe(32))"
```

Run the stack:

```bash
docker compose up --build -d
```

The default Docker setup exposes the app at `http://localhost:8000`.

## Production Notes

Production deployments should use PostgreSQL.

Important environment variables:

- `APP_ENV=production`
- `REQUIRE_POSTGRES=1`
- `DATABASE_URL=postgresql://...`
- `SECRET_KEY`
- `POSTGRES_PASSWORD`
- `REGISTRATION_TOKEN`
- `UPLOAD_MAX_MB`
- `RATE_LIMIT_ENABLED`
- `RATE_LIMIT_PUBLIC_WRITES_PER_MIN`
- `RATE_LIMIT_AUTH_WRITES_PER_MIN`
- `RATE_LIMIT_UPLOADS_PER_HOUR`
- `WEB_CONCURRENCY`
- `GUNICORN_THREADS`
- `TRUST_PROXY_HEADERS`
- `SESSION_COOKIE_SECURE`

Uploaded source files are only needed during import. Production deployments can safely use a cleanup policy after successful imports if audit metadata is retained separately.

## Repository

Public repository: [schtailmuel/litra](https://github.com/schtailmuel/litra)

## License

LITRA application code is licensed under Apache-2.0. See [LICENSE](LICENSE).

Documentation, demo data, dataset schemas, and released dataset content are
licensed under CC BY-SA 4.0 unless a release artifact, dataset card, or
file-level notice says otherwise. See [DATA_LICENSE.md](DATA_LICENSE.md).
