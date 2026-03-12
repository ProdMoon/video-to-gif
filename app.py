# app.py — Video Crop → GIF Converter
# stdlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog
import tkinter as tk

# third-party
import customtkinter
import cv2
from PIL import Image, ImageTk


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class VideoState:
    video_path: str = ""
    raw_width: int = 0
    raw_height: int = 0
    rotation: int = 0          # degrees (e.g. -90, 0, 90, 180)
    display_width: int = 0     # after rotation
    display_height: int = 0
    duration: float = 0.0
    fps: float = 30.0

    start_time: float = 0.0
    end_time: float = 0.0
    crop_x: int = 0            # canvas coordinate space
    crop_y: int = 0
    crop_w: int = 0
    crop_h: int = 0
    canvas_scale: float = 1.0  # canvas_pixels / display_pixels

    out_fps: int = 10
    out_scale: int = 50        # percent
    out_path: str = ""

    def reset(self):
        """Reset selection state (for new file)."""
        self.start_time = 0.0
        self.end_time = self.duration
        self.crop_x = self.crop_y = self.crop_w = self.crop_h = 0
        self.canvas_scale = 1.0

    def video_coords_from_display(self, cx, cy, cw, ch, canvas_w, canvas_h):
        """Convert canvas crop rect to display-space video coordinates."""
        if self.canvas_scale <= 0:
            return (0, 0, self.display_width, self.display_height)
        vx = int(cx / self.canvas_scale)
        vy = int(cy / self.canvas_scale)
        vw = int(cw / self.canvas_scale)
        vh = int(ch / self.canvas_scale)
        vx = max(0, min(vx, self.display_width - vw))
        vy = max(0, min(vy, self.display_height - vh))
        vw = min(vw, self.display_width - vx)
        vh = min(vh, self.display_height - vy)
        return (vx, vy, vw, vh)


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

class VideoProbe:
    FFPROBE = "/opt/homebrew/bin/ffprobe"

    @staticmethod
    def probe(path: str) -> VideoState:
        """Run ffprobe and return a populated VideoState."""
        cmd = [
            VideoProbe.FFPROBE,
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            path,
        ]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        data = json.loads(result.stdout)

        streams = data.get("streams", [])
        fmt = data.get("format", {})

        # Find the video stream
        video_stream = None
        for s in streams:
            if s.get("codec_type") == "video":
                video_stream = s
                break
        if video_stream is None and streams:
            video_stream = streams[0]
        if video_stream is None:
            raise ValueError("No video stream found in file.")

        raw_width = video_stream.get("width", 0)
        raw_height = video_stream.get("height", 0)
        duration = float(fmt.get("duration", video_stream.get("duration", 0)))

        # Parse fps from avg_frame_rate e.g. "810000/26953"
        avg_fr = video_stream.get("avg_frame_rate", "30/1")
        try:
            num, den = avg_fr.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 30.0
        except Exception:
            fps = 30.0

        # Rotation: check side_data_list first, then tags
        rotation = 0
        side_data_list = video_stream.get("side_data_list", [])
        if side_data_list:
            rotation = int(side_data_list[0].get("rotation", 0))
        if rotation == 0:
            tags = video_stream.get("tags", {})
            rotation = int(tags.get("rotate", 0))

        if abs(rotation) == 90 or abs(rotation) == 270:
            display_width = raw_height
            display_height = raw_width
        else:
            display_width = raw_width
            display_height = raw_height

        state = VideoState(
            video_path=path,
            raw_width=raw_width,
            raw_height=raw_height,
            rotation=rotation,
            display_width=display_width,
            display_height=display_height,
            duration=duration,
            fps=fps,
        )
        state.end_time = duration
        state.out_path = f"output/{Path(path).stem}.gif"
        return state


# ---------------------------------------------------------------------------
# Frame extractor
# ---------------------------------------------------------------------------

class FrameExtractor:
    @staticmethod
    def get_frame(path: str, timestamp: float, rotation: int) -> Image.Image:
        """Extract and return a single PIL Image at timestamp (seconds)."""
        cap = cv2.VideoCapture(path)
        cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
        ret, frame = cap.read()
        cap.release()

        if not ret or frame is None:
            # Return blank frame on failure
            return Image.new("RGB", (320, 240), color=(0, 0, 0))

        # BGR → RGB
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # Apply rotation
        if rotation in (-90, 270):
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif rotation in (90, -270):
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        elif rotation in (180, -180):
            frame = cv2.rotate(frame, cv2.ROTATE_180)

        return Image.fromarray(frame)


# ---------------------------------------------------------------------------
# Crop overlay
# ---------------------------------------------------------------------------

class CropOverlay:
    def __init__(self, on_crop_changed: callable):
        self._canvas = None
        self._start = None      # (x, y) where drag began
        self._rect_id = None    # canvas rectangle item id
        self.on_crop_changed = on_crop_changed  # callback(cx, cy, cw, ch)

    def attach(self, canvas: tk.Canvas):
        """Bind mouse events to canvas."""
        self._canvas = canvas
        canvas.bind("<ButtonPress-1>", self._start_drag)
        canvas.bind("<B1-Motion>", self._update_drag)
        canvas.bind("<ButtonRelease-1>", self._end_drag)

    def _start_drag(self, event):
        self._start = (event.x, event.y)
        # Remove previous rectangle
        self._canvas.delete("crop_rect")
        self._rect_id = None

    def _update_drag(self, event):
        if self._start is None or self._canvas is None:
            return
        x1, y1 = self._start
        x2 = max(0, min(event.x, self._canvas.winfo_width()))
        y2 = max(0, min(event.y, self._canvas.winfo_height()))

        self._canvas.delete("crop_rect")
        self._rect_id = self._canvas.create_rectangle(
            x1, y1, x2, y2,
            dash=(4, 4),
            outline="red",
            width=2,
            tags=("crop_rect",),
        )

    def _end_drag(self, event):
        if self._start is None:
            return
        x1, y1 = self._start
        x2 = max(0, min(event.x, self._canvas.winfo_width()))
        y2 = max(0, min(event.y, self._canvas.winfo_height()))

        cx = min(x1, x2)
        cy = min(y1, y2)
        cw = abs(x2 - x1)
        ch = abs(y2 - y1)

        self.on_crop_changed(cx, cy, cw, ch)

    def clear(self):
        """Remove the rectangle and reset state."""
        if self._canvas is not None:
            self._canvas.delete("crop_rect")
        self._rect_id = None
        self._start = None


# ---------------------------------------------------------------------------
# Timeline sliders
# ---------------------------------------------------------------------------

class TimelineSliders:
    def __init__(self, parent, state: VideoState, on_change: callable):
        self.state = state
        self.on_change = on_change

        self.frame = customtkinter.CTkFrame(parent)
        self.frame.pack(fill="x", pady=(0, 6))

        # Start slider row
        start_row = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        start_row.pack(fill="x", padx=6, pady=2)
        customtkinter.CTkLabel(start_row, text="Start:", width=45).pack(side="left")
        self.start_slider = customtkinter.CTkSlider(
            start_row,
            from_=0,
            to=1,
            command=self._on_start_changed,
        )
        self.start_slider.pack(side="left", fill="x", expand=True, padx=(4, 4))
        self.start_time_label = customtkinter.CTkLabel(start_row, text="0.00s", width=60)
        self.start_time_label.pack(side="left")

        # End slider row
        end_row = customtkinter.CTkFrame(self.frame, fg_color="transparent")
        end_row.pack(fill="x", padx=6, pady=2)
        customtkinter.CTkLabel(end_row, text="End:", width=45).pack(side="left")
        self.end_slider = customtkinter.CTkSlider(
            end_row,
            from_=0,
            to=1,
            command=self._on_end_changed,
        )
        self.end_slider.pack(side="left", fill="x", expand=True, padx=(4, 4))
        self.end_time_label = customtkinter.CTkLabel(end_row, text="0.00s", width=60)
        self.end_time_label.pack(side="left")

    def setup(self, state: VideoState):
        """Configure sliders for a newly loaded video."""
        self.state = state
        duration = state.duration
        steps = min(int(duration * 20), 1000)

        self.start_slider.configure(from_=0, to=duration, number_of_steps=steps)
        self.end_slider.configure(from_=0, to=duration, number_of_steps=steps)
        self.start_slider.set(0)
        self.end_slider.set(duration)

        self.start_time_label.configure(text=f"0.00s")
        self.end_time_label.configure(text=f"{duration:.2f}s")

    def _on_start_changed(self, value):
        value = float(value)
        end_val = float(self.end_slider.get())
        if value > end_val - 0.05:
            value = max(0.0, end_val - 0.05)
            self.start_slider.set(value)
        self.state.start_time = value
        self.start_time_label.configure(text=f"{value:.2f}s")
        self.on_change(value)

    def _on_end_changed(self, value):
        value = float(value)
        start_val = float(self.start_slider.get())
        if value < start_val + 0.05:
            value = start_val + 0.05
            self.end_slider.set(value)
        self.state.end_time = value
        self.end_time_label.configure(text=f"{value:.2f}s")
        self.on_change(value)


# ---------------------------------------------------------------------------
# GIF converter
# ---------------------------------------------------------------------------

class GifConverter:
    FFMPEG = "/opt/homebrew/bin/ffmpeg"

    def convert(self, state: VideoState, on_progress: callable, on_done: callable):
        """Start conversion in a daemon thread."""
        threading.Thread(
            target=self._run,
            args=(state, on_progress, on_done),
            daemon=True,
        ).start()

    def _run(self, state: VideoState, on_progress: callable, on_done: callable):
        tmpdir = tempfile.mkdtemp()
        try:
            # Ensure output directory exists
            os.makedirs("output", exist_ok=True)

            # --- Coordinate conversion ---
            canvas_w = int(state.display_width * state.canvas_scale)
            canvas_h = int(state.display_height * state.canvas_scale)
            vx, vy, vw, vh = state.video_coords_from_display(
                state.crop_x, state.crop_y, state.crop_w, state.crop_h,
                canvas_w, canvas_h,
            )
            has_crop = vw >= 2 and vh >= 2

            # --- Build filter chain ---
            # Determine rotation filter
            if state.rotation in (-90, 270):
                rotation_filter = "transpose=clock"       # 90° clockwise fix
            elif state.rotation in (90, -270):
                rotation_filter = "transpose=cclock"      # 90° counter-clockwise fix
            elif state.rotation in (180, -180):
                rotation_filter = "transpose=clock,transpose=clock"  # 180°
            else:
                rotation_filter = None

            parts = []
            if rotation_filter:
                parts.append(rotation_filter)
            parts.append(f"fps={state.out_fps}")
            if has_crop:
                parts.append(f"crop={vw}:{vh}:{vx}:{vy}")   # before scale (Fix 3)
            parts.append(f"scale=iw*{state.out_scale / 100:.2f}:-2:flags=lanczos")  # -2 (Fix 4)

            base_filter = ",".join(parts)

            start = state.start_time
            duration = state.end_time - state.start_time
            palette_path = os.path.join(tmpdir, "palette.png")

            # --- Pass 1: palette ---
            on_progress(0.1, "Generating palette\u2026")
            pass1_filter = f"{base_filter},palettegen=stats_mode=diff"
            cmd1 = [
                self.FFMPEG, "-y",
                "-ss", str(start),
                "-t", str(duration),
                "-i", state.video_path,
                "-vf", pass1_filter,
                palette_path,
            ]
            subprocess.run(cmd1, check=True, capture_output=True)

            # --- Pass 2: GIF ---
            on_progress(0.5, "Rendering GIF\u2026")
            pass2_lavfi = (
                f"{base_filter} [x]; "
                f"[x][1:v] paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
            )
            cmd2 = [
                self.FFMPEG, "-y",
                "-ss", str(start),
                "-t", str(duration),
                "-i", state.video_path,
                "-i", palette_path,
                "-lavfi", pass2_lavfi,
                state.out_path,
            ]
            subprocess.run(cmd2, check=True, capture_output=True)

            on_progress(1.0, "Done")
            on_done(state.out_path, None)

        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else str(e)
            on_done(None, stderr[:300])
        except Exception as e:
            on_done(None, str(e))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class App(customtkinter.CTk):
    def __init__(self):
        super().__init__()
        self.title("Video → GIF Converter")
        self.geometry("960x780")
        self.state_obj = VideoState()
        self.photo = None  # MUST keep reference to prevent GC
        self.crop_overlay = CropOverlay(self._on_crop_changed)
        self.gif_converter = GifConverter()
        self._build_ui()

        # Ensure output directory exists at startup
        os.makedirs("output", exist_ok=True)

    def _build_ui(self):
        main_frame = customtkinter.CTkFrame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # --- ROW 0: top bar ---
        top_bar = customtkinter.CTkFrame(main_frame, fg_color="transparent")
        top_bar.pack(side="top", fill="x", pady=(0, 6))

        open_btn = customtkinter.CTkButton(top_bar, text="파일 열기", command=self._open_file)
        open_btn.pack(side="left", padx=(0, 8))

        self.file_label = customtkinter.CTkLabel(top_bar, text="No file loaded")
        self.file_label.pack(side="left")

        # --- ROW 1: content area ---
        content_area = customtkinter.CTkFrame(main_frame, fg_color="transparent")
        content_area.pack(side="top", fill="both", expand=True)

        # COL 0: preview panel
        preview_panel = customtkinter.CTkFrame(content_area, width=400)
        preview_panel.pack(side="left", fill="y", padx=(0, 4))
        preview_panel.pack_propagate(False)

        customtkinter.CTkLabel(preview_panel, text="Preview").pack(pady=(4, 2))

        self.canvas = tk.Canvas(preview_panel, bg="black", width=360, height=640)
        self.canvas.pack(padx=4, pady=4)

        # COL 1: controls panel
        controls_panel = customtkinter.CTkScrollableFrame(content_area)
        controls_panel.pack(side="left", fill="both", expand=True, padx=(4, 0))

        # --- Time Range ---
        customtkinter.CTkLabel(
            controls_panel, text="[Time Range]", font=("", 13, "bold")
        ).pack(anchor="w", pady=(4, 2))

        self.timeline_sliders = TimelineSliders(
            controls_panel, self.state_obj, self._on_timeline_changed
        )

        # --- Crop Region ---
        customtkinter.CTkLabel(
            controls_panel, text="[Crop Region]", font=("", 13, "bold")
        ).pack(anchor="w", pady=(8, 2))

        self.crop_info_label = customtkinter.CTkLabel(
            controls_panel, text="None — full frame"
        )
        self.crop_info_label.pack(anchor="w", padx=6)

        clear_crop_btn = customtkinter.CTkButton(
            controls_panel, text="Clear Crop", command=self._clear_crop, width=100
        )
        clear_crop_btn.pack(anchor="w", padx=6, pady=(2, 0))

        # --- Output Settings ---
        customtkinter.CTkLabel(
            controls_panel, text="[Output Settings]", font=("", 13, "bold")
        ).pack(anchor="w", pady=(8, 2))

        # FPS slider
        fps_row = customtkinter.CTkFrame(controls_panel, fg_color="transparent")
        fps_row.pack(fill="x", padx=6, pady=2)
        self.fps_label = customtkinter.CTkLabel(fps_row, text="FPS: 10", width=70)
        self.fps_label.pack(side="left")
        self.fps_slider = customtkinter.CTkSlider(
            fps_row,
            from_=5,
            to=30,
            number_of_steps=25,
            command=self._on_fps_changed,
        )
        self.fps_slider.set(10)
        self.fps_slider.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Scale slider
        scale_row = customtkinter.CTkFrame(controls_panel, fg_color="transparent")
        scale_row.pack(fill="x", padx=6, pady=2)
        self.scale_label = customtkinter.CTkLabel(scale_row, text="Scale: 50%", width=70)
        self.scale_label.pack(side="left")
        self.scale_slider = customtkinter.CTkSlider(
            scale_row,
            from_=10,
            to=100,
            number_of_steps=18,
            command=self._on_scale_changed,
        )
        self.scale_slider.set(50)
        self.scale_slider.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Output path
        path_row = customtkinter.CTkFrame(controls_panel, fg_color="transparent")
        path_row.pack(fill="x", padx=6, pady=2)
        customtkinter.CTkLabel(path_row, text="Output:", width=55).pack(side="left")
        self.output_path_entry = customtkinter.CTkEntry(path_row)
        self.output_path_entry.pack(side="left", fill="x", expand=True, padx=(4, 4))
        browse_btn = customtkinter.CTkButton(
            path_row, text="Browse", width=70, command=self._browse_output
        )
        browse_btn.pack(side="left")

        # Convert button
        self.convert_btn = customtkinter.CTkButton(
            controls_panel,
            text="GIF로 변환",
            fg_color="green",
            command=self._start_conversion,
        )
        self.convert_btn.pack(fill="x", padx=6, pady=(10, 4))

        # Progress bar (initially hidden)
        self.progress_bar = customtkinter.CTkProgressBar(controls_panel)
        self.progress_bar.set(0)
        # Not packed yet — shown when conversion starts

        # Status label
        self.status_label = customtkinter.CTkLabel(
            controls_panel, text="Ready.", anchor="w"
        )
        self.status_label.pack(fill="x", padx=6, pady=(2, 0))

        # --- ROW 2: footer ---
        footer = customtkinter.CTkLabel(
            main_frame, text="Video → GIF Converter v1.0", text_color="gray"
        )
        footer.pack(side="top", pady=(6, 0))

    # -----------------------------------------------------------------------
    # Event handlers
    # -----------------------------------------------------------------------

    def _open_file(self):
        path = filedialog.askopenfilename(
            filetypes=[("MP4 files", "*.mp4"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            self.state_obj = VideoProbe.probe(path)
            self.file_label.configure(text=os.path.basename(path))
            self._setup_canvas()
            self.timeline_sliders.setup(self.state_obj)
            self._update_preview(0.0)
            self.crop_info_label.configure(text="None — full frame")
            self.status_label.configure(text="Video loaded.")
            # Populate output path entry
            self.output_path_entry.delete(0, "end")
            self.output_path_entry.insert(0, self.state_obj.out_path)
        except Exception as e:
            self.status_label.configure(text=f"Error: {e}")

    def _setup_canvas(self):
        """Resize canvas to fit the video, attach CropOverlay."""
        canvas_max_w, canvas_max_h = 360, 640
        s = self.state_obj
        scale = min(canvas_max_w / s.display_width, canvas_max_h / s.display_height)
        canvas_w = int(s.display_width * scale)
        canvas_h = int(s.display_height * scale)
        s.canvas_scale = scale
        self.canvas.configure(width=canvas_w, height=canvas_h)
        self.crop_overlay.attach(self.canvas)
        self.crop_overlay.clear()

    def _update_preview(self, timestamp: float):
        s = self.state_obj
        if not s.video_path:
            return
        img = FrameExtractor.get_frame(s.video_path, timestamp, s.rotation)
        canvas_w = int(s.display_width * s.canvas_scale)
        canvas_h = int(s.display_height * s.canvas_scale)
        img = img.resize((canvas_w, canvas_h), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(img)  # Keep reference
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        s = self.state_obj
        if s.crop_w > 0 and s.crop_h > 0:
            self.canvas.create_rectangle(
                s.crop_x, s.crop_y,
                s.crop_x + s.crop_w, s.crop_y + s.crop_h,
                outline="red", width=2, dash=(4, 4),
                tags=("crop_rect",),
            )

    def _on_timeline_changed(self, timestamp: float):
        self._update_preview(timestamp)

    def _on_crop_changed(self, cx, cy, cw, ch):
        s = self.state_obj
        s.crop_x, s.crop_y, s.crop_w, s.crop_h = cx, cy, cw, ch
        self.crop_info_label.configure(text=f"x={cx} y={cy} w={cw} h={ch}")

    def _clear_crop(self):
        s = self.state_obj
        s.crop_x = s.crop_y = s.crop_w = s.crop_h = 0
        self.crop_overlay.clear()
        self.crop_info_label.configure(text="None — full frame")
        self._update_preview(self.state_obj.start_time)

    def _on_fps_changed(self, v):
        self.fps_label.configure(text=f"FPS: {int(float(v))}")

    def _on_scale_changed(self, v):
        self.scale_label.configure(text=f"Scale: {int(float(v))}%")

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".gif",
            filetypes=[("GIF files", "*.gif")],
        )
        if path:
            self.output_path_entry.delete(0, "end")
            self.output_path_entry.insert(0, path)

    def _start_conversion(self):
        s = self.state_obj
        if not s.video_path:
            self.status_label.configure(text="No video loaded.")
            return
        # Update state from UI controls
        s.out_fps = int(self.fps_slider.get())
        s.out_scale = int(self.scale_slider.get())
        entry_path = self.output_path_entry.get().strip()
        if entry_path:
            s.out_path = entry_path
        if not s.out_path:
            self.status_label.configure(text="No output path set.")
            return

        os.makedirs("output", exist_ok=True)

        # Show progress bar
        self.progress_bar.pack(fill="x", padx=10, pady=5)
        self.progress_bar.set(0)
        self.convert_btn.configure(state="disabled")

        def on_progress(value, msg):
            self.after(0, lambda: self.progress_bar.set(value))
            self.after(0, lambda: self.status_label.configure(text=msg))

        def on_done(path, error):
            def _update():
                self.convert_btn.configure(state="normal")
                if error:
                    self.status_label.configure(text=f"Error: {error}")
                else:
                    self.status_label.configure(text=f"Saved: {path}")
                    self.progress_bar.set(1.0)
                # Hide progress bar after a short delay
                self.after(2000, lambda: self.progress_bar.pack_forget())
            self.after(0, _update)

        self.gif_converter.convert(s, on_progress, on_done)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    customtkinter.set_appearance_mode("dark")
    customtkinter.set_default_color_theme("blue")
    app = App()
    app.mainloop()
