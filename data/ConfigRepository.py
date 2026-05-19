from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


class ConfigRepository:
    def __init__(self, config_path: str = "config.yaml") -> None:
        self.config_path = Path(config_path)

    def load(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        text = self.config_path.read_text(encoding="utf-8")
        return self._parse_yaml_like(text)

    def save(self, config: dict[str, Any]) -> None:
        self.config_path.write_text(self._dump_yaml_like(config), encoding="utf-8")

    def update(self, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.load()
        merged = self._deep_merge(current, updates)
        self.save(merged)
        return merged

    def _deep_merge(self, base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        result = dict(base)
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _parse_scalar(self, value: str) -> Any:
        if value in ("true", "false"):
            return value == "true"
        if value.startswith('"') and value.endswith('"'):
            return value[1:-1]
        if value.startswith("'") and value.endswith("'"):
            return value[1:-1]
        if value.startswith("[") and value.endswith("]"):
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return []
        try:
            if "." in value:
                return float(value)
            return int(value)
        except ValueError:
            return value

    @staticmethod
    def _next_nonblank_is_list_item(lines: list[str], start: int, parent_key_indent: int) -> bool:
        """True si la primera línea con contenido tras start está más indentada que la clave y es un ítem `- `."""
        for k in range(start, len(lines)):
            raw = lines[k]
            line = raw.rstrip()
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            child_indent = len(line) - len(line.lstrip(" "))
            if child_indent <= parent_key_indent:
                return False
            return line.lstrip().startswith("- ")
        return False

    def _parse_yaml_like(self, text: str) -> dict[str, Any]:
        lines = text.splitlines()
        root: dict[str, Any] = {}
        # (indent, dict donde se añaden pares clave/valor, clave bajo la cual hay lista o None)
        stack: list[tuple[int, dict[str, Any], str | None]] = [(-1, root, None)]

        i = 0
        while i < len(lines):
            raw_line = lines[i]
            line = raw_line.rstrip()
            li = i
            i += 1
            if not line.strip() or line.lstrip().startswith("#"):
                continue

            indent = len(line) - len(line.lstrip(" "))
            stripped = line.strip()

            while stack and indent <= stack[-1][0]:
                stack.pop()

            parent_dict = stack[-1][1]

            if stripped.startswith("- "):
                item = self._parse_scalar(stripped[2:].strip())
                _, container, lk = stack[-1]
                if lk is not None and isinstance(container.get(lk), list):
                    container[lk].append(item)
                continue

            if ":" not in stripped:
                continue

            key, value_part = stripped.split(":", 1)
            key = key.strip()
            value_part = value_part.strip()

            if value_part == "":
                if self._next_nonblank_is_list_item(lines, li + 1, indent):
                    parent_dict[key] = []
                    stack.append((indent, parent_dict, key))
                else:
                    parent_dict[key] = {}
                    stack.append((indent, parent_dict[key], None))
                continue

            value = self._parse_scalar(value_part)
            parent_dict[key] = value

            if isinstance(value, list):
                stack.append((indent, parent_dict, key))

        return root

    def _dump_scalar(self, value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, list):
            return str(value).replace("'", '"')
        return f'"{value}"'

    def _dump_yaml_like(self, data: dict[str, Any], level: int = 0) -> str:
        lines: list[str] = []
        indent = "  " * level
        for key, value in data.items():
            if isinstance(value, dict):
                lines.append(f"{indent}{key}:")
                lines.append(self._dump_yaml_like(value, level + 1))
            elif isinstance(value, list):
                lines.append(f"{indent}{key}:")
                for item in value:
                    lines.append(f"{indent}  - {self._dump_scalar(item)}")
            else:
                lines.append(f"{indent}{key}: {self._dump_scalar(value)}")
        return "\n".join(line for line in lines if line != "")
