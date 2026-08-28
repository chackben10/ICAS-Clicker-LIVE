from __future__ import annotations

import subprocess


def run_applescript(script: str, timeout: float = 30) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "AppleScript failed")
    return result.stdout.strip()


def navigate_production_hub_pages(page_names: list[str]) -> list[str]:
    quoted = ", ".join(f'"{name.replace(chr(34), "")}"' for name in page_names)
    script = f'''
tell application "System Events"
  tell process "Production Hub"
    set frontmost to true
    set visitedPages to {{}}
    repeat with pageName in {{{quoted}}}
      click menu item (pageName as text) of menu "Navigate" of menu bar 1
      delay 0.15
      if not (exists window 1) then error "Production Hub window disappeared"
      set actionableCount to 0
      repeat with uiItem in entire contents of window 1
        try
          set itemRole to role of uiItem
          if enabled of uiItem is true and itemRole is in {{"AXButton", "AXCheckBox", "AXPopUpButton", "AXTextField", "AXComboBox", "AXRadioButton", "AXSlider"}} then
            set actionableCount to actionableCount + 1
          end if
        end try
      end repeat
      if actionableCount is 0 then error "No enabled actionable controls on " & (pageName as text)
      set end of visitedPages to pageName as text
    end repeat
    return visitedPages
  end tell
end tell
'''
    output = run_applescript(script, timeout=max(30, len(page_names) * 2))
    return [item.strip() for item in output.split(",") if item.strip()]


def enabled_button_names() -> list[str]:
    script = '''
tell application "System Events"
  tell process "Production Hub"
    set frontmost to true
    set buttonNames to {}
    repeat with uiItem in entire contents of window 1
      try
        if role of uiItem is "AXButton" and enabled of uiItem is true then
          set buttonName to name of uiItem
          if buttonName is missing value or buttonName is "" then
            set end of buttonNames to "<missing>"
          else
            set end of buttonNames to buttonName as text
          end if
        end if
      end try
    end repeat
    return buttonNames
  end tell
end tell
'''
    output = run_applescript(script)
    return [item.strip() for item in output.split(",") if item.strip()]
