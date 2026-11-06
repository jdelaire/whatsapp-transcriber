import os
import json
import stat
import zipfile
from pathlib import Path


def macho_libraries(output):
    return list(dict.fromkeys(line.strip().split(" (compatibility version")[0] for line in output.splitlines() if " (compatibility version" in line))


def load_manifest(path, portable, project):
    def expand(value):
        if isinstance(value, dict):
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, str):
            return value.replace("${PORTABLE_ROOT}", str(portable)).replace("${PROJECT_ROOT}", str(project))
        return value
    manifest = expand(json.loads(Path(path).read_text()))
    for record, field in [(manifest["python"], "venv_python"), *[(tool, "path") for tool in manifest["tools"].values()]]:
        value = Path(record[field])
        record[field] = str(value if value.is_absolute() else Path(portable) / value)
    return manifest


def sanitize_tree(root, replacements):
    ordered = sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True)
    for path in Path(root).rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if not any(old.encode() in data for old, _ in ordered):
            continue
        try:
            if b"\0" in data:
                raise UnicodeError()
            text = data.decode("utf-8")
        except UnicodeError as error:
            raise ValueError(f"Private path in binary source material: {path.relative_to(root)}") from error
        for old, new in ordered:
            text = text.replace(old, new)
        path.chmod(path.stat().st_mode | stat.S_IWUSR)
        path.write_text(text)


def reject_private_paths(root, prefixes):
    needles = [str(prefix).encode() for prefix in prefixes if str(prefix)]
    for path in Path(root).rglob("*"):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            if any(needle in data for needle in needles):
                raise ValueError(f"Private build path in release: {path.relative_to(root)}")


def zip_tree(root, destination):
    root, destination = Path(root), Path(destination)
    destination.unlink(missing_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, strict_timestamps=False) as archive:
        for path in sorted(root.rglob("*")):
            if any(part in (".git", ".DS_Store", "__MACOSX", "__pycache__") or part.startswith("._") for part in path.relative_to(root).parts):
                continue
            name = str(path.relative_to(root.parent))
            if path.is_symlink():
                target = os.readlink(path)
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError(f"External archive symlink: {name}")
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, target)
            elif path.is_file():
                archive.write(path, name)
