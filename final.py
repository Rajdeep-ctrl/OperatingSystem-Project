import threading, time, uuid, random, csv, os
from collections import deque
from dataclasses import dataclass, field
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

# -----------------------------
# Event structure
# -----------------------------
@dataclass
class IPCEvent:
    ts: float
    etype: str
    ch: str
    proc: str
    info: dict = field(default_factory=dict)

    def __str__(self):
        t = time.strftime("%H:%M:%S", time.localtime(self.ts))
        return f"[{t}] {self.etype} | ch:{self.ch} | proc:{self.proc} | {self.info}"

# -----------------------------
# Channels
# -----------------------------
class ChannelBase:
    def __init__(self, cid, ctype, capacity=10):
        self.id = cid
        self.ctype = ctype
        self.capacity = max(1, int(capacity))
        self.buf = deque()
        self.last_activity = time.time()
        self.lock = threading.Lock()

    def send(self, data):
        with self.lock:
            self.last_activity = time.time()
            if len(self.buf) >= self.capacity:
                return False
            self.buf.append(data)
            return True

    def recv(self):
        with self.lock:
            self.last_activity = time.time()
            if not self.buf:
                return None
            return self.buf.popleft()

    def size(self):
        with self.lock:
            return len(self.buf)

class SharedMemory(ChannelBase):
    def __init__(self, cid):
        super().__init__(cid, "shm", capacity=1)
        self.value = None
        self.last_write_time = 0

    def write(self, val):
        now = time.time()
        conflict = (now - self.last_write_time) < 0.05
        self.value = val
        self.last_write_time = now
        self.last_activity = now
        return conflict

    def read(self):
        self.last_activity = time.time()
        return self.value

# -----------------------------
# Simulated Process
# -----------------------------
class SimProcess(threading.Thread):
    def __init__(self, name, role, ch, rate, engine):
        super().__init__(daemon=True)
        self.name = name
        self.role = role
        self.channel = ch
        self.rate = max(rate, 0.05)
        self.engine = engine
        self.stop_flag = False

    def run(self):
        while not self.stop_flag:
            try:
                if self.role in ("producer", "both"):
                    if self.channel.ctype == "shm":
                        conflict = self.channel.write(random.randint(1, 9999))
                        et = "SHM_WRITE_CONFLICT" if conflict else "SHM_WRITE"
                        self.engine.emit(IPCEvent(time.time(), et, self.channel.id, self.name, {}))
                    else:
                        ok = self.channel.send({"from": self.name})
                        et = "SEND" if ok else "OVERFLOW"
                        self.engine.emit(IPCEvent(time.time(), et, self.channel.id, self.name, {}))

                if self.role in ("consumer", "both"):
                    if self.channel.ctype == "shm":
                        val = self.channel.read()
                        self.engine.emit(IPCEvent(time.time(), "SHM_READ", self.channel.id, self.name, {"value": val}))
                    else:
                        data = self.channel.recv()
                        et = "RECV" if data else "BLOCKED"
                        self.engine.emit(IPCEvent(time.time(), et, self.channel.id, self.name, {}))

                time.sleep(1 / self.rate)
            except Exception as e:
                # Emit an error event but keep thread alive
                self.engine.emit(IPCEvent(time.time(), "PROC_ERROR", self.channel.id if self.channel else "", self.name, {"error": str(e)}))
                time.sleep(0.2)

    def stop(self):
        self.stop_flag = True

# -----------------------------
# Engine + Analyzer
# -----------------------------
class Engine:
    def __init__(self):
        self.channels = {}
        self.processes = {}
        self.handlers = []
        self.events = deque(maxlen=5000)

    def subscribe(self, h):
        self.handlers.append(h)

    def emit(self, ev):
        self.events.append(ev)
        for h in list(self.handlers):
            try:
                h(ev)
            except Exception:
                pass

    def create_channel(self, ctype, cap):
        cid = uuid.uuid4().hex[:6]
        if ctype == "shm":
            ch = SharedMemory(cid)
        else:
            ch = ChannelBase(cid, ctype, cap)
        self.channels[cid] = ch
        self.emit(IPCEvent(time.time(), "CHANNEL_CREATED", cid, "engine", {}))
        return cid

    def create_process(self, name, role, cid, rate):
        ch = self.channels.get(cid)
        if not ch:
            raise ValueError(f"Channel {cid} does not exist")
        if name in self.processes:
            raise ValueError(f"Process {name} already exists")
        p = SimProcess(name, role, ch, rate, self)
        self.processes[name] = p
        p.start()
        self.emit(IPCEvent(time.time(), "PROC_STARTED", cid, name, {}))
        return p

    def stop_all(self):
        for p in list(self.processes.values()):
            p.stop()
        self.emit(IPCEvent(time.time(), "STOP_ALL", "", "engine", {}))

class Analyzer:
    def __init__(self, engine):
        self.engine = engine
        self.engine.subscribe(self.on_event)
        self.alerts = deque(maxlen=500)
        self.queue_history = {}
        self.blocked = {}
        self._running = True
        t = threading.Thread(target=self.loop, daemon=True)
        t.start()

    def on_event(self, ev):
        # Queue history for bottleneck
        if ev.etype in ("SEND", "RECV", "OVERFLOW"):
            ch = self.engine.channels.get(ev.ch)
            if ch:
                arr = self.queue_history.setdefault(ev.ch, deque(maxlen=20))
                arr.append(ch.size())
        if ev.etype == "BLOCKED":
            self.blocked[ev.proc] = time.time()
        if ev.etype == "OVERFLOW":
            self.alert("HIGH", f"Overflow on {ev.ch}")
        if ev.etype == "SHM_WRITE_CONFLICT":
            self.alert("HIGH", f"SHM conflict on {ev.ch}")

    def loop(self):
        while self._running:
            time.sleep(0.5)
            for ch, hist in list(self.queue_history.items()):
                if len(hist) >= 4 and hist[-1] > hist[0]:
                    self.alert("MEDIUM", f"Bottleneck suspected on {ch}")
            now = time.time()
            blocked = [p for p, t in list(self.blocked.items()) if now - t > 0.5]
            if len(blocked) >= 2:
                self.alert("HIGH", f"Possible deadlock among {blocked}")

    def alert(self, sev, msg):
        self.alerts.appendleft({"time": time.time(), "sev": sev, "msg": msg})

    def pop_alerts(self):
        items = list(self.alerts)
        self.alerts.clear()
        return items

# -----------------------------
# GUI
# -----------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("IPC Debugger A++ (Static Blocks)")

        self.engine = Engine()
        self.analyzer = Analyzer(self.engine)
        self.engine.subscribe(self.on_event)

        self.ch_visual = {}
        self.proc_visual = {}
        self.build_ui()
        self.root.after(200, self.update_visuals)

    def build_ui(self):
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True)

        left = ttk.Frame(main, width=360)
        left.pack(side="left", fill="y", padx=10, pady=10)
        right = ttk.Frame(main)
        right.pack(side="right", fill="both", expand=True, padx=10, pady=10)

        # CREATE CHANNEL
        cbox = ttk.LabelFrame(left, text="Create Channel")
        cbox.pack(fill="x", pady=5)
        ttk.Label(cbox, text="Type:").grid(row=0, column=0, sticky="w")
        self.ctype = tk.StringVar(value="msg_queue")
        ttk.Combobox(cbox, textvariable=self.ctype, values=["pipe", "msg_queue", "shm"], width=10).grid(row=0, column=1)
        ttk.Label(cbox, text="Capacity:").grid(row=1, column=0, sticky="w")
        self.capval = tk.IntVar(value=10)
        ttk.Entry(cbox, textvariable=self.capval, width=6).grid(row=1, column=1)
        ttk.Button(cbox, text="Create Channel", command=self.create_channel).grid(row=2, column=0, columnspan=2, pady=5)

        # CREATE PROCESS
        pbox = ttk.LabelFrame(left, text="Create Process")
        pbox.pack(fill="x", pady=5)
        ttk.Label(pbox, text="Name:").grid(row=0, column=0, sticky="w")
        self.pname = tk.StringVar(value="P1")
        ttk.Entry(pbox, textvariable=self.pname).grid(row=0, column=1)
        ttk.Label(pbox, text="Role:").grid(row=1, column=0, sticky="w")
        self.prole = tk.StringVar(value="producer")
        ttk.Combobox(pbox, textvariable=self.prole, values=["producer", "consumer", "both"], width=10).grid(row=1, column=1)
        ttk.Label(pbox, text="Channel ID:").grid(row=2, column=0, sticky="w")
        self.pcid = tk.StringVar()
        ttk.Entry(pbox, textvariable=self.pcid).grid(row=2, column=1)
        ttk.Label(pbox, text="Rate:").grid(row=3, column=0, sticky="w")
        self.prate = tk.DoubleVar(value=1.0)
        ttk.Entry(pbox, textvariable=self.prate, width=6).grid(row=3, column=1)
        ttk.Button(pbox, text="Start Process", command=self.start_process).grid(row=4, column=0, columnspan=2, pady=5)

        # PRODUCER / CONSUMER BLOCKS
        lists = ttk.Frame(left)
        lists.pack(fill="x", pady=5)
        self.prod_box = ttk.LabelFrame(lists, text="Producers")
        self.prod_box.pack(side="left", fill="y", padx=5)
        self.cons_box = ttk.LabelFrame(lists, text="Consumers")
        self.cons_box.pack(side="right", fill="y", padx=5)

        self.prod_tree = ttk.Treeview(self.prod_box, columns=("rate", "ch"), show="headings", height=5)
        self.prod_tree.heading("rate", text="Rate")
        self.prod_tree.heading("ch", text="Channel")
        self.prod_tree.pack()
        self.cons_tree = ttk.Treeview(self.cons_box, columns=("rate", "ch"), show="headings", height=5)
        self.cons_tree.heading("rate", text="Rate")
        self.cons_tree.heading("ch", text="Channel")
        self.cons_tree.pack()

        # BUTTONS
        ttk.Button(left, text="Inject Bottleneck", command=self.inject_bottleneck).pack(fill="x", pady=4)
        ttk.Button(left, text="Export Logs", command=self.export_logs).pack(fill="x", pady=4)
        ttk.Button(left, text="Stop All", command=self.engine.stop_all).pack(fill="x", pady=4)

        # RIGHT: canvas + log + alerts
        self.canvas = tk.Canvas(right, bg="white")
        self.canvas.pack(fill="both", expand=True)
        self.log = scrolledtext.ScrolledText(right, height=8, state="disabled")
        self.log.pack(fill="x")
        self.alerts = ttk.Treeview(right, columns=("sev", "msg"), show="headings", height=5)
        self.alerts.heading("sev", text="Severity")
        self.alerts.heading("msg", text="Message")
        self.alerts.pack(fill="x", pady=5)

    # -----------------------------
    # ACTIONS
    # -----------------------------
    def create_channel(self):
        cid = self.engine.create_channel(self.ctype.get(), self.capval.get())
        x, y = 40, 40 + len(self.ch_visual) * 120
        rect = self.canvas.create_rectangle(x, y, x + 250, y + 90, fill="gray60", outline="black", width=3)
        txt = self.canvas.create_text(x + 125, y + 45, text=f"{cid}\n0/{self.capval.get()}", font=("Arial", 12))
        self.ch_visual[cid] = {"rect": rect, "txt": txt, "x": x, "y": y}
        self.pcid.set(cid)

    def start_process(self):
        name = self.pname.get().strip()
        role = self.prole.get()
        cid = self.pcid.get().strip()
        rate = self.prate.get()
        if not name:
            messagebox.showerror("Error", "Please provide a process name")
            return
        if cid not in self.engine.channels:
            messagebox.showerror("Error", "Channel ID not found. Create a channel first or paste a valid ID.")
            return
        try:
            p = self.engine.create_process(name, role, cid, rate)
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return
        color = "lightblue" if role in ("producer", "both") else "orange"
        info = self.ch_visual[cid]
        px = info["x"] + 260 + (len(self.proc_visual) % 6) * 40
        py = info["y"] + 10 + (len(self.proc_visual) // 6) * 30
        oval = self.canvas.create_rectangle(px, py, px + 28, py + 28, fill=color)
        txt = self.canvas.create_text(px + 14, py + 14, text=name, font=("Arial", 8))
        self.proc_visual[name] = {"proc": p, "oval": oval, "txt": txt, "cid": cid}
        # Update list tree
        if role in ("producer", "both"):
            self.prod_tree.insert("", "end", iid=name, values=(rate, cid))
        if role in ("consumer", "both"):
            self.cons_tree.insert("", "end", iid=name, values=(rate, cid))

    def inject_bottleneck(self):
        for p in list(self.engine.processes.values()):
            if p.role in ("producer", "both"):
                p.rate *= 6

    def export_logs(self):
        fname = f"ipc_log_{int(time.time())}.txt"
        with open(fname, "w") as f:
            for e in list(self.engine.events):
                f.write(str(e) + "\n")
        messagebox.showinfo("Exported", f"Logs saved to {fname}")

    def on_event(self, ev):
        # Append to text log
        self.log["state"] = "normal"
        try:
            self.log.insert("end", str(ev) + "\n")
        except Exception:
            self.log.insert("end", repr(ev) + "\n")
        self.log["state"] = "disabled"
        self.log.see("end")
        alerts = self.analyzer.pop_alerts()
        for a in alerts:
            self.alerts.insert("", 0, values=(a["sev"], a["msg"]))

    # -----------------------------
    # Update visuals
    # -----------------------------
    def update_visuals(self):
        for cid, obj in list(self.ch_visual.items()):
            ch = self.engine.channels.get(cid)
            if not ch:
                continue
            ln = ch.size()
            cap = ch.capacity
            rect = obj["rect"]
            txt = obj["txt"]
            # color logic
            if ln == 0:
                color = "gray50" if time.time() - ch.last_activity > 1.2 else "green"
            elif ln < cap * 0.6:
                color = "yellow"
            else:
                color = "red"
            self.canvas.itemconfig(rect, fill=color)
            self.canvas.itemconfig(txt, text=f"{cid}\n{ln}/{cap}")
        self.root.after(200, self.update_visuals)

# -----------------------------
# MAIN
# -----------------------------
def main():
    root = tk.Tk()
    app = App(root)
    root.geometry("1200x700")
    root.mainloop()

if __name__ == "__main__":
    main()
