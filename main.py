"""
Invoice OCR Auto-Renamer — main.py
===================================
mode1 : All pages have barcode. Same barcode value → merge.
mode2 : First page has barcode, trailing pages (no barcode) always appended.
"""

import json
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Logging ───────────────────────────────────────────────────────────────────
_BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
LOG_PATH  = _BASE_DIR / "log.txt"

_handlers = [logging.FileHandler(LOG_PATH, mode="a", encoding="utf-8")]
if not getattr(sys, "frozen", False):
    _handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)
log = logging.getLogger(__name__)


# ── Suppress poppler console windows (frozen exe only) ─────────────────────────
if sys.platform == "win32" and getattr(sys, "frozen", False):
    import subprocess
    _orig_popen_init = subprocess.Popen.__init__
    def _no_console_popen_init(self, *args, **kwargs):
        si = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = si
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        _orig_popen_init(self, *args, **kwargs)
    subprocess.Popen.__init__ = _no_console_popen_init


# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "mode": "mode2",
    "paths": {
        "inbox":   "C:\\Scans\\Inbox",
        "output":  "C:\\Scans\\Output",
        "failed":  "C:\\Scans\\Failed",
        "archive": "C:\\Scans\\Archive",
    },
    "filename": {
        "prefix": "",
        "format": "{number}.pdf",
    },
    "barcode": {
        "min_length": 6,
        "max_length": 20,
        "multi_barcode_top_percent": 33,
        "dpi": 200,
        "region": {
            "enabled":        False,
            "left_percent":   0,
            "top_percent":    0,
            "right_percent":  100,
            "bottom_percent": 100,
        },
    },
    "ocr": {
        "language": "eng",
        "dpi": 300,
        "region": {
            "enabled": False,
            "left_percent":   0,
            "top_percent":    0,
            "right_percent":  100,
            "bottom_percent": 40,
        },
    },
    "patterns": {
        "extra_regex": [],
    },
}


def load_config(path: str = "config.json") -> dict:
    import copy
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    p = Path(path)
    if not p.exists():
        log.warning(f"config.json not found at {p.resolve()} — using defaults")
        return cfg
    try:
        with open(p, encoding="utf-8") as f:
            user = json.load(f)
        _deep_merge(cfg, user)
        log.info(f"Config loaded: {p.resolve()}")
    except Exception as e:
        log.error(f"Failed to read config: {e} — using defaults")
    return cfg


def _deep_merge(base: dict, override: dict):
    for k, v in override.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and k in base and isinstance(base[k], dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


# ── Barcode reading ───────────────────────────────────────────────────────────

def _crop_barcode_region(image, cfg: dict):
    """
    Crop image to barcode scan region if configured.
    Returns (cropped_image, was_cropped).
    """
    region = cfg.get("barcode", {}).get("region", {})
    if not region.get("enabled", False):
        return image, False
    w, h = image.size
    left   = int(w * region.get("left_percent",   0)   / 100)
    top    = int(h * region.get("top_percent",    0)   / 100)
    right  = int(w * region.get("right_percent",  100) / 100)
    bottom = int(h * region.get("bottom_percent", 100) / 100)
    log.debug(f"  Barcode region crop: left={left} top={top} right={right} bottom={bottom}")
    return image.crop((left, top, right, bottom)), True


def _read_barcode_zxing(image) -> list[str]:
    """Primary engine — zxing-cpp (no extra DLL needed on Windows)."""
    try:
        import zxingcpp
        import numpy as np
        img_np = np.array(image.convert("RGB"))
        results = zxingcpp.read_barcodes(img_np)
        return [r.text.strip() for r in results if r.text.strip()]
    except Exception as e:
        log.debug(f"  zxing-cpp error: {e}")
        return []


def _read_barcode_pyzbar(image) -> list[str]:
    """Fallback engine — pyzbar."""
    try:
        from pyzbar import pyzbar
        import numpy as np
        import cv2
        img_np = np.array(image.convert("RGB"))
        img_cv = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        decoded = pyzbar.decode(img_cv)
        if not decoded:
            gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
            _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            decoded = pyzbar.decode(thresh)
        return [d.data.decode("utf-8", errors="ignore").strip() for d in decoded]
    except Exception as e:
        log.debug(f"  pyzbar error: {e}")
        return []


def _pick_barcode(values: list[str], image, cfg: dict) -> Optional[str]:
    """
    Given a list of decoded barcode strings from one page:
    - If only one, return it.
    - If multiple, restrict to top N% of the page and return the topmost.
    Image passed in is already the cropped region image.
    """
    if not values:
        return None
    if len(values) == 1:
        return values[0]

    # Multiple barcodes — use positional filtering via zxing
    try:
        import zxingcpp
        import numpy as np
        top_pct = cfg.get("barcode", {}).get("multi_barcode_top_percent", 33)
        img_np  = np.array(image.convert("RGB"))
        results = zxingcpp.read_barcodes(img_np)
        h       = image.height
        cutoff  = int(h * top_pct / 100)
        candidates = [r for r in results if r.position.top_left.y < cutoff]
        if candidates:
            candidates.sort(key=lambda r: r.position.top_left.y)
            return candidates[0].text.strip()
    except Exception:
        pass

    # Fallback: return first value
    return values[0]


def _try_read_barcode(image, cfg: dict) -> list[str]:
    """Try zxing then pyzbar on a given image. Returns list of raw values."""
    values = _read_barcode_zxing(image)
    if not values:
        values = _read_barcode_pyzbar(image)
    return values


def read_barcode_from_image(image, cfg: dict) -> Optional[str]:
    """
    1. Crop to barcode region (if configured) → read.
       If region enabled and read fails:
         1a. Upscale cropped region 2x → read again.
         1b. Still fails → fallback to full page → read.
    2. If region not enabled, read full page directly.
    3. Pick best candidate via _pick_barcode.
    Returns raw barcode string or None. Validation done by caller.
    """
    from PIL import Image as PILImage

    scan_image, was_cropped = _crop_barcode_region(image, cfg)

    if was_cropped:
        region = cfg["barcode"]["region"]
        log.debug(
            f"  Scanning barcode region: "
            f"left={region['left_percent']}% top={region['top_percent']}% "
            f"right={region['right_percent']}% bottom={region['bottom_percent']}%"
        )

        # Step 1: read cropped region
        values = _try_read_barcode(scan_image, cfg)

        if not values:
            # Step 1a: upscale cropped region 2x → read again
            log.info("  Barcode: region read failed → upscaling 2x and retrying")
            upscaled = scan_image.resize(
                (scan_image.width * 2, scan_image.height * 2), PILImage.LANCZOS
            )
            values = _try_read_barcode(upscaled, cfg)
            if values:
                log.info("  Barcode: found after 2x upscale")

        if not values:
            # Step 1b: fallback to full page
            log.info("  Barcode: upscale failed → retrying on full page")
            values = _try_read_barcode(image, cfg)
            if values:
                log.info("  Barcode: found on full page fallback")
    else:
        # No region configured — read full page directly
        values = _try_read_barcode(scan_image, cfg)

    if not values:
        return None

    # Pick best candidate (uses original scan_image for position reference)
    return _pick_barcode(values, scan_image, cfg)


# ── Length validation ─────────────────────────────────────────────────────────

def validate_length(value: str, cfg: dict, section: str) -> bool:
    """
    Validate value length against min_length / max_length in the given config section.
    section: "barcode" or "ocr"
    """
    sec   = cfg.get(section, {})
    min_l = int(sec.get("min_length", 1))
    max_l = int(sec.get("max_length", 999))
    ok    = min_l <= len(value) <= max_l
    if not ok:
        log.info(
            f"  Length validation FAILED [{section}]: '{value}' "
            f"len={len(value)}, expected {min_l}–{max_l}"
        )
    return ok


# ── PDF utilities ─────────────────────────────────────────────────────────────

def _get_poppler_path() -> str | None:
    """
    Look for poppler in this order:
    1. poppler_portable/Library/bin/  (next to exe / script)  <- bundled
    2. System PATH                                              <- user installed
    Returns path string or None (None = rely on PATH).
    """
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).parent

    bundled = base / "poppler_portable" / "Library" / "bin"
    if bundled.exists() and (bundled / "pdftoppm.exe").exists():
        log.info(f"  Using bundled Poppler: {bundled}")
        return str(bundled)

    log.debug("  Bundled Poppler not found — relying on system PATH")
    return None


def pdf_to_first_page_image(pdf_path: str, cfg: dict):
    """Render first page of PDF to PIL Image."""
    from pdf2image import convert_from_path
    dpi          = cfg.get("barcode", {}).get("dpi", 200)
    poppler_path = _get_poppler_path()
    kwargs       = {"dpi": dpi, "first_page": 1, "last_page": 1}
    if poppler_path:
        kwargs["poppler_path"] = poppler_path
    images = convert_from_path(str(pdf_path), **kwargs)
    return images[0] if images else None


def merge_pdfs(pdf_paths: list[str], output_path: str):
    """Merge multiple PDFs into one."""
    try:
        from pypdf import PdfWriter
    except ImportError:
        from PyPDF2 import PdfWriter

    writer = PdfWriter()
    for p in pdf_paths:
        writer.append(str(p))
    with open(output_path, "wb") as f:
        writer.write(f)


def get_pdf_page_count(pdf_path: str) -> int:
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader
    try:
        return len(PdfReader(str(pdf_path)).pages)
    except Exception as e:
        log.warning(f"  Could not read page count for {Path(pdf_path).name}: {e} — treating as 1 page")
        return 1


def split_multipage_pdfs_in_inbox(inbox: Path, cfg: dict, run_ts: str):
    """
    Pre-processing step: scan inbox for multi-page PDFs, split each page into
    a separate file ({stem}-001.pdf, {stem}-002.pdf ...) in the same inbox folder,
    then archive/delete the original.  Single-page PDFs are left untouched.
    Called once in main() before the main pdf_files scan.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        from PyPDF2 import PdfReader, PdfWriter

    candidates = sorted([p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"])

    for pdf_path in candidates:
        page_count = get_pdf_page_count(str(pdf_path))
        if page_count <= 1:
            continue

        log.info(f"Multi-page detected: {pdf_path.name} ({page_count} pages) → splitting")
        try:
            reader = PdfReader(str(pdf_path))
            stem   = pdf_path.stem

            for i, page in enumerate(reader.pages, start=1):
                writer = PdfWriter()
                writer.add_page(page)
                page_path = inbox / f"{stem}-{i:03d}.pdf"
                with open(page_path, "wb") as f:
                    writer.write(f)
                log.info(f"  → Split page: {page_path.name}")

            # Archive original if configured, then delete from inbox
            archive_files([str(pdf_path)], cfg, run_ts)
            pdf_path.unlink()
            log.info(f"  Original removed from inbox: {pdf_path.name}")

        except Exception as e:
            log.error(f"  Split failed for {pdf_path.name}: {e} — leaving original intact")


# ── File operations ───────────────────────────────────────────────────────────

def build_filename(number: str, cfg: dict) -> str:
    fmt    = cfg.get("filename", {})
    prefix = fmt.get("prefix", "DOC")
    suffix = fmt.get("suffix", "")
    tmpl   = fmt.get("format", "{prefix}_{number}.pdf")
    date   = datetime.now().strftime(fmt.get("date_format", "%Y%m%d"))
    name   = tmpl.format(prefix=prefix, number=number, suffix=suffix, date=date)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def safe_path(directory: Path, filename: str) -> Path:
    dest = directory / filename
    if dest.exists():
        ts   = datetime.now().strftime("%Y%m%d%H%M%S")
        dest = directory / f"{Path(filename).stem}_{ts}.pdf"
        log.warning(f"  Name collision — saving as: {dest.name}")
    return dest


def _get_output_dir(cfg: dict) -> Path:
    """
    Returns the effective output directory.
    If output_folder.create_subfolder is enabled, creates and returns a
    dated subfolder inside the configured output path.
    """
    base = Path(cfg["paths"]["output"])
    of   = cfg.get("output_folder", {})
    if of.get("create_subfolder", False):
        fmt       = of.get("subfolder_format", "%m%d")
        subname   = datetime.now().strftime(fmt)
        subfolder = base / subname
        subfolder.mkdir(parents=True, exist_ok=True)
        return subfolder
    return base


def ensure_dirs(cfg: dict):
    for key in ("output", "failed", "archive"):
        path = cfg["paths"].get(key, "").strip()
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)
    # Pre-create output subfolder if configured
    _get_output_dir(cfg)


def _archive_enabled(cfg: dict) -> Optional[Path]:
    raw = cfg.get("paths", {}).get("archive", "").strip()
    if not raw:
        return None
    try:
        p = Path(raw)
        p.mkdir(parents=True, exist_ok=True)
        return p
    except Exception as e:
        log.warning(f"  Archive path unavailable ({raw}): {e} — skipping archive")
        return None


def archive_files(src_paths: list[str], cfg: dict, run_ts: str):
    """
    Copy originals to Archive/YYYY-MM-DD_HHMMSS/ folder (one folder per run).
    No-op if archive path is empty or unreachable.
    This function only COPIES — never deletes source files.
    """
    archive_base = _archive_enabled(cfg)
    if archive_base is None:
        return

    archive_dir = archive_base / run_ts
    archive_dir.mkdir(parents=True, exist_ok=True)
    for src in src_paths:
        if not Path(src).exists():
            log.warning(f"  Archive skip (file not found): {Path(src).name}")
            continue
        try:
            dst = safe_path(archive_dir, Path(src).name)
            shutil.copy2(src, str(dst))
            log.info(f"  → Archive: {run_ts}/{dst.name}")
        except Exception as e:
            log.warning(f"  Archive copy failed for {Path(src).name}: {e}")


def move_to_failed(src: str, cfg: dict, run_ts: str):
    """Copy to archive (if configured), then move to Failed folder."""
    archive_files([src], cfg, run_ts)
    dst = safe_path(Path(cfg["paths"]["failed"]), Path(src).name)
    shutil.move(src, str(dst))
    log.info(f"  → Failed: {dst.name}")


def save_output(src_paths: list[str], number: str, cfg: dict, run_ts: str):
    """
    Copy originals to archive (if configured), then merge/move to Output.
    """
    archive_files(src_paths, cfg, run_ts)

    out_dir  = _get_output_dir(cfg)
    filename = build_filename(number, cfg)
    dest     = safe_path(out_dir, filename)

    if len(src_paths) == 1:
        shutil.move(src_paths[0], str(dest))
    else:
        merge_pdfs(src_paths, str(dest))
        for p in src_paths:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception as e:
                log.warning(f"  Cleanup failed for {Path(p).name}: {e}")

    log.info(f"  → Output: {dest.name}  ({len(src_paths)} page(s) merged)")
    return str(dest)


# ── Mode 1 ────────────────────────────────────────────────────────────────────

def process_mode1(pdf_files: list[str], cfg: dict, run_ts: str):
    """
    Mode 1 — All barcode.
    Every page must have a barcode. Pages with the same barcode value are merged.
    Pages with no barcode, or barcode failing length validation, go to Failed.
    """
    log.info(f"=== MODE 1 (all barcode)  ({len(pdf_files)} files) ===")
    stats: dict = {"ok": 0, "fail": 0}

    # groups: {barcode_value: [pdf_path, ...]}
    groups: dict[str, list[str]] = {}

    for pdf_path in pdf_files:
        log.info(f"Reading: {Path(pdf_path).name}")
        try:
            image = pdf_to_first_page_image(pdf_path, cfg)
        except Exception as e:
            log.error(f"  Render failed: {e}")
            move_to_failed(pdf_path, cfg, run_ts)
            stats["fail"] += 1
            continue

        value = read_barcode_from_image(image, cfg) if image else None

        if value and validate_length(value, cfg, section="barcode"):
            if value not in groups:
                log.info(f"  Barcode: '{value}' → new group")
                groups[value] = []
            else:
                log.info(f"  Barcode: '{value}' → appending to existing group")
            groups[value].append(pdf_path)
        else:
            if value is None:
                log.warning(f"  No barcode found → Failed")
            else:
                log.warning(f"  Barcode '{value}' failed length validation → Failed")
            move_to_failed(pdf_path, cfg, run_ts)
            stats["fail"] += 1

    # Flush all groups
    for value, paths in groups.items():
        save_output(paths, value, cfg, run_ts)
        stats["ok"] += 1

    log.info(f"=== MODE 1 DONE  ✓{stats['ok']} groups  ✗{stats['fail']} failed ===\n")
    return stats


# ── Mode 2 ────────────────────────────────────────────────────────────────────

def process_mode2(pdf_files: list[str], cfg: dict, run_ts: str):
    """
    Mode 2 — Barcode + trailing pages.
    First page with barcode opens a group.
    Subsequent pages without barcode are always appended to the current group.
    A new barcode value closes the current group and opens a new one.
    Pages before the first barcode go to Failed.
    """
    log.info(f"=== MODE 2 (barcode + trailing)  ({len(pdf_files)} files) ===")
    stats: dict = {"ok": 0, "fail": 0}

    current_group: list[str] = []
    current_value: Optional[str] = None

    def flush_group():
        nonlocal current_group, current_value
        if not current_group or current_value is None:
            return
        save_output(current_group, current_value, cfg, run_ts)
        stats["ok"] += 1
        current_group = []
        current_value = None

    for pdf_path in pdf_files:
        log.info(f"Reading: {Path(pdf_path).name}")
        try:
            image = pdf_to_first_page_image(pdf_path, cfg)
        except Exception as e:
            log.error(f"  Render failed: {e}")
            flush_group()
            move_to_failed(pdf_path, cfg, run_ts)
            stats["fail"] += 1
            continue

        value = read_barcode_from_image(image, cfg) if image else None

        if value and validate_length(value, cfg, section="barcode"):
            if value == current_value:
                log.info(f"  Barcode: '{value}' matches current group → appending")
                current_group.append(pdf_path)
            else:
                flush_group()
                log.info(f"  Barcode: '{value}' → new group")
                current_value = value
                current_group = [pdf_path]
        else:
            if current_value is None:
                log.warning(f"  No barcode, no active group → Failed")
                move_to_failed(pdf_path, cfg, run_ts)
                stats["fail"] += 1
            else:
                log.info(f"  No barcode → appending to group '{current_value}'")
                current_group.append(pdf_path)

    flush_group()

    log.info(f"=== MODE 2 DONE  ✓{stats['ok']} groups  ✗{stats['fail']} failed ===\n")
    return stats



# ── Log housekeeping ──────────────────────────────────────────────────────────

LOG_RETENTION_DAYS_DEFAULT = 7


def housekeep_log(log_path: Path, cfg: dict):
    """
    Remove log entries older than N days from log.txt.
    Retention days read from config: app.log_retention_days (default 14).
    Each run block starts with the separator line '=' * 60.
    """
    try:
        raw = cfg.get("app", {}).get("log_retention_days", "")
        try:
            retention_days = int(str(raw).strip()) if str(raw).strip() else LOG_RETENTION_DAYS_DEFAULT
        except ValueError:
            retention_days = LOG_RETENTION_DAYS_DEFAULT

        if not log_path.exists():
            return

        with open(log_path, encoding="utf-8") as f:
            raw_text = f.read()

        if not raw_text.strip():
            return

        from datetime import timedelta
        cutoff  = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff -= timedelta(days=retention_days)

        separator = "=" * 60
        blocks: list[str] = []
        current: list[str] = []
        for line in raw_text.splitlines(keepends=True):
            if line.strip() == separator and current:
                blocks.append("".join(current))
                current = [line]
            else:
                current.append(line)
        if current:
            blocks.append("".join(current))

        kept    = []
        removed = 0
        for block in blocks:
            ts_match = re.search(r"RUN STARTED\s+(\d{4}-\d{2}-\d{2})_(\d{6})", block)
            if ts_match:
                try:
                    block_date = datetime.strptime(ts_match.group(1), "%Y-%m-%d")
                    if block_date < cutoff:
                        removed += 1
                        continue
                except ValueError:
                    pass
            kept.append(block)

        if removed:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("".join(kept))
            log.info(f"Log housekeep: removed {removed} run block(s) older than {retention_days} days")
        else:
            log.debug(f"Log housekeep: nothing to remove (retention={retention_days} days)")

    except Exception as e:
        log.warning(f"Log housekeep failed: {e}")


# ── Main entry ────────────────────────────────────────────────────────────────

def main():
    run_ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log.info("=" * 60)
    log.info(f"RUN STARTED  {run_ts}")
    log.info("=" * 60)

    if getattr(sys, "frozen", False):
        script_dir = Path(sys.executable).parent
    else:
        script_dir = Path(__file__).parent
    cfg = load_config(str(script_dir / "config.json"))

    housekeep_log(LOG_PATH, cfg)
    ensure_dirs(cfg)

    inbox = Path(cfg["paths"]["inbox"])
    if not inbox.exists():
        log.error(f"Inbox folder not found: {inbox}")
        sys.exit(1)

    split_multipage_pdfs_in_inbox(inbox, cfg, run_ts)

    pdf_files = sorted(
        [str(p) for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"]
    )

    if not pdf_files:
        log.info("No PDF files found in inbox.")
        return

    log.info(f"Found {len(pdf_files)} PDF(s) in: {inbox}")

    # ── Backward compatibility: honour legacy barcode.enabled if mode not set ──
    mode = cfg.get("mode", "").strip()
    if not mode:
        barcode_enabled = cfg.get("barcode", {}).get("enabled", True)
        mode = "mode2" if barcode_enabled else "mode2"
        log.warning(
            f"  'mode' not found in config — "
            f"falling back to mode2 (legacy barcode.enabled={barcode_enabled})"
        )

    log.info(f"Mode: {mode}")

    if mode == "mode1":
        process_mode1(pdf_files, cfg, run_ts)
    elif mode == "mode2":
        process_mode2(pdf_files, cfg, run_ts)
    else:
        log.error(f"Unknown mode '{mode}' in config — no processing done.")


if __name__ == "__main__":
    main()