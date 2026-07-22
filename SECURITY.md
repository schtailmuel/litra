# Security Policy

## Supported Versions

Security fixes are handled on the default branch until versioned releases are
published. After releases begin, supported release lines should be listed here.

## Reporting A Vulnerability

Please report security issues privately to the repository owner instead of
opening a public issue. Include:

- affected commit or release
- reproduction steps
- expected impact
- whether uploaded files, authentication, public translator/reviewer links, or
  exported datasets are involved

Do not include private project data or credentials in the report.

## Security-Relevant Areas

Pay special attention to:

- public translator, reviewer, and creator links
- upload parsing and file size limits
- exported JSONL, TXT ZIP, and DOCX packages
- registration tokens and session cookies
- PostgreSQL production configuration
- rate limiting and reverse-proxy headers
