from concurrent.futures import ThreadPoolExecutor
import hashlib
import requests
import questionary
import json
import argparse
import os
import sys
import logging
import logging.config
import threading
from tqdm import tqdm
import time
from zipfile import ZipFile, BadZipFile
from urllib.parse import urlparse, ParseResult
from io import BytesIO
from pathlib import Path
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version

MAX_RETRIES = 4


def get_app_version() -> str:
    try:
        return pkg_version("rinthdown")
    except PackageNotFoundError:
        return "unknown"


APP_VERSION = get_app_version()

session = requests.Session()
session.headers.update({
    "User-Agent": f"Aintdev/rinthdown/{APP_VERSION}"
})


def get_thread_count() -> int:
    cpu = os.cpu_count() or 1
    return min(32, max(4, cpu * 4))


class ColoredFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[92m",
        logging.WARNING: "\033[93m",
        logging.ERROR: "\033[91m",
        logging.CRITICAL: "\033[1;91m",
    }

    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, "")
        return f"{color}{super().format(record)}{self.RESET}"


DEFAULT_LOGGING = {
    "version": 1,
    "formatters": {
        "colored": {
            "()": ColoredFormatter,
            "format": "%(asctime)s %(levelname)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "simple": {
            "format": "%(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "colored",
            "level": "DEBUG",
            "stream": sys.stdout,
        }
    },
    "loggers": {
        __name__: {
            "level": "DEBUG",
            "handlers": ["console"],
            "propagate": False,
        },
    },
}

logging.config.dictConfig(DEFAULT_LOGGING)
logger = logging.getLogger(__name__)


def get_mods_path():
    cwd = Path.cwd()

    if cwd.name.lower() == "mods":
        return cwd

    mods_dir = cwd / "mods"
    if mods_dir.is_dir():
        return mods_dir

    possible_path = questionary.path(
        "Please specify the location of the `mods` directory.\n"
        "Warning: additional directories may be created beside the given directory.",
        default=str(mods_dir),
    ).ask()

    if not possible_path:
        raise KeyboardInterrupt

    path = Path(possible_path).expanduser()

    was_rel = False
    if not path.is_absolute():
        path = cwd / path
        was_rel = True

    path = path.resolve()

    if was_rel:
        answer = questionary.confirm(
            f"Did you mean this path?: {path}"
        ).ask()

        if answer is None:
            raise KeyboardInterrupt
        elif not answer:
            return get_mods_path()

    if not path.exists():
        logger.warning("Given path does not exist, creating directory...")
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        raise RinthDownError(f"Given path is not a directory: {path}")

    return path


def hash_file(path):
    h = hashlib.sha512()

    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


class ModrinthPackError(Exception):
    def __init__(self, message="A fatal error has occured."):
        super().__init__(message)


class ModrinthPack:
    def __init__(self, parse: ParseResult):
        self.parse: ParseResult = parse
        self.zip: ZipFile | None = None
        self._recursion_count = 0

        if parse.scheme == "":
            self._load_from_file()
        elif parse.scheme in ("http", "https"):
            self._load_from_bytes(self._download())
        else:
            raise ModrinthPackError("Invalid scheme.")

    def _download(self) -> BytesIO:
        in_memory_zip = BytesIO()

        try:
            with session.get(
                self.parse.geturl(),
                stream=True,
                timeout=10,
            ) as response:
                logger.debug("API-CALL: " + self.parse.geturl())
                response.raise_for_status()

                total_size = int(response.headers.get("content-length", 0))
                block_size = 1024

                logger.info(f"Starting download of {self.parse.path}.")

                with tqdm(
                    total=total_size,
                    unit="B",
                    unit_scale=True,
                ) as progress_bar:
                    for data in response.iter_content(block_size):
                        progress_bar.update(len(data))
                        in_memory_zip.write(data)

                if total_size and in_memory_zip.tell() != total_size:
                    raise RuntimeError("Could not download file")

                return in_memory_zip

        except Exception as e:
            logger.error(f"Something went wrong during the download: {e}")

            if self._recursion_count >= MAX_RETRIES:
                raise ModrinthPackError(
                    "Maximum download retries exceeded."
                ) from e

            retry = questionary.confirm("Try again?").ask()

            if retry is None:
                raise KeyboardInterrupt

            if retry:
                self._recursion_count += 1
                return self._download()

            raise KeyboardInterrupt

    def _load_from_bytes(self, data: BytesIO):
        self.zip = ZipFile(data, "r")

    def _load_from_file(self):
        with open(self.parse.path, "rb") as f:
            self._load_from_bytes(BytesIO(f.read()))

    def read_file(self, path: str):
        if self.zip is None:
            raise ModrinthPackError("Modrinth pack is not loaded.")

        return self.zip.read(path)


class RinthDownError(Exception):
    def __init__(self, message="A fatal error has occured."):
        super().__init__(message)


class RinthDownAPIError(RinthDownError):
    def __init__(self, message="A fatal error has occured."):
        super().__init__(message)


class RinthDownInterrupt(InterruptedError):
    def __init__(self, message="Interrupted."):
        super().__init__(message)


class RinthDown:
    def __init__(self, args):
        self._pack = args.mrpack
        self._server = args.isserver
        self._projects = args.projects
        self._dependencies = []
        self._mcversion = args.mcversion
        self._loader = args.loader
        self._stable = args.stable
        self._latest = args.latest
        self._installed = []
        self._workers = get_thread_count()
        self._outputpath = get_mods_path()
        self._cancel_event = threading.Event()
        self._had_errors = False

    @staticmethod
    def safe_destination(root: Path, relative_path: str) -> Path:
        """Resolve a relative path without allowing directory traversal."""
        normalized = str(relative_path).replace("\\", "/")
        relative = Path(normalized)

        if (
            not normalized
            or relative.is_absolute()
            or ".." in normalized.split("/")
            or "\x00" in normalized
            or (normalized.split("/")[0].endswith(":"))
        ):
            raise RinthDownError(
                f"Unsafe path in modpack: {relative_path!r}"
            )

        root = root.resolve()
        destination = (root / relative).resolve()

        if destination != root and root not in destination.parents:
            raise RinthDownError(
                f"Path escapes the target directory: {relative_path!r}"
            )

        if destination == root:
            raise RinthDownError(
                f"Invalid file path in modpack: {relative_path!r}"
            )

        return destination

    @staticmethod
    def verify_hashes(path: Path, hashes: dict | None) -> bool:
        """Verify a downloaded file against Modrinth-provided hashes."""
        if not hashes:
            return True

        algorithms = {
            "sha512": hashlib.sha512,
            "sha256": hashlib.sha256,
            "sha1": hashlib.sha1,
        }

        for algorithm, expected in hashes.items():
            constructor = algorithms.get(algorithm.lower())

            if constructor is None:
                continue

            digest = constructor()

            with path.open("rb") as file:
                for chunk in iter(
                    lambda: file.read(1024 * 1024), b""
                ):
                    digest.update(chunk)

            if digest.hexdigest().lower() != str(expected).lower():
                return False

        return True

    @staticmethod
    def get_releases():
        url = (
            "https://piston-meta.mojang.com/"
            "mc/game/version_manifest_v2.json"
        )

        response = session.get(url, timeout=10)
        response.raise_for_status()

        return [
            entry["id"]
            for entry in response.json()["versions"]
            if entry["type"] == "release"
        ]

    def calculate_projects(self):
        projects = questionary.text(
            'Mod Name (Split by ", "):'
        ).ask()

        if not projects:
            raise KeyboardInterrupt

        projects = projects.split(", ")
        self._projects = [
            (project, None)
            for project in projects
            if project
        ]

    def calculate_mcversion(self):
        self._mcversion = questionary.autocomplete(
            "Minecraft Version: ",
            RinthDown.get_releases(),
        ).ask()

        if self._mcversion is None:
            raise KeyboardInterrupt

    def calculate_pack(self):
        self._pack = questionary.path(
            "Pack [name/path/download url]: "
        ).ask()

        if self._pack is None:
            raise KeyboardInterrupt

        self._pack = self._pack.strip("\"")

    def calculate_loader(self):
        url = "https://api.modrinth.com/v2/tag/loader"

        response = session.get(url, timeout=10)

        logger.debug("API CALL: " + url)
        response.raise_for_status()

        loaders = response.json()
        loaders = [loader["name"] for loader in loaders]

        self._loader = questionary.autocomplete(
            "What Loader?",
            choices=loaders,
        ).ask()

        if not self._loader:
            raise KeyboardInterrupt

    def calculate_version(self, projectname, versions):
        if not versions:
            return None

        raw_version_choices = []

        latest_stable = None
        latest_overall = None

        for entry in versions:
            try:
                if (
                    self._server
                    and entry["environment"] == "client_only"
                ):
                    continue
            except KeyError:
                logger.critical(
                    "Modrinth's API seems to have changed. "
                    "Please notify developers."
                )
                return None

            raw_version_choices.append(entry)

            if not latest_overall:
                if self._latest:
                    return entry

                latest_overall = entry.copy()

            if (
                not latest_stable
                and entry["version_type"] == "release"
            ):
                if self._stable:
                    return entry

                latest_stable = entry.copy()

        if not raw_version_choices:
            if self._server:
                logger.error(
                    f"No version of project {projectname} "
                    "is server compatible."
                )
            return None

        if latest_stable:
            latest_stable["name"] = (
                "stable (" + latest_stable["name"] + ")"
            )

        if latest_overall:
            latest_overall["name"] = (
                "latest (" + latest_overall["name"] + ")"
            )

        raw_version_choices = (
            ([latest_stable] if latest_stable else [])
            + ([latest_overall] if latest_overall else [])
            + raw_version_choices
        )

        return raw_version_choices[
            self.ask_version(projectname, raw_version_choices)
        ]

    def ask_version(self, projectname, choices):
        prettyversions = [
            str(
                "[STBL] "
                if value["version_type"] == "release"
                else "[BETA] "
            ) + value["name"]
            for value in choices
        ]

        selected = questionary.select(
            f"What version of {projectname}?",
            choices=prettyversions,
        ).ask()

        if not selected:
            raise KeyboardInterrupt

        return prettyversions.index(selected)

    def ensure_data(self, flags=0b111):
        # ASK_MRPACK    = 0b1000
        # ASK_LOADER    = 0b100
        # ASK_MCVERSION = 0b10
        # ASK_PROJECTS  = 0b1

        if not self._projects and flags & 0b1:
            self.calculate_projects()

        if not self._mcversion and flags & 0b10:
            self.calculate_mcversion()

        if not self._loader and flags & 0b100:
            self.calculate_loader()

        if self._pack is True and flags & 0b1000:
            self.calculate_pack()

    def download(
        self,
        url,
        filename,
        broad=False,
        quiet=False,
        expected_hashes=None,
        record_error=True,
        _attempt=0,
    ):
        if self._cancel_event.is_set():
            return False

        try:
            if broad:
                root = Path(self._outputpath).parent.resolve()
                destination = self.safe_destination(root, filename)
            else:
                root = Path(self._outputpath).resolve()
                destination = self.safe_destination(
                    root, os.path.basename(filename)
                )

            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".part")

            if destination.is_file():
                valid = self.verify_hashes(
                    destination, expected_hashes
                )

                if valid and not expected_hashes:
                    try:
                        head = session.head(
                            url, timeout=10, allow_redirects=True
                        )
                        remote_size = int(
                            head.headers.get("content-length", 0)
                        )
                        valid = (
                            remote_size == 0
                            or destination.stat().st_size == remote_size
                        )
                    except requests.RequestException:
                        valid = False

                if valid:
                    logger.info(
                        f"Project {filename} already exists -> skipping..."
                    )
                    return True

                logger.warning(
                    f"Project {filename} failed validation -> re-downloading."
                )

            try:
                with session.get(
                    url, stream=True, timeout=(10, 10)
                ) as response:
                    response.raise_for_status()

                    total_size = int(
                        response.headers.get("content-length", 0)
                    )

                    logger.info(f"Starting download of {filename}.")

                    with tqdm(
                        total=total_size,
                        unit="B",
                        unit_scale=True,
                        disable=quiet,
                    ) as progress_bar:
                        with temporary.open("wb") as file:
                            for data in response.iter_content(64 * 1024):
                                if self._cancel_event.is_set():
                                    raise RinthDownInterrupt(
                                        "Download cancelled."
                                    )

                                if not data:
                                    continue

                                file.write(data)
                                progress_bar.update(len(data))

                if self._cancel_event.is_set():
                    return False

                downloaded_size = temporary.stat().st_size

                if total_size and downloaded_size != total_size:
                    raise RuntimeError(
                        f"Incomplete download: expected {total_size} bytes, "
                        f"got {downloaded_size}"
                    )

                if not self.verify_hashes(temporary, expected_hashes):
                    raise RuntimeError(
                        f"Hash verification failed for {filename}"
                    )

                if self._cancel_event.is_set():
                    return False

                os.replace(temporary, destination)
                return True

            finally:
                # Never leave an incomplete temporary download behind.
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as error:
                    logger.warning(
                        f"Could not remove temporary file {temporary}: {error}"
                    )

        except RinthDownInterrupt:
            return False

        except KeyboardInterrupt:
            self._cancel_event.set()
            raise

        except Exception as error:
            if self._cancel_event.is_set():
                return False

            logger.error(
                f"Download failed for {filename}: {error}"
            )

            if _attempt + 1 >= MAX_RETRIES or quiet:
                if record_error:
                    self._had_errors = True
                return False

            retry = questionary.confirm(
                f"Download failed for {filename}. Try again?"
            ).ask()

            if retry is None:
                self._cancel_event.set()
                raise KeyboardInterrupt

            if not retry:
                if record_error:
                    self._had_errors = True
                return False

            return self.download(
                url,
                filename,
                broad=broad,
                quiet=quiet,
                expected_hashes=expected_hashes,
                record_error=record_error,
                _attempt=_attempt + 1,
            )

    def download_any(
        self,
        urls,
        filename,
        expected_hashes=None,
        record_error=True,
    ):
        for url in urls:
            if self._cancel_event.is_set():
                return False

            if self.download(
                url,
                filename,
                broad=True,
                quiet=True,
                expected_hashes=expected_hashes,
                record_error=False,
            ):
                return True

            logger.warning(f"Mirror failed for {filename}: {url}")

        if record_error:
            self._had_errors = True
        return False

    def add_dependencies(self, choice):
        for dependency in choice["dependencies"]:
            if dependency["dependency_type"] in (
                "required",
                "embedded",
            ):
                project_id = dependency["project_id"]

                if project_id is None:
                    logger.warning(
                        "A required dependency is not on Modrinth, "
                        "install it manually."
                    )
                    continue

                self._dependencies.append(
                    (project_id, dependency["version_id"])
                )

    @staticmethod
    def handle_status_codes(
        status_code: int,
        projectname: str,
    ) -> bool:
        if status_code == 400:
            logger.error(
                "Invalid parameters. Please check mcversion and loaders."
            )
            raise RinthDownInterrupt

        elif status_code == 404:
            logger.error(
                f"Project {projectname} does not exist."
            )
            return True

        elif status_code == 410:
            logger.critical("API deprecated.")
            raise RinthDownAPIError

        elif status_code >= 500:
            logger.error("Server problems - try again later.")
            raise RinthDownAPIError

        elif status_code == 429:
            return False

        elif status_code != 200:
            logger.error(
                f"Something went wrong. Status-Code: {status_code}"
            )
            return True

        return False

    def init_installed(self):
        hash_to_file = {}

        for name in os.listdir(self._outputpath):
            if name.endswith(".jar"):
                path = os.path.join(self._outputpath, name)
                hash_to_file[hash_file(path)] = name

        if not hash_to_file:
            return

        url = "https://api.modrinth.com/v2/version_files"
        payload = {
            "hashes": list(hash_to_file),
            "algorithm": "sha512",
        }

        response = session.post(
            url,
            json=payload,
            timeout=10,
        )

        logger.debug(
            "API CALL: " + url + " " + json.dumps(payload)
        )

        response.raise_for_status()

        self._installed = [
            project["project_id"]
            for project in response.json().values()
        ]

    def fetch_versions(self, projectname):
        url = (
            f"https://api.modrinth.com/v2/project/"
            f"{projectname}/version"
        )

        params = {
            "game_versions": json.dumps([self._mcversion]),
            "loaders": json.dumps([self._loader]),
        }

        for attempt in range(MAX_RETRIES):
            response = session.get(
                url,
                params=params,
                timeout=10,
            )

            logger.debug(
                "API CALL: " + url + " " + json.dumps(params)
            )

            if response.status_code != 429:
                break

            try:
                wait = int(
                    response.headers.get("Retry-After", 10)
                )
            except ValueError:
                wait = 10

            logger.warning(
                f"Rate limited. Waiting {wait} seconds "
                f"({attempt + 1}/{MAX_RETRIES})."
            )
            time.sleep(wait + 1)

        else:
            logger.error(
                f"Still rate limited, skipping {projectname}."
            )
            return None

        if RinthDown.handle_status_codes(
            response.status_code,
            projectname,
        ):
            return None

        response.raise_for_status()
        return response.json()

    def run(self, project: tuple[str, str | None], versions=None):
        if self._cancel_event.is_set():
            return

        if not versions:
            versions = self.fetch_versions(project[0])

            if versions is None:
                return

        if not versions:
            logger.error(
                f"Did not find project {project[0]}."
            )
            return

        if project[1]:
            versions = [
                entry
                for entry in versions
                if project[1] in (
                    entry["version_number"],
                    entry["id"],
                )
            ]

            if not versions:
                logger.warning(
                    f"Did not find version {project[1]} "
                    f"of project {project[0]}."
                )

                retry = questionary.confirm(
                    "Continue with other, custom version?"
                ).ask()

                if retry is None:
                    raise KeyboardInterrupt

                if retry:
                    self.run((project[0], None))

                return

        if (
            versions[0].get("files")
            and str(versions[0]["files"][0]["filename"]).endswith(
                ".mrpack"
            )
        ):
            raise RinthDownError(
                "Do not use -p to download packs. "
                "Please use -mrpack parameter."
            )

        if versions[0]["project_id"] in self._installed:
            logger.info(
                f"Some project version of {versions[0]['name']} "
                "is already installed."
            )
            return

        choice = (
            versions[0]
            if project[1]
            else self.calculate_version(project[0], versions)
        )

        if not choice:
            logger.error(
                f"Couldn't find available version of project "
                f"{project[0]}"
            )
            return

        if not choice.get("files"):
            logger.error(
                f"Version of project {project[0]} has no files."
            )
            return

        success = self.download(
            choice["files"][0]["url"],
            choice["files"][0]["filename"],
            expected_hashes=choice["files"][0].get("hashes"),
        )

        if success:
            self._installed.append(choice["project_id"])
            self.add_dependencies(choice)
        else:
            self._had_errors = True

    def mrpack_run(self):
        self.ensure_data(0b1000)

        pack_path = Path(self._pack).expanduser()

        if pack_path.is_file():
            parse = ParseResult(
                scheme="",
                netloc="",
                path=str(pack_path.resolve()),
                params="",
                query="",
                fragment="",
            )
        else:
            parse = urlparse(self._pack)

        if parse.scheme == "":
            if not str(parse.path).lower().endswith(".mrpack"):
                self.ensure_data(0b010)
                versions = self.fetch_versions(parse.path)

                if not versions:
                    logger.error(f"Did not find project {parse.path}")
                    return 1

                choice = self.calculate_version(parse.path, versions)

                if not choice or not choice.get("files"):
                    logger.error("Selected version has no files.")
                    return 1

                parse = urlparse(choice["files"][0]["url"])

        pack = None

        try:
            pack = ModrinthPack(parse)

            try:
                index = json.loads(
                    pack.read_file("modrinth.index.json")
                )
            except KeyError as error:
                raise ModrinthPackError(
                    "The modpack is missing modrinth.index.json."
                ) from error
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise ModrinthPackError(
                    "The modpack index contains invalid JSON."
                ) from error

            if not isinstance(index, dict) or not isinstance(
                index.get("files"), list
            ):
                raise ModrinthPackError(
                    "The modpack index has an invalid format."
                )

            root = Path(self._outputpath).parent.resolve()
            executor = ThreadPoolExecutor(max_workers=self._workers)
            futures = []

            try:
                for entry in index["files"]:
                    if self._cancel_event.is_set():
                        break

                    if not isinstance(entry, dict):
                        raise ModrinthPackError(
                            "Invalid file entry in modpack index."
                        )

                    relative_path = entry.get("path")
                    downloads = entry.get("downloads")
                    hashes = entry.get("hashes", {})

                    if (
                        not isinstance(relative_path, str)
                        or not isinstance(downloads, list)
                        or not downloads
                        or not isinstance(hashes, dict)
                    ):
                        raise ModrinthPackError(
                            "Invalid path, downloads or hashes in modpack index."
                        )

                    destination = self.safe_destination(
                        root, relative_path
                    )

                    if self._server:
                        first_directory = Path(relative_path).parts[0]

                        if first_directory not in ("mods", "datapacks"):
                            continue

                    destination.parent.mkdir(
                        parents=True, exist_ok=True
                    )

                    # Mirrors are fallbacks, not parallel sources:
                    # validate all of them, then submit ONE future per file.
                    urls = []

                    for download_url in downloads:
                        if not isinstance(download_url, str):
                            raise ModrinthPackError(
                                "Invalid download URL in modpack index."
                            )

                        if urlparse(download_url).scheme not in (
                            "http", "https"
                        ):
                            raise ModrinthPackError(
                                "Invalid download URL scheme in modpack index."
                            )

                        urls.append(download_url)

                    # First pass: don't record errors yet, the retry pass decides.
                    futures.append((
                        executor.submit(
                            self.download_any,
                            urls,
                            relative_path,
                            hashes,
                            False,
                        ),
                        urls,
                        relative_path,
                        hashes,
                    ))

                retry_list = []

                for future, urls, relative_path, hashes in futures:
                    if self._cancel_event.is_set():
                        break

                    try:
                        ok = future.result()
                    except Exception:
                        logger.exception("A download worker failed.")
                        ok = False

                    if not ok:
                        retry_list.append((urls, relative_path, hashes))

                if retry_list and not self._cancel_event.is_set():
                    logger.warning(
                        f"Retrying {len(retry_list)} failed download(s)..."
                    )
                    time.sleep(2)  # small backoff

                    retry_futures = [
                        (
                            executor.submit(
                                self.download_any,
                                urls,
                                path,
                                hashes,
                                True,
                            ),
                            path,
                        )
                        for urls, path, hashes in retry_list
                    ]

                    still_failed = []

                    for future, path in retry_futures:
                        try:
                            if not future.result():
                                still_failed.append(path)
                        except Exception:
                            logger.exception("A download worker failed.")
                            still_failed.append(path)

                    if still_failed:
                        self._cancel_event.set()
                        executor.shutdown(
                            wait=True, cancel_futures=True
                        )
                        logger.error(
                            "Failed even after retry: "
                            + ", ".join(still_failed)
                        )
                        logger.error("Overrides will not be copied.")
                        return 1

            except BaseException:
                self._cancel_event.set()
                executor.shutdown(
                    wait=True, cancel_futures=True
                )
                raise
            else:
                executor.shutdown(wait=True)

            if self._cancel_event.is_set():
                return 1

            overrides_root = Path(self._outputpath).parent.resolve()

            for name in pack.zip.namelist():
                if not name.startswith("overrides/"):
                    continue

                relative = name.removeprefix("overrides/")

                if not relative or name.endswith("/"):
                    continue

                destination = self.safe_destination(
                    overrides_root, relative
                )

                if self._server and Path(relative).parts[0] not in (
                    "mods", "datapacks", "config"
                ):
                    continue

                if self._cancel_event.is_set():
                    return 1

                logger.info(f"Copying project {relative}.")

                destination.parent.mkdir(
                    parents=True, exist_ok=True
                )

                # Resolve again after creating parent directories to catch
                # existing symlinks in the destination path.
                destination = self.safe_destination(
                    overrides_root, relative
                )

                destination.write_bytes(
                    pack.read_file(name)
                )

            logger.info("Exiting.")
            return 1 if self._had_errors else 0

        except BadZipFile as error:
            raise ModrinthPackError(
                "The provided file is not a valid modpack ZIP."
            ) from error
        finally:
            if pack is not None and pack.zip is not None:
                pack.zip.close()

    def run_all(self):
        self.init_installed()

        if self._pack is not None:
            return self.mrpack_run()

        self.ensure_data()

        if not self._projects:
            logger.error("No projects were given.")
            return 1

        while self._projects:
            if self._cancel_event.is_set():
                return 1

            self.run(self._projects.pop())

        while self._dependencies:
            if self._cancel_event.is_set():
                return 1

            self.run(self._dependencies.pop())

        logger.info("Exiting.")
        return 1 if self._had_errors else 0


def parse_project(arg: str) -> tuple[str, str | None]:
    if "@" in arg:
        name, ver = arg.split("@", 1)
        return (name, ver)

    return (arg, None)


def get_args():
    parent_parser = argparse.ArgumentParser("rinthdown")

    client_or_server_parser = (
        parent_parser.add_mutually_exclusive_group()
    )

    client_or_server_parser.add_argument(
        "-s",
        "--isserver",
        action="store_true",
    )

    client_or_server_parser.add_argument(
        "-c",
        "--isclient",
        action="store_true",
    )

    parent_parser.add_argument(
        "-mcv",
        "--mcversion",
        type=str,
    )

    parent_parser.add_argument(
        "-l",
        "--loader",
        type=str,
    )

    release_parser = (
        parent_parser.add_mutually_exclusive_group()
    )

    release_parser.add_argument(
        "-stable",
        "--stable",
        action="store_true",
    )

    release_parser.add_argument(
        "-latest",
        "--latest",
        action="store_true",
    )

    parent_parser.add_argument(
        "-p",
        "--projects",
        type=parse_project,
        nargs="+",
    )

    parent_parser.add_argument(
        "-mrpack",
        "--mrpack",
        type=str,
        nargs="?",
        const=True,
    )

    parent_parser.add_argument(
        "--verbose",
        action="store_true",
    )

    parent_parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {APP_VERSION}",
    )

    args = parent_parser.parse_args()

    if args.mrpack and args.projects:
        parent_parser.error(
            "-mrpack cannot be used with "
            "[-p PROJECTS [PROJECTS ...]]"
        )

    if not args.isclient and not args.isserver:
        args.isserver = True

    logger.setLevel(
        logging.DEBUG
        if args.verbose
        else logging.INFO
    )

    return args


def main():
    args = get_args()

    try:
        app = RinthDown(args)
        return app.run_all()

    except KeyboardInterrupt:
        logger.info("Aborting.")

    except RinthDownInterrupt:
        logger.info("Aborting.")

    except (RinthDownError, ModrinthPackError) as e:
        message = str(e)

        if message == "A fatal error has occured.":
            logger.critical("Unexpected error")
        else:
            logger.critical(f"Unexpected error: {message}")

    except requests.RequestException as e:
        logger.critical(f"RequestException: {e}")

    return 1


if __name__ == "__main__":
    raise SystemExit(main())