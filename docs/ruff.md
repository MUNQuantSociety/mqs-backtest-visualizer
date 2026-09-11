# Ruff Quick Reference

`ruff` replaces `flake8`, `isort`, `black`, and `pylint` in a single tool.

## Key Commands

| Command | Purpose |
| --- | --- |
| `ruff check .` | Lint code for errors, bugs, and unused imports |
| `ruff check --fix .` | Auto-fix fixable linter errors and sort imports |
| `ruff format .` | Format code layout (drop-in `black` replacement) |
| `ruff format --check .` | Verify formatting without modifying files |

---

## Standard Workflow

Run fixes first, then format:

```bash
ruff check --fix . && ruff format .

```

---

## Minimal Configuration (`ruff.toml`)

```toml
line-length = 88
target-version = "py311"

[lint]
select = ["E", "W", "F", "I", "B"]
ignore = ["E501"]  # Defer line length enforcement to formatter

[format]
quote-style = "double"
docstring-code-format = true

```
