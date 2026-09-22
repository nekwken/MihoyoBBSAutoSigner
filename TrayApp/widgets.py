"""自绘控件：药丸开关、圆角卡片、动画标签栏（替代 ttk 默认控件）。"""
from __future__ import annotations

import ctypes
import sys
import time
import tkinter as tk

import theme as _theme
from dpi import logical_scale as _logical_scale, px as _px

FONT_FAMILY = "Microsoft YaHei UI"


def ui_font(size: float, weight: str | None = None) -> tuple:
    """密度与用户缩放感知的字体（单位是点，Tk 会再按 DPI 放大；Tk 只吃整数）。"""
    try:
        size = max(7, int(round(float(size) * _logical_scale())))
    except Exception:
        pass
    return (FONT_FAMILY, size, weight) if weight else (FONT_FAMILY, size)


class PillSwitch(tk.Canvas):
    """Windows 设置（Fluent）风格开关：40×20 药丸轨道 + 12px 圆钮。

    关：灰色描边轨道 + 灰色圆钮；开：强调色填充 + 白色圆钮。切换时圆钮滑动、
    轨道颜色同步渐变（14 帧 × 16ms，按实际耗时取样，避免掉帧）。

    曲线在 Tk 画布上由 GDI 绘制、没有抗锯齿，所以整颗开关用 PIL 超采样
    （4×后缩回）渲染成图片再贴到画布上；帧图片按「尺寸+配色+位置」全局缓存，
    同款开关共用，重建界面或换主题后自动失效。

    用法与 Checkbutton 相同：PillSwitch(parent, variable=var, bg=CARD, command=fn)
    """

    TRACK_W, TRACK_H = 40, 20      # 设计稿尺寸，取自 WinUI 3 ToggleSwitch
    THUMB, INSET = 12, 4
    DURATION = 0.22                # 动画时长（WinUI 在 167~250ms 之间）
    TICK_MS = 2                    # 逐像素推进，tick 必须比「走过 1 像素」更快

    def __init__(
        self,
        master,
        variable: tk.BooleanVar,
        command=None,
        width: int | None = None,
        height: int | None = None,
        bg: str | None = None,
    ) -> None:
        self._pw = _px(width or self.TRACK_W)
        self._ph = _px(height or self.TRACK_H)
        self._bg = bg or _palette_bg(master)
        super().__init__(
            master,
            width=self._pw,
            height=self._ph,
            highlightthickness=0,
            bd=0,
            bg=self._bg,
            cursor="hand2",
        )
        self._var = variable
        self._command = command
        self._enabled = True
        self._trace = None
        self._pos = 1.0 if bool(variable.get()) else 0.0     # 圆钮位置 0~1
        self._anim_job = None
        self._pressed = False
        self._item = None
        self._photo = None
        self._precision = None
        try:
            self._trace = variable.trace_add("write", lambda *_: self._on_var_changed())
        except Exception:
            pass
        self.bind("<Button-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Destroy>", lambda _e: self._end_precision())   # 动画中被重建也要还原
        self._draw()

    # ── 状态 ─────────────────────────────────────────────────────────────
    def set_enabled(self, value: bool) -> None:
        self._enabled = value
        self.configure(cursor="hand2" if value else "arrow")
        self._draw()

    def get(self) -> bool:
        return bool(self._var.get())

    # ── 交互 ─────────────────────────────────────────────────────────────
    def _on_var_changed(self) -> None:
        self._animate_to(1.0 if self.get() else 0.0)

    def _on_press(self, _event=None) -> None:
        if not self._enabled:
            return
        self._pressed = True
        self._draw()

    def _on_release(self, event=None) -> None:
        if not self._enabled:
            return
        was = self._pressed
        self._pressed = False
        inside = True
        if event is not None:
            try:
                inside = 0 <= event.x <= self._pw and 0 <= event.y <= self._ph
            except Exception:
                inside = True
        if was and inside:
            self._on_click()
        else:
            self._draw()

    def _on_leave(self, _event=None) -> None:
        if self._pressed:
            self._pressed = False
            self._draw()

    def _on_click(self) -> None:
        if not self._enabled:
            return
        try:
            self._var.set(not self._var.get())
        except Exception:
            return
        self._animate_to(1.0 if self.get() else 0.0)
        if self._command is not None:
            try:
                self._command()
            except Exception:
                pass

    # ── 动画 ─────────────────────────────────────────────────────────────
    def _cancel_anim(self) -> None:
        if self._anim_job is not None:
            try:
                self.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
        self._end_precision()

    def _animate_to(self, target: float) -> None:
        self._cancel_anim()
        start = self._pos
        travel = self._travel_px()
        target_idx = int(round(target * travel))
        cur_idx = int(round(start * travel))
        if target_idx == cur_idx:
            self._pos = target
            self._draw()
            return
        self._warm_frames()                  # 先把逐像素的帧渲染好，避免动画中途卡顿
        self._begin_precision()              # 15.6ms 定时器粒度会把帧数砍到 1/7
        t0 = time.perf_counter()

        def tick() -> None:
            nonlocal cur_idx
            if not self.winfo_exists():       # 界面重建时可能已被销毁
                self._end_precision()
                return
            k = min(1.0, (time.perf_counter() - t0) / self.DURATION)
            # 时钟只决定「该不该前进」，位置每次最多走 1 像素：缓出起步阶段最快，
            # 定时器抖动再大也不会跳过中间的像素位置
            want = int(round((start + (target - start) * (1 - (1 - k) ** 3)) * travel))
            if want != cur_idx:
                cur_idx += 1 if want > cur_idx else -1
            self._pos = cur_idx / travel
            self._draw()
            if k >= 1.0 and cur_idx == target_idx:
                self._pos = target
                self._anim_job = None
                self._draw()
                self._end_precision()
            else:
                self._anim_job = self.after(self.TICK_MS, tick)

        self._anim_job = self.after(self.TICK_MS, tick)

    # ── 定时器精度（15.6ms 粒度会把动画帧数砍半）─────────────────────────
    def _begin_precision(self) -> None:
        if self._precision is None:
            self._precision = timer_precision(1)
            self._precision.__enter__()

    def _end_precision(self) -> None:
        if self._precision is not None:
            self._precision.__exit__(None, None, None)
            self._precision = None

    # ── 绘制（PIL 超采样 → 图片）─────────────────────────────────────────
    def _style(self) -> tuple:
        """当前配色：(描边, 填充, 圆钮, 描边透明度)。"""
        p = _theme.palette()
        if self._enabled:
            return _hex_rgb(p["muted"]), _hex_rgb(p["accent"]), _hex_rgb(p["accent_text"]), 255, \
                _hex_rgb(p["muted"])
        return _hex_rgb(p["muted"]), _hex_rgb(p["accent"]), _hex_rgb(p["muted"]), 100, \
            _hex_rgb(p["muted"])

    def _travel_px(self) -> int:
        """圆钮的行程（物理像素）——也就是这段动画最多有几种不同画面。"""
        return max(1, self._pw - 2 * _px(self.INSET) - _px(self.THUMB))

    def _photo_for(self, pos: float, pressed: bool):
        stroke, on_fill, on_thumb, line_alpha, off_thumb = self._style()
        travel = self._travel_px()
        # 位置量化到物理像素：滑块只落在整数像素上，一张图对应一个像素位置
        idx = int(round(max(0.0, min(1.0, pos)) * travel))
        key = ("switch", self._pw, self._ph, stroke, on_fill, on_thumb, line_alpha,
               off_thumb, idx, pressed)
        if key in _PHOTO_CACHE:
            return _PHOTO_CACHE[key]
        t = idx / travel
        # 描边颜色随进度过渡到强调色（开态与填充同色即看不出描边），圆钮由灰变白
        ring = tuple(round(stroke[i] + (on_fill[i] - stroke[i]) * t) for i in range(3))
        thumb = tuple(round(off_thumb[i] + (on_thumb[i] - off_thumb[i]) * t)
                      for i in range(3))
        fill_alpha = int(round(255 * t))

        def make():
            from PIL import Image, ImageDraw, ImageTk
            scale = 4
            W, H = self._pw * scale, self._ph * scale
            img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            box = (0, 0, W - 1, H - 1)
            radius = (H - 1) / 2
            # 填充：关态完全透明（露出卡片底色），开态强调色，中间按进度渐变
            if fill_alpha > 0:
                d.rounded_rectangle(box, radius=radius, fill=on_fill + (fill_alpha,))
            # 描边：贴边向内 1px 的环（PIL 会给圆角做抗锯齿）
            d.rounded_rectangle(box, radius=radius, outline=ring + (line_alpha,),
                                width=scale)
            inset = _px(self.INSET)
            th = _px(self.THUMB) - (2 if pressed else 0) * _px(1)
            kx = (inset + idx + (th - _px(self.THUMB)) / 2) * scale   # 按下时保持中心
            ky = (H - th * scale) / 2
            d.ellipse((kx, ky, kx + th * scale, ky + th * scale), fill=thumb + (255,))
            return ImageTk.PhotoImage(img.resize((self._pw, self._ph), Image.LANCZOS))

        return _cached_photo(key, make)

    def _warm_frames(self) -> None:
        """预热整段动画：逐个像素位置的帧 + 两端按下态，之后切换只是换图片。"""
        travel = self._travel_px()
        for i in range(travel + 1):
            self._photo_for(i / travel, False)
        for i in (0, travel):          # 按下只可能发生在两端（动画中已经松手）
            self._photo_for(i / travel, True)

    def _draw(self) -> None:
        if not self.winfo_exists():
            return
        photo = self._photo_for(self._pos, self._pressed and self._enabled)
        self._photo = photo
        try:
            if self._item is None:
                self._item = self.create_image(0, 0, anchor="nw", image=photo)
            else:
                self.itemconfigure(self._item, image=photo)
        except Exception:
            self._item = None


def _hex_rgb(color: str) -> tuple:
    """#RRGGBB → (r, g, b)；解析失败返回中灰。"""
    try:
        text = str(color).strip().lstrip("#")
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) == 6:
            return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        pass
    return (128, 128, 128)


class timer_precision:
    """临时把系统定时器精度提到 1ms（退出时还原）。

    空闲消息循环里 Windows 的定时器粒度是 15.6ms：`after(2)` 会被拖到 ~15.3ms、
    `after(16)` 会被拖到 ~23ms，动画因此少画一半帧。动画期间才开启，结束立刻还原。
    """

    _active = 0

    def __init__(self, ms: int = 1) -> None:
        self._ms = ms
        self._held = False

    def __enter__(self):
        try:
            if sys.platform == "win32":
                if ctypes.windll.winmm.timeBeginPeriod(self._ms) == 0:
                    self._held = True
                    timer_precision._active += 1
        except Exception:
            self._held = False
        return self

    def __exit__(self, *_exc):
        if not self._held:
            return False
        self._held = False
        timer_precision._active = max(0, timer_precision._active - 1)
        try:
            ctypes.windll.winmm.timeEndPeriod(self._ms)
        except Exception:
            pass
        return False


# 超采样图片缓存：Tk 画布走 GDI，曲线没有抗锯齿，圆角/圆形一律用 PIL 画好再贴图。
# 缓存按 Tk 解释器失效（新建/销毁窗口后旧的 PhotoImage 不再可用）。
_PHOTO_CACHE: dict = {}
_PHOTO_ROOT = None


def _cached_photo(key, factory):
    global _PHOTO_ROOT
    root = getattr(tk, "_default_root", None)
    if root is not _PHOTO_ROOT:
        _PHOTO_CACHE.clear()
        _PHOTO_ROOT = root
    img = _PHOTO_CACHE.get(key)
    if img is None:
        try:
            img = factory()
        except Exception:
            return None
        _PHOTO_CACHE[key] = img
    return img


def rounded_photo(width: int, height: int, color: str, radius: float | None = None,
                  alpha: int = 255, scale: int = 4):
    """抗锯齿的圆角矩形/药丸图片（radius 省略时按半高取圆角）。"""
    w, h = max(1, int(round(width))), max(1, int(round(height)))
    r = (h - 0) / 2 if radius is None else float(radius)
    rgb = _hex_rgb(color)
    alpha = max(0, min(255, int(alpha)))
    key = ("round", w, h, round(r, 2), rgb, alpha, scale)

    def make():
        from PIL import Image, ImageDraw, ImageTk
        W, H = w * scale, h * scale
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(img).rounded_rectangle(
            (0, 0, W - 1, H - 1), radius=max(0.0, r * scale), fill=rgb + (alpha,))
        return ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))

    return _cached_photo(key, make)


class RoundedCard(tk.Frame):
    """卡片容器：实心卡片色 + 1px 描边（不做画布挖角，避免任何像素碎块）。内容放进 `.body`。"""

    def __init__(self, master, surround: str, card: str | None = None,
                 radius: int = 8, pad: int = 10) -> None:
        p = _theme.palette()
        self._card_bg = card or p["card"]
        self._surround = surround
        super().__init__(master, bg=self._card_bg, padx=_px(pad), pady=_px(pad),
                         highlightbackground=p["line"], highlightthickness=_px(1))
        self.body = self


class TabBar(tk.Canvas):
    """顶部标签栏：文字标签 + 可动画滑动的下划线指示条。"""

    def __init__(self, master, labels: list[str], on_select, bg: str,
                 fg: str, muted: str, accent: str, font) -> None:
        super().__init__(master, highlightthickness=0, bd=0, bg=bg,
                         height=_px(36), takefocus=0)
        self._labels = labels
        self._on_select = on_select
        self._text_fg = fg
        self._muted = muted
        self._accent = accent
        self._font = font
        self._items: list[int] = []
        self._bounds: list[tuple[int, int]] = []
        self._index = 0
        self._indicator: int | None = None
        self.bind("<Button-1>", self._click)
        self.after(_px(20), self._redraw)

    # ── 绘制 ─────────────────────────────────────────────────────────────
    def _redraw(self) -> None:
        try:
            self.delete("all")
        except Exception:
            return
        self._items = []
        self._bounds = []
        x = _px(2)
        for text in self._labels:
            item = self.create_text(x, _px(19), text=text, anchor="w",
                                    fill=self._muted, font=self._font)
            bbox = self.bbox(item)
            w = (bbox[2] - bbox[0]) if bbox else _px(40)
            self._items.append(item)
            self._bounds.append((x, w))
            x += w + _px(22)
        self.configure(width=x + _px(4))
        self._paint()

    def _paint(self) -> None:
        if not self._items:
            return
        for i, item in enumerate(self._items):
            self.itemconfigure(item, fill=self._accent if i == self._index else self._muted)
        if self._indicator is not None:
            try:
                self.delete(self._indicator)
            except Exception:
                pass
        x, w = self._bounds[self._index]
        self._indicator = self.create_rectangle(
            x, _px(31), x + w, _px(34), fill=self._accent, outline=self._accent)

    # ── 交互 ─────────────────────────────────────────────────────────────
    def _click(self, event) -> None:
        for i, (x, w) in enumerate(self._bounds):
            if x - _px(8) <= event.x <= x + w + _px(8):
                self.select(i)
                return

    def index(self) -> int:
        return self._index

    def select(self, idx: int, animate: bool = True) -> None:
        if not self._items:
            return
        idx = max(0, min(len(self._labels) - 1, int(idx)))
        if idx == self._index:
            return
        if getattr(self, "_anim_job", None):
            # 上一次动画还没结束：立即落位再开始新的，避免点击被吞
            try:
                self.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
            self._place_indicator(self._index)
        self._index = idx
        for i, item in enumerate(self._items):
            self.itemconfigure(item, fill=self._accent if i == idx else self._muted)
        if animate and self._indicator is not None:
            self._animate_indicator(self._bounds[idx])
        else:
            self._place_indicator(idx)
        self._on_select(idx)

    def _place_indicator(self, idx: int) -> None:
        if self._indicator is None:
            return
        x, w = self._bounds[idx]
        try:
            self.coords(self._indicator, x, _px(31), x + w, _px(34))
        except Exception:
            pass

    def _animate_indicator(self, target: tuple[int, int], step: int = 0) -> None:
        """非阻塞动画：每帧用 after 调度，避免阻塞事件循环（否则窗口会掉层、点击被吞）。"""
        if self._indicator is None or not self._bounds:
            return
        steps = 14
        if step == 0:
            self._anim_from = self._bounds[self._index]
        fx, fw = getattr(self, "_anim_from", self._bounds[self._index])
        tx, tw = target
        if step >= steps:
            self._place_indicator(self._index)
            self._anim_job = None
            return

        def ease(t: float) -> float:
            return 1 - (1 - t) * (1 - t)  # ease-out

        k = ease((step + 1) / steps)
        x = fx + (tx - fx) * k
        w = fw + (tw - fw) * k
        try:
            self.coords(self._indicator, x, _px(31), x + w, _px(34))
        except Exception:
            self._anim_job = None
            return
        self._anim_job = self.after(16, lambda: self._animate_indicator(target, step + 1))


class SlimScrollbar(tk.Canvas):
    """极简滚动条：细圆角滑块、无箭头、轨道透明，可点击跳转与拖动。"""

    def __init__(self, master, target: tk.Canvas, bg: str, width: int = 8) -> None:
        self._bar_w = _px(width)
        super().__init__(master, width=self._bar_w, highlightthickness=0, bd=0,
                         bg=bg, takefocus=0, cursor="arrow")
        self._target = target
        self._first, self._last = 0.0, 1.0
        self._drag_from = None
        self._hover = False
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_drag_from", None))
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        try:
            target.configure(yscrollcommand=self._on_scroll)
        except Exception:
            pass

    # ── 与目标画布同步 ───────────────────────────────────────────────────
    def _on_scroll(self, first, last) -> None:
        try:
            self._first, self._last = float(first), float(last)
        except Exception:
            return
        self._redraw()

    def _set_hover(self, value: bool) -> None:
        self._hover = value
        self._redraw()

    def _thumb(self) -> tuple[int, int]:
        h = self.winfo_height()
        top = int(h * self._first)
        bottom = int(h * self._last)
        if bottom - top < _px(28):
            bottom = min(h, top + _px(28))
        return top, bottom

    # ── 绘制 ─────────────────────────────────────────────────────────────
    def _redraw(self) -> None:
        try:
            self.delete("all")
        except Exception:
            return
        if self._last - self._first >= 0.999 or self.winfo_height() <= 8:
            return
        p = _theme.palette()
        color = p["accent"] if self._hover else p["muted"]
        top, bottom = self._thumb()
        bar_w = max(2, self._bar_w - _px(2))
        photo = rounded_photo(bar_w, max(4, bottom - top), color)
        if photo is None or not self.winfo_exists():
            return
        self._photo = photo                          # 必须持有引用，否则图片会被回收
        try:
            self.create_image((self._bar_w - bar_w) / 2, top, anchor="nw", image=photo)
        except Exception:                            # 图片属于别的解释器/已失效
            self._photo = None

    # ── 交互 ─────────────────────────────────────────────────────────────
    def _on_press(self, event) -> None:
        top, bottom = self._thumb()
        if top <= event.y <= bottom:
            self._drag_from = event.y
            return
        # 点击轨道：把视图居中到该位置
        h = max(1, self.winfo_height())
        span = max(0.001, self._last - self._first)
        start = max(0.0, min(1.0 - span, event.y / h - span / 2))
        try:
            self._target.yview_moveto(start)
        except Exception:
            pass

    def _on_drag(self, event) -> None:
        if self._drag_from is None:
            return
        h = max(1, self.winfo_height())
        span = max(0.001, self._last - self._first)
        delta = (event.y - self._drag_from) / h
        try:
            self._target.yview_moveto(max(0.0, min(1.0 - span, self._first + delta)))
        except Exception:
            pass
        self._drag_from = event.y


class WheelPicker(tk.Canvas):
    """iOS 风格滚轮选择器：滚轮/拖动滚动，松手后带动画吸附到中间项（可循环）。"""

    ROWS = 3                      # 可见行数（中间为选中行）
    ROW_H = 26

    def __init__(self, master, values: list[str], variable: tk.Variable,
                 command=None, width: int = 56, wrap: bool = True) -> None:
        p = _theme.palette()
        self._values = list(values)
        self._var = variable
        self._command = command
        self._wrap = wrap
        self._pw = _px(width)
        self._row_h = _px(self.ROW_H)
        self._ph = self._row_h * self.ROWS
        self._frac = 0.0            # 当前的滚动偏移（行数，含小数）
        self._anim_job = None
        self._drag_y = None
        self._surround = _palette_bg(master)
        super().__init__(master, width=self._pw, height=self._ph, bd=0,
                         highlightthickness=0, bg=self._surround, takefocus=0,
                         cursor="hand2")
        try:
            self._index = max(0, self._values.index(str(variable.get())))
        except Exception:
            self._index = 0
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda _e: (self._step(-1), "break")[1])
        self.bind("<Button-5>", lambda _e: (self._step(1), "break")[1])
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.after(_px(20), self._draw)

    # ── 取值 ─────────────────────────────────────────────────────────────
    def get(self) -> str:
        return str(self._var.get())

    def set(self, value: str, animate: bool = False) -> None:
        try:
            idx = self._values.index(str(value))
        except ValueError:
            return
        if idx == self._index:
            return
        delta = idx - self._index
        if self._wrap and abs(delta) > len(self._values) / 2:
            delta = int(delta - len(self._values) * (1 if delta > 0 else -1))
        if animate:
            self._animate_to(delta)
        else:
            self._index = idx
            self._frac = 0.0
            self._sync_var()
            self._draw()

    # ── 交互 ─────────────────────────────────────────────────────────────
    def _on_wheel(self, event) -> str:
        self._step(-1 if getattr(event, "delta", 0) > 0 else 1)
        return "break"          # 阻止冒泡到页面的滚动绑定

    def _step(self, direction: int) -> None:
        self._animate_to(direction)

    def _on_press(self, event) -> None:
        if self._anim_job:
            try:
                self.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
            self._index = self._index - int(round(self._frac))
            self._frac = 0.0
        self._drag_y = event.y
        self._drag_frac = 0.0

    def _on_drag(self, event) -> None:
        if self._drag_y is None:
            return
        self._drag_frac = (event.y - self._drag_y) / self._row_h
        self._draw(extra=self._drag_frac)

    def _on_release(self, _event) -> None:
        if self._drag_y is None:
            return
        self._drag_y = None
        # 按住拖动的距离 → 折算成行数并吸附
        steps = int(-self._drag_frac + (0.5 if self._drag_frac < 0 else -0.5))
        self._animate_to(steps, start=self._drag_frac)

    # ── 动画（非阻塞，缓出吸附）──────────────────────────────────────────
    def _animate_to(self, steps: int, start: float = 0.0) -> None:
        if self._anim_job:
            try:
                self.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
        self._tick(steps, start, 0)

    def _tick(self, steps: int, start: float, frame: int) -> None:
        frames = 7
        if frame >= frames:
            self._index = self._norm(self._index + steps)
            self._frac = 0.0
            self._anim_job = None
            self._sync_var()
            self._draw()
            return

        def ease(t: float) -> float:
            return 1 - (1 - t) * (1 - t)

        k = ease((frame + 1) / frames)
        self._frac = start * (1 - k) + (-steps) * k      # 从 start 平滑滚到 -steps
        self._draw()
        self._anim_job = self.after(14, lambda: self._tick(steps, start, frame + 1))

    def _norm(self, idx: int) -> int:
        n = len(self._values)
        return (idx % n + n) % n if self._wrap else max(0, min(n - 1, idx))

    def _sync_var(self) -> None:
        try:
            value = self._values[self._index]
        except Exception:
            return
        if str(self._var.get()) != value:
            self._var.set(value)
            if self._command is not None:
                try:
                    self._command()
                except Exception:
                    pass

    # ── 绘制 ─────────────────────────────────────────────────────────────
    def _draw(self, extra: float = 0.0) -> None:
        try:
            self.delete("all")
        except Exception:
            return
        p = _theme.palette()
        n = len(self._values)
        if not n:
            return
        center_y = self._ph // 2
        # iOS 风格：中间行高亮，上下两行渐隐
        self.create_rectangle(0, center_y - self._row_h // 2, self._pw,
                              center_y + self._row_h // 2,
                              fill=p["line"], outline=p["line"])
        frac = getattr(self, "_frac", 0.0) + extra
        span = self.ROWS // 2 + 1
        for off in range(-span, span + 1):
            idx = self._norm(self._index + off)
            y = center_y + int((off + frac) * self._row_h)
            if y < -self._row_h or y > self._ph + self._row_h:
                continue
            dist = abs(off + frac)
            if dist < 0.5:
                color, font = p["text"], ui_font(12, "bold")
            elif dist < 1.5:
                color, font = p["muted"], ui_font(11)
            else:
                continue
            self.create_text(self._pw // 2, y, text=self._values[idx], fill=color,
                             font=font)


class RoundedSelect(tk.Canvas):
    """圆角下拉选择：圆角字段 + 圆角弹出列表（替代 ttk.Combobox）。"""

    def __init__(self, master, values: list[str], variable: tk.Variable,
                 command=None, width: int = 96) -> None:
        self._values = list(values)
        self._var = variable
        self._command = command
        self._surround = _palette_bg(master)
        self._pw = _px(width)
        self._ph = _px(30)
        self._hover = False
        self._popup = None
        self._popup_canvas = None
        self._rows: list[tuple[int, str]] = []
        self._hover_row = -1
        super().__init__(master, width=self._pw, height=self._ph, bd=0,
                         highlightthickness=0, bg=self._surround, takefocus=0,
                         cursor="hand2")
        self.bind("<Button-1>", lambda _e: self.toggle())
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        try:
            variable.trace_add("write", lambda *_: self._draw())
        except Exception:
            pass
        self.after(_px(30), self._draw)

    def get(self) -> str:
        return str(self._var.get())

    def set(self, value: str) -> None:
        self._var.set(value)
        self._draw()

    def _font(self):
        return ui_font(10)

    # ── 绘制字段 ─────────────────────────────────────────────────────────
    def _set_hover(self, value: bool) -> None:
        self._hover = value
        self._draw()

    def _draw(self) -> None:
        try:
            self.delete("all")
        except Exception:
            return
        p = _theme.palette()
        w, h = self._pw, self._ph
        line = p["accent"] if self._hover else p["line"]
        self.create_rectangle(0, 0, w - 1, h - 1, fill=p["entry_bg"], outline=line)
        self.create_text(_px(10), h // 2, text=self.get(), anchor="w",
                         fill=p["entry_fg"], font=self._font())
        cx, cy = w - _px(13), h // 2
        self.create_line(cx - _px(4), cy - _px(2), cx, cy + _px(2), cx + _px(4),
                         cy - _px(2), fill=p["muted"], width=max(1, _px(1)))

    # ── 弹出列表 ─────────────────────────────────────────────────────────
    def toggle(self) -> None:
        if self._popup is not None:
            self.close()
        else:
            self.open()

    def open(self) -> None:
        """弹层：不透明圆角卡片 + 窗口区域裁剪（不用颜色键，避免碎块与可读性问题）。"""
        p = _theme.palette()
        row_h, pad = _px(28), _px(6)
        height = row_h * len(self._values) + pad * 2
        top = tk.Toplevel(self)
        top.overrideredirect(True)
        top.configure(bg=p["card"])
        field_bottom = self.winfo_rooty() + self._ph + _px(2)
        below_space = self.winfo_screenheight() - field_bottom
        y = field_bottom if below_space >= height + _px(8) else self.winfo_rooty() - height - _px(2)
        top.geometry(f"{self._pw}x{height}+{self.winfo_rootx()}+{y}")
        canvas = tk.Canvas(top, bg=p["card"], bd=0, highlightthickness=0, takefocus=0)
        canvas.pack(fill="both", expand=True)
        canvas.create_rectangle(0, 0, self._pw, height, fill=p["card"], outline=p["card"])
        self._popup, self._popup_canvas = top, canvas
        self._rows = []
        for i, text in enumerate(self._values):
            y0 = pad + row_h * i
            bgid = canvas.create_rectangle(0, y0, self._pw, y0 + row_h,
                                           fill=p["card"], outline=p["card"])
            item = canvas.create_text(_px(12), y0 + row_h // 2, text=text, anchor="w",
                                      fill=p["text"], font=self._font())
            current = str(self._var.get()) == text
            if current:
                canvas.itemconfigure(item, fill=p["accent"])
            self._rows.append((bgid, text))
            _ = item
        canvas.bind("<Motion>", self._on_motion)
        canvas.bind("<Button-1>", self._on_click)
        canvas.bind("<Leave>", lambda _e: self._clear_hover())
        top.bind("<Button-1>", lambda _e: self.close())
        top.update_idletasks()
        try:
            _theme.round_window_region(top, _px(8))
        except Exception:
            pass
        try:
            top.grab_set()
            top.bind("<Escape>", lambda _e: self.close())
            top.bind("<FocusOut>", lambda _e: self.close())
            top.focus_force()
        except Exception:
            pass
        self._hover_row = -1

    def close(self) -> None:
        try:
            if self._popup is not None:
                self._popup.grab_release()
                self._popup.destroy()
        except Exception:
            pass
        self._popup = None
        self._popup_canvas = None

    def _row_index(self, y: int) -> int:
        return int((y - _px(6)) // _px(26))

    def _clear_hover(self) -> None:
        p = _theme.palette()
        for bgid, _text in self._rows:
            try:
                self._popup_canvas.itemconfigure(bgid, fill=p["card"], outline=p["card"])
            except Exception:
                pass
        self._hover_row = -1

    def _on_motion(self, event) -> None:
        idx = self._row_index(event.y)
        if idx < 0 or idx >= len(self._rows):
            self._clear_hover()
            return
        if idx == self._hover_row:
            return
        self._clear_hover()
        p = _theme.palette()
        try:
            self._popup_canvas.itemconfigure(self._rows[idx][0], fill=p["line"],
                                            outline=p["line"])
        except Exception:
            pass
        self._hover_row = idx

    def _on_click(self, event) -> None:
        idx = self._row_index(event.y)
        if 0 <= idx < len(self._rows) and 0 <= event.x <= self._pw:
            self.set(self._rows[idx][1])
            if self._command is not None:
                try:
                    self._command()
                except Exception:
                    pass
        self.close()   # 点中选项或点在弹层外，都收起


class ProgressPill(tk.Canvas):
    """不定量进度条（Fluent 风格）：圆角轨道里一段圆角高光来回滑动。

    Tk 画布的圆角没有抗锯齿，所以轨道与高光都用 PIL 画好再贴图（同 PillSwitch）。
    """

    HEIGHT = 6
    TICK_MS = 16
    STEP = 0.028          # 每 tick 前进的轨道比例

    def __init__(self, master, bg: str, width: int = 300, height: int | None = None) -> None:
        self._pw = _px(width)
        self._ph = _px(height or self.HEIGHT)
        self._bg = bg
        super().__init__(master, width=self._pw, height=self._ph, bd=0,
                         highlightthickness=0, bg=bg, takefocus=0)
        self._pos = -0.4
        self._job = None
        self._precision = None
        self._track_img = None
        self._knob_img = None
        self._track_item = None
        self._knob_item = None
        self._track_w = 0

    # ── 动画 ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._job is not None or not self.winfo_exists():
            return
        if self._precision is None:
            self._precision = timer_precision(1)      # 否则 16ms tick 会被拖到 23ms
            self._precision.__enter__()
        self._draw()
        self._job = self.after(self.TICK_MS, self._tick)

    def stop(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        if self._precision is not None:
            self._precision.__exit__(None, None, None)
            self._precision = None

    def _tick(self) -> None:
        self._job = None
        if not self.winfo_exists():
            self.stop()
            return
        self._pos += self.STEP
        if self._pos > 1.4:
            self._pos = -0.4
        self._draw()
        self._job = self.after(self.TICK_MS, self._tick)

    # ── 绘制 ─────────────────────────────────────────────────────────────
    def _draw(self) -> None:
        if not self.winfo_exists():
            return
        p = _theme.palette()
        w = max(1, self.winfo_width() or self._pw)
        h = self._ph
        if self._track_item is None or self._track_w != w:
            img = _cached_photo(("track", w, h, p["card"], p["line"]),
                                lambda: _track_photo(w, h, p["card"], p["line"]))
            if img is None:
                return
            self._track_img = img
            self._track_w = w
            if self._track_item is None:
                self._track_item = self.create_image(0, 0, anchor="nw", image=img)
            else:
                self.itemconfigure(self._track_item, image=img)
        span = max(int(w * 0.3), _px(36))
        x0 = int(self._pos * w)
        x1 = x0 + span
        if x1 < 0 or x0 > w:                      # 高光完全在轨道外
            if self._knob_item is not None:
                self.itemconfigure(self._knob_item, state="hidden")
            return
        cx0, cx1 = max(0, x0), min(w, x1)
        img = rounded_photo(max(2, cx1 - cx0), h, p["accent"])
        if img is None:
            return
        self._knob_img = img
        if self._knob_item is None:
            self._knob_item = self.create_image(cx0, 0, anchor="nw", image=img)
        else:
            self.coords(self._knob_item, cx0, 0)
            self.itemconfigure(self._knob_item, image=img, state="normal")


def _track_photo(w: int, h: int, card: str, line: str):
    """进度条轨道：卡片色底 + 1px 描边的圆角长条（PIL 抗锯齿）。"""
    from PIL import Image, ImageDraw, ImageTk
    scale = 4
    W, H = max(1, w) * scale, max(1, h) * scale
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = (H - 1) / 2
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=radius,
                        fill=_hex_rgb(card) + (255,),
                        outline=_hex_rgb(line) + (255,), width=scale)
    return ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))


class ScrollPage(tk.Frame):
    """页面容器：装得下时完全不可滚动（不画滚动条、不响应滚轮），装不下才出现滚动。

    内容放进 `.inner`；滚动状态由自身测量决定，调用方只需在内容变化后调用 sync()。
    """

    def __init__(self, master, bg: str, bar_width: int = 8) -> None:
        super().__init__(master, bg=bg)
        self._bg = bg
        self.canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0, takefocus=0,
                                yscrollincrement=_px(20))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar = SlimScrollbar(self, target=self.canvas, bg=bg, width=bar_width)
        self.bar.pack(side="right", fill="y")
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self._scrollable = False
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.inner.bind("<Configure>", lambda _e: self.sync())

    def _on_canvas_configure(self, event) -> None:
        try:
            self.canvas.itemconfigure(self._win, width=event.width)
        except Exception:
            return
        self.sync()

    def sync(self) -> None:
        """重新测量内容与可视区，决定是否需要滚动。"""
        try:
            need = self.inner.winfo_reqheight()
            view = self.canvas.winfo_height()
        except Exception:
            return
        if view < 20:          # 窗口还没显示（或刚重建），几何值没有参考意义
            return
        try:
            self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), need))
        except Exception:
            return
        self._scrollable = need > view + 2
        if not self._scrollable:
            try:
                self.canvas.yview_moveto(0)
            except Exception:
                pass

    def is_scrollable(self) -> bool:
        return self._scrollable

    def scroll_by(self, units: int) -> None:
        try:
            self.canvas.yview_scroll(int(units), "units")
        except Exception:
            pass


class ReorderList(tk.Frame):
    """可拖动排序的行列表：行按绝对坐标摆放，拖动时其它行动画让位（不销毁重建，无闪烁）。

    用法：
        lst = ReorderList(parent, row_height=30, gap=2, bg=CARD, on_reorder=cb)
        lst.set_rows([(key, build_fn), ...], container_height=…)
    其中 build_fn(row_frame) 负责把该行内容填进给定的 Frame。
    """

    def __init__(self, master, row_height: int, gap: int, bg: str,
                 on_reorder=None, drag_cursor: str = "fleur") -> None:
        super().__init__(master, bg=bg)
        self._row_h = _px(row_height)
        self._gap = _px(gap)
        self._bg = bg
        self._on_reorder = on_reorder
        self._drag_cursor = drag_cursor
        self._rows: list[tuple[object, tk.Frame]] = []
        self._jobs: dict[tk.Frame, str] = {}
        self._drag: dict | None = None
        self._target_index = 0

    # ── 构建 ─────────────────────────────────────────────────────────────
    def set_rows(self, specs: list[tuple[object, object]], height: int | None = None) -> None:
        """specs = [(key, build_fn), ...]；按当前顺序重建（仅在结构变化时调用）。"""
        for _key, frame in self._rows:
            job = self._jobs.pop(frame, None)
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
            frame.destroy()
        self._rows = []
        for key, build in specs:
            frame = tk.Frame(self, bg=self._bg, height=self._row_h)
            frame.pack_propagate(False)
            build(frame)
            self._rows.append((key, frame))
        total = len(self._rows) * (self._row_h + self._gap)
        self.configure(height=height or total)
        self.pack_propagate(False)
        self._bind_drag()
        self._layout(animate=False)

    def keys(self) -> list[object]:
        return [key for key, _f in self._rows]

    def row_frame(self, key) -> tk.Frame | None:
        for k, frame in self._rows:
            if k == key:
                return frame
        return None

    def _slots(self) -> list[int]:
        step = self._row_h + self._gap
        return [i * step for i in range(len(self._rows))]

    def _layout(self, animate: bool = True) -> None:
        slots = self._slots()
        for (key, frame), y in zip(self._rows, slots):
            if self._drag and self._drag["key"] == key:
                continue
            self._place(frame, y, animate)

    def _place(self, frame: tk.Frame, y: int, animate: bool = True) -> None:
        job = self._jobs.pop(frame, None)
        if job:
            try:
                self.after_cancel(job)
            except Exception:
                pass
        cur = frame.winfo_y() if frame.winfo_manager() == "place" else y
        if not animate or abs(cur - y) <= 1:
            frame.place(x=0, y=y, relwidth=1, height=self._row_h)
            return
        self._tween(frame, cur, y, 0)

    def _tween(self, frame: tk.Frame, start: int, end: int, step: int) -> None:
        frames = 8
        if step >= frames:
            try:
                frame.place(x=0, y=end, relwidth=1, height=self._row_h)
            except Exception:
                pass
            self._jobs.pop(frame, None)
            return

        def ease(t: float) -> float:
            return 1 - (1 - t) * (1 - t)

        y = int(start + (end - start) * ease((step + 1) / frames))
        try:
            frame.place(x=0, y=y, relwidth=1, height=self._row_h)
        except Exception:
            self._jobs.pop(frame, None)
            return
        self._jobs[frame] = self.after(14, lambda: self._tween(frame, start, end, step + 1))

    # ── 拖动 ─────────────────────────────────────────────────────────────
    _INTERACTIVE = frozenset({
        "Canvas", "Button", "TButton", "Entry", "TEntry", "Text", "TCombobox",
        "Listbox", "Scale", "TScale", "Spinbox", "TSpinbox",
        "Checkbutton", "TCheckbutton", "Radiobutton", "TRadiobutton",
    })

    @staticmethod
    def _descendants(widget) -> list:
        out = []
        for child in widget.winfo_children():
            out.append(child)
            out.extend(ReorderList._descendants(child))
        return out

    def _bind_drag(self) -> None:
        """整行可拖：行内的静态子控件也登记拖动，开关/按钮等交互控件除外。"""
        for key, frame in self._rows:
            frame.configure(cursor=self._drag_cursor)
            frame.bind("<Button-1>", lambda e, k=key: self._drag_start(e, k))
            frame.bind("<B1-Motion>", self._drag_move)
            frame.bind("<ButtonRelease-1>", self._drag_end)
            for w in self._descendants(frame):
                if w.winfo_class() in self._INTERACTIVE:
                    continue
                try:
                    w.configure(cursor=self._drag_cursor)
                except Exception:
                    pass
                w.bind("<Button-1>", lambda e, k=key: self._drag_start(e, k))
                w.bind("<B1-Motion>", self._drag_move)
                w.bind("<ButtonRelease-1>", self._drag_end)

    def _drag_start(self, event, key) -> None:
        index = [k for k, _f in self._rows].index(key)
        frame = self.row_frame(key)
        if frame is None:
            return
        self._target_index = index
        self._drag = {
            "key": key, "frame": frame, "index": index,
            "y0": event.y_root, "base": self._slots()[index],
        }
        p = _theme.palette()
        try:
            frame.configure(highlightbackground=p["accent"], highlightthickness=_px(1))
            frame.lift()
        except Exception:
            pass

    def _drag_move(self, event) -> None:
        if not self._drag:
            return
        dy = event.y_root - self._drag["y0"]
        cur = max(0, self._drag["base"] + dy)
        frame = self._drag["frame"]
        try:
            frame.place(x=0, y=cur, relwidth=1, height=self._row_h)
        except Exception:
            return
        step = self._row_h + self._gap
        target = max(0, min(len(self._rows) - 1, int(round(cur / step))))
        if target != self._target_index:
            self._target_index = target
            self._shift_others()

    def _shift_others(self) -> None:
        """其它行按目标位置让位（带动画）。"""
        slots = self._slots()
        keys = [k for k, _f in self._rows]
        drag_key = self._drag["key"]
        pos = 0
        for key, frame in self._rows:
            if key == drag_key:
                continue
            y = slots[pos if pos < self._target_index else pos + 1]
            self._place(frame, y, animate=True)
            pos += 1

    def _drag_end(self, _event) -> None:
        if not self._drag:
            return
        drag = self._drag
        self._drag = None
        target = self._target_index
        frame = drag["frame"]
        try:
            frame.configure(highlightthickness=0)
        except Exception:
            pass
        self._place(frame, self._slots()[target], animate=True)
        keys = [k for k, _f in self._rows]
        key = drag["key"]
        keys.remove(key)
        keys.insert(max(0, min(len(keys), target)), key)
        order = {k: i for i, k in enumerate(keys)}
        self._rows.sort(key=lambda pair: order[pair[0]])
        if self._on_reorder is not None:
            try:
                self._on_reorder(self.keys())
            except Exception:
                pass


def _palette_bg(master) -> str:
    p = _theme.palette()
    try:
        return str(master.cget("bg"))
    except Exception:
        return p["panel"]
