# AudioSwitch

A Windows utility for instantly switching audio output between two input sources (A/B), with a global hotkey toggle and on-screen overlay.
New to 1.1: Added an optional push to talk system.

## Requirements

- Windows 10/11
- `numpy`, `sounddevice`, `pynput`, `pycaw`, `comtypes`

## Run from source

```bash
pip install numpy sounddevice pynput pycaw comtypes
python AudioSwitch.py
```

## Build

```bash
pyinstaller --onefile --windowed --name AudioSwitch --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 --hidden-import comtypes.stream AudioSwitch.py
```

## License

MIT — see [LICENSE](LICENSE).
