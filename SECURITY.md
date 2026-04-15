# Security policy

## Reporting a vulnerability

Please email [gbellamy@umd.edu](mailto:gbellamy@umd.edu) or open a private
security advisory on GitHub:
<https://github.com/ulfsri/alicatlib/security/advisories/new>.

Do **not** file public issues for security reports.

## Scope

`alicatlib` drives physical equipment over serial. Please report:

- Code paths that send destructive commands without `confirm=True`.
- Any path that logs credentials, DSNs, or secrets (`PostgresConfig.password`
  in particular is a non-logging field).
- SQL-injection surfaces in `PostgresSink`.
- Deserialisation of untrusted input in fixture loaders.
