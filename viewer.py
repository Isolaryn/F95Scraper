"""F95zone Database Viewer — PySide6 card-based browser with preview images."""

import json
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
    Signal, QObject, QRunnable, QThreadPool, QTimer, QEvent, QStringListModel,
)
from PySide6.QtGui import (
    QImage, QPixmap, QPainter, QColor, QFont, QFontMetrics, QPen,
    QBrush, QPainterPath, QCursor, QAction,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QListView, QAbstractItemView, QScrollArea, QSlider, QLineEdit,
    QComboBox, QCheckBox, QRadioButton, QButtonGroup, QPushButton,
    QLabel, QFrame, QCompleter, QStyleOptionViewItem, QStyledItemDelegate,
    QMenu, QInputDialog,
)

DB_PATH = Path(__file__).parent / "f95zone.db"
USER_DB_PATH = Path(__file__).parent / "user_data.db"
SETTINGS_PATH = Path(__file__).parent / "settings.json"
PAGE_SIZE = 80

DEFAULT_CARD_W = 280
MIN_CARD_W = 180
MAX_CARD_W = 600
CARD_PAD = 8

SORT_OPTIONS = ["Date", "Rating", "Views", "Likes", "Title", "My Rating"]
SORT2_OPTIONS = ["None"] + SORT_OPTIONS
STATUS_OPTIONS = ["played", "playing", "dropped", "wishlist"]
STATUS_COLORS = {
    "played": "#4caf50", "playing": "#42a5f5",
    "dropped": "#ef5350", "wishlist": "#fdd835",
}

# ── Colours ──────────────────────────────────────────────────────────────────

BG       = "#1e1e2e"
SIDEBAR  = "#1a1a2a"
CARD_BG  = "#2a2a3e"
CARD_HI  = "#363654"
TEXT     = "#e0e0ec"
TEXT_DIM = "#8888a8"
ACCENT   = "#7c6cf0"
STAR     = "#f0c040"
MY_HEART = "#e06090"
BORDER   = "#3a3a52"
ENTRY_BG = "#2e2e44"
BADGE_ENGINE = "#2e6ea6"
BADGE_STATUS = "#a63e5c"
BADGE_OTHER  = "#6e5ea6"
CHIP_BG  = "#3a3a5e"
THUMB_BG = QColor(30, 30, 46)


# ── Database helpers ─────────────────────────────────────────────────────────

def init_user_db():
    """Create user_data.db with personal marks tables if it doesn't exist."""
    db = sqlite3.connect(str(USER_DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS user_marks (
            thread_id INTEGER PRIMARY KEY,
            status    TEXT,
            my_rating INTEGER,
            notes     TEXT
        );
        CREATE TABLE IF NOT EXISTS user_tags (
            thread_id INTEGER NOT NULL,
            tag       TEXT NOT NULL,
            PRIMARY KEY (thread_id, tag)
        );
    """)
    db.commit()
    db.close()


def connect_db():
    """Open user_data.db read-write, attach f95zone.db as 'f95' read-only."""
    init_user_db()
    db = sqlite3.connect(str(USER_DB_PATH))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(f"ATTACH DATABASE ? AS f95", (str(DB_PATH),))
    return db


def dict_row(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


def fetch_all_tags(db):
    return [r["name"] for r in db.execute("SELECT name FROM f95.tags ORDER BY name")]


def fetch_all_user_tags(db):
    return [r["tag"] for r in db.execute("SELECT DISTINCT tag FROM user_tags ORDER BY tag")]


def fetch_prefixes_by_group(db):
    rows = db.execute(
        "SELECT p.id, p.name, p.group_name, COUNT(ip.thread_id) AS cnt "
        "FROM f95.prefixes p LEFT JOIN f95.item_prefixes ip ON ip.prefix_id = p.id "
        "GROUP BY p.id HAVING cnt > 0 ORDER BY cnt DESC"
    ).fetchall()
    grouped: dict[str, list] = {}
    for r in rows:
        g = r["group_name"] or "Other"
        grouped.setdefault(g, []).append((r["id"], r["name"], r["cnt"]))
    return grouped


_SORT_COL_MAP = {
    "Date": "i.timestamp", "Rating": "i.rating", "Views": "i.views",
    "Likes": "i.likes", "Title": "i.title", "My Rating": "um.my_rating",
}


def build_query(filters, count_only=False):
    sel = "COUNT(*)" if count_only else (
        "i.thread_id, i.title, i.creator, i.version, "
        "i.rating, i.views, i.likes, i.cover_url, i.date_text, "
        "um.status, um.my_rating, um.notes"
    )
    sql = f"SELECT {sel} FROM f95.items i"
    # Always LEFT JOIN user_marks
    sql += " LEFT JOIN user_marks um ON um.thread_id = i.thread_id"
    joins, wheres, params = [], [], []

    pids = filters.get("prefix_ids")
    if pids:
        ph = ",".join("?" * len(pids))
        joins.append(
            f"JOIN f95.item_prefixes ip ON ip.thread_id = i.thread_id "
            f"AND ip.prefix_id IN ({ph})"
        )
        params.extend(pids)

    tags = filters.get("tags")
    if tags:
        ph = ",".join("?" * len(tags))
        if filters.get("tag_mode", "AND") == "AND":
            joins.append(
                f"JOIN (SELECT thread_id FROM f95.item_tags "
                f"JOIN f95.tags ON f95.tags.id = f95.item_tags.tag_id "
                f"WHERE f95.tags.name IN ({ph}) "
                f"GROUP BY thread_id HAVING COUNT(DISTINCT f95.tags.name) = ?"
                f") tf ON tf.thread_id = i.thread_id"
            )
            params.extend(tags)
            params.append(len(tags))
        else:
            joins.append(
                f"JOIN (SELECT DISTINCT thread_id FROM f95.item_tags "
                f"JOIN f95.tags ON f95.tags.id = f95.item_tags.tag_id "
                f"WHERE f95.tags.name IN ({ph})"
                f") tf ON tf.thread_id = i.thread_id"
            )
            params.extend(tags)

    # User tag filter
    user_tags = filters.get("user_tags")
    if user_tags:
        ph = ",".join("?" * len(user_tags))
        if filters.get("user_tag_mode", "AND") == "AND":
            joins.append(
                f"JOIN (SELECT thread_id FROM user_tags "
                f"WHERE tag IN ({ph}) "
                f"GROUP BY thread_id HAVING COUNT(DISTINCT tag) = ?"
                f") utf ON utf.thread_id = i.thread_id"
            )
            params.extend(user_tags)
            params.append(len(user_tags))
        else:
            joins.append(
                f"JOIN (SELECT DISTINCT thread_id FROM user_tags "
                f"WHERE tag IN ({ph})"
                f") utf ON utf.thread_id = i.thread_id"
            )
            params.extend(user_tags)

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
        ("um.my_rating", "my_rating_min", ">="), ("um.my_rating", "my_rating_max", "<="),
    ]:
        val = filters.get(key)
        if val is not None:
            wheres.append(f"{col} {op} ?")
            params.append(val)

    # Status filter
    statuses = filters.get("statuses")
    if statuses is not None:
        clauses = []
        real_statuses = [s for s in statuses if s != "unmarked"]
        if real_statuses:
            ph = ",".join("?" * len(real_statuses))
            clauses.append(f"um.status IN ({ph})")
            params.extend(real_statuses)
        if "unmarked" in statuses:
            clauses.append("um.status IS NULL")
        if clauses:
            wheres.append(f"({' OR '.join(clauses)})")

    if wheres:
        sql += " WHERE " + " AND ".join(wheres)

    if not count_only:
        sort_col = _SORT_COL_MAP.get(filters.get("sort", "Date"), "i.timestamp")
        order = "ASC" if filters.get("order") == "Ascending" else "DESC"
        order_clause = f"{sort_col} {order}"

        sort2 = filters.get("sort2")
        if sort2 and sort2 != "None":
            sort2_col = _SORT_COL_MAP.get(sort2)
            if sort2_col and sort2_col != sort_col:
                order_clause += f", {sort2_col} {order}"

        sql += f" ORDER BY {order_clause} LIMIT ? OFFSET ?"

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
    ThreadIdRole  = Qt.UserRole + 1
    TitleRole     = Qt.UserRole + 2
    CreatorRole   = Qt.UserRole + 3
    RatingRole    = Qt.UserRole + 4
    ViewsRole     = Qt.UserRole + 5
    CoverUrlRole  = Qt.UserRole + 6
    VersionRole   = Qt.UserRole + 7
    DateTextRole  = Qt.UserRole + 8
    LikesRole     = Qt.UserRole + 9
    StatusRole    = Qt.UserRole + 10
    MyRatingRole  = Qt.UserRole + 11
    NotesRole     = Qt.UserRole + 12

    _ROLE_MAP = {
        ThreadIdRole: "thread_id", TitleRole: "title", CreatorRole: "creator",
        RatingRole: "rating", ViewsRole: "views", CoverUrlRole: "cover_url",
        VersionRole: "version", DateTextRole: "date_text", LikesRole: "likes",
        StatusRole: "status", MyRatingRole: "my_rating", NotesRole: "notes",
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

    def update_row(self, index: int, updates: dict):
        if 0 <= index < len(self._items):
            self._items[index].update(updates)
            idx = self.index(index)
            self.dataChanged.emit(idx, idx)


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
        self._my_rating_font = QFont("Segoe UI", 8)

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

        # Status indicator dot (top-right corner of card)
        status = index.data(CardModel.StatusRole)
        if status and status in STATUS_COLORS:
            dot_x = x0 + self._card_w - 18
            dot_y = y0 + 12
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(STATUS_COLORS[status]))
            painter.drawEllipse(dot_x, dot_y, 10, 10)

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

        # Bottom row
        bottom_y = y0 + self._card_h - 24

        # Site rating (left)
        rating = index.data(CardModel.RatingRole)
        rating_end_x = x0 + 8
        if rating is not None:
            painter.setFont(self._rating_font)
            painter.setPen(QColor(STAR))
            rating_text = f"\u2605 {rating:.1f}"
            painter.drawText(QRect(x0 + 8, bottom_y, 80, 18),
                             Qt.AlignLeft | Qt.AlignVCenter, rating_text)
            rating_end_x = x0 + 8 + QFontMetrics(self._rating_font).horizontalAdvance(rating_text) + 4

        # My rating (after site rating)
        my_rating = index.data(CardModel.MyRatingRole)
        if my_rating is not None:
            painter.setFont(self._my_rating_font)
            painter.setPen(QColor(MY_HEART))
            sep = "| " if rating is not None else ""
            painter.drawText(QRect(rating_end_x, bottom_y, 60, 18),
                             Qt.AlignLeft | Qt.AlignVCenter,
                             f"{sep}\u2665 {my_rating}")

        # Views (right)
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

        # Prefixes + tags (cached from f95 DB)
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

        # F95 tags
        if cached["tags"]:
            tag_str = ", ".join(cached["tags"])
            tag_lbl = QLabel(tag_str)
            tag_lbl.setFont(QFont("Segoe UI", 8))
            tag_lbl.setStyleSheet(f"color: {TEXT_DIM};")
            tag_lbl.setWordWrap(True)
            tag_lbl.setMaximumWidth(320)
            self._layout.addWidget(tag_lbl)

        # User data section
        has_user_data = data.get("status") or data.get("my_rating") or data.get("notes")
        if has_user_data or cached.get("user_tags"):
            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet(f"color: {BORDER};")
            self._layout.addWidget(sep)

        if data.get("status"):
            color = STATUS_COLORS.get(data["status"], TEXT_DIM)
            status_lbl = QLabel(f"\u25cf {data['status'].capitalize()}")
            status_lbl.setFont(QFont("Segoe UI", 9, QFont.Bold))
            status_lbl.setStyleSheet(f"color: {color};")
            self._layout.addWidget(status_lbl)

        if data.get("my_rating") is not None:
            mr = QLabel(f"\u2665 My Rating: {data['my_rating']}/10")
            mr.setFont(QFont("Segoe UI", 9))
            mr.setStyleSheet(f"color: {MY_HEART};")
            self._layout.addWidget(mr)

        if data.get("notes"):
            notes_lbl = QLabel(data["notes"])
            notes_lbl.setFont(QFont("Segoe UI", 8))
            notes_lbl.setStyleSheet(f"color: {TEXT_DIM};")
            notes_lbl.setWordWrap(True)
            notes_lbl.setMaximumWidth(320)
            self._layout.addWidget(notes_lbl)

        if cached.get("user_tags"):
            ut_str = "My tags: " + ", ".join(cached["user_tags"])
            ut_lbl = QLabel(ut_str)
            ut_lbl.setFont(QFont("Segoe UI", 8))
            ut_lbl.setStyleSheet(f"color: {ACCENT};")
            ut_lbl.setWordWrap(True)
            ut_lbl.setMaximumWidth(320)
            self._layout.addWidget(ut_lbl)

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
            "SELECT p.name, p.group_name FROM f95.item_prefixes ip "
            "JOIN f95.prefixes p ON p.id = ip.prefix_id WHERE ip.thread_id = ?",
            (tid,),
        ).fetchall()]
        tags = [r["name"] for r in db.execute(
            "SELECT t.name FROM f95.item_tags it JOIN f95.tags t ON t.id = it.tag_id "
            "WHERE it.thread_id = ? ORDER BY t.name", (tid,),
        ).fetchall()]
        user_tags = [r["tag"] for r in db.execute(
            "SELECT tag FROM user_tags WHERE thread_id = ? ORDER BY tag", (tid,),
        ).fetchall()]
        return {"prefixes": prefixes, "tags": tags, "user_tags": user_tags}

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

    def __init__(self, all_tags: list[str], prefix_groups: dict[str, list],
                 user_tags: list[str], parent=None):
        super().__init__(parent)
        self.setFixedWidth(250)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)

        self.all_tags = all_tags
        self.selected_tags: list[str] = []
        self.prefix_vars: dict[int, QCheckBox] = {}
        self.all_user_tags = user_tags
        self.selected_user_tags: list[str] = []
        self.status_vars: dict[str, QCheckBox] = {}

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
        self.sort_combo.addItems(SORT_OPTIONS)
        sort_layout.addWidget(self.sort_combo)
        self.order_combo = QComboBox()
        self.order_combo.addItems(["Descending", "Ascending"])
        sort_layout.addWidget(self.order_combo)
        self._layout.addWidget(sort_row)

        # Secondary sort
        sort2_row = QWidget()
        sort2_layout = QHBoxLayout(sort2_row)
        sort2_layout.setContentsMargins(0, 0, 0, 0)
        sort2_layout.setSpacing(6)
        then_lbl = QLabel("Then by")
        then_lbl.setFont(QFont("Segoe UI", 8))
        then_lbl.setStyleSheet(f"color: {TEXT_DIM};")
        sort2_layout.addWidget(then_lbl)
        self.sort2_combo = QComboBox()
        self.sort2_combo.addItems(SORT2_OPTIONS)
        sort2_layout.addWidget(self.sort2_combo)
        sort2_layout.addStretch()
        self._layout.addWidget(sort2_row)
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

        # ── User data filters ──

        # My Status
        self._heading("MY STATUS")
        for s in STATUS_OPTIONS:
            cb = QCheckBox(s.capitalize())
            self.status_vars[s] = cb
            self._layout.addWidget(cb)
        cb_unmarked = QCheckBox("Unmarked")
        self.status_vars["unmarked"] = cb_unmarked
        self._layout.addWidget(cb_unmarked)
        self._sep()

        # My Rating
        self._heading("MY RATING")
        my_rating_row = QWidget()
        my_rating_layout = QHBoxLayout(my_rating_row)
        my_rating_layout.setContentsMargins(0, 0, 0, 0)
        my_rating_layout.setSpacing(4)
        self.my_rating_min = QLineEdit()
        self.my_rating_min.setPlaceholderText("Min")
        self.my_rating_min.setFixedWidth(70)
        my_rating_layout.addWidget(self.my_rating_min)
        dash3 = QLabel("\u2014")
        dash3.setStyleSheet(f"color: {TEXT_DIM};")
        my_rating_layout.addWidget(dash3)
        self.my_rating_max = QLineEdit()
        self.my_rating_max.setPlaceholderText("Max")
        self.my_rating_max.setFixedWidth(70)
        my_rating_layout.addWidget(self.my_rating_max)
        my_rating_layout.addStretch()
        self._layout.addWidget(my_rating_row)
        self._sep()

        # My Tags
        self._heading("MY TAGS")
        self.user_tag_edit = QLineEdit()
        self.user_tag_edit.setPlaceholderText("Type user tag...")
        self._user_tag_completer_model = QStringListModel(self.all_user_tags)
        self._user_tag_completer = QCompleter(self._user_tag_completer_model)
        self._user_tag_completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._user_tag_completer.setFilterMode(Qt.MatchContains)
        self._user_tag_completer.setMaxVisibleItems(10)
        self.user_tag_edit.setCompleter(self._user_tag_completer)
        self._user_tag_completer.activated.connect(self._pick_user_tag)
        self.user_tag_edit.returnPressed.connect(self._on_user_tag_return)
        self._layout.addWidget(self.user_tag_edit)

        # User tag mode
        umode_row = QWidget()
        umode_layout = QHBoxLayout(umode_row)
        umode_layout.setContentsMargins(0, 0, 0, 0)
        umode_layout.setSpacing(8)
        self.user_tag_mode_group = QButtonGroup(self)
        self.user_tag_mode_and = QRadioButton("All (AND)")
        self.user_tag_mode_or = QRadioButton("Any (OR)")
        self.user_tag_mode_and.setChecked(True)
        self.user_tag_mode_group.addButton(self.user_tag_mode_and, 0)
        self.user_tag_mode_group.addButton(self.user_tag_mode_or, 1)
        umode_layout.addWidget(self.user_tag_mode_and)
        umode_layout.addWidget(self.user_tag_mode_or)
        umode_layout.addStretch()
        self._layout.addWidget(umode_row)

        self.user_chips_widget = QWidget()
        self.user_chips_layout = QVBoxLayout(self.user_chips_widget)
        self.user_chips_layout.setContentsMargins(0, 0, 0, 0)
        self.user_chips_layout.setSpacing(2)
        self._layout.addWidget(self.user_chips_widget)
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

    # ── F95 tag helpers ──

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
            self.chips_layout.addWidget(self._make_chip(t, self._remove_tag))

    # ── User tag helpers ──

    def _pick_user_tag(self, name: str):
        if name and name not in self.selected_user_tags:
            self.selected_user_tags.append(name)
            self._refresh_user_chips()
        QTimer.singleShot(0, lambda: self.user_tag_edit.clear())

    def _on_user_tag_return(self):
        text = self.user_tag_edit.text().strip().lower()
        match = next((t for t in self.all_user_tags
                      if t.lower() == text and t not in self.selected_user_tags), None)
        if match:
            self._pick_user_tag(match)

    def _remove_user_tag(self, name: str):
        if name in self.selected_user_tags:
            self.selected_user_tags.remove(name)
            self._refresh_user_chips()

    def _refresh_user_chips(self):
        while self.user_chips_layout.count():
            item = self.user_chips_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for t in self.selected_user_tags:
            self.user_chips_layout.addWidget(self._make_chip(t, self._remove_user_tag))

    # ── Shared chip builder ──

    def _make_chip(self, text: str, remove_cb) -> QWidget:
        chip = QWidget()
        chip.setStyleSheet(f"background-color: {CHIP_BG}; border-radius: 3px;")
        chip_layout = QHBoxLayout(chip)
        chip_layout.setContentsMargins(6, 2, 6, 2)
        chip_layout.setSpacing(4)
        lbl = QLabel(text)
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
        close_btn.clicked.connect(lambda checked=False, n=text: remove_cb(n))
        chip_layout.addWidget(close_btn)
        return chip

    # ── Gather / restore ──

    def gather_filters(self) -> dict:
        f: dict = {
            "title": self.title_edit.text(),
            "creator": self.creator_edit.text(),
            "sort": self.sort_combo.currentText(),
            "sort2": self.sort2_combo.currentText(),
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

        # User mark filters
        for attr, widget in [("my_rating_min", self.my_rating_min),
                             ("my_rating_max", self.my_rating_max)]:
            val = widget.text().strip()
            if val:
                try:
                    f[attr] = int(val)
                except ValueError:
                    pass

        checked_statuses = [s for s, cb in self.status_vars.items() if cb.isChecked()]
        if checked_statuses:
            f["statuses"] = checked_statuses

        if self.selected_user_tags:
            f["user_tags"] = list(self.selected_user_tags)
            f["user_tag_mode"] = "AND" if self.user_tag_mode_and.isChecked() else "OR"

        return f

    def get_full_state(self) -> dict:
        """Return all widget state for persistence."""
        return {
            "tile_size": self.tile_slider.value(),
            "title": self.title_edit.text(),
            "creator": self.creator_edit.text(),
            "sort": self.sort_combo.currentText(),
            "sort2": self.sort2_combo.currentText(),
            "order": self.order_combo.currentText(),
            "prefix_ids": [pid for pid, cb in self.prefix_vars.items() if cb.isChecked()],
            "tags": list(self.selected_tags),
            "tag_mode": "AND" if self.tag_mode_and.isChecked() else "OR",
            "rating_min": self.rating_min.text(),
            "rating_max": self.rating_max.text(),
            "views_min": self.views_min.text(),
            "views_max": self.views_max.text(),
            "statuses": [s for s, cb in self.status_vars.items() if cb.isChecked()],
            "my_rating_min": self.my_rating_min.text(),
            "my_rating_max": self.my_rating_max.text(),
            "user_tags": list(self.selected_user_tags),
            "user_tag_mode": "AND" if self.user_tag_mode_and.isChecked() else "OR",
        }

    def restore_state(self, data: dict):
        """Restore widget state from a saved dict."""
        if "tile_size" in data:
            self.tile_slider.setValue(data["tile_size"])
        self.title_edit.setText(data.get("title", ""))
        self.creator_edit.setText(data.get("creator", ""))

        idx = self.sort_combo.findText(data.get("sort", "Date"))
        if idx >= 0:
            self.sort_combo.setCurrentIndex(idx)
        idx2 = self.sort2_combo.findText(data.get("sort2", "None"))
        if idx2 >= 0:
            self.sort2_combo.setCurrentIndex(idx2)
        idx3 = self.order_combo.findText(data.get("order", "Descending"))
        if idx3 >= 0:
            self.order_combo.setCurrentIndex(idx3)

        saved_pids = set(data.get("prefix_ids", []))
        for pid, cb in self.prefix_vars.items():
            cb.setChecked(pid in saved_pids)

        self.selected_tags = list(data.get("tags", []))
        self._refresh_chips()
        if data.get("tag_mode") == "OR":
            self.tag_mode_or.setChecked(True)
        else:
            self.tag_mode_and.setChecked(True)

        self.rating_min.setText(data.get("rating_min", ""))
        self.rating_max.setText(data.get("rating_max", ""))
        self.views_min.setText(data.get("views_min", ""))
        self.views_max.setText(data.get("views_max", ""))

        saved_statuses = set(data.get("statuses", []))
        for s, cb in self.status_vars.items():
            cb.setChecked(s in saved_statuses)

        self.my_rating_min.setText(data.get("my_rating_min", ""))
        self.my_rating_max.setText(data.get("my_rating_max", ""))

        self.selected_user_tags = list(data.get("user_tags", []))
        self._refresh_user_chips()
        if data.get("user_tag_mode") == "OR":
            self.user_tag_mode_or.setChecked(True)
        else:
            self.user_tag_mode_and.setChecked(True)

    def _do_reset(self):
        self.title_edit.clear()
        self.creator_edit.clear()
        self.sort_combo.setCurrentIndex(0)
        self.sort2_combo.setCurrentIndex(0)
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
        for cb in self.status_vars.values():
            cb.setChecked(False)
        self.my_rating_min.clear()
        self.my_rating_max.clear()
        self.selected_user_tags.clear()
        self._refresh_user_chips()
        self.user_tag_mode_and.setChecked(True)
        self.filtersReset.emit()

    def refresh_user_tag_completer(self, tags: list[str]):
        self.all_user_tags = tags
        self._user_tag_completer_model.setStringList(tags)


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
QMenu {{
    background-color: {CARD_BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QMenu::item {{
    padding: 4px 20px;
    border-radius: 3px;
}}
QMenu::item:selected {{
    background-color: {ACCENT};
    color: white;
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 8px;
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
        self.total_count = self.db.execute("SELECT COUNT(*) AS c FROM f95.items").fetchone()["c"]
        self.all_tags = fetch_all_tags(self.db)
        self.prefix_groups = fetch_prefixes_by_group(self.db)
        self.user_tags = fetch_all_user_tags(self.db)

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
        self._load_settings()
        self._apply_filters()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Sidebar
        self._sidebar = FilterSidebar(self.all_tags, self.prefix_groups, self.user_tags)
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
        self._list_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self._list_view.customContextMenuRequested.connect(self._show_context_menu)
        self._list_view.setGridSize(
            QSize(DEFAULT_CARD_W + CARD_PAD, self._delegate._card_h + CARD_PAD))

        self._list_view.entered.connect(self._on_card_entered)
        self._list_view.clicked.connect(self._on_card_clicked)
        self._list_view.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._list_view.viewport().installEventFilter(self)
        layout.addWidget(self._list_view)

    # ── Settings persistence ──

    def _load_settings(self):
        if SETTINGS_PATH.exists():
            try:
                data = json.loads(SETTINGS_PATH.read_text("utf-8"))
                self._sidebar.restore_state(data)
            except (json.JSONDecodeError, KeyError):
                pass

    def _save_settings(self):
        data = self._sidebar.get_full_state()
        SETTINGS_PATH.write_text(json.dumps(data, indent=2), "utf-8")

    # ── Events ──

    def eventFilter(self, obj, event):
        if obj is self._list_view.viewport() and event.type() == QEvent.Leave:
            self._tooltip.hide_tooltip()
            self._tooltip_timer.stop()
            self._pending_tooltip_index = None
            if self._delegate._hovered_index is not None:
                self._delegate.set_hovered(None)
                self._list_view.viewport().update()
        return super().eventFilter(obj, event)

    def _on_card_entered(self, index: QModelIndex):
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

    # ── Context menu ──

    def _show_context_menu(self, pos):
        index = self._list_view.indexAt(pos)
        if not index.isValid():
            return
        data = self._model.get_row(index.row())
        if not data:
            return

        menu = QMenu(self)
        tid = data["thread_id"]

        # Status submenu
        status_menu = menu.addMenu("Status")
        for s in STATUS_OPTIONS:
            action = status_menu.addAction(s.capitalize())
            action.setCheckable(True)
            action.setChecked(data.get("status") == s)
            action.triggered.connect(lambda checked, st=s: self._set_status(index.row(), tid, st))
        status_menu.addSeparator()
        clear_action = status_menu.addAction("Clear")
        clear_action.triggered.connect(lambda: self._set_status(index.row(), tid, None))

        # Rating submenu
        rating_menu = menu.addMenu("My Rating")
        for r in range(1, 11):
            action = rating_menu.addAction(f"{r}/10")
            action.setCheckable(True)
            action.setChecked(data.get("my_rating") == r)
            action.triggered.connect(lambda checked, val=r: self._set_my_rating(index.row(), tid, val))
        rating_menu.addSeparator()
        clear_rating = rating_menu.addAction("Clear")
        clear_rating.triggered.connect(lambda: self._set_my_rating(index.row(), tid, None))

        # Notes
        menu.addSeparator()
        notes_action = menu.addAction("Notes...")
        notes_action.triggered.connect(lambda: self._edit_notes(index.row(), tid, data.get("notes", "")))

        # User tags
        user_tags_action = menu.addAction("User Tags...")
        user_tags_action.triggered.connect(lambda: self._edit_user_tags(index.row(), tid))

        menu.exec(self._list_view.viewport().mapToGlobal(pos))

    def _set_status(self, row: int, tid: int, status: str | None):
        self.db.execute(
            "INSERT INTO user_marks (thread_id, status) VALUES (?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET status = ?",
            (tid, status, status))
        self.db.commit()
        self._model.update_row(row, {"status": status})

    def _set_my_rating(self, row: int, tid: int, rating: int | None):
        self.db.execute(
            "INSERT INTO user_marks (thread_id, my_rating) VALUES (?, ?) "
            "ON CONFLICT(thread_id) DO UPDATE SET my_rating = ?",
            (tid, rating, rating))
        self.db.commit()
        self._model.update_row(row, {"my_rating": rating})

    def _edit_notes(self, row: int, tid: int, current: str):
        text, ok = QInputDialog.getMultiLineText(
            self, "Notes", f"Notes for this item:", current)
        if ok:
            notes = text.strip() or None
            self.db.execute(
                "INSERT INTO user_marks (thread_id, notes) VALUES (?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET notes = ?",
                (tid, notes, notes))
            self.db.commit()
            self._model.update_row(row, {"notes": notes})

    def _edit_user_tags(self, row: int, tid: int):
        existing = [r["tag"] for r in self.db.execute(
            "SELECT tag FROM user_tags WHERE thread_id = ? ORDER BY tag", (tid,)
        ).fetchall()]
        text, ok = QInputDialog.getText(
            self, "User Tags",
            f"Comma-separated tags (current: {', '.join(existing) or 'none'}):",
            text=", ".join(existing))
        if ok:
            new_tags = [t.strip() for t in text.split(",") if t.strip()]
            self.db.execute("DELETE FROM user_tags WHERE thread_id = ?", (tid,))
            for tag in new_tags:
                self.db.execute(
                    "INSERT OR IGNORE INTO user_tags (thread_id, tag) VALUES (?, ?)",
                    (tid, tag))
            self.db.commit()
            # Refresh user tag completer
            self.user_tags = fetch_all_user_tags(self.db)
            self._sidebar.refresh_user_tag_completer(self.user_tags)
            # Invalidate tooltip cache for this item
            self._tooltip._tooltip_cache.pop(tid, None)

    # ── Filtering ──

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
        self._save_settings()

    def _load_page(self):
        if self.loaded_rows >= self.result_count:
            return
        sql, params = build_query(self._current_filters)
        params.extend([PAGE_SIZE, self.loaded_rows])
        rows = self.db.execute(sql, params).fetchall()
        self._model.append_page(rows)
        self.loaded_rows += len(rows)

    def _on_image_loaded(self, url: str):
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
