#!/usr/bin/env python
# License: GPLv3 Copyright: 2020, Kovid Goyal <kovid at kovidgoyal.net>

from typing import TYPE_CHECKING, Optional

from .base import (
    MATCH_TAB_OPTION, ArgsType, Boss, PayloadGetType, PayloadType, RCOptions,
    RemoteCommand, ResponseType, Window
)

if TYPE_CHECKING:
    from kitty.cli_stub import NewWindowRCOptions as CLIOptions


class NewWindow(RemoteCommand):

    '''
    args+: The command line to run in the new window, as a list, use an empty list to run the default shell
    match: The tab to open the new window in
    title: Title for the new window
    cwd: Working directory for the new window
    keep_focus: Boolean indicating whether the current window should retain focus or not
    window_type: One of :code:`kitty` or :code:`os`
    new_tab: Boolean indicating whether to open a new tab
    tab_title: Title for the new tab
    no_response: Boolean indicating whether to send back the window id
    '''

    short_desc = 'Open new window'
    desc = (
        'DEPRECATED: Use the launch command instead.\n\n'
        'Open a new window in the specified tab. If you use the :option:`kitty @ new-window --match` option'
        ' the first matching tab is used. Otherwise the currently active tab is used.'
        ' Prints out the id of the newly opened window'
        ' (unless :option:`--no-response` is used). Any command line arguments'
        ' are assumed to be the command line used to run in the new window, if none'
        ' are provided, the default shell is run. For example:\n'
        ':code:`kitty @ new-window --title Email mutt`'
    )
    options_spec = MATCH_TAB_OPTION + '''\n
--title
The title for the new window. By default it will use the title set by the
program running in it.


--cwd
The initial working directory for the new window. Defaults to whatever
the working directory for the kitty process you are talking to is.


--keep-focus --dont-take-focus
type=bool-set
Keep the current window focused instead of switching to the newly opened window.


--window-type
default=kitty
choices=kitty,os
What kind of window to open. A kitty window or a top-level OS window.


--new-tab
type=bool-set
Open a new tab.


--tab-title
Set the title of the tab, when open a new tab.


--no-response
type=bool-set
default=false
Don't wait for a response giving the id of the newly opened window. Note that
using this option means that you will not be notified of failures and that
the id of the new window will not be printed out.
'''
    argspec = '[CMD ...]'

    def message_to_kitty(self, global_opts: RCOptions, opts: 'CLIOptions', args: ArgsType) -> PayloadType:
        ans = {'args': args or [], 'type': 'window'}
        for attr, val in opts.__dict__.items():
            if attr == 'new_tab':
                if val:
                    ans['type'] = 'tab'
            elif attr == 'window_type':
                if val == 'os' and ans['type'] != 'tab':
                    ans['type'] = 'os-window'
            else:
                ans[attr] = val
        return ans

    def response_from_kitty(self, boss: Boss, window: Optional[Window], payload_get: PayloadGetType) -> ResponseType:
        from .launch import launch
        return launch.response_from_kitty(boss, window, payload_get)


new_window = NewWindow()
