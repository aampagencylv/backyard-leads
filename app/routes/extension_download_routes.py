"""Chrome extension download — serves a zipped copy of the
`chrome-extension/` folder from the repo so the BDR team can install
without SSH access or git clone.

Approach: build the zip in-memory on each request from the on-disk
folder. The extension is small (~50KB), and rebuilding lets us ship
edits without a separate build step — push to main + deploy + the
served zip reflects the new version automatically.

Auth: any signed-in user can download. The download itself contains
no secrets — credentials live in chrome.storage.local after the user
signs in via the popup.

Versioning: GET /integrations/extension/version returns the
manifest's version string so the platform UI can show 'You're on
1.0.0' and 'Latest is 1.0.1 — reinstall to get it'.
"""
from __future__ import annotations
import json
import io
import logging
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from app.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/integrations/extension", tags=["extension-download"])
log = logging.getLogger("bmp.extension_download")


def _extension_dir() -> Path:
    """Locate chrome-extension/ relative to the app root. Lives at the
    repo top-level on the VPS at /opt/backyard-leads/chrome-extension."""
    # app/routes/extension_download_routes.py -> ../../chrome-extension
    return Path(__file__).resolve().parent.parent.parent / "chrome-extension"


def _read_manifest() -> dict:
    """Pull the manifest JSON so we can echo the version."""
    mf = _extension_dir() / "manifest.json"
    if not mf.exists():
        return {}
    try:
        return json.loads(mf.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"manifest parse failed: {e}")
        return {}


@router.get("/version")
async def extension_version(
    _user: User = Depends(get_current_user),
) -> dict:
    """Manifest metadata — UI shows the current shipped version."""
    mf = _read_manifest()
    return {
        "version": mf.get("version") or "0.0.0",
        "name": mf.get("name") or "Prospector CRM",
        "description": mf.get("description") or "",
    }


@router.get("/download")
async def extension_download(
    _user: User = Depends(get_current_user),
):
    """Build + serve a zip of the chrome-extension/ folder. Streamed
    inline as application/zip so the browser pops a save dialog."""
    ext_dir = _extension_dir()
    if not ext_dir.exists() or not ext_dir.is_dir():
        raise HTTPException(status_code=500, detail="extension folder missing on the server")

    manifest = _read_manifest()
    version = manifest.get("version") or "0.0.0"

    # Build the zip in memory. The extension folder is small enough that
    # building per-request is fine — under 100KB and a sub-millisecond
    # operation on the VPS disk.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(ext_dir.rglob("*")):
            if not p.is_file():
                continue
            # Skip mac metadata / hidden cruft
            if any(part.startswith(".") for part in p.relative_to(ext_dir).parts):
                continue
            if p.name in ("README.md",):
                # Skip the side-load docs from the zip — they're for repo readers,
                # not for BDRs who got the download
                continue
            arcname = "prospector-crm/" + str(p.relative_to(ext_dir))
            zf.write(p, arcname=arcname)

    data = buf.getvalue()
    fname = f"prospector-crm-v{version}.zip"
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store",
        },
    )
