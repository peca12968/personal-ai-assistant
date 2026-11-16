"""Scrollable forms whose controls remain reachable on smaller displays."""
import tkinter as tk
from tkinter import ttk


def fit_window(window, width, height):
    """Leave room for window decorations and the taskbar."""
    width = min(width, max(320, window.winfo_screenwidth() - 80))
    height = min(height, max(240, window.winfo_screenheight() - 120))
    window.geometry(f'{width}x{height}')
    window.minsize(min(640, width), min(420, height))


class ScrollableForm(ttk.Frame):
    def __init__(self, window, *, event_root=None):
        super().__init__(window)
        self.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(self, highlightthickness=0, takefocus=False)
        bar = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        bar.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.configure(yscrollcommand=bar.set)
        self.body = ttk.Frame(self.canvas)
        self.item = self.canvas.create_window(0, 0, anchor='nw', window=self.body)
        self.body.bind('<Configure>', self._content_changed)
        self.canvas.bind('<Configure>', self._viewport_changed)
        # Embedded forms bind at their toplevel; handlers restrict the widget scope.
        target = event_root or window
        target.bind('<MouseWheel>', self._wheel, add='+')
        target.bind('<FocusIn>', self._focus, add='+')

    def _content_changed(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def _viewport_changed(self, event):
        self.canvas.itemconfigure(self.item, width=event.width)

    def wrapped_label(self, parent, **kwargs):
        label = ttk.Label(parent, **kwargs)
        parent.bind('<Configure>', lambda event: label.configure(
            wraplength=max(120, event.width - 48)), add='+')
        return label

    def _wheel(self, event):
        if not str(event.widget).startswith(str(self) + '.'):
            return
        if event.widget.winfo_class() in ('Text', 'Listbox', 'Treeview', 'TCombobox'):
            return  # Keep native scrolling / selection inside these controls.
        if self.body.winfo_height() > self.canvas.winfo_height() and event.delta:
            units = -max(1, abs(event.delta) // 120) if event.delta > 0 else max(1, abs(event.delta) // 120)
            self.canvas.yview_scroll(units, 'units')
            return 'break'

    def _focus(self, event):
        # Keyboard navigation must also reveal controls outside the viewport.
        self.reveal(event.widget)

    def reveal(self, widget):
        if not str(widget).startswith(str(self.body) + '.'):
            return
        top = widget.winfo_rooty() - self.body.winfo_rooty()
        bottom = top + widget.winfo_height()
        visible_top = self.canvas.canvasy(0)
        visible_height = self.canvas.winfo_height()
        if top < visible_top:
            self.canvas.yview_moveto(max(0, top - 8) / max(1, self.body.winfo_height()))
        elif bottom > visible_top + visible_height:
            self.canvas.yview_moveto((bottom - visible_height + 8) / max(1, self.body.winfo_height()))
