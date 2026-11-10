
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

if len(sys.argv) != 3:
    raise SystemExit(
        "Usage: rewrite_paths.py <artifact_dir> <project_name>"
    )

ROOT = Path(sys.argv[1]).resolve()
PROJECT_NAME = sys.argv[2]

if not re.fullmatch(r"[a-z0-9-]{1,50}", PROJECT_NAME):
    raise SystemExit("Invalid project name")

PREFIX = f"/{PROJECT_NAME}"

SKIP_PREFIXES = (
    "//",
    "#",
    "data:",
    "blob:",
    "javascript:",
    "mailto:",
    "tel:",
    "http:",
    "https:",
)

def rewrite_url(value):
    value = value.strip()

    if not value or value.startswith(SKIP_PREFIXES):
        return value

    if not value.startswith("/"):
        return value

    return PREFIX + value


def rewrite_srcset(value):
    parts = []

    for item in value.split(","):
        item = item.strip()

        if not item:
            continue

        pieces = item.split(None, 1)
        url = rewrite_url(pieces[0])

        if len(pieces) > 1:
            url += " " + pieces[1]

        parts.append(url)

    return ", ".join(parts)


# HTML attributes
HTML_ATTRIBUTE = re.compile(
    r'(\b(?:src|href|poster|action|data-src|'
    r'data-href|xlink:href)\s*=\s*)'
    r'(["\'])(.*?)\2',
    re.IGNORECASE | re.DOTALL
)

SRCSET_ATTRIBUTE = re.compile(
    r'(\bsrcset\s*=\s*)(["\'])(.*?)\2',
    re.IGNORECASE | re.DOTALL
)


def rewrite_html(content):
    def replace_attribute(match):
        return (
            match.group(1)
            + match.group(2)
            + rewrite_url(match.group(3))
            + match.group(2)
        )

    def replace_srcset(match):
        return (
            match.group(1)
            + match.group(2)
            + rewrite_srcset(match.group(3))
            + match.group(2)
        )

    content = HTML_ATTRIBUTE.sub(
        replace_attribute, content
    )

    content = SRCSET_ATTRIBUTE.sub(
        replace_srcset, content
    )

    return content


# CSS url() and @import references
CSS_URL = re.compile(
    r'url\(\s*(["\']?)(.*?)\1\s*\)',
    re.IGNORECASE
)

CSS_IMPORT = re.compile(
    r'(@import\s+)(["\'])(.*?)\2',
    re.IGNORECASE
)


def rewrite_css(content):
    def replace_url(match):
        return (
            "url("
            + match.group(1)
            + rewrite_url(match.group(2))
            + match.group(1)
            + ")"
        )

    def replace_import(match):
        return (
            match.group(1)
            + match.group(2)
            + rewrite_url(match.group(3))
            + match.group(2)
        )

    content = CSS_URL.sub(replace_url, content)
    content = CSS_IMPORT.sub(replace_import, content)

    return content


# Common framework-generated JS asset paths.
JS_ASSET_PATH = re.compile(
    r'(["\'])'
    r'(/(?:assets|static|images|fonts|media|'
    r'build|_next/static|immutable)/[^"\']*)'
    r'\1'
)


def rewrite_js(content):
    def replace_match(match):
        return (
            match.group(1)
            + rewrite_url(match.group(2))
            + match.group(1)
        )

    return JS_ASSET_PATH.sub(replace_match, content)


# Web app manifests
def rewrite_manifest(data):
    if isinstance(data, dict):
        for key, value in data.items():

            if key in (
                "start_url",
                "scope",
                "src",
                "url",
            ) and isinstance(value, str):
                data[key] = rewrite_url(value)

            elif isinstance(value, (dict, list)):
                rewrite_manifest(value)

    elif isinstance(data, list):
        for item in data:
            rewrite_manifest(item)


def process_file(path):
    suffix = path.suffix.lower()

    if suffix not in (
        ".html", ".htm", ".css",
        ".js", ".mjs", ".json"
    ):
        return

    try:
        content = path.read_text(encoding="utf-8")

        if suffix in (".html", ".htm"):
            updated = rewrite_html(content)

        elif suffix == ".css":
            updated = rewrite_css(content)

        elif suffix in (".js", ".mjs"):
            updated = rewrite_js(content)

        elif suffix == ".json":
            data = json.loads(content)
            rewrite_manifest(data)
            updated = json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":")
            )

        else:
            return

        if updated != content:
            path.write_text(updated, encoding="utf-8")
            print(f"REWRITTEN: {path.relative_to(ROOT)}")

    except Exception as error:
        raise RuntimeError(
            f"Failed to process {path}: {error}"
        )


if not ROOT.is_dir():
    raise SystemExit("Artifact directory does not exist")

print(f"Artifact: {ROOT}")
print(f"Project prefix: {PREFIX}/")

for file in ROOT.rglob("*"):
    if file.is_file():
        process_file(file)

print("Path rewriting completed.")
