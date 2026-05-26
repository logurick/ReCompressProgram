from __future__ import annotations

import io
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from recompress import ArchiveImageSummary, find_archives, inspect_archive_images, recompress_paths

try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # Drag-and-drop is optional at import time.
    DND_FILES = None
    TkinterDnD = None


class ArchiveTooltip:
    def __init__(self, parent: tk.Tk) -> None:
        self.parent = parent
        self.window: tk.Toplevel | None = None
        self.photo: object | None = None

    def show(self, x: int, y: int, summary: ArchiveImageSummary | None) -> None:
        self.hide()
        self.window = tk.Toplevel(self.parent)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x + 16}+{y + 16}")

        frame = ttk.Frame(self.window, padding=8, relief="solid", borderwidth=1)
        frame.grid(row=0, column=0)

        if summary and summary.first_image_png and Image and ImageTk:
            image = Image.open(io.BytesIO(summary.first_image_png))
            self.photo = ImageTk.PhotoImage(image)
            ttk.Label(frame, image=self.photo).grid(row=0, column=0)
        else:
            message = summary.message if summary else "画像情報を確認中です。"
            ttk.Label(frame, text=message or "画像が見つかりません。").grid(row=0, column=0)

    def hide(self) -> None:
        if self.window:
            self.window.destroy()
            self.window = None
        self.photo = None


class ReCompressApp:
    def __init__(self) -> None:
        root_class = TkinterDnD.Tk if TkinterDnD else tk.Tk
        self.root = root_class()
        self.root.title("ReCompress Program")
        self.root.geometry("920x620")
        self.root.minsize(760, 520)

        self.paths: list[Path] = []
        self.item_paths: dict[str, Path] = {}
        self.summaries: dict[Path, ArchiveImageSummary] = {}
        self.exclude_zip = tk.BooleanVar(value=False)
        self.split_wide_images = tk.BooleanVar(value=False)
        self.split_order = tk.StringVar(value="right_left")
        self.running = False
        self.event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.tooltip = ArchiveTooltip(self.root)

        self.build_ui()
        self.root.after(100, self.process_queue)

    def build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(16, 16, 16, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        title = ttk.Label(header, text="圧縮ファイルをZIPへ再圧縮", font=("", 16, "bold"))
        title.grid(row=0, column=0, sticky="w")

        controls = ttk.Frame(header)
        controls.grid(row=0, column=1, sticky="e")
        ttk.Button(controls, text="ファイル追加", command=self.add_files).grid(row=0, column=0, padx=(0, 8))
        ttk.Button(controls, text="フォルダ追加", command=self.add_folder).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(controls, text="削除", command=self.remove_selected).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(controls, text="クリア", command=self.clear_paths).grid(row=0, column=3)

        main = ttk.Frame(self.root, padding=(16, 8, 16, 8))
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)
        main.rowconfigure(3, weight=1)

        columns = ("path", "wide_count")
        self.path_tree = ttk.Treeview(main, columns=columns, show="headings", selectmode="extended")
        self.path_tree.heading("path", text="圧縮ファイル")
        self.path_tree.heading("wide_count", text="横長画像数")
        self.path_tree.column("path", width=700, minwidth=260, stretch=True)
        self.path_tree.column("wide_count", width=100, minwidth=90, stretch=False, anchor="center")
        self.path_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(main, orient="vertical", command=self.path_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.path_tree.configure(yscrollcommand=scrollbar.set)
        self.path_tree.bind("<Button-3>", self.handle_tree_right_click)
        self.path_tree.bind("<Leave>", self.hide_tooltip)

        if DND_FILES:
            self.path_tree.drop_target_register(DND_FILES)
            self.path_tree.dnd_bind("<<Drop>>", self.handle_drop)

        image_options = ttk.LabelFrame(main, text="画像分割オプション", padding=8)
        image_options.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        image_options.columnconfigure(3, weight=1)
        ttk.Checkbutton(
            image_options,
            text="横長画像を中央で左右に分割する",
            variable=self.split_wide_images,
        ).grid(row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Label(image_options, text="ファイル名順").grid(row=0, column=1, sticky="w", padx=(0, 8))
        ttk.Radiobutton(
            image_options,
            text="右側→左側",
            value="right_left",
            variable=self.split_order,
        ).grid(row=0, column=2, sticky="w", padx=(0, 8))
        ttk.Radiobutton(
            image_options,
            text="左側→右側",
            value="left_right",
            variable=self.split_order,
        ).grid(row=0, column=3, sticky="w")

        options = ttk.Frame(main, padding=(0, 10, 0, 10))
        options.grid(row=2, column=0, columnspan=2, sticky="ew")
        options.columnconfigure(1, weight=1)
        ttk.Checkbutton(options, text="zipファイルを対象外にする", variable=self.exclude_zip).grid(row=0, column=0, sticky="w")
        self.start_button = ttk.Button(options, text="再圧縮開始", command=self.start_recompress)
        self.start_button.grid(row=0, column=2, sticky="e")

        log_frame = ttk.LabelFrame(main, text="ログ", padding=8)
        log_frame.grid(row=3, column=0, columnspan=2, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scrollbar.set)

        self.status = ttk.Label(self.root, text="ファイルまたはフォルダを追加してください。", padding=(16, 8))
        self.status.grid(row=2, column=0, sticky="ew")

    def add_files(self) -> None:
        filenames = filedialog.askopenfilenames(title="圧縮ファイルを選択")
        self.add_paths(Path(name) for name in filenames)

    def add_folder(self) -> None:
        folder = filedialog.askdirectory(title="フォルダを選択")
        if folder:
            self.add_paths([Path(folder)])

    def handle_drop(self, event: object) -> None:
        raw_paths = self.root.tk.splitlist(event.data)  # type: ignore[attr-defined]
        self.add_paths(Path(path) for path in raw_paths)

    def add_paths(self, paths: object) -> None:
        archives = find_archives((Path(path) for path in paths), exclude_zip=self.exclude_zip.get())
        existing = {path.resolve() for path in self.paths if path.exists()}
        added = 0
        for archive in archives:
            resolved = archive.resolve()
            if resolved in existing:
                continue
            self.paths.append(resolved)
            existing.add(resolved)
            item_id = self.path_tree.insert("", tk.END, values=(str(resolved), "確認中"))
            self.item_paths[item_id] = resolved
            added += 1
            threading.Thread(target=self.inspect_worker, args=(resolved,), daemon=True).start()

        self.status.configure(text=f"{added}件追加しました。合計 {len(self.paths)}件です。")

    def inspect_worker(self, path: Path) -> None:
        summary = inspect_archive_images(path)
        self.event_queue.put(("summary", summary))

    def remove_selected(self) -> None:
        selected = self.path_tree.selection()
        if not selected:
            return
        selected_paths = {self.item_paths[item] for item in selected if item in self.item_paths}
        self.paths = [path for path in self.paths if path not in selected_paths]
        for item in selected:
            self.item_paths.pop(item, None)
            self.path_tree.delete(item)
        self.status.configure(text=f"合計 {len(self.paths)}件です。")

    def clear_paths(self) -> None:
        self.paths.clear()
        self.item_paths.clear()
        self.summaries.clear()
        for item in self.path_tree.get_children():
            self.path_tree.delete(item)
        self.hide_tooltip()
        self.status.configure(text="ファイルまたはフォルダを追加してください。")

    def start_recompress(self) -> None:
        if self.running:
            return
        if not self.paths:
            messagebox.showinfo("対象なし", "ファイルまたはフォルダを追加してください。")
            return

        self.running = True
        self.start_button.configure(state="disabled")
        self.status.configure(text="処理中です...")
        self.append_log("=== Recompress started ===")

        worker = threading.Thread(target=self.run_worker, daemon=True)
        worker.start()

    def run_worker(self) -> None:
        def log(message: str) -> None:
            self.event_queue.put(("log", message))

        results = recompress_paths(
            self.paths,
            exclude_zip=False,
            split_wide_images=self.split_wide_images.get(),
            split_order=self.split_order.get(),
            log=log,
        )
        self.event_queue.put(("done", results))

    def process_queue(self) -> None:
        try:
            while True:
                event, payload = self.event_queue.get_nowait()
                if event == "log":
                    self.append_log(str(payload))
                elif event == "summary":
                    self.handle_summary(payload)
                elif event == "done":
                    self.handle_done(payload)
        except queue.Empty:
            pass
        self.root.after(100, self.process_queue)

    def handle_summary(self, payload: object) -> None:
        summary = payload
        if not isinstance(summary, ArchiveImageSummary):
            return
        self.summaries[summary.source] = summary
        for item, path in self.item_paths.items():
            if path == summary.source:
                value = "-" if summary.wide_image_count is None else str(summary.wide_image_count)
                self.path_tree.set(item, "wide_count", value)
                break

    def handle_tree_right_click(self, event: tk.Event) -> None:
        item = self.path_tree.identify_row(event.y)
        self.hide_tooltip()
        if not item:
            return
        self.path_tree.selection_set(item)
        self.show_tooltip(item, event.x_root, event.y_root)

    def show_tooltip(self, item: str, x: int, y: int) -> None:
        path = self.item_paths.get(item)
        if not path:
            return
        summary = self.summaries.get(path)
        self.tooltip.show(x, y, summary)

    def hide_tooltip(self, _event: object | None = None) -> None:
        self.tooltip.hide()

    def handle_done(self, payload: object) -> None:
        self.running = False
        self.start_button.configure(state="normal")
        results = list(payload)
        if not results:
            self.append_log("対象となる圧縮ファイルはありませんでした。")
            self.status.configure(text="対象となる圧縮ファイルはありませんでした。")
            return

        ok_count = sum(1 for result in results if result.status == "ok")
        failed_count = sum(1 for result in results if result.status == "failed")
        for result in results:
            if result.status == "ok":
                self.append_log(f"OK: {result.source} -> {result.output}")
            else:
                self.append_log(f"FAILED: {result.source} ({result.message})")
        self.status.configure(text=f"完了: 成功 {ok_count}件 / 失敗 {failed_count}件")
        messagebox.showinfo("完了", f"成功 {ok_count}件 / 失敗 {failed_count}件")

    def append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    ReCompressApp().run()
