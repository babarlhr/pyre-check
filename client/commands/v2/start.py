# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import dataclasses
import enum
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from ... import (
    command_arguments,
    commands,
    configuration as configuration_module,
    find_directories,
    log,
)


LOG: logging.Logger = logging.getLogger(__name__)


class MatchPolicy(enum.Enum):
    BASE_NAME = "base_name"
    FULL_PATH = "full_path"

    def __str__(self) -> str:
        return self.value


@dataclasses.dataclass(frozen=True)
class CriticalFile:
    policy: MatchPolicy
    path: str

    def serialize(self) -> Dict[str, str]:
        return {str(self.policy): self.path}


@dataclasses.dataclass(frozen=True)
class LoadSavedStateFromFile:
    shared_memory_path: str
    changed_files_path: Optional[str] = None

    def serialize(self) -> Tuple[str, Dict[str, str]]:
        return (
            "load_from_file",
            {
                "shared_memory_path": self.shared_memory_path,
                **(
                    {}
                    if self.changed_files_path is None
                    else {"changed_files_path": self.changed_files_path}
                ),
            },
        )


@dataclasses.dataclass(frozen=True)
class LoadSavedStateFromProject:
    project_name: str
    project_metadata: Optional[str] = None

    def serialize(self) -> Tuple[str, Dict[str, str]]:
        return (
            "load_from_project",
            {
                "project_name": self.project_name,
                **(
                    {}
                    if self.project_metadata is None
                    else {"project_metadata": self.project_metadata}
                ),
            },
        )


SavedStateAction = Union[LoadSavedStateFromFile, LoadSavedStateFromProject]


@dataclasses.dataclass(frozen=True)
class Arguments:
    """
    Data structure for configuration options the backend server can recognize.
    Need to keep in sync with `pyre/new_server/serverConfiguration.mli`
    """

    log_path: str
    global_root: str

    checked_directory_allowlist: Sequence[str] = dataclasses.field(default_factory=list)
    checked_directory_blocklist: Sequence[str] = dataclasses.field(default_factory=list)
    critical_files: Sequence[CriticalFile] = dataclasses.field(default_factory=list)
    debug: bool = False
    excludes: Sequence[str] = dataclasses.field(default_factory=list)
    extensions: Sequence[str] = dataclasses.field(default_factory=list)
    local_root: Optional[str] = None
    number_of_workers: int = 1
    parallel: bool = True
    saved_state_action: Optional[SavedStateAction] = None
    search_paths: Sequence[configuration_module.SearchPathElement] = dataclasses.field(
        default_factory=list
    )
    show_error_traces: bool = False
    source_paths: Sequence[str] = dataclasses.field(default_factory=list)
    store_type_check_resolution: bool = False
    strict: bool = False
    taint_models_path: Sequence[str] = dataclasses.field(default_factory=list)
    watchman_root: Optional[str] = None

    def serialize(self) -> Dict[str, Any]:
        return {
            "source_paths": self.source_paths,
            "search_paths": [
                element.command_line_argument() for element in self.search_paths
            ],
            "excludes": self.excludes,
            "checked_directory_allowlist": self.checked_directory_allowlist,
            "checked_directory_blocklist": self.checked_directory_blocklist,
            "extensions": self.extensions,
            "log_path": self.log_path,
            "global_root": self.global_root,
            **({} if self.local_root is None else {"local_root": self.local_root}),
            **(
                {}
                if self.watchman_root is None
                else {"watchman_root": self.watchman_root}
            ),
            "taint_model_paths": self.taint_models_path,
            "debug": self.debug,
            "strict": self.strict,
            "show_error_traces": self.show_error_traces,
            "critical_files": [
                critical_file.serialize() for critical_file in self.critical_files
            ],
            **(
                {}
                if self.saved_state_action is None
                else {"saved_state_action": self.saved_state_action.serialize()}
            ),
            "store_type_check_resolution": self.store_type_check_resolution,
            "parallel": self.parallel,
            "number_of_workers": self.number_of_workers,
        }


def get_critical_files(
    configuration: configuration_module.Configuration,
) -> List[CriticalFile]:
    def get_full_path(root: str, relative: str) -> str:
        full_path = (Path(root) / relative).resolve(strict=False)
        if not full_path.exists():
            LOG.warning(f"Critical file does not exist: {full_path}")
        return str(full_path)

    local_root = configuration.local_root
    return [
        CriticalFile(
            policy=MatchPolicy.FULL_PATH,
            path=get_full_path(
                root=configuration.project_root,
                relative=find_directories.CONFIGURATION_FILE,
            ),
        ),
        *(
            []
            if local_root is None
            else [
                CriticalFile(
                    policy=MatchPolicy.FULL_PATH,
                    path=get_full_path(
                        root=local_root,
                        relative=find_directories.LOCAL_CONFIGURATION_FILE,
                    ),
                )
            ]
        ),
        *(
            [
                CriticalFile(
                    policy=MatchPolicy.FULL_PATH,
                    path=get_full_path(root=path, relative=""),
                )
                for path in configuration.other_critical_files
            ]
        ),
    ]


def get_saved_state_action(
    start_arguments: command_arguments.StartArguments,
    relative_local_root: Optional[str] = None,
) -> Optional[SavedStateAction]:
    # Loading states from file takes precedence
    saved_state_file = start_arguments.load_initial_state_from
    if saved_state_file is not None:
        return LoadSavedStateFromFile(
            shared_memory_path=saved_state_file,
            changed_files_path=start_arguments.changed_files_path,
        )

    saved_state_project = start_arguments.saved_state_project
    if saved_state_project is not None:
        return LoadSavedStateFromProject(
            project_name=saved_state_project,
            project_metadata=relative_local_root.replace("/", "$")
            if relative_local_root is not None
            else None,
        )

    return None


def find_watchman_root(base: Path) -> Optional[Path]:
    return find_directories.find_parent_directory_containing_file(
        base, ".watchmanconfig"
    )


def create_server_arguments(
    configuration: configuration_module.Configuration,
    start_arguments: command_arguments.StartArguments,
) -> Arguments:
    """
    Translate client configurations and command-line flags to server
    configurations.

    This API is not pure since it needs to access filesystem to filter out
    nonexistent directories. It is idempotent though, since it does not alter
    any filesystem state.
    """
    source_directories = configuration.source_directories or []
    if len(source_directories) == 0:
        raise configuration_module.InvalidConfiguration(
            "New server does not have buck support yet."
        )
    return Arguments(
        log_path=configuration.log_directory,
        global_root=configuration.project_root,
        checked_directory_allowlist=(
            configuration.get_existent_do_not_ignore_errors_in_paths()
        ),
        checked_directory_blocklist=(
            configuration.get_existent_ignore_all_errors_paths()
        ),
        critical_files=get_critical_files(configuration),
        debug=start_arguments.debug,
        excludes=configuration.excludes,
        extensions=configuration.get_valid_extensions(),
        local_root=configuration.local_root,
        number_of_workers=configuration.get_number_of_workers(),
        parallel=not start_arguments.sequential,
        saved_state_action=get_saved_state_action(
            start_arguments, relative_local_root=configuration.relative_local_root
        ),
        search_paths=configuration.get_existent_search_paths(),
        show_error_traces=start_arguments.show_error_traces,
        source_paths=source_directories,
        store_type_check_resolution=start_arguments.store_type_check_resolution,
        strict=configuration.strict,
        taint_models_path=configuration.taint_models_path,
        watchman_root=None
        if start_arguments.no_watchman
        else str(find_watchman_root(Path(configuration.project_root))),
    )


def _write_argument_file(arguments: Arguments, to_path: Path) -> None:
    LOG.info(f"Writing server startup configurations into {to_path}...")
    serialized_arguments = arguments.serialize()
    LOG.debug(f"Arguments:\n{json.dumps(serialized_arguments, indent=2)}")
    to_path.write_text(json.dumps(serialized_arguments))


def _run_in_foreground(command: Sequence[str], environment: Mapping[str, str]) -> int:
    # In foreground mode, we shell out to the backend server and block on it.
    # Server stdout/stderr will be forwarded to the current terminal.
    try:
        LOG.info("Starting server in the foreground...\n")
        result = subprocess.run(
            command, env=environment, stdout=None, stderr=None, universal_newlines=True
        )
        return result.returncode
    except KeyboardInterrupt:
        # Backend server will exit cleanly when receiving SIGINT.
        return 0


def _run_in_background(
    command: Sequence[str], environment: Mapping[str, str], log_directory: Path
) -> int:
    # In background mode, we asynchronously start the server with `Popen` and
    # detach it from the current process immediately with `start_new_session`.
    # Do not call `wait()` on the Popen object to avoid blocking.
    # Server stdout/stderr will be forwarded to dedicated log files.
    with open(str(log_directory / "server.stdout"), "a") as server_stdout, open(
        str(log_directory / "server.stderr"), "a"
    ) as server_stderr:
        subprocess.Popen(
            command,
            stdout=server_stdout,
            stderr=server_stderr,
            env=environment,
            start_new_session=True,
            universal_newlines=True,
        )
    log.stdout.write("Server is starting in the background.\n")
    return 0


def run(
    configuration: configuration_module.Configuration,
    start_arguments: command_arguments.StartArguments,
) -> commands.ExitCode:
    binary_location = configuration.get_binary_respecting_override()
    if binary_location is None:
        raise configuration_module.InvalidConfiguration(
            "Cannot locate a Pyre binary to run."
        )

    log_directory = Path(configuration.log_directory) / "new_server"
    log_directory.mkdir(parents=True, exist_ok=True)
    # Use distinct file name for different PIDs to avoid file write races from
    # multiple concurrent `pyre start` processes.
    argument_file_path = log_directory / f"arguments_{os.getpid()}.json"
    _write_argument_file(
        arguments=create_server_arguments(configuration, start_arguments),
        to_path=argument_file_path,
    )

    server_command = [binary_location, "newserver", str(argument_file_path)]
    server_environment = {
        **os.environ,
        # This is to make sure that backend server shares the socket root
        # directory with the client.
        # TODO: It might be cleaner to turn this into a configuration option
        # instead.
        "TMPDIR": tempfile.gettempdir(),
    }
    if start_arguments.terminal:
        return_code = _run_in_foreground(server_command, server_environment)
    else:
        return_code = _run_in_background(
            server_command, server_environment, log_directory
        )

    if return_code == 0:
        return commands.ExitCode.SUCCESS
    else:
        LOG.error(f"Server exited with non-zero return code: {return_code}")
        return commands.ExitCode.FAILURE
