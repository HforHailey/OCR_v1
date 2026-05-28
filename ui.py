"""
ui.py — Invoice OCR Auto-Renamer UI
=====================================
Entry point for the packaged exe.
Requires: ttkbootstrap, Pillow, pdf2image (for region preview)

PyInstaller:
    pyinstaller --onefile ui.py
"""

import copy
import json
import logging
import queue
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import *
except ImportError:
    raise SystemExit("ttkbootstrap not installed. Run: pip install ttkbootstrap")

# ── Paths ─────────────────────────────────────────────────────────────────────
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent

CONFIG_PATH = BASE_DIR / "config.json"

# ── Colours ───────────────────────────────────────────────────────────────────
ORANGE      = "#F27224"
ORANGE_DARK = "#D4611E"
WHITE       = "#FFFFFF"
LIGHT_GREY  = "#F5F5F5"
MID_GREY    = "#E2E2E2"
TEXT_DARK   = "#1A1A1A"
TEXT_MUTED  = "#6B7280"
RED_ERR     = "#DC2626"
GREEN_OK    = "#16A34A"

# ── Shared helpers ───────────────────────────────────────────────────────────

def _shake(window: tk.Toplevel | tk.Tk):
    """Briefly shake a window left-right to draw attention."""
    x0 = window.winfo_x()
    y  = window.winfo_y()
    offsets = [10, -10, 8, -8, 5, -5, 2, -2, 0]

    def _step(i=0):
        if i >= len(offsets):
            window.geometry(f"+{x0}+{y}")
            return
        window.geometry(f"+{x0 + offsets[i]}+{y}")
        window.after(30, _step, i + 1)

    _step()


def _dirty_confirm_dialog(parent, on_save, on_discard):
    """
    Modal 'unsaved changes' prompt.
    Calls on_save() or on_discard() depending on user choice.
    """
    dlg = tk.Toplevel(parent)
    dlg.title("Unsaved Changes")
    dlg.geometry("600x230")
    dlg.resizable(False, False)
    dlg.configure(bg=WHITE)
    dlg.grab_set()
    dlg.transient(parent)

    tk.Label(
        dlg,
        text="You have unsaved changes. Closing will discard all changes.\n"
             "Do you want to save before closing?",
        bg=WHITE, font=("Segoe UI", 10),
        fg=TEXT_DARK, justify=tk.LEFT,
        pady=0, padx=0,
    ).pack(anchor=tk.W, padx=24, pady=(24, 0))

    bf = tk.Frame(dlg, bg=WHITE)
    bf.pack(fill=tk.X, padx=24, pady=20, side=tk.BOTTOM)

    def _do_discard():
        dlg.destroy()
        on_discard()

    def _do_save():
        dlg.destroy()
        on_save()

    ttk.Button(bf, text="Save",          bootstyle="warning",
               command=_do_save).pack(side=tk.RIGHT, padx=(8, 0))
    ttk.Button(bf, text="Discard & Close", bootstyle="secondary",
               command=_do_discard).pack(side=tk.RIGHT)


# ── Logging bridge (main.py → UI queue) ──────────────────────────────────────
_log_queue: queue.Queue = queue.Queue()


class _QueueHandler(logging.Handler):
    def emit(self, record):
        _log_queue.put(self.format(record))


def _install_queue_handler():
    root_log = logging.getLogger()
    h = _QueueHandler()
    h.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root_log.addHandler(h)


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    try:
        import main as m
        return m.load_config(str(CONFIG_PATH))
    except Exception:
        return _default_config()


def _default_config() -> dict:
    return {
        "mode": "mode2",
        "paths": {"inbox": "", "output": "", "failed": "", "archive": ""},
        "filename": {"prefix": "DOC", "format": "{prefix}_{number}.pdf",
                     "suffix": "", "date_format": "%Y%m%d"},
        "output_folder": {"create_subfolder": False, "subfolder_format": "%m%d"},
        "barcode": {
            "min_length": 6, "max_length": 20,
            "region": {"enabled": False, "left_percent": 0, "top_percent": 0,
                       "right_percent": 100, "bottom_percent": 100},
        },
        "ocr": {
            "language": "eng", "dpi": 300, "whitelist": "",
            "field_labels": [],
            "region": {"enabled": False, "left_percent": 0, "top_percent": 0,
                       "right_percent": 100, "bottom_percent": 40},
        },
        "patterns": {"extra_regex": []},
        "app": {"log_retention_days": 14},
    }


def _save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ── Region selector window ────────────────────────────────────────────────────

class RegionSelector(tk.Toplevel):
    """
    Popup window — drag a rectangle on a PDF preview image.
    Returns percent values via .result dict after Save.
    """

    def __init__(self, parent, cfg: dict, on_save_callback, mode: str = "mode2"):
        super().__init__(parent)
        self.title("Set Region")
        self.geometry("980x1080")
        self.minsize(620, 500)
        self.resizable(True, True)
        self.configure(bg=WHITE)
        self.grab_set()

        self._cfg            = cfg
        self._on_save        = on_save_callback
        self._mode           = mode
        self._canvas_img     = None
        self._pil_preview    = None
        self._preview_scale  = 1.0
        self._drag_start     = None
        self._rect_id        = None
        self._show_ocr_tab   = (mode == "mode3")

        # Per-tab region state: {"barcode": {...}, "ocr": {...}}
        self._regions = {
            "barcode": copy.deepcopy(cfg.get("barcode", {}).get("region", {})),
            "ocr":     copy.deepcopy(cfg.get("ocr", {}).get("region", {})),
        }
        self._orig_regions = copy.deepcopy(self._regions)

        self._build_ui()
        self._load_preview()
        self._draw_region_from_state()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # Shake this window when user tries to click the parent while we're open
        self.bind("<FocusIn>", self._on_self_focus_in)
        self._focus_just_opened = True
        self.after(300, self._clear_open_flag)

    def _clear_open_flag(self):
        self._focus_just_opened = False

    def _on_self_focus_in(self, event=None):
        if getattr(self, "_focus_just_opened", True):
            return
        if event and event.widget is not self:
            return
        _shake(self)

    def _cleanup_parent_binding(self):
        pass

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Top bar: tabs + apply-to-both ─────────────────────────────────────
        top = tk.Frame(self, bg=WHITE, pady=6, padx=10)
        top.pack(fill=tk.X)

        self._tab_var    = tk.StringVar(value="barcode")
        self._apply_both = tk.BooleanVar(value=False)

        tabs = [("barcode", "Barcode")]
        if self._show_ocr_tab:
            tabs.append(("ocr", "OCR"))

        for key, label in tabs:
            btn = tk.Button(
                top, text=label, relief=tk.FLAT, cursor="hand2",
                font=("Segoe UI", 10, "bold"),
                bg=ORANGE if key == "barcode" else MID_GREY,
                fg=WHITE  if key == "barcode" else TEXT_DARK,
                activebackground=ORANGE_DARK, activeforeground=WHITE,
                padx=12, pady=4,
                command=lambda k=key: self._switch_tab(k),
            )
            btn.pack(side=tk.LEFT, padx=(0, 2))
            setattr(self, f"_tab_btn_{key}", btn)
            if key == "barcode":
                btn.config(bg=ORANGE, fg=WHITE)

        if self._show_ocr_tab:
            ttk.Checkbutton(
                top, text="Apply to both Barcode & OCR",
                variable=self._apply_both, bootstyle="warning",
            ).pack(side=tk.LEFT, padx=(16, 0))

        # ── Enable region checkbox (above canvas) ─────────────────────────────
        self._enabled_var = tk.BooleanVar()
        enable_row = tk.Frame(self, bg=WHITE, padx=10, pady=2)
        enable_row.pack(fill=tk.X)
        ttk.Checkbutton(
            enable_row, text="Enable region",
            variable=self._enabled_var, bootstyle="warning",
            command=self._on_enable_toggle,
        ).pack(side=tk.LEFT)

        # ── Canvas — PDF preview, drag directly on image ──────────────────────
        canvas_frame = tk.Frame(self, bg=ORANGE, padx=3, pady=3)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(2, 0))
        inner = tk.Frame(canvas_frame, bg=LIGHT_GREY)
        inner.pack(fill=tk.BOTH, expand=True)

        self._canvas = tk.Canvas(
            inner, bg=LIGHT_GREY,
            cursor="crosshair", highlightthickness=0,
        )
        self._canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas.bind("<ButtonPress-1>",   self._on_press)
        self._canvas.bind("<B1-Motion>",       self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas.bind("<Configure>",       lambda e: self._redraw_canvas())

        # ── Percent inputs ────────────────────────────────────────────────────
        pct_frame = tk.Frame(self, bg=WHITE, pady=6, padx=10)
        pct_frame.pack(fill=tk.X)

        self._pct_vars = {}
        for i, (key, label) in enumerate(
            (("left_percent", "Left"), ("top_percent", "Top"),
             ("right_percent", "Right"), ("bottom_percent", "Bottom"))
        ):
            tk.Label(pct_frame, text=f"{label} %", bg=WHITE,
                     font=("Segoe UI", 9), fg=TEXT_MUTED).grid(
                row=0, column=i * 2, padx=(10 if i else 0, 2), sticky="e")
            var = tk.StringVar()
            var.trace_add("write", lambda *a, k=key: self._on_pct_edit(k))
            entry = ttk.Entry(pct_frame, textvariable=var, width=6,
                              bootstyle="warning")
            entry.grid(row=0, column=i * 2 + 1, padx=(0, 8))
            self._pct_vars[key] = var

        self._update_pct_display()

        # ── Bottom buttons ────────────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg=WHITE, pady=8, padx=10)
        btn_frame.pack(fill=tk.X)

        self._reset_btn = tk.Button(
            btn_frame, text="Reset",
            bg=MID_GREY, fg=TEXT_MUTED,
            font=("Segoe UI", 9), relief=tk.FLAT,
            cursor="hand2", padx=10, pady=4,
            state=tk.DISABLED,
            command=self._reset,
        )
        self._reset_btn.pack(side=tk.LEFT)

        ttk.Button(btn_frame, text="Cancel", bootstyle="secondary",
                   command=self._on_cancel).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btn_frame, text="Save", bootstyle="warning",
                   command=self._save).pack(side=tk.RIGHT)

    # ── Tab switching ─────────────────────────────────────────────────────────

    def _switch_tab(self, key: str):
        self._tab_var.set(key)
        for k in ("barcode", "ocr"):
            btn = getattr(self, f"_tab_btn_{k}", None)
            if btn is None:
                continue
            btn.config(bg=ORANGE if k == key else MID_GREY,
                       fg=WHITE  if k == key else TEXT_DARK)
        self._update_pct_display()
        self._draw_region_from_state()

    def _current_tab(self) -> str:
        return self._tab_var.get()

    # ── Preview loading ───────────────────────────────────────────────────────

    def _load_preview(self):
        inbox = self._cfg.get("paths", {}).get("inbox", "").strip()
        if inbox:
            inbox_path = Path(inbox)
            pdfs = sorted(inbox_path.glob("*.pdf")) if inbox_path.exists() else []
            if pdfs:
                try:
                    from pdf2image import convert_from_path
                    import main as m
                    dpi = min(self._cfg.get("barcode", {}).get("dpi", 150), 150)
                    pp  = m._get_poppler_path()
                    kw  = {"dpi": dpi, "first_page": 1, "last_page": 1}
                    if pp:
                        kw["poppler_path"] = pp
                    images = convert_from_path(str(pdfs[0]), **kw)
                    if images:
                        self._pil_preview = images[0]
                        return
                except Exception:
                    pass
        # No PDF found — generate blank A4 with orange border
        self._pil_preview = self._make_blank_a4()

    def _make_blank_a4(self):
        """Create a blank white A4 image with orange border as sample."""
        from PIL import Image as PILImage, ImageDraw
        w, h      = 595, 842   # A4 at 72dpi
        img       = PILImage.new("RGB", (w, h), WHITE)
        draw      = ImageDraw.Draw(img)
        border    = 6
        draw.rectangle(
            [border, border, w - border, h - border],
            outline=ORANGE, width=border,
        )
        return img

    # ── Canvas draw ───────────────────────────────────────────────────────────

    def _redraw_canvas(self):
        self._canvas.delete("all")
        self._canvas_img = None
        cw = self._canvas.winfo_width()
        ch = self._canvas.winfo_height()
        if cw < 10 or ch < 10:
            return

        if self._pil_preview:
            from PIL import ImageTk
            pw, ph = self._pil_preview.size
            scale  = min(cw / pw, ch / ph)
            nw, nh = int(pw * scale), int(ph * scale)
            img    = self._pil_preview.resize((nw, nh))
            self._preview_scale = scale
            self._canvas_img    = ImageTk.PhotoImage(img)
            ox = (cw - nw) // 2
            oy = (ch - nh) // 2
            self._canvas.create_image(ox, oy, anchor=tk.NW, image=self._canvas_img)
            self._canvas._img_offset = (ox, oy, nw, nh)
        else:
            self._canvas._img_offset = None

        self._draw_region_from_state()

    def _draw_region_from_state(self):
        self._canvas.delete("region")
        offset = getattr(self._canvas, "_img_offset", None)
        if offset is None:
            return
        ox, oy, nw, nh = offset
        if self._enabled_var.get():
            r  = self._regions[self._current_tab()]
            l  = ox + int(r.get("left_percent",   0)   / 100 * nw)
            t  = oy + int(r.get("top_percent",    0)   / 100 * nh)
            ri = ox + int(r.get("right_percent",  100) / 100 * nw)
            b  = oy + int(r.get("bottom_percent", 100) / 100 * nh)
        else:
            l, t, ri, b = ox, oy, ox + nw, oy + nh
        self._canvas.create_rectangle(
            l, t, ri, b,
            outline=ORANGE, width=2, dash=(6, 3), tags="region",
        )

    def _on_enable_toggle(self):
        enabled = self._enabled_var.get()
        self._regions[self._current_tab()]["enabled"] = enabled
        self._draw_region_from_state()
        self._update_reset_btn()

    # ── Drag handlers ─────────────────────────────────────────────────────────

    def _canvas_to_pct(self, cx, cy):
        offset = getattr(self._canvas, "_img_offset", None)
        if offset is None:
            return None, None
        ox, oy, nw, nh = offset
        px = max(0, min(100, (cx - ox) / nw * 100))
        py = max(0, min(100, (cy - oy) / nh * 100))
        return round(px, 1), round(py, 1)

    def _on_press(self, event):
        self._drag_start = (event.x, event.y)

    def _on_drag(self, event):
        if not self._drag_start:
            return
        self._canvas.delete("region")
        x0, y0 = self._drag_start
        self._canvas.create_rectangle(
            x0, y0, event.x, event.y,
            outline=ORANGE, width=2, dash=(6, 3), tags="region",
        )

    def _on_release(self, event):
        if not self._drag_start:
            return
        x0, y0 = self._drag_start
        x1, y1 = event.x, event.y
        lp, tp = self._canvas_to_pct(min(x0, x1), min(y0, y1))
        rp, bp = self._canvas_to_pct(max(x0, x1), max(y0, y1))
        if lp is None:
            return
        targets = ["barcode", "ocr"] if self._apply_both.get() else [self._current_tab()]
        for t in targets:
            self._regions[t].update(
                left_percent=lp, top_percent=tp,
                right_percent=rp, bottom_percent=bp,
            )
        self._drag_start = None
        self._update_pct_display()
        self._update_reset_btn()

    # ── Percent edit ──────────────────────────────────────────────────────────

    def _on_pct_edit(self, key: str):
        try:
            val = float(self._pct_vars[key].get())
            val = max(0, min(100, val))
        except ValueError:
            return
        self._regions[self._current_tab()][key] = val
        self._draw_region_from_state()
        self._update_reset_btn()

    def _update_pct_display(self):
        r = self._regions[self._current_tab()]
        for key, var in self._pct_vars.items():
            var.set(str(r.get(key, 0)))
        self._enabled_var.set(r.get("enabled", False))

    # ── Dirty state ───────────────────────────────────────────────────────────

    def _is_dirty(self) -> bool:
        return self._regions != self._orig_regions

    def _update_reset_btn(self):
        print(f"DEBUG _update_reset_btn hasattr={hasattr(self, '_reset_btn')}")
        if not hasattr(self, "_reset_btn"):
            return
        if self._is_dirty():
            self._reset_btn.config(
                state=tk.NORMAL, bg=ORANGE, fg=WHITE,
                activebackground=ORANGE_DARK, activeforeground=WHITE,
            )
        else:
            self._reset_btn.config(
                state=tk.DISABLED, bg=MID_GREY, fg=TEXT_MUTED,
            )

    # ── Reset / Cancel / Save ─────────────────────────────────────────────────

    def _reset(self):
        self._regions = copy.deepcopy(self._orig_regions)
        self._update_pct_display()
        self._draw_region_from_state()
        self._update_reset_btn()

    def _on_cancel(self):
        if self._is_dirty():
            _dirty_confirm_dialog(
                self,
                on_save=self._save,
                on_discard=lambda: (self._cleanup_parent_binding(), self.destroy()),
            )
        else:
            self._cleanup_parent_binding()
            self.destroy()

    def _save(self):
        for tab in ("barcode", "ocr"):
            self._regions[tab]["enabled"] = self._enabled_var.get() \
                if tab == self._current_tab() \
                else self._regions[tab].get("enabled", False)
        if self._apply_both.get():
            for tab in ("barcode", "ocr"):
                self._regions[tab]["enabled"] = self._enabled_var.get()
        self._on_save(copy.deepcopy(self._regions))
        self._cleanup_parent_binding()
        self.destroy()


# ── Settings window ───────────────────────────────────────────────────────────

class SettingsWindow(tk.Toplevel):

    def __init__(self, parent, cfg: dict, on_save_callback):
        super().__init__(parent)
        self.title("Settings")
        self.geometry("680x950")
        self.minsize(600, 800)
        self.resizable(True, True)
        self.configure(bg=WHITE)
        self.grab_set()

        self._cfg_orig   = cfg
        self._cfg        = copy.deepcopy(cfg)
        self._on_save    = on_save_callback
        self._vars: dict = {}

        self._build_ui()
        self._populate()
        # Snapshot taken after populate so StringVars reflect loaded values
        self._orig_snapshot = self._collect_snapshot()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # Shake this window when user tries to click the parent while we're open
        self.bind("<FocusIn>", self._on_self_focus_in)
        self._focus_just_opened = True
        self.after(300, self._clear_open_flag)
        self.bind("<FocusOut>", self._on_self_focus_out)
        self._focus_left = False

    def _clear_open_flag(self):
        self._focus_just_opened = False

    def _on_self_focus_in(self, event=None):
        if getattr(self, "_focus_just_opened", True):
            return
        if event and event.widget is not self:
            return
        if not getattr(self, "_focus_left", False):
            return
        self._focus_left = False
        _shake(self)

    def _on_self_focus_out(self, event=None):
        if event and event.widget is not self:
            return
        # Ignore focus loss caused by internal Combobox dropdown
        focused = self.focus_get()
        if focused and str(focused).endswith("popdown"):
            return
        self._focus_left = True

    def _cleanup_parent_binding(self):
        pass

    def _collect_snapshot(self) -> dict:
        """Lightweight snapshot of current var values for dirty detection."""
        return {k: v.get() for k, v in self._vars.items()}

    def _is_dirty(self) -> bool:
        return self._collect_snapshot() != self._orig_snapshot

    def _on_cancel(self):
        if self._is_dirty():
            _dirty_confirm_dialog(
                self,
                on_save=self._save,
                on_discard=lambda: (self._cleanup_parent_binding(), self.destroy()),
            )
        else:
            self._cleanup_parent_binding()
            self.destroy()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Scrollable body
        outer = tk.Frame(self, bg=WHITE)
        outer.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(outer, bg=WHITE, highlightthickness=0)
        sb     = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview,
                               bootstyle="warning-round")
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._scroll_frame = tk.Frame(canvas, bg=WHITE)
        win_id = canvas.create_window((0, 0), window=self._scroll_frame, anchor=tk.NW)

        def _on_frame_configure(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        def _on_canvas_configure(e):
            canvas.itemconfig(win_id, width=e.width)

        self._scroll_frame.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>",             _on_canvas_configure)
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(-1*(e.delta//120), "units"))

        body = self._scroll_frame
        pad  = {"padx": 18, "pady": 3}

        # ── Mode ──────────────────────────────────────────────────────────────
        self._section(body, "Mode")
        mf = tk.Frame(body, bg=WHITE)
        mf.pack(fill=tk.X, **pad)
        self._vars["mode"] = tk.StringVar()
        _mode_options = {
            "mode1": "All Barcode",
            "mode2": "Barcode + Trailing Pages",
        }
        self._mode_combo = ttk.Combobox(
            mf,
            textvariable=self._vars["mode"],
            values=list(_mode_options.values()),
            state="readonly",
            bootstyle="warning",
            width=28,
        )
        self._mode_combo.pack(anchor=tk.W)
        # Map display label ↔ value
        self._mode_label_to_val = {v: k for k, v in _mode_options.items()}
        self._mode_val_to_label = _mode_options
        self._mode_combo.bind("<<ComboboxSelected>>", self._on_mode_change)

        # ── Paths ─────────────────────────────────────────────────────────────
        self._section(body, "Paths")
        for key, label in (("inbox","Inbox"), ("output","Output"),
                           ("failed","Failed"), ("archive","Archive")):
            self._path_row(body, key, label)
            if key == "output":
                self._vars["output_folder.create_subfolder"] = tk.BooleanVar()
                sf = tk.Frame(body, bg=LIGHT_GREY, padx=18, pady=4)
                sf.pack(fill=tk.X, padx=18, pady=(0, 2))
                ttk.Checkbutton(
                    sf, text="Create subfolder in Output",
                    variable=self._vars["output_folder.create_subfolder"],
                    bootstyle="warning",
                ).pack(anchor=tk.W)
                fmt_row = tk.Frame(sf, bg=LIGHT_GREY)
                fmt_row.pack(fill=tk.X, pady=(2, 0))
                tk.Label(fmt_row, text="Subfolder Format", bg=LIGHT_GREY,
                         width=16, anchor=tk.W,
                         font=("Segoe UI", 9), fg=TEXT_MUTED).pack(side=tk.LEFT)
                self._vars["output_folder.subfolder_format"] = tk.StringVar()
                ttk.Entry(fmt_row,
                          textvariable=self._vars["output_folder.subfolder_format"],
                          bootstyle="warning", width=14).pack(side=tk.LEFT)
                tk.Label(sf, text="e.g. %m%d → 0515",
                         bg=LIGHT_GREY, font=("Segoe UI", 8),
                         fg=TEXT_MUTED).pack(anchor=tk.W)
                tk.Label(sf, text="     DONE_%b%d → DONE_MAY15",
                         bg=LIGHT_GREY, font=("Segoe UI", 8),
                         fg=TEXT_MUTED).pack(anchor=tk.W)

            if key == "archive":
                tk.Label(body, text="If Archive path is empty, original files will not be archived.",
                 bg=WHITE, font=("Segoe UI", 8), fg=TEXT_MUTED
                 ).pack(anchor=tk.W, padx=18, pady=(0, 4))
        # ── Filename ──────────────────────────────────────────────────────────
        self._section(body, "Filename")
        for key, label in (("prefix","Prefix"), ("format","Format"),
                           ("suffix","Suffix"), ("date_format","Date Format")):
            self._text_row(body, f"filename.{key}", label)

        # ── Barcode ───────────────────────────────────────────────────────────
        self._section(body, "Barcode")
        bf = tk.Frame(body, bg=WHITE)
        bf.pack(fill=tk.X, **pad)
        self._inline_int(bf, "barcode.min_length", "Min Length", 0)
        self._inline_int(bf, "barcode.max_length",  "Max Length", 1)
        self._inline_int(bf, "barcode.dpi","DPI",2)
        reg_row = tk.Frame(body, bg=WHITE)
        reg_row.pack(fill=tk.X, **pad)
        tk.Label(reg_row, text="Region", bg=WHITE, width=14, anchor=tk.W,
                 font=("Segoe UI", 9), fg=TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Button(reg_row, text="Set Region…", bootstyle="warning-outline",
                   command=lambda: self._open_region("barcode")).pack(side=tk.LEFT)
        self._region_lbl_barcode = tk.Label(
            reg_row, bg=WHITE, font=("Segoe UI", 8), fg=TEXT_MUTED)
        self._region_lbl_barcode.pack(side=tk.LEFT, padx=8)

        # ── OCR (mode3 only) ───────────────────────────────────────────────────
        self._ocr_section_header = self._section(body, "OCR", return_widgets=True)
        self._ocr_frame = tk.Frame(body, bg=WHITE)
        of = tk.Frame(self._ocr_frame, bg=WHITE)
        of.pack(fill=tk.X, **pad)
        self._inline_text(of, "ocr.language", "Language", 0)
        self._inline_int(of,  "ocr.dpi",      "DPI",      1)
        self._text_row(self._ocr_frame, "ocr.whitelist", "Whitelist")
        # Field Labels — hint on next line
        fl_outer = tk.Frame(self._ocr_frame, bg=WHITE)
        fl_outer.pack(fill=tk.X, padx=18, pady=2)
        tk.Label(fl_outer, text="Field Labels", bg=WHITE, width=14, anchor=tk.W,
                 font=("Segoe UI", 9), fg=TEXT_MUTED).pack(anchor=tk.W)
        self._vars["ocr.field_labels"] = tk.StringVar()
        ttk.Entry(fl_outer, textvariable=self._vars["ocr.field_labels"],
                  bootstyle="warning").pack(fill=tk.X)
        tk.Label(fl_outer, text="comma-separated, e.g. Customer PO, ACKN NO",
                 bg=WHITE, font=("Segoe UI", 8), fg=TEXT_MUTED).pack(anchor=tk.W)
        reg_row2 = tk.Frame(self._ocr_frame, bg=WHITE)
        reg_row2.pack(fill=tk.X, **pad)
        tk.Label(reg_row2, text="Region", bg=WHITE, width=14, anchor=tk.W,
                 font=("Segoe UI", 9), fg=TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Button(reg_row2, text="Set Region…", bootstyle="warning-outline",
                   command=lambda: self._open_region("ocr")).pack(side=tk.LEFT)
        self._region_lbl_ocr = tk.Label(
            reg_row2, bg=WHITE, font=("Segoe UI", 8), fg=TEXT_MUTED)
        self._region_lbl_ocr.pack(side=tk.LEFT, padx=8)

        # ── Bottom buttons ────────────────────────────────────────────────────
        sep = ttk.Separator(self)
        sep.pack(fill=tk.X, padx=0, pady=4)
        btn_frame = tk.Frame(self, bg=WHITE, pady=8, padx=14)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="Cancel", bootstyle="secondary",
                   command=self._on_cancel).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btn_frame, text="Save", bootstyle="warning",
                   command=self._save).pack(side=tk.RIGHT)

    # ── Widget helpers ────────────────────────────────────────────────────────

    def _section(self, parent, title: str, return_widgets: bool = False):
        bar = tk.Frame(parent, bg=ORANGE, height=2)
        bar.pack(fill=tk.X, padx=18, pady=(14, 2))
        lbl = tk.Label(parent, text=title, bg=WHITE,
                       font=("Segoe UI", 10, "bold"), fg=ORANGE)
        lbl.pack(anchor=tk.W, padx=18)
        if return_widgets:
            return (bar, lbl)

    def _on_mode_change(self, event=None):
        label = self._vars["mode"].get()
        val   = self._mode_label_to_val.get(label, "mode2")
        # Show OCR section only for mode3
        if val == "mode3":
            self._ocr_frame.pack(fill=tk.X)
            for w in self._ocr_section_header:
                w.pack_configure()
        else:
            self._ocr_frame.pack_forget()
            for w in self._ocr_section_header:
                w.pack_forget()

    def _path_row(self, parent, key: str, label: str):
        row = tk.Frame(parent, bg=WHITE)
        row.pack(fill=tk.X, padx=18, pady=2)
        tk.Label(row, text=label, bg=WHITE, width=10, anchor=tk.W,
                 font=("Segoe UI", 9), fg=TEXT_MUTED).pack(side=tk.LEFT)
        var = tk.StringVar()
        self._vars[f"paths.{key}"] = var
        ttk.Entry(row, textvariable=var, bootstyle="warning").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        ttk.Button(row, text="Browse", bootstyle="warning-outline",
                   command=lambda k=key, v=var: self._browse(k, v)).pack(side=tk.LEFT)

    def _text_row(self, parent, dotkey: str, label: str, hint: str = ""):
        row = tk.Frame(parent, bg=WHITE)
        row.pack(fill=tk.X, padx=18, pady=2)
        tk.Label(row, text=label, bg=WHITE, width=14, anchor=tk.W,
                 font=("Segoe UI", 9), fg=TEXT_MUTED).pack(side=tk.LEFT)
        var = tk.StringVar()
        self._vars[dotkey] = var
        entry = ttk.Entry(row, textvariable=var, bootstyle="warning")
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        if hint:
            tk.Label(row, text=hint, bg=WHITE,
                     font=("Segoe UI", 8), fg=TEXT_MUTED).pack(side=tk.LEFT, padx=4)

    def _inline_int(self, parent, dotkey: str, label: str, col: int):
        tk.Label(parent, text=label, bg=WHITE,
                 font=("Segoe UI", 9), fg=TEXT_MUTED).grid(
            row=0, column=col*2, padx=(0 if col else 0, 4), sticky=tk.W)
        var = tk.StringVar()
        self._vars[dotkey] = var
        ttk.Entry(parent, textvariable=var, width=6, bootstyle="warning").grid(
            row=0, column=col*2+1, padx=(0, 16))

    def _inline_text(self, parent, dotkey: str, label: str, col: int):
        tk.Label(parent, text=label, bg=WHITE,
                 font=("Segoe UI", 9), fg=TEXT_MUTED).grid(
            row=0, column=col*2, padx=(0, 4), sticky=tk.W)
        var = tk.StringVar()
        self._vars[dotkey] = var
        ttk.Entry(parent, textvariable=var, width=8, bootstyle="warning").grid(
            row=0, column=col*2+1, padx=(0, 16))

    # ── Browse ────────────────────────────────────────────────────────────────

    def _browse(self, key: str, var: tk.StringVar):
        current = var.get().strip()
        initial = current if Path(current).exists() else str(Path.home())
        chosen  = filedialog.askdirectory(
            parent=self, title=f"Select {key} folder",
            initialdir=initial,
        )
        if chosen:
            var.set(str(Path(chosen)))

    # ── Region opener ─────────────────────────────────────────────────────────

    def _open_region(self, which: str):
        live_cfg = copy.deepcopy(self._cfg)
        live_cfg["paths"]["inbox"] = self._vars["paths.inbox"].get()
        label    = self._vars["mode"].get()
        mode     = self._mode_label_to_val.get(label, "mode2")
        RegionSelector(self, live_cfg, self._on_region_saved, mode=mode)

    def _on_region_saved(self, regions: dict):
        for tab in ("barcode", "ocr"):
            self._cfg.setdefault(tab, {})["region"] = regions[tab]
        self._update_region_labels()

    def _update_region_labels(self):
        for tab, lbl in (("barcode", self._region_lbl_barcode),
                         ("ocr",     self._region_lbl_ocr)):
            r = self._cfg.get(tab, {}).get("region", {})
            if r.get("enabled", False):
                lbl.config(
                    text=f"L{r.get('left_percent',0)}% T{r.get('top_percent',0)}% "
                         f"R{r.get('right_percent',100)}% B{r.get('bottom_percent',100)}%",
                    fg=ORANGE,
                )
            else:
                lbl.config(text="(disabled)", fg=TEXT_MUTED)

    # ── Populate / collect ────────────────────────────────────────────────────

    def _populate(self):
        c = self._cfg
        raw_mode = c.get("mode", "mode2")
        self._vars["mode"].set(
            self._mode_val_to_label.get(raw_mode, "Barcode + Trailing Pages"))
        self._on_mode_change()  # apply OCR section visibility

        for key in ("inbox","output","failed","archive"):
            self._vars[f"paths.{key}"].set(
                c.get("paths", {}).get(key, ""))

        fn = c.get("filename", {})
        for key in ("prefix","format","suffix","date_format"):
            self._vars[f"filename.{key}"].set(fn.get(key, ""))

        bc = c.get("barcode", {})
        self._vars["barcode.min_length"].set(str(bc.get("min_length", 6)))
        self._vars["barcode.max_length"].set(str(bc.get("max_length", 20)))
        self._vars["barcode.dpi"].set(str(bc.get("dpi", 200)))

        ocr = c.get("ocr", {})
        self._vars["ocr.language"].set(ocr.get("language", "eng"))
        self._vars["ocr.dpi"].set(str(ocr.get("dpi", 300)))
        self._vars["ocr.whitelist"].set(ocr.get("whitelist", ""))
        labels = ocr.get("field_labels", [])
        self._vars["ocr.field_labels"].set(", ".join(labels) if labels else "")

        of = c.get("output_folder", {})
        self._vars["output_folder.create_subfolder"].set(
            bool(of.get("create_subfolder", False)))
        self._vars["output_folder.subfolder_format"].set(
            of.get("subfolder_format", "%m%d"))

        self._update_region_labels()

    def _collect(self) -> dict:
        c = copy.deepcopy(self._cfg)
        label      = self._vars["mode"].get()
        c["mode"]  = self._mode_label_to_val.get(label, "mode2")

        for key in ("inbox","output","failed","archive"):
            c.setdefault("paths", {})[key] = self._vars[f"paths.{key}"].get().strip()

        fn = c.setdefault("filename", {})
        for key in ("prefix","format","suffix","date_format"):
            fn[key] = self._vars[f"filename.{key}"].get().strip()

        bc = c.setdefault("barcode", {})
        try: bc["min_length"] = int(self._vars["barcode.min_length"].get())
        except ValueError: pass
        try: bc["max_length"] = int(self._vars["barcode.max_length"].get())
        except ValueError: pass
        try: bc["dpi"] = int(self._vars["barcode.dpi"].get())
        except ValueError: pass
        
        ocr = c.setdefault("ocr", {})
        ocr["language"] = self._vars["ocr.language"].get().strip()
        try: ocr["dpi"] = int(self._vars["ocr.dpi"].get())
        except ValueError: pass
        ocr["whitelist"] = self._vars["ocr.whitelist"].get().strip()
        raw_labels = self._vars["ocr.field_labels"].get().strip()
        ocr["field_labels"] = (
            [l.strip() for l in raw_labels.split(",") if l.strip()]
            if raw_labels else []
        )

        of = c.setdefault("output_folder", {})
        of["create_subfolder"] = self._vars["output_folder.create_subfolder"].get()
        of["subfolder_format"] = self._vars["output_folder.subfolder_format"].get().strip()

        return c

    def _save(self):
        cfg = self._collect()
        _save_config(cfg)
        self._cleanup_parent_binding()
        self._on_save(cfg)
        self.destroy()


# ── Main window ───────────────────────────────────────────────────────────────

class App(ttk.Window):

    def __init__(self):
        super().__init__(themename="flatly")
        self.title("AutoDoc Reader")
        self.geometry("820x720")
        self.minsize(640, 560)
        self.configure(bg=WHITE)

        # Override ttkbootstrap primary colour → orange
        style = ttk.Style()
        style.configure("warning.TButton",
                        background=ORANGE, foreground=WHITE,
                        font=("Segoe UI", 10, "bold"))
        style.configure("TEntry", fieldbackground=WHITE)

        self._cfg           = _load_config()
        self._run_thread    = None
        self._settings_open = False

        _install_queue_handler()
        self._build_ui()
        self._poll_log()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ────────────────────────────────────────────────────────────
        header = tk.Frame(self, bg=ORANGE, pady=10, padx=16)
        header.pack(fill=tk.X)

        tk.Label(header, text="AutoDoc Reader",
                 bg=ORANGE, fg=WHITE,
                 font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)

        self._settings_btn = tk.Button(
            header, text="⚙  Settings",
            bg=ORANGE, fg=WHITE,
            font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, cursor="hand2",
            activebackground=ORANGE_DARK, activeforeground=WHITE,
            padx=12, pady=4,
            command=self._open_settings,
        )
        self._settings_btn.pack(side=tk.RIGHT)
        self._settings_btn.config(bg=ORANGE, fg=WHITE)

        # ── Log area ──────────────────────────────────────────────────────────
        log_frame = tk.Frame(self, bg=WHITE, padx=12, pady=8)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self._log_text = tk.Text(
            log_frame,
            wrap=tk.WORD,
            bg=LIGHT_GREY, fg=TEXT_DARK,
            font=("Consolas", 9),
            relief=tk.FLAT,
            borderwidth=1,
            state=tk.DISABLED,
        )
        sb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL,
                           command=self._log_text.yview,
                           bootstyle="warning-round")
        self._log_text.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._log_text.tag_configure("error",   foreground=RED_ERR)
        self._log_text.tag_configure("warning", foreground="#D97706")
        self._log_text.tag_configure("info",    foreground=TEXT_DARK)

        # ── Status / summary line ─────────────────────────────────────────────
        self._status_var = tk.StringVar(value="")
        self._status_lbl = tk.Label(
            self, textvariable=self._status_var,
            bg=WHITE, font=("Segoe UI", 9, "bold"), fg=TEXT_MUTED,
            anchor=tk.W, padx=14,
        )
        self._status_lbl.pack(fill=tk.X)

        # ── Bottom bar ────────────────────────────────────────────────────────
        bar = tk.Frame(self, bg=WHITE, pady=8, padx=12)
        bar.pack(fill=tk.X)

        self._run_btn = ttk.Button(
            bar, text="▶  Run", bootstyle="warning",
            width=10, command=self._run,
        )
        self._run_btn.pack(side=tk.LEFT)

        self._stop_btn = ttk.Button(
            bar, text="■  Stop", bootstyle="secondary",
            width=10, command=self._stop, state=tk.DISABLED,
        )
        self._stop_btn.pack(side=tk.LEFT, padx=(8, 0))

        ttk.Button(bar, text="Clear Log", bootstyle="secondary-outline",
                   command=self._clear_log).pack(side=tk.LEFT, padx=(16, 0))

    # ── Log polling ───────────────────────────────────────────────────────────

    def _poll_log(self):
        try:
            while True:
                msg = _log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def _append_log(self, msg: str):
        self._log_text.configure(state=tk.NORMAL)
        tag = "info"
        upper = msg.upper()
        if "ERROR" in upper:
            tag = "error"
        elif "WARNING" in upper or "WARN" in upper:
            tag = "warning"
        self._log_text.insert(tk.END, msg + "\n", tag)
        self._log_text.see(tk.END)
        self._log_text.configure(state=tk.DISABLED)

    def _clear_log(self):
        self._log_text.configure(state=tk.NORMAL)
        self._log_text.delete("1.0", tk.END)
        self._log_text.configure(state=tk.DISABLED)
        self._status_var.set("")

    # ── Run / Stop ────────────────────────────────────────────────────────────

    def _run(self):
        if self._run_thread and self._run_thread.is_alive():
            return
        self._status_var.set("")
        self._run_btn.configure(state=tk.DISABLED)
        self._stop_btn.configure(state=tk.NORMAL)
        self._stop_event = threading.Event()
        self._run_thread = threading.Thread(
            target=self._run_worker, daemon=True)
        self._run_thread.start()

    def _stop(self):
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
        self._append_log("--- Stop requested ---")
        self._run_btn.configure(state=tk.NORMAL)
        self._stop_btn.configure(state=tk.DISABLED)

    def _run_worker(self):
        try:
            import main as m
            cfg    = self._cfg
            run_ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")

            log = logging.getLogger()
            log.info("=" * 60)
            log.info(f"RUN STARTED  {run_ts}")
            log.info("=" * 60)

            m.housekeep_log(m.LOG_PATH, cfg)
            m.ensure_dirs(cfg)

            from pathlib import Path
            inbox = Path(cfg["paths"]["inbox"])
            if not inbox.exists():
                log.error(f"Inbox folder not found: {inbox}")
                self.after(0, messagebox.showerror, "Error",
                           f"Inbox folder not found:\n{inbox}\n\nPlease check Settings.")
                self.after(0, self._on_run_done, None)
                return

            pdf_files = sorted(
                [str(p) for p in inbox.iterdir()
                 if p.is_file() and p.suffix.lower() == ".pdf"]
            )
            total = len(pdf_files)

            if not pdf_files:
                log.info("No PDF files found in inbox.")
                self.after(0, messagebox.showerror, "Error",
                           "No PDF files found in\nInbox folder.")
                self.after(0, self._on_run_done, {"ok": 0, "fail": 0, "total": 0})
                return

            log.info(f"Found {total} PDF(s) in: {inbox}")

            mode = cfg.get("mode", "mode2").strip() or "mode2"
            log.info(f"Mode: {mode}")

            if mode == "mode1":
                stats = m.process_mode1(pdf_files, cfg, run_ts)
            elif mode == "mode2":
                stats = m.process_mode2(pdf_files, cfg, run_ts)
            elif mode == "mode3":
                stats = m.process_mode3(pdf_files, cfg, run_ts)
            else:
                log.error(f"Unknown mode '{mode}'")
                stats = {"ok": 0, "fail": 0}

            stats["total"] = total
            self.after(0, self._on_run_done, stats)

        except Exception as e:
            logging.getLogger().error(f"Unexpected error: {e}", exc_info=True)
            self.after(0, self._on_run_done, None)

    def _on_run_done(self, stats):
        self._run_btn.configure(state=tk.NORMAL)
        self._stop_btn.configure(state=tk.DISABLED)
        if stats is None:
            self._status_var.set("Run failed — check log for details.")
            self._status_lbl.configure(fg=RED_ERR)
        else:
            total = stats.get("total", stats.get("ok", 0) + stats.get("fail", 0))
            ok    = stats.get("ok",   0)
            fail  = stats.get("fail", 0)
            self._status_var.set(
                f"Completed — {total} file(s) processed   ✓ {ok}   ✗ {fail}"
            )
            self._status_lbl.configure(fg=GREEN_OK if fail == 0 else RED_ERR)

    # ── Settings ──────────────────────────────────────────────────────────────

    def _open_settings(self):
        if self._settings_open:
            if hasattr(self, "_settings_win"):
                self._settings_win.focus_force()
                _shake(self._settings_win)
            return
        if self._run_thread and self._run_thread.is_alive():
            messagebox.showwarning(
                "Settings", "Cannot open Settings while a run is in progress.",
                parent=self)
            return
        self._settings_open = True
        self._run_btn.configure(state=tk.DISABLED)

        def on_close(new_cfg=None):
            if not self._settings_open:
                return
            self._settings_open = False
            self._run_btn.configure(state=tk.NORMAL)
            if new_cfg:
                self._cfg = new_cfg

        win = SettingsWindow(self, self._cfg, on_save_callback=lambda cfg: on_close(cfg))
        self._settings_win = win
        win.protocol("WM_DELETE_WINDOW", win._on_cancel)
        win.bind("<Destroy>", lambda e: on_close() if e.widget is win else None)


# ── Entry ─────────────────────────────────────────────────────────────────────

def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()