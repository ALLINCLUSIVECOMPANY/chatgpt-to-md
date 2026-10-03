# ChatGPT Export to Markdown — Updated Export Format Support

This repository is a fork of the original ChatGPT export-to-Markdown converter, updated to support the newer ChatGPT data export format while retaining the original goal: turn a ChatGPT account export into a portable, human-readable Markdown archive.

The original converter was written around an earlier ChatGPT export layout where conversations were stored in a single `conversations.json` file and exported assets generally retained directly usable filenames.

Recent ChatGPT exports use a substantially different structure. Conversations may now be split across multiple numbered JSON files, uploaded files are frequently stored as opaque `.dat` blobs, original attachment names are maintained separately, and exports can contain a mixture of older and newer asset identifier formats.

This fork adds support for those changes and makes the converter more resilient when processing large or evolving ChatGPT exports.

---


## Why this fork exists

A current ChatGPT export may look more like:

```text
export/
├── chat.html
├── conversations-000.json
├── conversations-001.json
├── conversations-002.json
├── ...
├── conversation_asset_file_names.json
├── file-AbCdEf123.dat
├── file_000000001234abcd.dat
├── ...
└── other export metadata
```

rather than:

```text
export/
├── conversations.json
└── image files...
```

The original script expects:

```text
conversations.json
```

to exist at the root of the extracted export. As a result, it cannot process newer exports that contain:

```text
conversations-000.json
conversations-001.json
conversations-002.json
...
```

This fork updates the loader and asset handling so both formats can be converted.

---

# Major changes

## 1. Support for sharded conversation exports

The converter now recognizes both:

```text
conversations.json
```

and the newer numbered format:

```text
conversations-000.json
conversations-001.json
conversations-002.json
...
```

Numbered files are discovered and loaded in numerical order.

All conversations are combined into one conversion pass before being sorted by conversation creation time.

Conversation IDs are also tracked while loading so duplicate conversations are not written twice if duplicate data is encountered.

This maintains compatibility with older exports while allowing current large ChatGPT exports to work without manually combining JSON files first.

---

## 2. ZIP files can be converted directly

The input no longer has to be an already extracted export directory.

You can provide either:

```text
/path/to/extracted-export/
```

or:

```text
/path/to/chatgpt-export.zip
```

When a ZIP file is supplied, the converter extracts it into a temporary directory, automatically locates the directory containing the conversation files, performs the conversion, and cleans up the temporary extraction afterward.

ZIP extraction also checks archive paths before extraction to prevent ZIP path traversal outside the temporary directory.

Example:

```bash
python convert_fixed.py chatgpt-export.zip markdown-export
```

The older extracted-directory workflow remains supported:

```bash
python convert_fixed.py extracted-export markdown-export
```

---

## 3. Automatic export-root discovery

The conversation files are not assumed to exist directly at the exact directory passed on the command line.

The converter searches for:

```text
conversations.json
```

or:

```text
conversations-###.json
```

and identifies the actual export root.

This is useful for ZIP archives or directories containing an additional top-level export folder.

When several possible export roots exist, the converter attempts to identify the correct one using other recognizable export files such as:

```text
chat.html
export_manifest.json
```

rather than silently selecting an arbitrary directory.

---

## 4. Support for modern `.dat` asset exports

One of the larger differences in recent ChatGPT exports is asset storage.

Uploaded images and files may now appear as opaque files such as:

```text
file-AbCdEf123.dat
file_000000001234abcd.dat
```

The `.dat` extension does not necessarily describe the original file type.

ChatGPT provides a separate mapping file:

```text
conversation_asset_file_names.json
```

which can associate those exported blobs with their original filenames.

This fork reads that mapping and uses it when copying referenced assets into the Markdown archive.

For example, an exported object such as:

```text
file-AbCdEf123.dat
```

may be restored to a useful archive filename derived from something like:

```text
diagram.png
```

while retaining the ChatGPT asset ID as part of the stored filename when necessary to avoid collisions.

---

## 5. Broader asset-ID handling

The original converter handled a narrower set of asset naming conventions.

The updated asset resolver recognizes both major ChatGPT file-ID styles:

```text
file-AbCdEf123
```

and:

```text
file_000000001234abcd
```

as well as asset pointers using schemes such as:

```text
sediment://...
file-service://...
```

Asset references are normalized before lookup so the same exported object can be resolved even when different parts of the export refer to it using slightly different forms.

The converter builds aliases for:

- complete filenames;
- filename stems;
- root-relative paths;
- normalized `file-...` IDs;
- normalized `file_...` IDs;
- entries found in `conversation_asset_file_names.json`.

This makes asset resolution significantly less dependent on one specific export-generation format.

---

## 6. Recursive asset discovery

Instead of assuming every asset exists in exactly one root directory layout, the converter searches the export recursively for ChatGPT file assets.

Legacy DALL-E export directories are also still recognized when present.

This allows the converter to handle exports containing assets in slightly different directory arrangements without requiring the paths to be hardcoded.

---

## 7. Message attachment support

The original converter primarily handled images embedded directly inside `multimodal_text`.

Modern exports can also describe uploaded files through:

```text
message.metadata.attachments
```

This fork inspects those attachments separately.

Referenced files can therefore be added to the resulting Markdown even when they are not represented as an inline `image_asset_pointer`.

Images are rendered as Markdown images when their MIME type or restored filename identifies them as images:

```markdown
![image.png](../../assets/file-id-image.png)
```

Other attachments are rendered as normal Markdown links:

```markdown
[document.pdf](../../assets/file-id-document.pdf)
```

The converter avoids rendering the same attachment twice when it already appears inline in the message content.

---

## 8. Correct asset paths for year-based conversation directories

Conversation Markdown files are written using the existing structure:

```text
output/
├── assets/
└── conversations/
    ├── 2024/
    ├── 2025/
    └── 2026/
```

A conversation file therefore lives two directory levels below the archive root:

```text
conversations/2026/example.md
```

while its asset lives at:

```text
assets/image.png
```

The original Markdown path:

```markdown
../assets/image.png
```

would resolve to:

```text
conversations/assets/image.png
```

which is incorrect.

The fork changes generated asset URLs to:

```markdown
../../assets/image.png
```

so links correctly resolve from:

```text
conversations/<year>/
```

back to the top-level:

```text
assets/
```

directory.

---

## 9. Markdown-safe asset URLs

Asset paths are URL-encoded before being written into Markdown.

This improves local Markdown compatibility with filenames containing spaces and other characters that should be escaped in URLs.

For example:

```text
My Screenshot.png
```

can safely become:

```markdown
../../assets/My%20Screenshot.png
```

instead of relying on individual Markdown viewers to interpret an unescaped path.

---

## 10. Safer filenames on Windows

The fork adds filename sanitization designed to work with Windows filesystems.

Characters Windows does not allow in filenames are replaced:

```text
< > : " / \ | ? *
```

Control characters are also removed.

Special Windows device names such as:

```text
CON
PRN
AUX
NUL
COM1
LPT1
```

are protected so an exported attachment cannot accidentally produce an invalid Windows path.

Long filenames are shortened to reduce the likelihood of path-length issues while retaining the extension where possible.

This is particularly useful for large exports being converted directly on Windows.

---

## 11. Asset filename collision handling

Different conversations can contain attachments with the same original filename.

For example:

```text
image.png
```

may occur hundreds of times across an account export.

The new asset copier tracks filenames already written into `assets/` and generates unique names where necessary rather than overwriting an earlier file.

The underlying ChatGPT asset identifier is used where useful to provide stable disambiguation.

---

## 12. Only referenced assets are copied

The converter first scans the conversation DAGs and collects the assets actually referenced by messages.

Only those assets are copied into the generated archive.

This avoids blindly duplicating every file contained in a potentially very large ChatGPT export.

If a conversation references an asset whose bytes are not present in the export, the converter records it as missing and prints a warning instead of crashing the entire conversion.

---

# Conversation handling

## ChatGPT conversations are DAGs

The original converter correctly treats ChatGPT conversations as a directed acyclic graph rather than assuming that every conversation is a flat chronological message list.

That behavior remains central to this fork.

Each conversation contains a mapping of message nodes.

Editing a prompt, regenerating an answer, or otherwise changing a conversation can create another branch rather than replacing the previous message.

The converter follows:

```text
current_node
```

back through each node's:

```text
parent
```

reference to determine the canonical path—the branch that represented the conversation when the export was created.

That path is rendered normally.

Non-canonical children are retained as alternative branches using collapsible HTML `<details>` blocks instead of being silently discarded.

This means edited prompts and regenerated responses can remain part of the archive without making the primary conversation path unreadable.

---

## 13. Missing `current_node` fallback

Some conversations may not contain a usable `current_node`, or the referenced node may no longer be present in the exported mapping.

The original script skipped conversations that lacked this information.

The fork adds a fallback.

When a valid `current_node` cannot be found, the converter searches the conversation DAG for leaf nodes and selects the best available leaf using message creation time.

That leaf is then treated as the canonical endpoint.

This allows partially inconsistent exports to remain recoverable rather than causing the entire conversation to be discarded.

---

## 14. Alternative branch rendering remains preserved

Canonical children are rendered first.

Other children from a branch point are emitted inside sections such as:

```html
<details>
<summary>Alternative response (branch 2 of 3)</summary>

...

</details>
```

Branch rendering was also adjusted so role headings are reset when entering or leaving an alternative branch.

This prevents an alternative branch from accidentally inheriting the `User` or `Assistant` heading state from the canonical path.

---

# Message roles

The existing role behavior is preserved.

```text
user      → ## User
assistant → ## Assistant
system    → skipped
tool      → rendered inline
```

System messages are intentionally omitted because they commonly contain injected runtime context, system configuration, custom instructions, or other information that was not displayed as part of the normal conversation.

Messages explicitly marked:

```text
is_visually_hidden_from_conversation
```

are also skipped.

Tool output remains inline rather than being given its own top-level conversation role heading.

---

# Content rendering

The converter continues to translate ChatGPT's structured message content into readable Markdown.

Explicit handling currently exists for:

| Content type | Output |
|---|---|
| `text` | Markdown text |
| `multimodal_text` | Text plus local image references |
| `code` | Fenced Markdown code block |
| `execution_output` | Collapsible output block |
| `tether_quote` | Blockquote with source |
| `sonic_webpage` | Blockquote containing webpage information |
| `thoughts` | Collapsible thinking block |
| `reasoning_recap` | Italic recap |
| `system_error` | Error message |

The following internal/display-oriented content types continue to be skipped:

```text
tether_browsing_display
user_editable_context
computer_output
app_pairing_content
```

Unknown content types now receive a more defensive fallback. If they expose a direct `text` field, that text is rendered; otherwise the converter attempts the normal text-parts renderer.

Nested `audio_transcription` data encountered inside multimodal messages also receives a simple text fallback.

---


# Metadata improvements

Conversation Markdown continues to include YAML frontmatter containing information such as:

```yaml
title: "Example conversation"
date: 2026-09-30T22:33:26Z
conversation_id: ...
model: ...
models_used: [...]
message_count: ...
is_archived: false
```

The timestamp formatting now explicitly includes:

```text
Z
```

to indicate UTC.

The footer no longer contains the original script's hardcoded export date.

Instead of always writing a fixed date such as:

```text
Exported from ChatGPT on 2026-01-26
```

the fork records the date on which the archive was actually converted:

```text
Converted from a ChatGPT data export on YYYY-MM-DD
```

This avoids embedding an unrelated hardcoded date into every generated file.

---

# Error handling and resilience

The fork adds explicit handling for several common failure cases, including:

- input path does not exist;
- input file is not a ZIP archive;
- malformed JSON;
- unsupported conversation JSON structure;
- missing conversation shards;
- multiple ambiguous export directories;
- unsafe paths inside ZIP archives;
- missing exported asset bytes;
- invalid timestamps;
- missing `current_node`;
- duplicate asset filenames.

Errors that prevent conversion return a non-zero exit status.

Missing individual assets produce warnings where possible rather than terminating an otherwise usable archive conversion.

---

# Python compatibility

The converter continues to require no external Python packages.

It uses only the Python standard library.

The updated implementation intentionally avoids Python 3.10-only type-hint syntax such as:

```python
str | None
```

and instead uses forms compatible with the advertised Python 3.8+ requirement.

For example:

```python
Optional[str]
```

This means the implementation now more closely matches the project's stated:

```text
Python 3.8+
```

requirement.

---

# Output structure

The resulting archive remains intentionally simple:

```text
markdown-export/
├── assets/
│   ├── file-id-image.png
│   ├── file-id-document.pdf
│   └── ...
└── conversations/
    ├── 2023/
    │   └── YYYY-MM-DD-title.md
    ├── 2024/
    │   └── YYYY-MM-DD-title.md
    ├── 2025/
    │   └── YYYY-MM-DD-title.md
    └── 2026/
        └── YYYY-MM-DD-title.md
```

Conversation filenames are based on:

```text
YYYY-MM-DD + slugified conversation title
```

and duplicate output filenames receive numbered suffixes instead of overwriting existing conversations.

The result is a filesystem-based archive that can be browsed directly, indexed by other tools, imported into knowledge-management systems, committed to Git, or processed further without depending on `chat.html`.

---

# Usage

Convert an extracted export:

```bash
python convert_fixed.py /path/to/export /path/to/output
```

Convert the original ChatGPT ZIP directly:

```bash
python convert_fixed.py /path/to/export.zip /path/to/output
```

Print each conversation while processing:

```bash
python convert_fixed.py /path/to/export.zip /path/to/output --verbose
```

Skip copying attachments:

```bash
python convert_fixed.py /path/to/export.zip /path/to/output --skip-assets
```

---

# Compatibility summary

This fork is intended to support both generations of ChatGPT exports.

### Original/legacy exports

```text
conversations.json
file_...
file-...
dalle-generations/
```

### Newer exports

```text
conversations-000.json
conversations-001.json
...
conversation_asset_file_names.json
file-....dat
file_....dat
```

The goal is not to change the fundamental archive format established by the original project.

The goal is to make that converter usable against the current ChatGPT export structure while improving asset recovery, attachment handling, Windows compatibility, direct ZIP processing, and resilience to imperfect export data.

---

## Summary of fork-specific additions

Compared with the original version, this fork adds:

- support for `conversations-###.json` shards;
- backwards compatibility with `conversations.json`;
- direct conversion from ChatGPT export ZIP files;
- automatic export-root detection;
- safe ZIP extraction;
- support for `conversation_asset_file_names.json`;
- support for current `.dat` asset blobs;
- normalization of `file-...` and `file_...` identifiers;
- recursive asset indexing;
- metadata attachment rendering;
- original attachment filename recovery where available;
- asset collision prevention;
- missing-asset reporting;
- corrected `../../assets/` relative paths;
- URL-safe Markdown asset paths;
- Windows-safe filenames;
- fallback handling for missing `current_node`;
- improved handling of malformed or incomplete exports;
- duplicate conversation protection while loading shards;
- explicit UTC metadata timestamps;
- dynamically generated conversion dates;
- implementation syntax compatible with Python 3.8+;
- improved unknown-content fallbacks.
- preserves the original thoughts renderer. 

The existing DAG-based conversation reconstruction, canonical-path behavior, alternative branch preservation, Markdown-per-conversation layout, chronological organization, model metadata collection, and role-based rendering remain based on the original converter.
