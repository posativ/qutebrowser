# vim: ft=python fileencoding=utf-8 sts=4 sw=4 et:

# Copyright 2014 Florian Bruhin (The Compiler) <mail@qutebrowser.org>
#
# This file is part of qutebrowser.
#
# qutebrowser is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# qutebrowser is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with qutebrowser.  If not, see <http://www.gnu.org/licenses/>.

"""The main statusbar widget."""

import collections

from PyQt5.QtCore import pyqtSignal, pyqtSlot, pyqtProperty, Qt, QTime, QSize
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QStackedLayout, QSizePolicy

from qutebrowser.config import config, style
from qutebrowser.utils import usertypes, log, objreg, utils
from qutebrowser.mainwindow.statusbar import (command, progress, keystring,
                                              percentage, url, prompt)
from qutebrowser.mainwindow.statusbar import text as textwidget


PreviousWidget = usertypes.enum('PreviousWidget', ['none', 'prompt',
                                                   'command'])


class StatusBar(QWidget):

    """The statusbar at the bottom of the mainwindow.

    Attributes:
        txt: The Text widget in the statusbar.
        keystring: The KeyString widget in the statusbar.
        percentage: The Percentage widget in the statusbar.
        url: The UrlText widget in the statusbar.
        prog: The Progress widget in the statusbar.
        cmd: The Command widget in the statusbar.
        _hbox: The main QHBoxLayout.
        _stack: The QStackedLayout with cmd/txt widgets.
        _text_queue: A deque of (error, text) tuples to be displayed.
                     error: True if message is an error, False otherwise
        _text_pop_timer: A Timer displaying the error messages.
        _stopwatch: A QTime for the last displayed message.
        _timer_was_active: Whether the _text_pop_timer was active before hiding
                           the command widget.
        _previous_widget: A PreviousWidget member - the widget which was
                          displayed when an error interrupted it.
        _win_id: The window ID the statusbar is associated with.

    Class attributes:
        _error: If there currently is an error, accessed through the error
                property.

                For some reason we need to have this as class attribute so
                pyqtProperty works correctly.

        _prompt_active: If we're currently in prompt-mode.

                        For some reason we need to have this as class attribute
                        so pyqtProperty works correctly.

        _insert_active: If we're currently in insert mode.

                        For some reason we need to have this as class attribute
                        so pyqtProperty works correctly.

    Signals:
        resized: Emitted when the statusbar has resized, so the completion
                 widget can adjust its size to it.
                 arg: The new size.
        moved: Emitted when the statusbar has moved, so the completion widget
               can move the the right position.
               arg: The new position.
    """

    resized = pyqtSignal('QRect')
    moved = pyqtSignal('QPoint')
    _error = False
    _prompt_active = False
    _insert_active = False

    STYLESHEET = """
        QWidget#StatusBar {
            {{ color['statusbar.bg'] }}
        }

        QWidget#StatusBar[insert_active="true"] {
            {{ color['statusbar.bg.insert'] }}
        }

        QWidget#StatusBar[prompt_active="true"] {
            {{ color['statusbar.bg.prompt'] }}
        }

        QWidget#StatusBar[error="true"] {
            {{ color['statusbar.bg.error'] }}
        }

        QLabel, QLineEdit {
            {{ color['statusbar.fg'] }}
            {{ font['statusbar'] }}
        }
    """

    def __init__(self, win_id, parent=None):
        super().__init__(parent)
        objreg.register('statusbar', self, scope='window', window=win_id)
        self.setObjectName(self.__class__.__name__)
        self.setAttribute(Qt.WA_StyledBackground)
        style.set_register_stylesheet(self)

        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

        self._win_id = win_id
        self._option = None
        self._stopwatch = QTime()

        self._hbox = QHBoxLayout(self)
        self._hbox.setContentsMargins(0, 0, 0, 0)
        self._hbox.setSpacing(5)

        self._stack = QStackedLayout()
        self._hbox.addLayout(self._stack)
        self._stack.setContentsMargins(0, 0, 0, 0)

        self.cmd = command.Command(win_id)
        self._stack.addWidget(self.cmd)
        objreg.register('status-command', self.cmd, scope='window',
                        window=win_id)

        self.txt = textwidget.Text()
        self._stack.addWidget(self.txt)
        self._timer_was_active = False
        self._text_queue = collections.deque()
        self._text_pop_timer = usertypes.Timer(self, 'statusbar_text_pop')
        self._text_pop_timer.timeout.connect(self._pop_text)
        self.set_pop_timer_interval()
        objreg.get('config').changed.connect(self.set_pop_timer_interval)

        self.prompt = prompt.Prompt(win_id)
        self._stack.addWidget(self.prompt)
        self._previous_widget = PreviousWidget.none

        self.cmd.show_cmd.connect(self._show_cmd_widget)
        self.cmd.hide_cmd.connect(self._hide_cmd_widget)
        self._hide_cmd_widget()
        prompter = objreg.get('prompter', scope='window', window=self._win_id)
        prompter.show_prompt.connect(self._show_prompt_widget)
        prompter.hide_prompt.connect(self._hide_prompt_widget)
        self._hide_prompt_widget()

        self.keystring = keystring.KeyString()
        self._hbox.addWidget(self.keystring)

        self.url = url.UrlText()
        self._hbox.addWidget(self.url)

        self.percentage = percentage.Percentage()
        self._hbox.addWidget(self.percentage)

        # We add a parent to Progress here because it calls self.show() based
        # on some signals, and if that happens before it's added to the layout,
        # it will quickly blink up as independent window.
        self.prog = progress.Progress(self)
        self._hbox.addWidget(self.prog)

    def __repr__(self):
        return utils.get_repr(self)

    @pyqtProperty(bool)
    def error(self):
        """Getter for self.error, so it can be used as Qt property."""
        # pylint: disable=method-hidden
        return self._error

    def _set_error(self, val):
        """Setter for self.error, so it can be used as Qt property.

        Re-set the stylesheet after setting the value, so everything gets
        updated by Qt properly.
        """
        if self._error == val:
            # This gets called a lot (e.g. if the completion selection was
            # changed), and setStyleSheet is relatively expensive, so we ignore
            # this if there's nothing to change.
            return
        log.statusbar.debug("Setting error to {}".format(val))
        self._error = val
        self.setStyleSheet(style.get_stylesheet(self.STYLESHEET))
        if val:
            # If we got an error while command/prompt was shown, raise the text
            # widget.
            self._stack.setCurrentWidget(self.txt)

    @pyqtProperty(bool)
    def prompt_active(self):
        """Getter for self.prompt_active, so it can be used as Qt property."""
        # pylint: disable=method-hidden
        return self._prompt_active

    def _set_prompt_active(self, val):
        """Setter for self.prompt_active.

        Re-set the stylesheet after setting the value, so everything gets
        updated by Qt properly.
        """
        log.statusbar.debug("Setting prompt_active to {}".format(val))
        self._prompt_active = val
        self.setStyleSheet(style.get_stylesheet(self.STYLESHEET))

    @pyqtProperty(bool)
    def insert_active(self):
        """Getter for self.insert_active, so it can be used as Qt property."""
        # pylint: disable=method-hidden
        return self._insert_active

    def _set_insert_active(self, val):
        """Setter for self.insert_active.

        Re-set the stylesheet after setting the value, so everything gets
        updated by Qt properly.
        """
        log.statusbar.debug("Setting insert_active to {}".format(val))
        self._insert_active = val
        self.setStyleSheet(style.get_stylesheet(self.STYLESHEET))

    def _set_mode_text(self, mode):
        """Set the mode text."""
        text = "-- {} MODE --".format(mode.upper())
        self.txt.set_text(self.txt.Text.normal, text)

    def _pop_text(self):
        """Display a text in the statusbar and pop it from _text_queue."""
        try:
            error, text = self._text_queue.popleft()
        except IndexError:
            self._set_error(False)
            self.txt.set_text(self.txt.Text.temp, '')
            self._text_pop_timer.stop()
            # If a previous widget was interrupted by an error, restore it.
            if self._previous_widget == PreviousWidget.prompt:
                self._stack.setCurrentWidget(self.prompt)
            elif self._previous_widget == PreviousWidget.command:
                self._stack.setCurrentWidget(self.command)
            elif self._previous_widget == PreviousWidget.none:
                pass
            else:
                raise AssertionError("Unknown _previous_widget!")
            return
        log.statusbar.debug("Displaying {} message: {}".format(
            'error' if error else 'text', text))
        log.statusbar.debug("Remaining: {}".format(self._text_queue))
        self._set_error(error)
        self.txt.set_text(self.txt.Text.temp, text)

    def _show_cmd_widget(self):
        """Show command widget instead of temporary text."""
        self._set_error(False)
        self._previous_widget = PreviousWidget.prompt
        if self._text_pop_timer.isActive():
            self._timer_was_active = True
        self._text_pop_timer.stop()
        self._stack.setCurrentWidget(self.cmd)

    def _hide_cmd_widget(self):
        """Show temporary text instead of command widget."""
        log.statusbar.debug("Hiding cmd widget, queue: {}".format(
            self._text_queue))
        self._previous_widget = PreviousWidget.none
        if self._timer_was_active:
            # Restart the text pop timer if it was active before hiding.
            self._pop_text()
            self._text_pop_timer.start()
            self._timer_was_active = False
        self._stack.setCurrentWidget(self.txt)

    def _show_prompt_widget(self):
        """Show prompt widget instead of temporary text."""
        if self._stack.currentWidget() is self.prompt:
            return
        self._set_error(False)
        self._set_prompt_active(True)
        self._previous_widget = PreviousWidget.prompt
        if self._text_pop_timer.isActive():
            self._timer_was_active = True
        self._text_pop_timer.stop()
        self._stack.setCurrentWidget(self.prompt)

    def _hide_prompt_widget(self):
        """Show temporary text instead of prompt widget."""
        self._set_prompt_active(False)
        self._previous_widget = PreviousWidget.none
        log.statusbar.debug("Hiding prompt widget, queue: {}".format(
            self._text_queue))
        if self._timer_was_active:
            # Restart the text pop timer if it was active before hiding.
            self._pop_text()
            self._text_pop_timer.start()
            self._timer_was_active = False
        self._stack.setCurrentWidget(self.txt)

    def _disp_text(self, text, error, immediately=False):
        """Inner logic for disp_error and disp_temp_text.

        Args:
            text: The message to display.
            error: Whether it's an error message (True) or normal text (False)
            immediately: If set, message gets displayed immediately instead of
                         queued.
        """
        log.statusbar.debug("Displaying text: {} (error={})".format(
            text, error))
        mindelta = config.get('ui', 'message-timeout')
        if self._stopwatch.isNull():
            delta = None
            self._stopwatch.start()
        else:
            delta = self._stopwatch.restart()
        log.statusbar.debug("queue: {} / delta: {}".format(
            self._text_queue, delta))
        if not self._text_queue and (delta is None or delta > mindelta):
            # If the queue is empty and we didn't print messages for long
            # enough, we can take the short route and display the message
            # immediately. We then start the pop_timer only to restore the
            # normal state in 2 seconds.
            log.statusbar.debug("Displaying immediately")
            self._set_error(error)
            self.txt.set_text(self.txt.Text.temp, text)
            self._text_pop_timer.start()
        elif self._text_queue and self._text_queue[-1] == (error, text):
            # If we get the same message multiple times in a row and we're
            # still displaying it *anyways* we ignore the new one
            log.statusbar.debug("ignoring")
        elif immediately:
            # This message is a reaction to a keypress and should be displayed
            # immediately, temporarily interrupting the message queue.
            # We display this immediately and restart the timer.to clear it and
            # display the rest of the queue later.
            log.statusbar.debug("Moving to beginning of queue")
            self._set_error(error)
            self.txt.set_text(self.txt.Text.temp, text)
            self._text_pop_timer.start()
        else:
            # There are still some messages to be displayed, so we queue this
            # up.
            log.statusbar.debug("queueing")
            self._text_queue.append((error, text))
            self._text_pop_timer.start()

    @pyqtSlot(str, bool)
    def disp_error(self, text, immediately=False):
        """Display an error in the statusbar.

        Args:
            text: The message to display.
            immediately: If set, message gets displayed immediately instead of
                         queued.
        """
        self._disp_text(text, True, immediately)

    @pyqtSlot(str, bool)
    def disp_temp_text(self, text, immediately):
        """Display a temporary text in the statusbar.

        Args:
            text: The message to display.
            immediately: If set, message gets displayed immediately instead of
                         queued.
        """
        self._disp_text(text, False, immediately)

    @pyqtSlot(str)
    def set_text(self, val):
        """Set a normal (persistent) text in the status bar."""
        self.txt.set_text(self.txt.Text.normal, val)

    @pyqtSlot(usertypes.KeyMode)
    def on_mode_entered(self, mode):
        """Mark certain modes in the commandline."""
        mode_manager = objreg.get('mode-manager', scope='window',
                                  window=self._win_id)
        if mode in mode_manager.passthrough:
            self._set_mode_text(mode.name)
        if mode == usertypes.KeyMode.insert:
            self._set_insert_active(True)

    @pyqtSlot(usertypes.KeyMode, usertypes.KeyMode)
    def on_mode_left(self, old_mode, new_mode):
        """Clear marked mode."""
        mode_manager = objreg.get('mode-manager', scope='window',
                                  window=self._win_id)
        if old_mode in mode_manager.passthrough:
            if new_mode in mode_manager.passthrough:
                self._set_mode_text(new_mode.name)
            else:
                self.txt.set_text(self.txt.Text.normal, '')
        if old_mode == usertypes.KeyMode.insert:
            self._set_insert_active(False)

    @config.change_filter('ui', 'message-timeout')
    def set_pop_timer_interval(self):
        """Update message timeout when config changed."""
        self._text_pop_timer.setInterval(config.get('ui', 'message-timeout'))

    def resizeEvent(self, e):
        """Extend resizeEvent of QWidget to emit a resized signal afterwards.

        Args:
            e: The QResizeEvent.
        """
        super().resizeEvent(e)
        self.resized.emit(self.geometry())

    def moveEvent(self, e):
        """Extend moveEvent of QWidget to emit a moved signal afterwards.

        Args:
            e: The QMoveEvent.
        """
        super().moveEvent(e)
        self.moved.emit(e.pos())

    def minimumSizeHint(self):
        """Set the minimum height to the text height plus some padding."""
        width = super().minimumSizeHint().width()
        height = self.fontMetrics().height() + 3
        return QSize(width, height)
