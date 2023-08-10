"""
Hardhat platform
"""
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from crytic_compile.compiler.compiler import CompilerVersion
from crytic_compile.platform.exceptions import InvalidCompilation
from crytic_compile.platform.types import Type
from crytic_compile.utils.naming import convert_filename, extract_name
from crytic_compile.utils.natspec import Natspec
from crytic_compile.utils.subprocess import run
from crytic_compile.platform.abstract_platform import AbstractPlatform, PlatformConfig

# Handle cycle
from crytic_compile.platform.solc import relative_to_short
from crytic_compile.compilation_unit import CompilationUnit

if TYPE_CHECKING:
    from crytic_compile import CryticCompile

LOGGER = logging.getLogger("CryticCompile")

# pylint: disable=too-many-locals
def hardhat_like_parsing(
    crytic_compile: "CryticCompile", target: str, build_directory: Path, working_dir: str
) -> None:
    """
    This function parse the output generated by hardhat.
    It can be re-used by any platform that follows the same schema (ex:foudnry)


    Args:
        crytic_compile: CryticCompile object
        target: target
        build_directory: build directory
        working_dir: working directory

    Raises:
        InvalidCompilation: If hardhat failed to run

    """
    if not os.path.isdir(build_directory):
        txt = (
            f"Compilation failed. Can you run build command?\n{build_directory} is not a directory."
        )
        raise InvalidCompilation(txt)

    files = sorted(
        os.listdir(build_directory), key=lambda x: os.path.getmtime(Path(build_directory, x))
    )
    files = [str(f) for f in files if str(f).endswith(".json")]
    if not files:
        txt = f"Compilation failed. Can you run build command?\n{build_directory} is empty."
        raise InvalidCompilation(txt)

    for file in files:
        build_info = Path(build_directory, file)

        # The file here should always ends .json, but just in case use ife
        uniq_id = file if ".json" not in file else file[0:-5]
        compilation_unit = CompilationUnit(crytic_compile, uniq_id)

        with open(build_info, encoding="utf8") as file_desc:
            loaded_json = json.load(file_desc)

            targets_json = loaded_json["output"]

            version_from_config = loaded_json["solcVersion"]  # TODO supper vyper
            input_json = loaded_json["input"]
            compiler = "solc" if input_json["language"] == "Solidity" else "vyper"
            optimized = input_json["settings"]["optimizer"]["enabled"]

            compilation_unit.compiler_version = CompilerVersion(
                compiler=compiler, version=version_from_config, optimized=optimized
            )

            skip_filename = compilation_unit.compiler_version.version in [
                f"0.4.{x}" for x in range(0, 10)
            ]

            if "sources" in targets_json:
                for path, info in targets_json["sources"].items():
                    if skip_filename:
                        path = convert_filename(
                            target,
                            relative_to_short,
                            crytic_compile,
                            working_dir=working_dir,
                        )
                    else:
                        path = convert_filename(
                            path,
                            relative_to_short,
                            crytic_compile,
                            working_dir=working_dir,
                        )

                    source_unit = compilation_unit.create_source_unit(path)
                    source_unit.ast = info.get("ast", info.get("legacyAST"))
                    if source_unit.ast is None:
                        raise InvalidCompilation(
                            f"AST not found for {path} in {build_info} directory"
                        )

            if "contracts" in targets_json:
                for original_filename, contracts_info in targets_json["contracts"].items():

                    filename = convert_filename(
                        original_filename,
                        relative_to_short,
                        crytic_compile,
                        working_dir=working_dir,
                    )

                    source_unit = compilation_unit.create_source_unit(filename)

                    for original_contract_name, info in contracts_info.items():
                        contract_name = extract_name(original_contract_name)

                        source_unit.add_contract_name(contract_name)
                        compilation_unit.filename_to_contracts[filename].add(contract_name)

                        source_unit.abis[contract_name] = info["abi"]
                        source_unit.bytecodes_init[contract_name] = info["evm"]["bytecode"][
                            "object"
                        ]
                        source_unit.bytecodes_runtime[contract_name] = info["evm"][
                            "deployedBytecode"
                        ]["object"]
                        source_unit.srcmaps_init[contract_name] = info["evm"]["bytecode"][
                            "sourceMap"
                        ].split(";")
                        source_unit.srcmaps_runtime[contract_name] = info["evm"][
                            "deployedBytecode"
                        ]["sourceMap"].split(";")
                        userdoc = info.get("userdoc", {})
                        devdoc = info.get("devdoc", {})
                        natspec = Natspec(userdoc, devdoc)
                        source_unit.natspec[contract_name] = natspec


class Hardhat(AbstractPlatform):
    """
    Hardhat platform
    """

    NAME = "Hardhat"
    PROJECT_URL = "https://github.com/nomiclabs/hardhat"
    TYPE = Type.HARDHAT

    def compile(self, crytic_compile: "CryticCompile", **kwargs: str) -> None:
        """Run the compilation

        Args:
            crytic_compile (CryticCompile): Associated CryticCompile object
            **kwargs: optional arguments. Used: "hardhat_ignore", "hardhat_ignore_compile", "ignore_compile",
                "hardhat_artifacts_directory","hardhat_working_dir","npx_disable"

        """

        hardhat_ignore_compile, base_cmd = self._settings(kwargs)

        detected_paths = self._get_hardhat_paths(base_cmd, kwargs)

        build_directory = Path(
            self._target,
            detected_paths["artifacts"],
            "build-info",
        )

        hardhat_working_dir = str(Path(self._target, detected_paths["root"]))

        if not hardhat_ignore_compile:
            cmd = base_cmd + ["compile", "--force"]
            run(cmd, cwd=self._target)

        hardhat_like_parsing(crytic_compile, self._target, build_directory, hardhat_working_dir)

    def clean(self, **kwargs: str) -> None:
        """Clean compilation artifacts

        Args:
            **kwargs: optional arguments.
        """

        hardhat_ignore_compile, base_cmd = self._settings(kwargs)

        if hardhat_ignore_compile:
            return

        for clean_cmd in [["clean"], ["clean", "--global"]]:
            run(base_cmd + clean_cmd, cwd=self._target)

    @staticmethod
    def is_supported(target: str, **kwargs: str) -> bool:
        """Check if the target is an hardhat project

        Args:
            target (str): path to the target
            **kwargs: optional arguments. Used: "hardhat_ignore"

        Returns:
            bool: True if the target is an hardhat project
        """
        hardhat_ignore = kwargs.get("hardhat_ignore", False)
        if hardhat_ignore:
            return False

        return (
            os.path.isfile(os.path.join(target, "hardhat.config.js"))
            or os.path.isfile(os.path.join(target, "hardhat.config.ts"))
            or os.path.isfile(os.path.join(target, "hardhat.config.cjs"))
        )

    @staticmethod
    def config(working_dir: str) -> Optional[PlatformConfig]:
        """Return configuration data that should be passed to solc, such as remappings.

        Args:
            working_dir (str): path to the working directory

        Returns:
            Optional[PlatformConfig]: Platform configuration data such as optimization, remappings...
        """
        return None

    def is_dependency(self, path: str) -> bool:
        """Check if the path is a dependency

        Args:
            path (str): path to the target

        Returns:
            bool: True if the target is a dependency
        """
        if path in self._cached_dependencies:
            return self._cached_dependencies[path]
        ret = "node_modules" in Path(path).parts
        self._cached_dependencies[path] = ret
        return ret

    def _guessed_tests(self) -> List[str]:
        """Guess the potential unit tests commands

        Returns:
            List[str]: The guessed unit tests commands
        """
        return ["hardhat test"]

    @staticmethod
    def _settings(args: Dict[str, Any]) -> Tuple[bool, List[str]]:
        hardhat_ignore_compile = args.get("hardhat_ignore_compile", False) or args.get(
            "ignore_compile", False
        )

        base_cmd = ["hardhat"]
        if not args.get("npx_disable", False):
            base_cmd = ["npx"] + base_cmd

        return hardhat_ignore_compile, base_cmd

    def _get_hardhat_paths(
        self, base_cmd: List[str], args: Dict[str, str]
    ) -> Dict[str, Union[Path, str]]:
        """Obtain hardhat configuration paths, defaulting to the
        standard config if needed.

        Args:
            base_cmd ([str]): hardhat command
            args (Dict[str, str]): crytic-compile options that may affect paths

        Returns:
            Dict[str, str]: hardhat paths configuration
        """
        target_path = Path(self._target).resolve()
        default_paths = {
            "root": target_path,
            "configFile": target_path.joinpath("hardhat.config.js"),
            "sources": target_path.joinpath("contracts"),
            "cache": target_path.joinpath("cache"),
            "artifacts": target_path.joinpath("artifacts"),
            "tests": target_path.joinpath("test"),
        }
        override_paths = {}

        if args.get("hardhat_cache_directory", None):
            override_paths["cache"] = Path(target_path, args["hardhat_cache_directory"])

        if args.get("hardhat_artifacts_directory", None):
            override_paths["artifacts"] = Path(target_path, args["hardhat_artifacts_directory"])

        if args.get("hardhat_working_dir", None):
            override_paths["root"] = Path(target_path, args["hardhat_working_dir"])

        print_paths = "console.log(JSON.stringify(config.paths));process.exit()"

        try:
            config_str = self._run_hardhat_console(base_cmd, print_paths)
            paths = json.loads(config_str or "{}")
            return {**default_paths, **paths, **override_paths}
        except ValueError as e:
            LOGGER.info("Problem deserializing hardhat configuration, using defaults: %s", e)
        except (OSError, subprocess.SubprocessError) as e:
            LOGGER.info("Problem executing hardhat to fetch configuration, using defaults: %s", e)

        return {**default_paths, **override_paths}

    def _run_hardhat_console(self, base_cmd: List[str], command: str) -> Optional[str]:
        """Run a JS command in the hardhat console

        Args:
            base_cmd ([str]): hardhat command
            command (str): console command to run

        Returns:
            Optional[str]: command output if execution succeeds
        """
        with subprocess.Popen(
            base_cmd + ["console", "--no-compile"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self._target,
            executable=shutil.which(base_cmd[0]),
        ) as process:
            stdout_bytes, stderr_bytes = process.communicate(command.encode("utf-8"))
            stdout, stderr = (
                stdout_bytes.decode(),
                stderr_bytes.decode(errors="backslashreplace"),
            )

            if stderr:
                LOGGER.info("Problem executing hardhat: %s", stderr)
                return None

            return stdout
