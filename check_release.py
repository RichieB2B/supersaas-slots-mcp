"""Check metadata shared by the Python package and MCP Registry entry."""

import argparse
import ast
import json
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="Release tag, for example v0.1.0")
    args = parser.parse_args()

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    server = json.loads((ROOT / "server.json").read_text())
    version = project["version"]
    assert server["version"] == version
    assert server["packages"][0]["version"] == version
    assert server["packages"][0]["identifier"] == project["name"]
    assert "mcp-name: " + server["name"] in (ROOT / "README.md").read_text()
    assert project["name"] in project["scripts"]
    source = ast.parse((ROOT / "supersaas_mcp.py").read_text())
    server_versions = [
        keyword.value.value
        for node in ast.walk(source)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "FastMCP"
        for keyword in node.keywords
        if keyword.arg == "version" and isinstance(keyword.value, ast.Constant)
    ]
    assert server_versions == [version]
    if args.tag:
        assert args.tag == "v" + version
    print(f"Release metadata matches version {version}")


if __name__ == "__main__":
    main()
