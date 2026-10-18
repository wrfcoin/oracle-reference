# Contributing

Read the [README](README.md) for this repository's purpose and technical details.
Follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Build and test

Use Python 3.11, matching CI. Install the slim test dependencies and run:

```sh
python -m pip install -r requirements-ci.txt
ruff check app agent tests
pytest -q
```

The default suite excludes live upstream tests. See the README for optional GRIB tests and full service dependencies.

## Branch and pull request flow

Fork the repository and create a focused branch from current `main`.
Add tests for behavior changes and run the checks above before submitting a pull request.
Describe the change, its motivation, and validation results; use `Refs #N` to link the issue.
Keep unrelated changes separate and wait for maintainer review before merging.
Report vulnerabilities through [SECURITY.md](SECURITY.md).
