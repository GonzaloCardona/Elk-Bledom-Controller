"""
ELK-BLEDOM LED Controller
─────────────────────────────────────────────────────────────────────────────
Requisitos:
    pip install bleak pyaudiowpatch numpy mss pystray pillow

Optimizaciones de memoria:
  · La ventana Tk se destruye al minimizar y se recrea al abrir
    → ahorra ~150-200 MB que tkinter retiene aunque esté oculta
  · Los workers (audio/pantalla) se pausan en segundo plano si no hay
    LEDs activos, reduciendo uso de CPU y RAM auxiliar
  · Lazy imports: numpy/mss/pyaudio se importan solo cuando se usan
  · El loop BLE reduce su frecuencia de polling en background

Protocolo ELK-BLEDOM (9 bytes):
  Encender :  7e 00 04 01 00 00 00 00 ef
  Apagar   :  7e 00 04 00 00 00 ff 00 ef
  Color    :  7e 00 05 03 RR GG BB 00 ef
  Brillo   :  7e 00 01 XX 00 00 00 00 ef   XX = 0-100
  Velocidad:  7e 00 02 SS 00 00 00 00 ef   SS = 0-100
  Efecto   :  7e 00 03 EE 03 00 00 00 ef
"""

import asyncio, threading, time, colorsys, math, json, os, sys, io
import tkinter as tk
from tkinter import messagebox, ttk

# ── Imports opcionales (lazy donde sea posible) ───────────────────────────────
try:
    from bleak import BleakScanner, BleakClient
    BLEAK_OK = True
except ImportError:
    BLEAK_OK = False

try:
    import pystray
    from PIL import Image as PILImage, ImageDraw, ImageTk
    TRAY_OK = True
except ImportError:
    TRAY_OK = False

try:
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as SMTCManager
    from winrt.windows.storage.streams import DataReader
    MEDIA_OK = True
except ImportError:
    MEDIA_OK = False

# numpy, mss y pyaudiowpatch se importan solo cuando se activa cada modo

# ── Config ────────────────────────────────────────────────────────────────────

APP_NAME    = "ELK-BLEDOM"
APPDATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "elk_bledom")
CONFIG_FILE = os.path.join(APPDATA_DIR, "config.json")
REG_NAME    = "ELK_BLEDOM_LED"
REG_KEY     = r"Software\Microsoft\Windows\CurrentVersion\Run"
os.makedirs(APPDATA_DIR, exist_ok=True)

def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f: return json.load(f)
    except Exception: return {}

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception: pass

def _exe_path():
    if getattr(sys, "frozen", False): return sys.executable
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'

def is_autostart():
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(k, REG_NAME); winreg.CloseKey(k); return True
    except Exception: return False

def set_autostart(enable):
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_SET_VALUE)
        if enable: winreg.SetValueEx(k, REG_NAME, 0, winreg.REG_SZ, _exe_path())
        else:
            try: winreg.DeleteValue(k, REG_NAME)
            except FileNotFoundError: pass
        winreg.CloseKey(k); return True
    except Exception: return False

# ── Protocolo ─────────────────────────────────────────────────────────────────

WRITE_UUIDS = [
    "0000fff3-0000-1000-8000-00805f9b34fb",
    "0000ffe1-0000-1000-8000-00805f9b34fb",
]
DEVICE_PREFIXES = ["ELK-BLE","MELK","LEDBLE","LED-","JACKYLED","XROCKER","ELK-BULB","ELK-BTC"]

EFFECTS = {
    "Sin efecto":             None,
    "🔀 Saltar RGB":          0x87,
    "🌈 Saltar 7 colores":    0x88,
    "🎨 Saltar RG":           0x92,
    "🔵 Saltar RB":           0x93,
    "🟢 Saltar GB":           0x94,
    "⚡ Parpadeo 7 colores":  0x95,
    "❤️ Parpadeo rojo":       0x96,
    "💚 Parpadeo verde":      0x97,
    "💙 Parpadeo azul":       0x98,
    "🟡 Parpadeo amarillo":   0x99,
    "🩵 Parpadeo cian":       0x9a,
    "💜 Parpadeo magenta":    0x9b,
    "⬜ Parpadeo blanco":     0x9c,
    "🌊 Gradiente 7 colores": 0x8b,
    "🌙 Gradiente RGB":       0x8d,
    "💜 Gradiente rojo":      0x8e,
    "💚 Gradiente verde":     0x8f,
    "💙 Gradiente azul":      0x90,
    "🟡 Gradiente amarillo":  0x8c,
    "🩵 Gradiente RG":        0x91,
}

def cmd_on():          return bytes([0x7e,0x00,0x04,0x01,0x00,0x00,0x00,0x00,0xef])
def cmd_off():         return bytes([0x7e,0x00,0x04,0x00,0x00,0x00,0xff,0x00,0xef])
def cmd_color(r,g,b):  return bytes([0x7e,0x00,0x05,0x03,r,g,b,0x00,0xef])
def cmd_brightness(p):
    v=max(0,min(100,int(p))); return bytes([0x7e,0x00,0x01,v,0x00,0x00,0x00,0x00,0xef])
def cmd_speed(p):
    v=max(0,min(100,int(p))); return bytes([0x7e,0x00,0x02,v,0x00,0x00,0x00,0x00,0xef])
def cmd_effect(eid):
    return bytes([0x7e,0x00,0x03,eid,0x03,0x00,0x00,0x00,0xef])

# ── Motor BLE ─────────────────────────────────────────────────────────────────

class BLEEngine:
    def __init__(self, on_log, on_status, on_devices):
        self.on_log=on_log; self.on_status=on_status; self.on_devices=on_devices
        self.loop=asyncio.new_event_loop(); self.client=self.write_uuid=None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop); self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _scan(self):
        self.on_log("🔍 Escaneando BLE (10 s)..."); self.on_status("scanning")
        try:
            devs = await BleakScanner.discover(timeout=10.0)
            found = [(d.name,d.address) for d in devs
                     if any((d.name or "").upper().startswith(p.upper()) for p in DEVICE_PREFIXES)]
            for n,a in found: self.on_log(f"   ✅ {n} [{a}]")
            if not found: self.on_log("   ⚠️  Sin dispositivos compatibles")
            self.on_devices(found)
        except Exception as e: self.on_log(f"   ❌ {e}")
        finally: self.on_status("idle")

    def scan(self): self.submit(self._scan())

    async def _connect(self, addr):
        self.on_log(f"🔗 Conectando a {addr}..."); self.on_status("connecting")
        try:
            if self.client and self.client.is_connected:
                await self.client.disconnect()
            self.client = BleakClient(addr)
            await self.client.connect()
            self.write_uuid = None
            for svc in self.client.services:
                for ch in svc.characteristics:
                    if ch.uuid in WRITE_UUIDS and \
                       ("write" in ch.properties or "write-without-response" in ch.properties):
                        self.write_uuid=ch.uuid; break
                if self.write_uuid: break
            if not self.write_uuid:
                for svc in self.client.services:
                    for ch in svc.characteristics:
                        if "write" in ch.properties or "write-without-response" in ch.properties:
                            self.write_uuid=ch.uuid
                            self.on_log(f"   ⚠️  UUID fallback: {self.write_uuid}"); break
                    if self.write_uuid: break
            if not self.write_uuid: raise RuntimeError("Sin UUID de escritura")
            self.on_log(f"   ✅ Conectado  UUID={self.write_uuid}")
            self.on_status("connected"); await self._write(cmd_on())
        except Exception as e:
            self.on_log(f"   ❌ {e}"); self.on_status("idle"); self.client=None

    def connect(self, addr): self.submit(self._connect(addr))

    async def _disconnect(self):
        try:
            if self.client and self.client.is_connected:
                await self.client.disconnect(); self.on_log("🔌 Desconectado")
        except Exception as e: self.on_log(f"   ⚠️  {e}")
        finally: self.client=self.write_uuid=None; self.on_status("idle")

    def disconnect(self): self.submit(self._disconnect())

    async def _write(self, data):
        if not(self.client and self.client.is_connected and self.write_uuid): return
        try: await self.client.write_gatt_char(self.write_uuid, data, response=False)
        except Exception as e: self.on_log(f"   ❌ BLE: {e}")

    def send(self, data): self.submit(self._write(data))

# ── Modo Música ───────────────────────────────────────────────────────────────

# ── Paleta de discoteca ultra saturada (cero luz blanca) ─────────────────────

DISCO_PALETTE = [
    (255, 0, 75),    # Magenta Neón / Rosa eléctrico
    (0, 240, 255),   # Cian láser
    (255, 230, 0),   # Amarillo eléctrico
    (150, 0, 255),   # Púrpura discoteca
    (0, 255, 60),    # Verde neón
    (255, 35, 0),    # Rojo fuego
    (0, 90, 255),    # Azul cobalto
    (255, 120, 0),   # Naranja láser
    (0, 255, 180),   # Turquesa brillante
    (255, 0, 190),   # Fucsia salvaje
]

class AudioAnalyzer:
    CHUNK = 1024

    def __init__(self, on_color, on_log, style="club", sensitivity=6, min_brightness=10):
        self.on_color       = on_color
        self.on_log         = on_log
        self.style          = style           # "club" | "strobe" | "pure_eq"
        self.sensitivity    = sensitivity     # 1 a 10 (umbral de beat)
        self.min_brightness = min_brightness / 100.0  # 0.0 a 0.30 (oscuridad de fondo)
        self._running       = False
        self._paused        = False
        self._bass_avg      = 0.5
        self._last_beat_t   = 0.0
        self._cur_color     = DISCO_PALETTE[0]
        self._intensity     = 0.0

    def start(self):
        try:
            import pyaudiowpatch  # lazy
        except ImportError:
            self.on_log("❌ pip install pyaudiowpatch"); return False
        try:
            import numpy  # lazy
        except ImportError:
            self.on_log("❌ pip install numpy"); return False
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()
        return True

    def stop(self):  self._running = False
    def pause(self): self._paused = True
    def resume(self): self._paused = False

    def update_params(self, style=None, sensitivity=None, min_brightness=None):
        if style is not None: self.style = style
        if sensitivity is not None: self.sensitivity = int(sensitivity)
        if min_brightness is not None: self.min_brightness = float(min_brightness) / 100.0

    def _loop(self):
        import pyaudiowpatch as pa
        import numpy as np
        import random
        p = stream = None
        try:
            p = pa.PyAudio()
            wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
            loopback = p.get_wasapi_loopback_analogue_by_index(wasapi["defaultOutputDevice"])
            sr = int(loopback["defaultSampleRate"])
            ch = int(loopback["maxInputChannels"])
            dev = int(loopback["index"])
            self.on_log(f"🎵 Modo Discoteca activo ({loopback['name']})")
            stream = p.open(format=pa.paFloat32, channels=ch, rate=sr,
                            input=True, input_device_index=dev,
                            frames_per_buffer=self.CHUNK)

            # Velocidad de decaimiento rítmico (~18% por buffer de 20ms)
            decay = 0.82

            while self._running:
                if self._paused:
                    time.sleep(0.1); continue

                raw = stream.read(self.CHUNK, exception_on_overflow=False)
                samples = np.frombuffer(raw, dtype=np.float32)
                if ch > 1: samples = samples.reshape(-1, ch).mean(axis=1)

                win = np.hanning(len(samples))
                fft = np.abs(np.fft.rfft(samples * win))
                freqs = np.fft.rfftfreq(len(samples), d=1.0 / sr)

                # Graves / Bombo (Kick): 30 - 130 Hz
                m_bass = (freqs >= 30) & (freqs < 130)
                bass = float(np.mean(fft[m_bass])) if m_bass.any() else 0.0

                # Medios: 300 - 2200 Hz
                m_mid = (freqs >= 300) & (freqs < 2200)
                mid = float(np.mean(fft[m_mid])) if m_mid.any() else 0.0

                # Agudos / Hi-Hats: 2500 - 14000 Hz
                m_high = (freqs >= 2500) & (freqs < 14000)
                high = float(np.mean(fft[m_high])) if m_high.any() else 0.0

                # Actualizar media adaptativa de graves
                self._bass_avg = self._bass_avg * 0.95 + bass * 0.05
                now = time.time()

                if self.style == "club":
                    # Umbral adaptativo según sensibilidad (1 a 10)
                    multiplier = 1.15 + (10 - self.sensitivity) * 0.12
                    threshold = max(0.08, self._bass_avg * multiplier)

                    # Detección de beat con bloqueo mínimo de 130ms entre bombos
                    is_beat = (bass > threshold) and (now - self._last_beat_t > 0.13)
                    if is_beat:
                        self._last_beat_t = now
                        candidates = [c for c in DISCO_PALETTE if c != self._cur_color]
                        self._cur_color = random.choice(candidates)
                        self._intensity = 1.0
                    else:
                        self._intensity = max(self.min_brightness, self._intensity * decay)

                    # Colores 100% saturados sin blanco
                    out_r = int(self._cur_color[0] * self._intensity)
                    out_g = int(self._cur_color[1] * self._intensity)
                    out_b = int(self._cur_color[2] * self._intensity)
                    self.on_color(out_r, out_g, out_b)

                elif self.style == "strobe":
                    threshold = max(0.12, self._bass_avg * (1.2 + (10 - self.sensitivity) * 0.15))
                    is_hit = (bass > threshold or high > threshold * 1.5) and (now - self._last_beat_t > 0.09)
                    if is_hit:
                        self._last_beat_t = now
                        flash = random.choice(DISCO_PALETTE)
                        self.on_color(flash[0], flash[1], flash[2])
                    else:
                        min_c = int(255 * self.min_brightness)
                        self.on_color(min_c, 0, min_c // 2)

                elif self.style == "pure_eq":
                    # Ecualizador puro: la banda dominante gana al 100% (evita luz blanca)
                    b = bass ** 2.2
                    m = (mid * 1.8) ** 2.2
                    h = (high * 2.2) ** 2.2
                    tot = b + m + h + 1e-9
                    nb, nm, nh = b / tot, m / tot, h / tot
                    mx = max(nb, nm, nh)

                    if mx == nb:
                        cur = (255, int(nm * 50), 0)    # Rojo / Naranja neón de graves
                    elif mx == nm:
                        cur = (0, 255, int(nh * 60))    # Verde esmeralda de medios
                    else:
                        cur = (int(nb * 70), 0, 255)    # Azul / Púrpura de agudos

                    vol = min(1.0, math.log1p(bass + mid + high) / math.log1p(3.0))
                    vol = max(self.min_brightness, vol)
                    self.on_color(int(cur[0] * vol), int(cur[1] * vol), int(cur[2] * vol))

        except Exception as e:
            self.on_log(f"❌ Audio: {e}")
        finally:
            self._running = False
            if stream:
                try: stream.stop_stream(); stream.close()
                except: pass
            if p:
                try: p.terminate()
                except: pass

# ── Modo Ambilight ────────────────────────────────────────────────────────────

class ScreenAnalyzer:
    FPS=10; SMOOTH=0.4

    def __init__(self, on_color, on_log, monitor_idx=1):
        self.on_color=on_color; self.on_log=on_log
        self.monitor_idx=monitor_idx; self._running=False; self._paused=False
        self._r=self._g=self._b=128.0

    @staticmethod
    def list_monitors():
        try:
            import mss as mss_lib
            with mss_lib.mss() as sct:
                return [f"Monitor {i}: {m['width']}x{m['height']}"
                        for i,m in enumerate(sct.monitors) if i>0]
        except Exception: return []

    def start(self):
        try: import mss  # lazy
        except ImportError: self.on_log("❌ pip install mss"); return False
        try: import numpy  # lazy
        except ImportError: self.on_log("❌ pip install numpy"); return False
        self._running=True
        threading.Thread(target=self._loop, daemon=True).start(); return True

    def stop(self):  self._running=False
    def pause(self): self._paused=True
    def resume(self):self._paused=False

    def _loop(self):
        import mss as mss_lib
        import numpy as np
        interval=1.0/self.FPS
        try:
            with mss_lib.mss() as sct:
                mons=sct.monitors
                if self.monitor_idx>=len(mons): self.monitor_idx=1
                mon=mons[self.monitor_idx]
                self.on_log(f"🖥️  Ambilight: {mon['width']}x{mon['height']} @ {self.FPS}fps")
                while self._running:
                    if self._paused:
                        time.sleep(0.2); continue
                    t0=time.time()
                    shot=sct.grab(mon)
                    arr=np.frombuffer(shot.bgra,dtype=np.uint8).reshape((shot.height,shot.width,4))
                    h,w=arr.shape[:2]; th,tw=18,32
                    sy=max(1,h//th); sx=max(1,w//tw)
                    small=arr[::sy,::sx,:3][:th,:tw]
                    r_ch=small[:,:,2].astype(np.float32)
                    g_ch=small[:,:,1].astype(np.float32)
                    b_ch=small[:,:,0].astype(np.float32)
                    mx=np.maximum(np.maximum(r_ch,g_ch),b_ch)
                    mn=np.minimum(np.minimum(r_ch,g_ch),b_ch)
                    sat=np.where(mx>0,(mx-mn)/(mx+1e-6),0)
                    wt=sat+0.15; total=wt.sum()+1e-9
                    ar=(r_ch*wt).sum()/total
                    ag=(g_ch*wt).sum()/total
                    ab=(b_ch*wt).sum()/total
                    hv,sv,vv=colorsys.rgb_to_hsv(ar/255,ag/255,ab/255)
                    sv=min(1.0,sv*1.8); vv=min(1.0,vv*1.2)
                    fr,fg,fb=colorsys.hsv_to_rgb(hv,sv,vv)
                    s=self.SMOOTH
                    self._r=self._r*(1-s)+fr*255*s
                    self._g=self._g*(1-s)+fg*255*s
                    self._b=self._b*(1-s)+fb*255*s
                    self.on_color(int(self._r),int(self._g),int(self._b))
                    elapsed=time.time()-t0
                    if elapsed<interval: time.sleep(interval-elapsed)
        except Exception as e:
            self.on_log(f"❌ Pantalla: {e}"); self._running=False

# ── Analizador de Portada (Media / Álbum de Windows) ──────────────────────────

class AlbumCoverAnalyzer:
    """Extrae el color de la portada del reproductor activo de Windows (Spotify, YouTube, etc.)"""
    def __init__(self, on_color, on_log, on_track=None, method="vibrant", sat_boost=1.8, bri_scale=0.85, pure_color=True):
        self.on_color   = on_color
        self.on_log     = on_log
        self.on_track   = on_track
        self.method     = method     # "vibrant" | "dominant"
        self.sat_boost  = sat_boost  # 1.0 - 2.5 (aumenta saturación para que los LEDs no se vean blancos)
        self.bri_scale  = bri_scale  # 0.4 - 1.0 (ajusta el brillo para no deslumbrar ni blanquear)
        self.pure_color = pure_color # forzar saturación mínima en colores cromáticos
        self._running   = False
        self._paused    = False
        self._thread    = None
        self._last_id   = None
        self._cur_rgb   = None
        self._last_raw  = None

    def start(self):
        if not MEDIA_OK:
            self.on_log("❌ winrt-Windows.Media.Control no disponible"); return False
        self._running = True; self._paused = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def pause(self): self._paused = True
    def resume(self): self._paused = False

    def set_method(self, m):
        self.method = m
        self._last_id = None  # forzar re-análisis inmediato

    def update_params(self, sat_boost=None, bri_scale=None, pure_color=None):
        if sat_boost is not None: self.sat_boost = float(sat_boost)
        if bri_scale is not None: self.bri_scale = float(bri_scale)
        if pure_color is not None: self.pure_color = bool(pure_color)
        if self._last_raw is not None:
            rgb = self._boost_led_color(*self._last_raw)
            self._cur_rgb = rgb
            self.on_color(rgb[0], rgb[1], rgb[2])
            if self.on_track and self._last_id:
                self.on_track(self._last_id, rgb, None)

    def _run_loop(self):
        self.on_log("💿 Modo Portada: iniciado (escuchando reproductor de Windows)")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            while self._running:
                if self._paused:
                    time.sleep(0.5); continue
                try:
                    loop.run_until_complete(self._check_media())
                except Exception:
                    pass
                for _ in range(10):
                    if not self._running or self._paused: break
                    time.sleep(0.1)
        finally:
            loop.close()

    async def _check_media(self):
        mgr = await SMTCManager.request_async()
        sess = mgr.get_current_session()
        if not sess:
            return
        props = await sess.try_get_media_properties_async()
        title = props.title or ""
        artist = props.artist or ""
        track_id = f"{title} - {artist}".strip(" -")
        if not track_id:
            return

        if track_id == self._last_id and self._cur_rgb is not None:
            return

        thumb = props.thumbnail
        if not thumb:
            self._last_id = track_id
            self.on_log(f"🎵 {track_id} (sin carátula)")
            if self.on_track:
                self.on_track(track_id, (180, 180, 180), None)
            return

        stream = await thumb.open_read_async()
        size = stream.size
        if size <= 0: return
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        buf = bytearray(size)
        reader.read_bytes(buf)
        img = PILImage.open(io.BytesIO(buf)).convert("RGB")

        raw_rgb = self._extract_raw_color(img, self.method)
        self._last_raw = raw_rgb
        rgb = self._boost_led_color(*raw_rgb)
        self._last_id = track_id
        self._cur_rgb = rgb

        self.on_log(f"🎵 {track_id} → Color LED RGB{rgb}")
        self.on_color(rgb[0], rgb[1], rgb[2])
        if self.on_track:
            self.on_track(track_id, rgb, img)

    def _boost_led_color(self, r, g, b):
        """Calibra el color para tiras LED RGB físicas evitando el blanqueado."""
        h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
        # Si tiene matiz cromático perceptible
        if s > 0.06:
            if self.pure_color:
                # Elevar saturación mínima para apagar los diodos secundarios que blanquean
                s = min(1.0, max(0.70, s * self.sat_boost))
            else:
                s = min(1.0, s * self.sat_boost)
        # Ajustar brillo para que no queme los fósforos y deslave el color
        v = min(1.0, v * self.bri_scale)
        fr, fg, fb = colorsys.hsv_to_rgb(h, s, v)
        return int(fr * 255), int(fg * 255), int(fb * 255)

    def _extract_raw_color(self, img, method):
        small = img.resize((48, 48), PILImage.Resampling.LANCZOS)
        quantized = small.quantize(colors=16, method=PILImage.Quantize.FASTOCTREE).convert("RGB")
        palette = quantized.getcolors(maxcolors=256) or []
        if not palette: return (200, 200, 200)

        if method == "dominant":
            # Color con más píxeles (descartando casi negros totales)
            valid = [c for c in palette if max(c[1]) > 25]
            if not valid: valid = palette
            return max(valid, key=lambda x: x[0])[1]

        # Método vibrante (estilo Wallpaper Engine / Spotify)
        vibrant = []
        fallback = []
        for count, (r, g, b) in palette:
            h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
            if v < 0.12: continue
            if s > 0.15:
                # Ponderación: saturación^1.4 x brillo^0.5
                score = count * (s ** 1.4) * (v ** 0.5)
                vibrant.append((score, (r, g, b)))
            else:
                score = count * (v ** 0.8)
                fallback.append((score, (r, g, b)))

        if vibrant:
            return max(vibrant, key=lambda x: x[0])[1]
        elif fallback:
            return max(fallback, key=lambda x: x[0])[1]
        return (200, 200, 200)

# ── Icono bandeja ─────────────────────────────────────────────────────────────

def _make_icon(size=64):
    img=PILImage.new("RGBA",(size,size),(0,0,0,0))
    d=ImageDraw.Draw(img)
    d.ellipse([2,2,size-2,size-2],fill=(30,20,50,255))
    d.ellipse([2,2,size-2,size-2],outline=(124,106,247,255),width=3)
    cx=cy=size//2; r=size//5
    d.ellipse([cx-r,cy-r,cx+r,cy+r],fill=(200,180,255,255))
    d.ellipse([cx-r//2,cy-r//2,cx,cy],fill=(255,255,255,200))
    return img

# ── Paleta ────────────────────────────────────────────────────────────────────

BG=DARK="#0f0f14"; PANEL="#1a1a24"; CARD="#22222e"
ACCENT="#7c6af7"; ACCENT2="#f76a8b"
TXT="#e8e8f0"; DIM="#6a6a80"
OK="#4ade80"; WARN="#facc15"; ERR="#f87171"
MUSIC="#f76af0"; AMBIL="#38bdf8"; MEDIA="#ec4899"

# ── Controlador principal (sin ventana Tk) ────────────────────────────────────
# Separamos el estado de la app (AppState) de la UI (AppWindow).
# Cuando la ventana se cierra se destruye completamente liberando su RAM.
# AppState sigue vivo manteniendo la conexión BLE y los workers.

class AppState:
    """Toda la lógica y estado que debe sobrevivir aunque la ventana esté cerrada."""

    def __init__(self):
        self.cfg         = load_config()
        self.connected   = False
        self.power_on    = True
        self.active_mode = None      # None | "music" | "screen"
        self._worker     = None
        self._last_sent  = (0,0,0)
        self._last_ts    = 0.0
        self._log_buf    = []        # buffer de log mientras la ventana está cerrada
        self._log_cb     = None      # callback hacia la ventana cuando está abierta
        self._status_cb  = None
        self._devices_cb = None
        self.window      = None      # referencia a AppWindow (puede ser None)

        self.ble = BLEEngine(self._log, self._on_status, self._on_devices) if BLEAK_OK else None

    # ── log ───────────────────────────────────────────────────────────────

    def _log(self, msg):
        self._log_buf.append(msg)
        if len(self._log_buf) > 300:        # límite buffer
            self._log_buf = self._log_buf[-200:]
        if self._log_cb:
            try: self._log_cb(msg)
            except Exception: pass

    # ── callbacks BLE (pueden llegar sin ventana) ─────────────────────────

    def _on_status(self, s):
        if s == "connected":
            self.connected = True
            if self._tray:
                try: self._tray.title = f"{APP_NAME} · Conectado"
                except: pass
        else:
            if s == "idle": self.connected = False
            if self._tray:
                try: self._tray.title = APP_NAME
                except: pass
        if self._status_cb:
            try: self._status_cb(s)
            except Exception: pass

    def _on_devices(self, devs):
        if self._devices_cb:
            try: self._devices_cb(devs)
            except Exception: pass

    # ── workers ───────────────────────────────────────────────────────────

    def stop_worker(self):
        if self._worker: self._worker.stop(); self._worker=None
        self.active_mode=None

    def start_music(self, style="club", sensitivity=6, min_brightness=10):
        self.stop_worker()
        w=AudioAnalyzer(self._on_reactive_color, self._log, style=style,
                        sensitivity=sensitivity, min_brightness=min_brightness)
        if w.start():
            self._worker=w; self.active_mode="music"; return True
        return False

    def start_screen(self, mon_idx=1):
        self.stop_worker()
        w=ScreenAnalyzer(self._on_reactive_color, self._log, monitor_idx=mon_idx)
        if w.start():
            self._worker=w; self.active_mode="screen"; return True
        return False

    def start_album(self, method="vibrant", sat_boost=1.8, bri_scale=0.85, pure_color=True):
        self.stop_worker()
        w=AlbumCoverAnalyzer(self._on_reactive_color, self._log, on_track=self._on_track_change,
                             method=method, sat_boost=sat_boost, bri_scale=bri_scale, pure_color=pure_color)
        if w.start():
            self._worker=w; self.active_mode="album"; return True
        return False

    def _on_track_change(self, track_str, rgb, img):
        if self.window:
            try:
                self.window.after(0, lambda: self.window.update_album_info(track_str, rgb, img))
            except Exception: pass

    def set_background(self, is_bg: bool):
        """Pausar/reanudar workers según si estamos en segundo plano."""
        if self._worker:
            if is_bg: self._worker.pause()
            else:     self._worker.resume()

    def _on_reactive_color(self, r, g, b):
        now=time.time()
        if now-self._last_ts<0.06: return
        self._last_ts=now
        if not self.connected: return
        pr,pg,pb=self._last_sent
        if abs(r-pr)+abs(g-pg)+abs(b-pb)<5: return
        self._last_sent=(r,g,b)
        if self.ble: self.ble.send(cmd_color(r,g,b))
        # notificar a la ventana si está abierta
        if self.window:
            try: self.window.after(0,lambda r=r,g=g,b=b: self.window.update_reactive(r,g,b))
            except Exception: pass

    def send(self, data):
        if not self.ble or not self.connected: return
        self.ble.send(data)

    # referencia al tray (se asigna desde afuera)
    _tray = None

# ── Ventana principal (se puede destruir y recrear) ───────────────────────────

class AppWindow(tk.Tk):
    def __init__(self, state: AppState, on_close_to_tray):
        super().__init__()
        self.state = state
        self._on_close_to_tray = on_close_to_tray
        self._monmap = {}

        self.title(APP_NAME)
        self.geometry("940x720"); self.minsize(820,600)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self._close)

        # Variables Tk (locales a la ventana)
        self.dev_var       = tk.StringVar()
        self.effect_var    = tk.StringVar(value="Sin efecto")
        self.speed_var     = tk.IntVar(value=60)
        self.bright_var    = tk.IntVar(value=80)
        self.r_var         = tk.IntVar(value=255)
        self.g_var         = tk.IntVar(value=100)
        self.b_var         = tk.IntVar(value=50)
        self.smooth_music  = tk.DoubleVar(value=0.35)
        self.smooth_screen = tk.DoubleVar(value=0.40)
        self.monitor_var   = tk.StringVar()
        self.autostart_var = tk.BooleanVar(value=is_autostart())
        self.cur_color     = (255,100,50)
        self.music_sens_var = tk.IntVar(value=6)
        self.music_dark_var = tk.IntVar(value=10)
        self._music_style_map = {
            "🎉 Club / Beat Jump (Cambio de color con bombo)": "club",
            "⚡ Strobe / Destellos rápidos": "strobe",
            "🌊 Ecualizador Puro (Sin luz blanca)": "pure_eq"
        }
        self._album_tk_img = None
        self.album_sat_var   = tk.DoubleVar(value=1.8)
        self.album_bri_var   = tk.IntVar(value=85)
        self.album_pure_var  = tk.BooleanVar(value=True)
        self._album_method_map = {
            "🎨 Vibrante / Acento (Wallpaper Engine)": "vibrant",
            "📊 Dominante (Color que más se repite)": "dominant"
        }

        # Conectar callbacks del estado a esta ventana
        state._log_cb     = self._append_log
        state._status_cb  = self._on_status
        state._devices_cb = self._on_devices
        state.window      = self

        self._build()

        # Volcar buffer de log acumulado mientras no había ventana
        for msg in state._log_buf:
            self._append_log(msg)

        # Restaurar estado visual
        if state.connected:
            self._on_status("connected")
        if state.active_mode == "music":
            self.btn_music.config(text="⏹  Detener modo discoteca", bg=ERR)
        elif state.active_mode == "screen":
            self.btn_screen.config(text="⏹  Detener modo pantalla", bg=ERR)
        elif state.active_mode == "album":
            self.btn_album.config(text="⏹  Detener modo portada", bg=ERR)

        # La ventana pasa a primer plano → reanudar workers
        state.set_background(False)

    def _close(self):
        """Ocultar ventana: destruirla para liberar RAM de Tk."""
        # Pausar workers (siguen corriendo en AppState, solo pausados)
        self.state.set_background(True)
        # Desconectar callbacks
        self.state._log_cb    = None
        self.state._status_cb = None
        self.state._devices_cb= None
        self.state.window     = None
        self._on_close_to_tray()
        self.destroy()   # ← libera toda la RAM de Tk (~150-200 MB)

    # ═══════════════════════════════════════════════════════════════════════
    # UI
    # ═══════════════════════════════════════════════════════════════════════

    def _build(self):
        hdr=tk.Frame(self,bg=PANEL,height=56); hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Label(hdr,text=f"● {APP_NAME}  LED Controller",fg=TXT,bg=PANEL,
                 font=("Courier New",15,"bold")).pack(side="left",padx=16,pady=12)
        self.lbl_st=tk.Label(hdr,text="⬤  Desconectado",fg=ERR,bg=PANEL,font=("Helvetica",10))
        self.lbl_st.pack(side="right",padx=16)

        body=tk.Frame(self,bg=BG); body.pack(fill="both",expand=True,padx=10,pady=8)
        right=tk.Frame(body,bg=BG,width=240); right.pack(side="right",fill="y",padx=(8,0))
        right.pack_propagate(False); self._build_log(right)

        lw=tk.Frame(body,bg=BG); lw.pack(side="left",fill="both",expand=True)
        self._cv=tk.Canvas(lw,bg=BG,highlightthickness=0)
        vsb=tk.Scrollbar(lw,orient="vertical",command=self._cv.yview)
        self._cv.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right",fill="y"); self._cv.pack(side="left",fill="both",expand=True)
        self._fr=tk.Frame(self._cv,bg=BG)
        self._wid=self._cv.create_window((0,0),window=self._fr,anchor="nw")
        self._fr.bind("<Configure>",lambda e:self._cv.configure(scrollregion=self._cv.bbox("all")))
        self._cv.bind("<Configure>",lambda e:self._cv.itemconfig(self._wid,width=e.width))
        self._cv.bind_all("<MouseWheel>",
            lambda e:self._cv.yview_scroll(-1*(e.delta//120),"units"))

        self._build_ble(self._fr)
        self._build_power(self._fr)
        self._build_color(self._fr)
        self._build_presets(self._fr)
        self._build_effects(self._fr)
        self._build_music(self._fr)
        self._build_screen(self._fr)
        self._build_album(self._fr)
        self._build_settings(self._fr)

    # ── helpers ──────────────────────────────────────────────────────────

    def _card(self,parent,title):
        f=tk.Frame(parent,bg=CARD); f.pack(fill="x",pady=(0,8))
        if title:
            tk.Label(f,text=title,fg=DIM,bg=CARD,
                     font=("Helvetica",8,"bold")).pack(anchor="w",padx=12,pady=(8,2))
        inn=tk.Frame(f,bg=CARD); inn.pack(fill="x",padx=12,pady=(0,10)); return inn

    def _btn(self,parent,text,cmd,bg=ACCENT,**kw):
        kw.setdefault("fg","white"); kw.setdefault("font",("Helvetica",10,"bold"))
        kw.setdefault("bd",0); kw.setdefault("relief","flat"); kw.setdefault("cursor","hand2")
        kw.setdefault("activebackground",bg); kw.setdefault("activeforeground","white")
        return tk.Button(parent,text=text,bg=bg,command=cmd,**kw)

    def _style_cb(self):
        s=ttk.Style(); s.theme_use("clam")
        s.configure("TCombobox",fieldbackground=PANEL,background=PANEL,foreground=TXT,
                    selectbackground=ACCENT,selectforeground="white",
                    bordercolor=CARD,lightcolor=CARD,darkcolor=CARD,arrowcolor=TXT)

    # ── paneles ──────────────────────────────────────────────────────────

    def _build_ble(self,p):
        inn=self._card(p,"DISPOSITIVO BLUETOOTH")
        r1=tk.Frame(inn,bg=CARD); r1.pack(fill="x",pady=(0,6))
        self.dev_cb=ttk.Combobox(r1,textvariable=self.dev_var,state="readonly",font=("Courier New",9))
        self.dev_cb.pack(side="left",fill="x",expand=True,padx=(0,6)); self._style_cb()
        self._btn(r1,"🔍 Escanear",self._do_scan,ACCENT).pack(side="left",ipadx=6,ipady=4)
        r2=tk.Frame(inn,bg=CARD); r2.pack(fill="x")
        self.btn_con=self._btn(r2,"Conectar",self._do_connect,OK)
        self.btn_dis=self._btn(r2,"Desconectar",self._do_disconnect,ERR)
        self.btn_con.pack(side="left",ipadx=12,ipady=5)
        self.btn_dis.pack(side="left",padx=8,ipadx=8,ipady=5)
        self.btn_dis.config(state="disabled" if not self.state.connected else "normal")
        self.btn_con.config(state="normal"   if not self.state.connected else "disabled")
        # Restaurar último dispositivo en el combo
        last=self.state.cfg.get("last_device")
        if last:
            lbl=f"{last.get('name','?')}  [{last.get('address','')}]"
            self.dev_cb["values"]=[lbl]; self.dev_var.set(lbl)

    def _build_power(self,p):
        inn=self._card(p,"ENCENDIDO / BRILLO")
        row=tk.Frame(inn,bg=CARD); row.pack(fill="x")
        self.btn_pwr=self._btn(row,"⏻  ENCENDIDO",self._toggle_power,OK)
        self.btn_pwr.pack(side="left",ipadx=12,ipady=6)
        tk.Label(row,text="Brillo:",fg=DIM,bg=CARD,font=("Helvetica",10)).pack(side="left",padx=(16,4))
        self.lbl_br=tk.Label(row,text="80%",fg=TXT,bg=CARD,font=("Courier New",11,"bold"),width=4)
        self.lbl_br.pack(side="right")
        tk.Scale(inn,from_=0,to=100,orient="horizontal",variable=self.bright_var,
                 bg=CARD,fg=TXT,troughcolor=PANEL,activebackground=ACCENT,
                 highlightthickness=0,bd=0,sliderlength=18,command=self._on_bright
                 ).pack(fill="x",pady=(4,0))

    def _build_color(self,p):
        inn=self._card(p,"COLOR")
        row=tk.Frame(inn,bg=CARD); row.pack(fill="x")
        WH=150
        self.wc=tk.Canvas(row,width=WH,height=WH,bg=CARD,highlightthickness=0,cursor="crosshair")
        self.wc.pack(side="left"); self._draw_wheel(WH)
        self.wc.bind("<Button-1>",self._wclick); self.wc.bind("<B1-Motion>",self._wclick)
        sf=tk.Frame(row,bg=CARD); sf.pack(side="left",fill="both",expand=True,padx=(12,0))
        for lbl,var,col in [("R",self.r_var,ERR),("G",self.g_var,OK),("B",self.b_var,"#60a5fa")]:
            r2=tk.Frame(sf,bg=CARD); r2.pack(fill="x",pady=2)
            tk.Label(r2,text=lbl,fg=col,bg=CARD,font=("Courier New",11,"bold"),width=2).pack(side="left")
            tk.Scale(r2,from_=0,to=255,orient="horizontal",variable=var,
                     bg=CARD,fg=TXT,troughcolor=PANEL,activebackground=col,
                     highlightthickness=0,bd=0,sliderlength=14,command=lambda _:self._on_rgb()
                     ).pack(side="left",fill="x",expand=True,padx=4)
            tk.Label(r2,textvariable=var,fg=DIM,bg=CARD,font=("Courier New",9),width=3).pack(side="right")
        pr=tk.Frame(sf,bg=CARD); pr.pack(fill="x",pady=(8,0))
        tk.Label(pr,text="Vista previa:",fg=DIM,bg=CARD,font=("Helvetica",9)).pack(side="left")
        self.preview=tk.Label(pr,text="       ",width=8,relief="flat",bg=self._hex(*self.cur_color))
        self.preview.pack(side="left",padx=6,ipady=8)
        self._btn(sf,"▶  Enviar color",self._send_color,ACCENT).pack(fill="x",pady=(8,0),ipady=5)

    def _draw_wheel(self,WH):
        cx=cy=WH//2; r=WH//2-4
        for y in range(WH):
            for x in range(WH):
                dx,dy=x-cx,y-cy; d=(dx*dx+dy*dy)**.5
                if d<=r:
                    h=math.atan2(dy,dx)/(2*math.pi)%1.0
                    rgb=colorsys.hsv_to_rgb(h,d/r,1.0)
                    c="#{:02x}{:02x}{:02x}".format(int(rgb[0]*255),int(rgb[1]*255),int(rgb[2]*255))
                    self.wc.create_rectangle(x,y,x+1,y+1,outline=c,fill=c)
        self.wc.create_oval(cx-r-1,cy-r-1,cx+r+1,cy+r+1,outline=PANEL,width=3)
        self.wm=self.wc.create_oval(cx-5,cy-5,cx+5,cy+5,outline="white",fill="",width=2)

    def _wclick(self,e):
        WH=150; cx=cy=WH//2; r=WH//2-4
        dx,dy=e.x-cx,e.y-cy; d=min((dx*dx+dy*dy)**.5,r)
        h=math.atan2(dy,dx)/(2*math.pi)%1.0
        rgb=colorsys.hsv_to_rgb(h,d/r,1.0)
        rv,gv,bv=int(rgb[0]*255),int(rgb[1]*255),int(rgb[2]*255)
        self.r_var.set(rv); self.g_var.set(gv); self.b_var.set(bv); self._upd_prev(rv,gv,bv)
        mag=max((dx*dx+dy*dy)**.5,1); nx,ny=cx+dx/mag*d,cy+dy/mag*d
        self.wc.coords(self.wm,nx-5,ny-5,nx+5,ny+5)

    def _on_rgb(self): self._upd_prev(self.r_var.get(),self.g_var.get(),self.b_var.get())
    def _upd_prev(self,r,g,b): self.cur_color=(r,g,b); self.preview.config(bg=self._hex(r,g,b))
    def _hex(self,r,g,b): return "#{:02x}{:02x}{:02x}".format(r,g,b)

    def _build_presets(self,p):
        inn=self._card(p,"COLORES RAPIDOS")
        row=tk.Frame(inn,bg=CARD); row.pack(fill="x")
        for emoji,r,g,b,bg in [
            ("❤️",255,0,0,"#f87171"),("🟠",255,80,0,"#fb923c"),("💛",255,200,0,"#facc15"),
            ("💚",0,255,0,"#4ade80"),("🩵",0,255,200,"#2dd4bf"),("💙",0,80,255,"#60a5fa"),
            ("💜",120,0,255,"#a78bfa"),("🩷",255,0,160,"#f472b6"),
            ("⬜",255,255,255,"#cbd5e1"),("🌡️",255,180,80,"#fde68a"),
        ]:
            tk.Button(row,text=emoji,bg=bg,fg="white",bd=0,relief="flat",cursor="hand2",font=("",13),
                      command=lambda r=r,g=g,b=b:self._preset(r,g,b)
                      ).pack(side="left",padx=2,ipadx=3,ipady=3)

    def _preset(self,r,g,b):
        self.r_var.set(r); self.g_var.set(g); self.b_var.set(b); self._upd_prev(r,g,b)
        self._send_color()

    def _build_effects(self,p):
        inn=self._card(p,"EFECTOS")
        row=tk.Frame(inn,bg=CARD); row.pack(fill="x")
        self.eff_cb=ttk.Combobox(row,textvariable=self.effect_var,state="readonly",
                                   values=list(EFFECTS.keys()),font=("Helvetica",10))
        self.eff_cb.pack(side="left",fill="x",expand=True,padx=(0,8))
        self._btn(row,"▶ Aplicar",self._send_effect,ACCENT2).pack(side="left",ipadx=10,ipady=4)
        sr=tk.Frame(inn,bg=CARD); sr.pack(fill="x",pady=(6,0))
        tk.Label(sr,text="Velocidad:",fg=DIM,bg=CARD,font=("Helvetica",9)).pack(side="left")
        tk.Scale(sr,from_=1,to=100,orient="horizontal",variable=self.speed_var,
                 bg=CARD,fg=TXT,troughcolor=PANEL,activebackground=ACCENT2,
                 highlightthickness=0,bd=0,sliderlength=14
                 ).pack(side="left",fill="x",expand=True,padx=6)
        tk.Label(sr,textvariable=self.speed_var,fg=DIM,bg=CARD,font=("Courier New",9),width=3).pack(side="right")

    def _build_music(self,p):
        inn=self._card(p,"🎵  MODO DISCOTECA / MÚSICA EN VIVO")
        tk.Label(inn,fg=DIM,bg=CARD,font=("Helvetica",8),justify="left",
                 text="Luces reactivas al ritmo de la música con colores neón puros (sin luz blanca).\n"
                      "Requiere:  pip install pyaudiowpatch numpy"
                 ).pack(anchor="w",pady=(0,6))

        mr=tk.Frame(inn,bg=CARD); mr.pack(fill="x",pady=(0,6))
        tk.Label(mr,text="Estilo de fiesta:",fg=DIM,bg=CARD,font=("Helvetica",9)).pack(side="left",padx=(0,6))
        self.music_style_cb=ttk.Combobox(mr,values=list(self._music_style_map.keys()),state="readonly",font=("Helvetica",8))
        self.music_style_cb.current(0)
        self.music_style_cb.pack(side="left",fill="x",expand=True)
        self.music_style_cb.bind("<<ComboboxSelected>>",self._on_music_param_change)

        sr1=tk.Frame(inn,bg=CARD); sr1.pack(fill="x",pady=(4,2))
        tk.Label(sr1,text="Sensibilidad al bombo:",fg=DIM,bg=CARD,font=("Helvetica",8)).pack(side="left")
        tk.Scale(sr1,from_=1,to=10,orient="horizontal",variable=self.music_sens_var,
                 bg=CARD,fg=TXT,troughcolor=PANEL,activebackground=MUSIC,
                 highlightthickness=0,bd=0,sliderlength=14,
                 command=lambda v:self._on_music_param_change()
                 ).pack(side="left",fill="x",expand=True,padx=6)
        tk.Label(sr1,textvariable=self.music_sens_var,fg=DIM,bg=CARD,font=("Courier New",8),width=3).pack(side="right")

        sr2=tk.Frame(inn,bg=CARD); sr2.pack(fill="x",pady=(0,6))
        tk.Label(sr2,text="Luz entre bombos:",fg=DIM,bg=CARD,font=("Helvetica",8)).pack(side="left")
        tk.Scale(sr2,from_=0,to=30,orient="horizontal",variable=self.music_dark_var,
                 bg=CARD,fg=TXT,troughcolor=PANEL,activebackground=MUSIC,
                 highlightthickness=0,bd=0,sliderlength=14,
                 command=lambda v:self._on_music_param_change()
                 ).pack(side="left",fill="x",expand=True,padx=6)
        tk.Label(sr2,textvariable=self.music_dark_var,fg=DIM,bg=CARD,font=("Courier New",8),width=3).pack(side="right")

        pr=tk.Frame(inn,bg=CARD); pr.pack(fill="x",pady=(2,6))
        tk.Label(pr,text="Color en vivo:",fg=DIM,bg=CARD,font=("Helvetica",9)).pack(side="left")
        self.music_preview=tk.Label(pr,text="         ",width=10,relief="flat",bg=CARD)
        self.music_preview.pack(side="left",padx=6,ipady=4)
        self.lbl_music=tk.Label(pr,text="",fg=DIM,bg=CARD,font=("Courier New",8))
        self.lbl_music.pack(side="left")

        self.btn_music=self._btn(inn,"🎵  Activar modo discoteca",self._toggle_music,MUSIC)
        self.btn_music.pack(fill="x",ipady=6)

    def _build_screen(self,p):
        inn=self._card(p,"🖥️  MODO PANTALLA (Ambilight)")
        tk.Label(inn,fg=DIM,bg=CARD,font=("Helvetica",8),justify="left",
                 text="Los LEDs cambian con el color predominante de la pantalla.\n"
                      "Requiere:  pip install mss numpy"
                 ).pack(anchor="w",pady=(0,6))
        mr=tk.Frame(inn,bg=CARD); mr.pack(fill="x",pady=(0,6))
        tk.Label(mr,text="Monitor:",fg=DIM,bg=CARD,font=("Helvetica",9)).pack(side="left",padx=(0,4))
        self.mon_cb=ttk.Combobox(mr,textvariable=self.monitor_var,state="readonly",font=("Courier New",8))
        self.mon_cb.pack(side="left",fill="x",expand=True,padx=(0,4))
        self._btn(mr,"↺",self._refresh_monitors,PANEL,fg=DIM,font=("Helvetica",12)
                  ).pack(side="left",ipadx=4,ipady=1)
        self._refresh_monitors()
        pr=tk.Frame(inn,bg=CARD); pr.pack(fill="x",pady=(0,6))
        tk.Label(pr,text="Color actual:",fg=DIM,bg=CARD,font=("Helvetica",9)).pack(side="left")
        self.screen_preview=tk.Label(pr,text="         ",width=10,relief="flat",bg=CARD)
        self.screen_preview.pack(side="left",padx=6,ipady=8)
        self.lbl_screen_rgb=tk.Label(pr,text="",fg=DIM,bg=CARD,font=("Courier New",8))
        self.lbl_screen_rgb.pack(side="left")
        sr=tk.Frame(inn,bg=CARD); sr.pack(fill="x",pady=(0,6))
        tk.Label(sr,text="Suavizado:",fg=DIM,bg=CARD,font=("Helvetica",9)).pack(side="left")
        tk.Scale(sr,from_=0.05,to=1.0,resolution=0.05,orient="horizontal",
                 variable=self.smooth_screen,bg=CARD,fg=TXT,troughcolor=PANEL,
                 activebackground=AMBIL,highlightthickness=0,bd=0,sliderlength=14,
                 command=lambda v:setattr(self.state._worker,"SMOOTH",float(v))
                                  if isinstance(self.state._worker,ScreenAnalyzer) else None
                 ).pack(side="left",fill="x",expand=True,padx=6)
        self.btn_screen=self._btn(inn,"🖥️  Activar modo pantalla",self._toggle_screen,AMBIL)
        self.btn_screen.pack(fill="x",ipady=6)

    def _build_album(self,p):
        inn=self._card(p,"💿  MODO PORTADA (Álbum / Reproductor)")
        tk.Label(inn,fg=DIM,bg=CARD,font=("Helvetica",8),justify="left",
                 text="Sincroniza los LEDs con la carátula de lo que escuchas (Spotify, YouTube, etc.).\n"
                      "Requiere:  pip install winrt-Windows.Media.Control"
                 ).pack(anchor="w",pady=(0,6))
        mr=tk.Frame(inn,bg=CARD); mr.pack(fill="x",pady=(0,6))
        tk.Label(mr,text="Modo de color:",fg=DIM,bg=CARD,font=("Helvetica",9)).pack(side="left",padx=(0,6))
        self.album_method_cb=ttk.Combobox(mr,values=list(self._album_method_map.keys()),state="readonly",font=("Helvetica",8))
        self.album_method_cb.current(0)
        self.album_method_cb.pack(side="left",fill="x",expand=True)
        self.album_method_cb.bind("<<ComboboxSelected>>",self._on_album_method_change)

        # Calibración óptica para LEDs: evitar color deslavado/blanco y deslumbramiento
        sr1=tk.Frame(inn,bg=CARD); sr1.pack(fill="x",pady=(4,2))
        tk.Label(sr1,text="Saturación (Color vivo):",fg=DIM,bg=CARD,font=("Helvetica",8)).pack(side="left")
        tk.Scale(sr1,from_=1.0,to=2.5,resolution=0.1,orient="horizontal",
                 variable=self.album_sat_var,bg=CARD,fg=TXT,troughcolor=PANEL,
                 activebackground=MEDIA,highlightthickness=0,bd=0,sliderlength=14,
                 command=lambda v:self._on_album_param_change()
                 ).pack(side="left",fill="x",expand=True,padx=6)
        tk.Label(sr1,textvariable=self.album_sat_var,fg=DIM,bg=CARD,font=("Courier New",8),width=4).pack(side="right")

        sr2=tk.Frame(inn,bg=CARD); sr2.pack(fill="x",pady=(0,4))
        tk.Label(sr2,text="Brillo de carátula:",fg=DIM,bg=CARD,font=("Helvetica",8)).pack(side="left")
        tk.Scale(sr2,from_=30,to=100,resolution=5,orient="horizontal",
                 variable=self.album_bri_var,bg=CARD,fg=TXT,troughcolor=PANEL,
                 activebackground=MEDIA,highlightthickness=0,bd=0,sliderlength=14,
                 command=lambda v:self._on_album_param_change()
                 ).pack(side="left",fill="x",expand=True,padx=6)
        tk.Label(sr2,textvariable=self.album_bri_var,fg=DIM,bg=CARD,font=("Courier New",8),width=4).pack(side="right")

        prev_row=tk.Frame(inn,bg=CARD); prev_row.pack(fill="x",pady=(4,8))
        self.album_thumb_canvas=tk.Canvas(prev_row,width=50,height=50,bg=PANEL,highlightthickness=0)
        self.album_thumb_canvas.pack(side="left",padx=(0,8))

        info_col=tk.Frame(prev_row,bg=CARD); info_col.pack(side="left",fill="both",expand=True)
        self.lbl_album_track=tk.Label(info_col,text="Sin reproducción activa",fg=TXT,bg=CARD,
                                      font=("Helvetica",9,"bold"),anchor="w",justify="left")
        self.lbl_album_track.pack(fill="x")
        self.lbl_album_rgb=tk.Label(info_col,text="Esperando música...",fg=DIM,bg=CARD,
                                    font=("Courier New",8),anchor="w")
        self.lbl_album_rgb.pack(fill="x")

        self.album_color_preview=tk.Label(prev_row,text="       ",width=8,relief="flat",bg=CARD)
        self.album_color_preview.pack(side="right",padx=6,ipady=12)

        self.btn_album=self._btn(inn,"💿  Activar modo portada",self._toggle_album,MEDIA)
        self.btn_album.pack(fill="x",ipady=6)

    def _build_settings(self,p):
        inn=self._card(p,"⚙️  AJUSTES")
        cf=tk.Frame(inn,bg=CARD); cf.pack(fill="x",pady=(0,8))
        tk.Checkbutton(cf,text="Iniciar con Windows",variable=self.autostart_var,
                       command=self._toggle_autostart,bg=CARD,fg=TXT,selectcolor=PANEL,
                       activebackground=CARD,activeforeground=TXT,
                       font=("Helvetica",10),cursor="hand2").pack(side="left")
        self.lbl_auto=tk.Label(cf,text="",fg=DIM,bg=CARD,font=("Helvetica",8))
        self.lbl_auto.pack(side="left",padx=(8,0))
        tk.Label(inn,fg=DIM,bg=CARD,font=("Helvetica",8),justify="left",
                 text="Al cerrar la ventana la app sigue en segundo plano (~15 MB).\n"
                      "Haz clic en el icono de la bandeja para volver a abrirla.\n"
                      "Requiere:  pip install pystray pillow"
                 ).pack(anchor="w")

    def _build_log(self,p):
        tk.Label(p,text="REGISTRO",fg=DIM,bg=BG,font=("Helvetica",8,"bold")).pack(anchor="w",pady=(0,4))
        lf=tk.Frame(p,bg=CARD); lf.pack(fill="both",expand=True)
        self.log_txt=tk.Text(lf,bg=CARD,fg=DIM,font=("Courier New",8),wrap="word",
                              bd=0,padx=6,pady=6,state="disabled",selectbackground=ACCENT)
        sb=tk.Scrollbar(lf,command=self.log_txt.yview,bg=CARD)
        self.log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right",fill="y"); self.log_txt.pack(fill="both",expand=True)
        self._btn(p,"Limpiar log",self._clear_log,PANEL,fg=DIM).pack(fill="x",pady=(4,0),ipady=2)

    def _refresh_monitors(self):
        mons=ScreenAnalyzer.list_monitors()
        self.mon_cb["values"]=mons
        self._monmap={m:i+1 for i,m in enumerate(mons)}
        if mons: self.monitor_var.set(mons[0])

    # ── acciones ─────────────────────────────────────────────────────────

    def _do_scan(self):
        if not BLEAK_OK: self.state._log("❌ pip install bleak"); return
        self.dev_cb["values"]=[]; self.state.ble.scan()

    def _do_connect(self):
        if not BLEAK_OK: return
        sel=self.dev_var.get()
        if not sel: messagebox.showwarning("Sin dispositivo","Escanea primero."); return
        addr=sel.split("[")[-1].rstrip("]") if "[" in sel else sel
        name=sel.split("[")[0].strip() if "[" in sel else addr
        self.state.cfg["last_device"]={"address":addr,"name":name}
        save_config(self.state.cfg)
        self.state.ble.connect(addr)

    def _do_disconnect(self):
        self.state.stop_worker()
        self._reset_mode_buttons()
        if self.state.ble: self.state.ble.disconnect()

    def _toggle_power(self):
        self.state.power_on=not self.state.power_on
        if self.state.power_on:
            self.btn_pwr.config(text="⏻  ENCENDIDO",bg=OK)
            self.state.send(cmd_on()); self.state._log("💡 Encendido")
        else:
            self.btn_pwr.config(text="⏸  APAGADO",bg=ERR)
            self.state.send(cmd_off()); self.state._log("🌑 Apagado")

    def _on_bright(self,val):
        p=int(val); self.lbl_br.config(text=f"{p}%"); self.state.send(cmd_brightness(p))

    def _send_color(self):
        r,g,b=self.r_var.get(),self.g_var.get(),self.b_var.get()
        self.state.send(cmd_color(r,g,b)); self.state._log(f"🎨 Color RGB({r},{g},{b})")

    def _send_effect(self):
        if not self.state.connected: self.state._log("⚠️  Conecta primero"); return
        name=self.effect_var.get(); eid=EFFECTS.get(name)
        if eid is None: self.state._log("ℹ️  Elige un efecto (no 'Sin efecto')"); return
        spd=self.speed_var.get()
        self.state.ble.send(cmd_speed(spd)); self.state.ble.send(cmd_effect(eid))
        self.state._log(f"✨ Efecto {name}  id=0x{eid:02x}  vel={spd}")

    def _toggle_music(self):
        if self.state.active_mode=="music":
            self.state.stop_worker(); self._reset_mode_buttons()
            self.state._log("⏹  Modo discoteca desactivado"); return
        if not self.state.connected:
            self.state._log("⚠️  Conecta primero un dispositivo LED"); return
        self._reset_mode_buttons()
        style_name=self.music_style_cb.get()
        style=self._music_style_map.get(style_name,"club")
        sens=self.music_sens_var.get()
        dark=self.music_dark_var.get()
        if self.state.start_music(style=style,sensitivity=sens,min_brightness=dark):
            self.btn_music.config(text="⏹  Detener modo discoteca",bg=ERR)
            self.state._log("🎵 Modo discoteca activo")

    def _on_music_param_change(self,e=None):
        if isinstance(self.state._worker,AudioAnalyzer):
            style_name=self.music_style_cb.get()
            style=self._music_style_map.get(style_name,"club")
            sens=self.music_sens_var.get()
            dark=self.music_dark_var.get()
            self.state._worker.update_params(style=style,sensitivity=sens,min_brightness=dark)

    def _toggle_screen(self):
        if self.state.active_mode=="screen":
            self.state.stop_worker(); self._reset_mode_buttons()
            self.state._log("⏹  Modo pantalla desactivado"); return
        if not self.state.connected:
            self.state._log("⚠️  Conecta primero un dispositivo LED"); return
        self._reset_mode_buttons()
        mon_idx=self._monmap.get(self.monitor_var.get(),1)
        if self.state.start_screen(mon_idx):
            self.btn_screen.config(text="⏹  Detener modo pantalla",bg=ERR)
            self.state._log("🖥️  Modo pantalla activo")

    def _toggle_album(self):
        if self.state.active_mode=="album":
            self.state.stop_worker(); self._reset_mode_buttons()
            self.state._log("⏹  Modo portada desactivado"); return
        if not self.state.connected:
            self.state._log("⚠️  Conecta primero un dispositivo LED"); return
        self._reset_mode_buttons()
        method_name=self.album_method_cb.get()
        method=self._album_method_map.get(method_name,"vibrant")
        sat=self.album_sat_var.get()
        bri=self.album_bri_var.get()/100.0
        pure=self.album_pure_var.get()
        if self.state.start_album(method=method,sat_boost=sat,bri_scale=bri,pure_color=pure):
            self.btn_album.config(text="⏹  Detener modo portada",bg=ERR)
            self.state._log("💿 Modo portada activo")

    def _on_album_param_change(self):
        if isinstance(self.state._worker,AlbumCoverAnalyzer):
            sat=self.album_sat_var.get()
            bri=self.album_bri_var.get()/100.0
            pure=self.album_pure_var.get()
            self.state._worker.update_params(sat_boost=sat,bri_scale=bri,pure_color=pure)

    def _on_album_method_change(self,e=None):
        method_name=self.album_method_cb.get()
        method=self._album_method_map.get(method_name,"vibrant")
        if isinstance(self.state._worker,AlbumCoverAnalyzer):
            self.state._worker.set_method(method)
            self.state._log(f"💿 Método portada cambiado a: {method}")

    def update_album_info(self,track_str,rgb,pil_img):
        try:
            self.lbl_album_track.config(text=track_str[:38]+("..." if len(track_str)>38 else ""))
            self.lbl_album_rgb.config(text=f"Color RGB: ({rgb[0]}, {rgb[1]}, {rgb[2]})")
            hex_c=f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
            self.album_color_preview.config(bg=hex_c)
            if pil_img and ImageTk:
                thumb=pil_img.resize((50,50),PILImage.Resampling.LANCZOS)
                self._album_tk_img=ImageTk.PhotoImage(thumb)
                self.album_thumb_canvas.delete("all")
                self.album_thumb_canvas.create_image(25,25,image=self._album_tk_img)
        except Exception: pass

    def _reset_mode_buttons(self):
        self.btn_music.config(text="🎵  Activar modo discoteca",bg=MUSIC)
        self.btn_screen.config(text="🖥️  Activar modo pantalla",bg=AMBIL)
        self.btn_album.config(text="💿  Activar modo portada",bg=MEDIA)
        self.music_preview.config(bg=CARD); self.lbl_music.config(text="")
        self.screen_preview.config(bg=CARD); self.lbl_screen_rgb.config(text="")
        self.album_color_preview.config(bg=CARD)

    def _toggle_autostart(self):
        ok=set_autostart(self.autostart_var.get())
        if ok:
            estado="activado" if self.autostart_var.get() else "desactivado"
            self.lbl_auto.config(text=f"✅ {estado}")
        else:
            self.lbl_auto.config(text="❌ Error")
            self.autostart_var.set(not self.autostart_var.get())

    # ── log ───────────────────────────────────────────────────────────────

    def _append_log(self,msg):
        try:
            self.log_txt.config(state="normal")
            self.log_txt.insert("end",msg+"\n")
            self.log_txt.see("end")
            self.log_txt.config(state="disabled")
        except Exception: pass

    def _clear_log(self):
        self.state._log_buf.clear()
        self.log_txt.config(state="normal"); self.log_txt.delete("1.0","end")
        self.log_txt.config(state="disabled")

    # ── callbacks BLE → UI ────────────────────────────────────────────────

    def _on_status(self,s):
        try:
            if s=="scanning":    self.lbl_st.config(text="⬤  Escaneando...",fg=WARN)
            elif s=="connecting":self.lbl_st.config(text="⬤  Conectando...",fg=WARN)
            elif s=="connected":
                self.lbl_st.config(text="⬤  Conectado",fg=OK)
                self.btn_con.config(state="disabled"); self.btn_dis.config(state="normal")
            else:
                self.lbl_st.config(text="⬤  Desconectado",fg=ERR)
                self.btn_con.config(state="normal"); self.btn_dis.config(state="disabled")
        except Exception: pass

    def _on_devices(self,devs):
        try:
            if devs:
                vals=[f"{n}  [{a}]" for n,a in devs]
                self.dev_cb["values"]=vals; self.dev_var.set(vals[0])
                self.state._log(f"📋 {len(devs)} dispositivo(s)")
            else:
                self.dev_cb["values"]=[]; self.dev_var.set("")
        except Exception: pass

    def update_reactive(self,r,g,b):
        """Actualizar VU/preview desde el hilo de audio/pantalla."""
        try:
            hx=self._hex(r,g,b)
            if self.state.active_mode=="music":
                self.music_preview.config(bg=hx)
                self.lbl_music.config(text=f" RGB({r:3d},{g:3d},{b:3d})",
                                       fg=hx if max(r,g,b)>25 else DIM)
            elif self.state.active_mode=="screen":
                self.screen_preview.config(bg=hx)
                self.lbl_screen_rgb.config(text=f" RGB({r:3d},{g:3d},{b:3d})",
                                            fg=hx if max(r,g,b)>20 else DIM)
        except Exception: pass

# ── Punto de entrada ──────────────────────────────────────────────────────────

def main():
    state = AppState()

    # Log inicial
    state._log(f"💡 {APP_NAME} Controller listo")
    if not BLEAK_OK: state._log("⚠️  pip install bleak")
    if not TRAY_OK:  state._log("⚠️  pip install pystray pillow")

    # Reconexión automática
    last = state.cfg.get("last_device")
    if last and BLEAK_OK:
        addr=last.get("address"); name=last.get("name","?")
        if addr:
            state._log(f"🔄 Reconectando con '{name}' [{addr}]...")
            # Conectar tras 1 s sin bloquear
            def _auto():
                time.sleep(1.0)
                state.ble.connect(addr)
            threading.Thread(target=_auto, daemon=True).start()

    window_ref = [None]   # lista para poder mutar desde closures

    def open_window():
        if window_ref[0] is not None:
            try: window_ref[0].lift(); window_ref[0].focus_force(); return
            except Exception: window_ref[0]=None
        win = AppWindow(state, on_close_to_tray=lambda: None)
        window_ref[0] = win
        win.mainloop()
        window_ref[0] = None

    def tray_show(icon=None, item=None):
        threading.Thread(target=open_window, daemon=True).start()

    def tray_power_on(icon=None, item=None):
        state.power_on=True; state.send(cmd_on()); state._log("💡 Encendido desde bandeja")

    def tray_power_off(icon=None, item=None):
        state.power_on=False; state.send(cmd_off()); state._log("🌑 Apagado desde bandeja")

    def tray_quit(icon=None, item=None):
        state.stop_worker()
        if state.ble: state.ble.disconnect()
        if tray_icon: tray_icon.stop()

    if TRAY_OK:
        icon_img = _make_icon()
        menu = pystray.Menu(
            pystray.MenuItem("Mostrar ventana", tray_show, default=True),
            pystray.MenuItem("Encender LEDs",   tray_power_on),
            pystray.MenuItem("Apagar LEDs",     tray_power_off),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir",           tray_quit),
        )
        tray_icon = pystray.Icon(APP_NAME, icon_img, APP_NAME, menu)
        AppState._tray = tray_icon

        # Abrir ventana inicial en hilo separado
        threading.Thread(target=open_window, daemon=True).start()
        tray_icon.run()   # bloquea el hilo principal (necesario para la bandeja)
    else:
        # Sin bandeja: abrir ventana directamente
        tray_icon = None
        open_window()


if __name__ == "__main__":
    main()
