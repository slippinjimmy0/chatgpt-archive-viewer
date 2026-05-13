#!/usr/bin/env python3
"""
ChatGPT Archive Viewer
----------------------
Drop this file into your unzipped ChatGPT export folder and run:

    python app.py

Opens your browser automatically. No pip installs required — pure stdlib.
Python 3.8+ required.
"""

import json
import mimetypes
import os
import re
import shutil
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# ── Find the export folder ───────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()

def find_export_dir():
    if len(sys.argv) > 1:
        p = Path(sys.argv[1]).resolve()
        if p.is_dir():
            return p
        print(f"Error: '{sys.argv[1]}' is not a directory.")
        sys.exit(1)
    if list(SCRIPT_DIR.glob("conversations-*.json")):
        return SCRIPT_DIR
    subdirs = [d for d in SCRIPT_DIR.iterdir() if d.is_dir() and list(d.glob("conversations-*.json"))]
    if len(subdirs) == 1:
        return subdirs[0]
    print("Could not find ChatGPT export folder.")
    print("Usage:  python app.py [path/to/export/folder]")
    print("Or:     place app.py inside your export folder and run it there.")
    sys.exit(1)

EXPORT_DIR = find_export_dir()
print(f"📂 Export folder: {EXPORT_DIR}")

# ── Load conversations ───────────────────────────────────────────────────────
def get_msg_preview(conv: dict) -> str:
    """Return first non-empty user text snippet for sidebar preview."""
    mapping = conv.get("mapping") or {}
    for node in mapping.values():
        msg = node.get("message") or {}
        if msg.get("author", {}).get("role") != "user":
            continue
        for part in (msg.get("content") or {}).get("parts") or []:
            if isinstance(part, str) and part.strip():
                return part.strip()[:120]
    return ""

def load_conversations():
    """Load all conversations, build a lean index, keep full data in a lookup."""
    conv_index   = []   # lightweight list sent to frontend sidebar
    conv_lookup  = {}   # id -> full conversation object (stays in Python)

    for f in sorted(EXPORT_DIR.glob("conversations-*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                continue
            for c in data:
                cid = c.get("id") or c.get("conversation_id") or ""
                if not cid or cid in conv_lookup:
                    continue
                conv_lookup[cid] = c
                conv_index.append({
                    "id":      cid,
                    "title":   c.get("title") or "Untitled",
                    "create_time":   c.get("create_time"),
                    "update_time":   c.get("update_time"),
                    "model":   c.get("default_model_slug") or "",
                    "preview": get_msg_preview(c),
                })
        except Exception as e:
            print(f"  Warning: could not read {f.name}: {e}")

    conv_index.sort(key=lambda c: c.get("create_time") or 0, reverse=True)
    print(f"✅ Loaded {len(conv_index)} conversations")
    return conv_index, conv_lookup

CONV_INDEX, CONV_LOOKUP = load_conversations()

# conv_id -> source JSON file path (needed to rewrite on delete)
CONV_SOURCE: dict = {}
for _f in sorted(EXPORT_DIR.glob("conversations-*.json")):
    try:
        _data = json.loads(_f.read_text(encoding="utf-8"))
        if isinstance(_data, list):
            for _c in _data:
                _cid = _c.get("id") or _c.get("conversation_id") or ""
                if _cid:
                    CONV_SOURCE[_cid] = _f
    except Exception:
        pass

# ── Scan images ──────────────────────────────────────────────────────────────
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

def classify_image(rel_path: Path) -> str:
    for part in rel_path.parts[:-1]:
        if re.match(r"user-[A-Za-z0-9_-]+", part):
            return "generated"
        if part.lower() in ("dalle-generations", "dalle_generations"):
            return "generated"
    if re.match(r"file_[0-9a-f]+\.", rel_path.name, re.IGNORECASE):
        return "generated"
    return "uploaded"

def scan_images():
    images = []
    seen = set()
    for root, dirs, files in os.walk(EXPORT_DIR):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fname in sorted(files):
            p = Path(root) / fname
            if p.suffix.lower() not in IMAGE_EXTS:
                continue
            rel = p.relative_to(EXPORT_DIR)
            key = rel.as_posix()
            if key in seen:
                continue
            seen.add(key)
            images.append({
                "id":   len(images),
                "name": fname,
                "path": key,
                "type": classify_image(rel),
            })
    print(f"✅ Found {len(images)} images")
    return images

ALL_IMAGES = scan_images()

# Numeric id -> absolute Path for fast serving
IMAGE_BY_ID: dict = {img["id"]: EXPORT_DIR / img["path"] for img in ALL_IMAGES}

# Inline image lookup: pointer ID -> image numeric id.
# pointer "sediment://file_HEX32"     -> filename "file_HEX32-uuid.png"  (exact prefix)
# pointer "file-service://file-ALPHA" -> filename "file-ALPHA-image.png" (exact prefix)
# We index each image under every plausible prefix length (36 and 37 chars cover both).
# Build FILE_ID_MAP — prefer manifest if present (authoritative), else fall back to regex
FILE_ID_MAP: dict = {}   # pointer_id -> numeric image id

def build_file_id_map_from_manifest(manifest_path: Path) -> dict:
    """Parse export_manifest.json to get exact pointer_id -> relative_path mappings."""
    result = {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        img_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        for key in manifest.get("logical_files", {}):
            if not any(key.lower().endswith(e) for e in img_exts):
                continue
            if "#" in key.split("/")[-1]:   # skip sharded tiles
                continue
            basename = key.split("/")[-1]
            m = re.match(r"(file[-_][A-Za-z0-9]+)", basename)
            if m:
                result[m.group(1)] = key    # pointer_id -> relative path
    except Exception as e:
        print(f"  Warning: could not parse manifest: {e}")
    return result

manifest_path = EXPORT_DIR / "export_manifest.json"
if manifest_path.exists():
    ptr_to_relpath = build_file_id_map_from_manifest(manifest_path)
    # Convert relative path -> numeric image id using the path index
    path_to_id = {img["path"]: img["id"] for img in ALL_IMAGES}
    for ptr_id, rel_path in ptr_to_relpath.items():
        img_id = path_to_id.get(rel_path)
        if img_id is not None:
            FILE_ID_MAP[ptr_id] = img_id
    print(f"✅ Indexed {len(FILE_ID_MAP)} images via manifest")
    # Pre-check: scan all conversations and report missing pointers now
    _missing = set()
    _img_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    for _cf in sorted(EXPORT_DIR.glob("conversations-*.json")):
        try:
            _cdata = json.loads(_cf.read_text(encoding="utf-8"))
            for _c in _cdata:
                for _node in (_c.get("mapping") or {}).values():
                    _msg = _node.get("message") or {}
                    for _p in (_msg.get("content") or {}).get("parts") or []:
                        if isinstance(_p, dict) and _p.get("content_type") == "image_asset_pointer":
                            _ptr = _p.get("asset_pointer", "")
                            _fid = re.sub(r"^(sediment|file-service)://", "", _ptr)
                            _m = re.match(r"(file[-_][A-Za-z0-9]+)", _fid)
                            if _m and _m.group(1) not in FILE_ID_MAP:
                                _missing.add(_m.group(1))
        except Exception:
            pass
    if _missing:
        print(f"  ⚠ {len(_missing)} image pointer(s) not in export (shown as 'not in export'):")
        for _mid in sorted(_missing):
            print(f"    {_mid}")
    else:
        print("  ✅ All conversation image pointers resolved!")
else:
    # Fallback: regex-based prefix matching
    for img in ALL_IMAGES:
        m = re.match(r"(file[-_][A-Za-z0-9]+)", img["name"])
        if m:
            FILE_ID_MAP[m.group(1)] = img["id"]
    print(f"✅ Indexed {len(FILE_ID_MAP)} images via filename regex (no manifest found)")

# ── Trash ────────────────────────────────────────────────────────────────────
TRASH_DIR = EXPORT_DIR / ".trash"

def ensure_trash():
    (TRASH_DIR / "images").mkdir(parents=True, exist_ok=True)
    (TRASH_DIR / "conversations").mkdir(parents=True, exist_ok=True)

ensure_trash()

def trash_conversation(cid: str) -> dict:
    """Remove conversation from its JSON file, save metadata to trash."""
    conv = CONV_LOOKUP.get(cid)
    if not conv:
        return {"ok": False, "error": "not found"}
    src = CONV_SOURCE.get(cid)
    if not src:
        return {"ok": False, "error": "source file not found"}
    # Save full conv to trash
    trash_file = TRASH_DIR / "conversations" / f"{cid}.json"
    trash_file.write_text(json.dumps({
        "source_file": src.name,
        "conversation": conv,
    }, ensure_ascii=False), encoding="utf-8")
    # Rewrite source JSON without this conversation
    data = json.loads(src.read_text(encoding="utf-8"))
    data = [c for c in data if (c.get("id") or c.get("conversation_id")) != cid]
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    # Remove from in-memory structures
    CONV_LOOKUP.pop(cid, None)
    CONV_SOURCE.pop(cid, None)
    idx = next((i for i, c in enumerate(CONV_INDEX) if c["id"] == cid), None)
    if idx is not None:
        CONV_INDEX.pop(idx)
    print(f"🗑  Trashed conversation: {conv.get('title','?')}")
    return {"ok": True}

def trash_message(cid: str, node_id: str) -> dict:
    """Remove a single message node from a conversation mapping."""
    conv = CONV_LOOKUP.get(cid)
    if not conv:
        return {"ok": False, "error": "conversation not found"}
    mapping = conv.get("mapping") or {}
    if node_id not in mapping:
        return {"ok": False, "error": "message not found"}
    src = CONV_SOURCE.get(cid)
    if not src:
        return {"ok": False, "error": "source file not found"}
    # Save deleted node to trash
    trash_file = TRASH_DIR / "conversations" / f"{cid}_msg_{node_id}.json"
    trash_file.write_text(json.dumps({
        "source_file": src.name,
        "conv_id": cid,
        "node_id": node_id,
        "node": mapping[node_id],
    }, ensure_ascii=False), encoding="utf-8")
    # Remove from mapping; fix parent/child refs
    node = mapping.pop(node_id)
    parent_id = node.get("parent")
    children  = node.get("children") or []
    # Reattach children to grandparent
    if parent_id and parent_id in mapping:
        parent = mapping[parent_id]
        parent_children = parent.get("children") or []
        if node_id in parent_children:
            parent_children.remove(node_id)
        parent_children.extend(children)
        parent["children"] = parent_children
    for child_id in children:
        if child_id in mapping:
            mapping[child_id]["parent"] = parent_id
    # Rewrite source JSON
    data = json.loads(src.read_text(encoding="utf-8"))
    for c in data:
        if (c.get("id") or c.get("conversation_id")) == cid:
            c["mapping"] = mapping
            break
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"🗑  Trashed message {node_id[:8]}… from '{conv.get('title','?')}'")
    return {"ok": True}

def trash_image(img_id: int) -> dict:
    """Move an image file to the trash folder."""
    path = IMAGE_BY_ID.get(img_id)
    if not path or not path.exists():
        return {"ok": False, "error": "image not found"}
    dest = TRASH_DIR / "images" / path.name
    # Avoid collision
    if dest.exists():
        dest = TRASH_DIR / "images" / f"{img_id}_{path.name}"
    shutil.move(str(path), str(dest))
    IMAGE_BY_ID.pop(img_id, None)
    img = next((i for i in ALL_IMAGES if i["id"] == img_id), None)
    if img:
        ALL_IMAGES.remove(img)
    print(f"🗑  Trashed image: {path.name}")
    return {"ok": True}

def list_trash() -> dict:
    """Return lists of trashed conversations and images."""
    convs = []
    for f in sorted((TRASH_DIR / "conversations").glob("*.json")):
        if "_msg_" in f.name:
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            c = d.get("conversation") or {}
            convs.append({
                "id":    c.get("id") or c.get("conversation_id") or f.stem,
                "title": c.get("title") or "Untitled",
                "create_time": c.get("create_time"),
                "source_file": d.get("source_file"),
                "trash_file":  f.name,
            })
        except Exception:
            pass
    imgs = []
    for f in sorted((TRASH_DIR / "images").iterdir()):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
            imgs.append({"name": f.name, "trash_file": f.name})
    return {"conversations": convs, "images": imgs,
            "total": len(convs) + len(imgs)}

def restore_conversation(trash_fname: str) -> dict:
    """Move a trashed conversation back into its source JSON."""
    tf = TRASH_DIR / "conversations" / trash_fname
    if not tf.exists():
        return {"ok": False, "error": "trash file not found"}
    d = json.loads(tf.read_text(encoding="utf-8"))
    conv = d["conversation"]
    src_name = d.get("source_file", "conversations-000.json")
    src = EXPORT_DIR / src_name
    if src.exists():
        data = json.loads(src.read_text(encoding="utf-8"))
    else:
        data = []
    data.append(conv)
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tf.unlink()
    # Re-add to in-memory structures
    cid = conv.get("id") or conv.get("conversation_id") or ""
    if cid and cid not in CONV_LOOKUP:
        CONV_LOOKUP[cid] = conv
        CONV_SOURCE[cid] = src
        CONV_INDEX.append({
            "id": cid, "title": conv.get("title") or "Untitled",
            "create_time": conv.get("create_time"),
            "update_time": conv.get("update_time"),
            "model": conv.get("default_model_slug") or "",
            "preview": get_msg_preview(conv),
        })
        CONV_INDEX.sort(key=lambda c: c.get("create_time") or 0, reverse=True)
    print(f"♻️  Restored conversation: {conv.get('title','?')}")
    return {"ok": True}

def restore_image(trash_fname: str) -> dict:
    """Move a trashed image back to the export root."""
    tf = TRASH_DIR / "images" / trash_fname
    if not tf.exists():
        return {"ok": False, "error": "trash file not found"}
    dest = EXPORT_DIR / tf.name
    if dest.exists():
        return {"ok": False, "error": "file already exists at destination"}
    shutil.move(str(tf), str(dest))
    print(f"♻️  Restored image: {tf.name}")
    return {"ok": True}

def empty_trash() -> dict:
    """Permanently delete everything in the trash."""
    count = 0
    for f in TRASH_DIR.rglob("*"):
        if f.is_file():
            f.unlink()
            count += 1
    print(f"💥 Emptied trash: {count} files permanently deleted")
    return {"ok": True, "deleted": count}

# ── Stats ────────────────────────────────────────────────────────────────────
def build_stats():
    total_msgs = 0
    models: dict = {}
    years:  dict = {}
    for item in CONV_INDEX:
        m = item.get("model") or "unknown"
        models[m] = models.get(m, 0) + 1
        if item.get("create_time"):
            y = str(time.gmtime(item["create_time"]).tm_year)
            years[y] = years.get(y, 0) + 1
    # Count messages from full data
    for c in CONV_LOOKUP.values():
        mapping = c.get("mapping") or {}
        for node in mapping.values():
            msg = node.get("message")
            if msg and msg.get("author", {}).get("role") not in (None, "system"):
                total_msgs += 1
    return {
        "conversations": len(CONV_INDEX),
        "messages":      total_msgs,
        "images":        len(ALL_IMAGES),
        "models":        dict(sorted(models.items())),
        "years":         dict(sorted(years.items(), reverse=True)),
    }

STATS = build_stats()

# ── HTML ─────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ChatGPT Archive</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&family=Cal+Sans&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/9.1.6/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
/* ── THEMES ── */
:root{
  --bg:#0d0d0f;--surf:#131316;--surf2:#18181c;--surf3:#1f1f24;
  --border:#27272d;--border2:#35353d;
  --gold:#d4956a;--gold-dim:rgba(212,149,106,.13);--gold-mid:rgba(212,149,106,.28);
  --teal:#5ec4b8;--teal-dim:rgba(94,196,184,.1);
  --rose:#e07a7a;--rose-dim:rgba(224,122,122,.1);
  --green:#6ec87e;
  --text:#f0ede8;--text2:#8a8a94;--text3:#48484f;
  /* user bubble: warm amber glow */
  --user-bg:#1e1509;--user-border:#3d2e18;
  --user-badge-bg:#3d2e18;--user-badge-text:#d4956a;
  /* assistant bubble: cool dark */
  --asst-bg:#18181c;--asst-border:#27272d;
  --asst-badge-bg:#1f2a2a;--asst-badge-text:#5ec4b8;
  --sidebar-w:300px;--top-h:50px;--tab-h:40px;
}

/* ── Nord ── */
[data-theme="nord"]{
  --bg:#242933;--surf:#2e3440;--surf2:#3b4252;--surf3:#434c5e;
  --border:#3b4252;--border2:#4c566a;
  --gold:#ebcb8b;--gold-dim:rgba(235,203,139,.13);--gold-mid:rgba(235,203,139,.28);
  --teal:#88c0d0;--teal-dim:rgba(136,192,208,.1);
  --rose:#bf616a;--rose-dim:rgba(191,97,106,.12);
  --green:#a3be8c;
  --text:#eceff4;--text2:#d8dee9;--text3:#7a8898;
  --user-bg:#2d2e1e;--user-border:#4a4830;
  --user-badge-bg:#4a4830;--user-badge-text:#ebcb8b;
  --asst-bg:#2e3440;--asst-border:#3b4252;
  --asst-badge-bg:#1e2a38;--asst-badge-text:#88c0d0;
}

/* ── Slate ── */
[data-theme="slate"]{
  --bg:#0a0e1a;--surf:#0f1525;--surf2:#141c30;--surf3:#1a243c;
  --border:#1e2d4a;--border2:#263860;
  --gold:#7aa8f5;--gold-dim:rgba(122,168,245,.12);--gold-mid:rgba(122,168,245,.25);
  --teal:#4dd9b0;--teal-dim:rgba(77,217,176,.09);
  --rose:#f87171;--rose-dim:rgba(248,113,113,.1);
  --green:#4dd9b0;
  --text:#dde8f8;--text2:#7a96c0;--text3:#3a506e;
  --user-bg:#0a1628;--user-border:#1a3060;
  --user-badge-bg:#1a3060;--user-badge-text:#7aa8f5;
  --asst-bg:#141c30;--asst-border:#1e2d4a;
  --asst-badge-bg:#0d1e30;--asst-badge-text:#4dd9b0;
}

/* ── Mocha ── */
[data-theme="mocha"]{
  --bg:#0e0b09;--surf:#171210;--surf2:#201a16;--surf3:#2a221c;
  --border:#352a22;--border2:#45382e;
  --gold:#d4956a;--gold-dim:rgba(212,149,106,.12);--gold-mid:rgba(212,149,106,.26);
  --teal:#7ec8a4;--teal-dim:rgba(126,200,164,.09);
  --rose:#d97878;--rose-dim:rgba(217,120,120,.1);
  --green:#7ec8a4;
  --text:#f0e0cc;--text2:#9a7e68;--text3:#52402e;
  --user-bg:#201408;--user-border:#452a14;
  --user-badge-bg:#452a14;--user-badge-text:#d4956a;
  --asst-bg:#201a16;--asst-border:#352a22;
  --asst-badge-bg:#0e1e18;--asst-badge-text:#7ec8a4;
}

/* ── Arctic (light) ── */
[data-theme="arctic"]{
  --bg:#f4f6fa;--surf:#ffffff;--surf2:#edf0f7;--surf3:#e0e6f0;
  --border:#ccd4e8;--border2:#a8b8d8;
  --gold:#3b6fd4;--gold-dim:rgba(59,111,212,.09);--gold-mid:rgba(59,111,212,.2);
  --teal:#0e8a7a;--teal-dim:rgba(14,138,122,.08);
  --rose:#d43b3b;--rose-dim:rgba(212,59,59,.08);
  --green:#1a8a4a;
  --text:#1a2340;--text2:#4a5878;--text3:#8a9ab8;
  --user-bg:#dde8ff;--user-border:#a8b8e8;
  --user-badge-bg:#c0d0f8;--user-badge-text:#1a4090;
  --asst-bg:#ffffff;--asst-border:#ccd4e8;
  --asst-badge-bg:#e0f0ee;--asst-badge-text:#0e6a5a;
}

/* ── Terminal ── */
[data-theme="terminal"]{
  --bg:#050505;--surf:#0a0a0a;--surf2:#101010;--surf3:#161616;
  --border:#222222;--border2:#303030;
  --gold:#00e87a;--gold-dim:rgba(0,232,122,.08);--gold-mid:rgba(0,232,122,.18);
  --teal:#00c8f0;--teal-dim:rgba(0,200,240,.07);
  --rose:#ff4455;--rose-dim:rgba(255,68,85,.1);
  --green:#00e87a;
  --text:#d8d8d8;--text2:#787878;--text3:#404040;
  --user-bg:#041408;--user-border:#0a3018;
  --user-badge-bg:#0a3018;--user-badge-text:#00e87a;
  --asst-bg:#101010;--asst-border:#222222;
  --asst-badge-bg:#081820;--asst-badge-text:#00c8f0;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:'Inter',sans-serif;font-size:14px;-webkit-font-smoothing:antialiased;letter-spacing:-.01em}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border2);border-radius:99px}
::-webkit-scrollbar-thumb:hover{background:var(--text3)}

/* TOPBAR */
#topbar{position:fixed;top:0;left:0;right:0;height:var(--top-h);background:var(--surf);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;padding:0 16px;z-index:100;backdrop-filter:blur(12px)}
#logo{font-family:'Cal Sans','Inter',sans-serif;font-size:15px;font-weight:600;color:var(--gold);white-space:nowrap;display:flex;align-items:center;gap:8px;flex-shrink:0;letter-spacing:.01em}
#logo span{color:var(--text3);font-weight:400;font-size:11px;font-family:'JetBrains Mono',monospace;letter-spacing:0}
#search-wrap{flex:1;max-width:400px;position:relative}
#search{width:100%;background:var(--surf2);border:1px solid var(--border);color:var(--text);font-family:'Inter',sans-serif;font-size:12px;padding:7px 12px 7px 32px;border-radius:8px;outline:none;transition:border-color .15s,background .15s}
#search:focus{border-color:var(--gold);background:var(--bg)}
#search::placeholder{color:var(--text3)}
.search-icon{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--text3);font-size:13px;pointer-events:none}
.bar-right{margin-left:auto;display:flex;align-items:center;gap:6px;flex-shrink:0}
select{background:var(--surf2);border:1px solid var(--border);color:var(--text2);font-family:'Inter',sans-serif;font-size:11px;padding:5px 8px;border-radius:6px;cursor:pointer;outline:none;transition:border-color .15s}
select:hover,select:focus{border-color:var(--gold);color:var(--text)}
#stats-pill{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3);white-space:nowrap;padding:4px 10px;background:var(--surf2);border:1px solid var(--border);border-radius:99px}

/* TABBAR */
#tabbar{position:fixed;top:var(--top-h);left:0;right:0;height:var(--tab-h);background:var(--surf);border-bottom:1px solid var(--border);display:flex;align-items:stretch;z-index:99;padding:0 16px;gap:0}
.tab-btn{display:flex;align-items:center;gap:6px;padding:0 14px;font-family:'Inter',sans-serif;font-size:11px;font-weight:500;color:var(--text3);cursor:pointer;border:none;background:none;border-bottom:2px solid transparent;transition:color .15s,border-color .15s;letter-spacing:.02em}
.tab-btn:hover{color:var(--text2)}
.tab-btn.active{color:var(--gold);border-bottom-color:var(--gold)}
.tab-badge{background:var(--surf3);color:var(--text3);font-size:10px;padding:2px 6px;border-radius:99px;font-family:'JetBrains Mono',monospace;font-weight:400}
.tab-btn.active .tab-badge{background:var(--gold-dim);color:var(--gold)}

#shell{position:fixed;top:calc(var(--top-h) + var(--tab-h));left:0;right:0;bottom:0;display:flex;overflow:hidden}

/* SIDEBAR */
#conv-tab{display:flex;flex:1;overflow:hidden;width:100%}
#sidebar{width:var(--sidebar-w);flex-shrink:0;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;background:var(--surf)}
#sidebar-hd{padding:10px 14px;border-bottom:1px solid var(--border);flex-shrink:0;display:flex;justify-content:space-between;align-items:center}
#conv-count{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3);letter-spacing:.04em}
#conv-list{flex:1;overflow-y:auto}
.conv-item{padding:10px 14px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .1s;position:relative;padding-right:44px}
.conv-item:hover{background:var(--surf2)}
.conv-item.active{background:var(--surf2);border-left:3px solid var(--gold);padding-left:11px}
.ci-title{font-size:12.5px;font-weight:500;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:3px}
.ci-meta{display:flex;gap:5px;align-items:center}
.ci-date{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--text3)}
.ci-model{font-family:'JetBrains Mono',monospace;font-size:9px;padding:1px 5px;border-radius:4px;background:var(--teal-dim);color:var(--teal);white-space:nowrap}
.ci-model.m4{background:var(--gold-dim);color:var(--gold)}
.ci-model.mo{background:var(--rose-dim);color:var(--rose)}
.ci-preview{font-size:11px;color:var(--text3);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.4}
.no-results{padding:40px 16px;text-align:center;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text3)}

/* READER */
#reader{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0;background:var(--bg)}
#reader-hd{padding:14px 24px;border-bottom:1px solid var(--border);flex-shrink:0;background:var(--surf)}
#reader-hd-inner{max-width:780px;margin:0 auto}
#reader-hd h2{font-family:'Cal Sans','Inter',sans-serif;font-size:17px;font-weight:600;margin-bottom:5px;line-height:1.3;letter-spacing:.01em}
#reader-meta{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
#reader-meta span{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3)}
#reader-meta span b{color:var(--text2);font-weight:400}

/* MESSAGE THREAD */
#messages{flex:1;overflow-y:auto;padding:20px 28px;display:flex;flex-direction:column;gap:6px}
/* Chat bubble layout — centered column, offset bubbles within it */
#messages-inner{
  width:100%;
  max-width:900px;       /* column never goes wider than this */
  margin:0 auto;
  display:flex;flex-direction:column;gap:6px;
}
.msg-block{
  display:flex;flex-direction:column;
  padding:16px 18px;border-radius:12px;
  border:1px solid transparent;position:relative;
  width:75%;             /* bubble is 75% of the column */
}
.msg-block.role-user{
  background:var(--user-bg);border-color:var(--user-border);
  margin-left:auto;      /* push to right side of column */
  border-bottom-right-radius:4px;
}
.msg-block.role-assistant,.msg-block.role-tool{
  background:var(--asst-bg);border-color:var(--asst-border);
  margin-right:auto;     /* push to left side of column */
  border-bottom-left-radius:4px;
}
.msg-role-row{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.role-badge{font-size:10px;font-weight:600;letter-spacing:.04em;padding:3px 9px;border-radius:6px;flex-shrink:0;font-family:'Inter',sans-serif}
.role-badge.user{background:var(--user-badge-bg);color:var(--user-badge-text)}
.role-badge.assistant,.role-badge.tool{background:var(--asst-badge-bg);color:var(--asst-badge-text)}
.msg-ts{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--text3);opacity:.7}
.role-line{flex:1;max-width:40px;height:1px;background:var(--border);opacity:.5}

/* MARKDOWN */
.msg-body{font-size:13.5px;line-height:1.78;color:var(--text);word-break:break-word}
.msg-body p{margin-bottom:10px}
.msg-body p:last-child{margin-bottom:0}
.msg-body h1,.msg-body h2,.msg-body h3,.msg-body h4{font-family:'Cal Sans','Inter',sans-serif;font-weight:600;margin:18px 0 7px;color:var(--text);letter-spacing:.01em}
.msg-body h1{font-size:19px}.msg-body h2{font-size:16px}.msg-body h3{font-size:14px}.msg-body h4{font-size:13px}
.msg-body ul,.msg-body ol{padding-left:20px;margin-bottom:10px}
.msg-body li{margin-bottom:3px;line-height:1.7}
.msg-body blockquote{border-left:3px solid var(--gold);padding:8px 14px;margin:10px 0;color:var(--text2);background:var(--surf2);border-radius:0 8px 8px 0}
.msg-body hr{border:none;border-top:1px solid var(--border);margin:14px 0}
.msg-body a{color:var(--teal);text-decoration:none;border-bottom:1px solid var(--teal-dim)}
.msg-body a:hover{border-bottom-color:var(--teal)}
.msg-body strong{color:var(--text);font-weight:600}
.msg-body em{font-style:italic;color:var(--text2)}
.msg-body table{border-collapse:collapse;width:100%;margin:10px 0;font-size:12.5px;border-radius:8px;overflow:hidden}
.msg-body th,.msg-body td{padding:8px 12px;border:1px solid var(--border);text-align:left}
.msg-body th{background:var(--surf3);color:var(--text);font-weight:600;font-size:11px}
.msg-body tr:nth-child(even){background:rgba(255,255,255,.02)}
.msg-body :not(pre) > code{font-family:'JetBrains Mono',monospace;font-size:11.5px;background:var(--surf3);padding:2px 6px;border-radius:5px;color:var(--teal);border:1px solid var(--border)}
.msg-body pre{margin:10px 0;border-radius:10px;border:1px solid var(--border);overflow:hidden}
.code-header{display:flex;align-items:center;justify-content:space-between;padding:8px 14px;background:var(--surf3);border-bottom:1px solid var(--border);font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3)}
.msg-body pre code.hljs{font-size:12px;line-height:1.6;padding:14px 16px;border-radius:0;background:#0d0d0e;display:block;font-family:'JetBrains Mono',monospace}
.copy-btn{font-family:'Inter',sans-serif;font-size:10px;font-weight:500;padding:2px 8px;background:var(--surf2);border:1px solid var(--border);color:var(--text3);border-radius:4px;cursor:pointer;transition:all .15s}
.copy-btn:hover{border-color:var(--gold);color:var(--gold)}
.copy-btn.copied{border-color:var(--green);color:var(--green)}
.msg-body .img-wrap{margin:10px 0}
.msg-body .img-wrap img{max-width:460px;max-height:340px;border-radius:10px;border:1px solid var(--border);cursor:zoom-in;transition:opacity .15s;display:block}
.msg-body .img-wrap img:hover{opacity:.9}
.img-missing{display:inline-flex;align-items:center;gap:5px;padding:4px 9px;background:var(--surf2);border:1px dashed var(--border);border-radius:5px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text3);opacity:.4}
.img-debug{display:inline-flex;flex-direction:column;gap:4px;padding:10px 14px;background:var(--surf2);border:1px dashed var(--border2);border-radius:8px;font-family:'JetBrains Mono',monospace;cursor:pointer;transition:border-color .15s;max-width:460px}
.img-debug:hover{border-color:var(--gold)}
.img-debug .id-label{font-size:10px;color:var(--text3)}
.img-debug .id-val{font-size:11px;color:var(--teal);word-break:break-all}
.img-debug .id-hint{font-size:9px;color:var(--text3)}
.img-debug.copied .id-hint{color:var(--green)}
#reader-empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px}
#reader-empty .big{font-size:52px;opacity:.06}

/* IMAGES TAB — hidden by default, shown via .active class */
#images-tab{display:none;flex:1;flex-direction:column;overflow:hidden;width:100%}
#images-tab.active{display:flex}
#img-controls{padding:12px 20px;border-bottom:1px solid var(--border);flex-shrink:0;display:flex;align-items:center;gap:10px;background:var(--surf2)}
#img-filter-btns{display:flex;gap:6px}
.img-filter-btn{font-family:'DM Mono',monospace;font-size:11px;padding:5px 12px;border-radius:5px;border:1px solid var(--border);background:none;color:var(--text3);cursor:pointer;transition:all .15s}
.img-filter-btn:hover{border-color:var(--border2);color:var(--text2)}
.img-filter-btn.active{background:var(--gold-dim);border-color:var(--gold);color:var(--gold)}
#img-search{background:var(--bg);border:1px solid var(--border);color:var(--text);font-family:'DM Mono',monospace;font-size:11px;padding:5px 10px;border-radius:5px;outline:none;width:200px;transition:border-color .15s}
#img-search:focus{border-color:var(--gold)}
#img-search::placeholder{color:var(--text3)}
#img-count{font-family:'DM Mono',monospace;font-size:11px;color:var(--text3)}
#img-pagination{display:flex;align-items:center;gap:6px;margin-left:auto}
.pg-btn{font-family:'DM Mono',monospace;font-size:11px;padding:4px 10px;border-radius:5px;border:1px solid var(--border);background:none;color:var(--text3);cursor:pointer;transition:all .15s;min-width:32px}
.pg-btn:hover:not(:disabled){border-color:var(--gold);color:var(--gold)}
.pg-btn:disabled{opacity:.3;cursor:default}
.pg-btn.active{background:var(--gold-dim);border-color:var(--gold);color:var(--gold)}
#pg-info{font-family:'DM Mono',monospace;font-size:11px;color:var(--text3);padding:0 6px;white-space:nowrap}
#img-grid{flex:1;overflow-y:auto;padding:16px;display:grid;grid-template-columns:repeat(6,1fr);gap:8px;align-content:start}
@media(max-width:1200px){#img-grid{grid-template-columns:repeat(5,1fr)}}
@media(max-width:900px){#img-grid{grid-template-columns:repeat(4,1fr)}}
.img-card{position:relative;cursor:pointer;border-radius:6px;overflow:hidden;border:1px solid var(--border);background:var(--surf3);padding-top:100%;transition:border-color .15s,opacity .15s}
.img-card:hover{border-color:var(--border2);opacity:.88}
.img-card img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
.img-card-type{position:absolute;top:6px;left:6px;font-family:'DM Mono',monospace;font-size:9px;padding:2px 6px;border-radius:3px;text-transform:uppercase;letter-spacing:.06em;backdrop-filter:blur(8px);font-weight:500}
.img-card-type.uploaded{background:rgba(212,168,83,.85);color:#000}
.img-card-type.generated{background:rgba(94,196,196,.85);color:#000}
.img-no-files{grid-column:1/-1;text-align:center;padding:80px 20px;font-family:'DM Mono',monospace;font-size:12px;color:var(--text3);line-height:2}

/* LOADING */
#loading{position:fixed;inset:0;background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:20px;z-index:300}
.load-title{font-family:'Syne',sans-serif;font-size:28px;font-weight:700;color:var(--gold)}
.load-sub{font-family:'DM Mono',monospace;font-size:13px;color:var(--text3)}
.spinner{width:32px;height:32px;border:2px solid var(--border2);border-top-color:var(--gold);border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* LIGHTBOX */
#lightbox{position:fixed;inset:0;z-index:500;background:rgba(0,0,0,.93);display:none;align-items:center;justify-content:center;flex-direction:column;gap:14px}
#lightbox.open{display:flex}
#lightbox img{max-width:90vw;max-height:82vh;border-radius:8px;box-shadow:0 0 80px rgba(0,0,0,.9);display:block}
#lightbox-close{position:fixed;top:18px;right:22px;font-size:26px;color:var(--text2);cursor:pointer;background:none;border:none;line-height:1;transition:color .15s}
#lightbox-close:hover{color:var(--text)}
#lightbox-info{font-family:'DM Mono',monospace;font-size:11px;color:var(--text3);text-align:center;max-width:600px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#lightbox-nav{display:flex;gap:12px;align-items:center}
.lb-nav-btn{font-family:'DM Mono',monospace;font-size:12px;padding:7px 20px;background:var(--surf2);border:1px solid var(--border);color:var(--text2);border-radius:6px;cursor:pointer;transition:all .15s}
.lb-nav-btn:hover{border-color:var(--gold);color:var(--gold)}
#lb-open-btn{font-family:'DM Mono',monospace;font-size:11px;padding:6px 14px;background:none;border:1px solid var(--border);color:var(--text3);border-radius:5px;cursor:pointer;text-decoration:none;transition:all .15s}
#lb-open-btn:hover{border-color:var(--teal);color:var(--teal)}

mark{background:rgba(212,168,83,.28);color:inherit;border-radius:2px;padding:0 1px}
.hidden{display:none!important}

/* DELETE BUTTONS */
.del-btn{position:absolute;opacity:0;pointer-events:none;background:rgba(224,122,122,.15);border:1px solid rgba(224,122,122,.3);color:var(--rose);font-family:'DM Mono',monospace;font-size:10px;padding:3px 8px;border-radius:4px;cursor:pointer;transition:all .15s;z-index:10}
.del-btn:hover{background:rgba(224,122,122,.28);border-color:var(--rose)}
.conv-item:hover .del-btn,.msg-block:hover .del-btn,.img-card:hover .del-btn{opacity:1;pointer-events:auto}
.conv-item{position:relative;padding-right:52px}
.conv-item .del-btn{top:50%;right:10px;transform:translateY(-50%)}
.msg-block{position:relative}
.msg-block .del-btn{top:16px;right:16px}
.img-card .del-btn{top:6px;right:6px;padding:2px 6px;font-size:9px}

/* TRASH TAB */
#trash-tab{flex:1;display:none;flex-direction:column;overflow:hidden;width:100%}
#trash-tab.active{display:flex}
#trash-controls{padding:12px 20px;border-bottom:1px solid var(--border);flex-shrink:0;display:flex;align-items:center;gap:12px;background:var(--surf2)}
#trash-section-btns{display:flex;gap:6px}
.trash-section-btn{font-family:'DM Mono',monospace;font-size:11px;padding:5px 12px;border-radius:5px;border:1px solid var(--border);background:none;color:var(--text3);cursor:pointer;transition:all .15s}
.trash-section-btn:hover{border-color:var(--border2);color:var(--text2)}
.trash-section-btn.active{background:var(--rose-dim);border-color:var(--rose);color:var(--rose)}
#trash-count{font-family:'DM Mono',monospace;font-size:11px;color:var(--text3)}
#empty-trash-btn{margin-left:auto;font-family:'DM Mono',monospace;font-size:11px;padding:5px 14px;background:var(--rose-dim);border:1px solid var(--rose);color:var(--rose);border-radius:5px;cursor:pointer;transition:all .15s}
#empty-trash-btn:hover{background:rgba(224,122,122,.25)}
#trash-list{flex:1;overflow-y:auto;padding:20px}
.trash-item{display:flex;align-items:center;gap:12px;padding:12px 14px;border:1px solid var(--border);border-radius:8px;margin-bottom:8px;background:var(--surf2)}
.trash-item-info{flex:1;min-width:0}
.trash-item-title{font-size:13px;font-weight:500;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trash-item-meta{font-family:'DM Mono',monospace;font-size:10px;color:var(--text3);margin-top:3px}
.trash-item img{width:56px;height:56px;object-fit:cover;border-radius:5px;border:1px solid var(--border);flex-shrink:0}
.restore-btn{font-family:'DM Mono',monospace;font-size:10px;padding:4px 10px;background:var(--teal-dim);border:1px solid rgba(94,196,196,.3);color:var(--teal);border-radius:4px;cursor:pointer;white-space:nowrap;transition:all .15s;flex-shrink:0}
.restore-btn:hover{background:rgba(94,196,196,.2)}
.trash-empty-msg{text-align:center;padding:60px 20px;font-family:'DM Mono',monospace;font-size:12px;color:var(--text3)}
#trash-img-grid{flex:1;overflow-y:auto;padding:20px;display:grid;grid-template-columns:repeat(6,1fr);gap:8px;align-content:start;display:none}
#trash-img-grid.active{display:grid}
</style>
</head>
<body>

<div id="loading">
  <div class="load-title">ChatGPT Archive</div>
  <div class="spinner"></div>
  <div class="load-sub" id="load-sub">Loading…</div>
</div>

<div id="topbar" class="hidden">
  <div id="logo">ChatGPT Archive <span id="logo-sub"></span></div>
  <div id="search-wrap">
    <span class="search-icon">⌕</span>
    <input id="search" type="text" placeholder="search conversations…" autocomplete="off" spellcheck="false">
  </div>
  <div class="bar-right">
    <select id="model-filter"><option value="">all models</option></select>
    <select id="year-filter"><option value="">all years</option></select>
    <select id="sort-filter">
      <option value="desc">newest first</option>
      <option value="asc">oldest first</option>
    </select>
    <select id="theme-picker" title="Theme">
      <option value="">🌑 Dark</option>
      <option value="nord">❄️ Nord</option>
      <option value="slate">🔷 Slate</option>
      <option value="mocha">☕ Mocha</option>
      <option value="arctic">🏔 Arctic</option>
      <option value="terminal">💻 Terminal</option>
    </select>
    <div id="stats-pill">—</div>
  </div>
</div>

<div id="tabbar" class="hidden">
  <button class="tab-btn active" data-tab="conv">
    💬 Conversations <span class="tab-badge" id="tab-conv-count">0</span>
  </button>
  <button class="tab-btn" data-tab="images">
    🖼 Images <span class="tab-badge" id="tab-img-count">0</span>
  </button>
  <button class="tab-btn" data-tab="trash">
    🗑 Trash <span class="tab-badge" id="tab-trash-count">0</span>
  </button>
</div>

<div id="shell" class="hidden">
  <div id="conv-tab">
    <div id="sidebar">
      <div id="sidebar-hd"><span id="conv-count">0 conversations</span></div>
      <div id="conv-list"></div>
    </div>
    <div id="reader">
      <div id="reader-empty">
        <div class="big">✦</div>
        <div style="font-family:'DM Mono',monospace;font-size:12px;color:var(--text3)">select a conversation</div>
      </div>
      <div id="reader-hd" class="hidden">
        <div id="reader-hd-inner">
          <h2 id="rdr-title"></h2>
          <div id="reader-meta">
            <span><b id="rdr-date"></b></span>
            <span><b id="rdr-model"></b></span>
            <span id="rdr-msgs" style="color:var(--text3)"></span>
          </div>
        </div>
      </div>
      <div id="messages" class="hidden"></div>
    </div>
  </div>

  <div id="images-tab">
    <div id="img-controls">
      <div id="img-filter-btns">
        <button class="img-filter-btn active" data-type="all">All</button>
        <button class="img-filter-btn" data-type="uploaded">Uploaded</button>
        <button class="img-filter-btn" data-type="generated">Generated</button>
      </div>
      <input id="img-search" type="text" placeholder="filter by filename…" autocomplete="off">
      <span id="img-count"></span>
      <div id="img-pagination">
        <button class="pg-btn" id="pg-first" onclick="goPage(0)">«</button>
        <button class="pg-btn" id="pg-prev"  onclick="goPage(imgPage-1)">‹</button>
        <span id="pg-info"></span>
        <button class="pg-btn" id="pg-next"  onclick="goPage(imgPage+1)">›</button>
        <button class="pg-btn" id="pg-last"  onclick="goPage(imgLastPage())">»</button>
      </div>
    </div>
    <div id="img-grid"></div>
  </div>
  <div id="trash-tab">
    <div id="trash-controls">
      <div id="trash-section-btns">
        <button class="trash-section-btn active" data-section="conversations">Conversations</button>
        <button class="trash-section-btn" data-section="images">Images</button>
      </div>
      <span id="trash-count">0 items</span>
      <button id="empty-trash-btn" onclick="emptyTrash()">💥 Empty Trash</button>
    </div>
    <div id="trash-list"></div>
  </div>
</div>

<div id="lightbox">
  <button id="lightbox-close">✕</button>
  <img id="lb-img" src="" alt="">
  <div id="lightbox-info"></div>
  <div id="lightbox-nav">
    <button class="lb-nav-btn" id="lb-prev">← prev</button>
    <a id="lb-open-btn" href="#" target="_blank">⬡ open file</a>
    <button class="lb-nav-btn" id="lb-next">next →</button>
  </div>
</div>

<script>
// ── Configure marked + hljs ───────────────────────────────────────────────────
const renderer = new marked.Renderer();
renderer.code = function(code, lang) {
  const validLang = lang && hljs.getLanguage(lang) ? lang : null;
  let highlighted;
  try {
    highlighted = validLang
      ? hljs.highlight(code, { language: validLang }).value
      : hljs.highlightAuto(code).value;
  } catch(e) { highlighted = escHtml(code); }
  const id = 'cb' + Math.random().toString(36).slice(2,8);
  const label = validLang || '';
  return `<pre><div class="code-header"><span>${label}</span><button class="copy-btn" onclick="copyCode(this,'${id}')">copy</button></div><code id="${id}" class="hljs">${highlighted}</code></pre>`;
};
renderer.link = (href, title, text) =>
  `<a href="${href}" target="_blank" rel="noopener"${title?` title="${title}"`:''}>${text}</a>`;
marked.use({ renderer, breaks: true, gfm: true });

function copyCode(btn, id) {
  const el = document.getElementById(id);
  if (!el) return;
  navigator.clipboard.writeText(el.textContent).then(() => {
    btn.textContent = 'copied!'; btn.classList.add('copied');
    setTimeout(() => { btn.textContent = 'copy'; btn.classList.remove('copied'); }, 1800);
  });
}

// ── State ─────────────────────────────────────────────────────────────────────
let allConversations = [], filteredConversations = [], activeConvId = null;
let allImages = [], filteredImages = [], lightboxIndex = 0;

// ── Trash ────────────────────────────────────────────────────────────────────
let trashSection = 'conversations';
let trashData    = { conversations: [], images: [] };

async function trashConversation(id, btn) {
  if (!confirm('Move this conversation to trash?')) return;
  const res = await fetch(`/api/conversation/${encodeURIComponent(id)}`, { method: 'DELETE' });
  const d = await res.json();
  if (d.ok) {
    allConversations = allConversations.filter(c => c.id !== id);
    if (activeConvId === id) {
      activeConvId = null;
      document.getElementById('reader-empty').classList.remove('hidden');
      document.getElementById('reader-hd').classList.add('hidden');
      document.getElementById('messages').classList.add('hidden');
    }
    applyFilters();
    document.getElementById('tab-trash-count').textContent =
      (+document.getElementById('tab-trash-count').textContent || 0) + 1;
  } else { alert('Error: ' + d.error); }
}

async function trashMessage(convId, nodeId, btn) {
  if (!confirm('Delete this message? It will be moved to trash.')) return;
  const res = await fetch(`/api/message/${encodeURIComponent(convId)}/${encodeURIComponent(nodeId)}`, { method: 'DELETE' });
  const d = await res.json();
  if (d.ok) {
    btn.closest('.msg-block').remove();
  } else { alert('Error: ' + d.error); }
}

async function trashImage(imgId, btn) {
  if (!confirm('Move this image to trash?')) return;
  const res = await fetch(`/api/image/${imgId}`, { method: 'DELETE' });
  const d = await res.json();
  if (d.ok) {
    allImages = allImages.filter(i => i.id !== imgId);
    const activeType = document.querySelector('.img-filter-btn.active')?.dataset.type || 'all';
    renderImages(activeType, document.getElementById('img-search').value);
    document.getElementById('tab-img-count').textContent =
      Math.max(0, (+document.getElementById('tab-img-count').textContent || 1) - 1);
    document.getElementById('tab-trash-count').textContent =
      (+document.getElementById('tab-trash-count').textContent || 0) + 1;
  } else { alert('Error: ' + d.error); }
}

async function loadTrash() {
  const res = await fetch('/api/trash');
  trashData = await res.json();
  document.getElementById('tab-trash-count').textContent = trashData.total || 0;
  document.getElementById('trash-count').textContent = trashData.total + ' items';
  renderTrash();
}

function renderTrash() {
  const list = document.getElementById('trash-list');
  list.innerHTML = '';
  const items = trashSection === 'conversations' ? trashData.conversations : trashData.images;
  if (!items.length) {
    list.innerHTML = `<div class="trash-empty-msg">Nothing here${trashSection === 'conversations' ? ' — no trashed conversations' : ' — no trashed images'}.</div>`;
    return;
  }
  if (trashSection === 'conversations') {
    items.forEach(c => {
      const el = document.createElement('div');
      el.className = 'trash-item';
      el.innerHTML = `
        <div class="trash-item-info">
          <div class="trash-item-title">${escHtml(c.title)}</div>
          <div class="trash-item-meta">${c.create_time ? fmtDate(c.create_time) : ''}</div>
        </div>
        <button class="restore-btn" onclick="restoreConversation('${escHtml(c.trash_file)}')">♻ Restore</button>
      `;
      list.appendChild(el);
    });
  } else {
    items.forEach(img => {
      const el = document.createElement('div');
      el.className = 'trash-item';
      el.innerHTML = `
        <img src="/img/trash/${encodeURIComponent(img.trash_file)}" onerror="this.style.display='none'">
        <div class="trash-item-info">
          <div class="trash-item-title">${escHtml(img.name)}</div>
          <div class="trash-item-meta">trashed image</div>
        </div>
        <button class="restore-btn" onclick="restoreImage('${escHtml(img.trash_file)}')">♻ Restore</button>
      `;
      list.appendChild(el);
    });
  }
}

async function restoreConversation(trashFile) {
  const res = await fetch('/api/trash/restore/conversation', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ trash_file: trashFile })
  });
  const d = await res.json();
  if (d.ok) {
    await loadTrash();
    // Reload conversation index
    const convRes = await fetch('/api/conversations');
    allConversations = await convRes.json();
    applyFilters();
  } else { alert('Error: ' + d.error); }
}

async function restoreImage(trashFile) {
  const res = await fetch('/api/trash/restore/image', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ trash_file: trashFile })
  });
  const d = await res.json();
  if (d.ok) { await loadTrash(); }
  else { alert('Error: ' + d.error); }
}

async function emptyTrash() {
  const t = trashData.total || 0;
  if (!t) { alert('Trash is already empty.'); return; }
  if (!confirm(`Permanently delete ${t} item${t !== 1 ? 's' : ''}? This cannot be undone.`)) return;
  const res = await fetch('/api/trash/empty', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
  const d = await res.json();
  if (d.ok) {
    trashData = { conversations: [], images: [], total: 0 };
    document.getElementById('tab-trash-count').textContent = '0';
    document.getElementById('trash-count').textContent = '0 items';
    renderTrash();
  } else { alert('Error: ' + d.error); }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
async function boot() {
  const [convRes, imgRes, statsRes] = await Promise.all([
    fetch('/api/conversations'),
    fetch('/api/images'),
    fetch('/api/stats'),
  ]);
  allConversations = await convRes.json();  // lean index only
  allImages        = await imgRes.json();
  const stats      = await statsRes.json();

  document.getElementById('loading').classList.add('hidden');
  document.getElementById('topbar').classList.remove('hidden');
  document.getElementById('tabbar').classList.remove('hidden');
  document.getElementById('shell').classList.remove('hidden');

  document.getElementById('tab-conv-count').textContent = stats.conversations.toLocaleString();
  document.getElementById('tab-img-count').textContent  = stats.images.toLocaleString();
  document.getElementById('logo-sub').textContent       = stats.conversations.toLocaleString() + ' convos';

  buildFilters(stats);
  applyFilters();
  renderImages('all', '');

  document.getElementById('search').addEventListener('input', debounce(applyFilters, 180));
  document.getElementById('model-filter').addEventListener('change', applyFilters);
  document.getElementById('year-filter').addEventListener('change', applyFilters);
  document.getElementById('sort-filter').addEventListener('change', applyFilters);
  document.querySelectorAll('.tab-btn').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));
  document.querySelectorAll('.img-filter-btn').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('.img-filter-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    renderImages(b.dataset.type, document.getElementById('img-search').value);
  }));
  document.getElementById('img-search').addEventListener('input', debounce(() => {
    const active = document.querySelector('.img-filter-btn.active');
    renderImages(active ? active.dataset.type : 'all', document.getElementById('img-search').value);
  }, 200));

  document.querySelectorAll('.trash-section-btn').forEach(b => b.addEventListener('click', () => {
    document.querySelectorAll('.trash-section-btn').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    trashSection = b.dataset.section;
    renderTrash();
  }));

  // Load initial trash count
  fetch('/api/trash').then(r => r.json()).then(d => {
    document.getElementById('tab-trash-count').textContent = d.total || 0;
  });

  // Theme picker — persist to localStorage
  const themePicker = document.getElementById('theme-picker');
  const savedTheme = localStorage.getItem('archive-theme') || '';
  themePicker.value = savedTheme;
  applyTheme(savedTheme);
  themePicker.addEventListener('change', () => {
    applyTheme(themePicker.value);
    localStorage.setItem('archive-theme', themePicker.value);
  });

  // Keyboard shortcuts
  document.addEventListener('keydown', e => {
    // Ctrl/Cmd+K -> focus search
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      const s = document.getElementById('search');
      s.focus(); s.select();
    }
    // Escape -> clear search
    if (e.key === 'Escape' && document.activeElement === document.getElementById('search')) {
      document.getElementById('search').value = '';
      applyFilters();
    }
  });

  document.getElementById('lightbox').addEventListener('click', e => {
    if (e.target.id === 'lightbox') closeLightbox();
  });
  document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
  document.getElementById('lb-prev').addEventListener('click', e => { e.stopPropagation(); lightboxNav(-1); });
  document.getElementById('lb-next').addEventListener('click', e => { e.stopPropagation(); lightboxNav(1); });
  document.addEventListener('keydown', e => {
    if (!document.getElementById('lightbox').classList.contains('open')) return;
    if (e.key === 'Escape') closeLightbox();
    if (e.key === 'ArrowLeft')  lightboxNav(-1);
    if (e.key === 'ArrowRight') lightboxNav(1);
  });
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  document.getElementById('conv-tab').style.display = tab === 'conv' ? 'flex' : 'none';
  document.getElementById('images-tab').classList.toggle('active', tab === 'images');
  document.getElementById('trash-tab').classList.toggle('active', tab === 'trash');
  const vis = tab === 'conv' ? 'visible' : 'hidden';
  document.getElementById('search-wrap').style.visibility = vis;
  document.querySelector('.bar-right').style.visibility   = vis;
  if (tab === 'trash') loadTrash();
}

// ── Filters ───────────────────────────────────────────────────────────────────
function buildFilters(stats) {
  const ms = document.getElementById('model-filter');
  Object.keys(stats.models).sort().forEach(m => ms.appendChild(new Option(m, m)));
  const ys = document.getElementById('year-filter');
  Object.keys(stats.years).forEach(y => ys.appendChild(new Option(y, y)));
}

function applyFilters() {
  const q     = (document.getElementById('search').value || '').trim().toLowerCase();
  const model = document.getElementById('model-filter').value;
  const year  = document.getElementById('year-filter').value;
  const sort  = document.getElementById('sort-filter').value;

  filteredConversations = allConversations.filter(c => {
    if (model && c.default_model_slug !== model) return false;
    if (year && c.create_time && new Date(c.create_time * 1000).getFullYear().toString() !== year) return false;
    if (q) {
      const inTitle = (c.title || '').toLowerCase().includes(q);
      const inMsgs  = getMsgTexts(c).some(t => t.toLowerCase().includes(q));
      if (!inTitle && !inMsgs) return false;
    }
    return true;
  });
  filteredConversations.sort((a,b) => sort === 'asc'
    ? (a.create_time||0) - (b.create_time||0)
    : (b.create_time||0) - (a.create_time||0));
  document.getElementById('stats-pill').textContent = filteredConversations.length.toLocaleString() + ' conversations';
  renderList(q);
}

function getMsgTexts(conv) {
  // Index items have a preview string; full conv objects have mapping
  if (conv.preview) return [conv.preview];
  if (!conv.mapping) return [];
  return Object.values(conv.mapping).map(n =>
    (n.message?.content?.parts || []).filter(p => typeof p === 'string').join(' ')
  ).filter(Boolean);
}

// ── List ──────────────────────────────────────────────────────────────────────
function renderList(q = '') {
  const list = document.getElementById('conv-list');
  document.getElementById('conv-count').textContent = filteredConversations.length.toLocaleString() + ' conversations';
  if (!filteredConversations.length) { list.innerHTML = '<div class="no-results">no results</div>'; return; }
  const frag = document.createDocumentFragment();
  filteredConversations.forEach(c => {
    const el = document.createElement('div');
    el.className = 'conv-item' + (c.id === activeConvId ? ' active' : '');
    el.dataset.id = c.id;
    const model = c.model || c.default_model_slug || '';
    const mcls = /o[13]/.test(model) ? 'mo' : (model.includes('4') || model.includes('5')) ? 'm4' : '';
    const sm = model.replace('gpt-','').replace('-latest','').replace('-preview','');
    const preview = c.preview || '';
    el.innerHTML = `
      <div class="ci-title">${hl(escHtml(c.title || 'Untitled'), q)}</div>
      <div class="ci-meta">
        <span class="ci-date">${c.create_time ? fmtDate(c.create_time) : ''}</span>
        ${model ? `<span class="ci-model ${mcls}">${sm}</span>` : ''}
      </div>
      ${preview ? `<div class="ci-preview">${hl(escHtml(preview), q)}</div>` : ''}
      <button class="del-btn" title="Move to trash" onclick="event.stopPropagation();trashConversation('${c.id}',this)">🗑</button>
    `;
    el.addEventListener('click', () => openConv(c.id));
    frag.appendChild(el);
  });
  list.innerHTML = '';
  list.appendChild(frag);
}

// ── Reader ────────────────────────────────────────────────────────────────────
async function openConv(id) {
  activeConvId = id;
  document.querySelectorAll('.conv-item').forEach(el => el.classList.toggle('active', el.dataset.id === id));

  // Show loading state in reader
  const meta = allConversations.find(c => c.id === id);
  document.getElementById('reader-empty').classList.add('hidden');
  document.getElementById('reader-hd').classList.remove('hidden');
  document.getElementById('messages').classList.remove('hidden');
  document.getElementById('rdr-title').textContent  = meta?.title || 'Loading…';
  document.getElementById('rdr-date').textContent   = '';
  document.getElementById('rdr-model').textContent  = '';
  document.getElementById('rdr-msgs').textContent   = '';
  document.getElementById('messages').innerHTML     = '<div style="padding:32px;font-family:var(--font-mono);font-size:12px;color:var(--text3)">Loading…</div>';

  // Fetch full conversation
  const res  = await fetch(`/api/conversation/${encodeURIComponent(id)}`);
  if (!res.ok) { document.getElementById('messages').innerHTML = '<div style="padding:32px;color:var(--rose)">Failed to load conversation.</div>'; return; }
  const conv = await res.json();

  const msgs = buildChain(conv);
  const q    = (document.getElementById('search').value || '').trim().toLowerCase();

  document.getElementById('rdr-title').textContent  = conv.title || 'Untitled';
  document.getElementById('rdr-date').textContent   = conv.create_time ? '📅 ' + fmtDate(conv.create_time, true) : '';
  document.getElementById('rdr-model').textContent  = conv.default_model_slug ? '🤖 ' + conv.default_model_slug : '';
  document.getElementById('rdr-msgs').textContent   = `💬 ${msgs.length} messages`;

  const messagesEl = document.getElementById('messages');
  messagesEl.innerHTML = '<div id="messages-inner"></div>';
  const container = document.getElementById('messages-inner');
  msgs.forEach(msg => {
    const role = msg.author?.role || '';
    if (role === 'system') return;
    const parts = msg.content?.parts || [];
    if (!parts.length) return;
    const bodyHtml = renderParts(parts, q);
    if (!bodyHtml.trim()) return;
    const block = document.createElement('div');
    // Tool image messages — show ChatGPT label so it's clear who sent it
    if (role === 'tool') {
      block.className = 'msg-block role-tool';
      block.innerHTML = `
        <div class="msg-role-row">
          <span class="role-badge assistant">ChatGPT</span>
          <span class="role-line"></span>
        </div>
        <div class="msg-body">${bodyHtml}</div>
      `;
    } else {
      block.className = `msg-block role-${role}`;
      const roleLabel = role === 'user' ? 'You' : role === 'assistant' ? 'ChatGPT' : role;
      const ts = msg.create_time ? `<span class="msg-ts">${fmtDate(msg.create_time, true)}</span>` : '';
      const nodeId = msg.id || '';
      block.innerHTML = `
        <div class="msg-role-row">
          <span class="role-badge ${role}">${roleLabel}</span>
          ${ts}<span class="role-line"></span>
        </div>
        <div class="msg-body">${bodyHtml}</div>
        ${nodeId ? `<button class="del-btn" title="Delete message" onclick="trashMessage('${activeConvId}','${nodeId}',this)">🗑</button>` : ''}
      `;
    }
    container.appendChild(block);
  });
  messagesEl.scrollTop = 0;
}

function buildChain(conv) {
  if (!conv.mapping) return [];
  const mapping = conv.mapping;

  // Walk backwards from current_node -> root to get the canonical branch,
  // then reverse. This preserves correct order even when create_time is 0
  // (which happens on tool/image nodes).
  let nid = conv.current_node;
  const chain = [];
  const visited = new Set();
  while (nid && !visited.has(nid)) {
    visited.add(nid);
    const node = mapping[nid];
    if (!node) break;
    const msg  = node.message;
    const role = msg?.author?.role;
    if (role && role !== 'system' && msg?.content) {
      // Keep tool messages only if they have image pointers
      if (role === 'tool') {
        const parts = msg.content?.parts || [];
        if (parts.some(p => p?.content_type === 'image_asset_pointer')) {
          chain.push(msg);
        }
      } else {
        chain.push(msg);
      }
    }
    nid = node.parent;
  }
  return chain.reverse();
}

function renderParts(parts, q = '') {
  let out = '';
  parts.forEach(p => {
    if (typeof p === 'string' && p.trim()) {
      const trimmed = p.trim();
      // Skip raw JSON tool blobs (image_group, carousel, search params etc.)
      if ((trimmed.startsWith('{') || trimmed.startsWith('[')) &&
          /"(query|layout|carousel|image_group|image_search|num_per_query|aspect_ratio)"/.test(trimmed)) {
        return;
      }
      // Skip thetool text that starts with image_group[ or similar
      if (/^\s*(\u25a0?image_group|\u25a0?image_search)/.test(trimmed)) {
        return;
      }
      let rendered = marked.parse(trimmed);
      if (q) rendered = hl(rendered, q);
      out += rendered;
    } else if (p?.content_type === 'image_asset_pointer') {
      const ptr = p.asset_pointer || '';
      const url = `/img-by-id/${encodeURIComponent(ptr)}`;
      const safePtr = escHtml(ptr);
      out += `<div class="img-wrap" data-ptr="${safePtr}"><img src="${url}" alt="image" loading="lazy" onclick="openLightboxByUrl('${url}')" onerror="showImgDebug(this)"></div>`;
    }
  });
  return out;
}

// ── Images tab (pagination) ───────────────────────────────────────────────────
const PAGE_SIZE = 120;
let imgPage = 0;

function imgLastPage() {
  return Math.max(0, Math.ceil(filteredImages.length / PAGE_SIZE) - 1);
}

function renderImages(typeFilter, nameFilter) {
  const name = (nameFilter || '').toLowerCase();
  filteredImages = allImages.filter(i => {
    if (typeFilter && typeFilter !== 'all' && i.type !== typeFilter) return false;
    if (name && !i.name.toLowerCase().includes(name)) return false;
    return true;
  });
  imgPage = 0;
  document.getElementById('img-count').textContent = filteredImages.length.toLocaleString() + ' images';
  renderPage();
}

function goPage(n) {
  const last = imgLastPage();
  imgPage = Math.max(0, Math.min(n, last));
  renderPage();
  document.getElementById('img-grid').scrollTop = 0;
}

function renderPage() {
  const grid = document.getElementById('img-grid');
  grid.innerHTML = '';

  if (!filteredImages.length) {
    grid.innerHTML = '<div class="img-no-files">No images match the current filter.</div>';
    updatePagination();
    return;
  }

  const start = imgPage * PAGE_SIZE;
  const end   = Math.min(start + PAGE_SIZE, filteredImages.length);
  const frag  = document.createDocumentFragment();

  for (let i = start; i < end; i++) {
    const img = filteredImages[i];
    const idx = i;
    const card = document.createElement('div');
    card.className = 'img-card';
    card.innerHTML = `
      <img src="/img/${img.id}" alt="${escHtml(img.name)}" loading="lazy"
        onerror="this.style.opacity='.15'">
      <span class="img-card-type ${img.type}">${img.type === 'uploaded' ? 'upload' : 'gen'}</span>
      <button class="del-btn" title="Move to trash" onclick="event.stopPropagation();trashImage(${img.id},this)">🗑</button>
    `;
    card.addEventListener('click', () => openLightbox(idx));
    frag.appendChild(card);
  }

  grid.appendChild(frag);
  updatePagination();
}

function updatePagination() {
  const last   = imgLastPage();
  const total  = Math.ceil(filteredImages.length / PAGE_SIZE);
  const show   = filteredImages.length > 0;
  document.getElementById('img-pagination').style.display = show ? 'flex' : 'none';
  document.getElementById('pg-info').textContent = show ? `${imgPage + 1} / ${total}` : '';
  document.getElementById('pg-first').disabled = imgPage === 0;
  document.getElementById('pg-prev').disabled  = imgPage === 0;
  document.getElementById('pg-next').disabled  = imgPage >= last;
  document.getElementById('pg-last').disabled  = imgPage >= last;
}

// ── Lightbox ──────────────────────────────────────────────────────────────────
function openLightbox(idx) {
  lightboxIndex = idx; showLightboxFrame();
  document.getElementById('lightbox').classList.add('open');
}
function openLightboxByUrl(url) {
  const idx = filteredImages.findIndex(i => `/img/${i.id}` === url);
  if (idx >= 0) { openLightbox(idx); return; }
  document.getElementById('lb-img').src = url;
  document.getElementById('lightbox-info').textContent = '';
  document.getElementById('lightbox-nav').style.visibility = 'hidden';
  document.getElementById('lightbox').classList.add('open');
}
function showLightboxFrame() {
  const img = filteredImages[lightboxIndex];
  if (!img) return;
  const src = `/img/${img.id}`;
  document.getElementById('lb-img').src = src;
  document.getElementById('lb-open-btn').href = src;
  document.getElementById('lightbox-nav').style.visibility = '';
  document.getElementById('lightbox-info').textContent =
    `${img.name}  ·  ${img.type}  ·  ${lightboxIndex + 1} / ${filteredImages.length}`;
}
function lightboxNav(dir) {
  lightboxIndex = (lightboxIndex + dir + filteredImages.length) % filteredImages.length;
  showLightboxFrame();
}
function closeLightbox() {
  document.getElementById('lightbox').classList.remove('open');
  document.getElementById('lb-img').src = '';
}

// ── Utils ─────────────────────────────────────────────────────────────────────
function showImgDebug(img) {
  const wrap = img.parentElement;
  const ptr  = wrap ? (wrap.dataset.ptr || '') : '';
  const clean = ptr.replace(/^(sediment|file-service):\/\//, '');
  const el = document.createElement('div');
  el.className = 'img-debug';
  el.innerHTML = `
    <span class="id-label">🖼 not in export</span>
    <span class="id-val">${escHtml(clean)}</span>
    <span class="id-hint">click to copy pointer ID</span>
  `;
  el.addEventListener('click', () => {
    navigator.clipboard.writeText(ptr).then(() => {
      el.classList.add('copied');
      el.querySelector('.id-hint').textContent = '✓ copied to clipboard';
      setTimeout(() => {
        el.classList.remove('copied');
        el.querySelector('.id-hint').textContent = 'click to copy pointer ID';
      }, 2000);
    });
  });
  wrap.replaceWith(el);
}

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function hl(html, q) {
  if (!q || q.length < 2) return html;
  try { return html.replace(new RegExp(`(${q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')})`, 'gi'), '<mark>$1</mark>'); }
  catch { return html; }
}
function fmtDate(ts, long = false) {
  const d = new Date(ts * 1000);
  return long
    ? d.toLocaleDateString('en-US',{year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})
    : d.toLocaleDateString('en-US',{year:'numeric',month:'short',day:'numeric'});
}
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(()=>fn(...a), ms); }; }

boot();
</script>
</body>
</html>
"""

# ── HTTP Server ───────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def send_json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        try:
            path.resolve().relative_to(EXPORT_DIR.resolve())
        except ValueError:
            self.send_error(403)
            return
        mime, _ = mimetypes.guess_type(str(path))
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        p = parsed.path
        try:
            if p.startswith("/api/conversation/"):
                cid = unquote(p[18:])
                result = trash_conversation(cid)
            elif p.startswith("/api/message/"):
                # /api/message/{cid}/{node_id}
                parts = unquote(p[13:]).split("/", 1)
                if len(parts) == 2:
                    result = trash_message(parts[0], parts[1])
                else:
                    result = {"ok": False, "error": "bad path"}
            elif p.startswith("/api/image/"):
                try:
                    img_id = int(p[11:])
                    result = trash_image(img_id)
                except ValueError:
                    result = {"ok": False, "error": "bad id"}
            else:
                self.send_error(404)
                return
            self.send_json(result)
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def do_POST(self):
        parsed = urlparse(self.path)
        p = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        try:
            if p == "/api/trash/restore/conversation":
                result = restore_conversation(body.get("trash_file", ""))
            elif p == "/api/trash/restore/image":
                result = restore_image(body.get("trash_file", ""))
            elif p == "/api/trash/empty":
                result = empty_trash()
            else:
                self.send_error(404)
                return
            self.send_json(result)
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path

        if p in ("/", "/index.html"):
            self.send_html(HTML)
        elif p == "/api/conversations":
            self.send_json(CONV_INDEX)          # lightweight index only
        elif p == "/api/images":
            self.send_json(ALL_IMAGES)
        elif p == "/api/stats":
            self.send_json(STATS)
        elif p.startswith("/api/conversation/"):
            cid = unquote(p[18:])               # full conversation on demand
            conv = CONV_LOOKUP.get(cid)
            if conv:
                self.send_json(conv)
            else:
                self.send_error(404)
        elif p.startswith("/img/"):
            # /img/{numeric_id}
            try:
                img_id = int(p[5:])
                path = IMAGE_BY_ID.get(img_id)
                if path:
                    self.send_file(path)
                else:
                    self.send_error(404)
            except (ValueError, IndexError):
                self.send_error(400)
        elif p.startswith("/img-by-id/"):
            raw = unquote(p[11:])
            file_id = re.sub(r"^(sediment|file-service)://", "", raw)
            img_id = FILE_ID_MAP.get(file_id)
            if img_id is not None:
                path = IMAGE_BY_ID.get(img_id)
                if path:
                    self.send_file(path)
                    return
                else:
                    print(f"  ⚠ img_id {img_id} not in IMAGE_BY_ID (file moved/deleted?)")
            else:
                print(f"  ⚠ no map entry for: {file_id[:50]}")
            self.send_error(404)
        elif p == "/api/trash":
            self.send_json(list_trash())
        elif p.startswith("/img/trash/"):
            # Serve trashed image preview
            fname = unquote(p[11:])
            tp = TRASH_DIR / "images" / Path(fname).name
            self.send_file(tp)
        else:
            self.send_error(404)


def find_free_port(start=8765):
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("localhost", port)) != 0:
                return port
    return start


def main():
    port = find_free_port()
    url  = f"http://localhost:{port}"
    server = HTTPServer(("localhost", port), Handler)

    print(f"\n🚀 ChatGPT Archive Viewer → {url}")
    print(f"   {STATS['conversations']:,} conversations  ·  {STATS['images']:,} images")
    print("   Press Ctrl+C to stop.\n")

    threading.Thread(
        target=lambda: (time.sleep(0.6), webbrowser.open(url)),
        daemon=True
    ).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
