# Background Editor - portrait-aware background removal
# Copyright (C) 2026 Optimey CommV
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation, either
# version 3 of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with this
# program. If not, see <https://www.gnu.org/licenses/>.

"""The slim, non-modal bar above the preview that announces a new version of the app, and
the inert view of its release notes.

The release notes are the one part of a release that the signed manifest does not cover:
anyone who can edit the GitHub release can write them. They are therefore shown inertly.
Raw HTML is not interpreted, every image is replaced by its alt text before the notes are
shown, no resource (image, style sheet, file or network address) is ever loaded, and a
link opens only when it is an https:// address on github.com; any other link is plain text.
"""

from __future__ import annotations

import html

import bgeditor
from PyQt6.QtCore import QEvent, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QIcon,
    QImage,
    QPalette,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QToolButton,
    QWidget,
)

from .resources import logo_pixmap

RELEASE_LINK_HOST = "github.com"
NO_NOTES = "*This release has no notes.*"

# How a permitted link is opened; the tests replace it.
_open_url = QDesktopServices.openUrl


def is_release_link(url) -> bool:
    """True only for an https:// address inside this app's GitHub repository (no user name,
    no other port).
    Everything else - file:, UNC paths, http:, javascript:, search-ms:, other hosts - is
    never opened from release notes."""
    if not isinstance(url, QUrl):
        url = QUrl(str(url or ""))
    if not (
        url.isValid()
        and url.scheme().lower() == "https"
        and url.host().lower() == RELEASE_LINK_HOST
        and not url.userInfo()
        and url.port() in (-1, 443)
    ):
        return False
    # Only pages of this app's own repository: notes can be edited without the signing key.
    path = url.adjusted(QUrl.UrlFormattingOption.NormalizePathSegments).path()
    if ".." in path.split("/"):
        return False
    prefix = f"/{bgeditor.UPDATE_REPO}".lower()
    return path.lower() == prefix or path.lower().startswith(prefix + "/")


def open_release_link(url) -> bool:
    """Open url in the browser when is_release_link() allows it; ignore it otherwise."""
    if not isinstance(url, QUrl):
        url = QUrl(str(url or ""))
    if not is_release_link(url):
        return False
    _open_url(url)
    return True


def _inert_resource(kind) -> object:
    """What a notes document gets for any resource it asks for: a transparent 1x1 image, or
    an empty text. Never None, because Qt then loads a local or UNC file by itself."""
    if int(kind) == QTextDocument.ResourceType.ImageResource.value:
        image = QImage(1, 1, QImage.Format.Format_ARGB32)
        image.fill(0)
        return image
    return ""


class InertDocument(QTextDocument):
    """A text document that never loads a resource (see _inert_resource)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.requested: list[str] = []  # what was asked for (nothing, normally)

    def loadResource(self, kind, url):  # noqa: N802 - Qt's name
        self.requested.append(url.toString() if isinstance(url, QUrl) else str(url))
        return _inert_resource(kind)


def _unsafe_fragments(doc: QTextDocument) -> list[tuple[str, int, int, QTextCharFormat]]:
    """(kind, position, length, format) of every image and of every link that is not a
    permitted release link, in document order."""
    found = []
    block = doc.begin()
    while block.isValid():
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                fmt = frag.charFormat()
                if fmt.isImageFormat():
                    found.append(("image", frag.position(), frag.length(), fmt))
                elif fmt.isAnchor() and not is_release_link(QUrl(fmt.anchorHref())):
                    found.append(("link", frag.position(), frag.length(), fmt))
            it += 1
        block = block.next()
    return found


def _defuse(doc: QTextDocument) -> None:
    """Replace every image by its alt text and turn every link that may not be opened into
    plain text. Raises RuntimeError when anything is left."""
    cursor = QTextCursor(doc)
    for kind, pos, length, fmt in reversed(_unsafe_fragments(doc)):
        cursor.setPosition(pos)
        cursor.setPosition(pos + length, QTextCursor.MoveMode.KeepAnchor)
        if kind == "image":
            alt = str(fmt.property(QTextFormat.Property.ImageAltText) or "").strip()
            plain = QTextCharFormat()
            plain.setFontItalic(True)
            cursor.insertText(f"[image: {alt}]" if alt else "[image]", plain)
        else:
            plain = QTextCharFormat(fmt)
            for prop in (QTextFormat.Property.IsAnchor, QTextFormat.Property.AnchorHref, QTextFormat.Property.AnchorName):
                plain.clearProperty(prop)
            plain.setFontUnderline(False)
            plain.clearForeground()
            cursor.setCharFormat(plain)
    if _unsafe_fragments(doc):
        raise RuntimeError("the release notes still hold an image or a link")


def release_notes_document(notes: str, parent=None) -> InertDocument:
    """The release notes (GitHub Markdown, raw HTML shown as text) as an inert document.
    When they cannot be made safe, they are shown as plain text."""
    doc = InertDocument(parent)
    text = (notes or "").strip() or NO_NOTES
    features = QTextDocument.MarkdownFeature(
        QTextDocument.MarkdownFeature.MarkdownDialectGitHub.value | QTextDocument.MarkdownFeature.MarkdownNoHTML.value
    )
    try:
        doc.setMarkdown(text, features)
        _defuse(doc)
    except Exception:  # plain text holds no image and no link
        doc.setPlainText(text)
    return doc


class ReleaseNotesView(QTextBrowser):
    """The release notes, inert: see the module docstring."""

    def __init__(self, notes: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.setDocument(release_notes_document(notes, self))
        self.anchorClicked.connect(open_release_link)

    def loadResource(self, kind, url):  # noqa: N802 - Qt's name; the document never asks here
        return _inert_resource(kind)


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


class UpdateBar(QFrame):
    """'Background Editor X is available' with Update now, What's new, Skip this version and
    a close button. It only emits signals; the main window does the work."""

    updateRequested = pyqtSignal()
    notesRequested = pyqtSignal()
    skipRequested = pyqtSignal()
    dismissed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("updateBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAccessibleName("New version available")
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 4, 4, 4)
        h.setSpacing(6)

        logo = QLabel()
        logo.setPixmap(logo_pixmap(20))
        logo.setFixedSize(QSize(20, 20))
        h.addWidget(logo)
        self.lbl_text = QLabel("")
        self.lbl_text.setTextFormat(Qt.TextFormat.RichText)
        self.lbl_text.setMinimumWidth(60)
        self.lbl_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        h.addWidget(self.lbl_text, 1)

        self.btn_update = QPushButton("Update now")
        self.btn_update.clicked.connect(self.updateRequested.emit)
        self.btn_notes = QPushButton("What's new")
        self.btn_notes.setToolTip("The release notes of the new version")
        self.btn_notes.clicked.connect(self.notesRequested.emit)
        self.btn_skip = QPushButton("Skip this version")
        self.btn_skip.setToolTip("Do not announce this version at startup again (Check for updates still shows it)")
        self.btn_skip.clicked.connect(self.skipRequested.emit)
        for b in (self.btn_update, self.btn_notes, self.btn_skip):
            b.setAutoDefault(False)
            h.addWidget(b)
        # Drawn as the primary (accent) button; there is no dialog here for Enter to trigger it.
        self.btn_update.setDefault(True)
        self.btn_close = QToolButton()
        self.btn_close.setIcon(QIcon.fromTheme(QIcon.ThemeIcon.WindowClose))
        self.btn_close.setIconSize(QSize(12, 12))
        self.btn_close.setAutoRaise(True)
        self.btn_close.setToolTip("Hide this message until the next start")
        self.btn_close.setAccessibleName("Hide")
        self.btn_close.clicked.connect(self._close)
        h.addWidget(self.btn_close)
        self._text = ""
        self._restyling = False
        self._restyle()

    def set_report(self, report) -> None:
        """Show an 'available' AppUpdateReport (a release whose signed manifest verified)."""
        headline = f"Background Editor {html.escape(report.latest)} is available"
        self._text = f"<b>{headline}</b> &nbsp;·&nbsp; you have {html.escape(report.current)}"
        self.lbl_text.setText(self._text)
        self.lbl_text.setToolTip(report.message)
        if report.installable:
            self.btn_update.setText("Update now")
            self.btn_update.setToolTip(
                "Downloads the new version, checks it and installs it; Background Editor closes for that."
            )
        else:
            self.btn_update.setText("Open release page")
            self.btn_update.setToolTip(report.message)
        self.btn_notes.setEnabled(True)

    def _close(self) -> None:
        self.setVisible(False)
        self.dismissed.emit()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and not self._restyling:
            self._restyle()

    def _restyle(self) -> None:
        """A light tint of the accent colour, readable in light and dark mode. Setting a style
        sheet changes the palette too, so the change that causes is not answered again, and the
        colours come from the application's palette, which the style sheet does not touch."""
        pal = QApplication.palette()
        window = pal.color(QPalette.ColorRole.Window)
        accent = pal.color(QPalette.ColorRole.Accent)
        if not accent.isValid() or accent.alpha() == 0:
            accent = pal.color(QPalette.ColorRole.Highlight)
        dark = window.lightness() < 128
        background = _mix(window, accent, 0.22 if dark else 0.12)
        border = _mix(window, accent, 0.55)
        text = pal.color(QPalette.ColorRole.WindowText)
        sheet = (
            "QFrame#updateBar { background-color: %s; border: 1px solid %s; border-radius: 6px; }"
            "QFrame#updateBar QLabel { color: %s; background: transparent; }"
            % (background.name(), border.name(), text.name())
        )
        if sheet == self.styleSheet():
            return
        self._restyling = True
        try:
            self.setStyleSheet(sheet)
        finally:
            self._restyling = False
