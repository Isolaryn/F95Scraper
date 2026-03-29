"""F95zone Database Viewer — PySide6 card-based browser with preview images."""

import sys
import sqlite3
import threading
import webbrowser
from collections import OrderedDict
from pathlib import Path
from urllib.request import urlopen, Request
from io import BytesIO

from PySide6.QtCore import (
    Qt, QAbstractListModel, QModelIndex, QSize, QRect, QRectF, QPoint,
    Signal, QObject, QRunnable, QThreadPool, QTimer, QEvent,
)
from PySide6.QtGui import (
    QImage, QPixmap, QPainter, QColor, QFont, QFontMetrics, QPen,
    QBrush, QPainterPath, QCursor,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListView, QAbstractItemView, QScrollArea, QSlider, QLineEdit,
    QComboBox, QCheckBox, QRadioButton, QButtonGroup, QPushButton,
    QLabel, QFrame, QCompleter, QStyleOptionViewItem, QStyledItemDelegate,
)

DB_PATH = Path(__file__).parent / "f95zone.db"
PAGE_SIZE = 80

DEFAULT_CARD_W = 280
MIN_CARD_W = 180
MAX_CARD_W = 400
CARD_PAD = 8

# ── Colours ──────────────────────────────────────────────────────────────────

BG       = "#1e1e2e"
SIDEBAR  = "#1a1a2a"
CARD_BG  = "#2a2a3e"
CARD_HI  = "#363654"
TEXT     = "#e0e0ec"
TEXT_DIM = "#8888a8"
ACCENT   = "#7c6cf0"
STAR     = "#f0c040"
BORDER   = "#3a3a52"
ENTRY_BG = "#2e2e44"
BADGE_ENGINE = "#2e6ea6"
BADGE_STATUS = "#a63e5c"
BADGE_OTHER  = "#6e5ea6"
CHIP_BG  = "#3a3a5e"
THUMB_BG = QColor(30, 30, 46)


# ── Database helpers ─────────────────────────────────────────────────────────

def connect_db():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)


def dict_row(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def fetch_all_tags(db):
    return [r["name"] for r in db.execute("SELECT name FROM tags ORDER BY name")]


def fetch_prefixes_by_group(db):
    rows = db.execute(
        "SELECT p.id, p.name, p.group_name, COUNT(ip.thread_id) AS cnt "
        "FROM prefixes p LEFT JOIN item_prefixes ip ON ip.prefix_id = p.id "
        "GROUP BY p.id HAVING cnt > 0 ORDER BY cnt DESC"
    ).fetchall()
    grouped: dict[str, list] = {}
    for r in rows:
        g = r["group_name"] or "Other"
        grouped.setdefault(g, []).append((r["id"], r["name"], r["cnt"]))
    return grouped


def build_query(filters, count_only=False):
    sel = "COUNT(*)" if count_only else (
        "i.thread_id, i.title, i.creator, i.version, "
        "i.rating, i.views, i.likes, i.cover_url, i.date_text"
    )
    sql = f"SELECT {sel} FROM items i"
    joins, wheres, params = [], [], []

    pids = filters.get("prefix_ids")
    if pids:
        ph = ",".join("?" * len(pids))
        joins.append(
            f"JOIN item_prefixes ip ON ip.thread_id = i.thread_id "
            f"AND ip.prefix_id IN ({ph})"
        )
        params.extend(pids)

    tags = filters.get("tags")
    if tags:
        ph = ",".join("?" * len(tags))
        if filters.get("tag_mode", "AND") == "AND":
            joins.append(
                f"JOIN (SELECT thread_id FROM item_tags "
                f"JOIN tags ON tags.id = item_tags.tag_id "
                f"WHERE tags.name IN ({ph}) "
                f"GROUP BY thread_id HAVING COUNT(DISTINCT tags.name) = ?"
                f") tf ON tf.thread_id = i.thread_id"
            )
            params.extend(tags)
            params.append(len(tags))
        else:
            joins.append(
                f"JOIN (SELECT DISTINCT thread_id FROM item_tags "
                f"JOIN tags ON tags.id = item_tags.tag_id "
                f"WHERE tags.name IN ({ph})"
                f") tf ON tf.thread_id = i.thread_id"
            )
            params.extend(tags)

    if joins:
        sql += " " + " ".join(joins)

    for field, key in [("i.title", "title"), ("i.creator", "creator")]:
        val = filters.get(key, "").strip()
        if val:
            wheres.append(f"{field} LIKE ?")
            params.append(f"%{val}%")

    for col, key, op in [
        ("i.rating", "rating_min", ">="), ("i.rating", "rating_max", "<="),
        ("i.views", "views_min", ">="),   ("i.views", "views_max", "<="),
    ]:
        val = filters.get(key)
        if val is not None:
            wheres.append(f"{col} {op} ?")
            params.append(val)

    if wheres:
        sql += " WHERE " + " AND ".join(wheres)

    if not count_only:
        sort_col = {
            "Date": "i.timestamp", "Rating": "i.rating", "Views": "i.views",
            "Likes": "i.likes", "Title": "i.title",
        }.get(filters.get("sort", "Date"), "i.timestamp")
        order = "ASC" if filters.get("order") == "Ascending" else "DESC"
        sql += f" ORDER BY {sort_col} {order} LIMIT ? OFFSET ?"

    return sql, params


# ── Async image loader ───────────────────────────────────────────────────────

class _ImageSignals(QObject):
    loaded = Signal(str, QImage)


class ImageLoadRunnable(QRunnable):
    def __init__(self, url: str, signals: _ImageSignals):
        super().__init__()
        self.url = url
        self.signals = signals
        self.setAutoDelete(True)

    def run(self):
        try:
            req = Request(self.url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=10) as resp:
                data = resp.read()
            img = QImage()
            img.loadFromData(data)
            if not img.isNull():
                self.signals.loaded.emit(self.url, img)
        except Exception:
            pass


class ImageCache:
    def __init__(self, max_entries=500, max_workers=10):
        self._raw: OrderedDict[str, QImage] = OrderedDict()
        self._pixmap_cache: dict[str, QPixmap] = {}
        self._max = max_entries
        self._loading: set[str] = set()
        self._lock = threading.Lock()
        self._pool = QThreadPool.globalInstance()
        self._pool.setMaxThreadCount(max_workers)
        self._signals = _ImageSignals()
        self._on_loaded_callback = None
        self._signals.loaded.connect(self._on_loaded)
        self._current_size: tuple[int, int] = (0, 0)

    def set_callback(self, cb):
        self._on_loaded_callback = cb

    def get_pixmap(self, url: str, width: int, height: int) -> QPixmap | None:
        if not url:
            return None
        size_key = f"{url}:{width}x{height}"
        if size_key in self._pixmap_cache:
            with self._lock:
                if url in self._raw:
                    self._raw.move_to_end(url)
            return self._pixmap_cache[size_key]
        with self._lock:
            if url in self._raw:
                self._raw.move_to_end(url)
                raw = self._raw[url]
                pm = self._make_pixmap(raw, width, height)
                self._pixmap_cache[size_key] = pm
                return pm
            if url not in self._loading:
                self._loading.add(url)
                runner = ImageLoadRunnable(url, self._signals)
                self._pool.start(runner)
        return None

    def _make_pixmap(self, img: QImage, w: int, h: int) -> QPixmap:
        scaled = img.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        canvas = QImage(w, h, QImage.Format_RGB32)
        canvas.fill(THUMB_BG)
        painter = QPainter(canvas)
        x = (w - scaled.width()) // 2
        y = (h - scaled.height()) // 2
        painter.drawImage(x, y, scaled)
        painter.end()
        return QPixmap.fromImage(canvas)

    def _on_loaded(self, url: str, img: QImage):
        with self._lock:
            self._raw[url] = img
            self._loading.discard(url)
            if len(self._raw) > self._max:
                evicted_url, _ = self._raw.popitem(last=False)
                keys_to_remove = [k for k in self._pixmap_cache if k.startswith(evicted_url + ":")]
                for k in keys_to_remove:
                    del self._pixmap_cache[k]
        if self._on_loaded_callback:
            self._on_loaded_callback(url)

    def invalidate_sized(self):
        self._pixmap_cache.clear()

    def shutdown(self):
        self._pool.waitForDone(1000)


# ── Card model ───────────────────────────────────────────────────────────────

class CardModel(QAbstractListModel):
    ThreadIdRole = Qt.UserRole + 1
    TitleRole    = Qt.UserRole + 2
    CreatorRole  = Qt.UserRole + 3
    RatingRole   = Qt.UserRole + 4
    ViewsRole    = Qt.UserRole + 5
    CoverUrlRole = Qt.UserRole + 6
    VersionRole  = Qt.UserRole + 7
    DateTextRole = Qt.UserRole + 8
    LikesRole    = Qt.UserRole + 9

    _ROLE_MAP = {
        ThreadIdRole: "thread_id", TitleRole: "title", CreatorRole: "creator",
        RatingRole: "rating", ViewsRole: "views", CoverUrlRole: "cover_url",
        VersionRole: "version", DateTextRole: "date_text", LikesRole: "likes",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[dict] = []

    def rowCount(self, parent=QModelIndex()):
        return len(self._items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._items[index.row()]
        key = self._ROLE_MAP.get(role)
        if key:
            return row.get(key)
        if role == Qt.DisplayRole:
            return row.get("title", "")
        return None

    def reset_data(self, rows: list[dict]):
        self.beginResetModel()
        self._items = list(rows)
        self.endResetModel()

    def append_page(self, rows: list[dict]):
        if not rows:
            return
        start = len(self._items)
        self.beginInsertRows(QModelIndex(), start, start + len(rows) - 1)
        self._items.extend(rows)
        self.endInsertRows()

    def get_row(self, index: int) -> dict | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None


# ── Card delegate ────────────────────────────────────────────────────────────

class CardDelegate(QStyledItemDelegate):
    def __init__(self, image_cache: ImageCache, parent=None):
        super().__init__(parent)
        self._cache = image_cache
        self._card_w = DEFAULT_CARD_W
        self._hovered_index: QModelIndex | None = None

        self._title_font = QFont("Segoe UI", 9)
        self._title_font.setBold(True)
        self._creator_font = QFont("Segoe UI", 8)
        self._rating_font = QFont("Segoe UI", 8)
        self._rating_font.setBold(True)
        self._views_font = QFont("Segoe UI", 8)

    @property
    def _thumb_w(self):
        return self._card_w - 16

    @property
    def _thumb_h(self):
        return int(self._thumb_w * 0.67)

    @property
    def _card_h(self):
        return self._thumb_h + 135

    def set_card_width(self, w: int):
        self._card_w = w

    def set_hovered(self, index: QModelIndex | None):
        self._hovered_index = index

    def sizeHint(self, option, index):
        return QSize(self._card_w, self._card_h)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)

        rect = option.rect
        is_hovered = (self._hovered_index is not None
                      and self._hovered_index.isValid()
                      and self._hovered_index.row() == index.row())

        # Card background
        bg = QColor(CARD_HI if is_hovered else CARD_BG)
        path = QPainterPath()
        path.addRoundedRect(QRectF(rect), 6, 6)
        painter.fillPath(path, bg)

        # Border
        painter.setPen(QPen(QColor(ACCENT if is_hovered else BORDER), 1))
        painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)

        x0, y0 = rect.x(), rect.y()
        tw, th = self._thumb_w, self._thumb_h

        # Thumbnail
        thumb_rect = QRect(x0 + 8, y0 + 8, tw, th)
        url = index.data(CardModel.CoverUrlRole)
        pm = self._cache.get_pixmap(url, tw, th) if url else None
        if pm:
            painter.drawPixmap(thumb_rect, pm)
        else:
            painter.fillRect(thumb_rect, THUMB_BG)

        # Title
        title = index.data(CardModel.TitleRole) or ""
        painter.setFont(self._title_font)
        painter.setPen(QColor(TEXT))
        title_rect = QRect(x0 + 8, y0 + th + 14, self._card_w - 16, 40)
        fm = QFontMetrics(self._title_font)
        elided = fm.elidedText(title, Qt.ElideRight, title_rect.width())
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignTop, elided)

        # Creator
        creator = index.data(CardModel.CreatorRole) or "Unknown"
        painter.setFont(self._creator_font)
        painter.setPen(QColor(TEXT_DIM))
        creator_rect = QRect(x0 + 8, y0 + th + 34, self._card_w - 16, 20)
        fm2 = QFontMetrics(self._creator_font)
        elided_c = fm2.elidedText(creator, Qt.ElideRight, creator_rect.width())
        painter.drawText(creator_rect, Qt.AlignLeft | Qt.AlignTop, elided_c)

        # Bottom row: rating (left) and views (right)
        bottom_y = y0 + self._card_h - 24

        rating = index.data(CardModel.RatingRole)
        if rating is not None:
            painter.setFont(self._rating_font)
            painter.setPen(QColor(STAR))
            painter.drawText(QRect(x0 + 8, bottom_y, 80, 18),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             f"\u2605 {rating:.1f}")

        views = index.data(CardModel.ViewsRole)
        if views is not None:
            vstr = (f"{views / 1e6:.1f}M" if views >= 1_000_000
                    else f"{views / 1e3:.0f}K" if views >= 1_000
                    else str(views))
            painter.setFont(self._views_font)
            painter.setPen(QColor(TEXT_DIM))
            painter.drawText(QRect(x0 + self._card_w - 70, bottom_y, 62, 18),
                             Qt.AlignRight | Qt.AlignVCenter, vstr)

        painter.restore()


# ── Tooltip popup ────────────────────────────────────────────────────────────

class TooltipPopup(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setStyleSheet(f"""
            TooltipPopup {{
                background-color: {CARD_BG};
                border: 1px solid {BORDER};
                border-radius: 6px;
            }}
            QLabel {{ background: transparent; }}
        """)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 10, 14, 10)
        self._layout.setSpacing(4)
        self._tooltip_cache: dict[int, dict] = {}

    def show_for(self, data: dict, db, global_pos: QPoint):
        self._clear()
        tid = data["thread_id"]

        # Title
        title_lbl = QLabel(data.get("title", ""))
        title_lbl.setFont(QFont("Segoe UI", 11, QFont.Bold))
        title_lbl.setStyleSheet(f"color: {TEXT};")
        title_lbl.setWordWrap(True)
        title_lbl.setMaximumWidth(320)
        self._layout.addWidget(title_lbl)

        # Subtitle
        sub = f"by {data.get('creator') or 'Unknown'}"
        if data.get("version"):
            sub += f"  \u2022  {data['version']}"
        sub_lbl = QLabel(sub)
        sub_lbl.setFont(QFont("Segoe UI", 9))
        sub_lbl.setStyleSheet(f"color: {TEXT_DIM};")
        self._layout.addWidget(sub_lbl)

        # Prefixes (cached)
        cached = self._tooltip_cache.get(tid)
        if cached is None:
            cached = self._fetch_tooltip_data(tid, db)
            self._tooltip_cache[tid] = cached
            if len(self._tooltip_cache) > 200:
                oldest = next(iter(self._tooltip_cache))
                del self._tooltip_cache[oldest]

        if cached["prefixes"]:
            badge_row = QWidget()
            badge_layout = QHBoxLayout(badge_row)
            badge_layout.setContentsMargins(0, 4, 0, 4)
            badge_layout.setSpacing(4)
            for pname, pgroup in cached["prefixes"]:
                c = {"Engine": BADGE_ENGINE, "Status": BADGE_STATUS}.get(pgroup, BADGE_OTHER)
                badge = QLabel(pname)
                badge.setFont(QFont("Segoe UI", 8))
                badge.setStyleSheet(
                    f"color: white; background-color: {c}; "
                    f"padding: 1px 6px; border-radius: 3px;"
                )
                badge_layout.addWidget(badge)
            badge_layout.addStretch()
            self._layout.addWidget(badge_row)

        # Stats
        parts = []
        if data.get("rating") is not None:
            stars = "\u2605" * round(data["rating"]) + "\u2606" * (5 - round(data["rating"]))
            parts.append(f"{stars}  {data['rating']:.2f}")
        if data.get("views") is not None:
            parts.append(f"{data['views']:,} views")
        if data.get("likes") is not None:
            parts.append(f"{data['likes']:,} likes")
        if parts:
            stats = QLabel("   \u2022   ".join(parts))
            stats.setFont(QFont("Segoe UI", 9))
            stats.setStyleSheet(f"color: {TEXT};")
            self._layout.addWidget(stats)

        # Date
        if data.get("date_text"):
            dt = QLabel(f"Updated {data['date_text']} ago")
            dt.setFont(QFont("Segoe UI", 8))
            dt.setStyleSheet(f"color: {TEXT_DIM};")
            self._layout.addWidget(dt)

        # Tags
        if cached["tags"]:
            tag_str = ", ".join(cached["tags"])
            tag_lbl = QLabel(tag_str)
            tag_lbl.setFont(QFont("Segoe UI", 8))
            tag_lbl.setStyleSheet(f"color: {TEXT_DIM};")
            tag_lbl.setWordWrap(True)
            tag_lbl.setMaximumWidth(320)
            self._layout.addWidget(tag_lbl)

        self.adjustSize()

        # Position near cursor, clamped to screen
        screen = QApplication.primaryScreen().availableGeometry()
        x = global_pos.x() + 18
        y = global_pos.y() + 12
        if x + self.width() > screen.right():
            x = global_pos.x() - self.width() - 18
        if y + self.height() > screen.bottom():
            y = global_pos.y() - self.height() - 12
        self.move(x, y)
        self.show()

    def _fetch_tooltip_data(self, tid: int, db) -> dict:
        prefixes = [(r["name"], r["group_name"]) for r in db.execute(
            "SELECT p.name, p.group_name FROM item_prefixes ip "
            "JOIN prefixes p ON p.id = ip.prefix_id WHERE ip.thread_id = ?",
            (tid,),
        ).fetchall()]
        tags = [r["name"] for r in db.execute(
            "SELECT t.name FROM item_tags it JOIN tags t ON t.id = it.tag_id "
            "WHERE it.thread_id = ? ORDER BY t.name", (tid,),
        ).fetchall()]
        return {"prefixes": prefixes, "tags": tags}

    def _clear(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def hide_tooltip(self):
        self.hide()


# ── Filter sidebar ───────────────────────────────────────────────────────────

class FilterSidebar(QScrollArea):
    filtersApplied = Signal()
    filtersReset = Signal()
    tileSizeChanged = Signal(int)

    def __init__(self, all_tags: list[str], prefix_groups: dict[str, list], parent=None):
        super().__init__(parent)
        self.setFixedWidth(250)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)

        self.all_tags = all_tags
        self.selected_tags: list[str] = []
        self.prefix_vars: dict[int, QCheckBox] = {}

        inner = QWidget()
        self._layout = QVBoxLayout(inner)
        self._layout.setContentsMargins(12, 8, 12, 8)
        self._layout.setSpacing(2)
        self.setWidget(inner)

        self._build(prefix_groups)

    def _heading(self, text: str):
        lbl = QLabel(text)
        lbl.setFont(QFont("Segoe UI", 9, QFont.Bold))
        lbl.setStyleSheet(f"color: {ACCENT}; padding-top: 8px;")
        self._layout.addWidget(lbl)

    def _sep(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {BORDER};")
        self._layout.addWidget(line)

    def _build(self, prefix_groups):
        # Tile size
        self._heading("TILE SIZE")
        self.tile_slider = QSlider(Qt.Horizontal)
        self.tile_slider.setRange(MIN_CARD_W, MAX_CARD_W)
        self.tile_slider.setValue(DEFAULT_CARD_W)
        self.tile_slider.valueChanged.connect(self.tileSizeChanged.emit)
        self._layout.addWidget(self.tile_slider)
        self._sep()

        # Search
        self._heading("SEARCH")
        lbl_title = QLabel("Title")
        lbl_title.setFont(QFont("Segoe UI", 8))
        lbl_title.setStyleSheet(f"color: {TEXT_DIM};")
        self._layout.addWidget(lbl_title)
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Search title...")
        self._layout.addWidget(self.title_edit)

        lbl_creator = QLabel("Creator")
        lbl_creator.setFont(QFont("Segoe UI", 8))
        lbl_creator.setStyleSheet(f"color: {TEXT_DIM};")
        self._layout.addWidget(lbl_creator)
        self.creator_edit = QLineEdit()
        self.creator_edit.setPlaceholderText("Search creator...")
        self._layout.addWidget(self.creator_edit)
        self._sep()

        # Sort
        self._heading("SORT BY")
        sort_row = QWidget()
        sort_layout = QHBoxLayout(sort_row)
        sort_layout.setContentsMargins(0, 0, 0, 0)
        sort_layout.setSpacing(6)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Date", "Rating", "Views", "Likes", "Title"])
        sort_layout.addWidget(self.sort_combo)
        self.order_combo = QComboBox()
        self.order_combo.addItems(["Descending", "Ascending"])
        sort_layout.addWidget(self.order_combo)
        self._layout.addWidget(sort_row)
        self._sep()

        # Prefix groups
        for group in ("Engine", "Status", "Other"):
            items = prefix_groups.get(group, [])
            if not items:
                continue
            self._heading(group.upper())
            for pid, pname, cnt in items:
                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(0, 0, 0, 0)
                row_layout.setSpacing(4)
                cb = QCheckBox(pname)
                self.prefix_vars[pid] = cb
                row_layout.addWidget(cb)
                row_layout.addStretch()
                cnt_lbl = QLabel(f"{cnt:,}")
                cnt_lbl.setFont(QFont("Segoe UI", 8))
                cnt_lbl.setStyleSheet(f"color: {TEXT_DIM};")
                row_layout.addWidget(cnt_lbl)
                self._layout.addWidget(row)
            self._sep()

        # Tags
        self._heading("TAGS")
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("Type tag name...")
        self._completer = QCompleter(self.all_tags)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchContains)
        self._completer.setMaxVisibleItems(10)
        self.tag_edit.setCompleter(self._completer)
        self._completer.activated.connect(self._pick_tag)
        self.tag_edit.returnPressed.connect(self._on_tag_return)
        self._layout.addWidget(self.tag_edit)

        # Tag mode
        mode_row = QWidget()
        mode_layout = QHBoxLayout(mode_row)
        mode_layout.setContentsMargins(0, 0, 0, 0)
        mode_layout.setSpacing(8)
        self.tag_mode_group = QButtonGroup(self)
        self.tag_mode_and = QRadioButton("All (AND)")
        self.tag_mode_or = QRadioButton("Any (OR)")
        self.tag_mode_and.setChecked(True)
        self.tag_mode_group.addButton(self.tag_mode_and, 0)
        self.tag_mode_group.addButton(self.tag_mode_or, 1)
        mode_layout.addWidget(self.tag_mode_and)
        mode_layout.addWidget(self.tag_mode_or)
        mode_layout.addStretch()
        self._layout.addWidget(mode_row)

        # Tag chips container
        self.chips_widget = QWidget()
        self.chips_layout = QVBoxLayout(self.chips_widget)
        self.chips_layout.setContentsMargins(0, 0, 0, 0)
        self.chips_layout.setSpacing(2)
        self._layout.addWidget(self.chips_widget)
        self._sep()

        # Rating
        self._heading("RATING")
        rating_row = QWidget()
        rating_layout = QHBoxLayout(rating_row)
        rating_layout.setContentsMargins(0, 0, 0, 0)
        rating_layout.setSpacing(4)
        self.rating_min = QLineEdit()
        self.rating_min.setPlaceholderText("Min")
        self.rating_min.setFixedWidth(70)
        rating_layout.addWidget(self.rating_min)
        dash = QLabel("\u2014")
        dash.setStyleSheet(f"color: {TEXT_DIM};")
        rating_layout.addWidget(dash)
        self.rating_max = QLineEdit()
        self.rating_max.setPlaceholderText("Max")
        self.rating_max.setFixedWidth(70)
        rating_layout.addWidget(self.rating_max)
        rating_layout.addStretch()
        self._layout.addWidget(rating_row)

        # Views
        self._heading("VIEWS")
        views_row = QWidget()
        views_layout = QHBoxLayout(views_row)
        views_layout.setContentsMargins(0, 0, 0, 0)
        views_layout.setSpacing(4)
        self.views_min = QLineEdit()
        self.views_min.setPlaceholderText("Min")
        self.views_min.setFixedWidth(70)
        views_layout.addWidget(self.views_min)
        dash2 = QLabel("\u2014")
        dash2.setStyleSheet(f"color: {TEXT_DIM};")
        views_layout.addWidget(dash2)
        self.views_max = QLineEdit()
        self.views_max.setPlaceholderText("Max")
        self.views_max.setFixedWidth(70)
        views_layout.addWidget(self.views_max)
        views_layout.addStretch()
        self._layout.addWidget(views_row)
        self._sep()

        # Buttons
        self.apply_btn = QPushButton("Apply Filters")
        self.apply_btn.setObjectName("applyBtn")
        self.apply_btn.setCursor(Qt.PointingHandCursor)
        self.apply_btn.clicked.connect(self.filtersApplied.emit)
        self._layout.addWidget(self.apply_btn)

        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setObjectName("resetBtn")
        self.reset_btn.setCursor(Qt.PointingHandCursor)
        self.reset_btn.clicked.connect(self._do_reset)
        self._layout.addWidget(self.reset_btn)

        self.count_label = QLabel("")
        self.count_label.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.count_label.setStyleSheet(f"color: {TEXT};")
        self.count_label.setAlignment(Qt.AlignCenter)
        self._layout.addWidget(self.count_label)

        self._layout.addStretch()

    def _pick_tag(self, name: str):
        if name and name not in self.selected_tags:
            self.selected_tags.append(name)
            self._refresh_chips()
        QTimer.singleShot(0, lambda: self.tag_edit.clear())

    def _on_tag_return(self):
        text = self.tag_edit.text().strip().lower()
        match = next((t for t in self.all_tags
                      if t.lower() == text and t not in self.selected_tags), None)
        if match:
            self._pick_tag(match)

    def _remove_tag(self, name: str):
        if name in self.selected_tags:
            self.selected_tags.remove(name)
            self._refresh_chips()

    def _refresh_chips(self):
        while self.chips_layout.count():
            item = self.chips_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for t in self.selected_tags:
            chip = QWidget()
            chip.setStyleSheet(f"background-color: {CHIP_BG}; border-radius: 3px;")
            chip_layout = QHBoxLayout(chip)
            chip_layout.setContentsMargins(6, 2, 6, 2)
            chip_layout.setSpacing(4)
            lbl = QLabel(t)
            lbl.setFont(QFont("Segoe UI", 8))
            lbl.setStyleSheet(f"color: {TEXT}; background: transparent;")
            chip_layout.addWidget(lbl)
            chip_layout.addStretch()
            close_btn = QPushButton("\u2715")
            close_btn.setFixedSize(16, 16)
            close_btn.setCursor(Qt.PointingHandCursor)
            close_btn.setStyleSheet(
                f"color: {TEXT_DIM}; background: transparent; border: none; "
                f"font-size: 10px; font-weight: bold;"
            )
            close_btn.clicked.connect(lambda checked=False, n=t: self._remove_tag(n))
            chip_layout.addWidget(close_btn)
            self.chips_layout.addWidget(chip)

    def gather_filters(self) -> dict:
        f: dict = {
            "title": self.title_edit.text(),
            "creator": self.creator_edit.text(),
            "sort": self.sort_combo.currentText(),
            "order": self.order_combo.currentText(),
        }
        if self.selected_tags:
            f["tags"] = list(self.selected_tags)
            f["tag_mode"] = "AND" if self.tag_mode_and.isChecked() else "OR"

        pids = [pid for pid, cb in self.prefix_vars.items() if cb.isChecked()]
        if pids:
            f["prefix_ids"] = pids

        for attr, widget in [("rating_min", self.rating_min), ("rating_max", self.rating_max),
                             ("views_min", self.views_min), ("views_max", self.views_max)]:
            val = widget.text().strip()
            if val:
                try:
                    f[attr] = float(val) if "rating" in attr else int(val)
                except ValueError:
                    pass
        return f

    def _do_reset(self):
        self.title_edit.clear()
        self.creator_edit.clear()
        self.sort_combo.setCurrentIndex(0)
        self.order_combo.setCurrentIndex(0)
        self.rating_min.clear()
        self.rating_max.clear()
        self.views_min.clear()
        self.views_max.clear()
        self.selected_tags.clear()
        self._refresh_chips()
        for cb in self.prefix_vars.values():
            cb.setChecked(False)
        self.tag_mode_and.setChecked(True)
        self.filtersReset.emit()


# ── QSS Theme ────────────────────────────────────────────────────────────────

DARK_THEME_QSS = f"""
QMainWindow, QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-family: "Segoe UI";
}}
QScrollArea {{
    background-color: {SIDEBAR};
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background-color: {SIDEBAR};
}}
QLineEdit {{
    background-color: {ENTRY_BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}
QComboBox {{
    background-color: {ENTRY_BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 12px;
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {TEXT_DIM};
    margin-right: 6px;
}}
QComboBox QAbstractItemView {{
    background-color: {CARD_BG};
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: white;
    border: 1px solid {BORDER};
}}
QCheckBox {{
    color: {TEXT};
    spacing: 6px;
    font-size: 12px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background-color: {ENTRY_BG};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}
QRadioButton {{
    color: {TEXT_DIM};
    font-size: 11px;
    spacing: 4px;
}}
QRadioButton::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {BORDER};
    border-radius: 7px;
    background-color: {ENTRY_BG};
}}
QRadioButton::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}
QPushButton#applyBtn {{
    background-color: {ACCENT};
    color: white;
    border: none;
    border-radius: 4px;
    padding: 7px 16px;
    font-weight: bold;
    font-size: 12px;
}}
QPushButton#applyBtn:hover {{
    background-color: #6a5ce0;
}}
QPushButton#applyBtn:pressed {{
    background-color: #5a4cd0;
}}
QPushButton#resetBtn {{
    background-color: {ENTRY_BG};
    color: {TEXT};
    border: none;
    border-radius: 4px;
    padding: 6px 16px;
    font-size: 12px;
}}
QPushButton#resetBtn:hover {{
    background-color: {CARD_HI};
}}
QSlider::groove:horizontal {{
    background: {ENTRY_BG};
    height: 6px;
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {ACCENT};
    width: 16px;
    height: 16px;
    margin: -5px 0;
    border-radius: 8px;
}}
QSlider::handle:horizontal:hover {{
    background: #6a5ce0;
}}
QScrollBar:vertical {{
    background: {BG};
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {TEXT_DIM};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}
QListView {{
    background-color: {BG};
    border: none;
    outline: none;
}}
QCompleter QAbstractItemView {{
    background-color: {ENTRY_BG};
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: white;
    border: 1px solid {BORDER};
    font-size: 12px;
}}
"""


# ── Application ──────────────────────────────────────────────────────────────

class ViewerApp(QMainWindow):
    def __init__(self):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyleSheet(DARK_THEME_QSS)
        super().__init__()
        self.setWindowTitle("F95zone Browser")
        self.resize(1600, 950)
        self.setMinimumSize(900, 600)

        self.db = connect_db()
        self.db.row_factory = dict_row
        self.total_count = self.db.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
        self.all_tags = fetch_all_tags(self.db)
        self.prefix_groups = fetch_prefixes_by_group(self.db)

        self.loaded_rows = 0
        self.result_count = 0
        self._current_filters: dict = {}

        self._image_cache = ImageCache()
        self._image_cache.set_callback(self._on_image_loaded)

        self._model = CardModel(self)
        self._delegate = CardDelegate(self._image_cache, self)
        self._tooltip = TooltipPopup()
        self._tooltip_timer = QTimer(self)
        self._tooltip_timer.setSingleShot(True)
        self._tooltip_timer.setInterval(150)
        self._tooltip_timer.timeout.connect(self._show_pending_tooltip)
        self._pending_tooltip_index: QModelIndex | None = None

        self._build_ui()
        self._apply_filters()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Sidebar
        self._sidebar = FilterSidebar(self.all_tags, self.prefix_groups)
        self._sidebar.filtersApplied.connect(self._apply_filters)
        self._sidebar.filtersReset.connect(self._apply_filters)
        self._sidebar.tileSizeChanged.connect(self._on_tile_resize)
        layout.addWidget(self._sidebar)

        # Card list view
        self._list_view = QListView()
        self._list_view.setModel(self._model)
        self._list_view.setItemDelegate(self._delegate)
        self._list_view.setViewMode(QListView.IconMode)
        self._list_view.setWrapping(True)
        self._list_view.setResizeMode(QListView.Adjust)
        self._list_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._list_view.setSpacing(CARD_PAD)
        self._list_view.setUniformItemSizes(True)
        self._list_view.setSelectionMode(QAbstractItemView.NoSelection)
        self._list_view.setMouseTracking(True)
        self._list_view.setGridSize(
            QSize(DEFAULT_CARD_W + CARD_PAD, self._delegate._card_h + CARD_PAD))

        self._list_view.entered.connect(self._on_card_entered)
        self._list_view.clicked.connect(self._on_card_clicked)
        self._list_view.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._list_view.viewport().installEventFilter(self)
        layout.addWidget(self._list_view)

    def eventFilter(self, obj, event):
        if obj is self._list_view.viewport() and event.type() == QEvent.Leave:
            self._tooltip.hide_tooltip()
            self._tooltip_timer.stop()
            self._pending_tooltip_index = None
            if self._delegate._hovered_index is not None:
                old = self._delegate._hovered_index
                self._delegate.set_hovered(None)
                self._list_view.viewport().update()
        return super().eventFilter(obj, event)

    def _on_card_entered(self, index: QModelIndex):
        old = self._delegate._hovered_index
        self._delegate.set_hovered(index)
        self._list_view.viewport().update()

        self._pending_tooltip_index = index
        self._tooltip_timer.start()

    def _show_pending_tooltip(self):
        index = self._pending_tooltip_index
        if index is None or not index.isValid():
            return
        data = self._model.get_row(index.row())
        if data:
            self._tooltip.show_for(data, self.db, QCursor.pos())

    def _on_card_clicked(self, index: QModelIndex):
        data = self._model.get_row(index.row())
        if data:
            webbrowser.open(f"https://f95zone.to/threads/{data['thread_id']}/")

    def _on_scroll(self, value):
        sb = self._list_view.verticalScrollBar()
        if sb.maximum() > 0 and self.loaded_rows < self.result_count:
            ratio = value / sb.maximum()
            if ratio > 0.85:
                self._load_page()

    def _on_tile_resize(self, value: int):
        self._delegate.set_card_width(value)
        self._image_cache.invalidate_sized()
        self._list_view.setGridSize(
            QSize(value + CARD_PAD, self._delegate._card_h + CARD_PAD))
        self._list_view.viewport().update()

    def _apply_filters(self):
        filters = self._sidebar.gather_filters()
        csql, cparams = build_query(filters, count_only=True)
        self.result_count = self.db.execute(csql, cparams).fetchone()["COUNT(*)"]

        self.loaded_rows = 0
        self._current_filters = filters

        sql, params = build_query(filters)
        params.extend([PAGE_SIZE, 0])
        rows = self.db.execute(sql, params).fetchall()
        self._model.reset_data(rows)
        self.loaded_rows = len(rows)

        self._sidebar.count_label.setText(f"{self.result_count:,}  /  {self.total_count:,}")
        self._list_view.scrollToTop()

    def _load_page(self):
        if self.loaded_rows >= self.result_count:
            return
        sql, params = build_query(self._current_filters)
        params.extend([PAGE_SIZE, self.loaded_rows])
        rows = self.db.execute(sql, params).fetchall()
        self._model.append_page(rows)
        self.loaded_rows += len(rows)

    def _on_image_loaded(self, url: str):
        # Only repaint the visible viewport — Qt will re-call paint() for
        # any visible card that uses this URL.
        self._list_view.viewport().update()

    def closeEvent(self, event):
        self._tooltip.hide_tooltip()
        self._image_cache.shutdown()
        self.db.close()
        super().closeEvent(event)

    def mainloop(self):
        self.show()
        self._app.exec()


if __name__ == "__main__":
    ViewerApp().mainloop()
