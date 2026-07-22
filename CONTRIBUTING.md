# Contributing To LITRA

Thanks for helping improve LITRA. This project is a Flask application for
translation workflows and multilingual dataset preparation, so contributions
should preserve data integrity, reviewer ergonomics, and clear export behavior.

## Development Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
flask --app app run --debug
```

Open the Flask URL, register an account, and create a project with one of the
demo files.

## Before Opening A Pull Request

Run the focused local checks:

```bash
python -m compileall app.py scripts tests
python -m pytest
```

For UI changes, also click through the affected workflow manually. The most
important paths are import format creation, project import, translator editing,
reviewer editing, batch status updates, and JSONL/TXT/DOCX exports.

## Contribution Guidelines

- Keep changes scoped to one workflow or concern.
- Preserve backward compatibility for existing JSONL/JSON import formats where
  possible.
- Add tests for parsing, export, database, or permission behavior when changing
  those areas.
- Do not commit `.env`, database files, uploads, generated exports, or personal
  production paths.
- Document any import/export format changes in the README or release notes.

## Licensing

Code contributions are accepted under Apache-2.0.
Documentation and example-file contributions are accepted under Apache-2.0.
