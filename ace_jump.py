from __future__ import annotations

import abc
import re
from typing import Any, Generator, Iterable, TypeVar

import sublime
import sublime_plugin

from .libs import char_width_converter
from .libs.xpinyin import Pinyin

assert __package__

_T = TypeVar("_T")

PLUGIN_NAME = __package__.partition(".")[0]

SETTINGS_FILENAME = "AceJump.sublime-settings"
SYTNAX_FILENAME = f"Packages/{PLUGIN_NAME}/AceJump.sublime-syntax"
XPINYIN_DICT_PATH = f"Packages/{PLUGIN_NAME}/libs/xpinyin/Mandarin.dat"

CHINESE_REGEX_OBJ = re.compile("[\u4e00-\u9fd5]+", re.UNICODE)

PHANTOM_TEMPLATE = """
<body class="ace-jump-phantom">
    <style>{css}</style>
    <span class="label">{label}</span>
</body>
"""

# plugin modes
MODE_ADD_CURSOR = 0
MODE_SELECT = 1
MODE_JUMP_BEFORE = 2
MODE_JUMP_AFTER = 3
MODE_DEFAULT = MODE_JUMP_BEFORE

# plugin hinting modes
HINTING_MODE_REPLACE_CHAR = 1
HINTING_MODE_INLINE_PHANTOM = 2
HINTING_MODE_DEFAULT = HINTING_MODE_REPLACE_CHAR

xpy: Pinyin | None = None
mode = MODE_DEFAULT
last_index = 0
hints: list[sublime.Region] = []
phantom_sets: dict[int, sublime.PhantomSet] = {}

next_search: int | bool = False
ace_jump_active = False


def plugin_loaded() -> None:
    init_xpy()


def init_xpy() -> None:
    global xpy

    pinyin_map: dict[str, str] = {}
    for line_num0, line in enumerate(sublime.load_resource(XPINYIN_DICT_PATH).splitlines()):
        try:
            k, v = line.split("\t")
            pinyin_map[k] = v
        except ValueError:
            print_msg(f"Malformed pinyin data line {line_num0 + 1}: `{line}`")

    xpy = Pinyin(pinyin_map)


def only_truthy(iterable: Iterable[_T | None]) -> Generator[_T, None, None]:
    yield from filter(None, iterable)


def get_active_views(window: sublime.Window, current_buffer_only: bool) -> list[sublime.View]:
    """Returns all currently visible views"""
    if current_buffer_only:
        group_indexes = [window.active_group()]
    else:
        group_indexes = list(range(window.num_groups()))

    return list(only_truthy(window.active_view_in_group(idx) for idx in group_indexes))


def set_views_setting(views: list[sublime.View], key: str, view_values: list[Any]) -> None:
    """Sets the value for the setting in all given views"""

    for view, view_value in zip(views, view_values):
        view.settings().set(key, view_value)


def set_views_settings(views: list[sublime.View], keys: list[str], views_values: list[list[Any]]) -> None:
    """Sets the values for all settings in all given views"""

    for key, view_values in zip(keys, views_values):
        set_views_setting(views, key, view_values)


def get_views_setting(views: list[sublime.View], key: str) -> list[Any]:
    """Returns the setting value for all given views"""

    return [view.settings().get(key) for view in views]


def get_views_settings(views: list[sublime.View], keys: list[str]) -> list[list[Any]]:
    """Gets the settings for every given view"""

    return [get_views_setting(views, key) for key in keys]


def set_views_syntax(views: list[sublime.View], syntaxes: str | list[str]) -> None:
    """Sets the syntax highlighting for all given views"""

    if not syntaxes:
        return

    if isinstance(syntaxes, str):
        syntaxes = [syntaxes]

    for i in range(len(views)):
        try:
            syntax = syntaxes[i]
        except IndexError:
            syntax = syntaxes[-1]

        views[i].assign_syntax(syntax)


def set_views_sel(views: list[sublime.View], selections: list[sublime.Selection]) -> None:
    """Sets the selections for all given views"""

    for view, selection in zip(views, selections):
        for region in selection:
            view.sel().add(region)


def get_views_sel(views: list[sublime.View]) -> list[sublime.Selection]:
    """Returns the current selection for each from the given views"""

    return [view.sel() for view in views]


def get_view_phantom_set(view: sublime.View) -> sublime.PhantomSet:
    return phantom_sets.setdefault(view.id(), sublime.PhantomSet(view))


def set_plugin_mode(_mode: int) -> None:
    global mode

    mode = _mode

    if mode == MODE_ADD_CURSOR:
        msg = "AceJump (add cursor)"
    elif mode == MODE_SELECT:
        msg = "AceJump (select)"
    elif mode == MODE_JUMP_BEFORE:
        msg = "AceJump (jump before)"
    elif mode == MODE_JUMP_AFTER:
        msg = "AceJump (jump after)"
    else:
        msg = ""

    sublime.status_message(msg)


def print_msg(msg: str) -> None:
    print(f"[{PLUGIN_NAME}] {msg}")


# set the default plugin mode
set_plugin_mode(MODE_DEFAULT)


class AceJumpCommand(sublime_plugin.WindowCommand):
    """Base command class for AceJump plugin"""

    def run(self, current_buffer_only: bool = False) -> None:
        global ace_jump_active
        ace_jump_active = True

        self.char = ""
        self.target = ""
        self.views: list[sublime.View] = []
        self.changed_views: list[sublime.View] = []
        self.breakpoints: list[int] = []

        self.all_views = get_active_views(self.window, current_buffer_only)
        self.syntax: list[str] = get_views_setting(self.all_views, "syntax")
        self.sel = get_views_sel(self.all_views)

        settings = sublime.load_settings(SETTINGS_FILENAME)
        self.labels_scope: str = settings.get("labels_scope", "invalid")
        self.inactive_carets_scope: str = settings.get("inactive_carets_scope", "text.plain")
        self.labels: str = settings.get("labels", "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
        self.case_sensitivity: bool = settings.get("search_case_sensitivity", True)
        self.jump_behind_last: bool = settings.get("jump_behind_last_characters", False)
        self.save_files_after_jump: bool = settings.get("save_files_after_jump", False)
        self.hinting_mode: int = settings.get("hinting_mode", HINTING_MODE_DEFAULT)

        self.view_settings_keys: list[Any] = settings.get("view_settings_keys", [])
        self.view_settings_values = get_views_settings(self.all_views, self.view_settings_keys)

        self.show_prompt(self.prompt(), self.init_value())

    def is_enabled(self) -> bool:
        return not ace_jump_active

    def show_prompt(self, title: str, value: str) -> None:
        """Shows a prompt with the given title and value in the window"""

        self.window.show_input_panel(title, value, self.next_batch, self.on_input, self.submit)
        self.add_faked_carets(self.all_views)

    def next_batch(self, command: str) -> None:
        """Displays the next batch of labels after pressing return"""

        self.remove_artifacts()
        self.show_prompt(self.prompt(), self.char)

    def on_input(self, command: str) -> None:
        """Fires the necessary actions for the current input"""

        if len(command) == 1:
            self.char = command
            if self.char in "<>":
                # re.escape escapes these 2 characters but it isn't needed for view.find()
                self.add_labels(self.regex().format(self.char))
            else:
                self.add_labels(self.regex().format(re.escape(self.char)))
            return

        if len(command) == 2:
            self.target = command[1]

        self.window.run_command("hide_panel", {"cancel": True})

    def submit(self) -> None:
        """Handles the behavior after closing the prompt"""
        global next_search, ace_jump_active
        next_search = False

        self.remove_artifacts()
        set_views_sel(self.all_views, self.sel)
        set_views_syntax(self.all_views, self.syntax)

        if self.valid_target(self.target):
            self.jump(self.labels.find(self.target))

        set_plugin_mode(MODE_DEFAULT)
        ace_jump_active = False

        """Saves changed views after jump is complete"""
        if self.save_files_after_jump:
            for view in self.changed_views:
                if not view.is_read_only() and not view.is_dirty():
                    view.run_command("save")

    def add_faked_carets(self, views: list[sublime.View]) -> None:
        """
        After showing the prompt, we lose the view focus and can't see existing carets.
        Hence here, we use add_regions() to mimic existing carets.
        """

        for view in views:
            view.add_regions(
                "ace_jump_faked_carets",
                [sublime.Region(region.b) for region in view.sel()],
                self.inactive_carets_scope,
                flags=sublime.DRAW_EMPTY | sublime.DRAW_NO_FILL,
            )

    def add_labels(self, regex: str) -> None:
        """Adds labels to characters matching the regex"""

        global last_index

        last_index = 0
        hints.clear()

        self.views = self.views_to_label()
        self.region_type = self.get_region_type()
        self.changed_views = []
        self.breakpoints = []
        changed_buffers: list[int] = []

        for view in self.views.copy():
            if view.buffer_id() in changed_buffers:
                break

            view.run_command(
                "add_ace_jump_labels",
                {
                    "regex": regex,
                    "region_type": self.region_type,
                    "labels": self.labels,
                    "labels_scope": self.labels_scope,
                    "case_sensitive": self.case_sensitivity,
                },
            )
            self.breakpoints.append(last_index)
            self.changed_views.append(view)
            changed_buffers.append(view.buffer_id())

            if next_search:
                break

            self.views.remove(view)

        if self.hinting_mode == HINTING_MODE_REPLACE_CHAR:
            set_views_syntax(self.all_views, SYTNAX_FILENAME)
            set_views_settings(self.all_views, self.view_settings_keys, self.view_settings_values)

    def remove_labels(self) -> None:
        """Removes all previously added labels"""

        last_breakpoint = 0
        for breakpoint in self.breakpoints:
            if breakpoint != last_breakpoint:
                view = self.changed_views[self.view_for_index(breakpoint - 1)]
                view.run_command("remove_ace_jump_labels")
                last_breakpoint = breakpoint

    def remove_faked_carets(self) -> None:
        """Removes all previously added faked carets"""

        for view in self.all_views:
            view.erase_regions("ace_jump_faked_carets")

    def remove_artifacts(self) -> None:
        self.remove_labels()
        self.remove_faked_carets()

    def jump(self, index: int) -> None:
        """Performs the jump action"""

        region = hints[index].begin()
        view = self.changed_views[self.view_for_index(index)]

        self.window.focus_view(view)
        view.run_command("perform_ace_jump", {"target": region})
        self.after_jump(view)

    def views_to_label(self) -> list[sublime.View]:
        """Returns the views that still have to be labeled"""

        if mode != MODE_DEFAULT:
            return [view] if (view := self.window.active_view()) else []

        return self.all_views[:] if len(self.views) == 0 else self.views

    def view_for_index(self, index: int) -> int:
        """Returns a view index for the given label index"""

        for breakpoint in self.breakpoints:
            if index < breakpoint:
                return self.breakpoints.index(breakpoint)

        return -1

    def valid_target(self, target: str) -> bool:
        """Check if jump target is valid"""

        index = self.labels.find(target)

        return target != "" and index >= 0 and index < last_index

    def get_region_type(self) -> str:
        """Return region type for labeling"""

        return "visible_region"

    @abc.abstractmethod
    def prompt(self) -> str:
        return ""

    @abc.abstractmethod
    def init_value(self) -> str:
        return ""

    @abc.abstractmethod
    def regex(self) -> str:
        return r""

    @abc.abstractmethod
    def after_jump(self, view: sublime.View) -> None:
        pass


class AceJumpWordCommand(AceJumpCommand):
    """Specialized command for word-mode"""

    def prompt(self) -> str:
        return "Head char"

    def init_value(self) -> str:
        return ""

    def regex(self) -> str:
        return r"\b{}"

    def after_jump(self, view: sublime.View) -> None:
        if mode == MODE_JUMP_AFTER:
            view.run_command("move", {"by": "word_ends", "forward": True})
            set_plugin_mode(MODE_DEFAULT)


class AceJumpCharCommand(AceJumpCommand):
    """Specialized command for char-mode"""

    def prompt(self) -> str:
        return "Char"

    def init_value(self) -> str:
        return ""

    def regex(self) -> str:
        return r"{}"

    def after_jump(self, view: sublime.View) -> None:
        if mode == MODE_JUMP_AFTER:
            view.run_command("move", {"by": "characters", "forward": True})
            set_plugin_mode(MODE_DEFAULT)

    def jump(self, index: int) -> None:
        view = self.changed_views[self.view_for_index(index)]
        if self.jump_behind_last and "\n" in view.substr(hints[index].end()):
            set_plugin_mode(MODE_JUMP_AFTER)

        AceJumpCommand.jump(self, index)


class AceJumpLineCommand(AceJumpCommand):
    """Specialized command for line-mode"""

    def prompt(self) -> str:
        return ""

    def init_value(self) -> str:
        return " "

    def regex(self) -> str:
        return r"(.*)[^\s](.*)\n"

    def after_jump(self, view: sublime.View) -> None:
        if mode == MODE_JUMP_AFTER:
            view.run_command("move", {"by": "lines", "forward": True})
            view.run_command("move", {"by": "characters", "forward": False})
            set_plugin_mode(MODE_DEFAULT)


class AceJumpWithinLineCommand(AceJumpCommand):
    """Specialized command for within-line-mode"""

    def prompt(self) -> str:
        return ""

    def init_value(self) -> str:
        return " "

    def regex(self) -> str:
        return r"\b\w"

    def after_jump(self, view: sublime.View) -> None:
        if mode == MODE_JUMP_AFTER:
            view.run_command("move", {"by": "word_ends", "forward": True})
            set_plugin_mode(MODE_DEFAULT)

    def get_region_type(self) -> str:
        return "current_line"


class AceJumpSelectCommand(sublime_plugin.WindowCommand):
    """Command for turning on select mode"""

    def run(self) -> None:
        set_plugin_mode(MODE_DEFAULT if mode == MODE_SELECT else MODE_SELECT)


class AceJumpAddCursorCommand(sublime_plugin.WindowCommand):
    """Command for turning on multiple cursor mode"""

    def run(self) -> None:
        set_plugin_mode(MODE_DEFAULT if mode == MODE_ADD_CURSOR else MODE_ADD_CURSOR)


class AceJumpAfterCommand(sublime_plugin.WindowCommand):
    """Modifier-command which lets you jump behind a character, word or line"""

    def run(self) -> None:
        set_plugin_mode(MODE_DEFAULT if mode == MODE_JUMP_AFTER else MODE_JUMP_AFTER)


class AddAceJumpLabelsCommand(sublime_plugin.TextCommand):
    """Command for adding labels to the views"""

    def run(
        self, edit: sublime.Edit, regex: str, region_type: str, labels: str, labels_scope: str, case_sensitive: bool
    ) -> None:
        global hints

        settings = sublime.load_settings(SETTINGS_FILENAME)
        self.should_find_chinese: bool = settings.get("should_find_chinese", True)
        self.hinting_mode: int = settings.get("hinting_mode", HINTING_MODE_DEFAULT)
        self.phantom_css: str = settings.get("phantom_css", "")

        characters = self.find(regex, region_type, len(labels), case_sensitive)
        self.add_labels(edit, characters, labels)

        if self.hinting_mode == HINTING_MODE_REPLACE_CHAR:
            self.view.add_regions("ace_jump_hints", characters, labels_scope)

        hints += characters

    def find(self, regex: str, region_type: str, max_labels: int, case_sensitive: bool) -> list[sublime.Region]:
        """Returns a list with all occurences matching the regex"""

        global next_search, last_index

        found_regions: list[sublime.Region] = []

        region = self.get_target_region(region_type)
        content = self.view.substr(region)
        next_search = next_search or region.begin()
        last_search = region.end()

        if self.should_find_chinese:
            # 測試用句子：如果方法中若传入变量，那么直接加前缀是不可以了。而是要将变量转为utf-8编码
            # find matched Chinese chars from the target region
            matched_chinese_chars = set()
            for match in CHINESE_REGEX_OBJ.finditer(content):
                chinese_string = content[slice(*match.span())]

                assert xpy
                for idx, char_pinyin in enumerate(xpy.get_pinyin(chinese_string, "-").split("-")):
                    if re.match(regex, char_pinyin[0]):
                        matched_chinese_chars.add(chinese_string[idx])

            # add matched Chinese chars into the search regex which is used later
            if matched_chinese_chars:
                regex += r"|[{}]".format("".join(matched_chinese_chars))

        while next_search < last_search and last_index < max_labels:
            word = self.view.find(regex, next_search, 0 if case_sensitive else sublime.IGNORECASE)

            if not word or word.end() > last_search:
                break

            last_index += 1
            next_search = word.end()
            found_regions.append(sublime.Region(word.begin(), word.begin() + 1))

        if last_index < max_labels:
            next_search = False

        return found_regions

    def add_labels(self, edit: sublime.Edit, regions: list[sublime.Region], labels: str) -> None:
        """Replaces the given regions with labels"""

        phantoms = []  # List[sublime.Phantom]

        for idx, region in enumerate(regions):
            label = labels[last_index + idx - len(regions)]

            if self.hinting_mode == HINTING_MODE_REPLACE_CHAR:
                # if the target char is Chinese,
                # use full-width label to prevent from content position shifting
                if CHINESE_REGEX_OBJ.match(self.view.substr(region)):
                    label = char_width_converter.h2f(label)

                self.view.replace(edit, region, label)
            elif self.hinting_mode == HINTING_MODE_INLINE_PHANTOM:
                phantoms.append(
                    sublime.Phantom(
                        region,
                        PHANTOM_TEMPLATE.format(css=self.phantom_css, label=label),
                        sublime.LAYOUT_INLINE,
                    )
                )

        ps = get_view_phantom_set(self.view)
        ps.update(phantoms)

    def get_target_region(self, region_type: str) -> sublime.Region:
        if region_type == "visible_region":
            return self.view.visible_region()
        if region_type == "current_line":
            return self.view.line(self.view.sel()[0])
        raise ValueError(f"Invalid region type: {region_type}")


class RemoveAceJumpLabelsCommand(sublime_plugin.TextCommand):
    """Command for removing labels from the views"""

    def run(self, edit: sublime.Edit) -> None:
        settings = sublime.load_settings(SETTINGS_FILENAME)
        self.hinting_mode: bool = settings.get("hinting_mode", HINTING_MODE_DEFAULT)

        if self.hinting_mode == HINTING_MODE_REPLACE_CHAR:
            self.view.erase_regions("ace_jump_hints")
            self.view.end_edit(edit)
            self.view.run_command("undo")
        elif self.hinting_mode == HINTING_MODE_INLINE_PHANTOM:
            ps = get_view_phantom_set(self.view)
            ps.update([])


class PerformAceJumpCommand(sublime_plugin.TextCommand):
    """Command performing the jump"""

    def run(self, edit: sublime.Edit, target: int) -> None:
        if mode == MODE_JUMP_BEFORE or mode == MODE_JUMP_AFTER:
            self.view.sel().clear()

        self.view.sel().add(self.target_region(target))
        self.view.show(target)

    def target_region(self, target: int) -> sublime.Region:
        if mode == MODE_SELECT:
            for cursor in self.view.sel():
                return sublime.Region(cursor.begin(), target)

        return sublime.Region(target)
