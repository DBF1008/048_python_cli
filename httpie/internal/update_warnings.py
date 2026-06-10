import json
from contextlib import nullcontext, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Callable

import requests

import httpie
from httpie.context import Environment, LogLevel
from httpie.internal.__build_channel__ import BUILD_CHANNEL
from httpie.internal.daemons import spawn_daemon
from httpie.utils import (
    is_version_greater,
    acquire_lockfile,
    atomic_write_json,
)

# Automatically updated package version index.
PACKAGE_INDEX_LINK = 'https://packages.httpie.io/latest.json'

FETCH_INTERVAL = timedelta(weeks=2)
WARN_INTERVAL = timedelta(weeks=1)

UPDATE_MESSAGE_FORMAT = """\
A new HTTPie release ({last_released_version}) is available.
To see how you can update, please visit https://httpie.io/docs/cli/{installation_method}
"""

ALREADY_UP_TO_DATE_MESSAGE = """\
You are already up-to-date.
"""

# Schema for version_info.json with safe defaults.
# Used by _validate_version_info() to fill in missing fields.
_VERSION_INFO_DEFAULTS = {
    'last_warned_date': None,        # None = never warned
    'last_fetched_date': None,       # None = never fetched (triggers fetch)
    'last_released_versions': {},    # Empty = no known versions
}


def _get_defaults() -> dict:
    """Return a fresh copy of version_info defaults (deep enough to
    avoid sharing mutable values across calls)."""
    return {
        'last_warned_date': None,
        'last_fetched_date': None,
        'last_released_versions': {},
    }


def _validate_version_info(data: dict) -> dict:
    """Ensure data has all expected fields with correct types.

    For each field, if the value is missing or has the wrong type,
    replace it with the default. This prevents KeyError/ValueError
    downstream when fields are absent due to partial writes or
    concurrent access.
    """
    defaults = _get_defaults()
    result = {}
    for key in _VERSION_INFO_DEFAULTS:
        value = data.get(key)
        default = defaults[key]

        if value is not None and isinstance(value, type(default)):
            # Value present with the expected non-None type
            result[key] = value
        elif default is None:
            # Fields whose default is None accept None or str
            if value is None or isinstance(value, str):
                result[key] = value
            else:
                result[key] = None
        else:
            # Value is None or wrong type — fall back to fresh default
            result[key] = default
    return result


def _read_version_info(file: Path) -> dict:
    """Read version_info.json, returning a dict with all expected fields.

    Handles:
    - File not found -> returns defaults
    - Invalid JSON -> returns defaults
    - Valid JSON but not a dict -> returns defaults
    - Valid JSON but missing fields -> fills in defaults
    - Valid JSON but wrong types for fields -> fills in defaults
    """
    try:
        with open(file) as stream:
            raw = json.load(stream)
    except (ValueError, OSError):
        return _get_defaults()

    if not isinstance(raw, dict):
        return _get_defaults()

    return _validate_version_info(raw)


def _fetch_updates(env: Environment) -> str:
    """Fetch latest version info from the package index and persist it.

    The HTTP request is performed outside the lock to avoid holding the
    lock during potentially slow network I/O. A lock is acquired only
    for the brief read-merge-write cycle to prevent TOCTOU races.
    Uses atomic write to prevent partial-write corruption.
    """
    file = env.config.version_info_file

    # Pre-fetch: read existing data (no lock, stale read is acceptable
    # since we re-read inside the lock below)
    response = requests.get(PACKAGE_INDEX_LINK, verify=False)
    response.raise_for_status()

    versions = response.json()
    if not isinstance(versions, dict):
        return  # Unexpected response format; skip silently

    with acquire_lockfile(file):
        # Re-read inside the lock to get the latest last_warned_date
        current = _read_version_info(file)
        current['last_fetched_date'] = datetime.now().isoformat()
        current['last_released_versions'] = versions
        # last_warned_date is preserved from _read_version_info

        atomic_write_json(file, current)


def fetch_updates(env: Environment, lazy: bool = True):
    if lazy:
        spawn_daemon('fetch_updates')
    else:
        _fetch_updates(env)


def maybe_fetch_updates(env: Environment) -> None:
    """Trigger a background fetch if enough time has passed since the last one.

    Reads without a lock — this is a throttle check, so a slightly stale
    read is acceptable. The worst case is an extra daemon spawn, which is
    harmless (the daemon itself uses a lock for the actual write).
    """
    if env.config.get('disable_update_warnings'):
        return None

    data = _read_version_info(env.config.version_info_file)

    last_fetched = data.get('last_fetched_date')
    if last_fetched is not None:
        try:
            last_fetched_date = datetime.fromisoformat(last_fetched)
        except (ValueError, TypeError):
            # Malformed date — treat as never fetched, trigger a fetch
            last_fetched_date = None

        if last_fetched_date is not None:
            earliest_fetch_date = last_fetched_date + FETCH_INTERVAL
            if datetime.now() < earliest_fetch_date:
                return None

    # Either no data, never fetched, or interval has passed
    fetch_updates(env)


def _get_suppress_context(env: Environment) -> Any:
    """Return a context manager that suppress
    all possible errors.

    Note: if you have set the developer_mode=True in
    your config, then it will show all errors for easier
    debugging."""
    if env.config.developer_mode:
        return nullcontext()
    else:
        return suppress(BaseException)


def _update_checker(
    func: Callable[[Environment], None]
) -> Callable[[Environment], None]:
    """Control the execution of the update checker (suppress errors, trigger
    auto updates etc.)"""

    def wrapper(env: Environment) -> None:
        with _get_suppress_context(env):
            func(env)

        with _get_suppress_context(env):
            maybe_fetch_updates(env)

    return wrapper


def _get_update_status(env: Environment) -> Optional[str]:
    """If there is a new update available, return the warning text.
    Otherwise just return None."""
    file = env.config.version_info_file
    if not file.exists():
        return None

    with _get_suppress_context(env):
        # If the user quickly spawns multiple httpie processes
        # we don't want to end in a race.
        with acquire_lockfile(file):
            version_info = _read_version_info(file)

        available_channels = version_info.get('last_released_versions', {})
        if not isinstance(available_channels, dict):
            return None

        if BUILD_CHANNEL not in available_channels:
            return None

        current_version = httpie.__version__
        last_released_version = available_channels[BUILD_CHANNEL]
        if not isinstance(last_released_version, str):
            return None

        if not is_version_greater(last_released_version, current_version):
            return None

        text = UPDATE_MESSAGE_FORMAT.format(
            last_released_version=last_released_version,
            installation_method=BUILD_CHANNEL,
        )
        return text


def get_update_status(env: Environment) -> str:
    return _get_update_status(env) or ALREADY_UP_TO_DATE_MESSAGE


@_update_checker
def check_updates(env: Environment) -> None:
    """Check for updates and warn if appropriate.

    Uses a single lock acquisition for the entire read-check-write cycle
    to prevent TOCTOU races between concurrent processes.
    """
    if env.config.get('disable_update_warnings'):
        return None

    file = env.config.version_info_file
    if not file.exists():
        return None

    with acquire_lockfile(file):
        # === BEGIN CRITICAL SECTION (single lock) ===

        # Step 1: Read and validate
        version_info = _read_version_info(file)

        # Step 2: Check if an update is available
        available_channels = version_info.get('last_released_versions', {})
        if not isinstance(available_channels, dict):
            return None

        if BUILD_CHANNEL not in available_channels:
            return None

        current_version = httpie.__version__
        last_released_version = available_channels[BUILD_CHANNEL]
        if not isinstance(last_released_version, str):
            return None

        if not is_version_greater(last_released_version, current_version):
            return None

        # Step 3: Check throttle (WARN_INTERVAL)
        current_date = datetime.now()
        last_warned_date = version_info.get('last_warned_date')
        if last_warned_date is not None:
            try:
                last_warned_dt = datetime.fromisoformat(last_warned_date)
                earliest_warn_date = last_warned_dt + WARN_INTERVAL
                if current_date < earliest_warn_date:
                    return None
            except (ValueError, TypeError):
                # Malformed date — allow the warning through
                pass

        # Step 4: Emit warning
        update_status = UPDATE_MESSAGE_FORMAT.format(
            last_released_version=last_released_version,
            installation_method=BUILD_CHANNEL,
        )
        env.log_error(update_status, level=LogLevel.INFO)

        # Step 5: Update last_warned_date and write back atomically
        version_info['last_warned_date'] = current_date.isoformat()
        atomic_write_json(file, version_info)

        # === END CRITICAL SECTION ===
