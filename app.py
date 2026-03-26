import json
import math
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import ttk
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
import requests
from PIL import Image, ImageTk


class ObsEventHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = []
        self._latest_payload = {
            "upper": "",
            "lower": "",
            "raw_upper": "",
            "raw_lower": "",
            "ts": time.time(),
        }

    def subscribe(self):
        subscriber = queue.Queue(maxsize=8)
        with self._lock:
            self._subscribers.append(subscriber)
            latest = dict(self._latest_payload)
        return subscriber, latest

    def unsubscribe(self, subscriber):
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def publish(self, payload):
        message = dict(payload)
        message["ts"] = time.time()
        with self._lock:
            self._latest_payload = message
            subscribers = list(self._subscribers)

        for subscriber in subscribers:
            try:
                subscriber.put_nowait(message)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                try:
                    subscriber.put_nowait(message)
                except queue.Full:
                    pass

    def snapshot(self):
        with self._lock:
            return dict(self._latest_payload)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class ObsBrowserServer:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.hub = ObsEventHub()
        self.httpd = None
        self.thread = None
        self.template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "obs_overlay.html")

    def start(self):
        if self.httpd:
            return

        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "SevenSegObsServer/1.0"

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._serve_overlay_html(parsed)
                    return
                if parsed.path == "/events":
                    self._serve_sse()
                    return
                if parsed.path == "/state":
                    self._serve_json(server_ref.hub.snapshot())
                    return
                self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

            def do_POST(self):
                parsed = urlparse(self.path)
                if parsed.path != "/api/publish":
                    self.send_error(HTTPStatus.NOT_FOUND, "Not Found")
                    return

                content_length = int(self.headers.get("Content-Length", "0") or "0")
                raw_body = self.rfile.read(content_length)
                try:
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
                    return

                normalized = {
                    "upper": str(payload.get("upper", "")),
                    "lower": str(payload.get("lower", "")),
                    "raw_upper": str(payload.get("raw_upper", payload.get("upper", ""))),
                    "raw_lower": str(payload.get("raw_lower", payload.get("lower", ""))),
                }
                server_ref.hub.publish(normalized)
                self._serve_json({"ok": True, "state": server_ref.hub.snapshot()})

            def log_message(self, _format, *_args):
                return

            def _serve_overlay_html(self, parsed):
                params = parse_qs(parsed.query)
                target = params.get("target", ["both"])[0]
                html = server_ref.build_overlay_html(target)
                body = html.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_json(self, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_sse(self):
                subscriber, latest = server_ref.hub.subscribe()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                try:
                    self._write_sse_message(latest)
                    while True:
                        try:
                            payload = subscriber.get(timeout=15)
                            self._write_sse_message(payload)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    server_ref.hub.unsubscribe(subscriber)

            def _write_sse_message(self, payload):
                body = json.dumps(payload, ensure_ascii=False)
                message = f"event: update\ndata: {body}\n\n".encode("utf-8")
                self.wfile.write(message)
                self.wfile.flush()

        self.httpd = QuietThreadingHTTPServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def build_overlay_html(self, target):
        safe_target = target if target in {"upper", "lower", "both"} else "both"
        with open(self.template_path, "r", encoding="utf-8") as fh:
            template = fh.read()
        return template.replace("__TARGET__", json.dumps(safe_target))

    def stop(self):
        if not self.httpd:
            return
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=1.0)
        self.httpd = None
        self.thread = None

    def publish(self, upper, lower, raw_upper="", raw_lower=""):
        self.hub.publish(
            {
                "upper": upper,
                "lower": lower,
                "raw_upper": raw_upper,
                "raw_lower": raw_lower,
            }
        )


class SevenSegReaderApp:
    PREVIEW_WIDTH = 640
    PREVIEW_HEIGHT = 360
    SETTINGS_FILE = "app_settings.json"

    DIGITS_LOOKUP = {
        (1, 1, 1, 1, 1, 1, 0): "0",
        (0, 1, 1, 0, 0, 0, 0): "1",
        (1, 1, 0, 1, 1, 0, 1): "2",
        (1, 1, 1, 1, 0, 0, 1): "3",
        (0, 1, 1, 0, 0, 1, 1): "4",
        (1, 0, 1, 1, 0, 1, 1): "5",
        (1, 0, 1, 1, 1, 1, 1): "6",
        (1, 1, 1, 0, 0, 0, 0): "7",
        (1, 1, 1, 1, 1, 1, 1): "8",
        (1, 1, 1, 1, 0, 1, 1): "9",
    }

    def __init__(self, root):
        self.root = root
        self.root.title("7セグLED 読み取りツール")
        self.root.geometry("1240x900")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.cap = None
        self.video_loop_id = None
        self.process_loop_id = None
        self.debug_loop_id = None
        self.live_loop_id = None
        self.image_on_canvas = None
        self.photo = None
        self.current_frame = None
        self.is_processing = False

        self.roi_target = None
        self.rois = {"upper": None, "lower": None}
        self.rects = {"upper": None, "lower": None}
        self.start_x = 0
        self.start_y = 0

        self.debug_photo_upper = None
        self.debug_photo_lower = None

        self.obs_out_var = tk.BooleanVar(value=False)
        self.debug_continuous_var = tk.BooleanVar(value=False)
        self.live_continuous_var = tk.BooleanVar(value=False)
        self.fisheye_var = tk.BooleanVar(value=False)
        self.image_skew_var = tk.BooleanVar(value=False)
        self.adaptive_thresh_var = tk.BooleanVar(value=True)
        self.segment_score_var = tk.BooleanVar(value=True)
        self.auto_digit_bounds_var = tk.BooleanVar(value=False)
        self.auto_digit_fill_var = tk.DoubleVar(value=8.0)
        self.decimal_area_var = tk.DoubleVar(value=12.0)

        self.last_text = {"upper": "", "lower": ""}
        self.last_save_time = {"upper": 0.0, "lower": 0.0}
        self.current_val = {"upper": "", "lower": ""}
        self.raw_val = {"upper": "", "lower": ""}
        self.stable_val = {"upper": "", "lower": ""}
        self.pending_val = {"upper": "", "lower": ""}
        self.pending_count = {"upper": 0, "lower": 0}
        self.invalid_count = {"upper": 0, "lower": 0}
        self.last_obs_payload = None
        self.obs_server = None
        self.camera_status_var = tk.StringVar(value="未接続")
        self.processing_status_var = tk.StringVar(value="停止")
        self.obs_server_status_var = tk.StringVar(value="停止")
        self.obs_publish_status_var = tk.StringVar(value="待機中")

        self.roi_angle_vars = {
            "upper": tk.DoubleVar(value=0.0),
            "lower": tk.DoubleVar(value=0.0),
        }
        self.tilt_scales = {}

        self.settings_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), self.SETTINGS_FILE)

        self.main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.main_paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.left_frame = ttk.LabelFrame(self.main_paned, text="映像入力・ROI")
        self.main_paned.add(self.left_frame, weight=3)

        conn_frame = ttk.Frame(self.left_frame)
        conn_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Label(conn_frame, text="RTSP/カメラ:").pack(side=tk.LEFT)
        self.url_entry = ttk.Entry(conn_frame, width=36)
        self.url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(conn_frame, text="接続", command=self.connect_camera).pack(side=tk.LEFT, padx=2)
        ttk.Button(conn_frame, text="切断", command=self.disconnect_camera).pack(side=tk.LEFT, padx=2)

        preview_frame = ttk.Frame(self.left_frame, height=self.PREVIEW_HEIGHT + 10)
        preview_frame.pack(fill=tk.X, padx=5, pady=5)
        preview_frame.pack_propagate(False)

        self.canvas = tk.Canvas(
            preview_frame,
            bg="black",
            width=self.PREVIEW_WIDTH,
            height=self.PREVIEW_HEIGHT,
            highlightthickness=0,
        )
        self.canvas.pack(anchor=tk.CENTER)
        self.canvas.bind("<ButtonPress-1>", self.on_button_press)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_button_release)

        roi_frame = ttk.Frame(self.left_frame)
        roi_frame.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(roi_frame, text="上段のROI", command=lambda: self.set_roi_target("upper")).pack(side=tk.LEFT, padx=2)
        ttk.Button(roi_frame, text="下段のROI", command=lambda: self.set_roi_target("lower")).pack(side=tk.LEFT, padx=2)
        ttk.Button(roi_frame, text="リセット", command=self.reset_roi).pack(side=tk.LEFT, padx=2)

        prep_frame = ttk.LabelFrame(self.left_frame, text="映像補正")
        prep_frame.pack(fill=tk.X, padx=5, pady=(0, 5))
        ttk.Checkbutton(prep_frame, text="レンズ補正", variable=self.fisheye_var).grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        self.fisheye_scale = ttk.Scale(prep_frame, from_=-50, to=50, orient=tk.HORIZONTAL)
        self.fisheye_scale.set(-15)
        self.fisheye_scale.grid(row=0, column=1, sticky=tk.EW, padx=5, pady=2)

        ttk.Checkbutton(prep_frame, text="画像斜め補正", variable=self.image_skew_var).grid(row=1, column=0, sticky=tk.W, padx=5, pady=2)
        self.image_skew_scale = ttk.Scale(prep_frame, from_=-150, to=150, orient=tk.HORIZONTAL)
        self.image_skew_scale.set(0)
        self.image_skew_scale.grid(row=1, column=1, sticky=tk.EW, padx=5, pady=2)

        ttk.Label(prep_frame, text="上段ROI角度").grid(row=2, column=0, sticky=tk.W, padx=5, pady=2)
        self.tilt_scales["upper"] = ttk.Scale(prep_frame, from_=-10, to=10, orient=tk.HORIZONTAL, variable=self.roi_angle_vars["upper"], command=lambda _value: self.redraw_rois())
        self.tilt_scales["upper"].grid(row=2, column=1, sticky=tk.EW, padx=5, pady=2)
        ttk.Label(prep_frame, text="-10° .. +10°").grid(row=2, column=2, sticky=tk.W, padx=(0, 5), pady=2)

        ttk.Label(prep_frame, text="下段ROI角度").grid(row=3, column=0, sticky=tk.W, padx=5, pady=2)
        self.tilt_scales["lower"] = ttk.Scale(prep_frame, from_=-10, to=10, orient=tk.HORIZONTAL, variable=self.roi_angle_vars["lower"], command=lambda _value: self.redraw_rois())
        self.tilt_scales["lower"].grid(row=3, column=1, sticky=tk.EW, padx=5, pady=2)
        ttk.Label(prep_frame, text="-10° .. +10°").grid(row=3, column=2, sticky=tk.W, padx=(0, 5), pady=2)
        prep_frame.columnconfigure(1, weight=1)

        res_frame = ttk.LabelFrame(self.left_frame, text="認識結果・デバッグ画像")
        res_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        ttk.Label(res_frame, text="上段").pack(anchor=tk.W, padx=5)
        self.upper_lbl = ttk.Label(res_frame, text="------", font=("Courier", 24, "bold"), foreground="red", background="black")
        self.upper_lbl.pack(padx=5, pady=2)
        self.upper_img_lbl = tk.Label(res_frame, bg="gray")
        self.upper_img_lbl.pack(padx=5, pady=5, fill=tk.X)

        ttk.Label(res_frame, text="下段").pack(anchor=tk.W, padx=5, pady=(5, 0))
        self.lower_lbl = ttk.Label(res_frame, text="------", font=("Courier", 24, "bold"), foreground="green", background="black")
        self.lower_lbl.pack(padx=5, pady=2)
        self.lower_img_lbl = tk.Label(res_frame, bg="gray")
        self.lower_img_lbl.pack(padx=5, pady=5, fill=tk.X)

        runtime_frame = ttk.LabelFrame(self.left_frame, text="現在の状態")
        runtime_frame.pack(fill=tk.X, padx=5, pady=(0, 5))
        ttk.Label(runtime_frame, text="カメラ").grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        ttk.Label(runtime_frame, textvariable=self.camera_status_var).grid(row=0, column=1, sticky=tk.W, padx=5, pady=2)
        ttk.Label(runtime_frame, text="認識").grid(row=1, column=0, sticky=tk.W, padx=5, pady=2)
        ttk.Label(runtime_frame, textvariable=self.processing_status_var).grid(row=1, column=1, sticky=tk.W, padx=5, pady=2)
        ttk.Label(runtime_frame, text="OBS配信").grid(row=2, column=0, sticky=tk.W, padx=5, pady=2)
        ttk.Label(runtime_frame, textvariable=self.obs_server_status_var).grid(row=2, column=1, sticky=tk.W, padx=5, pady=2)
        ttk.Label(runtime_frame, text="配信内容").grid(row=3, column=0, sticky=tk.W, padx=5, pady=2)
        ttk.Label(runtime_frame, textvariable=self.obs_publish_status_var).grid(row=3, column=1, sticky=tk.W, padx=5, pady=2)
        runtime_frame.columnconfigure(1, weight=1)

        self.right_outer = ttk.Frame(self.main_paned)
        self.main_paned.add(self.right_outer, weight=2)

        self.right_canvas = tk.Canvas(self.right_outer, highlightthickness=0)
        self.right_scrollbar = ttk.Scrollbar(self.right_outer, orient=tk.VERTICAL, command=self.right_canvas.yview)
        self.right_canvas.configure(yscrollcommand=self.right_scrollbar.set)
        self.right_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.right_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.right_frame = ttk.Frame(self.right_canvas)
        self.right_canvas_window = self.right_canvas.create_window((0, 0), window=self.right_frame, anchor=tk.NW)
        self.right_frame.bind("<Configure>", self.on_right_frame_configure)
        self.right_canvas.bind("<Configure>", self.on_right_canvas_configure)

        param_frame = ttk.LabelFrame(self.right_frame, text="認識パラメータ")
        param_frame.pack(fill=tk.X, padx=5, pady=5)


        ttk.Checkbutton(param_frame, text="適応しきい値を使う", variable=self.adaptive_thresh_var).pack(anchor=tk.W, padx=5, pady=(4, 0))
        ttk.Checkbutton(param_frame, text="セグメント充填率スコア化", variable=self.segment_score_var).pack(anchor=tk.W, padx=5, pady=(2, 0))
        ttk.Checkbutton(param_frame, text="桁境界を自動推定", variable=self.auto_digit_bounds_var).pack(anchor=tk.W, padx=5, pady=(2, 0))

        ttk.Label(param_frame, text="自動桁境界の列しきい値 (%)").pack(anchor=tk.W, padx=5, pady=(5, 0))
        self.auto_digit_fill_scale = ttk.Scale(param_frame, from_=3, to=20, orient=tk.HORIZONTAL, variable=self.auto_digit_fill_var)
        self.auto_digit_fill_scale.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(param_frame, text="ドット除去サイズ上限 (%)").pack(anchor=tk.W, padx=5, pady=(5, 0))
        self.decimal_area_scale = ttk.Scale(param_frame, from_=2, to=20, orient=tk.HORIZONTAL, variable=self.decimal_area_var)
        self.decimal_area_scale.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(param_frame, text="固定しきい値").pack(anchor=tk.W, padx=5, pady=(6, 0))
        self.thresh_scale = ttk.Scale(param_frame, from_=0, to=255, orient=tk.HORIZONTAL)
        self.thresh_scale.set(128)
        self.thresh_scale.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(param_frame, text="ノイズ除去強度").pack(anchor=tk.W, padx=5, pady=(5, 0))
        self.noise_scale = ttk.Scale(param_frame, from_=0, to=5, orient=tk.HORIZONTAL)
        self.noise_scale.set(1)
        self.noise_scale.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(param_frame, text="セグメントONしきい値 (%)").pack(anchor=tk.W, padx=5, pady=(5, 0))
        self.fill_scale = ttk.Scale(param_frame, from_=5, to=80, orient=tk.HORIZONTAL)
        self.fill_scale.set(30)
        self.fill_scale.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(param_frame, text="桁間ギャップ (px)").pack(anchor=tk.W, padx=5, pady=(5, 0))
        self.gap_scale = ttk.Scale(param_frame, from_=0, to=50, orient=tk.HORIZONTAL)
        self.gap_scale.set(10)
        self.gap_scale.pack(fill=tk.X, padx=5, pady=2)

        out_frame = ttk.LabelFrame(self.right_frame, text="出力設定")
        out_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(out_frame, text="認識間隔 (ms):").grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        self.interval_entry = ttk.Entry(out_frame, width=10)
        self.interval_entry.insert(0, "100")
        self.interval_entry.grid(row=0, column=1, sticky=tk.W, padx=5, pady=2)

        obs_frame = ttk.Frame(out_frame)
        obs_frame.grid(row=1, column=0, columnspan=2, sticky=tk.W, padx=5, pady=2)
        ttk.Checkbutton(obs_frame, text="OBSテキスト出力", variable=self.obs_out_var).pack(side=tk.LEFT)
        ttk.Label(obs_frame, text=" / 保存間隔(ms):").pack(side=tk.LEFT)
        self.save_interval_entry = ttk.Entry(obs_frame, width=6)
        self.save_interval_entry.insert(0, "100")
        self.save_interval_entry.pack(side=tk.LEFT)
        out_frame.columnconfigure(1, weight=1)

        browser_frame = ttk.LabelFrame(self.right_frame, text="OBSブラウザソース配信")
        browser_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(browser_frame, text="Bind:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        self.obs_host_entry = ttk.Entry(browser_frame, width=14)
        self.obs_host_entry.grid(row=0, column=1, sticky=tk.W, padx=5, pady=2)

        ttk.Label(browser_frame, text="Port:").grid(row=0, column=2, sticky=tk.W, padx=5, pady=2)
        self.obs_port_entry = ttk.Entry(browser_frame, width=8)
        self.obs_port_entry.grid(row=0, column=3, sticky=tk.W, padx=5, pady=2)

        ttk.Button(browser_frame, text="配信開始", command=self.start_obs_server).grid(row=0, column=4, padx=5, pady=2)
        ttk.Button(browser_frame, text="停止", command=self.stop_obs_server).grid(row=0, column=5, padx=5, pady=2)

        ttk.Label(browser_frame, text="表示URL:").grid(row=1, column=0, sticky=tk.W, padx=5, pady=2)
        self.obs_url_var = tk.StringVar(value="")
        self.obs_url_entry = ttk.Entry(browser_frame, textvariable=self.obs_url_var)
        self.obs_url_entry.grid(row=1, column=1, columnspan=5, sticky=tk.EW, padx=5, pady=2)

        ttk.Label(browser_frame, text="上段だけは ?target=upper / 下段だけは ?target=lower").grid(
            row=2, column=0, columnspan=6, sticky=tk.W, padx=5, pady=(0, 4)
        )
        browser_frame.columnconfigure(5, weight=1)

        api_frame = ttk.LabelFrame(self.right_frame, text="API連携・デバッグ")
        api_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(api_frame, text="URL:").grid(row=0, column=0, sticky=tk.W, padx=5, pady=2)
        self.api_entry = ttk.Entry(api_frame, width=28)
        self.api_entry.grid(row=0, column=1, columnspan=2, sticky=tk.EW, padx=5, pady=2)
        ttk.Button(api_frame, text="本番値を送信", command=self.send_api_manual).grid(row=0, column=3, padx=5, pady=2)

        live_frame = ttk.Frame(api_frame)
        live_frame.grid(row=1, column=0, columnspan=4, sticky=tk.W, padx=5, pady=2)
        ttk.Checkbutton(live_frame, text="本番値を連続送信", variable=self.live_continuous_var, command=self.toggle_live_loop).pack(side=tk.LEFT)
        ttk.Label(live_frame, text="間隔(ms):").pack(side=tk.LEFT, padx=(10, 0))
        self.live_interval_entry = ttk.Entry(live_frame, width=6)
        self.live_interval_entry.insert(0, "300")
        self.live_interval_entry.pack(side=tk.LEFT)

        ttk.Separator(api_frame, orient=tk.HORIZONTAL).grid(row=2, column=0, columnspan=4, sticky=tk.EW, pady=5)

        ttk.Label(api_frame, text="[ダミー] 上段:").grid(row=3, column=0, sticky=tk.E, padx=5, pady=2)
        self.dummy_upper_entry = ttk.Entry(api_frame, width=10)
        self.dummy_upper_entry.insert(0, "123456")
        self.dummy_upper_entry.grid(row=3, column=1, sticky=tk.W, padx=5, pady=2)

        ttk.Label(api_frame, text="下段:").grid(row=3, column=2, sticky=tk.E, padx=5, pady=2)
        self.dummy_lower_entry = ttk.Entry(api_frame, width=10)
        self.dummy_lower_entry.insert(0, "789012")
        self.dummy_lower_entry.grid(row=3, column=3, sticky=tk.W, padx=5, pady=2)

        ctrl_api_frame = ttk.Frame(api_frame)
        ctrl_api_frame.grid(row=4, column=0, columnspan=4, sticky=tk.W, padx=5, pady=5)
        ttk.Button(ctrl_api_frame, text="ダミー値を1回送信", command=self.send_api_dummy).pack(side=tk.LEFT, padx=2)
        ttk.Checkbutton(ctrl_api_frame, text="ダミー値を連続送信", variable=self.debug_continuous_var, command=self.toggle_debug_loop).pack(side=tk.LEFT, padx=(10, 2))
        ttk.Label(ctrl_api_frame, text="間隔(ms):").pack(side=tk.LEFT)
        self.dummy_interval_entry = ttk.Entry(ctrl_api_frame, width=6)
        self.dummy_interval_entry.insert(0, "300")
        self.dummy_interval_entry.pack(side=tk.LEFT)
        api_frame.columnconfigure(1, weight=1)

        ctrl_frame = ttk.Frame(self.right_frame)
        ctrl_frame.pack(fill=tk.X, padx=5, pady=10)
        ttk.Button(ctrl_frame, text="認識スタート", command=self.start_processing).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        ttk.Button(ctrl_frame, text="ストップ", command=self.stop_processing).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        self.status_var = tk.StringVar(value="準備中...")
        self.status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.load_settings()
        self._bind_persistent_entry(self.url_entry)
        self._bind_persistent_entry(self.api_entry)
        self._bind_persistent_entry(self.obs_host_entry)
        self._bind_persistent_entry(self.obs_port_entry)
        self.refresh_runtime_status()
        self.root.after(100, self.start_obs_server)

    def _bind_persistent_entry(self, entry):
        entry.bind("<FocusOut>", lambda _event: (self.update_obs_url_display(), self.save_settings()))
        entry.bind("<Return>", lambda _event: (self.update_obs_url_display(), self.save_settings()))

    def _set_entry_text(self, entry, value):
        entry.delete(0, tk.END)
        entry.insert(0, value)

    def _read_interval(self, entry, fallback):
        try:
            value = int(entry.get())
            return max(10, value)
        except (TypeError, ValueError):
            return fallback

    def format_time_value(self, value):
        raw = str(value or "").strip()
        if not raw:
            return ""
        if "." in raw:
            return raw
        if raw.isdigit() and len(raw) == 6:
            return f"{raw[:3]}.{raw[3:]}"
        return raw

    def _get_bind_host(self):
        host = self.obs_host_entry.get().strip()
        return host or "0.0.0.0"

    def _get_obs_port(self):
        try:
            port = int(self.obs_port_entry.get().strip())
            if 1 <= port <= 65535:
                return port
        except (TypeError, ValueError):
            pass
        return 8765

    def _detect_local_ip(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            sock.close()

    def update_obs_url_display(self):
        host = self._get_bind_host()
        port = self._get_obs_port()
        display_host = self._detect_local_ip() if host == "0.0.0.0" else host
        self.obs_url_var.set(f"http://{display_host}:{port}/")

    def refresh_runtime_status(self):
        self.camera_status_var.set("接続中" if self.cap and self.cap.isOpened() else "未接続")
        self.processing_status_var.set("認識中" if self.is_processing else "停止")
        self.obs_server_status_var.set(self.obs_url_var.get() if self.obs_server else "停止")

        upper = self.current_val["upper"] or "---.---"
        lower = self.current_val["lower"] or "---.---"
        if self.obs_server:
            self.obs_publish_status_var.set(f"上:{upper} / 下:{lower}")
        else:
            self.obs_publish_status_var.set(f"上:{upper} / 下:{lower}")

    def publish_obs_state(self, force=False):
        payload = {
            "upper": self.current_val["upper"],
            "lower": self.current_val["lower"],
            "raw_upper": self.raw_val["upper"],
            "raw_lower": self.raw_val["lower"],
        }
        if not force and payload == self.last_obs_payload:
            self.refresh_runtime_status()
            return
        self.last_obs_payload = dict(payload)
        if self.obs_server:
            self.obs_server.publish(**payload)
        self.refresh_runtime_status()

    def on_right_frame_configure(self, _event=None):
        self.right_canvas.configure(scrollregion=self.right_canvas.bbox("all"))

    def on_right_canvas_configure(self, event):
        self.right_canvas.itemconfigure(self.right_canvas_window, width=event.width)

    def load_settings(self):
        defaults = {
            "rtsp_url": "0",
            "api_url": "http://localhost:8080/api",
            "obs_bind_host": "0.0.0.0",
            "obs_bind_port": "8765",
        }
        settings = defaults
        if os.path.exists(self.settings_path):
            try:
                with open(self.settings_path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                settings = {**defaults, **loaded}
            except (OSError, json.JSONDecodeError):
                settings = defaults

        self._set_entry_text(self.url_entry, settings.get("rtsp_url", defaults["rtsp_url"]))
        self._set_entry_text(self.api_entry, settings.get("api_url", defaults["api_url"]))
        self._set_entry_text(self.obs_host_entry, settings.get("obs_bind_host", defaults["obs_bind_host"]))
        self._set_entry_text(self.obs_port_entry, settings.get("obs_bind_port", defaults["obs_bind_port"]))
        self.update_obs_url_display()

    def save_settings(self):
        settings = {
            "rtsp_url": self.url_entry.get().strip(),
            "api_url": self.api_entry.get().strip(),
            "obs_bind_host": self._get_bind_host(),
            "obs_bind_port": str(self._get_obs_port()),
        }
        try:
            with open(self.settings_path, "w", encoding="utf-8") as fh:
                json.dump(settings, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def start_obs_server(self):
        host = self._get_bind_host()
        port = self._get_obs_port()
        self.update_obs_url_display()

        if self.obs_server and self.obs_server.host == host and self.obs_server.port == port:
            self.status_var.set(f"OBS配信サーバー稼働中: {self.obs_url_var.get()}")
            self.publish_obs_state(force=True)
            self.refresh_runtime_status()
            return

        self.stop_obs_server(update_status=False)
        try:
            self.obs_server = ObsBrowserServer(host, port)
            self.obs_server.start()
            self.save_settings()
            self.publish_obs_state(force=True)
            self.status_var.set(f"OBS配信サーバーを開始しました: {self.obs_url_var.get()}")
            self.refresh_runtime_status()
        except OSError as exc:
            self.obs_server = None
            self.status_var.set(f"OBS配信サーバー起動失敗: {exc}")
            self.refresh_runtime_status()

    def stop_obs_server(self, update_status=True):
        if self.obs_server:
            self.obs_server.stop()
            self.obs_server = None
        if update_status:
            self.status_var.set("OBS配信サーバーを停止しました")
        self.refresh_runtime_status()

    def set_roi_target(self, target):
        self.roi_target = target
        label = "上段" if target == "upper" else "下段"
        self.status_var.set(f"{label}のROIをドラッグしてください")

    def reset_roi(self):
        for key in self.rects:
            if self.rects[key]:
                self.canvas.delete(self.rects[key])
        self.rects = {"upper": None, "lower": None}
        self.rois = {"upper": None, "lower": None}
        self.roi_target = None
        self.status_var.set("ROIをリセットしました")

    def on_button_press(self, event):
        if not self.roi_target:
            return
        self.start_x = event.x
        self.start_y = event.y
        if self.rects[self.roi_target]:
            self.canvas.delete(self.rects[self.roi_target])
        color = "red" if self.roi_target == "upper" else "blue"
        self.rects[self.roi_target] = self.canvas.create_rectangle(
            self.start_x,
            self.start_y,
            self.start_x,
            self.start_y,
            outline=color,
            width=2,
        )

    def on_mouse_drag(self, event):
        if not self.roi_target or not self.rects[self.roi_target]:
            return
        self.canvas.coords(self.rects[self.roi_target], self.start_x, self.start_y, event.x, event.y)

    def on_button_release(self, event):
        if not self.roi_target:
            return
        x1 = max(0, min(self.start_x, event.x))
        y1 = max(0, min(self.start_y, event.y))
        x2 = min(self.PREVIEW_WIDTH, max(self.start_x, event.x))
        y2 = min(self.PREVIEW_HEIGHT, max(self.start_y, event.y))
        self.rois[self.roi_target] = (x1, y1, x2, y2)
        label = "上段" if self.roi_target == "upper" else "下段"
        self.status_var.set(f"{label}のROIを設定しました")
        self.roi_target = None

    def connect_camera(self):
        self.disconnect_camera(clear_status=False)
        url = self.url_entry.get().strip()
        self.save_settings()
        source = int(url) if url.isdigit() else url
        self.cap = cv2.VideoCapture(source)
        if self.cap.isOpened():
            self.status_var.set("カメラに接続しました")
            self.refresh_runtime_status()
            self.update_frame()
        else:
            self.cap = None
            self.status_var.set("エラー: カメラに接続できません")
            self.refresh_runtime_status()

    def disconnect_camera(self, clear_status=True):
        self.stop_processing(update_status=False)
        if self.video_loop_id:
            self.root.after_cancel(self.video_loop_id)
            self.video_loop_id = None
        if self.cap:
            self.cap.release()
            self.cap = None
        self.current_frame = None
        self.image_on_canvas = None
        self.photo = None
        self.canvas.delete("all")
        self.redraw_rois()
        if clear_status:
            self.status_var.set("切断しました")
        self.refresh_runtime_status()

    def redraw_rois(self):
        for target, roi in self.rois.items():
            if self.rects[target]:
                self.canvas.delete(self.rects[target])
                self.rects[target] = None
            if roi:
                color = "red" if target == "upper" else "blue"
                points = self.get_rotated_roi_points(roi, self.roi_angle_vars[target].get())
                self.rects[target] = self.canvas.create_polygon(points, outline=color, fill="", width=2)

    def prepare_preview_frame(self, frame):
        frame_h, frame_w = frame.shape[:2]
        scale = min(self.PREVIEW_WIDTH / frame_w, self.PREVIEW_HEIGHT / frame_h)
        resized_w = max(1, int(frame_w * scale))
        resized_h = max(1, int(frame_h * scale))
        resized = cv2.resize(frame, (resized_w, resized_h))

        preview = np.zeros((self.PREVIEW_HEIGHT, self.PREVIEW_WIDTH, 3), dtype=np.uint8)
        offset_x = (self.PREVIEW_WIDTH - resized_w) // 2
        offset_y = (self.PREVIEW_HEIGHT - resized_h) // 2
        preview[offset_y:offset_y + resized_h, offset_x:offset_x + resized_w] = resized
        return preview

    def update_frame(self):
        if not self.cap or not self.cap.isOpened():
            return

        ret, frame = self.cap.read()
        if ret:
            preview_frame = self.prepare_preview_frame(frame)
            if self.fisheye_var.get():
                h, w = preview_frame.shape[:2]
                k = np.array([[w, 0, w / 2], [0, w, h / 2], [0, 0, 1]], dtype=np.float32)
                d = np.array([self.fisheye_scale.get() / 100.0, 0, 0, 0], dtype=np.float32)
                self.current_frame = cv2.undistort(preview_frame, k, d)
            else:
                self.current_frame = preview_frame

            cv_img = cv2.cvtColor(self.current_frame, cv2.COLOR_BGR2RGB)
            self.photo = ImageTk.PhotoImage(image=Image.fromarray(cv_img))
            if self.image_on_canvas is None:
                self.image_on_canvas = self.canvas.create_image(0, 0, image=self.photo, anchor=tk.NW)
                self.canvas.tag_lower(self.image_on_canvas)
            else:
                self.canvas.itemconfig(self.image_on_canvas, image=self.photo)
            self.redraw_rois()

        self.video_loop_id = self.root.after(33, self.update_frame)

    def start_processing(self):
        if not self.cap or not self.cap.isOpened() or (not self.rois["upper"] and not self.rois["lower"]):
            self.status_var.set("カメラ接続とROI設定を確認してください")
            return

        self.last_text = {"upper": "", "lower": ""}
        self.last_save_time = {"upper": 0.0, "lower": 0.0}
        self.current_val = {"upper": "", "lower": ""}
        self.raw_val = {"upper": "", "lower": ""}
        self.stable_val = {"upper": "", "lower": ""}
        self.pending_val = {"upper": "", "lower": ""}
        self.pending_count = {"upper": 0, "lower": 0}
        self.invalid_count = {"upper": 0, "lower": 0}
        self.last_obs_payload = None

        self.is_processing = True
        self.status_var.set("認識処理を開始しました")
        self.publish_obs_state(force=True)
        self.refresh_runtime_status()
        self.process_loop()

    def stop_processing(self, update_status=True):
        self.is_processing = False
        if self.process_loop_id:
            self.root.after_cancel(self.process_loop_id)
            self.process_loop_id = None
        if update_status:
            self.status_var.set("認識処理を停止しました")
        self.refresh_runtime_status()

    def stabilize_result(self, target, raw_result):
        raw_result = raw_result.strip()
        stable_result = self.stable_val[target]

        if raw_result and "?" not in raw_result:
            self.invalid_count[target] = 0
            if not stable_result or raw_result == stable_result:
                self.stable_val[target] = raw_result
                self.pending_val[target] = ""
                self.pending_count[target] = 0
                return raw_result

            if self.pending_val[target] == raw_result:
                self.pending_count[target] += 1
            else:
                self.pending_val[target] = raw_result
                self.pending_count[target] = 1

            if self.pending_count[target] >= 2:
                self.stable_val[target] = raw_result
                self.pending_val[target] = ""
                self.pending_count[target] = 0

            return self.stable_val[target]

        self.invalid_count[target] += 1
        if self.invalid_count[target] <= 2 and stable_result:
            return stable_result
        return raw_result or stable_result

    def apply_image_skew_correction(self, image):
        if not self.image_skew_var.get():
            return image

        skew = self.image_skew_scale.get()
        height, width = image.shape[:2]
        matrix = np.float32([[1, skew / max(1.0, height), 0], [0, 1, 0]])
        border_value = 0 if len(image.shape) == 2 else (0, 0, 0)
        return cv2.warpAffine(
            image,
            matrix,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=border_value,
        )

    def preprocess_threshold(self, gray):
        corrected_gray = self.apply_image_skew_correction(gray)
        if self.adaptive_thresh_var.get():
            blur = cv2.GaussianBlur(corrected_gray, (5, 5), 0)
            thresh = cv2.adaptiveThreshold(
                blur,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                31,
                8,
            )
        else:
            _, thresh = cv2.threshold(corrected_gray, self.thresh_scale.get(), 255, cv2.THRESH_BINARY)

        noise_val = int(self.noise_scale.get())
        if noise_val > 0:
            kernel = np.ones((noise_val, noise_val), np.uint8)
            thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
        return thresh

    def get_rotated_roi_points(self, roi, angle_deg):
        x1, y1, x2, y2 = roi
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        corners = [
            (x1, y1),
            (x2, y1),
            (x2, y2),
            (x1, y2),
        ]
        angle_rad = math.radians(angle_deg)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)
        points = []
        for px, py in corners:
            dx = px - cx
            dy = py - cy
            rx = cx + (dx * cos_a) - (dy * sin_a)
            ry = cy + (dx * sin_a) + (dy * cos_a)
            points.extend((rx, ry))
        return points

    def extract_roi_image(self, image, target):
        roi = self.rois[target]
        if not roi:
            return None

        x1, y1, x2, y2 = roi
        angle_deg = self.roi_angle_vars[target].get()
        if abs(angle_deg) < 0.01:
            return image[y1:y2, x1:x2]

        height, width = image.shape[:2]
        center = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        matrix = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
        rotated = cv2.warpAffine(
            image,
            matrix,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        return rotated[y1:y2, x1:x2]

    def detect_digit_regions(self, norm_thresh):
        _height, width = norm_thresh.shape
        if not self.auto_digit_bounds_var.get():
            return self.detect_digit_regions_fallback(width)

        column_fill = np.mean(norm_thresh > 0, axis=0)
        active = column_fill > (self.auto_digit_fill_var.get() / 100.0)
        segments = []
        start = None
        for idx, is_active in enumerate(active):
            if is_active and start is None:
                start = idx
            elif not is_active and start is not None:
                if idx - start >= 6:
                    segments.append([start, idx])
                start = None
        if start is not None and width - start >= 6:
            segments.append([start, width])

        if not segments:
            return self.detect_digit_regions_fallback(width)

        merged = [segments[0]]
        for start, end in segments[1:]:
            if start - merged[-1][1] <= max(2, int(self.gap_scale.get() * 0.6)):
                merged[-1][1] = end
            else:
                merged.append([start, end])

        merged = sorted(merged, key=lambda seg: seg[1] - seg[0], reverse=True)[:6]
        merged = sorted(merged, key=lambda seg: seg[0])
        if len(merged) != 6:
            return self.detect_digit_regions_fallback(width)
        return [(start, end) for start, end in merged]

    def detect_digit_regions_fallback(self, width):
        gap = int(self.gap_scale.get())
        digit_w = max(1, (width - (gap * 5)) // 6)
        regions = []
        for i in range(6):
            x1 = i * (digit_w + gap)
            x2 = min(width, x1 + digit_w)
            if x1 >= width:
                break
            regions.append((x1, x2))
        return regions

    def build_segments(self, digit_h, digit_w):
        return [
            ((0, int(digit_h * 0.2)), (int(digit_w * 0.2), int(digit_w * 0.8))),
            ((int(digit_h * 0.1), int(digit_h * 0.5)), (int(digit_w * 0.7), digit_w)),
            ((int(digit_h * 0.5), int(digit_h * 0.9)), (int(digit_w * 0.7), digit_w)),
            ((int(digit_h * 0.8), digit_h), (int(digit_w * 0.2), int(digit_w * 0.8))),
            ((int(digit_h * 0.5), int(digit_h * 0.9)), (0, int(digit_w * 0.3))),
            ((int(digit_h * 0.1), int(digit_h * 0.5)), (0, int(digit_w * 0.3))),
            ((int(digit_h * 0.4), int(digit_h * 0.6)), (int(digit_w * 0.2), int(digit_w * 0.8))),
        ]

    def suppress_decimal_point(self, digit_img):
        num_labels, labels, stats, _centroids = cv2.connectedComponentsWithStats(digit_img, connectivity=8)
        if num_labels <= 1:
            return digit_img

        cleaned = digit_img.copy()
        digit_h, digit_w = digit_img.shape
        max_area = max(6, int(digit_h * digit_w * (self.decimal_area_var.get() / 100.0)))
        max_w = max(3, int(digit_w * 0.35))
        max_h = max(3, int(digit_h * 0.35))

        for label in range(1, num_labels):
            x = stats[label, cv2.CC_STAT_LEFT]
            y = stats[label, cv2.CC_STAT_TOP]
            w = stats[label, cv2.CC_STAT_WIDTH]
            h = stats[label, cv2.CC_STAT_HEIGHT]
            area = stats[label, cv2.CC_STAT_AREA]
            if area > max_area or w > max_w or h > max_h:
                continue

            center_x = x + (w / 2.0)
            center_y = y + (h / 2.0)
            if center_x < digit_w * 0.60 or center_y < digit_h * 0.60:
                continue

            cleaned[labels == label] = 0

        return cleaned

    def classify_digit(self, fill_ratios):
        fill_threshold = self.fill_scale.get() / 100.0
        on_pattern = tuple(1 if ratio > fill_threshold else 0 for ratio in fill_ratios)

        if not self.segment_score_var.get():
            return self.DIGITS_LOOKUP.get(on_pattern, "?"), on_pattern

        best_digit = "?"
        best_score = float("inf")
        for pattern, digit in self.DIGITS_LOOKUP.items():
            score = 0.0
            for ratio, expected in zip(fill_ratios, pattern):
                target = 0.75 if expected else 0.05
                weight = 1.25 if expected else 0.8
                score += abs(ratio - target) * weight
            if score < best_score:
                best_score = score
                best_digit = digit
        return best_digit, on_pattern

    def process_loop(self):
        if not self.is_processing or self.current_frame is None:
            return

        gray = cv2.cvtColor(self.current_frame, cv2.COLOR_BGR2GRAY)
        thresh = self.preprocess_threshold(gray)

        current_time = time.time()
        save_interval_sec = self._read_interval(self.save_interval_entry, 100) / 1000.0

        for target in ["upper", "lower"]:
            if not self.rois[target]:
                continue

            roi_img = self.extract_roi_image(thresh, target)
            if roi_img is None or roi_img.size == 0 or roi_img.shape[1] < 10:
                continue

            normalized_img = cv2.resize(roi_img, (600, 100))

            result_str, debug_img = self.logic_a_7seg(normalized_img)
            stable_result = self.stabilize_result(target, result_str)
            formatted_raw = self.format_time_value(result_str)
            formatted_stable = self.format_time_value(stable_result)
            self.raw_val[target] = formatted_raw
            self.current_val[target] = formatted_stable

            debug_img_rgb = cv2.cvtColor(debug_img, cv2.COLOR_BGR2RGB)
            display_img = cv2.resize(debug_img_rgb, (300, 50))
            photo = ImageTk.PhotoImage(image=Image.fromarray(display_img))

            if target == "upper":
                self.upper_lbl.config(text=formatted_stable or "---.---")
                self.debug_photo_upper = photo
                self.upper_img_lbl.config(image=self.debug_photo_upper)
            else:
                self.lower_lbl.config(text=formatted_stable or "---.---")
                self.debug_photo_lower = photo
                self.lower_img_lbl.config(image=self.debug_photo_lower)

            if self.obs_out_var.get() and formatted_stable != self.last_text[target]:
                if (current_time - self.last_save_time[target]) >= save_interval_sec:
                    filename = f"{target}_time.txt"
                    try:
                        with open(filename, "w", encoding="utf-8") as fh:
                            fh.write(formatted_stable)
                        self.last_text[target] = formatted_stable
                        self.last_save_time[target] = current_time
                    except OSError as exc:
                        print(f"file write error: {exc}")

        self.publish_obs_state()
        interval = self._read_interval(self.interval_entry, 100)
        self.process_loop_id = self.root.after(interval, self.process_loop)

    def logic_a_7seg(self, norm_thresh):
        result_str = ""
        debug_img = cv2.cvtColor(norm_thresh, cv2.COLOR_GRAY2BGR)
        digit_regions = self.detect_digit_regions(norm_thresh)

        for x1, x2 in digit_regions:
            digit_img = norm_thresh[:, x1:x2]
            digit_img = self.suppress_decimal_point(digit_img)
            digit_h, digit_w = digit_img.shape
            if digit_w == 0 or digit_h == 0:
                result_str += "?"
                continue

            segments = self.build_segments(digit_h, digit_w)
            fill_ratios = []
            for (y1, y2), (sx1, sx2) in segments:
                seg_crop = digit_img[y1:y2, sx1:sx2]
                if seg_crop.size == 0:
                    fill_ratios.append(0.0)
                    continue
                fill_ratios.append(cv2.countNonZero(seg_crop) / float(seg_crop.shape[0] * seg_crop.shape[1]))

            digit_char, on_pattern = self.classify_digit(fill_ratios)
            result_str += digit_char

            cv2.rectangle(debug_img, (x1, 0), (x2, digit_h - 1), (255, 180, 0), 1)
            for index, ((y1, y2), (sx1, sx2)) in enumerate(segments):
                is_on = on_pattern[index] == 1
                color = (0, 255, 0) if is_on else (0, 0, 255)
                cv2.rectangle(debug_img, (x1 + sx1, y1), (x1 + sx2, y2), color, 1)

        return result_str, debug_img

    def send_api_manual(self):
        if not self.current_val["upper"] and not self.current_val["lower"]:
            self.status_var.set("本番値がまだありません")
            return
        self.publish_obs_state(force=True)
        self._fire_api_request(self.current_val["upper"], self.current_val["lower"])

    def send_api_dummy(self):
        upper_val = self.format_time_value(self.dummy_upper_entry.get())
        lower_val = self.format_time_value(self.dummy_lower_entry.get())
        if self.obs_server:
            self.obs_server.publish(upper_val, lower_val, upper_val, lower_val)
        self._fire_api_request(upper_val, lower_val)

    def _fire_api_request(self, upper_val, lower_val):
        url = self.api_entry.get().strip()
        if not url:
            self.status_var.set("エラー: API URLを入力してください")
            return

        self.save_settings()
        payload = {
            "upper": self.format_time_value(upper_val),
            "lower": self.format_time_value(lower_val),
        }
        self.status_var.set(f"API送信中... ({upper_val}, {lower_val})")
        threading.Thread(target=self._post_api_thread, args=(url, payload), daemon=True).start()

    def _post_api_thread(self, url, payload):
        try:
            response = requests.post(url, json=payload, timeout=2)
            if response.status_code == 200:
                self.root.after(0, lambda: self.status_var.set("API送信成功"))
            else:
                self.root.after(0, lambda: self.status_var.set(f"APIエラー: HTTP {response.status_code}"))
        except requests.exceptions.RequestException:
            self.root.after(0, lambda: self.status_var.set("API送信失敗: 通信エラー"))

    def toggle_debug_loop(self):
        if self.debug_continuous_var.get():
            self.status_var.set("ダミー値の連続送信を開始しました")
            self.continuous_debug_loop()
            return

        if self.debug_loop_id:
            self.root.after_cancel(self.debug_loop_id)
            self.debug_loop_id = None
        self.status_var.set("ダミー値の連続送信を停止しました")

    def continuous_debug_loop(self):
        if not self.debug_continuous_var.get():
            return
        self.send_api_dummy()
        interval = self._read_interval(self.dummy_interval_entry, 300)
        self.debug_loop_id = self.root.after(interval, self.continuous_debug_loop)

    def toggle_live_loop(self):
        if self.live_continuous_var.get():
            self.status_var.set("本番値の連続送信を開始しました")
            self.continuous_live_loop()
            return

        if self.live_loop_id:
            self.root.after_cancel(self.live_loop_id)
            self.live_loop_id = None
        self.status_var.set("本番値の連続送信を停止しました")

    def continuous_live_loop(self):
        if not self.live_continuous_var.get():
            return
        if self.current_val["upper"] or self.current_val["lower"]:
            self._fire_api_request(self.current_val["upper"], self.current_val["lower"])
        interval = self._read_interval(self.live_interval_entry, 300)
        self.live_loop_id = self.root.after(interval, self.continuous_live_loop)

    def on_close(self):
        self.save_settings()
        self.debug_continuous_var.set(False)
        self.live_continuous_var.set(False)
        if self.debug_loop_id:
            self.root.after_cancel(self.debug_loop_id)
        if self.live_loop_id:
            self.root.after_cancel(self.live_loop_id)
        if self.process_loop_id:
            self.root.after_cancel(self.process_loop_id)
        if self.video_loop_id:
            self.root.after_cancel(self.video_loop_id)
        if self.cap:
            self.cap.release()
        self.stop_obs_server(update_status=False)
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = SevenSegReaderApp(root)
    root.mainloop()
