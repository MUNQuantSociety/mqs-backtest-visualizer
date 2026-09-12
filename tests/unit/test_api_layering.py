"""HTTP routes depend on services, which own the engine integration."""

import ast
from pathlib import Path


def test_api_layer_does_not_import_the_engine():
    root = Path(__file__).resolve().parents[2]
    violations = []
    for path in sorted((root / "src" / "api").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            else:
                continue
            if any(module == "engine" or module.startswith("engine.") for module in modules):
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not violations, "API must import services, not engine modules: " + ", ".join(violations)
