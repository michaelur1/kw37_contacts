#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Michael Urban
"""Window front end for kw37_contacts (tkinter). All device logic lives in kw37_contacts.py."""
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import kw37_contacts as core

BG = '#ffffff'
INK = '#1f2328'
MUTED = '#6b7280'
LINE = '#e5e7eb'
ACCENT = '#2563eb'
OK = '#15803d'
WARN = '#b45309'
BAD = '#b91c1c'
FONT = 'Segoe UI'


class QueueWriter:
    """Stands in for stdout: forwards core's print() output to the GUI log."""
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
            self.q.put(('log', s))

    def flush(self):
        pass


class App:
    def __init__(self, root, demo=None):
        self.root = root
        self.demo = demo
        self.q = queue.Queue()
        self.dev = None
        self.img = None
        self.chunks = None
        self.labels = {}
        self.busy = False
        self.stop = threading.Event()
        sys.stdout = sys.stderr = QueueWriter(self.q)

        root.title('Kontakty KW37')
        root.configure(bg=BG)
        root.minsize(520, 0)
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        st = ttk.Style()
        try:
            st.theme_use('vista')
        except tk.TclError:
            pass
        st.configure('TFrame', background=BG)
        st.configure('TLabel', background=BG, foreground=INK, font=(FONT, 10))
        st.configure('Muted.TLabel', foreground=MUTED, font=(FONT, 9))
        st.configure('Title.TLabel', font=(FONT, 16, 'bold'))
        st.configure('Head.TLabel', foreground=MUTED, font=(FONT, 9, 'bold'))
        st.configure('TButton', font=(FONT, 10), padding=(14, 6))
        st.configure('TEntry', padding=4)
        st.configure('TCheckbutton', background=BG, foreground=MUTED, font=(FONT, 9))

        wrap = ttk.Frame(root, padding=(24, 20, 24, 16))
        wrap.pack(fill='both', expand=True)

        ttk.Label(wrap, text='Kontakty v hodinkách', style='Title.TLabel').pack(anchor='w')
        ttk.Label(wrap, text='Canyon KW37 „Alfie“ – vlastní názvy deseti kontaktních pozic',
                  style='Muted.TLabel').pack(anchor='w', pady=(2, 14))

        # status line
        srow = ttk.Frame(wrap)
        srow.pack(fill='x')
        self.dot = tk.Canvas(srow, width=12, height=12, bg=BG, highlightthickness=0)
        self.dot.pack(side='left', padx=(0, 8))
        self.dot_id = self.dot.create_oval(1, 1, 11, 11, fill=MUTED, outline='')
        self.status = ttk.Label(srow, text='', wraplength=440, justify='left')
        self.status.pack(side='left', fill='x', expand=True)
        barbox = tk.Frame(wrap, height=6, bg=BG)          # keeps the layout steady when the bar hides
        barbox.pack(fill='x', pady=(10, 0))
        barbox.pack_propagate(False)
        self.bar = ttk.Progressbar(barbox, mode='indeterminate')

        tk.Frame(wrap, height=1, bg=LINE).pack(fill='x', pady=14)

        # table
        grid = ttk.Frame(wrap)
        grid.pack(fill='x')
        grid.columnconfigure(2, weight=1)
        for col, text in enumerate(('#', 'Tovární název', 'Název v hodinkách', '')):
            ttk.Label(grid, text=text, style='Head.TLabel').grid(row=0, column=col, sticky='w',
                                                                 padx=(0, 16), pady=(0, 6))
        self.vars, self.entries, self.counters = {}, {}, {}
        for n, (factory, _, _, mx) in core.SLOTS.items():
            ttk.Label(grid, text=str(n), style='Muted.TLabel').grid(row=n, column=0, sticky='w', padx=(0, 16))
            ttk.Label(grid, text=factory).grid(row=n, column=1, sticky='w', padx=(0, 16))
            var = tk.StringVar()
            vcmd = (root.register(lambda new, mx=mx: len(new) <= mx), '%P')
            e = ttk.Entry(grid, textvariable=var, font=(FONT, 10), validate='key', validatecommand=vcmd)
            e.grid(row=n, column=2, sticky='ew', pady=3)
            cnt = ttk.Label(grid, text=f'0/{mx}', style='Muted.TLabel', width=5, anchor='e')
            cnt.grid(row=n, column=3, sticky='e', padx=(10, 0))
            var.trace_add('write', lambda *_, n=n: self.on_edit(n))
            self.vars[n], self.entries[n], self.counters[n] = var, e, cnt

        # buttons
        brow = ttk.Frame(wrap)
        brow.pack(fill='x', pady=(18, 0))
        self.main_btn = ttk.Button(brow, text='Připojit hodinky', command=self.on_main)
        self.main_btn.pack(side='right')
        self.factory_btn = ttk.Button(brow, text='Tovární názvy', command=self.on_factory)
        self.factory_btn.pack(side='right', padx=(0, 8))
        self.cancel_btn = ttk.Button(brow, text='Odpojit', command=self.on_cancel)
        self.cancel_btn.pack(side='left')

        # details
        self.show_log = tk.BooleanVar(value=False)
        drow = ttk.Frame(wrap)
        drow.pack(fill='x', pady=(14, 0))
        ttk.Checkbutton(drow, text='Zobrazit podrobnosti', variable=self.show_log,
                        command=self.toggle_log).pack(side='left')
        self.log = tk.Text(wrap, height=9, font=('Consolas', 9), bg='#f6f8fa', fg=INK, relief='flat',
                           padx=8, pady=6, state='disabled', wrap='word')

        self.footer = ttk.Label(wrap, style='Muted.TLabel', wraplength=470, justify='left',
                                text=core.DISCLAIMER.replace('\n', ' ')
                                + f'\nkw37-contacts {core.__version__} · © 2026 Michael Urban · licence MIT')
        self.footer.pack(anchor='w', pady=(14, 0))

        self.set_state('idle')
        self.root.after(80, self.poll)

    # ----------------------------------------------------------- UI state --
    def set_status(self, text, color=MUTED):
        self.status.configure(text=text)
        self.dot.itemconfigure(self.dot_id, fill=color)

    def set_state(self, state):
        self.state = state
        connected = state == 'connected'
        for e in self.entries.values():
            e.configure(state='normal' if connected else 'disabled')
        self.factory_btn.configure(state='normal' if connected else 'disabled')
        self.cancel_btn.configure(state='normal' if state in ('connected', 'waiting') else 'disabled')
        self.cancel_btn.configure(text='Zrušit' if state == 'waiting' else 'Odpojit')
        if state == 'idle':
            self.main_btn.configure(text='Připojit hodinky', state='normal')
            self.set_status('Vypni hodinky a klikni na „Připojit hodinky“.')
        elif state == 'waiting':
            self.main_btn.configure(state='disabled')
        elif state == 'connected':
            self.main_btn.configure(text='Zapsat do hodinek', state='normal')
            self.set_status('Hodinky jsou připojené. Uprav názvy a klikni na „Zapsat do hodinek“.', OK)
        elif state == 'working':
            self.main_btn.configure(state='disabled')
        if state in ('waiting', 'working'):
            self.bar.pack(fill='both', expand=True)
            self.bar.start(12)
        else:
            self.bar.stop()
            self.bar.pack_forget()

    def toggle_log(self):
        if self.show_log.get():
            self.log.pack(fill='both', expand=True, pady=(8, 0), before=self.footer)
        else:
            self.log.pack_forget()

    def on_edit(self, n):
        v = self.vars[n].get()
        self.counters[n].configure(text=f'{len(v)}/{core.SLOTS[n][3]}')

    def add_log(self, s):
        self.log.configure(state='normal')
        if '\r' in s:                                   # progress line: replace the last line
            s = s.split('\r')[-1]
            self.log.delete('end-1c linestart', 'end-1c')
        self.log.insert('end', s)
        self.log.see('end')
        self.log.configure(state='disabled')

    # ------------------------------------------------------------ actions --
    def on_main(self):
        if self.state == 'idle':
            self.start_connect()
        elif self.state == 'connected':
            self.start_write()

    def on_factory(self):
        for n, (factory, _, _, _) in core.SLOTS.items():
            self.vars[n].set(factory)

    def on_cancel(self):
        if self.state == 'waiting':
            self.stop.set()
        elif self.state == 'connected':
            self.disconnect()
            self.set_state('idle')

    def disconnect(self):
        if self.dev is not None:
            self.dev.reset()
            try:
                self.dev.s.close()
            except Exception:
                pass
            self.dev = None

    def on_close(self):
        if self.state == 'working':
            messagebox.showwarning('Kontakty KW37', 'Probíhá zápis do hodinek. Počkej, až skončí.')
            return
        self.stop.set()
        self.disconnect()
        self.root.destroy()

    def run_bg(self, fn):
        def wrapper():
            try:
                fn()
            except core.Abort as e:
                self.q.put(('error', str(e)))
            except Exception as e:                       # serial errors etc.
                self.q.put(('error', f'Chyba komunikace: {e}\n\nOdpoj hodinky, vypni je '
                                     '(reset dírkou) a zkus to znovu.'))
        threading.Thread(target=wrapper, daemon=True).start()

    def start_connect(self):
        need_driver = not self.demo and not core.driver_ready()
        if need_driver and not messagebox.askokcancel(
                'Ovladač', 'Na tomto počítači ještě není ovladač pro port hodinek.\n\n'
                'Nainstaluji ho teď. Windows požádá o oprávnění správce. Vytvoří se k tomu '
                'jednorázový certifikát, jehož soukromý klíč se hned smaže.'):
            return
        self.stop.clear()
        self.set_state('waiting')
        self.set_status('Instaluji ovladač…' if need_driver else
                        'Čekám na hodinky – připoj je teď VYPNUTÉ kabelem k počítači.', ACCENT)

        def job():
            if self.demo:
                with open(self.demo, 'rb') as f:
                    full = f.read()
                img = {s: bytearray(full[s:s + core.SECTOR]) for s in core.all_sectors()}
            else:
                if need_driver:
                    core.driver_action(install=True)
                    self.q.put(('status', ('Čekám na hodinky – připoj je teď VYPNUTÉ kabelem '
                                           'k počítači.', ACCENT)))
                self.dev = core.Watch('auto', stop=self.stop.is_set)
                self.q.put(('status', ('Hodinky nalezeny, kontroluji a zálohuji…', ACCENT)))
                self.dev.selftest()
                core.backup_if_first_use(self.dev)
                img = core.read_sectors(self.dev)
            chunks, labels = core.load_state(img)
            self.q.put(('connected', (img, chunks, labels)))
        self.run_bg(job)

    def start_write(self):
        new_labels = {}
        for n in core.SLOTS:
            v = self.vars[n].get().strip()
            if not v:
                messagebox.showinfo('Kontakty KW37', f'Pozice {n} nemá vyplněný název.')
                return
            if v != self.labels[n]:
                new_labels[n] = v
        if not new_labels:
            messagebox.showinfo('Kontakty KW37', 'Žádný název se nezměnil.')
            return
        summary = '\n'.join(f'{n}.  {self.labels[n]}  →  {v}' for n, v in sorted(new_labels.items()))
        if not messagebox.askyesno(
                'Zapsat do hodinek?', f'{summary}\n\nZápis provádíš na vlastní riziko. '
                'Během zápisu neodpojuj kabel.\n\nPokračovat?', icon='warning', default='no'):
            return
        self.set_state('working')
        self.set_status('Zapisuji do hodinek – neodpojuj kabel…', WARN)
        img, chunks = self.img, self.chunks

        def job():
            new = core.build(img, chunks, new_labels)
            if self.demo:
                print('Demo režim: do hodinek se nic nezapisuje.')
            else:
                if core.read_sectors(self.dev) != img:
                    raise core.Abort('Obsah hodinek se od načtení změnil. Připoj je znovu.')
                core.write_changes(self.dev, img, new)
            self.q.put(('written', None))
        self.run_bg(job)

    # ------------------------------------------------------- worker events --
    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == 'log':
                    self.add_log(data)
                elif kind == 'status':
                    self.set_status(*data)
                elif kind == 'connected':
                    self.img, self.chunks, self.labels = data
                    self.set_state('connected')
                    for n, v in self.labels.items():
                        self.vars[n].set(v)
                elif kind == 'written':
                    self.disconnect()
                    self.set_state('idle')
                    self.set_status('Hotovo. Odpoj kabel a zapni hodinky.', OK)
                    messagebox.showinfo('Hotovo', 'Názvy jsou zapsané a ověřené.\n\n'
                                                  'Odpoj kabel a zapni hodinky.')
                elif kind == 'error':
                    fits = 'nevejde' in data
                    if fits and (self.dev is not None or self.demo):
                        self.set_state('connected')      # names can simply be adjusted
                    else:
                        self.disconnect()
                        self.set_state('idle')
                    if data.strip() != 'Zrušeno.':
                        self.set_status(data.split('\n')[0], BAD)
                        messagebox.showerror('Kontakty KW37', data)
        except queue.Empty:
            pass
        self.root.after(80, self.poll)


def main():
    demo = None
    if len(sys.argv) == 3 and sys.argv[1] == '--demo':
        demo = sys.argv[2]
    try:                                                  # crisp text on high-DPI screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root, demo)
    root.mainloop()


if __name__ == '__main__':
    main()
