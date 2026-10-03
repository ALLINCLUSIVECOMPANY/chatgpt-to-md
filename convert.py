from pathlib import Path

"""Convert an OpenAI ChatGPT data export into Markdown files.

Supports both older and current export layouts, including:
  - conversations.json
  - conversations-000.json, conversations-001.json, ...
  - conversation_asset_file_names.json + *.dat attachments
  - an extracted export directory OR the export .zip itself

Usage:
    python convert_fixed.py <export-dir-or-zip> <output-dir> [--skip-assets] [--verbose]

Requires Python 3.8+ and no external dependencies.
"""

import argparse
import json
import mimetypes
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import quote


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def slugify(text: str, max_len: int = 60) -> str:
    """Convert text to a filename-safe ASCII slug."""
    text = (text or "").lower()
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "untitled"


def safe_filename(name: str, fallback: str = "attachment") -> str:
    """Return a filesystem-safe filename while preserving a useful extension."""
    name = Path(name or "").name.strip()
    if not name:
        return fallback

    # Windows-invalid characters + control characters.
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.rstrip(" .")
    if not name:
        return fallback

    # Avoid Windows reserved device names.
    stem = Path(name).stem.upper()
    if stem in {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        name = "_" + name

    # Leave room for long output paths on Windows.
    if len(name) > 180:
        suffix = Path(name).suffix[:20]
        stem_text = Path(name).stem[: max(1, 180 - len(suffix))]
        name = stem_text + suffix

    return name


def dedupe_filename(path: Path) -> Path:
    """Append -2, -3, ... if *path* already exists."""
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    n = 2
    while True:
        candidate = parent / "{}-{}{}".format(stem, n, suffix)
        if not candidate.exists():
            return candidate
        n += 1


def markdown_path(path: str) -> str:
    """URL-encode a relative path for use in Markdown."""
    return quote(path.replace("\\", "/"), safe="/._-~")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def ts_to_datetime(ts) -> datetime:
    """Convert a Unix timestamp to a UTC datetime."""
    if ts is None:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def format_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Export loading
# ---------------------------------------------------------------------------

def _safe_extract_zip(zip_path: Path, dest: Path) -> None:
    """Extract a ZIP while refusing path traversal entries."""
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(str(zip_path), "r") as zf:
        for member in zf.infolist():
            candidate = (dest / member.filename).resolve()
            try:
                candidate.relative_to(dest_resolved)
            except ValueError:
                raise RuntimeError(
                    "Unsafe path in ZIP: {}".format(member.filename)
                )
        zf.extractall(str(dest))


def find_export_root(root: Path) -> Path:
    """Find the directory containing the ChatGPT conversation JSON files."""
    if list(root.glob("conversations.json")) or list(root.glob("conversations-*.json")):
        return root

    candidates = []
    for path in root.rglob("conversations*.json"):
        if re.fullmatch(r"conversations(?:-\d+)?\.json", path.name):
            candidates.append(path.parent)

    unique = sorted(set(candidates), key=lambda p: (len(p.parts), str(p).lower()))
    if len(unique) == 1:
        return unique[0]
    if not unique:
        raise FileNotFoundError(
            "Could not find conversations.json or conversations-###.json under {}".format(root)
        )

    # Prefer a directory that also has chat.html or export_manifest.json.
    preferred = [
        p for p in unique
        if (p / "chat.html").exists() or (p / "export_manifest.json").exists()
    ]
    if len(preferred) == 1:
        return preferred[0]

    raise RuntimeError(
        "Found multiple possible export directories:\n  {}".format(
            "\n  ".join(str(p) for p in unique)
        )
    )


def conversation_files(export_dir: Path) -> List[Path]:
    """Return conversation JSON files without accidentally matching sidecar JSON."""
    single = export_dir / "conversations.json"
    if single.is_file():
        return [single]

    shards = []
    pattern = re.compile(r"^conversations-(\d+)\.json$")
    for path in export_dir.iterdir():
        if not path.is_file():
            continue
        m = pattern.match(path.name)
        if m:
            shards.append((int(m.group(1)), path))

    shards.sort(key=lambda item: item[0])
    return [p for _, p in shards]


def load_conversations(export_dir: Path) -> List[dict]:
    files = conversation_files(export_dir)
    if not files:
        raise FileNotFoundError(
            "No conversations.json or conversations-###.json files found in {}".format(export_dir)
        )

    conversations = []
    seen_ids = set()

    for path in files:
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Invalid JSON in {}: {}".format(path.name, exc))

        if isinstance(data, dict) and isinstance(data.get("conversations"), list):
            data = data["conversations"]

        if not isinstance(data, list):
            raise RuntimeError(
                "{} has an unsupported top-level JSON shape; expected a list.".format(path.name)
            )

        for convo in data:
            if not isinstance(convo, dict):
                continue
            cid = convo.get("conversation_id") or convo.get("id")
            if cid and cid in seen_ids:
                continue
            if cid:
                seen_ids.add(cid)
            conversations.append(convo)

    return conversations


def read_asset_file_names(export_dir: Path) -> Dict[str, str]:
    """Read current-export .dat filename -> original filename mapping."""
    path = export_dir / "conversation_asset_file_names.json"
    if not path.is_file():
        return {}

    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            "Warning: could not read {}: {}".format(path.name, exc),
            file=sys.stderr,
        )
        return {}

    if not isinstance(data, dict):
        return {}

    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[str(key)] = value
    return result


# ---------------------------------------------------------------------------
# DAG traversal
# ---------------------------------------------------------------------------

def get_canonical_ids(mapping: dict, current_node: Optional[str]) -> Set[str]:
    """Return node IDs on the last-viewed path."""
    ids = set()
    node_id = current_node
    while node_id:
        ids.add(node_id)
        node = mapping.get(node_id)
        if not node:
            break
        node_id = node.get("parent")
    return ids


def find_root(mapping: dict) -> Optional[str]:
    """Find a root node (a node with no parent)."""
    for nid, node in mapping.items():
        if isinstance(node, dict) and node.get("parent") is None:
            return nid
    return None


def latest_leaf(mapping: dict, root: str) -> Optional[str]:
    """Best-effort fallback current node if current_node is absent."""
    best_id = None
    best_key = (-1.0, "")
    stack = [root]
    seen = set()

    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)

        node = mapping.get(nid) or {}
        children = [c for c in (node.get("children") or []) if c in mapping]
        if children:
            stack.extend(children)
            continue

        msg = node.get("message") or {}
        ts = msg.get("create_time")
        try:
            ts_value = float(ts) if ts is not None else -1.0
        except (TypeError, ValueError):
            ts_value = -1.0

        key = (ts_value, str(nid))
        if key > best_key:
            best_key = key
            best_id = nid

    return best_id


def walk_tree(mapping: dict, node_id: str, canonical_ids: Set[str]):
    """Depth-first walk yielding (message_or_none, is_branch_marker, info)."""
    node = mapping.get(node_id)
    if not node:
        return

    msg = node.get("message")
    if msg:
        yield msg, False, None

    children = [c for c in (node.get("children") or []) if c in mapping]
    if not children:
        return

    canonical = [c for c in children if c in canonical_ids]
    alternatives = [c for c in children if c not in canonical_ids]

    for child in canonical:
        yield from walk_tree(mapping, child, canonical_ids)

    if alternatives and canonical:
        canonical_count = len(canonical)
        for i, child in enumerate(alternatives):
            yield None, True, {
                "branch_start": True,
                "index": canonical_count + i + 1,
                "total": len(children),
            }
            yield from walk_tree(mapping, child, canonical_ids)
            yield None, True, {"branch_end": True}
    else:
        for child in alternatives:
            yield from walk_tree(mapping, child, canonical_ids)


# ---------------------------------------------------------------------------
# Asset handling
# ---------------------------------------------------------------------------

_ASSET_SCHEME_RE = re.compile(r"^(?:sediment|file-service)://", re.I)
_ASSET_BASENAME_RE = re.compile(r"^(file[-_][^.\-]+)")


def normalize_asset_ref(value) -> str:
    if not isinstance(value, str):
        return ""

    value = _ASSET_SCHEME_RE.sub("", value.strip())
    value = value.split("?", 1)[0].split("#", 1)[0]
    value = value.replace("\\", "/")
    base = value.rsplit("/", 1)[-1]

    if base.lower().endswith(".dat"):
        base = base[:-4]

    # Keep complete file IDs such as file-ABC123 / file_0000....
    m = re.match(r"^(file[-_][A-Za-z0-9]+)", base)
    return m.group(1) if m else base


def asset_keys_for_path(path: Path, export_dir: Path) -> Set[str]:
    keys = set()

    try:
        rel = str(path.relative_to(export_dir)).replace("\\", "/")
        keys.add(rel)
    except ValueError:
        pass

    keys.add(path.name)
    keys.add(path.stem)

    for value in list(keys):
        norm = normalize_asset_ref(value)
        if norm:
            keys.add(norm)

    return {k for k in keys if k}


def build_asset_index(export_dir: Path) -> Tuple[Dict[str, Path], Dict[str, str]]:
    """Build asset lookup and read original names for current .dat exports."""
    original_names = read_asset_file_names(export_dir)
    index = {}

    # Current/legacy uploaded assets.
    for entry in export_dir.rglob("file*"):
        if not entry.is_file():
            continue
        for key in asset_keys_for_path(entry, export_dir):
            index.setdefault(key, entry)

    # Legacy DALL-E directory if present.
    dalle_dir = export_dir / "dalle-generations"
    if dalle_dir.is_dir():
        for entry in dalle_dir.rglob("*"):
            if entry.is_file():
                for key in asset_keys_for_path(entry, export_dir):
                    index.setdefault(key, entry)

    # Add aliases from conversation_asset_file_names.json.
    for stored_name, original_name in original_names.items():
        stored_path = export_dir / stored_name
        if not stored_path.exists():
            # Mapping keys are normally root-relative, but basename fallback
            # makes this resilient to a nested path.
            matches = list(export_dir.rglob(Path(stored_name).name))
            stored_path = matches[0] if matches else stored_path

        if stored_path.is_file():
            aliases = {
                stored_name,
                Path(stored_name).name,
                Path(stored_name).stem,
                normalize_asset_ref(stored_name),
            }
            for alias in aliases:
                if alias:
                    index.setdefault(alias, stored_path)

    return index, original_names


def original_name_for_asset(
    src: Path,
    export_dir: Path,
    original_names: Dict[str, str],
) -> str:
    """Resolve a .dat blob's original filename when the mapping supplies one."""
    candidates = []
    try:
        candidates.append(str(src.relative_to(export_dir)).replace("\\", "/"))
    except ValueError:
        pass
    candidates.extend([src.name, src.stem])

    for candidate in candidates:
        mapped = original_names.get(candidate)
        if mapped:
            return Path(mapped).name

    # Also tolerate mapping keys that use slash style differently.
    for key, value in original_names.items():
        if Path(key).name == src.name and value:
            return Path(value).name

    return src.name


def iter_asset_refs(value) -> Iterable[str]:
    """Recursively find likely file/asset references in message data."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"asset_pointer", "file_id"} and isinstance(child, str):
                ref = normalize_asset_ref(child)
                if ref:
                    yield ref
            elif key == "id" and isinstance(child, str) and child.startswith(("file-", "file_")):
                ref = normalize_asset_ref(child)
                if ref:
                    yield ref
            yield from iter_asset_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_asset_refs(child)


def collect_referenced_assets(convo: dict) -> Set[str]:
    """Return asset/file IDs referenced anywhere in a conversation mapping."""
    refs = set()
    mapping = convo.get("mapping") or {}
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        refs.update(iter_asset_refs(msg))
    return refs


def make_unique_asset_name(
    src: Path,
    original_name: str,
    asset_id: str,
    used_names: Set[str],
) -> str:
    original_name = safe_filename(original_name, fallback=src.name)

    if src.suffix.lower() == ".dat" and original_name.lower().endswith(".dat"):
        # We do not know the real extension. Keep .dat rather than guessing.
        desired = "{}.dat".format(safe_filename(asset_id or src.stem))
    elif src.suffix.lower() == ".dat":
        # Prefix with asset ID to avoid collisions between repeated filenames.
        desired = "{}-{}".format(
            safe_filename(asset_id or src.stem),
            original_name,
        )
    else:
        desired = safe_filename(src.name)

    candidate = desired
    stem = Path(desired).stem
    suffix = Path(desired).suffix
    n = 2
    while candidate.lower() in used_names:
        candidate = "{}-{}{}".format(stem, n, suffix)
        n += 1

    used_names.add(candidate.lower())
    return candidate


def copy_assets(
    referenced: Set[str],
    asset_index: Dict[str, Path],
    original_names: Dict[str, str],
    export_dir: Path,
    assets_dir: Path,
) -> Tuple[Dict[str, str], Set[str]]:
    """Copy referenced assets and return ref -> copied filename plus missing refs."""
    assets_dir.mkdir(parents=True, exist_ok=True)

    result = {}
    missing = set()
    copied_by_source = {}
    used_names = set()

    for ref in sorted(referenced):
        normalized = normalize_asset_ref(ref)
        src = (
            asset_index.get(ref)
            or asset_index.get(normalized)
            or asset_index.get(ref + ".dat")
            or asset_index.get(normalized + ".dat")
        )
        if not src:
            missing.add(ref)
            continue

        src_key = str(src.resolve())
        if src_key in copied_by_source:
            dest_name = copied_by_source[src_key]
        else:
            original_name = original_name_for_asset(src, export_dir, original_names)
            dest_name = make_unique_asset_name(
                src=src,
                original_name=original_name,
                asset_id=normalized,
                used_names=used_names,
            )
            dest = assets_dir / dest_name
            shutil.copy2(str(src), str(dest))
            copied_by_source[src_key] = dest_name

        result[ref] = dest_name
        if normalized:
            result[normalized] = dest_name

        # Add all known aliases for the same source.
        for alias, indexed_src in asset_index.items():
            try:
                same = indexed_src.resolve() == src.resolve()
            except OSError:
                same = indexed_src == src
            if same:
                result.setdefault(alias, dest_name)

    return result, missing


# ---------------------------------------------------------------------------
# Content renderers
# ---------------------------------------------------------------------------

ASSET_PREFIX = "../../assets"

SKIP_CONTENT_TYPES = {
    "tether_browsing_display",
    "user_editable_context",
    "computer_output",
    "app_pairing_content",
}


def asset_markdown_target(filename: str) -> str:
    return markdown_path("{}/{}".format(ASSET_PREFIX, filename))


def lookup_asset_filename(asset_ref: str, assets_map: Dict[str, str]) -> Optional[str]:
    if not asset_ref:
        return None
    return assets_map.get(asset_ref) or assets_map.get(normalize_asset_ref(asset_ref))


def render_text(content: dict) -> str:
    parts = content.get("parts") or []
    texts = []
    for part in parts:
        if isinstance(part, str) and part.strip():
            texts.append(part)
        elif isinstance(part, dict) and part.get("content_type") in (None, "text"):
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text)
    return "\n\n".join(texts)


def render_multimodal(content: dict, assets_map: Dict[str, str]) -> str:
    parts = content.get("parts") or []
    texts = []

    for part in parts:
        if isinstance(part, str):
            if part.strip():
                texts.append(part)
            continue

        if not isinstance(part, dict):
            continue

        ct = part.get("content_type")
        if ct == "image_asset_pointer":
            pointer = part.get("asset_pointer", "")
            asset_id = normalize_asset_ref(pointer)
            meta = part.get("metadata") or {}
            dalle = meta.get("dalle") or {}
            alt = dalle.get("prompt") or "image"
            filename = lookup_asset_filename(asset_id, assets_map)

            if filename:
                texts.append("![{}]({})".format(
                    str(alt).replace("]", r"\]"),
                    asset_markdown_target(filename),
                ))
            else:
                texts.append("*[Image asset not included in export: {}]*".format(asset_id or pointer))
        elif ct in ("text", None):
            text = part.get("text", "")
            if isinstance(text, str) and text.strip():
                texts.append(text)
        elif ct == "audio_transcription":
            text = part.get("text") or part.get("transcript") or ""
            if isinstance(text, str) and text.strip():
                texts.append(text)

    return "\n\n".join(texts)


def render_code(content: dict) -> str:
    lang = content.get("language") or ""
    if lang == "unknown":
        lang = ""
    text = content.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return ""
    return "```{}\n{}\n```".format(lang, text)


def render_execution_output(content: dict) -> str:
    text = content.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return ""
    return (
        "<details><summary>Output</summary>\n\n"
        "```\n{}\n```\n\n"
        "</details>".format(text)
    )


def render_tether_quote(content: dict) -> str:
    text = str(content.get("text") or "").strip()
    url = str(content.get("url") or "")
    domain = str(content.get("domain") or "")
    if not text:
        return ""

    quoted = "\n".join("> " + line for line in text.split("\n"))
    source = ""
    if url and not url.startswith("file-"):
        source = "\n> — [{}]({})".format(domain or url, url)
    elif domain:
        source = "\n> — {}".format(domain)
    return quoted + source


def render_sonic_webpage(content: dict) -> str:
    title = str(content.get("title") or "")
    url = str(content.get("url") or "")
    text = str(content.get("text") or "").strip()
    if not text and not title:
        return ""

    parts = []
    if title and url:
        parts.append("> **[{}]({})**".format(title, url))
    elif title:
        parts.append("> **{}**".format(title))

    if text:
        cleaned = re.sub(r"[\ue200-\ue2ff]\w*[\ue200-\ue2ff]?", "", text).strip()
        if cleaned:
            parts.append("\n".join("> " + line for line in cleaned.split("\n")))

    return "\n".join(parts)


def render_reasoning_recap(content: dict) -> str:
    text = str(content.get("content") or "").strip()
    return "*{}*".format(text) if text else ""


def render_system_error(content: dict) -> str:
    name = str(content.get("name") or "Error")
    text = str(content.get("text") or "").strip()
    return "**{}:** {}".format(name, text) if text else "**{}**".format(name)


def render_message_content(content: dict, assets_map: Dict[str, str]) -> str:
    """Render a message content dict to Markdown."""
    if not isinstance(content, dict):
        return ""

    ct = content.get("content_type", "text")

    if ct in SKIP_CONTENT_TYPES:
        return ""

    if ct == "text":
        return render_text(content)
    if ct == "multimodal_text":
        return render_multimodal(content, assets_map)
    if ct == "code":
        return render_code(content)
    if ct == "execution_output":
        return render_execution_output(content)
    if ct == "tether_quote":
        return render_tether_quote(content)
    if ct == "sonic_webpage":
        return render_sonic_webpage(content)
    if ct == "reasoning_recap":
        return render_reasoning_recap(content)
    if ct == "system_error":
        return render_system_error(content)

    # Fallbacks for newer/unknown content types.
    text = content.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return render_text(content)


def attachment_display_name(att: dict, filename: Optional[str]) -> str:
    for key in ("name", "filename", "file_name"):
        value = att.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value).name
    if filename:
        # Remove our asset-ID prefix for display when possible.
        return filename
    for key in ("id", "file_id", "asset_pointer"):
        value = att.get(key)
        if isinstance(value, str) and value.strip():
            return normalize_asset_ref(value) or value
    return "attachment"


def render_message_attachments(msg: dict, assets_map: Dict[str, str]) -> str:
    """Render metadata.attachments entries not already represented inline."""
    metadata = msg.get("metadata") or {}
    attachments = metadata.get("attachments") or []
    if not isinstance(attachments, list):
        return ""

    content_refs = set(iter_asset_refs(msg.get("content") or {}))
    rendered = []
    seen = set()

    for att in attachments:
        if not isinstance(att, dict):
            continue

        refs = []
        for key in ("id", "file_id", "asset_pointer"):
            value = att.get(key)
            if isinstance(value, str) and value:
                refs.append(normalize_asset_ref(value))

        ref = next((r for r in refs if r), "")
        if ref and ref in content_refs:
            continue
        if ref and ref in seen:
            continue
        if ref:
            seen.add(ref)

        filename = lookup_asset_filename(ref, assets_map)
        display = attachment_display_name(att, filename)
        mime = str(att.get("mime_type") or att.get("mimeType") or "")
        is_image = mime.startswith("image/")
        if not is_image and filename:
            guessed, _ = mimetypes.guess_type(filename)
            is_image = bool(guessed and guessed.startswith("image/"))

        if filename:
            target = asset_markdown_target(filename)
            if is_image:
                rendered.append("![{}]({})".format(display.replace("]", r"\]"), target))
            else:
                rendered.append("[{}]({})".format(display.replace("]", r"\]"), target))
        else:
            rendered.append("*[Attachment not included in export: {}]*".format(display))

    return "\n\n".join(rendered)


# ---------------------------------------------------------------------------
# Conversation renderer
# ---------------------------------------------------------------------------

ROLE_HEADINGS = {
    "user": "## User",
    "assistant": "## Assistant",
}


def escape_yaml(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_conversation(convo: dict, assets_map: Dict[str, str]) -> Optional[str]:
    """Render a conversation dict to Markdown. Returns None to skip."""
    mapping = convo.get("mapping") or {}
    if not isinstance(mapping, dict) or not mapping:
        return None

    root = find_root(mapping)
    if not root:
        return None

    current_node = convo.get("current_node")
    if not current_node or current_node not in mapping:
        current_node = latest_leaf(mapping, root)

    canonical_ids = get_canonical_ids(mapping, current_node)

    models_used = set()
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if not isinstance(msg, dict):
            continue
        model = (msg.get("metadata") or {}).get("model_slug")
        if model:
            models_used.add(str(model))

    title = str(convo.get("title") or "Untitled")
    create_time = convo.get("create_time") or convo.get("update_time")
    dt = ts_to_datetime(create_time)
    conv_id = convo.get("conversation_id") or convo.get("id") or ""
    default_model = str(convo.get("default_model_slug") or "")
    is_archived = bool(convo.get("is_archived", False))

    message_count = 0
    for node in mapping.values():
        if not isinstance(node, dict):
            continue
        msg = node.get("message")
        if isinstance(msg, dict):
            role = (msg.get("author") or {}).get("role")
            if role in ("user", "assistant"):
                message_count += 1

    models_list = sorted(models_used)
    if not models_list and default_model:
        models_list = [default_model]

    lines = [
        "---",
        'title: "{}"'.format(escape_yaml(title)),
        "date: {}".format(format_iso(dt)),
        "conversation_id: {}".format(conv_id),
    ]
    if default_model:
        lines.append("model: {}".format(default_model))
    if models_list:
        lines.append("models_used: [{}]".format(", ".join(models_list)))
    lines.append("message_count: {}".format(message_count))
    lines.append("is_archived: {}".format("true" if is_archived else "false"))
    lines.append("---")
    lines.append("")
    lines.append("# {}".format(title))
    lines.append("")

    body_parts = []
    last_role = None

    for msg, is_marker, info in walk_tree(mapping, root, canonical_ids):
        if is_marker:
            if info.get("branch_start"):
                body_parts.append(
                    "\n<details><summary>Alternative response "
                    "(branch {} of {})</summary>\n".format(
                        info["index"], info["total"]
                    )
                )
                # Force a fresh heading inside the branch.
                last_role = None
            elif info.get("branch_end"):
                body_parts.append("\n</details>\n")
                last_role = None
            continue

        role = (msg.get("author") or {}).get("role", "")
        content = msg.get("content") or {}

        if role == "system":
            continue
        if (msg.get("metadata") or {}).get("is_visually_hidden_from_conversation"):
            continue

        rendered = render_message_content(content, assets_map)
        attachment_md = render_message_attachments(msg, assets_map)

        if rendered and attachment_md:
            rendered = rendered.rstrip() + "\n\n" + attachment_md
        elif attachment_md:
            rendered = attachment_md

        if not rendered.strip():
            continue

        if role == "tool":
            body_parts.append(rendered)
            body_parts.append("")
            continue

        heading = ROLE_HEADINGS.get(role)
        if heading and role != last_role:
            body_parts.append(heading)
            body_parts.append("")
            last_role = role

        body_parts.append(rendered)
        body_parts.append("")

    body = "\n".join(body_parts).strip()
    if not body:
        return None

    export_date = datetime.now(timezone.utc).date().isoformat()
    footer = "\n\n---\n\n*Converted from a ChatGPT data export on {}*\n".format(export_date)

    return "\n".join(lines) + body + footer


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert an OpenAI ChatGPT data export to Markdown files."
    )
    parser.add_argument(
        "export_path",
        help="Path to an extracted ChatGPT export directory or the export ZIP",
    )
    parser.add_argument("output_dir", help="Directory to write the Markdown archive")
    parser.add_argument(
        "--skip-assets",
        action="store_true",
        help="Do not copy referenced image/file assets",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each conversation as it is processed",
    )
    args = parser.parse_args()

    input_path = Path(args.export_path).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    if not input_path.exists():
        print("Error: input path not found: {}".format(input_path), file=sys.stderr)
        return 2

    temp_ctx = None
    try:
        if input_path.is_file():
            if not zipfile.is_zipfile(str(input_path)):
                print(
                    "Error: input file is not a ZIP archive: {}".format(input_path),
                    file=sys.stderr,
                )
                return 2

            temp_ctx = tempfile.TemporaryDirectory(prefix="chatgpt-export-")
            extract_root = Path(temp_ctx.name)
            print("Extracting {} ...".format(input_path.name))
            _safe_extract_zip(input_path, extract_root)
            export_dir = find_export_root(extract_root)
        else:
            export_dir = find_export_root(input_path)

        print("Export directory: {}".format(export_dir))

        files = conversation_files(export_dir)
        print(
            "Loading {} conversation file{} ...".format(
                len(files), "" if len(files) == 1 else "s"
            )
        )
        conversations = load_conversations(export_dir)
        print("Found {} conversations".format(len(conversations)))

        convos_dir = output_dir / "conversations"
        assets_dir = output_dir / "assets"
        convos_dir.mkdir(parents=True, exist_ok=True)

        assets_map = {}
        missing_assets = set()

        if not args.skip_assets:
            print("Indexing assets ...")
            asset_index, original_names = build_asset_index(export_dir)
            print("Found {} asset aliases".format(len(asset_index)))

            all_referenced = set()
            for convo in conversations:
                all_referenced.update(collect_referenced_assets(convo))

            assets_map, missing_assets = copy_assets(
                referenced=all_referenced,
                asset_index=asset_index,
                original_names=original_names,
                export_dir=export_dir,
                assets_dir=assets_dir,
            )
            copied_files = set(assets_map.values())
            print("Copied {} referenced asset file(s)".format(len(copied_files)))
            if missing_assets:
                print(
                    "Warning: {} referenced asset(s) were not present in the export.".format(
                        len(missing_assets)
                    ),
                    file=sys.stderr,
                )

        conversations.sort(key=lambda c: c.get("create_time") or 0)

        converted = 0
        skipped = 0

        for i, convo in enumerate(conversations, 1):
            title = str(convo.get("title") or "Untitled")
            if args.verbose:
                print("[{}/{}] {}".format(i, len(conversations), title))

            md = render_conversation(convo, assets_map)
            if md is None:
                skipped += 1
                continue

            create_time = convo.get("create_time") or convo.get("update_time")
            dt = ts_to_datetime(create_time)
            year = str(dt.year)
            date_prefix = dt.strftime("%Y-%m-%d")
            filename = "{}-{}.md".format(date_prefix, slugify(title))

            year_dir = convos_dir / year
            year_dir.mkdir(parents=True, exist_ok=True)

            out_path = dedupe_filename(year_dir / filename)
            out_path.write_text(md, encoding="utf-8")
            converted += 1

        print("")
        print("Done: {} converted, {} skipped".format(converted, skipped))
        print("Output: {}".format(output_dir))
        if missing_assets:
            print(
                "Missing assets: {} (usually means ChatGPT did not include those bytes in the export)".format(
                    len(missing_assets)
                )
            )

        return 0

    except (OSError, RuntimeError, FileNotFoundError, zipfile.BadZipFile) as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 1
    finally:
        if temp_ctx is not None:
            temp_ctx.cleanup()


if __name__ == "__main__":
    sys.exit(main())
