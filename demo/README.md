# Demo characters

Curated showcase output for the GitHub README and docs. **Tracked in git.**

Local work-in-progress stays under `output/` (gitignored). When a character is ready to show publicly, copy it here:

```bash
# Example: promote a finished character into the demo gallery
cp -r output/characters/samurai demo/samurai
```

On Windows (PowerShell):

```powershell
Copy-Item -Recurse -Force output\characters\samurai demo\samurai
```

## What to include

Prefer a complete character folder so others can inspect plans and layers:

- `base/` — armature copy
- `design/` — `plan.json`, `layers/`, `compose_preview.png` (and scaled/grid if useful)
- `anims/<name>/` — `plan.json`, `frame_00..07.png`, `*_preview.gif`, `*_contact_sheet.png`

Optional / heavy (omit if you want a smaller repo):

- `anims/*/draft/` — iteration drafts
- `design/refs/` — paint alignment refs

## README gallery

After adding `demo/<name>/`, link the compose preview and animation GIFs from the root [README.md](../README.md) **Demo characters** section.
