"""The app, stamped by the newest released HRI Manager, with the manager's own code.

    python .github/manager_compat_check.py <manager checkout> [<repository root>]

HRI Manager (trailro/hass-remote-integration-manager) creates HRI instances as local apps from app/config.yaml, and
refuses a template with a key, or a value shape, it has not vetted (hrimgr.stamp.vet_template): the manager writes app
definitions with the Supervisor's manager role, so a new key needs a manager release that knows it.  0.1.1 refused
0.25.2's backup_pre, and every instance update stopped until the manager caught up.  This imports hrimgr.stamp from a
checkout of the manager (CI: its latest release; it needs PyYAML only) and runs, on this repository's app/config.yaml:

  - vet_template on each key alone, so the failure names every key the manager refuses;
  - parse_template, the manager's whole check of a template (vet_template among it);
  - stamp for a sample instance on both channels, with Bluetooth and without, and dump, what the manager writes.

The exit status is 1 when the manager refuses anything: release a manager that accepts it first.
"""

import pathlib
import sys

SAMPLE_NAME = "compat"  # a valid instance name (names.NAME_RE), not a reserved one
GIT_VERSION = "0.0.0-0123456789ab"  # what the manager stamps for a git build (names.GIT_VERSION_RE)
SOURCE = "the manager compatibility check"


def check(manager: pathlib.Path, root: pathlib.Path) -> list[str]:
    sys.path.insert(0, str(manager / "hri_manager"))
    import yaml
    from hrimgr import stamp

    raw = (root / "app" / "config.yaml").read_bytes()
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        return ["app/config.yaml is not a mapping"]
    problems = []
    for key, value in data.items():
        try:
            stamp.vet_template({key: value})
        except stamp.TemplateError as err:
            problems.append(f"release a manager that accepts {key} first ({err})")
    if problems:
        return problems
    try:
        template = stamp.parse_template(raw)
    except stamp.TemplateError as err:
        return [f"release a manager that accepts this app/config.yaml first ({err})"]
    for channel in ("release", "git"):
        version = str(template["version"]) if channel == "release" else GIT_VERSION
        for bluetooth in (False, True):
            try:
                stamp.dump(stamp.stamp(template, SAMPLE_NAME, version, channel, bluetooth=bluetooth), SOURCE)
            except (stamp.TemplateError, ValueError) as err:
                problems.append(f"release a manager that stamps this app/config.yaml first (channel {channel}, "
                                f"bluetooth {bluetooth}: {err})")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    manager = pathlib.Path(argv[1]).resolve()
    root = pathlib.Path(argv[2]).resolve() if len(argv) == 3 else pathlib.Path(__file__).resolve().parent.parent
    problems = check(manager, root)
    from hrimgr import VERSION

    for problem in problems:
        print(f"HRI Manager {VERSION}: {problem}", file=sys.stderr)
    if not problems:
        print(f"HRI Manager {VERSION} accepts and stamps app/config.yaml")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
