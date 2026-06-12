# Contributing to KeenySpace

KeenySpace is released under the GNU Affero General Public License v3.0 (AGPL-3.0).
Contributions are welcome.

## Developer Certificate of Origin (DCO)

All commits must include a `Signed-off-by` trailer. This certifies that you wrote the
contribution or have the right to submit it under the project license.

To sign off, use:

```
git commit -s -m "your commit message"
```

This appends a trailer to your commit:

```
Signed-off-by: Your Name <your@email.com>
```

The DCO bot enforces this requirement on all pull requests. External contributors
must sign off every commit in a PR. Repository members are exempt per `.github/dco.yml`
(`require: members: false`), but are encouraged to sign off anyway.

Full DCO text: https://developercertificate.org/

## Development setup

The project uses a `uv` workspace. To install all packages:

```
uv sync
```

To run the server tests:

```
uv run --package keenyspace-server pytest packages/server/tests/
```

To run the client tests:

```
uv run --package keenyspace pytest packages/client/tests/
```

Linting and type checking (both must pass before submitting a PR):

```
uv run ruff check .
uv run mypy --strict packages/
```

## Pull request checklist

- [ ] Tests pass (`pytest`)
- [ ] `ruff check .` reports no issues
- [ ] `mypy --strict` reports no issues
- [ ] Every commit is signed off with `git commit -s`
- [ ] Commit messages are concise and describe the change, not the implementation
