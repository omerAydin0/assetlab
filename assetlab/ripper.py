"""Drive AssetRipper headlessly through its local HTTP API.

AssetRipper.GUI.Free hosts a documented web API (see /openapi.json) and takes
``--headless --port``, so the one manual GUI step in the chain can be scripted:

    settings -> POST /Settings/Update
    load     -> POST /LoadFolder      {path}
    export   -> POST /Export/UnityProject {path}

The settings AssetLab depends on are enforced here rather than trusted. Sprite YAML
and .meta GUIDs are what make sprite slicing and the reference graph possible at
all, and `ScriptExportMode` decides whether the script vocabulary exists: on the
installation default the export quietly writes assemblies instead of C# and
classification loses every name it would have read.

Only stdlib is used, so this adds no dependency.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

#: Where the executable is looked for when none is given. An installation this
#: does not know about is named with --exe or ASSETRIPPER_EXE rather than guessed.
EXE_ENV = "ASSETRIPPER_EXE"
EXE_NAME = "AssetRipper.GUI.Free.exe"
EXE_HINTS = (
    Path("AssetRipper") / EXE_NAME,
    Path.home() / "Desktop" / "AssetRipper" / EXE_NAME,
    Path.home() / "AssetRipper" / EXE_NAME,
    Path("C:/Program Files/AssetRipper") / EXE_NAME,
)


def find_exe(given: Path | None = None) -> Path | None:
    """The AssetRipper executable, or None - in which case a running one may serve."""
    if given:
        return given if Path(given).is_file() else None
    from_env = os.environ.get(EXE_ENV)
    if from_env and Path(from_env).is_file():
        return Path(from_env)
    for hint in EXE_HINTS:
        if hint.is_file():
            return hint
    found = shutil.which(EXE_NAME) or shutil.which("AssetRipper.GUI.Free")
    return Path(found) if found else None

REQUIRED_SETTINGS = {
    "BundledAssetsExportMode": "DirectExport",
    "SpriteExportMode": "Yaml",
    "ImageExportFormat": "Png",
    "AudioExportFormat": "Default",
    # Without this the export writes assemblies instead of C#, so there is no
    # enum or type vocabulary for classification to read - and nothing fails
    # while it happens. `Hybrid`, the installation default, is exactly that case.
    "ScriptExportMode": "Decompiled",
    # Method bodies, which is where mechanic names appear when a build declares
    # them in code rather than in an enum.
    "ScriptContentLevel": "Level2",
}

SELECT_RE = re.compile(r'<select[^>]*\bname="([^"]+)"[^>]*>(.*?)</select>', re.S)
SELECTED_RE = re.compile(r'<option[^>]*\bvalue="([^"]*)"[^>]*\bselected', re.S)
TEXT_INPUT_RE = re.compile(r'<input[^>]*\btype="text"[^>]*>', re.S)
CHECKBOX_RE = re.compile(r'<input[^>]*\btype="checkbox"[^>]*>', re.S)
NAME_ATTR_RE = re.compile(r'\bname="([^"]+)"')
VALUE_ATTR_RE = re.compile(r'\bvalue="([^"]*)"')


class RipperError(RuntimeError):
    pass


class Ripper:
    """A headless AssetRipper instance, started here or already running."""

    def __init__(self, exe: Path | None = None, port: int = 5599,
                 base_url: str | None = None) -> None:
        self.exe = Path(exe) if exe else None
        self.port = port
        self.base_url = base_url or f"http://127.0.0.1:{port}"
        self.process: subprocess.Popen | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self, timeout: float = 90.0) -> "Ripper":
        if self.is_up():
            return self
        if not self.exe or not self.exe.is_file():
            raise RipperError(
                f"AssetRipper is not running on {self.base_url} and no --exe was given")
        self.process = subprocess.Popen(
            [str(self.exe), "--headless", "--port", str(self.port)],
            cwd=str(self.exe.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_up():
                return self
            if self.process.poll() is not None:
                raise RipperError("AssetRipper exited during startup")
            time.sleep(1.0)
        raise RipperError(f"AssetRipper did not answer on {self.base_url} within {timeout}s")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None

    def __enter__(self) -> "Ripper":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    def is_up(self) -> bool:
        try:
            self._get("/", timeout=3)
            return True
        except (urllib.error.URLError, OSError, RipperError):
            return False

    # -- transport ---------------------------------------------------------
    def _get(self, path: str, timeout: float = 60) -> str:
        with urllib.request.urlopen(self.base_url + path, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")

    def _post(self, path: str, fields: dict[str, str], timeout: float = 60) -> int:
        data = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method="POST")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status
        except urllib.error.HTTPError as error:
            if error.code in (301, 302, 303):  # a normal form redirect
                return error.code
            raise RipperError(f"POST {path} failed: {error.code} {error.reason}") from error

    # -- operations --------------------------------------------------------
    def read_settings(self) -> dict[str, str]:
        """Parse the settings form so an update can preserve untouched fields.

        AssetRipper serves a stub page saying "Settings can only be changed before
        loading files" once a build is loaded, so callers must Reset first.
        """
        html = self._get("/Settings/Edit")
        if "<select" not in html:
            raise RipperError(
                "settings are locked because a build is already loaded; call reset() first")
        settings: dict[str, str] = {}
        for name, body in SELECT_RE.findall(html):
            chosen = SELECTED_RE.search(body)
            if chosen:
                settings[name] = chosen.group(1)
        for tag in TEXT_INPUT_RE.findall(html):
            name = NAME_ATTR_RE.search(tag)
            value = VALUE_ATTR_RE.search(tag)
            if name:
                settings[name] = value.group(1) if value else ""
        for tag in CHECKBOX_RE.findall(html):
            name = NAME_ATTR_RE.search(tag)
            if name and "checked" in tag:
                settings[name] = "on"
        return settings

    def configure(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        """Apply AssetLab's required settings on top of the current ones."""
        settings = self.read_settings()
        settings.update(REQUIRED_SETTINGS)
        settings.update(overrides or {})
        self._post("/Settings/Update", settings)
        applied = self.read_settings()
        wrong = {key: applied.get(key) for key, want in REQUIRED_SETTINGS.items()
                 if applied.get(key) != want}
        if wrong:
            raise RipperError(f"settings did not stick: {wrong}")
        return applied

    def load_folder(self, path: Path, timeout: float = 3600) -> list[str]:
        self._post("/LoadFolder", {"path": str(Path(path).resolve())}, timeout=timeout)
        return self.loaded_bundles()

    def loaded_bundles(self) -> list[str]:
        """Names at the root of the loaded GameBundle.

        `/Collections/Count` is not a global counter - it needs a `Path` query and
        404s without one - so the root bundle listing is what tells us a load
        actually produced something.
        """
        query = urllib.parse.quote(json.dumps({"P": []}))
        try:
            html = self._get(f"/Bundles/View?Path={query}", timeout=120)
        except (RipperError, urllib.error.URLError):
            return []
        body = html.split("</header>")[-1]
        return [re.sub(r"<[^>]+>", "", anchor).strip()
                for anchor in re.findall(
                    r'<a href="/(?:Bundles|Collections)/View\?Path=[^"]*"[^>]*>.*?</a>',
                    body, re.S)]

    def failed_files(self) -> list[str]:
        try:
            html = self._get("/FailedFiles/View")
        except (RipperError, urllib.error.URLError):
            return []
        return [re.sub(r"<[^>]+>", "", cell).strip()
                for cell in re.findall(r"<td[^>]*>.*?</td>", html, re.S)]

    def export_unity_project(self, out_dir: Path, timeout: float = 14400) -> Path:
        out_dir = Path(out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        self._post("/Export/UnityProject", {"path": str(out_dir)}, timeout=timeout)
        exported = out_dir / "ExportedProject" / "Assets"
        if not exported.is_dir():
            raise RipperError(f"export finished but {exported} does not exist")
        return exported

    def reset(self) -> None:
        self._post("/Reset", {})


def rip(input_dir: Path, out_dir: Path, exe: Path | None, port: int,
        keep_running: bool = False) -> Path:
    ripper = Ripper(exe, port)
    started_here = not ripper.is_up()
    ripper.start()
    try:
        # Settings are only editable with nothing loaded, and a reused instance may
        # still hold a previous game.
        ripper.reset()
        applied = ripper.configure()
        print("settings   " + ", ".join(f"{k}={applied[k]}" for k in REQUIRED_SETTINGS))
        print(f"loading    {input_dir}", flush=True)
        started = time.time()
        bundles = ripper.load_folder(input_dir)
        print(f"loaded     {len(bundles)} root bundles in {time.time() - started:.0f}s"
              + (f" e.g. {', '.join(bundles[:5])}" if bundles else ""))
        if not bundles:
            raise RipperError("nothing loaded - is this really a Unity build tree?")
        failed = ripper.failed_files()
        if failed:
            print(f"           {len(failed)} file(s) failed to parse (see /FailedFiles/View)")
        print(f"exporting  {out_dir}", flush=True)
        started = time.time()
        exported = ripper.export_unity_project(out_dir)
        print(f"exported   in {time.time() - started:.0f}s -> {exported}")
        return exported
    finally:
        if started_here and not keep_running:
            ripper.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run AssetRipper headlessly and produce a Unity Project export.")
    parser.add_argument("--input", required=True, type=Path,
                        help="staged input tree (assetlab.ingest output 'input' folder)")
    parser.add_argument("--out", required=True, type=Path, help="export destination")
    parser.add_argument("--exe", type=Path, default=None,
                        help="AssetRipper.GUI.Free.exe; found automatically when it "
                             "sits in a usual place, and ignored entirely if one is "
                             "already running on --port")
    parser.add_argument("--port", type=int, default=5599)
    parser.add_argument("--keep-running", action="store_true",
                        help="leave AssetRipper up afterwards (useful for several games)")
    args = parser.parse_args()

    if not args.input.is_dir():
        parser.error(f"input directory not found: {args.input}")
    exported = rip(args.input, args.out, find_exe(args.exe), args.port,
                   args.keep_running)
    print(f"\nNext: python -m assetlab.run --export \"{exported}\" --out \"out/<name>\"")


if __name__ == "__main__":
    main()
