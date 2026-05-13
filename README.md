# ChatGPT Archive Viewer

A fast, local viewer for your ChatGPT data export. **Zero installs beyond Python** — no pip, no npm, no dependencies. Just run one command and your browser opens automatically.

---

## Quick Start

1. **Export your data** — ChatGPT → Settings → Data Controls → Export Data. Unzip the file you receive.

2. **Drop `app.py`** into your unzipped export folder.

3. **Run it:**
   ```
   python app.py
   ```
   Your browser opens automatically at `http://localhost:8765`.

4. Press `Ctrl+C` in the terminal to stop.

That's it. No `pip install` needed — uses only Python's standard library.

---

## Features

- 💬 **Conversation browser** — searchable, filterable sidebar with title, date, model, and preview
- 🔍 **Full-text search** — searches both titles and message content instantly (`Ctrl+K` to focus)
- 📅 **Filter by model and year** — find GPT-4o vs o1 conversations, sort newest or oldest first
- 🖼 **Image gallery** — all uploaded and AI-generated images in a paginated grid with lightbox viewer
- 🔗 **Inline images** — images appear inside conversations right where they were used, resolved via `export_manifest.json`
- 🗑 **Trash system** — delete conversations, messages, or images; restore or permanently delete from a Trash tab
- 🎨 **Themes** — Dark, Nord, Slate, Mocha, Arctic, Terminal
- ✨ **Markdown rendering** — bold, italics, headers, tables, code blocks with syntax highlighting and copy button
- ⌨️ **Keyboard shortcuts** — `Ctrl+K` to search, arrow keys in lightbox, `Escape` to close

---

## Requirements

- **Python 3.8 or newer** — that's it.
- Works on Windows, macOS, and Linux.
- Internet connection on first load only (for fonts and syntax highlighting CDN).

**Check your Python version:**
```
python --version
```

If you don't have Python, get it at [python.org](https://python.org). On Windows, check **"Add Python to PATH"** during install.

---

## Export Folder Structure

Your unzipped ChatGPT export will look like this — `app.py` handles all of it automatically:

```
📁 your-export/
  app.py                           ← drop it here
  export_manifest.json             ← used for accurate image resolution
  conversations-000.json
  conversations-001.json
  ...
  file-abc123-image.png            ← images you uploaded to ChatGPT
  file_000000001a2b3c4d-uuid.png   ← AI-generated images
  📁 user-XXXXXXXXX/              ← more AI-generated images
  📁 dalle-generations/            ← DALL-E generation history
```

You can also pass the export folder as an argument if you want to keep `app.py` elsewhere:
```
python app.py /path/to/export/folder
```

---

## Image Resolution

The app uses `export_manifest.json` (included in your ChatGPT export) to accurately map conversation image references to their files on disk. This handles all the different pointer formats ChatGPT uses internally (`sediment://`, `file-service://`) and resolves them to the correct file regardless of which subfolder it lives in.

Images that show as **"not in export"** were referenced in conversations but not included by OpenAI in the export — this is an OpenAI limitation, not a bug. On startup the app will print a list of any unresolvable image pointers so you know exactly what's missing.

---

## Trash

Deleted items are moved to a `.trash/` folder inside your export directory — nothing is permanently deleted until you click **Empty Trash**. You can restore individual conversations or images from the Trash tab at any time.

---

## Privacy

Everything runs locally. The server only listens on `localhost` — nothing is accessible from outside your computer and nothing is uploaded anywhere. Your conversation data never leaves your machine.

---

## Contributing

PRs welcome. Ideas:
- [ ] Export conversations to Markdown or PDF
- [ ] Usage stats dashboard (messages per day, model breakdown over time)
- [ ] Bookmark / star conversations
- [ ] Full-text search index for faster search on large exports

---

## License

MIT
