import tomllib
from pathlib import Path


def test_wheel_force_include_does_not_duplicate_packaged_files() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    force_include = wheel.get("force-include", {})

    for package in wheel.get("packages", []):
        package_path = Path(package)
        package_name = package_path.name

        for source, destination in force_include.items():
            source_path = Path(source)
            destination_path = Path(destination)

            assert not (
                source_path.is_relative_to(package_path)
                and destination_path.is_relative_to(package_name)
            ), (
                f"{source!r} is already included by package {package!r}; "
                f"force-including it as {destination!r} duplicates wheel files"
            )
