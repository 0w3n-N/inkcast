"""Live capture of one desktop window, for desktop annotation.

Uses the standard ScreenCast portal (xdg-desktop-portal), so the user picks the
window in the system share dialog (on Omarchy: hyprland-preview-share-picker),
exactly like screen sharing in a browser. The portal hands back a PipeWire
stream; GStreamer scales it and encodes JPEG frames, which the server relays to
the iPad and the share window.

Needs python-gobject, gstreamer, gst-plugin-pipewire and gst-plugins-good. Imported
lazily: without them Inkcast works as before and annotation reports what's missing.
"""

import secrets
import threading
import time

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
SOURCE_WINDOW = 2
CURSOR_EMBEDDED = 2
CURSOR_HIDDEN = 1


def missing_dependencies():
    """Names of packages needed for window capture that aren't installed."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError):
        return ["python-gobject", "gstreamer"]
    Gst.init(None)
    need = {"pipewiresrc": "gst-plugin-pipewire", "jpegenc": "gst-plugins-good",
            "videoconvert": "gst-plugins-base", "videoscale": "gst-plugins-base",
            "videorate": "gst-plugins-base", "appsink": "gst-plugins-base"}
    return sorted({pkg for el, pkg in need.items() if Gst.ElementFactory.find(el) is None})


class CaptureError(Exception):
    pass


class WindowCapture:
    """One capture session. Callbacks run on the capture thread.

    on_start(width, height)  stream is live (source size in pixels)
    on_frame(jpeg_bytes)     a new frame
    on_resize(width, height) the window changed size
    on_end(reason)           finished: "stopped", "cancelled", "closed" or an error message
    """

    def __init__(self, on_start, on_frame, on_resize, on_end, max_width=1600, fps=15, quality=72):
        self.on_start, self.on_frame, self.on_resize, self.on_end = on_start, on_frame, on_resize, on_end
        self.max_width, self.fps, self.quality = max_width, fps, quality
        self._loop = None
        self._ctx = None
        self._pipeline = None
        self._bus = None
        self._session = None
        self._ended = False
        self._stop_requested = False
        self._size = None
        self._frames = 0
        self.thread = threading.Thread(target=self._run, daemon=True, name="inkcast-capture")

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop_requested = True
        if self._loop:
            self._in_loop(lambda: self._finish("stopped"))

    def _in_loop(self, fn):
        """Run fn on the capture thread's main loop."""
        src = self.GLib.idle_source_new()
        src.set_callback(lambda *_: (fn(), False)[1])
        src.attach(self._ctx)

    # ------------------------------------------------------------- thread

    def _run(self):
        try:
            import gi
            gi.require_version("Gst", "1.0")
            from gi.repository import Gio, GLib, Gst
        except (ImportError, ValueError):
            self._end("Window capture needs python-gobject and GStreamer. Run scripts/setup.")
            return
        self.Gio, self.GLib, self.Gst = Gio, GLib, Gst
        Gst.init(None)
        self._ctx = GLib.MainContext.new()
        self._ctx.push_thread_default()
        self._loop = GLib.MainLoop.new(self._ctx, False)
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._sender = self._bus.get_unique_name()[1:].replace(".", "_")
            self._create_session()
            self._loop.run()
        except Exception as e:  # report anything to the user instead of dying silently
            self._end(f"Window capture failed: {e}")
        finally:
            self._teardown()
            self._ctx.pop_thread_default()

    def _end(self, reason):
        if not self._ended:
            self._ended = True
            try:
                self.on_end(reason)
            except Exception:
                pass

    def _finish(self, reason):
        self._end(reason)
        if self._loop:
            self._loop.quit()

    def _teardown(self):
        if self._pipeline is not None:
            self._pipeline.set_state(self.Gst.State.NULL)
            self._pipeline = None
        if self._session and self._bus:
            try:
                self._bus.call_sync(PORTAL, self._session, "org.freedesktop.portal.Session", "Close",
                                    None, None, self.Gio.DBusCallFlags.NONE, 2000, None)
            except Exception:
                pass
            self._session = None

    # ------------------------------------------------------------- portal

    def _request(self, method, args_builder, on_response):
        """Call a portal method that answers through a Request object's Response signal."""
        GLib, Gio = self.GLib, self.Gio
        token = "inkcast" + secrets.token_hex(6)
        path = f"/org/freedesktop/portal/desktop/request/{self._sender}/{token}"

        def handler(conn, sender, obj, iface, signal, params):
            conn.signal_unsubscribe(sub_id[0])
            code, results = params.unpack()
            if self._stop_requested:
                return self._finish("stopped")
            if code == 1:
                return self._finish("cancelled")
            if code != 0:
                return self._finish("The window picker was closed or failed.")
            try:
                on_response(results)
            except Exception as e:
                self._finish(f"Window capture failed: {e}")

        # Subscribe before calling, so a fast response can't be missed.
        sub_id = [self._bus.signal_subscribe(PORTAL, "org.freedesktop.portal.Request", "Response", path,
                                             None, Gio.DBusSignalFlags.NONE, handler)]
        self._bus.call_sync(PORTAL, PORTAL_PATH, SCREENCAST, method, args_builder(token),
                            None, Gio.DBusCallFlags.NONE, 30000, None)

    def _create_session(self):
        GLib = self.GLib

        def args(token):
            return GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", token),
                "session_handle_token": GLib.Variant("s", "inkcast" + secrets.token_hex(6)),
            },))

        self._request("CreateSession", args, self._on_session)

    def _on_session(self, results):
        GLib = self.GLib
        self._session = results["session_handle"]
        cursor = CURSOR_EMBEDDED if self._cursor_modes() & CURSOR_EMBEDDED else CURSOR_HIDDEN

        def args(token):
            return GLib.Variant("(oa{sv})", (self._session, {
                "handle_token": GLib.Variant("s", token),
                "types": GLib.Variant("u", SOURCE_WINDOW),
                "multiple": GLib.Variant("b", False),
                "cursor_mode": GLib.Variant("u", cursor),
            }))

        self._request("SelectSources", args, self._on_sources)

    def _cursor_modes(self):
        try:
            v = self._bus.call_sync(PORTAL, PORTAL_PATH, "org.freedesktop.DBus.Properties", "Get",
                                    self.GLib.Variant("(ss)", (SCREENCAST, "AvailableCursorModes")),
                                    None, self.Gio.DBusCallFlags.NONE, 5000, None)
            return int(v.unpack()[0])
        except Exception:
            return CURSOR_HIDDEN

    def _on_sources(self, _results):
        GLib = self.GLib

        def args(token):
            return GLib.Variant("(osa{sv})", (self._session, "", {"handle_token": GLib.Variant("s", token)}))

        # This is where the window picker appears; it waits for the user.
        self._request("Start", args, self._on_started)

    def _on_started(self, results):
        streams = results.get("streams") or []
        if not streams:
            return self._finish("No window was shared.")
        node_id, props = streams[0]
        size = props.get("size")
        reply, fds = self._bus.call_with_unix_fd_list_sync(
            PORTAL, PORTAL_PATH, SCREENCAST, "OpenPipeWireRemote",
            self.GLib.Variant("(oa{sv})", (self._session, {})), None,
            self.Gio.DBusCallFlags.NONE, 10000, None, None)
        fd = fds.get(reply.unpack()[0])
        self._start_pipeline(fd, node_id, size)

    # ------------------------------------------------------------- gstreamer

    def _target_size(self, w, h):
        tw = min(int(w), self.max_width)
        th = max(2, round(int(h) * tw / max(1, int(w))))
        return tw - tw % 2, th - th % 2

    def _start_pipeline(self, fd, node_id, size):
        Gst = self.Gst
        desc = (f"pipewiresrc name=src fd={int(fd)} path={int(node_id)} do-timestamp=true keepalive-time=1000 "
                f"! videoconvert ! videoscale ! capsfilter name=size "
                f"! videorate drop-only=true max-rate={int(self.fps)} "
                f"! jpegenc quality={int(self.quality)} "
                f"! appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false")
        self._pipeline = Gst.parse_launch(desc)
        if size:
            self._apply_size(*size, first=True)
        src_pad = self._pipeline.get_by_name("src").get_static_pad("src")
        src_pad.connect("notify::caps", self._on_caps)
        self._pipeline.get_by_name("sink").connect("new-sample", self._on_sample)
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", lambda *_: self._finish("closed"))
        bus.connect("message::error", self._on_error)
        if self._pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise CaptureError("GStreamer could not start the stream")

    def _apply_size(self, w, h, first=False):
        if w <= 0 or h <= 0 or (w, h) == self._size:
            return
        self._size = (w, h)
        tw, th = self._target_size(w, h)
        caps = self.Gst.Caps.from_string(f"video/x-raw,width={tw},height={th},pixel-aspect-ratio=1/1")
        self._pipeline.get_by_name("size").set_property("caps", caps)
        (self.on_start if first else self.on_resize)(w, h)

    def _on_caps(self, pad, _param):
        caps = pad.get_current_caps()
        if not caps:
            return
        s = caps.get_structure(0)
        ok_w, w = s.get_int("width")
        ok_h, h = s.get_int("height")
        if ok_w and ok_h:
            first = self._size is None
            self._in_loop(lambda: self._apply_size(w, h, first=first))

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return self.Gst.FlowReturn.EOS
        buf = sample.get_buffer()
        ok, info = buf.map(self.Gst.MapFlags.READ)
        if ok:
            try:
                self._frames += 1
                self.on_frame(bytes(info.data))
            finally:
                buf.unmap(info)
        return self.Gst.FlowReturn.OK

    def _on_error(self, _bus, message):
        err, _debug = message.parse_error()
        # Closing the shared window ends the stream with an error from pipewiresrc.
        self._finish("closed" if self._frames else f"Capture error: {err.message if err else 'stream error'}")
