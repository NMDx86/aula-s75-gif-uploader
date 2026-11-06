#!/usr/bin/env python3
"""
AULA S75 Pro screen uploader, native version (no browser, no WebHID).

Same protocol as the web page (ported from AULA Toolkit, GPL-3.0, (C) 2026 Livvaa):
  BEGIN, SCREEN(slot 1, chunk count), 4096-byte chunks on the screen interface, END.
The log lines match the web page's, so the two runs can be compared.

This version lets you choose the HID devices yourself, in case auto-detection
does not find your keyboard (different PID, usage pages reported as 0, etc.).

Install:   pip3 install hidapi pillow
Run:       python3 aula_uploader_pick.py            (GUI, has a Device section)
           python3 aula_uploader_pick.py --list             (show HID devices, then exit)
           python3 aula_uploader_pick.py --list --all       (include non-AULA devices)
           python3 aula_uploader_pick.py my.gif --cli --pick         (choose interactively)
           python3 aula_uploader_pick.py my.gif --cli --ctrl 2 --screen 3   (numbers from --list)
           python3 aula_uploader_pick.py --cli --test      (colour test pattern)

The keyboard exposes two interfaces: a control interface (feature reports,
usage page 0xFF13) and a screen interface (data chunks, usage page 0xFF68).
You pick one of each.
"""
import argparse
import json
import math
import platform
import queue
import struct
import sys
import threading
import time
import urllib.request
import uuid

BUILD = 'native-1-pick'
SHARE_URL = ''  # e.g. 'https://xxxx.trycloudflare.com' - if set, the log is posted to <SHARE_URL>/api/log

VID, PID = 0x0C45, 0x800A
W, H, HEAD, MAX_FRAMES, CHUNK = 135, 240, 256, 141, 4096
OP_BEGIN, OP_END, OP_SCREEN = 0x18, 0x02, 0x72
# Tunable settings (also set from the window or the command line). Times are in ms.
CFG = dict(
    slot=1,               # screen slot the image is written to
    speed=100,            # animation speed in %, 200 = twice as fast
    min_delay=30,         # shortest frame delay; frames are dropped to keep to it
    max_delay=500,        # longest frame delay (the keyboard stores at most 510)
    cmd_delay=35,         # pause around control commands
    ack_wait=2000,        # how long to wait for a chunk's acknowledgement
    first_ack_wait=300,   # how long to wait for the first acknowledgement
    resends=2,            # times a chunk is sent again before giving up
    paced_delay=8,        # pause between chunks when not waiting for acknowledgements
    paced=False,          # True = never wait for acknowledgements, just pace the chunks
)
CTRL_PAGE, SCREEN_PAGE = 0xFF13, 0xFF68
IS_MAC = sys.platform == 'darwin'


# ---------------------------------------------------------------- protocol (pure)

def command(op, b2=0, count=0):
    b = bytearray(64)
    b[0], b[1], b[2] = 0x04, op, b2
    b[8], b[9] = count & 0xFF, (count >> 8) & 0xFF
    return bytes(b)


def rgb565(im):
    """PIL RGB image (135x240) -> RGB565 little-endian bytes."""
    d = im.tobytes()
    px = [((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3) for r, g, b in zip(d[0::3], d[1::3], d[2::3])]
    return struct.pack('<%dH' % len(px), *px)


def screen_image(frames):
    """frames: [(rgb565 bytes, delay_ms)] -> (data padded with FF to a multiple of 4096, chunk count)."""
    fs = W * H * 2
    chunks = -(-(HEAD + fs * len(frames)) // CHUNK)
    buf = bytearray(b'\xff' * (chunks * CHUNK))
    buf[0] = len(frames)
    for i, (px, delay) in enumerate(frames):
        buf[1 + i] = max(1, min(255, int(delay / 2 + 0.5)))
        buf[HEAD + i * fs:HEAD + (i + 1) * fs] = px[:fs]
    return bytes(buf), chunks


def test_pattern():
    """Every 4096-byte chunk gets its own colour (16 hues), bright/dark every 512 bytes."""
    def hsv(h, v):
        def k(a):
            x = (a + h / 60) % 6
            return v * (1 - max(0, min(x, 4 - x, 1)))
        return [int(k(a) * 255 + 0.5) for a in (5, 3, 1)]
    out = bytearray(W * H * 2)
    for p in range(W * H):
        o = HEAD + p * 2
        c, sub = o // CHUNK, (o % CHUNK) // 512
        r, g, b = hsv(c * 360 / 16, 0.45 if sub % 2 else 1)
        v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[p * 2], out[p * 2 + 1] = v & 0xFF, v >> 8
    return bytes(out), 1000


def fit_frames(delays, mx, min_delay=0):
    """Spread at most mx frames evenly over the animation without changing its speed -> [(index, delay_ms)]."""
    if len(delays) <= mx and all(d >= min_delay for d in delays):
        return list(enumerate(delays))
    starts, total = [], 0
    for d in delays:
        starts.append(total)
        total += d
    count = max(1, min(mx, int(total // min_delay) if min_delay > 0 else mx))
    step = total / count
    out, i = [], 0
    for k in range(count):
        t = k * step
        while i + 1 < len(delays) and starts[i + 1] <= t:
            i += 1
        if out and out[-1][0] == i:
            out[-1][1] += step
        else:
            out.append([i, step])
    return [(a, int(b + 0.5)) for a, b in out]


# ---------------------------------------------------------------- images

def load_gif(path, mode='cover'):
    """-> (list of RGB 135x240 PIL images, list of delays in ms)."""
    from PIL import Image, ImageSequence
    src = Image.open(path)
    images, delays = [], []
    for fr in ImageSequence.Iterator(src):
        delay = fr.info.get('duration', 100) or 0
        rgba = fr.convert('RGBA')
        flat = Image.new('RGB', rgba.size, (0, 0, 0))
        flat.paste(rgba, mask=rgba.split()[3])
        w, h = flat.size
        if mode == 'stretch':
            im = flat.resize((W, H), Image.LANCZOS)
        else:
            s = max(W / w, H / h) if mode == 'cover' else min(W / w, H / h)
            nw, nh = max(1, round(w * s)), max(1, round(h * s))
            scaled = flat.resize((nw, nh), Image.LANCZOS)
            im = Image.new('RGB', (W, H), (0, 0, 0))
            im.paste(scaled, ((W - nw) // 2, (H - nh) // 2))
        images.append(im)
        delays.append(100 if delay <= 10 else delay)
    return images, delays


def plan(images, delays, limit):
    sp = max(CFG['speed'], 1) / 100.0
    scaled = [d / sp for d in delays]
    return [(images[i], max(1, min(CFG['max_delay'], int(d + 0.5))))
            for i, d in fit_frames(scaled, limit, CFG['min_delay'])]


# ---------------------------------------------------------------- device

class Log:
    def __init__(self, sink=None):
        self.lines, self.sink = [], sink
        self.session = uuid.uuid4().hex[:16]
        self.share_url = SHARE_URL
        self._told = False

    def __call__(self, msg):
        line = '[%s] %s' % (time.strftime('%I:%M:%S %p'), msg)
        self.lines.append(line)
        if self.sink:
            self.sink(line)
        else:
            print(line, flush=True)

    def text(self):
        return '\n'.join(self.lines) + '\n'

    def share(self):
        if not self.share_url:
            return
        if not self._told:
            self._told = True
            self('log shared publicly as session %s (see /logs)' % self.session)
        body = json.dumps({'id': self.session, 'build': BUILD, 'text': self.text()}).encode()
        url = self.share_url.rstrip('/') + '/api/log'

        def post():
            try:
                req = urllib.request.Request(url, body, {'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=10).read()
            except Exception:
                pass
        threading.Thread(target=post, daemon=True).start()


def list_devices(show_all=False):
    """HID devices, sorted so the interfaces of one device sit together.
    Default: only the AULA VID/PID. show_all: every HID device on the system."""
    import hid
    devs = list(hid.enumerate(0, 0) if show_all else hid.enumerate(VID, PID))
    devs.sort(key=lambda d: (d.get('vendor_id', 0) != VID, d.get('vendor_id', 0), d.get('product_id', 0),
                             d.get('interface_number', -1), d.get('usage_page', 0), d.get('usage', 0)))
    return devs


def describe(d):
    return '%04x:%04x  %s  |  iface %s  usage %04x/%04x' % (
        d.get('vendor_id', 0), d.get('product_id', 0),
        d.get('product_string') or d.get('manufacturer_string') or 'HID device',
        d.get('interface_number'), d.get('usage_page', 0), d.get('usage', 0))


def guess_interfaces(devs):
    """-> (control index or None, screen index or None), by usage page, preferring the AULA VID."""
    ctrl = scr = None
    for prefer_vid in (True, False):
        for i, d in enumerate(devs):
            if prefer_vid and d.get('vendor_id') != VID:
                continue
            up, u = d.get('usage_page', 0), d.get('usage', 0)
            if ctrl is None and CTRL_PAGE in (up, u):
                ctrl = i
            if scr is None and SCREEN_PAGE in (up, u):
                scr = i
    return ctrl, scr


def print_devices(devs):
    c, s = guess_interfaces(devs)
    for i, d in enumerate(devs):
        tag = '  <- control (guess)' if i == c else '  <- screen (guess)' if i == s else ''
        print('%3d  %s%s' % (i, describe(d), tag))


class Keyboard:
    def __init__(self, log):
        self.log, self.ctrl, self.screen = log, None, None

    def open(self, ctrl_path=None, screen_path=None):
        """With both paths given, open exactly those. Otherwise auto-detect like the original script."""
        import hid
        if ctrl_path is None or screen_path is None:
            devs = hid.enumerate(VID, PID)
            if not devs:
                raise RuntimeError('No AULA keyboard found with VID/PID %04x:%04x. Plug it in with the USB cable, '
                                   'switch it to USB mode, or choose the devices yourself (--list / --pick, or the '
                                   'Device section in the window).' % (VID, PID))
            for d in devs:
                self.log(describe(d))
            c, s = guess_interfaces(devs)
            if c is None or s is None:
                raise RuntimeError('Found the keyboard, but not its %s interface. Choose the devices yourself '
                                   '(--list / --pick, or the Device section in the window).' %
                                   ('configuration and screen' if c is None and s is None else 'screen' if c is not None else 'configuration'))
            ctrl_path, screen_path = devs[c]['path'], devs[s]['path']
        self.ctrl, self.screen = hid.device(), hid.device()
        try:
            self.ctrl.open_path(ctrl_path)
            self.log('opened control interface: %r' % (ctrl_path,))
            self.screen.open_path(screen_path)
            self.log('opened screen interface: %r' % (screen_path,))
            # read(n, 0) blocks forever on a blocking handle; reads with a timeout still wait as asked.
            self.screen.set_nonblocking(1)
        except Exception as e:
            self.close()
            raise RuntimeError('could not open the HID device (%s). On macOS grant Input Monitoring to your '
                               'terminal; on Linux check hidraw permissions (udev rule or sudo); on Windows '
                               'close other AULA software.' % e)

    def close(self):
        for d in (self.ctrl, self.screen):
            try:
                d.close()
            except Exception:
                pass
        self.ctrl = self.screen = None

    def feature(self, data, readback):
        time.sleep(CFG['cmd_delay'] / 1000)
        self.ctrl.send_feature_report(b'\x00' + data)
        if not readback:
            return None
        time.sleep(CFG['cmd_delay'] / 1000)
        res = self._get()
        if res[1] == data[1] and res[3] == 0xFF:
            self.log('device answered ff, retrying once')
            time.sleep(CFG['cmd_delay'] / 1000)
            self.ctrl.send_feature_report(b'\x00' + data)
            time.sleep(CFG['cmd_delay'] / 1000)
            res = self._get()
        return res

    def _get(self):
        r = bytes(self.ctrl.get_feature_report(0, 65))
        return r[1:] if len(r) == 65 else r

    def drain(self):
        # The screen handle is non-blocking (set in open), so this returns at once when nothing is queued.
        n = 0
        while n < 256 and self.screen.read(64, 0):
            n += 1
        return n

    def write_chunk(self, chunk):
        n = self.screen.write(b'\x00' + chunk)
        if n < 0:
            raise RuntimeError('write failed: %s' % self.screen.error())
        return n


# ---------------------------------------------------------------- upload

def run_upload(kb, frames, log, settle_ms=0, start_ms=0, progress=None, cancel=None):
    """frames: [(rgb565 bytes, delay_ms)]. Mirrors the web page: wait for each chunk's acknowledgement."""
    if not frames or len(frames) > MAX_FRAMES:
        raise RuntimeError('Need 1 to %d frames' % MAX_FRAMES)
    data, chunks = screen_image(frames)
    s = dict(last_ok=0, acks=0, misses=0, resends=0, paced=CFG['paced'], slow=0, max_ack=0.0, send_total=0.0)
    recent, payloads = [], {}
    t0 = time.time()

    def summary():
        total = chunks
        if s['last_ok'] < total:
            log('stopped after chunk %d of %d (%.1f%%). For comparison, 63/64 of %d is %d' %
                (s['last_ok'], total, 100 * s['last_ok'] / total, total, round(total * 63 / 64)))
        log('chunks acknowledged %d/%d, timed out %d, resent %d%s' %
            (s['acks'], total, s['misses'], s['resends'], ', paced mode' if s['paced'] else ''))
        log('timing: slowest acknowledgement %.0f ms, %d over 300 ms, average write %.0f ms, settle %d ms%s' %
            (s['max_ack'], s['slow'], s['send_total'] / s['acks'] if s['acks'] else 0, settle_ms, ' (macOS)' if IS_MAC else ''))
        log('last acknowledgements (chunk: ms): ' + (', '.join('%d: %d' % r for r in recent) or 'none'))
        for k, n in payloads.items():
            log('acknowledgement bytes %s x%d' % (k, n))

    def hexs(b, n=16):
        return ' '.join('%02x' % x for x in b[:n]) + (' … (%d B)' % len(b) if len(b) > n else '')

    log('uploading %d frames to slot %d (%d chunks)' % (len(frames), CFG['slot'], chunks))
    log('sending BEGIN')
    r = kb.feature(command(OP_BEGIN), True)
    log('BEGIN reply: ' + hexs(bytes(r)))
    log('sending SCREEN (slot %d, %d chunks)' % (CFG['slot'], chunks))
    r = kb.feature(command(OP_SCREEN, CFG['slot'], chunks), True)
    log('SCREEN reply: ' + hexs(bytes(r)))
    try:
        if start_ms:
            log('waiting %d ms before the first chunk' % start_ms)
            time.sleep(start_ms / 1000)
        for i in range(chunks):
            if cancel and cancel():
                raise RuntimeError('cancelled')
            if progress:
                progress(i, chunks)
            chunk = data[i * CHUNK:(i + 1) * CHUNK]
            if s['paced']:
                kb.write_chunk(chunk)
                time.sleep(CFG['paced_delay'] / 1000)
                continue
            attempt = 0
            while True:
                stale = kb.drain()
                if stale:
                    log('chunk %d: %d unread input report(s) discarded before sending' % (i + 1, stale))
                t = time.perf_counter()
                kb.write_chunk(chunk)
                sent = time.perf_counter()
                wait = CFG['first_ack_wait'] if s['acks'] == 0 else CFG['ack_wait']
                got = bytes(kb.screen.read(64, int(wait)))
                if got:
                    ack_ms = (time.perf_counter() - t) * 1000
                    s['last_ok'] = i + 1
                    s['acks'] += 1
                    s['send_total'] += (sent - t) * 1000
                    s['max_ack'] = max(s['max_ack'], ack_ms)
                    if ack_ms > 300:
                        s['slow'] += 1
                    key = hexs(got)
                    new = key not in payloads
                    if len(payloads) < 6 or not new:
                        payloads[key] = payloads.get(key, 0) + 1
                    recent.append((i + 1, round(ack_ms)))
                    del recent[:-6]
                    if s['acks'] == 1:
                        log('first acknowledgement: ' + key)
                    elif new and len(payloads) <= 6:
                        log('chunk %d: acknowledgement changed to %s' % (i + 1, key))
                    if settle_ms:
                        time.sleep(settle_ms / 1000)
                    break
                s['misses'] += 1
                if s['acks'] == 0:
                    if s['misses'] >= 4:
                        s['paced'] = True
                        log('no acknowledgements from the screen interface, pacing chunks every %d ms instead' % CFG['paced_delay'])
                    break
                if attempt >= CFG['resends']:
                    raise RuntimeError('the keyboard stopped acknowledging data at chunk %d' % (i + 1))
                attempt += 1
                s['resends'] += 1
                log('chunk %d: no acknowledgement after %d ms, sending it again' % (i + 1, CFG['ack_wait']))
    except Exception:
        try:
            kb.feature(command(OP_END), False)
        except Exception:
            pass
        summary()
        raise
    kb.feature(command(OP_END), True)
    if progress:
        progress(chunks, chunks)
    log('done: %d chunks in %.0fs' % (chunks, time.time() - t0))
    summary()


def log_header(log):
    log('page build: %s, Python %s, %s' % (BUILD, platform.python_version(), platform.platform()))
    try:
        import hid
        log('hidapi module: %s' % getattr(hid, '__version__', 'unknown'))
    except Exception as e:
        log('hidapi import failed: %s' % e)
    log('platform: %s' % ('macOS' if IS_MAC else platform.system()))


DEFAULTS = dict(settle=50 if IS_MAC else 0, start=1000 if IS_MAC else 0, cap=60 if IS_MAC else MAX_FRAMES)


def do_upload(log, frames, settle, start, progress=None, cancel=None, ctrl_path=None, screen_path=None):
    kb = Keyboard(log)
    try:
        kb.open(ctrl_path, screen_path)
        log('settle %d ms, start wait %d ms, %d frame(s)' % (settle, start, len(frames)))
        log('config: ' + ', '.join('%s=%s' % kv for kv in CFG.items()))
        run_upload(kb, frames, log, settle, start, progress, cancel)
    finally:
        kb.close()
        log.share()


# ---------------------------------------------------------------- GUI

def gui(args):
    import tkinter as tk
    from tkinter import filedialog, ttk
    try:
        from PIL import ImageTk
    except ImportError:
        ImageTk = None

    root = tk.Tk()
    root.title('AULA S75 Pro screen uploader (%s)' % BUILD)
    q = queue.Queue()
    log = Log(sink=lambda line: q.put(('log', line)))
    if args.share:
        log.share_url = args.share
    state = dict(images=[], delays=[], path='', busy=False, cancel=False, preview=[], shown=0, tkimg=None, timer=None)

    frm = ttk.Frame(root, padding=10)
    frm.grid(sticky='nsew')
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    left = ttk.Frame(frm)
    left.grid(row=0, column=0, sticky='n', padx=(0, 12))
    canvas = tk.Canvas(left, width=W * 2, height=H * 2, bg='black', highlightthickness=1)
    canvas.pack()
    info = ttk.Label(left, text='No GIF chosen', wraplength=W * 2)
    info.pack(pady=4)

    right = ttk.Frame(frm)
    right.grid(row=0, column=1, sticky='nsew')
    frm.columnconfigure(1, weight=1)

    btn_pick = ttk.Button(right, text='Choose GIF…')
    btn_pick.grid(row=0, column=0, sticky='w')

    # ---- device selection
    dev = ttk.LabelFrame(right, text='HID device', padding=6)
    dev.grid(row=1, column=0, sticky='ew', pady=6)
    dev.columnconfigure(1, weight=1)
    v_all = tk.BooleanVar(value=False)
    v_ctrl = tk.StringVar()
    v_scr = tk.StringVar()
    devs_now = []
    ttk.Label(dev, text='Control interface').grid(row=0, column=0, sticky='w')
    cb_ctrl = ttk.Combobox(dev, textvariable=v_ctrl, state='readonly', width=58)
    cb_ctrl.grid(row=0, column=1, sticky='ew', padx=6, pady=1)
    ttk.Label(dev, text='Screen interface').grid(row=1, column=0, sticky='w')
    cb_scr = ttk.Combobox(dev, textvariable=v_scr, state='readonly', width=58)
    cb_scr.grid(row=1, column=1, sticky='ew', padx=6, pady=1)
    devrow = ttk.Frame(dev)
    devrow.grid(row=2, column=0, columnspan=2, sticky='w', pady=(4, 0))
    btn_refresh = ttk.Button(devrow, text='Refresh')
    btn_refresh.pack(side='left')
    ttk.Checkbutton(devrow, text='Show all HID devices', variable=v_all,
                    command=lambda: refresh_devices()).pack(side='left', padx=8)
    dev_note = ttk.Label(dev, text='', wraplength=520)
    dev_note.grid(row=3, column=0, columnspan=2, sticky='w')

    def refresh_devices(*_):
        try:
            found = list_devices(v_all.get())
        except Exception as e:
            found = []
            dev_note.config(text='Could not list HID devices: %s' % e)
        devs_now[:] = found
        names = ['%d: %s' % (i, describe(d)) for i, d in enumerate(found)]
        cb_ctrl.config(values=names)
        cb_scr.config(values=names)
        c, s = guess_interfaces(found)
        v_ctrl.set(names[c] if c is not None else '')
        v_scr.set(names[s] if s is not None else '')
        if not found:
            dev_note.config(text='No AULA device found. Plug in the cable, set USB mode, or tick "Show all HID devices".')
        elif c is None or s is None:
            dev_note.config(text='Could not guess every interface. Pick them by hand '
                                 '(control is usually usage page ff13, screen ff68).')
        else:
            dev_note.config(text='%d interface(s) found. Control and screen were picked automatically; change them if needed.' % len(found))

    def selected_paths():
        def idx(v):
            t = v.get()
            return int(t.split(':', 1)[0]) if t else None
        c, s = idx(v_ctrl), idx(v_scr)
        if c is None or s is None or c >= len(devs_now) or s >= len(devs_now):
            raise RuntimeError('Choose both a control interface and a screen interface.')
        return devs_now[c]['path'], devs_now[s]['path']

    opts = ttk.LabelFrame(right, text='Options', padding=6)
    opts.grid(row=2, column=0, sticky='ew', pady=6)
    v_settle = tk.IntVar(value=args.settle if args.settle is not None else DEFAULTS['settle'])
    v_start = tk.IntVar(value=args.start if args.start is not None else DEFAULTS['start'])
    v_cap = tk.IntVar(value=args.maxframes if args.maxframes is not None else DEFAULTS['cap'])
    v_mode = tk.StringVar(value='cover')
    cfg_vars = {k: tk.IntVar(value=int(CFG[k])) for k in
                ('slot', 'speed', 'min_delay', 'max_delay', 'cmd_delay', 'ack_wait', 'first_ack_wait', 'resends', 'paced_delay')}
    v_paced = tk.BooleanVar(value=bool(CFG['paced']))
    rows = [('Max frames (animation)', v_cap, 1, MAX_FRAMES),
            ('Speed (%, 200 = twice as fast)', cfg_vars['speed'], 10, 1000),
            ('Shortest frame delay (ms)', cfg_vars['min_delay'], 2, 500),
            ('Longest frame delay (ms)', cfg_vars['max_delay'], 2, 510),
            ('Screen slot', cfg_vars['slot'], 0, 9),
            ('Pause after each chunk (ms)', v_settle, 0, 1000),
            ('Wait before first chunk (ms)', v_start, 0, 5000),
            ('Pause around commands (ms)', cfg_vars['cmd_delay'], 0, 500),
            ('Wait for chunk acknowledgement (ms)', cfg_vars['ack_wait'], 50, 10000),
            ('Wait for first acknowledgement (ms)', cfg_vars['first_ack_wait'], 50, 10000),
            ('Resends per chunk', cfg_vars['resends'], 0, 10),
            ('Pace between chunks, no-ack mode (ms)', cfg_vars['paced_delay'], 0, 200)]
    for r, (label, var, lo, hi) in enumerate(rows):
        ttk.Label(opts, text=label).grid(row=r, column=0, sticky='w')
        ttk.Spinbox(opts, from_=lo, to=hi, textvariable=var, width=7).grid(row=r, column=1, padx=6)
    n = len(rows)
    ttk.Checkbutton(opts, text="Don't wait for acknowledgements (send paced)", variable=v_paced).grid(
        row=n, column=0, columnspan=2, sticky='w')
    ttk.Label(opts, text='Fit').grid(row=n + 1, column=0, sticky='w')
    cb = ttk.Combobox(opts, textvariable=v_mode, values=['cover', 'contain', 'stretch'], width=8, state='readonly')
    cb.grid(row=n + 1, column=1, padx=6)
    ttk.Label(opts, text='Share log to (optional)').grid(row=n + 2, column=0, sticky='w')
    v_share = tk.StringVar(value=log.share_url)
    ttk.Entry(opts, textvariable=v_share, width=34).grid(row=n + 3, column=0, columnspan=2, sticky='ew')

    def apply_cfg():
        for k, v in cfg_vars.items():
            try:
                CFG[k] = int(v.get())
            except Exception:
                raise RuntimeError('Setting "%s" must be a whole number.' % k)
        CFG['paced'] = bool(v_paced.get())
        if CFG['min_delay'] > CFG['max_delay']:
            raise RuntimeError('Shortest frame delay is larger than the longest.')

    row = ttk.Frame(right)
    row.grid(row=3, column=0, sticky='w')
    btn_up = ttk.Button(row, text='Upload', state='disabled')
    btn_up.pack(side='left')
    btn_test = ttk.Button(row, text='Colour test pattern')
    btn_test.pack(side='left', padx=6)
    btn_stop = ttk.Button(row, text='Stop', state='disabled')
    btn_stop.pack(side='left')
    bar = ttk.Progressbar(right, maximum=1.0)
    bar.grid(row=4, column=0, sticky='ew', pady=6)
    status = ttk.Label(right, text='Choose a GIF, then Upload.', wraplength=420)
    status.grid(row=5, column=0, sticky='w')

    txt = tk.Text(right, width=64, height=14, wrap='word')
    txt.grid(row=6, column=0, sticky='nsew', pady=6)
    right.rowconfigure(6, weight=1)
    btn_copy = ttk.Button(right, text='Copy log')
    btn_copy.grid(row=7, column=0, sticky='w')

    def say(t):
        status.config(text=t)

    def sync():
        btn_up.config(state='normal' if state['images'] and not state['busy'] else 'disabled')
        btn_test.config(state='disabled' if state['busy'] else 'normal')
        btn_pick.config(state='disabled' if state['busy'] else 'normal')
        btn_refresh.config(state='disabled' if state['busy'] else 'normal')
        btn_stop.config(state='normal' if state['busy'] else 'disabled')

    def restart_preview(*_):
        if state['timer']:
            root.after_cancel(state['timer'])
            state['timer'] = None
        if not state['images'] or not ImageTk:
            return
        state['preview'] = plan(state['images'], state['delays'], max(1, min(v_cap.get(), MAX_FRAMES)))
        state['shown'] = 0
        tick()

    def tick():
        pv = state['preview']
        if not pv:
            return
        im, d = pv[state['shown'] % len(pv)]
        state['tkimg'] = ImageTk.PhotoImage(im.resize((W * 2, H * 2)))
        canvas.create_image(0, 0, anchor='nw', image=state['tkimg'])
        state['shown'] += 1
        if len(pv) > 1:
            state['timer'] = root.after(d, tick)

    def pick():
        path = filedialog.askopenfilename(title='Choose a GIF', filetypes=[('GIF / images', '*.gif *.png *.jpg *.jpeg *.webp'), ('All files', '*.*')])
        if not path:
            return
        say('Reading the file…')
        root.update_idletasks()
        try:
            state['images'], state['delays'] = load_gif(path, v_mode.get())
        except Exception as e:
            say('Could not read that file: %s' % e)
            return
        state['path'] = path
        n = len(state['images'])
        info.config(text='%s: %d frame%s' % (path.split('/')[-1].split('\\')[-1], n, '' if n == 1 else 's'))
        say('Ready to upload.')
        restart_preview()
        sync()

    def on_mode(_=None):
        if state['path']:
            try:
                state['images'], state['delays'] = load_gif(state['path'], v_mode.get())
                restart_preview()
            except Exception as e:
                say(str(e))

    def worker(frames, paths):
        try:
            log.share_url = v_share.get().strip()
            log_header(log)
            do_upload(log, frames, v_settle.get(), v_start.get(),
                      progress=lambda d, t: q.put(('bar', d / t)), cancel=lambda: state['cancel'],
                      ctrl_path=paths[0], screen_path=paths[1])
            q.put(('done', 'Uploaded. The keyboard keeps it, even on other computers.'))
        except Exception as e:
            log('error: %s' % e)
            log.share()
            q.put(('done', 'Upload failed: %s' % e))

    def start(frames):
        try:
            paths = selected_paths()
            apply_cfg()
        except Exception as e:
            say(str(e))
            return
        state['busy'], state['cancel'] = True, False
        bar['value'] = 0
        txt.delete('1.0', 'end')
        log.lines.clear()
        log._told = False
        log.session = uuid.uuid4().hex[:16]
        say('Uploading. Keep this window open and leave the keyboard plugged in.')
        sync()
        threading.Thread(target=worker, args=(frames, paths), daemon=True).start()

    def upload():
        try:
            apply_cfg()
        except Exception as e:
            say(str(e))
            return
        pv = plan(state['images'], state['delays'], max(1, min(v_cap.get(), MAX_FRAMES)))
        start([(rgb565(im), d) for im, d in pv])

    def pattern():
        start([test_pattern()])

    def copy():
        root.clipboard_clear()
        root.clipboard_append(log.text())
        say('Log copied.')

    def stop():
        state['cancel'] = True

    def poll():
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == 'log':
                    txt.insert('end', val + '\n')
                    txt.see('end')
                elif kind == 'bar':
                    bar['value'] = val
                elif kind == 'done':
                    state['busy'] = False
                    say(val)
                    sync()
        except queue.Empty:
            pass
        root.after(60, poll)

    btn_pick.config(command=pick)
    btn_up.config(command=upload)
    btn_test.config(command=pattern)
    btn_stop.config(command=stop)
    btn_copy.config(command=copy)
    btn_refresh.config(command=refresh_devices)
    cb.bind('<<ComboboxSelected>>', on_mode)
    v_cap.trace_add('write', lambda *_: restart_preview())
    refresh_devices()
    poll()
    root.mainloop()


# ---------------------------------------------------------------- CLI / entry

def main():
    ap = argparse.ArgumentParser(description='AULA S75 Pro screen uploader (native HID)')
    ap.add_argument('gif', nargs='?', help='GIF to upload')
    ap.add_argument('--cli', action='store_true', help='no window')
    ap.add_argument('--test', action='store_true', help='send the colour test pattern')
    ap.add_argument('--settle', type=int, help='ms pause after each chunk (default 50 on macOS, else 0)')
    ap.add_argument('--start', type=int, help='ms wait before the first chunk (default 1000 on macOS, else 0)')
    ap.add_argument('--maxframes', type=int, help='frame cap (default 60 on macOS, else 141)')
    ap.add_argument('--fit', default='cover', choices=['cover', 'contain', 'stretch'])
    ap.add_argument('--share', default='', help='post the log to <URL>/api/log')
    for key, default in CFG.items():
        if key == 'paced':
            ap.add_argument('--paced', action='store_true', default=None,
                            help="don't wait for acknowledgements, pace the chunks instead")
        else:
            ap.add_argument('--' + key.replace('_', '-'), type=int, dest='cfg_' + key,
                            help='%s (default %s)' % (key.replace('_', ' '), default))
    ap.add_argument('--list', action='store_true', help='list HID devices (numbered) and exit')
    ap.add_argument('--all', action='store_true', help='with --list/--pick/--ctrl/--screen: include every HID device, not only the AULA VID/PID')
    ap.add_argument('--pick', action='store_true', help='choose the control and screen interface interactively')
    ap.add_argument('--ctrl', type=int, help='control interface, number from --list')
    ap.add_argument('--screen', type=int, help='screen interface, number from --list')
    args = ap.parse_args()
    for key in CFG:
        val = args.paced if key == 'paced' else getattr(args, 'cfg_' + key)
        if val is not None:
            CFG[key] = val

    if args.list:
        try:
            devs = list_devices(args.all)
        except Exception as e:
            print('Could not list HID devices: %s' % e)
            sys.exit(1)
        if not devs:
            print('No HID devices found%s.' % ('' if args.all else ' for %04x:%04x (try --all)' % (VID, PID)))
        else:
            print_devices(devs)
        return

    if not (args.cli or args.test):
        try:
            import tkinter  # noqa: F401
        except ImportError:
            print('tkinter is not available, falling back to the command line (use --cli). '
                  'On Homebrew Python: brew install python-tk')
            sys.exit(1)
        return gui(args)

    log = Log()
    if args.share:
        log.share_url = args.share
    log_header(log)
    settle = DEFAULTS['settle'] if args.settle is None else args.settle
    start = DEFAULTS['start'] if args.start is None else args.start
    cap = DEFAULTS['cap'] if args.maxframes is None else args.maxframes
    try:
        ctrl_path = screen_path = None
        if args.pick or args.ctrl is not None or args.screen is not None:
            devs = list_devices(args.all)
            if not devs:
                raise RuntimeError('No HID devices found%s.' % ('' if args.all else ' for %04x:%04x (try --all)' % (VID, PID)))
            gc, gs = guess_interfaces(devs)
            c, s = args.ctrl, args.screen
            if args.pick:
                print_devices(devs)

                def ask(label, default, current):
                    if current is not None:
                        return current
                    shown = '' if default is None else ' [%d]' % default
                    while True:
                        t = input('%s interface number%s: ' % (label, shown)).strip()
                        if not t and default is not None:
                            return default
                        if t.isdigit() and int(t) < len(devs):
                            return int(t)
                        print('Enter a number from the list.')
                c, s = ask('Control', gc, c), ask('Screen', gs, s)
            c = gc if c is None else c
            s = gs if s is None else s
            if c is None or s is None or not (0 <= c < len(devs)) or not (0 <= s < len(devs)):
                raise RuntimeError('Give both --ctrl and --screen as numbers from --list (or use --pick).')
            ctrl_path, screen_path = devs[c]['path'], devs[s]['path']
            log('using control: %s' % describe(devs[c]))
            log('using screen:  %s' % describe(devs[s]))
        if args.test:
            frames = [test_pattern()]
            log('sending the colour test pattern (1 frame, 16 chunks)')
        else:
            if not args.gif:
                ap.error('give a GIF path, or use --test')
            images, delays = load_gif(args.gif, args.fit)
            frames = [(rgb565(im), d) for im, d in plan(images, delays, max(1, min(cap, MAX_FRAMES)))]
        do_upload(log, frames, settle, start, ctrl_path=ctrl_path, screen_path=screen_path)
    except Exception as e:
        log('error: %s' % e)
        log.share()
        sys.exit(1)


if __name__ == '__main__':
    main()
