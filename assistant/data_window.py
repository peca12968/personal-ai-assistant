"""Own data-work dialogs until their terminal queue result is consumed."""
import tkinter as tk


class DataWindow:
    shutdown_text = '正在停止，等候本次工作的終止結果後關閉。'
    shutdown_controls = ()

    def own_window(self, parent):
        self.root = parent._root()
        self.window = tk.Toplevel(self.root)
        self.closing = self.closed = False
        self.poll_timer = None
        if not hasattr(self.root, '_assistant_data_windows'):
            self.root._assistant_data_windows = []
        self.root._assistant_data_windows.append(self)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Destroy>', self._destroyed, add='+')

    def active(self):
        return bool(getattr(self, 'busy', False) or getattr(self, 'running', False))

    def unavailable(self):
        return self.closed or self.closing or getattr(self.root, '_assistant_closing', False)

    def disable_controls(self):
        for name in self.shutdown_controls:
            getattr(self, name).configure(state='disabled')

    def request_shutdown(self):
        if self.closed:
            return
        self.closing = True
        cancel = getattr(self, 'cancel', None)
        if cancel is not None:
            cancel.set()
        self.disable_controls()
        if self.active():
            self.notice.set(self.shutdown_text)

    def cancel_poll(self):
        if self.poll_timer is not None:
            self.window.after_cancel(self.poll_timer)
            self.poll_timer = None

    def begin_poll(self):
        if self.closed:
            return False
        # Also support explicit UI polling without leaving a second callback.
        self.cancel_poll()
        return True

    def schedule_poll(self):
        if self.closed:
            return
        if self.unavailable():
            self.disable_controls()
        if self.closing and not self.active():
            self.close()
        else:
            self.cancel_poll()
            self.poll_timer = self.window.after(100, self.poll)

    def close(self):
        if self.closed:
            return
        self.request_shutdown()
        if not self.active():
            self.cancel_poll()
            self.window.destroy()

    def _destroyed(self, event):
        if event.widget != self.window or self.closed:
            return
        self.cancel_poll()
        self.closed = True
        owned = self.root._assistant_data_windows
        if self in owned:
            owned.remove(self)
