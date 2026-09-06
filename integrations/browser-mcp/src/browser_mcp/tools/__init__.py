"""Register browser MCP tools by importing each group for decorator side effects."""

from __future__ import annotations

from browser_mcp.tools.batch import browser_act
from browser_mcp.tools.capture import browser_screenshot
from browser_mcp.tools.interact import (
    browser_click,
    browser_click_by_index,
    browser_click_text,
    browser_fill,
    browser_input_by_index,
    browser_press_key,
    browser_scroll,
    browser_select_by_index,
    browser_upload,
    browser_wait_for,
    browser_wait_idle,
)
from browser_mcp.tools.playbook import (
    browser_list_playbooks,
    browser_read_playbook,
    browser_run_playbook,
    browser_write_playbook,
)
from browser_mcp.tools.navigate import browser_goto
from browser_mcp.tools.read import (
    browser_extract,
    browser_get_elements,
    browser_get_text,
    browser_js,
    browser_page_info,
)
from browser_mcp.tools.tab_control import (
    browser_close_tab,
    browser_login,
    browser_open_tab,
    browser_passkey,
    browser_read_tabs,
    browser_stop,
    browser_switch_tab,
    browser_tabs,
)

__all__ = [
    "browser_act",
    "browser_click",
    "browser_click_by_index",
    "browser_click_text",
    "browser_close_tab",
    "browser_extract",
    "browser_fill",
    "browser_get_elements",
    "browser_get_text",
    "browser_goto",
    "browser_input_by_index",
    "browser_js",
    "browser_list_playbooks",
    "browser_login",
    "browser_open_tab",
    "browser_page_info",
    "browser_passkey",
    "browser_press_key",
    "browser_read_playbook",
    "browser_read_tabs",
    "browser_run_playbook",
    "browser_screenshot",
    "browser_scroll",
    "browser_select_by_index",
    "browser_stop",
    "browser_switch_tab",
    "browser_tabs",
    "browser_upload",
    "browser_wait_for",
    "browser_wait_idle",
    "browser_write_playbook",
]
