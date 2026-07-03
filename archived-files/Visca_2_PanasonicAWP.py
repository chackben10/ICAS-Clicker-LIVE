import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
import socket
import threading
import select
import json
import os
import subprocess
import signal
import platform
import time
import urllib.request
import urllib.parse
import base64
import queue

CONFIG_FILE = "ptz_bridge_config.json"

# ==========================================
# CUSTOM DIALOG: Port Conflict Resolution
# ==========================================
class PortConflictDialog(tk.Toplevel):
    def __init__(self, parent, port):
        super().__init__(parent)
        self.title("Port Conflict Detected")
        self.geometry("450x220")
        self.resizable(False, False)
        self.result = "cancel" 
        
        self.transient(parent)
        self.grab_set()

        ttk.Label(self, text=f"Port {port} is locked by another application.", font=("Arial", 11, "bold")).pack(pady=(15, 5))
        ttk.Label(self, text="How would you like to proceed?").pack(pady=(0, 15))

        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill=tk.X, padx=20)

        ttk.Button(btn_frame, text="Cancel (I will enter a different port)", command=self.do_cancel).pack(fill=tk.X, pady=4)
        ttk.Button(btn_frame, text="Politely Share (Run alongside OBS)", command=self.do_share).pack(fill=tk.X, pady=4)
        
        btn_kill = tk.Button(btn_frame, text="Destructively Force (Kill blocking app)", fg="red", command=self.do_kill)
        btn_kill.pack(fill=tk.X, pady=4)

        self.wait_window(self)

    def do_cancel(self): self.result = "cancel"; self.destroy()
    def do_share(self): self.result = "share"; self.destroy()
    def do_kill(self): self.result = "kill"; self.destroy()

# ==========================================
# MAIN APPLICATION
# ==========================================
class PTZBridgeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("VISCA to Panasonic Bridge - Broadcast Edition")
        self.root.geometry("1000x800")
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        self.style = ttk.Style()
        self.style.configure("Active.TButton", foreground="black")
        self.style.configure("Inactive.TButton", foreground="#999999")
        
        self.is_listening = False
        self.udp_thread = None
        self.log_queue = queue.Queue()
        self.logs_data = [] 
        self.last_ptz_cmd = ""
        self.terminal_visible = True
        self.af_state = True

        self.config = {
            "panasonic_ip": "192.168.50.80",
            "panasonic_user": "admin",
            "panasonic_pass": "12345",
            "visca_ip": "0.0.0.0",
            "visca_port": "52383",
            "preset_names": {} 
        }
        self.load_config()
        self.build_ui()
        
        self.root.after(100, self.process_log_queue)
        self.root.after(5000, self.prune_terminal_history)

    # --- UI CONSTRUCTION ---
    def build_ui(self):
        self.main_frame = ttk.Frame(self.root, padding="15")
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # 1: Network Configuration
        top_frame = ttk.Frame(self.main_frame)
        top_frame.pack(fill=tk.X, pady=(0, 10))

        network_lf = ttk.LabelFrame(top_frame, text="Network Configuration", padding="10")
        network_lf.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(network_lf, text="Panasonic IP:").grid(row=0, column=0, sticky=tk.W)
        self.entry_cam_ip = ttk.Entry(network_lf, width=14)
        self.entry_cam_ip.insert(0, self.config["panasonic_ip"])
        self.entry_cam_ip.grid(row=0, column=1, padx=5, pady=2)

        ttk.Label(network_lf, text="User:").grid(row=0, column=2, sticky=tk.W, padx=(5,0))
        self.entry_cam_user = ttk.Entry(network_lf, width=8)
        self.entry_cam_user.insert(0, self.config["panasonic_user"])
        self.entry_cam_user.grid(row=0, column=3, padx=5, pady=2)

        self.entry_cam_pass = ttk.Entry(network_lf, width=10, show="*")
        self.entry_cam_pass.insert(0, self.config["panasonic_pass"])
        self.entry_cam_pass.grid(row=0, column=4, padx=2, pady=2)

        self.btn_show_pass = ttk.Button(network_lf, text="O", width=2, command=self.toggle_pass_visibility)
        self.btn_show_pass.grid(row=0, column=5, padx=(0, 15))

        ttk.Label(network_lf, text="Listen IP:").grid(row=1, column=0, sticky=tk.W)
        self.entry_visca_ip = ttk.Entry(network_lf, width=14)
        self.entry_visca_ip.insert(0, self.config["visca_ip"])
        self.entry_visca_ip.grid(row=1, column=1, padx=5, pady=2)

        ttk.Label(network_lf, text="Port:").grid(row=1, column=2, sticky=tk.W, padx=(5,0))
        self.entry_visca_port = ttk.Entry(network_lf, width=8)
        self.entry_visca_port.insert(0, self.config["visca_port"])
        self.entry_visca_port.grid(row=1, column=3, padx=5, pady=2)

        self.btn_toggle_bridge = ttk.Button(network_lf, text="▶ START VISCA BRIDGE", command=self.toggle_bridge_request)
        self.btn_toggle_bridge.grid(row=0, column=6, rowspan=2, padx=15, sticky=tk.NSEW)

        # 2: Middle Layout (System, PTZ, Presets)
        mid_frame = ttk.Frame(self.main_frame)
        mid_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        # SYSTEM COLUMN
        osd_lf = ttk.LabelFrame(mid_frame, text="System (aw_cam)", padding="10")
        osd_lf.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))

        ttk.Button(osd_lf, text="Menu ON", command=lambda: self.send_panasonic_cgi("DUS:1", "GUI", "aw_cam")).pack(fill=tk.X, pady=2)
        ttk.Button(osd_lf, text="Menu OFF", command=lambda: self.send_panasonic_cgi("DUS:0", "GUI", "aw_cam")).pack(fill=tk.X, pady=2)
        ttk.Button(osd_lf, text="CAMERA FEED", command=lambda: self.send_panasonic_cgi("DCB:0", "GUI", "aw_cam")).pack(fill=tk.X, pady=2)
        ttk.Button(osd_lf, text="Color Bars", command=lambda: self.send_panasonic_cgi("DCB:1", "GUI", "aw_cam")).pack(fill=tk.X, pady=2)
        
        osd_nav = ttk.Frame(osd_lf)
        osd_nav.pack(pady=10)
        ttk.Button(osd_nav, text="▲", width=3, command=lambda: self.send_panasonic_cgi("DUP:1", "GUI", "aw_cam")).grid(row=0, column=1)
        ttk.Button(osd_nav, text="◀", width=3, command=lambda: self.send_panasonic_cgi("DLT:1", "GUI", "aw_cam")).grid(row=1, column=0)
        ttk.Button(osd_nav, text="OK", width=3, command=lambda: self.send_panasonic_cgi("DIT:1", "GUI", "aw_cam")).grid(row=1, column=1)
        ttk.Button(osd_nav, text="▶", width=3, command=lambda: self.send_panasonic_cgi("DRT:1", "GUI", "aw_cam")).grid(row=1, column=2)
        ttk.Button(osd_nav, text="▼", width=3, command=lambda: self.send_panasonic_cgi("DDW:1", "GUI", "aw_cam")).grid(row=2, column=1)
        
        ttk.Separator(osd_lf, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        ttk.Button(osd_lf, text="POWER ON", command=lambda: self.send_panasonic_cgi("#On", "GUI", "aw_ptz")).pack(fill=tk.X, pady=2)
        ttk.Button(osd_lf, text="Standby", command=lambda: self.send_panasonic_cgi("#Of", "GUI", "aw_ptz")).pack(fill=tk.X, pady=2)

        # PTZ COLUMN
        ptz_lf = ttk.LabelFrame(mid_frame, text="Live PTZ & Lens", padding="15")
        ptz_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        pt_frame = ttk.Frame(ptz_lf)
        pt_frame.pack(fill=tk.X, pady=(0, 15))
        
        pad_frame = ttk.Frame(pt_frame)
        pad_frame.pack(side=tk.LEFT, padx=(0, 20))
        self.btn_up = ttk.Button(pad_frame, text="▲", width=4)
        self.btn_left = ttk.Button(pad_frame, text="◀", width=4)
        self.btn_right = ttk.Button(pad_frame, text="▶", width=4)
        self.btn_down = ttk.Button(pad_frame, text="▼", width=4)
        self.btn_up.grid(row=0, column=1, pady=2)
        self.btn_left.grid(row=1, column=0, padx=2)
        self.btn_right.grid(row=1, column=2, padx=2)
        self.btn_down.grid(row=2, column=1, pady=2)

        pt_spd_frame = ttk.Frame(pt_frame)
        pt_spd_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(pt_spd_frame, text="PAN/TILT Speed:").pack(anchor=tk.W)
        self.var_pt_spd = tk.IntVar(value=25)
        self.lbl_pt_val = ttk.Label(pt_spd_frame, text="25", font=("Arial", 10, "bold"))
        self.lbl_pt_val.pack(side=tk.RIGHT)
        ttk.Scale(pt_spd_frame, from_=1, to=49, variable=self.var_pt_spd, orient=tk.HORIZONTAL, command=lambda v: self.lbl_pt_val.config(text=f"{int(float(v))}")).pack(fill=tk.X, padx=(0, 10))

        ttk.Separator(ptz_lf, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=5)

        z_frame = ttk.Frame(ptz_lf)
        z_frame.pack(fill=tk.X, pady=10)
        z_btn_frame = ttk.Frame(z_frame)
        z_btn_frame.pack(side=tk.LEFT, padx=(0, 20))
        self.btn_zout = ttk.Button(z_btn_frame, text="➖ OUT", width=8)
        self.btn_zin = ttk.Button(z_btn_frame, text="➕ IN", width=8)
        self.btn_zout.grid(row=0, column=0, padx=2)
        self.btn_zin.grid(row=0, column=1, padx=2)

        z_spd_frame = ttk.Frame(z_frame)
        z_spd_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(z_spd_frame, text="ZOOM Speed:").pack(anchor=tk.W)
        self.var_z_spd = tk.IntVar(value=20)
        self.lbl_z_val = ttk.Label(z_spd_frame, text="20", font=("Arial", 10, "bold"))
        self.lbl_z_val.pack(side=tk.RIGHT)
        ttk.Scale(z_spd_frame, from_=1, to=49, variable=self.var_z_spd, orient=tk.HORIZONTAL, command=lambda v: self.lbl_z_val.config(text=f"{int(float(v))}")).pack(fill=tk.X, padx=(0, 10))

        ttk.Separator(ptz_lf, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=5)

        f_frame = ttk.Frame(ptz_lf)
        f_frame.pack(fill=tk.X, pady=10)
        f_btn_frame = ttk.Frame(f_frame)
        f_btn_frame.pack(side=tk.LEFT, padx=(0, 20))
        self.btn_f_auto = ttk.Button(f_btn_frame, text="AUTO", width=6, command=self.set_focus_auto, style="Active.TButton")
        self.btn_f_near = ttk.Button(f_btn_frame, text="NEAR", width=6, style="Inactive.TButton")
        self.btn_f_far = ttk.Button(f_btn_frame, text="FAR", width=6, style="Inactive.TButton")
        self.btn_f_auto.grid(row=0, column=0, padx=2)
        self.btn_f_near.grid(row=0, column=1, padx=2)
        self.btn_f_far.grid(row=0, column=2, padx=2)

        f_spd_frame = ttk.Frame(f_frame)
        f_spd_frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(f_spd_frame, text="FOCUS Speed:").pack(anchor=tk.W)
        self.var_f_spd = tk.IntVar(value=20)
        self.lbl_f_val = ttk.Label(f_spd_frame, text="20", font=("Arial", 10, "bold"))
        self.lbl_f_val.pack(side=tk.RIGHT)
        ttk.Scale(f_spd_frame, from_=1, to=49, variable=self.var_f_spd, orient=tk.HORIZONTAL, command=lambda v: self.lbl_f_val.config(text=f"{int(float(v))}")).pack(fill=tk.X, padx=(0, 10))

        self.btn_up.bind("<ButtonPress-1>", lambda e: self.gui_move("up")); self.btn_up.bind("<ButtonRelease-1>", lambda e: self.gui_stop("pt"))
        self.btn_down.bind("<ButtonPress-1>", lambda e: self.gui_move("down")); self.btn_down.bind("<ButtonRelease-1>", lambda e: self.gui_stop("pt"))
        self.btn_left.bind("<ButtonPress-1>", lambda e: self.gui_move("left")); self.btn_left.bind("<ButtonRelease-1>", lambda e: self.gui_stop("pt"))
        self.btn_right.bind("<ButtonPress-1>", lambda e: self.gui_move("right")); self.btn_right.bind("<ButtonRelease-1>", lambda e: self.gui_stop("pt"))
        self.btn_zin.bind("<ButtonPress-1>", lambda e: self.gui_move("zin")); self.btn_zin.bind("<ButtonRelease-1>", lambda e: self.gui_stop("z"))
        self.btn_zout.bind("<ButtonPress-1>", lambda e: self.gui_move("zout")); self.btn_zout.bind("<ButtonRelease-1>", lambda e: self.gui_stop("z"))
        self.btn_f_near.bind("<ButtonPress-1>", lambda e: self.gui_move("fnear")); self.btn_f_near.bind("<ButtonRelease-1>", lambda e: self.gui_stop("f"))
        self.btn_f_far.bind("<ButtonPress-1>", lambda e: self.gui_move("ffar")); self.btn_f_far.bind("<ButtonRelease-1>", lambda e: self.gui_stop("f"))

        # PRESETS COLUMN
        preset_lf = ttk.LabelFrame(mid_frame, text="Position Presets", padding="10")
        preset_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))
        
        ttk.Label(preset_lf, text="Dbl-Click: Recall | Right-Click: Options", font=("Arial", 9, "italic")).pack(anchor=tk.W, pady=(0,5))

        list_frame = ttk.Frame(preset_lf)
        list_frame.pack(fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, font=("Arial", 14), selectbackground="#0078D7", activestyle='none')
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.listbox.yview)

        self.populate_presets()

        self.listbox.bind("<Double-Button-1>", self.on_preset_double_click)
        self.listbox.bind("<Button-2>", self.show_preset_context_menu)
        self.listbox.bind("<Button-3>", self.show_preset_context_menu)
        
        self.preset_menu = tk.Menu(self.root, tearoff=0)
        self.preset_menu.add_command(label="▶ Recall Preset", command=self.recall_selected_preset)
        self.preset_menu.add_command(label="💾 Save Position Here", command=self.save_selected_preset)
        self.preset_menu.add_separator()
        self.preset_menu.add_command(label="✎ Rename", command=self.rename_selected_preset)

        # 3: Diagnostic Terminal (Collapsible)
        term_wrapper = ttk.Frame(self.main_frame)
        term_wrapper.pack(fill=tk.X, side=tk.BOTTOM)
        
        self.btn_toggle_term = ttk.Button(term_wrapper, text="▼ Diagnostic Terminal", command=self.toggle_terminal)
        self.btn_toggle_term.pack(fill=tk.X)

        self.term_container = ttk.Frame(term_wrapper)
        self.term_container.pack(fill=tk.BOTH, expand=True, pady=(5,0))
        
        self.txt_term = tk.Text(self.term_container, height=8, bg="#1a1a1a", fg="#00ff00", font=("Courier", 10), state=tk.DISABLED)
        self.txt_term.pack(fill=tk.BOTH, expand=True)

    # --- UI LOGIC ---
    def toggle_pass_visibility(self):
        if self.entry_cam_pass.cget("show") == "*":
            self.entry_cam_pass.config(show=""); self.btn_show_pass.config(text="X")
        else:
            self.entry_cam_pass.config(show="*"); self.btn_show_pass.config(text="O")

    def toggle_terminal(self):
        if self.terminal_visible:
            self.term_container.pack_forget()
            self.btn_toggle_term.config(text="▶ Diagnostic Terminal")
            self.terminal_visible = False
        else:
            self.term_container.pack(fill=tk.BOTH, expand=True, pady=(5,0))
            self.btn_toggle_term.config(text="▼ Diagnostic Terminal")
            self.terminal_visible = True

    def set_focus_auto(self):
        self.send_panasonic_cgi("#D11", "GUI")
        self.af_state = True
        self.btn_f_auto.config(style="Active.TButton")
        self.btn_f_near.config(style="Inactive.TButton")
        self.btn_f_far.config(style="Inactive.TButton")

    def set_focus_manual(self):
        if self.af_state:
            self.send_panasonic_cgi("#D10", "GUI")
            self.af_state = False
            self.btn_f_auto.config(style="Inactive.TButton")
            self.btn_f_near.config(style="Active.TButton")
            self.btn_f_far.config(style="Active.TButton")

    def gui_move(self, dir):
        p_spd, z_spd, f_spd = self.var_pt_spd.get(), self.var_z_spd.get(), self.var_f_spd.get()
        
        if dir == "up": cmd = f"#PTS50{50 + p_spd:02d}"
        elif dir == "down": cmd = f"#PTS50{50 - p_spd:02d}"
        elif dir == "left": cmd = f"#PTS{50 - p_spd:02d}50"
        elif dir == "right": cmd = f"#PTS{50 + p_spd:02d}50"
        elif dir == "zin": cmd = f"#Z{50 + z_spd:02d}"
        elif dir == "zout": cmd = f"#Z{50 - z_spd:02d}"
        elif dir in ["ffar", "fnear"]:
            self.set_focus_manual()
            cmd = f"#F{50 + f_spd:02d}" if dir == "ffar" else f"#F{50 - f_spd:02d}"
        self.send_panasonic_cgi(cmd, "GUI")

    def gui_stop(self, type):
        if type == "pt": cmd = "#PTS5050"
        elif type == "z": cmd = "#Z50"
        elif type == "f": cmd = "#F50"
        self.send_panasonic_cgi(cmd, "GUI")

    # --- PRESET LOGIC ---
    def populate_presets(self):
        self.listbox.delete(0, tk.END)
        zebra_color = "#f4f6f8" 
        
        self.listbox.insert(tk.END, "   Preset 00 - HOME")
        self.listbox.itemconfigure(0, background=zebra_color)

        for i in range(1, 101):
            name = self.config["preset_names"].get(str(i), "")
            display_text = f"   Preset {i:02d}" + (f" - {name}" if name else "")
            self.listbox.insert(tk.END, display_text)
            if i % 2 == 0:
                self.listbox.itemconfigure(i, background=zebra_color)

    def on_preset_double_click(self, event): self.recall_selected_preset()

    def show_preset_context_menu(self, event):
        try:
            index = self.listbox.nearest(event.y)
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(index)
            self.listbox.activate(index)
            self.preset_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.preset_menu.grab_release()

    def get_selected_preset_num(self):
        selection = self.listbox.curselection()
        if not selection: return None
        return selection[0] 

    def recall_selected_preset(self):
        num = self.get_selected_preset_num()
        if num is not None: self.send_panasonic_cgi(f"#R{num:02d}", "GUI PRESET")

    def save_selected_preset(self):
        num = self.get_selected_preset_num()
        if num == 0:
            self.log_message("[GUI] Cannot overwrite Home Preset 00.")
            return 
        if num is not None:
            self.send_panasonic_cgi(f"#M{num:02d}", "GUI PRESET")

    def rename_selected_preset(self):
        num = self.get_selected_preset_num()
        if num == 0: return 
        if num is not None:
            current_name = self.config["preset_names"].get(str(num), "")
            new_name = simpledialog.askstring("Rename Preset", f"Name for Preset {num:02d}:", initialvalue=current_name)
            if new_name is not None:
                self.config["preset_names"][str(num)] = new_name
                self.save_config()
                self.populate_presets()

    # --- HTTP ENGINE ---
    def send_panasonic_cgi(self, cmd, source="SYS", endpoint="aw_ptz"):
        ip = self.entry_cam_ip.get()
        user = self.entry_cam_user.get()
        pwd = self.entry_cam_pass.get()
        params = urllib.parse.urlencode({'cmd': cmd, 'res': '1'})
        url = f"http://{ip}/cgi-bin/{endpoint}?{params}"

        def _worker():
            try:
                req = urllib.request.Request(url)
                auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
                req.add_header("Authorization", f"Basic {auth}")
                urllib.request.urlopen(req, timeout=1.0)
            except Exception as e:
                self.log_message(f"[HTTP ERR] {str(e)}")
        threading.Thread(target=_worker, daemon=True).start()

    # --- PORT ASSASSIN LOGIC ---
    def force_free_port(self, port):
        if platform.system() != "Darwin": return False 
        self.log_message(f"[SYSTEM] Hunting for process holding UDP port {port}...")
        try:
            cmd = f"lsof -t -i UDP:{port}"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            pids = result.stdout.strip().split('\n')
            
            killed_something = False
            for pid_str in pids:
                if pid_str.isdigit():
                    pid = int(pid_str)
                    os.kill(pid, signal.SIGKILL)
                    killed_something = True
            
            if killed_something:
                time.sleep(0.5) 
                return True
            return False
        except Exception as e:
            return False

    # --- VISCA SERVER & PARSER ---
    def toggle_bridge_request(self):
        if self.is_listening:
            self.is_listening = False
            self.btn_toggle_bridge.config(text="▶ START VISCA BRIDGE")
            self.log_message("[BRIDGE] Stopped listening.")
        else:
            self.save_config()
            self.start_udp_listener()

    def start_udp_listener(self, force_share=False):
        ip = self.entry_visca_ip.get()
        port = int(self.entry_visca_port.get())
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        if force_share:
            try: sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except AttributeError: pass 

        try:
            sock.bind((ip, port))
        except OSError as e:
            if e.errno in (48, 98) and not force_share:
                dialog = PortConflictDialog(self.root, port)
                ans = dialog.result

                if ans == "share":
                    sock.close()
                    self.start_udp_listener(force_share=True)
                    return
                elif ans == "kill":
                    if self.force_free_port(port):
                        sock.close()
                        self.start_udp_listener(force_share=False)
                        return
                sock.close()
                self.is_listening = False
                self.btn_toggle_bridge.config(text="▶ START VISCA BRIDGE")
                return
            else:
                sock.close()
                return

        sock.setblocking(False)
        self.is_listening = True
        self.btn_toggle_bridge.config(text="⏹ STOP VISCA BRIDGE")
        self.log_message(f"[BRIDGE] Active on {ip}:{port} (Force Share: {force_share})")
        
        self.udp_thread = threading.Thread(target=self.udp_loop, args=(sock,), daemon=True)
        self.udp_thread.start()

    def udp_loop(self, sock):
        try:
            while self.is_listening:
                ready = select.select([sock], [], [], 0.1)
                if ready[0]:
                    data, addr = sock.recvfrom(1024)
                    self.parse_visca(data, sock, addr)
        finally:
            try: sock.close()
            except: pass

    def parse_visca(self, data, sock, addr):
        raw_hex = data.hex().upper()
        
        # 1. SEND RETURN PACKETS (Fixes Tenveo Latency/Timeout)
        if raw_hex.startswith("0100"):
            seq_num = data[4:8] 
            ack_pkt = bytes.fromhex("01110003") + seq_num + bytes.fromhex("9041FF")
            comp_pkt = bytes.fromhex("01110003") + seq_num + bytes.fromhex("9051FF")
            try:
                sock.sendto(ack_pkt, addr)
                sock.sendto(comp_pkt, addr)
            except: pass
        elif raw_hex.startswith("8101") or raw_hex.startswith("8109"):
            try:
                sock.sendto(bytes.fromhex("9041FF"), addr)
                sock.sendto(bytes.fromhex("9051FF"), addr)
            except: pass

        # 2. EXECUTE COMMANDS (Handle Coalesced Packets)
        # Split the payload by the 'FF' terminator to ensure simultaneous Pan/Zoom commands aren't dropped
        chunks = raw_hex.split("FF")
        
        for chunk in chunks:
            start_idx = chunk.find("81")
            if start_idx == -1: continue
            
            # Reattach the terminator for processing
            visca = chunk[start_idx:] + "FF"
            
            if not visca.startswith("81010601"):
                self.log_message(f"[VISCA IN] {visca}")

            # TENVEO SPECIFIC LOG MAP
            if visca.startswith("8101060602FF"): 
                self.send_panasonic_cgi("DUS:1", "TENVEO", "aw_cam")
            elif visca.startswith("81010604FF"): 
                self.send_panasonic_cgi("#R00", "TENVEO")
            elif visca.startswith("8101043802FF"): 
                self.root.after(0, self.set_focus_auto)
            elif visca.startswith("8101043803FF"): 
                self.root.after(0, self.set_focus_manual)
            elif visca.startswith("81010435"): 
                self.send_panasonic_cgi("#AWA", "TENVEO")

            # STANDARD COMMANDS
            # Pan/Tilt
            elif visca.startswith("81010601") and len(visca) >= 18:
                v, w = int(visca[8:10], 16), int(visca[10:12], 16)
                x, y = visca[12:14], visca[14:16]
                p_val, t_val = 50, 50
                p_off = int((v / 24.0) * 49) if v > 0 else 0
                t_off = int((w / 20.0) * 49) if w > 0 else 0

                if x == "01": p_val = 50 - p_off
                elif x == "02": p_val = 50 + p_off
                if y == "01": t_val = 50 + t_off
                elif y == "02": t_val = 50 - t_off
                
                cmd = f"#PTS{p_val:02d}{t_val:02d}"
                if cmd != self.last_ptz_cmd:
                    self.last_ptz_cmd = cmd
                    self.send_panasonic_cgi(cmd, "BRIDGE")

            # Zoom
            elif visca.startswith("81010407") and len(visca) >= 12:
                cmd_byte = visca[8:10]
                if cmd_byte == "00": 
                    self.send_panasonic_cgi("#Z50", "BRIDGE")
                # Catch Standard Joystick Tele (02)
                elif cmd_byte == "02":
                    self.send_panasonic_cgi(f"#Z{50 + self.var_z_spd.get():02d}", "BRIDGE")
                # Catch Standard Joystick Wide (03)
                elif cmd_byte == "03":
                    self.send_panasonic_cgi(f"#Z{50 - self.var_z_spd.get():02d}", "BRIDGE")
                # Variable Tele (2x)
                elif cmd_byte.startswith("2"): 
                    speed = int(cmd_byte[1], 16) 
                    p_speed = 50 + int((speed / 7.0) * 49) if speed > 0 else 75
                    self.send_panasonic_cgi(f"#Z{p_speed:02d}", "BRIDGE")
                # Variable Wide (3x)
                elif cmd_byte.startswith("3"): 
                    speed = int(cmd_byte[1], 16) 
                    p_speed = 50 - int((speed / 7.0) * 49) if speed > 0 else 25
                    self.send_panasonic_cgi(f"#Z{p_speed:02d}", "BRIDGE")

            # Focus Movement
            elif visca.startswith("81010408") and len(visca) >= 12:
                cmd_byte = visca[8:10]
                if cmd_byte == "00": 
                    self.send_panasonic_cgi("#F50", "BRIDGE")
                elif cmd_byte == "02": # Far
                    self.send_panasonic_cgi(f"#F{50 + self.var_f_spd.get():02d}", "BRIDGE")
                elif cmd_byte == "03": # Near
                    self.send_panasonic_cgi(f"#F{50 - self.var_f_spd.get():02d}", "BRIDGE")

            # Preset Recall/Save
            elif visca.startswith("8101043F02") and len(visca) >= 14:
                p_num = int(visca[10:12], 16)
                self.send_panasonic_cgi(f"#R{p_num:02d}", "BRIDGE")
            elif visca.startswith("8101043F01") and len(visca) >= 14:
                p_num = int(visca[10:12], 16)
                self.send_panasonic_cgi(f"#M{p_num:02d}", "BRIDGE")
        raw_hex = data.hex().upper()
        
        # 1. SEND RETURN PACKETS (Fixes Tenveo Latency/Timeout)
        # If VISCA-over-IP header detected (Payload Type 01 00 Command)
        if raw_hex.startswith("0100"):
            seq_num = data[4:8] # Extract exact sequence number to reply
            ack_pkt = bytes.fromhex("01110003") + seq_num + bytes.fromhex("9041FF")
            comp_pkt = bytes.fromhex("01110003") + seq_num + bytes.fromhex("9051FF")
            try:
                sock.sendto(ack_pkt, addr)
                sock.sendto(comp_pkt, addr)
            except: pass
        # If Raw UDP VISCA detected (No Header)
        elif raw_hex.startswith("8101") or raw_hex.startswith("8109"):
            try:
                sock.sendto(bytes.fromhex("9041FF"), addr)
                sock.sendto(bytes.fromhex("9051FF"), addr)
            except: pass

        # 2. EXECUTE COMMANDS
        start_idx = raw_hex.find("81") # Find VISCA start byte
        if start_idx == -1: return
        visca = raw_hex[start_idx:]
        
        # Don't log constant P/T movement spam to keep terminal clean
        if not visca.startswith("81010601"):
            self.log_message(f"[VISCA IN] {visca}")

        # TENVEO SPECIFIC LOG MAP
        if visca.startswith("8101060602FF"): # Tenveo Menu
            self.send_panasonic_cgi("DUS:1", "TENVEO", "aw_cam")
        elif visca.startswith("81010604FF"): # Tenveo Home
            self.send_panasonic_cgi("#R00", "TENVEO")
        elif visca.startswith("8101043802FF"): # Tenveo Auto Focus
            self.root.after(0, self.set_focus_auto)
        elif visca.startswith("8101043803FF"): # Tenveo Focus Manual Switch
            self.root.after(0, self.set_focus_manual)
        elif visca.startswith("81010435"): # Tenveo AWB Trigger
            self.send_panasonic_cgi("#AWA", "TENVEO")

        # STANDARD COMMANDS
        # Pan/Tilt
        elif visca.startswith("81010601") and len(visca) >= 18:
            v, w = int(visca[8:10], 16), int(visca[10:12], 16)
            x, y = visca[12:14], visca[14:16]
            p_val, t_val = 50, 50
            p_off = int((v / 24.0) * 49) if v > 0 else 0
            t_off = int((w / 20.0) * 49) if w > 0 else 0

            if x == "01": p_val = 50 - p_off
            elif x == "02": p_val = 50 + p_off
            if y == "01": t_val = 50 + t_off
            elif y == "02": t_val = 50 - t_off
            
            cmd = f"#PTS{p_val:02d}{t_val:02d}"
            if cmd != self.last_ptz_cmd:
                self.last_ptz_cmd = cmd
                self.send_panasonic_cgi(cmd, "BRIDGE")

        # Zoom
        elif visca.startswith("81010407") and len(visca) >= 12:
            cmd_byte = visca[8:10]
            if cmd_byte == "00": 
                self.send_panasonic_cgi("#Z50", "BRIDGE")
            elif cmd_byte.startswith("2"): 
                speed = int(cmd_byte[1], 16) 
                p_speed = 50 + int((speed / 7.0) * 49) if speed > 0 else 75
                self.send_panasonic_cgi(f"#Z{p_speed:02d}", "BRIDGE")
            elif cmd_byte.startswith("3"): 
                speed = int(cmd_byte[1], 16) 
                p_speed = 50 - int((speed / 7.0) * 49) if speed > 0 else 25
                self.send_panasonic_cgi(f"#Z{p_speed:02d}", "BRIDGE")

        # Focus Movement
        elif visca.startswith("81010408") and len(visca) >= 12:
            cmd_byte = visca[8:10]
            if cmd_byte == "00": 
                self.send_panasonic_cgi("#F50", "BRIDGE")
            elif cmd_byte == "02": # Far
                self.send_panasonic_cgi(f"#F{50 + self.var_f_spd.get():02d}", "BRIDGE")
            elif cmd_byte == "03": # Near
                self.send_panasonic_cgi(f"#F{50 - self.var_f_spd.get():02d}", "BRIDGE")

        # Preset Recall/Save
        elif visca.startswith("8101043F02") and len(visca) >= 14:
            p_num = int(visca[10:12], 16)
            self.send_panasonic_cgi(f"#R{p_num:02d}", "BRIDGE")
        elif visca.startswith("8101043F01") and len(visca) >= 14:
            p_num = int(visca[10:12], 16)
            self.send_panasonic_cgi(f"#M{p_num:02d}", "BRIDGE")

    # --- HOUSEKEEPING ---
    def load_config(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f: self.config.update(json.load(f))
            except: pass

    def save_config(self):
        self.config.update({
            "panasonic_ip": self.entry_cam_ip.get(),
            "panasonic_user": self.entry_cam_user.get(),
            "panasonic_pass": self.entry_cam_pass.get(),
            "visca_ip": self.entry_visca_ip.get(),
            "visca_port": self.entry_visca_port.get()
        })
        with open(CONFIG_FILE, 'w') as f: json.dump(self.config, f)

    def log_message(self, msg):
        t = time.time()
        self.logs_data.append((t, f"[{time.strftime('%H:%M:%S', time.localtime(t))}] {msg}\n"))
        self.log_queue.put(self.logs_data[-1][1])

    def process_log_queue(self):
        while not self.log_queue.empty():
            m = self.log_queue.get()
            self.txt_term.config(state=tk.NORMAL)
            self.txt_term.insert(tk.END, m); self.txt_term.see(tk.END)
            self.txt_term.config(state=tk.DISABLED)
        self.root.after(100, self.process_log_queue)

    def prune_terminal_history(self):
        now = time.time()
        self.logs_data = [l for l in self.logs_data if (now - l[0]) < 300]
        self.root.after(5000, self.prune_terminal_history)

    def on_closing(self):
        self.is_listening = False 
        self.save_config()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = PTZBridgeApp(root)
    root.mainloop()
